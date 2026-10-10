# -*- coding: utf-8 -*-
"""/shit 指令：立刻做一轮抖音搬运 —— 与 2h 自动搬运完全同一条管线。

    /shit

流程与自动搬运一致：收集发送池里达到合并门槛的作品 → 按猎奇指数降序拼
≤5min 合集（图 2s、封面 1s、视频 2~30s、简介字幕、背景乐连续）→ 发到
目标群 → 发完清发送池（标注池不动）。只有两点不同：

  * 不受 23:00-09:00 免打扰限制（手动指令随时执行）；
  * 队列里视频总时长 < 15s 时不发，回复"队列太少了，等一下再来看吧！"。

发送成功后不额外回文本（合集本身就是结果，与自动搬运行为一致）。
"""

import logging
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "tools"))

import video_merge  # noqa: E402
from douyin_forwarder import (  # noqa: E402
    collect_items,
    delete_all_media,
    load_config,
    record_forwarded,
    select_budget,
    send_one,
)

COMMAND = "/shit"
ALIASES = []

MIN_QUEUE_SECONDS = 15.0

_log = logging.getLogger(__name__)
_burst_lock = threading.Lock()


def handle(content, context):
    # /shit 不需要参数：args 为空/None 都照常执行（router 里 after 可能是 ''）
    #
    # 只在搬运目标群可用（2026-10-02 规则）：别的群里发 /shit
    # 不许触发搬运（以前会照拼照发进目标群，等于谁都能遥控搬运）。
    try:
        cfg = load_config()
    except Exception:
        return "搬运配置不可用"
    if ((context or {}).get("group") or "") != str(cfg.get("target") or ""):
        return "该群暂未启用搬屎功能。"

    # 同一时刻只允许一轮（手动连发 /shit 不叠加）
    if not _burst_lock.acquire(blocking=False):
        return "上一轮还在拼/发，等它完事再来"

    try:
        target = cfg["target"]
        watch_dir = Path(cfg["watch_dir"])
        manifest_path = str(watch_dir / "manifest.json")
        budget = float(cfg.get("max_compilation_seconds", 300))
        image_seconds = float(cfg.get("image_seconds", 2))
        max_segment = float(cfg.get("max_segment_seconds", 20))
        min_segment = float(cfg.get("min_segment_seconds", 2))
        min_age = float(cfg.get("min_file_age_seconds", 60))
        merge_min_ratio = float(cfg.get("merge_min_ratio", 2.0))
        merge_min_digg = int(cfg.get("merge_min_digg", 5))

        items = collect_items(watch_dir, manifest_path, min_age, image_seconds,
                              max_segment, merge_min_ratio, merge_min_digg,
                              min_segment)
        total = sum(float(it.get("cost") or 0) for it in items)
        if total < MIN_QUEUE_SECONDS:
            _log.info("[SHIT] queue too small: %d item(s), total %.0fs < %.0fs",
                      len(items), total, MIN_QUEUE_SECONDS)
            return "队列太少了，等一下再来看吧！"

        picked, picked_total = select_budget(items, budget)
        if not picked:
            return "队列太少了，等一下再来看吧！"
        _log.info("[SHIT] burst: %d item(s) queued (%.0fs), picked %d (%.0fs)",
                  len(items), total, len(picked), picked_total)

        build_dir = Path(tempfile.gettempdir()) / "douyin_compilation"
        build_dir.mkdir(parents=True, exist_ok=True)
        merged = build_dir / ("shit_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".mp4")
        merged.unlink(missing_ok=True)
        skipped = []
        compiled = video_merge.build_compilation(picked, merged, image_seconds,
                                                 max_segment, min_segment,
                                                 skipped=skipped)
        _log.info("[SHIT] compilation built %.0fs %.1fMB", compiled,
                  merged.stat().st_size / 1e6)
        if not send_one(cfg, target, merged):
            merged.unlink(missing_ok=True)
            return "发送失败了，媒体留着下一轮再发"
        record_forwarded(watch_dir, len(picked), compiled, group=target)
        delete_all_media(watch_dir)   # 与自动搬运一致：发完清发送池
        merged.unlink(missing_ok=True)
        _log.info("[SHIT] sent %d item(s) %.0fs to %s", len(picked), compiled, target)
        return None   # 合集本身就是结果
    except Exception as e:
        _log.exception("[SHIT] burst failed")
        return f"/shit 执行出错：{type(e).__name__}: {e}"
    finally:
        _burst_lock.release()
