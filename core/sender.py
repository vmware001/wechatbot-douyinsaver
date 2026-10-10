import os
import random
import threading
import time

import requests
from mcrcon import MCRcon


SEND_DELAY_CONFIG = {
    "enabled": False,
    "min_seconds": 0.0,
    "max_seconds": 0.0
}

# Optional observers are kept outside the LLM layer so every successfully
# delivered Bot text (including plugin output) can be counted consistently.
_SEND_LISTENER_LOCK = threading.RLock()
_SEND_LISTENERS = {}

# 全局发送互斥锁：串行化所有发送（文本/文件/语音/RCON），
# 防止多个线程/发送路径同时操作微信窗口（切群、点击、键盘注入、语音 Shift 长按）造成冲突。
_SEND_LOCK = threading.RLock()

# 前端发送服务地址（sender_service.py），由 configure() 从 config.json 的 frontend.sender_url 读取
_SENDER_BASE_URL = "http://127.0.0.1:9999"

# 前端发送服务的 API token，由 configure() 从 config.json 顶层 token 读取
_SENDER_TOKEN = ""


def _post_wxsend(data, timeout):
    """POST JSON 到前端发送服务。连接失败时重试 2 次（指数退避），
    仍失败则抛出带明确提示的异常（由 send() 统一捕获转为 (False, msg)）。
    返回前端响应体（dict），供调用方判断是否被出站过滤拦下。"""
    url = f"{_SENDER_BASE_URL}/wxSend"
    headers = {"X-Token": _SENDER_TOKEN} if _SENDER_TOKEN else None
    last_err = None
    for attempt in range(3):
        try:
            resp = requests.post(url, json=data, headers=headers, timeout=timeout)
            try:
                body = resp.json()
            except Exception:
                body = {}
            if resp.status_code != 200:
                detail = body.get("error") if isinstance(body, dict) else None
                raise RuntimeError(
                    f"前端发送服务返回错误（HTTP {resp.status_code}）：{detail or resp.text[:200]}"
                )
            if isinstance(body, dict) and body.get("code"):
                raise RuntimeError(f"前端发送服务返回错误：{body.get('error')} {body.get('details') or ''}".strip())
            return body if isinstance(body, dict) else {}
        except requests.exceptions.ConnectionError as e:
            last_err = e
            if attempt < 2:
                wait = 0.5 * (2 ** attempt)
                print(f"[SEND] 前端发送服务未连接（{_SENDER_BASE_URL}），{wait:.1f}s 后重试（{attempt + 1}/2）", flush=True)
                time.sleep(wait)
    raise RuntimeError(
        f"前端发送服务未连接（{_SENDER_BASE_URL}），请先运行 start_frontend.bat；原始错误：{last_err}"
    )


def register_send_listener(name, callback):
    """Register or replace a best-effort listener for successful sends."""
    key = str(name or "").strip()
    if not key:
        return False
    with _SEND_LISTENER_LOCK:
        if callable(callback):
            _SEND_LISTENERS[key] = callback
        else:
            _SEND_LISTENERS.pop(key, None)
    return True


def _notify_send_listeners(target, mode, content=None, file_path=None):
    with _SEND_LISTENER_LOCK:
        listeners = list(_SEND_LISTENERS.values())
    for callback in listeners:
        try:
            callback(target=target, mode=mode, content=content, file_path=file_path)
        except Exception as exc:
            print(f"[SEND LISTENER ERROR] {exc}")


def prepare_group_chat(target):
    """LLM 已受理、回复尚未生成时，让前端预先把微信窗口切到目标群（fire-and-forget）。

    纯省时优化：回复生成后的发送路径仍会自行校验/搜索目标群，
    预热失败、预热到错误的群都不影响发送正确性（发送前缓存校验会自动纠正）。

    注意：这里仍然是 fire-and-forget（不阻塞、失败不改控制流），但会把前端的
    "没切过去"结论打成 warn —— 否则一旦前端谎报成功（2026-09-26 23:09 那次：
    日志说已进入目标群、窗口其实停在私聊 root），事后完全无法解释。
    """
    group = str(target or "").strip()
    if not group:
        return

    def _do():
        try:
            resp = requests.post(
                f"{_SENDER_BASE_URL}/wxPrepare",
                json={"target": group},
                headers={"X-Token": _SENDER_TOKEN} if _SENDER_TOKEN else None,
                timeout=20,
            )
            try:
                body = resp.json() if resp.content else {}
            except Exception:
                body = {}
            if not isinstance(body, dict):
                body = {}
            if resp.status_code != 200:
                detail = body.get("details") or body.get("error") or resp.text[:160]
                print(f"[PREPARE] 预切群未成功（目标={group} HTTP {resp.status_code}）：{detail}",
                      flush=True)
            elif body.get("verified") is False:
                print(f"[PREPARE] 预切群未经标题校验（目标={group}，"
                      f"sender.verify_chat_title 可能已关闭），按未确认处理", flush=True)
        except Exception as exc:
            print(f"[PREPARE] 预切群请求失败（目标={group}）：{type(exc).__name__}: {exc}",
                  flush=True)

    threading.Thread(target=_do, daemon=True, name=f"prepare-{group}").start()


# =========================
# 🚫 内容合法性检查（核心）
# =========================
def _is_invalid_content(content):
    if content is None:
        return True

    if isinstance(content, str):
        c = content.strip().lower()

        # 拦截“伪空内容”
        if c in ("none", "null", "undefined", ""):
            return True

    return False


# =========================
# 🧠 文本处理
# =========================
def _normalize_content(content):
    if content is None:
        return ""

    content = str(content)

    # 再次兜底防止 "None" 漏网
    if content.strip().lower() in ("none", "null", "undefined"):
        return ""

    return content.replace("\r\n", "\n").replace("\r", "\n")


def configure(config=None):
    delay_cfg = (config or {}).get("send_delay", {}) or {}

    enabled = bool(delay_cfg.get("enabled", False))

    try:
        min_seconds = float(delay_cfg.get("min_seconds", 0))
    except Exception:
        min_seconds = 0.0

    try:
        max_seconds = float(delay_cfg.get("max_seconds", 0))
    except Exception:
        max_seconds = 0.0

    min_seconds = max(0.0, min_seconds)
    max_seconds = max(0.0, max_seconds)

    if max_seconds < min_seconds:
        min_seconds, max_seconds = max_seconds, min_seconds

    SEND_DELAY_CONFIG["enabled"] = enabled
    SEND_DELAY_CONFIG["min_seconds"] = min_seconds
    SEND_DELAY_CONFIG["max_seconds"] = max_seconds

    # 前端发送服务地址（前后端分离：sender_service.py 独立进程）
    global _SENDER_BASE_URL
    frontend_cfg = (config or {}).get("frontend", {}) or {}
    sender_url = str(frontend_cfg.get("sender_url") or "").strip()
    if sender_url:
        _SENDER_BASE_URL = sender_url.rstrip("/")

    # 前端发送服务 API token（/wxSend 鉴权头）
    global _SENDER_TOKEN
    _SENDER_TOKEN = str((config or {}).get("token") or "").strip()


def preview_delay_seconds(mode="wechat_text"):
    if mode not in ("wechat_text", "wechat_file", "wechat_voice"):
        return 0.0

    if not SEND_DELAY_CONFIG["enabled"]:
        return 0.0

    min_seconds = SEND_DELAY_CONFIG["min_seconds"]
    max_seconds = SEND_DELAY_CONFIG["max_seconds"]

    if max_seconds <= 0:
        return 0.0

    if min_seconds == max_seconds:
        return min_seconds

    return random.uniform(min_seconds, max_seconds)


def _apply_send_delay(mode, delay_seconds=None):
    actual_delay = delay_seconds
    if actual_delay is None:
        actual_delay = preview_delay_seconds(mode)

    try:
        actual_delay = float(actual_delay)
    except Exception:
        actual_delay = 0.0

    actual_delay = max(0.0, actual_delay)

    if actual_delay > 0:
        print(f"[SEND DELAY] mode={mode}, sleep={actual_delay:.3f}s")
        time.sleep(actual_delay)

    return actual_delay


# =========================
# 💬 统一发送入口
# =========================
def send(target, content=None, file_path=None, mode="wechat_text", rcon=None, delay_seconds=None, duration=None, voice_start=None, silent_on_filter=False):
    """
    mode:
        wechat_text
        wechat_file
        wechat_voice
        rcon

    silent_on_filter:
        文本命中出站敏感词时【不要】兜底话术、也【不要】发任何内容，
        返回 (False, "blocked: <命中词>")。仅随机接话等被动场景使用；
        @触发的正常回复保持默认 False（发兜底话术，用户不会看到空白）。
    """

    with _SEND_LOCK:
        try:

            # =========================
            # 🚨 全局拦截（修复版）
            # =========================
            if mode in ("wechat_text", "rcon") and _is_invalid_content(content):
                print("[SEND BLOCK] invalid content -> blocked")
                return False, "invalid content"

            # =========================
            # 💬 微信文本
            # =========================
            if mode == "wechat_text":
                content = _normalize_content(content)

                if not content.strip():
                    print("[SEND BLOCK] empty content after normalize")
                    return False, "empty content"

                _apply_send_delay(mode, delay_seconds=delay_seconds)
                result = _send_wechat_text(target, content, silent_on_filter=silent_on_filter)
                if result and result[0]:
                    _notify_send_listeners(target, mode, content=content)
                return result

            # =========================
            # 📁 微信文件
            # =========================
            if mode == "wechat_file":
                _apply_send_delay(mode, delay_seconds=delay_seconds)
                result = _send_wechat_file(target, file_path)
                if result and result[0]:
                    _notify_send_listeners(target, mode, file_path=file_path)
                return result

            # =========================
            # 🎙️ 微信语音（注入虚拟麦克风）
            # =========================
            if mode == "wechat_voice":
                if _is_invalid_content(file_path):
                    print("[SEND BLOCK] invalid voice file -> blocked")
                    return False, "invalid voice file"
                _apply_send_delay(mode, delay_seconds=delay_seconds)
                result = _send_wechat_voice(target, file_path, duration, voice_start)
                if result and result[0]:
                    _notify_send_listeners(target, mode, file_path=file_path)
                return result

            # =========================
            # 🟢 RCON
            # =========================
            if mode == "rcon":
                if _is_invalid_content(content):
                    return False, "invalid content"

                return _send_rcon(content, rcon)

            return False, "unknown mode"

        except Exception as e:
            return False, str(e)


    # =========================
    # 💬 微信文本
    # =========================
def _send_wechat_text(target, content, silent_on_filter=False):
    data = {
        "target": target,
        "content": content
    }
    if silent_on_filter:
        # 让前端在命中出站敏感词时直接放弃发送（不发兜底话术），并回报 sent=False
        data["silent_on_filter"] = True

    # 分段粘贴一次发送可能耗时10秒以上，超时放宽到60秒，
    # 否则客户端先放弃、服务端发完后回写响应时连接已断
    body = _post_wxsend(data, timeout=60)

    filtered = body.get("filtered")
    if body.get("sent") is False:
        match = (filtered or {}).get("match")
        print(
            f"[SEND FILTER] 出站内容命中敏感词 {match!r}，已放弃发送"
            f"（silent_on_filter，目标={target}）",
            flush=True,
        )
        return False, f"blocked: {match or 'outbound filter'}"
    if filtered:
        print(
            f"[SEND FILTER] 出站内容命中敏感词 {filtered.get('match')!r}，"
            f"已替换为兜底话术（目标={target}）",
            flush=True,
        )
    return True, None


# =========================
# 📁 微信文件
# =========================
def _send_wechat_file(target, file_path):
    if not file_path:
        return False, "file_path is empty"

    file_path = str(file_path)

    if not os.path.exists(file_path):
        return False, "file not found"

    data = {
        "target": target,
        "file": file_path
    }

    _post_wxsend(data, timeout=10)
    return True, None


# =========================
# 🎙️ 微信语音
# =========================
def _send_wechat_voice(target, voice_path, duration=None, voice_start=None):
    if not voice_path:
        return False, "voice_path is empty"

    voice_path = str(voice_path)

    if not os.path.exists(voice_path):
        return False, "voice file not found"

    data = {
        "target": target,
        "voice": voice_path
    }
    if duration is not None:
        try:
            # 支持双精度浮点秒（如 29.5），钳制到 1~30 秒
            data["duration"] = max(1.0, min(float(duration), 30.0))
        except (TypeError, ValueError):
            return False, "invalid duration"
    if voice_start is not None:
        try:
            # 起始秒（从音频第 N 秒开始注入），非负
            data["start"] = max(0.0, float(voice_start))
        except (TypeError, ValueError):
            return False, "invalid voice start"

    # 语音注入最长 60 秒 + 群聊切换耗时，超时放宽到 90 秒
    _post_wxsend(data, timeout=90)
    return True, None


# =========================
# 🟢 RCON
# =========================
def _send_rcon(content, rcon):
    if not rcon:
        return False, "missing rcon config"

    host = rcon.get("host")
    port = rcon.get("port")
    password = rcon.get("password")

    if not all([host, port, password]):
        return False, "invalid rcon config"

    with MCRcon(host, password, port) as mcr:
        _send_multiline_rcon(mcr, content)

    return True, None


# =========================
# 🧠 RCON 多行
# =========================
def _send_multiline_rcon(mcr, msg: str):
    if not msg:
        return

    for line in msg.split("\n"):
        line = line.strip()
        if not line:
            continue

        line = line.replace("\\", "\\\\").replace('"', '\\"')

        cmd = f'tellraw @a {{"text":"{line}","color":"green"}}'
        mcr.command(cmd)
