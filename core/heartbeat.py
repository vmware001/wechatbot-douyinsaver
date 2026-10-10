# -*- coding: utf-8 -*-
"""后端心跳：把"后端还活着"这件事写到磁盘上，给前端侧的保活看门狗看。

为什么要写文件而不是开端口：后端（main.py）没有 HTTP 服务，只有一个占着主线程的
SSE 客户端；前端侧看门狗是独立进程，读一个每 10s 刷新的 json 比去猜进程名可靠得多
（热重载、被杀、卡死三种情况里，只有"卡死"能靠"进程在但心跳不发"识别出来）。

文件：runtime/backend_heartbeat.json
    {"pid": 1234, "ts": 1696112345.6, "iso": "2026-10-01T00:30:00", "state": "running"}
"""

import json
import os
import threading
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNTIME_DIR = os.path.join(BASE_DIR, "runtime")
HEARTBEAT_PATH = os.path.join(RUNTIME_DIR, "backend_heartbeat.json")

INTERVAL_SECONDS = 10

_started = False
_lock = threading.Lock()
_state = "running"


def set_state(state: str):
    """更新心跳里附带的状态（如 starting / reloading / running）。"""
    global _state
    _state = str(state or "running")


def write_once():
    """立刻写一次心跳（启动时先写一次，前端看门狗不用等第一个周期）。"""
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        now = time.time()
        payload = {
            "pid": os.getpid(),
            "ts": now,
            "iso": datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S"),
            "state": _state,
        }
        tmp = HEARTBEAT_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, HEARTBEAT_PATH)
        return True
    except Exception as e:
        print(f"[HEARTBEAT] 写心跳失败: {type(e).__name__}: {e}", flush=True)
        return False


def read_heartbeat():
    """读心跳文件；读不到返回 None。"""
    try:
        with open(HEARTBEAT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _loop():
    while True:
        write_once()
        time.sleep(INTERVAL_SECONDS)


def start():
    """启动心跳线程（幂等）。"""
    global _started
    with _lock:
        if _started:
            return True
        _started = True
    write_once()
    threading.Thread(target=_loop, name="heartbeat", daemon=True).start()
    print(f"[HEARTBEAT] 心跳已启动: {HEARTBEAT_PATH} (每 {INTERVAL_SECONDS}s)", flush=True)
    return True
