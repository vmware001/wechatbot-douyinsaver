import json
import os
import sys
import threading
import time


BASE_DIR = os.path.dirname(os.path.dirname(__file__))
_RESTARTING = False
_RESTART_LOCK = threading.Lock()

# 热重载宽限期：等在途 LLM 请求收尾的上限，读 config.json 顶层 reload_grace_seconds
_DEFAULT_GRACE_SECONDS = 25.0
_MAX_GRACE_SECONDS = 120.0
_GRACE_POLL_SECONDS = 0.2


_EXCLUDE_DIRS = {"__pycache__", "backup", ".venv", "venv", ".git", "node_modules", "weflow-core", "tools", "data"}

# config.json 也纳入监控：改配置（如 target_group）与改代码一样触发后端重启。
# 无重启循环风险：运行期没有模块回写 config.json（已全量排查：main.py 仅在
# token 缺失时写一次且发生在监控基线扫描之前；weflow-core 只在自身启动/向导时写，
# 后端重启不会反过来触发它；setup_wizard.py 是手动工具）。
_CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def _grace_seconds():
    """热重载宽限期上限（秒）：config.json 顶层 reload_grace_seconds，clamp 0~120。

    读不到就按默认值，绝不因为配置问题让重载链路上的代码抛异常。
    """
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            value = json.load(f).get("reload_grace_seconds")
    except Exception:
        return _DEFAULT_GRACE_SECONDS
    if value is None:
        return _DEFAULT_GRACE_SECONDS
    try:
        return max(0.0, min(float(value), _MAX_GRACE_SECONDS))
    except (TypeError, ValueError):
        return _DEFAULT_GRACE_SECONDS


def _in_flight_depth():
    """当前在途 LLM 任务 (占用中, 排队中)。

    core.llm_gate 延迟 import：本模块被 main.py 最先导入，顶层只依赖标准库，
    不引入与 core.* 的循环依赖。任何异常都当作"没有在途请求"——宽限等待
    不能变成重载卡死。
    """
    try:
        from core.llm_gate import get_active_gate
        gate = get_active_gate()
    except Exception:
        return (0, 0)
    if gate is None:
        return (0, 0)
    try:
        return gate.depth()
    except Exception:
        return (0, 0)


def _wait_for_in_flight(grace_seconds):
    """等在途 LLM 请求收尾，最多 grace_seconds 秒，返回实际等待秒数。

    超过上限就走（不算失败，调用方照常重启）：一条慢请求不能把热重载卡在
    那里，用户会以为 bot 死了。三个分支都打日志，方便事后从 run.log 判断
    这次重载有没有等在途请求、等了多久。
    """
    if grace_seconds <= 0:
        print("[HOT-RELOAD] reload_grace_seconds=0，不等在途请求", flush=True)
        return 0.0

    depth = _in_flight_depth()
    if depth == (0, 0):
        print(
            f"[HOT-RELOAD] 无在途请求，直接重启（宽限上限 {grace_seconds:.0f}s）",
            flush=True,
        )
        return 0.0

    started = time.monotonic()
    print(
        f"[HOT-RELOAD] 等待在途请求结束（最多 {grace_seconds:.0f}s）: "
        f"占用={depth[0]} 排队={depth[1]}",
        flush=True,
    )

    timed_out = False
    while True:
        time.sleep(_GRACE_POLL_SECONDS)
        if _in_flight_depth() == (0, 0):
            break
        if time.monotonic() - started >= grace_seconds:
            timed_out = True
            break

    waited = time.monotonic() - started
    if timed_out:
        print(f"[HOT-RELOAD] 宽限期到（已等 {waited:.1f}s），强制重启", flush=True)
    else:
        print(f"[HOT-RELOAD] 在途请求已结束（等了 {waited:.1f}s），继续重启", flush=True)
    return waited


def _scan_py_mtimes():
    mtimes = {}

    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [d for d in dirs if d not in _EXCLUDE_DIRS]

        for name in files:
            if not name.endswith(".py"):
                continue

            path = os.path.join(root, name)

            try:
                mtimes[path] = os.path.getmtime(path)
            except OSError:
                continue

    try:
        mtimes[_CONFIG_PATH] = os.path.getmtime(_CONFIG_PATH)
    except OSError:
        pass

    return mtimes


def _restart_process(changed_path):
    global _RESTARTING

    with _RESTART_LOCK:
        if _RESTARTING:
            return
        _RESTARTING = True

    print(f"[HOT-RELOAD] 检测到变更，退出码 100 → launcher 接管重启: {changed_path}")
    sys.stdout.flush()
    sys.stderr.flush()

    # 前后端分离后 weflow-core / sender 是独立前端进程，热重载只重启后端，
    # 不再 taskkill WeFlow.exe（前端不受后端重载影响）

    # 【核心】LLM 回复生成到一半就 os._exit → 这条回复永久丢失（用户在群里 @ 了
    # 机器人却"没反应"，run.log 里能看到 [LLM REQUEST] 紧跟着 [HOT-RELOAD]）。
    # 退出前给在途请求一段有上限的宽限期，等它把回复发出去；超时才照常硬退。
    # 宽限等待本身的异常一律忽略：热重载不能因为等请求而卡住。
    try:
        _wait_for_in_flight(_grace_seconds())
    except Exception as exc:
        print(f"[HOT-RELOAD] 宽限等待异常（忽略，继续退出）: {exc}")
    sys.stdout.flush()

    # os._exit 跳过 atexit → 必须先手动 flush 内存里的 token 计数，
    # 否则每次热重载丢掉最后 60 秒（节流窗口）的用量。flush 失败也必须照常退出。
    try:
        from core import bot_stats
        bot_stats.flush_tokens()
    except Exception as exc:
        print(f"[HOT-RELOAD] token 统计落盘失败（忽略，继续退出）: {exc}")
        sys.stdout.flush()

    # 硬退出，不执行 atexit/finally。launcher.py 检测到 100 后立即重启 main.py
    os._exit(100)


def _watch_loop(interval):
    known = _scan_py_mtimes()
    print("[HOT-RELOAD] 项目级 .py 监控已启动")

    while True:
        time.sleep(interval)

        current = _scan_py_mtimes()

        for path, mtime in current.items():
            if known.get(path) != mtime:
                _restart_process(path)
                return

        for path in list(known.keys()):
            if path not in current:
                _restart_process(path)
                return

        known = current


def start(interval=1):
    threading.Thread(target=_watch_loop, args=(interval,), daemon=True).start()
