# -*- coding: utf-8 -*-
"""前后端相互保活。

分工：
  * 后端侧（本模块，跑在 main.py 里的守护线程）：盯着两个前端服务的 /health，
    连续两次不通就按需拉起对应的前端启动脚本（frontend_weflow.bat / frontend_sender.bat）。
    拉起前先确认"那个脚本的进程还在不在"——脚本自带 5s 重启循环，进程还在时
    只是服务正在重启，别去开第二份。
  * 前端侧（tools/fe_keepalive.py，独立进程）：用后端心跳文件
    （runtime/backend_heartbeat.json，见 core/heartbeat.py）判断后端死活——
    心跳停 2 分钟以上判为"卡死"，杀掉 main.py 让 start_bot_vm.bat 的循环拉起来；
    连 main.py 进程都没有就直接拉起 start_bot_vm.bat。

两边都用这里的健康检查/进程探测/拉起工具，逻辑只有一份。
"""

import os
import subprocess
import threading
import time

import psutil

try:
    import requests as _requests
except Exception:  # pragma: no cover - requests 是项目依赖，缺了也不该炸掉保活
    _requests = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 前端服务定义：key -> (健康检查地址, 期望的 service 名, 启动脚本, 进程特征)
FRONTENDS = {
    "weflow": {
        "url": "http://127.0.0.1:5031",
        "service": "weflow-core",
        "bat": "frontend_weflow.bat",
        "procs": ("frontend_weflow.bat", "WeFlow.exe"),
    },
    "sender": {
        "url": "http://127.0.0.1:9999",
        "service": "sender",
        "bat": "frontend_sender.bat",
        "procs": ("frontend_sender.bat", "sender_service.py"),
    },
}

BACKEND_SUPERVISOR_PROCS = ("start_bot_vm.bat", "run_bot.ps1")
BACKEND_PROCS = ("main.py",)

CHECK_INTERVAL_SECONDS = 30      # 后端侧轮询间隔
FAILS_TO_RESTART = 2             # 连续失败几次才动手（一次抖动不折腾）
RESTART_COOLDOWN_SECONDS = 180   # 同一个服务两次拉起之间至少隔这么久

_started = False
_lock = threading.Lock()


def _log(msg):
    print(f"[KEEPALIVE] {msg}", flush=True)


def health_ok(url, service=None, timeout=2.0):
    """GET <url>/health 是否返回预期 service 名。"""
    if _requests is None:
        return False
    try:
        r = _requests.get(f"{str(url).rstrip('/')}/health", timeout=timeout)
        if r.status_code != 200:
            return False
        if not service:
            return True
        try:
            return (r.json() or {}).get("service") == service
        except Exception:
            return False
    except Exception:
        return False


def find_processes(patterns, exclude_pids=(None,)):
    """按命令行/进程名匹配进程，返回 [(pid, name, cmdline), ...]。"""
    hits = []
    pats = [str(p).lower() for p in patterns if p]
    if not pats:
        return hits
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] in exclude_pids:
                continue
            name = (proc.info.get("name") or "").lower()
            cmdline = " ".join(proc.info.get("cmdline") or []).lower()
            if any(p in name or p in cmdline for p in pats):
                hits.append((proc.info["pid"], proc.info.get("name"), cmdline))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception:
            continue
    return hits


def spawn_bat(bat_name, base_dir=None):
    """在独立最小化窗口里跑一个 .bat（不阻塞当前进程）。"""
    base = base_dir or BASE_DIR
    path = bat_name if os.path.isabs(bat_name) else os.path.join(base, bat_name)
    if not os.path.isfile(path):
        _log(f"拉起失败：找不到 {path}")
        return False
    try:
        flags = 0
        if hasattr(subprocess, "CREATE_NEW_CONSOLE"):
            flags |= subprocess.CREATE_NEW_CONSOLE
        subprocess.Popen(
            ["cmd", "/c", path],
            cwd=base,
            close_fds=True,
            creationflags=flags,
        )
        _log(f"已拉起 {os.path.basename(path)}")
        return True
    except Exception as e:
        _log(f"拉起 {os.path.basename(path)} 失败: {type(e).__name__}: {e}")
        return False


def frontend_status(key):
    """返回 (健康, 启动脚本进程是否还在)。"""
    cfg = FRONTENDS[key]
    ok = health_ok(cfg["url"], cfg["service"])
    alive = bool(find_processes(cfg["procs"]))
    return ok, alive


def ensure_frontend(key, base_dir=None):
    """前端服务不在就把它拉起来（进程还在时不动，等它自己的循环重启）。

    返回 True 表示"已经健康或已经动手"，False 表示"没动手/动手失败"。
    """
    cfg = FRONTENDS[key]
    ok, alive = frontend_status(key)
    if ok:
        return True
    if alive:
        _log(f"{key}: 端口不通但启动脚本还在跑（自带重启循环中），不重复拉起")
        return True
    _log(f"{key}: 端口不通且启动脚本已退出 → 拉起 {cfg['bat']}")
    return spawn_bat(cfg["bat"], base_dir)


def backend_status(base_dir=None, max_age_seconds=120):
    """后端状态：返回 (心跳是否新鲜, main.py 是否在跑)。

    心跳文件由 core/heartbeat.py 每 10s 刷新；进程在但心跳过期 = 后端卡死。
    """
    from core import heartbeat  # 延迟导入，避免前端看门狗导入整个 core 包时副作用

    hb = heartbeat.read_heartbeat()
    fresh = False
    if isinstance(hb, dict):
        try:
            fresh = (time.time() - float(hb.get("ts") or 0)) <= max_age_seconds
        except Exception:
            fresh = False
    procs = find_processes(BACKEND_PROCS)
    return fresh, bool(procs)


def kill_backend(base_dir=None):
    """杀掉 main.py（交给 start_bot_vm.bat 的循环重启）。返回杀掉的进程数。"""
    killed = 0
    for pid, name, _ in find_processes(BACKEND_PROCS):
        try:
            psutil.Process(pid).terminate()
            killed += 1
            _log(f"已终止卡死的后端进程 pid={pid} ({name})")
        except Exception as e:
            _log(f"终止 pid={pid} 失败: {type(e).__name__}: {e}")
    return killed


def _loop(base_dir=None):
    fails = {k: 0 for k in FRONTENDS}
    last_action = {k: 0.0 for k in FRONTENDS}
    while True:
        for key in FRONTENDS:
            try:
                ok, alive = frontend_status(key)
            except Exception as e:
                _log(f"{key}: 状态检查异常 {type(e).__name__}: {e}")
                continue
            if ok:
                if fails[key]:
                    _log(f"{key}: 已恢复健康")
                fails[key] = 0
                continue
            fails[key] += 1
            _log(f"{key}: 健康检查失败 第{fails[key]}次（启动脚本进程{'在' if alive else '不在'}）")
            if fails[key] < FAILS_TO_RESTART:
                continue
            if time.time() - last_action[key] < RESTART_COOLDOWN_SECONDS:
                continue
            last_action[key] = time.time()
            ensure_frontend(key, base_dir)
        time.sleep(CHECK_INTERVAL_SECONDS)


def start(config=None, base_dir=None):
    """后端侧保活线程（幂等）。config 里的 frontend.*_url 会覆盖默认端口。

    配置 config.json → keepalive.enabled=false 可整体关闭（前端侧的
    tools/fe_keepalive.py 也认这一项，会自己退出）。
    """
    global _started
    if config:
        cfg = (config.get("keepalive") or {})
        if cfg.get("enabled") is False:
            _log("keepalive.enabled=false → 后端侧看门狗未启动")
            return False
        fe = (config.get("frontend") or {})
        if fe.get("weflow_url"):
            FRONTENDS["weflow"]["url"] = str(fe["weflow_url"])
        if fe.get("sender_url"):
            FRONTENDS["sender"]["url"] = str(fe["sender_url"])
    with _lock:
        if _started:
            return True
        _started = True
    threading.Thread(target=_loop, args=(base_dir,), name="service-watchdog", daemon=True).start()
    _log("后端侧看门狗已启动（盯 :5031 / :9999，每隔 %ds 检查）" % CHECK_INTERVAL_SECONDS)
    return True
