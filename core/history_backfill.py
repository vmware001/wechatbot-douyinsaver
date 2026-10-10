# -*- coding: utf-8 -*-
"""启动回填：把 weflow 里最近的消息补录进群历史（只补历史，不触发任何回复）。

背景（core/weflow_client.py）：SSE 推送先 dedup.add(rawid) 再 queue.put(msg)。
进程若在 worker 消费该消息前被热重载杀掉（project_reloader 用 os._exit(100)），
这条消息既没进群历史，rawid 又已持久化去重 → 重启后 weflow 重放也会被跳过，
永久丢失（表现为群上下文缺一块、被引用消息在本地历史里查不到）。

这里直接按 talker 拉取最近消息补录。add_group_message 自带 local_id/server_id
去重，所以重复调用是幂等的，启动时无脑跑一次即可。

补录是往历史文件末尾追加，但消息时间戳是旧的 → 文件里会出现时间倒序，
而 MemoryManager.get_group_messages 取的是"文件末尾 N 条"当上下文。所以某个群
真的补了消息（added > 0）时，补完立刻按原始 timestamp 稳定重排该群历史文件。
"""

import os

import requests

from llm.memory.memory_manager import _GROUP_FILE_LOCK

_DEFAULT_API_BASE = "http://127.0.0.1:5031"
# [图片] 必须保留：自动附图功能需要它的 local_id 来取图（同 core/worker.py）
_PLACEHOLDER_KEEP = "[图片]"
_UNKNOWN_SENDER = "群友"


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def api_base(config):
    """weflow-core 地址：与 WeFlowClient 一致优先用 frontend.weflow_url。"""
    base = str(((config.get("frontend") or {}).get("weflow_url")) or "").strip()
    if not base:
        port = (config.get("weflow") or {}).get("apiPort") or 5031
        base = f"http://127.0.0.1:{port}"
    return base.rstrip("/")


def _get(api_base_url, token, path, params, timeout):
    resp = requests.get(
        f"{api_base_url}{path}",
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def _session_map(api_base_url, token, timeout):
    """displayName -> username（形如 49794819082@chatroom）"""
    data = _get(api_base_url, token, "/api/v1/sessions", None, timeout)
    sessions = data.get("sessions") if isinstance(data, dict) else None
    mapping = {}
    for item in sessions or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("displayName") or "").strip()
        username = str(item.get("username") or "").strip()
        if name and username:
            mapping[name] = username
    return mapping


def _member_map(api_base_url, token, chatroom, timeout):
    """wxid -> 群昵称：displayName 缺失时退回 nickname，再退回 wxid 本身。"""
    data = _get(
        api_base_url, token, "/api/v1/group-members",
        {"talker": chatroom}, timeout,
    )
    members = data.get("members") if isinstance(data, dict) else None
    mapping = {}
    for item in members or []:
        if not isinstance(item, dict):
            continue
        wxid = str(item.get("wxid") or "").strip()
        if not wxid:
            continue
        mapping[wxid] = (
            str(item.get("displayName") or "").strip()
            or str(item.get("nickname") or "").strip()
            or wxid
        )
    return mapping


def _recent_messages(api_base_url, token, chatroom, limit, timeout):
    """返回按时间旧→新排序的消息。不带 media=1：带 media 会让 weflow 解密导出
    上百张图（一次约 10 秒），不带只要 0.04 秒。"""
    data = _get(
        api_base_url, token, "/api/v1/messages",
        {"talker": chatroom, "limit": limit}, timeout,
    )
    messages = data.get("messages") if isinstance(data, dict) else None
    items = [m for m in (messages or []) if isinstance(m, dict)]
    items.sort(key=lambda m: (_as_int(m.get("createTime")), _as_int(m.get("localId"))))
    return items


def _should_skip(content):
    """照抄 core/worker.py 的入历史规则：占位消息跳过，但 [图片] 必须保留。

    额外跳过系统消息/appmsg/撤回的原始 XML（如 <sysmsg ...>）：SSE 推送路径
    不会把这些写进历史（run.log 与 group_messages.json 里都查不到 sysmsg），
    补录也不该引入。
    """
    if not content:
        return True
    if content.startswith("[") and content.endswith("]") and content != _PLACEHOLDER_KEEP:
        return True
    return content.startswith("<")


def _sort_key_ts(item):
    """排序键：timestamp 缺失/非数字（含非 dict 条目）一律视为 0，绝不抛异常。"""
    if not isinstance(item, dict):
        return 0.0
    try:
        return float(item.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0.0


def sort_history(group_id, memory_manager):
    """把某个群的历史文件按 timestamp 稳定排序写回（回填的旧消息原本追加在末尾）。

    稳定：时间戳相同的条目保持原有相对顺序（旧索引作为次序键）。已经有序则跳过
    写盘。写盘复用 MemoryManager 的 _write_json（临时文件 + os.replace），并拿同一把
    _GROUP_FILE_LOCK，避免和其它写入路径抢同一个文件。异常由调用方兜住，
    不写坏历史文件。
    """
    with _GROUP_FILE_LOCK:
        group_dir = memory_manager._ensure_group_dir(group_id)
        path = os.path.join(group_dir, "group_messages.json")
        items = memory_manager._load_json(path, default=[])
        if not isinstance(items, list) or len(items) < 2:
            return 0
        indexed = sorted(
            enumerate(items),
            key=lambda pair: (_sort_key_ts(pair[1]), pair[0]),
        )
        if all(position == index for position, (index, _) in enumerate(indexed)):
            return 0
        ordered = [item for _, item in indexed]
        memory_manager._write_json(path, ordered)
        return len(ordered)


def run(config, memory_manager, timeout=20.0, limit=200):
    """启动时把 weflow 里最近的消息补录进群历史（只补历史，不触发任何回复）。

    返回补录条数。任何异常只打 [BACKFILL ERROR] 就返回，绝不影响启动。
    """
    total = 0
    try:
        groups = [str(g).strip() for g in (config.get("target_group") or []) if str(g).strip()]
        token = str(((config.get("weflow") or {}).get("apiToken")) or "").strip()
        if not groups or not token or memory_manager is None:
            return 0

        api_base_url = api_base(config)
        session_timeout = min(float(timeout), 10.0)
        sessions = _session_map(api_base_url, token, session_timeout)

        for group in groups:
            chatroom = sessions.get(group)
            if not chatroom:
                print(f"[BACKFILL] 群「{group}」在 weflow 会话列表中找不到，跳过", flush=True)
                continue

            members = _member_map(api_base_url, token, chatroom, timeout)
            added = 0
            for msg in _recent_messages(api_base_url, token, chatroom, limit, timeout):
                content = str(msg.get("content") or "").strip()
                if _should_skip(content):
                    continue
                sender = str(msg.get("senderUsername") or "").strip()
                try:
                    ok = memory_manager.add_group_message(
                        group_id=group,
                        nickname=members.get(sender) or sender or _UNKNOWN_SENDER,
                        content=content,
                        local_id=msg.get("localId"),
                        server_id=msg.get("serverId"),
                        session_id=chatroom,
                        is_bot=(_as_int(msg.get("isSend")) == 1),
                        timestamp=msg.get("createTime"),
                    )
                except Exception as exc:
                    print(f"[BACKFILL ERROR] 群「{group}」写入历史失败: {exc}", flush=True)
                    continue
                if ok:
                    added += 1
                    total += 1
            print(f"[BACKFILL] 群「{group}」补录 {added} 条", flush=True)
            # 只有真补了消息才重排：没补录的群不重写文件，省掉无谓 IO。
            if added:
                try:
                    reordered = sort_history(group, memory_manager)
                    if reordered:
                        print(
                            f"[BACKFILL] 群「{group}」历史已按时间戳重排（{reordered} 条）",
                            flush=True,
                        )
                except Exception as exc:
                    print(f"[BACKFILL ERROR] 群「{group}」历史排序失败: {exc}", flush=True)

        print(f"[BACKFILL] 合计补录 {total} 条", flush=True)
    except Exception as exc:
        print(f"[BACKFILL ERROR] {type(exc).__name__}: {exc}", flush=True)
    return total
