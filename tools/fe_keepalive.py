# -*- coding: utf-8 -*-
"""前端侧保活看门狗：盯着后端（心跳 + 进程）和两个前端服务，谁不在就拉谁。

设计要点：
  * 后端死活以 core/heartbeat.py 写出的 runtime/backend_heartbeat.json 为准：
    心跳新鲜 = 活着；进程在但心跳过期 = 卡死（杀掉，交给 start_bot_vm.bat 的重启循环）；
    进程都没了 = 直接拉起 start_bot_vm.bat（脚本自带 10s 重启循环）。
  * 拉起/终止都要管理员权限（微信和这些服务都是管理员跑的），所以本脚本必须跑在
    管理员窗口里——start_all.bat / start_bot_vm.bat 拉起的 wt 标签页天然是管理员。
  * 有冷却时间，避免服务反复拉起时把机器刷爆。

用法：
    python tools/fe_keepalive.py            # 常驻
    python tools/fe_keepalive.py --once     # 只检查一轮（诊断用）
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import heartbeat  # noqa: E402
from core import service_watchdog as sw  # noqa: E402

CHECK_INTERVAL_SECONDS = 20
STUCK_CHECKS_TO_KILL = 3          # 心跳过期再观察几轮才杀（热重载期间也会短暂停跳）
MISSING_CHECKS_TO_SPAWN_BE = 3    # main.py 不在时，先等监督者自己拉，几轮还没起来才动手
ACTION_COOLDOWN_SECONDS = 300     # 同一个动作（杀/拉后端）间隔


def log(msg):
    print(f"[FE-KEEPALIVE] {msg}", flush=True)


def check_once(state):
    """跑一轮检查，state 用来跨轮记数。返回本轮摘要字符串。"""
    actions = []

    # ── 后端 ──────────────────────────────────────────────
    try:
        fresh, alive = sw.backend_status()
    except Exception as e:
        log(f"读后端心跳异常: {type(e).__name__}: {e}")
        fresh, alive = False, False

    if fresh:
        if state.get("be_stuck"):
            log("后端心跳恢复")
        state["be_stuck"] = 0
        state["be_missing"] = 0
    else:
        if alive:
            state["be_stuck"] = state.get("be_stuck", 0) + 1
            state["be_missing"] = 0
            log(f"后端进程在但心跳过期 第{state['be_stuck']}轮")
            if state["be_stuck"] >= STUCK_CHECKS_TO_KILL:
                if time.time() - state.get("be_action_at", 0) >= ACTION_COOLDOWN_SECONDS:
                    state["be_action_at"] = time.time()
                    state["be_stuck"] = 0
                    n = sw.kill_backend()
                    if n:
                        actions.append(f"杀掉卡死后端 x{n}")
                        log("已杀掉卡死的后端，等监督者拉起（10s 后）")
                    else:
                        actions.append("尝试杀后端失败")
        else:
            state["be_missing"] = state.get("be_missing", 0) + 1
            state["be_stuck"] = 0
            supervisor = sw.find_processes(sw.BACKEND_SUPERVISOR_PROCS)
            log(f"后端 main.py 不在 第{state['be_missing']}轮（监督者{'在' if supervisor else '不在'}）")
            if not supervisor or state["be_missing"] >= MISSING_CHECKS_TO_SPAWN_BE:
                if time.time() - state.get("be_action_at", 0) >= ACTION_COOLDOWN_SECONDS:
                    state["be_action_at"] = time.time()
                    state["be_missing"] = 0
                    if sw.spawn_bat("start_bot_vm.bat"):
                        actions.append("拉起后端 start_bot_vm.bat")

    # ── 前端 ──────────────────────────────────────────────
    for key in sw.FRONTENDS:
        try:
            ok, alive = sw.frontend_status(key)
        except Exception as e:
            log(f"检查 {key} 异常: {type(e).__name__}: {e}")
            continue
        if ok:
            if state.get(f"fe_{key}_fails"):
                log(f"{key} 已恢复健康")
            state[f"fe_{key}_fails"] = 0
            continue
        state[f"fe_{key}_fails"] = state.get(f"fe_{key}_fails", 0) + 1
        log(f"{key} 健康检查失败 第{state[f'fe_{key}_fails']}轮"
            f"（启动脚本进程{'在' if alive else '不在'}）")
        if state[f"fe_{key}_fails"] >= 2:
            if time.time() - state.get(f"fe_{key}_at", 0) >= ACTION_COOLDOWN_SECONDS:
                state[f"fe_{key}_at"] = time.time()
                state[f"fe_{key}_fails"] = 0
                if sw.ensure_frontend(key):
                    actions.append(f"处理 {key}")

    return "; ".join(actions) if actions else "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="只检查一轮后退出")
    args = ap.parse_args()

    # keepalive.enabled=false 时整体退出（和 core/service_watchdog.py 一致），
    # 免得用户想彻底停服时被看门狗反复拉起
    try:
        with open(os.path.join(sw.BASE_DIR, "config.json"), encoding="utf-8") as f:
            _cfg = json.load(f)
        if (_cfg.get("keepalive") or {}).get("enabled") is False:
            log("config.json → keepalive.enabled=false，看门狗退出（不做任何拉起）")
            return 0
    except Exception as e:
        log(f"读 config.json 失败（按默认继续保活）: {type(e).__name__}: {e}")

    log(f"启动：轮询间隔 {CHECK_INTERVAL_SECONDS}s，后端心跳 {heartbeat.HEARTBEAT_PATH}")
    state = {}
    if args.once:
        summary = check_once(state)
        print(json.dumps({"summary": summary, "state": state}, ensure_ascii=False))
        return 0
    while True:
        try:
            check_once(state)
        except Exception as e:
            log(f"本轮检查异常（已忽略，继续下一轮）: {type(e).__name__}: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    sys.exit(main())
