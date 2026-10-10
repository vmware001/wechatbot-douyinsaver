import json
import os
import sys
import signal
import threading
import subprocess
import ctypes

# 强制 UTF-8 输出，防止 Windows 中文控制台(GBK)下打印 emoji 时
# 抛 UnicodeEncodeError 把主线程炸死
for _stream in (sys.stdout, sys.stderr):
    if _stream and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_MUTEX_HANDLE = None


def acquire_single_instance_mutex():
    """全局互斥锁：同一台机器只允许一个 bot 进程。
    第二个实例直接退出，避免双 bot 抢端口/重复回复。"""
    global _MUTEX_HANDLE
    ERROR_ALREADY_EXISTS = 183
    # 热重载时旧进程刚退出，互斥锁可能还没释放，重试几次
    for attempt in range(10):
        _MUTEX_HANDLE = ctypes.windll.kernel32.CreateMutexW(None, True, "Local\\GSMPSWechatBot_SingleInstance")
        if ctypes.windll.kernel32.GetLastError() != ERROR_ALREADY_EXISTS:
            return
        import time as _time
        _time.sleep(1)
    print("[FATAL] 检测到另一个 bot 实例已在运行，本实例退出。", flush=True)
    sys.exit(2)

from core.project_reloader import start as start_project_reloader
from core.sender import configure as configure_sender
from core.weflow_media import configure as configure_weflow_media
from core.windows_awake import start as start_windows_awake
from core.queue import task_queue
from core.weflow_client import WeFlowClient
from core.router import Router
from core.dispatcher import Dispatcher
from core.worker import Worker
from core.weflow_headless import WeFlowCore
from utils.dedup import Dedup
from utils.sqlite_store import migrate_legacy_storage


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.append(BASE_DIR)


# 全局引用，供信号处理器使用
_weflow: WeFlowCore | None = None


def graceful_shutdown(signum, frame):
    """Ctrl+C → 退出后端（前端 weflow-core/sender 为独立进程，不受影响）"""
    print("\n⚠️  收到终止信号，后端正在退出（前端服务保持运行）...", flush=True)
    if _weflow:
        _weflow.stop()  # connect 模式下为 no-op
    sys.exit(0)


def _wait_sender_ready(config: dict, timeout: int = 30) -> bool:
    """等待前端发送服务（sender_service.py，默认 127.0.0.1:9999）就绪"""
    import time as _time
    import requests as _requests
    frontend_cfg = config.get("frontend") or {}
    sender_url = str(frontend_cfg.get("sender_url") or "http://127.0.0.1:9999").rstrip("/")
    print(f"[bot] 等待前端发送服务就绪 ({sender_url})...", flush=True)
    for _ in range(timeout * 2):
        try:
            r = _requests.get(f"{sender_url}/health", timeout=1)
            if r.status_code == 200 and r.json().get("service") == "sender":
                print(f"[bot] ✅ 前端发送服务就绪 ({sender_url})", flush=True)
                return True
        except Exception:
            pass
        _time.sleep(0.5)
    print(f"[bot] ❌ 等待前端发送服务超时 ({sender_url})", flush=True)
    print("[bot]    请先运行 start_frontend.bat 启动前端服务", flush=True)
    return False


signal.signal(signal.SIGINT, graceful_shutdown)
signal.signal(signal.SIGTERM, graceful_shutdown)


def _launch_weflow_setup_wizard():
    """启动 weflow-core 交互式配置向导（独立终端窗口）

    当 config.json 配置不完整时，弹出一个可见的终端窗口，
    让用户通过 weflow-core 的交互式引导完成配置。
    向导完成后自动退出，由 start.bat 重新启动。
    """
    weflow_dir = os.path.join(BASE_DIR, "weflow-core")
    dist_js = os.path.join(weflow_dir, "dist", "index.js")
    config_path = os.path.join(BASE_DIR, "config.json")

    if not os.path.isfile(dist_js):
        print("[ERROR] weflow-core 未构建，请先运行 start.bat 完成首次构建", flush=True)
        sys.exit(1)

    if sys.platform == "win32":
        # Windows: 使用 start 打开新终端窗口，/wait 等待用户完成
        # 使用 & 而非 && 连接 pause，确保即使 node 异常退出也能等待用户按键
        # 路径加双引号，防止路径含空格/& 时命令断裂（cmd /c 会剥掉最外层引号，内层引号保留生效）
        cmd = (
            f'start "WeFlow 配置向导" /wait cmd /c '
            f'"cd /d "{weflow_dir}" && node dist/index.js "{config_path}" & echo. & echo 按任意键退出... & pause >nul"'
        )
    else:
        # macOS / Linux: 尝试打开新终端
        term = os.environ.get("TERM_PROGRAM", "")
        if term == "Apple_Terminal" or sys.platform == "darwin":
            cmd = (
                f"osascript -e 'tell app \"Terminal\" to do script "
                f"\"cd {weflow_dir} && node dist/index.js {config_path}; echo; echo 配置完成，按回车继续...; read\"'"
            )
        else:
            cmd = (
                f"x-terminal-emulator -e "
                f"bash -c 'cd {weflow_dir} && node dist/index.js {config_path}; echo; echo 配置完成，按回车继续...; read'"
            )

    try:
        subprocess.run(cmd, shell=True)
    except Exception as e:
        print(f"[ERROR] 无法启动配置向导: {e}", flush=True)
        print("[HINT] 请手动运行: cd weflow-core && node dist/index.js ../config.json", flush=True)
        sys.exit(1)


def main():
    global _weflow

    acquire_single_instance_mutex()

    try:
        with open("config.json", encoding="utf-8") as f:
            config = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        print(f"\n[ERROR] 无法加载 config.json: {e}", flush=True)
        print("[HINT] 请复制 sample_config.json 为 config.json 并填入真实值", flush=True)
        sys.exit(1)

    # ── 自动生成 API Token（内部鉴权用，无需用户手动填写）──
    _token = config.get("token", "").strip()
    if not _token or _token == "your-token-here":
        import secrets
        _token = secrets.token_hex(32)
        config["token"] = _token
        try:
            with open("config.json", "w", encoding="utf-8") as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            print(f"[INFO] 已自动生成 API Token: {_token[:8]}...", flush=True)
        except Exception as e:
            print(f"[WARN] 无法保存自动生成的 Token: {e}", flush=True)

    # ── 启动前配置预检 ──
    _weflow_cfg = config.get("weflow", {})

    # weflow 相关缺失项（可通过 weflow-core 向导自动配置）
    _weflow_errors = []
    if not _weflow_cfg.get("dbPath", "").strip():
        _weflow_errors.append("weflow.dbPath 未填写（微信文件目录）")
    if not _weflow_cfg.get("decryptKey", "").strip() or len(_weflow_cfg.get("decryptKey", "")) != 64:
        _weflow_errors.append("weflow.decryptKey 未填写（64位十六进制解密密钥）")
    if not _weflow_cfg.get("myWxid", "").strip():
        _weflow_errors.append("weflow.myWxid 未填写（微信账号 wxid）")

    # 仅 target_group 缺失（weflow-core 向导无法处理，直接报错退出）
    _tg_missing = not config.get("target_group") or config["target_group"] == ["Your Target Group Name"]

    if _weflow_errors:
        print("\n[INFO] 检测到 weflow 配置不完整，正在启动 weflow-core 交互式配置向导...", flush=True)
        for err in _weflow_errors:
            print(f"  - {err}", flush=True)
        if _tg_missing:
            print("  - target_group 未填写（向导完成后请在 config.json 中手动设置）", flush=True)
        _launch_weflow_setup_wizard()
        # 向导完成后退出，由 start.bat 自动重启加载新配置
        print("\n[INFO] 配置向导已关闭，正在重启...", flush=True)
        sys.exit(0)

    if _tg_missing:
        print("\n[ERROR] target_group 未填写（请设置为你的目标群名）", flush=True)
        print("[HINT] 编辑 config.json，将 target_group 改为你的微信群名称", flush=True)
        sys.exit(1)

    migrate_legacy_storage()
    start_windows_awake()
    configure_sender(config)
    configure_weflow_media(config)
    start_project_reloader()

    # 心跳尽早开始：前端侧保活看门狗靠它判断后端死活，启动慢也不该被判成"后端没了"
    from core import heartbeat
    heartbeat.set_state("starting")
    heartbeat.start()

    # ═══════════════════════════════════════
    # 等待前端发送服务（sender_service.py，独立进程，默认 :9999）
    # ═══════════════════════════════════════
    if not _wait_sender_ready(config):
        sys.exit(1)

    # ═══════════════════════════════════════
    # 🚀 连接 weflow-core（独立前端进程，由 start_frontend.bat 启动）
    # ═══════════════════════════════════════
    print("[bot] 正在连接 weflow-core...", flush=True)
    _weflow = WeFlowCore(BASE_DIR)
    if not _weflow.connect():
        print("[bot] ❌ weflow-core 未就绪，退出", flush=True)
        sys.exit(1)

    dedup = Dedup()

    llm_config = config.get("llm", {}) or {}
    configured_prefix_mode = str(config.get("prefix_mode", "strict") or "strict").strip().lower()
    if configured_prefix_mode == "only":
        configured_prefix_mode = "strict"
    if configured_prefix_mode not in {"strict", "mixed"}:
        configured_prefix_mode = "strict"
    auto_reply_config = llm_config.get("auto_reply")
    if not isinstance(auto_reply_config, dict):
        auto_reply_config = {}

    # Mixed mode enables the LLM state machine by default.  Strict mode is a
    # prefix-only performance mode and always uses direct forced replies.
    proactive_enabled = bool(
        llm_config.get("enabled")
        and configured_prefix_mode == "mixed"
        and auto_reply_config.get("enabled", True)
    )

    router = Router(
        prefix=config.get("prefix", "@服务器状态@我"),
        target_group=config.get("target_group", []),
        time_slots=config.get("time_slots", []),
        rate_limit_cfg=config.get("rate_limit", {}),
        prefix_mode=configured_prefix_mode,
        proactive_enabled=proactive_enabled
    )

    dispatcher = Dispatcher()
    dispatcher.load_plugins()
    dispatcher.init_plugins(config)   # ⭐如果你需要 config

    worker = Worker(task_queue, router, dispatcher, config=config)
    worker.start(int(config.get("worker_num") or 2))

    # 启动回填：热重载会在 worker 消费前杀掉进程，而 rawid 已持久化去重 →
    # 这些消息永远不会进群历史。后台线程补录，不阻塞启动、不触发任何回复。
    try:
        from core import history_backfill
        _backfill_memory = getattr(worker, "memory_manager", None)
        if _backfill_memory is not None:
            threading.Thread(
                target=history_backfill.run,
                args=(config, _backfill_memory),
                name="history-backfill",
                daemon=True,
            ).start()
    except Exception as _exc:
        print(f"[BACKFILL ERROR] 启动回填线程失败: {_exc}", flush=True)

    from core import daily_summary
    daily_summary.start(dispatcher)

    # 后端侧保活：盯两个前端服务的 /health，挂了就按需拉起（与前端侧 fe_keepalive.py 互为看护）
    heartbeat.set_state("running")
    from core import service_watchdog
    service_watchdog.start(config, BASE_DIR)

    from core import gc as cache_gc
    cache_gc.start(config)

    client = WeFlowClient(
        token=config["token"],
        queue=task_queue,
        dedup=dedup,
        api_base=(config.get("frontend") or {}).get("weflow_url"),
    )

    # SSE receiving is the main service loop. Keeping it in the main thread
    # keeps process lifecycle and signal handling in one well-defined place.
    print("[bot] SSE client running in main thread", flush=True)
    client.start()


if __name__ == "__main__":
    main()
