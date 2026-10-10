# -*- coding: utf-8 -*-
"""本地缓存垃圾回收：定期清理 weflow 的图片/媒体缓存，防止磁盘被撑爆。

清理目标（全部是可再生的缓存，删了不影响功能，需要时前端会重新解密/导出）：
- ~/.weflow-core/image-cache/    图片解密缓存
- ~/.weflow-core/cache/api-media/  /api/v1/media 导出缓存（图片/语音/表情）

策略：mtime 超过 max_age_days（默认 7 天）的文件删除，顺带清理空目录。
启动时跑一次，之后每 6 小时跑一次。
"""

import os
import threading
import time

_INTERVAL_SECONDS = 6 * 3600

_CACHE_DIRS = [
    os.path.join(os.path.expanduser("~"), ".weflow-core", "image-cache"),
    os.path.join(os.path.expanduser("~"), ".weflow-core", "cache", "api-media"),
]

# 下载临时目录（download_file 的产物无人删除，语音失败残留也在这里）：
# 用更短的 1 天阈值单独清理
_TEMP_DOWNLOAD_DIR = os.path.join(os.path.expandvars(r"%TEMP%"), "WechatRobot")
_TEMP_MAX_AGE_DAYS = 1

_started = False


def _collect_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            yield os.path.join(dirpath, name)


def run_once(max_age_days=7):
    """立即执行一轮清理，返回 (删除文件数, 释放字节数)。"""
    deleted, freed = _run_cleanup(_CACHE_DIRS, max_age_days)
    # 下载临时目录残留（含 voice/ 子目录中发送失败的语音）用更短阈值
    t_deleted, t_freed = _run_cleanup([_TEMP_DOWNLOAD_DIR], _TEMP_MAX_AGE_DAYS)
    deleted += t_deleted
    freed += t_freed
    print(
        f"[GC] 缓存清理完成: 删除 {deleted} 个文件, 释放 {freed / 1024 / 1024:.1f} MB",
        flush=True,
    )
    return deleted, freed


def _run_cleanup(roots, max_age_days):
    cutoff = time.time() - max(1, int(max_age_days)) * 86400
    deleted = 0
    freed = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for path in _collect_files(root):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime > cutoff:
                continue
            try:
                os.remove(path)
                deleted += 1
                freed += st.st_size
            except OSError:
                continue
        # 清掉空目录（自底向上）
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            if dirpath == root:
                continue
            try:
                if not os.listdir(dirpath):
                    os.rmdir(dirpath)
            except OSError:
                continue
    return deleted, freed


def _loop(config):
    cfg = (config or {}).get("gc") or {}
    max_age_days = cfg.get("max_age_days", 7)
    run_once(max_age_days)
    while True:
        time.sleep(_INTERVAL_SECONDS)
        try:
            run_once(max_age_days)
        except Exception as exc:
            print(f"[GC ERROR] {type(exc).__name__}: {exc}", flush=True)


def start(config):
    """启动 GC 线程（幂等）。config.gc.enabled 默认为 True。"""
    global _started
    cfg = (config or {}).get("gc") or {}
    if not cfg.get("enabled", True):
        print("[GC] disabled in config", flush=True)
        return False
    if _started:
        return True
    _started = True
    threading.Thread(target=_loop, args=(config,), daemon=True).start()
    return True
