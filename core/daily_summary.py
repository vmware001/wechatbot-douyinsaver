# -*- coding: utf-8 -*-
"""每日群聊总结：每天 23:56 向 AI 请求过去 24 小时总结，00:00 准点发送。

- 每个目标群各生成一份总结，各发各的群（互不混用）
- 超过 1000 字就打回让 AI 重写（最多 3 次），绝不截断
- 某个群当天没有聊天记录，就跳过那个群
- /summary 命令走 run_now() 立即生成发送（测试/手动用）
"""

import json
import threading
import time
from datetime import datetime, timedelta

MAX_CHARS = 1000
MAX_ATTEMPTS = 3
LOG_CHAR_CAP = 50000  # 24h 聊天记录上限，控制 token 成本

SYSTEM_PROMPT = (
    "你是群聊总结助手。根据给出的过去 24 小时微信群聊天记录，写一份中文总结。\n"
    "记录每行的格式是 [HH:MM] 昵称: 内容。\n"
    "要求：\n"
    "1. 严格控制在 1000 字以内，这是硬性要求\n"
    "2. 按时间段组织，每个时间段写清楚几点到几点、都有谁、在聊什么。"
    "例如「09:00-10:30，史皓驰和kid在讨论军训帽子丢了怎么办，有人建议去飞虎队报到」。"
    "相邻且话题相同的发言合并成一个时间段，话题切换就另起一段\n"
    "3. 口语化、轻松幽默，符合大学同学群的氛围\n"
    "4. 不要逐条复述发言，不要出现\"AI\"\"机器人\"\"总结如下\"这类字眼\n"
    "5. 可以用简短分段，不要用 Markdown 标题\n"
    "6. 记录中\"bot(我)\"是群里的 bot，它的发言也是聊天的一部分\n"
    "7. 输出 JSON 格式：{\"summary\": \"总结正文\"}，不要输出其他字段"
)

_thread_lock = threading.Lock()
_thread_started = False


def _collect_log(memory_manager, group, now=None):
    """取过去 24 小时的群消息，渲染成「昵称: 内容」文本。"""
    now = now or time.time()
    cutoff = now - 86400
    try:
        messages = memory_manager.get_all_group_messages(group) or []
    except Exception:
        return ""
    lines = []
    for item in messages:
        if not isinstance(item, dict):
            continue
        try:
            ts = float(item.get("timestamp") or 0)
        except (TypeError, ValueError):
            ts = 0
        if ts <= cutoff:
            continue
        nickname = str(item.get("nickname") or "").strip()
        is_bot = (
            bool(item.get("is_bot"))
            or str(item.get("role") or "") == "assistant"
            or nickname == "LLM"
        )
        if is_bot:
            nickname = "bot(我)"
        elif not nickname:
            nickname = "?"
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        time_str = datetime.fromtimestamp(ts).strftime("%H:%M") if ts else "??:??"
        lines.append(f"[{time_str}] {nickname}: {content}")
    text = "\n".join(lines)
    if len(text) > LOG_CHAR_CAP:
        text = text[-LOG_CHAR_CAP:]
    return text


def _extract_summary(raw):
    """从模型输出中提取总结正文。provider 强制 json_object 模式，
    正常应返回 {"summary": "..."}；解析失败时把原文当正文兜底。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        obj = json.loads(text)
    except Exception:
        return text
    if isinstance(obj, dict):
        val = obj.get("summary") or obj.get("messages") or ""
        if isinstance(val, list):
            val = "\n".join(str(x) for x in val)
        return str(val).strip()
    return ""


def _generate_summary(config, memory_manager, group):
    """生成 ≤1000 字总结；超长则把字数反馈给 AI 重写，最多 MAX_ATTEMPTS 次。
    全部超长的极端情况下返回最短的一次（不截断）。"""
    log_text = _collect_log(memory_manager, group)
    if not log_text:
        print(f"[DAILY SUMMARY] 「{group}」过去24小时无聊天记录，跳过", flush=True)
        return None

    from llm.provider.deepseek_provider import DeepSeekProvider
    provider = DeepSeekProvider()
    conversation = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"以下是「{group}」过去 24 小时的聊天记录"
            "（格式 [HH:MM] 昵称: 内容；[图片]/[表情]/[语音] 是占位符）：\n\n"
            + log_text
        )},
    ]
    attempts = []
    for attempt in range(1, MAX_ATTEMPTS + 1):
        message = provider.send_chat(conversation)
        text = _extract_summary(getattr(message, "content", None))
        if not text:
            print(f"[DAILY SUMMARY] 第 {attempt} 次生成: 空输出，重试", flush=True)
            continue
        attempts.append(text)
        print(f"[DAILY SUMMARY] 第 {attempt} 次生成: {len(text)} 字", flush=True)
        if len(text) <= MAX_CHARS:
            return text
        conversation.append({"role": "assistant", "content": text})
        conversation.append({"role": "user", "content": (
            f"太长了（{len(text)} 字），超过了 1000 字限制。"
            "保留最有意思的内容，压缩到 1000 字以内重写。"
        )})
    if attempts:
        shortest = min(attempts, key=len)
        print(f"[DAILY SUMMARY] {MAX_ATTEMPTS} 次仍超限，取最短一次 {len(shortest)} 字", flush=True)
        return shortest
    return None


def run_now(dispatcher, group):
    """/summary 手动触发：立即生成并返回文本（由调用方发送）。"""
    mm = getattr(getattr(dispatcher, "llm_service", None), "memory_manager", None)
    if mm is None:
        return "记忆模块不可用，无法生成总结"
    config = getattr(dispatcher, "config", {}) or {}
    return _generate_summary(config, mm, group) or "过去 24 小时没有聊天记录，没什么好总结的"


def _target_groups(config):
    """目标群列表（去重、保持顺序）。兼容旧的单字符串配置。"""
    tg = (config or {}).get("target_group")
    if isinstance(tg, (list, tuple)):
        raw = [str(x).strip() for x in tg]
    else:
        raw = [str(tg or "").strip()]
    groups = []
    for g in raw:
        if g and g not in groups:
            groups.append(g)
    return groups


def _loop(dispatcher):
    from core.sender import send
    config = getattr(dispatcher, "config", {}) or {}
    groups = _target_groups(config)
    if not groups:
        print("[DAILY SUMMARY] 未配置 target_group，线程退出", flush=True)
        return
    print(
        f"[DAILY SUMMARY] 定时线程已启动，目标群={groups}，每天 23:56 请求 / 00:00 发送",
        flush=True,
    )
    while True:
        now = datetime.now()
        request_at = now.replace(hour=23, minute=56, second=0, microsecond=0)
        if now >= request_at:
            request_at += timedelta(days=1)
        wait = (request_at - now).total_seconds()
        print(f"[DAILY SUMMARY] 下次请求时间 {request_at:%Y-%m-%d %H:%M}", flush=True)
        time.sleep(max(1.0, wait))

        # 每个群各生成一份总结（各自的历史、各自的 LLM 请求）
        mm = getattr(getattr(dispatcher, "llm_service", None), "memory_manager", None)
        summaries = {}
        for group in groups:
            if mm is None:
                break
            try:
                summary = _generate_summary(config, mm, group)
            except Exception as exc:
                print(f"[DAILY SUMMARY ERROR] 「{group}」生成失败: {type(exc).__name__}: {exc}", flush=True)
                summary = None
            if summary:
                summaries[group] = summary
        if not summaries:
            continue

        send_at = (request_at + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        wait = (send_at - datetime.now()).total_seconds()
        if wait > 0:
            print(
                f"[DAILY SUMMARY] 已生成 {len(summaries)} 个群的总结，等待 {wait:.0f}s 后 00:00 准点发送",
                flush=True,
            )
            time.sleep(wait)
        for group, summary in summaries.items():
            try:
                send(target=group, content=summary, mode="wechat_text")
                print(f"[DAILY SUMMARY] 已发送到「{group}」（{len(summary)} 字）", flush=True)
            except Exception as exc:
                print(f"[DAILY SUMMARY ERROR] 「{group}」发送失败: {type(exc).__name__}: {exc}", flush=True)


def start(dispatcher):
    """启动每日总结线程（幂等，热重载后旧进程退出线程随之消失）。"""
    global _thread_started
    cfg = ((getattr(dispatcher, "config", {}) or {}).get("daily_summary") or {})
    if not cfg.get("enabled", False):
        print("[DAILY SUMMARY] disabled in config, thread not started", flush=True)
        return False
    with _thread_lock:
        if _thread_started:
            return True
        _thread_started = True
    t = threading.Thread(target=_loop, args=(dispatcher,), daemon=True)
    t.start()
    return True
