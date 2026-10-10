# -*- coding: utf-8 -*-
"""`/voice <文本>`：把文字合成语音发到当前会话（小米 MiMo TTS）。

触发方式就是命令本身——不做"每条回复都发语音"。
失败只回自然中文，绝不把内部报错发进群（沿用项目约定）。
"""

import os
import threading

COMMAND = "/voice"
DEFAULT_TARGET = "文件传输助手"

_SINGLE_FLIGHT = threading.Lock()
_service = None
_config = {}


def init(config):
    global _service, _config
    _config = config or {}
    try:
        from core.sender import configure as sender_configure
        sender_configure(_config)
    except Exception as exc:
        print(f"[TTS] 发送端配置失败: {exc}", flush=True)
    try:
        from services.tts import TtsService
        _service = TtsService(_config)
    except Exception as exc:
        _service = None
        print(f"[TTS] 初始化失败: {type(exc).__name__}: {exc}", flush=True)
        return False
    if _service.ready:
        print(
            f"[TTS] /voice 初始化完成：model={_service.model} "
            f"max_chars={_service.max_chars} voice={_service.voice or '默认'}",
            flush=True,
        )
    else:
        print("[TTS] /voice 初始化完成：未启用或未配置 api_key", flush=True)
    return True


def _get_target(context):
    context = context or {}
    return context.get("group") or context.get("user") or DEFAULT_TARGET


def handle(content, context):
    """content = /voice 后面的全部文本。"""
    text = str(content or "").strip()
    if not text:
        return "在 /voice 后面写上要说的话，比如：/voice 今天天气不错"

    service = _service
    if service is None or not service.ready:
        print("[TTS] 未配置或未启用，拒绝请求", flush=True)
        return "语音功能还没配置好，先找管理员开一下"

    if len(text) > service.max_chars:
        return f"太长了，{service.max_chars} 字以内才能转成语音"

    # 合成+发送串行化：语音发送要占用虚拟声卡，不能并发
    with _SINGLE_FLIGHT:
        result = service.generate(text)
        if not result.get("success"):
            print(
                f"[TTS ERROR] 合成失败 reason={result.get('reason')}: {result.get('error')}",
                flush=True,
            )
            return "语音生成失败了，等下再试试"
        path = result.get("path")
        try:
            from core.sender import send
            ok, reason = send(
                _get_target(context),
                file_path=path,
                mode="wechat_voice",
                duration=result.get("duration"),
            )
            if not ok:
                print(f"[TTS ERROR] 发送语音失败: {reason}", flush=True)
                return "语音合成了但没发出去，稍后再试"
            print(
                f"[TTS] 已发送 target={_get_target(context)} "
                f"{float(result.get('duration') or 0):.1f}s {result.get('bytes')}B",
                flush=True,
            )
            # 语音本身就是回复，不再另发文字（与 /image、/song 一致）
            return None
        finally:
            # 发送端发完通常会自己删，这里兜底清理（删不到就算了）
            try:
                if path and os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
