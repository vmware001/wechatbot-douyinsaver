# -*- coding: utf-8 -*-
"""小米 MiMo TTS：文字合成语音。

接口形态（2026-09-27 实测确认，不是猜的）：
    POST https://api.xiaomimimo.com/v1/chat/completions
    {"model": "mimo-v2.5-tts", "messages": [{"role": "assistant", "content": "要说的文本"}]}
    → choices[0].message.audio.data = base64 的 WAV（RIFF 头）
要点：
  * 走 chat/completions，**不是** /audio/speech（那个 404）；
  * 文本必须放在 **assistant** 消息里 —— 放 user 会报
    "messages must contain an assistant role for TTS model"；
  * 音色参数可选，不传就用默认音色。
输出文件必须落在**发送端白名单目录**（%TEMP%\\WechatRobot\\voice）内，
否则 /wxVoice 会以「路径不允许」拒绝（实测踩过）。
"""

import base64
import os
import tempfile
import time
import wave

import requests

DEFAULT_BASE_URL = "https://api.xiaomimimo.com/v1"
DEFAULT_MODEL = "mimo-v2.5-tts"


class TtsService:
    def __init__(self, config=None):
        cfg = (config or {}).get("tts") or {}
        if not isinstance(cfg, dict):
            cfg = {}
        self.enabled = bool(cfg.get("enabled", False))
        self.api_key = str(cfg.get("api_key") or "").strip()
        self.model = str(cfg.get("model") or DEFAULT_MODEL).strip() or DEFAULT_MODEL
        self.base_url = str(cfg.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        self.voice = str(cfg.get("voice") or "").strip()
        self.max_chars = max(1, min(int(cfg.get("max_chars") or 120), 500))
        try:
            self.timeout = max(5.0, min(float(cfg.get("timeout_seconds") or 90), 180.0))
        except (TypeError, ValueError):
            self.timeout = 90.0

    @property
    def ready(self):
        return bool(self.enabled and self.api_key)

    @staticmethod
    def out_dir():
        """发送端白名单目录之一，别改（改了语音发不出去）。"""
        path = os.path.join(tempfile.gettempdir(), "WechatRobot", "voice")
        os.makedirs(path, exist_ok=True)
        return path

    def generate(self, text):
        """返回 {"success": bool, "path":…, "duration":…, "reason":…, "error":…}。"""
        text = str(text or "").strip()
        if not text:
            return {"success": False, "reason": "empty", "error": "文本为空"}
        if len(text) > self.max_chars:
            return {
                "success": False,
                "reason": "too_long",
                "error": f"文本 {len(text)} 字超过上限 {self.max_chars}",
            }
        body = {
            "model": self.model,
            "messages": [{"role": "assistant", "content": text}],
        }
        if self.voice:
            body["audio"] = {"voice": self.voice}
        try:
            resp = requests.post(
                self.base_url + "/chat/completions",
                headers={
                    "Authorization": "Bearer " + self.api_key,
                    "Content-Type": "application/json",
                },
                json=body,
                timeout=self.timeout,
            )
        except Exception as exc:
            return {"success": False, "reason": "error", "error": f"{type(exc).__name__}: {exc}"}
        if resp.status_code != 200:
            return {"success": False, "reason": "error",
                    "error": f"HTTP {resp.status_code}: {str(resp.text)[:200]}"}
        try:
            data = resp.json()
            message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
            audio = message.get("audio") or {}
            raw = base64.b64decode(str(audio.get("data") or ""))
        except Exception as exc:
            return {"success": False, "reason": "error", "error": f"响应解析失败: {exc}"}
        if len(raw) < 44 or raw[:4] != b"RIFF":
            return {"success": False, "reason": "error", "error": "返回内容不是 WAV"}
        path = os.path.join(self.out_dir(), f"tts_{int(time.time() * 1000)}.wav")
        try:
            with open(path, "wb") as fh:
                fh.write(raw)
            with wave.open(path, "rb") as wav:
                duration = wav.getnframes() / float(wav.getframerate() or 1)
        except Exception as exc:
            return {"success": False, "reason": "error", "error": f"写音频失败: {exc}"}
        return {
            "success": True,
            "path": path,
            "duration": duration,
            "bytes": len(raw),
            "chars": len(text),
        }
