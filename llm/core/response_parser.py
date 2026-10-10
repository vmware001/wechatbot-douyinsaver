import json
import re


DEFAULT_ERROR_MESSAGE = "LLM转发失败：返回内容格式不符合预期"
BALANCE_ERROR_MESSAGE = "余额不足，请检查llm余额是否充足"

# 模型返回的 JSON 契约不合法时，原始输出最多发多少字。
# 300 字对闲聊足够（闲聊回复本来就很短），同时堵住提示词注入诱导模型
# 全文外泄系统提示词/记忆的通道。/pro 的答案本来就长，会传更大的上限，
# 否则群里看到的就是被砍断的半截回答加一个省略号（实测 2026-09-25
# run.log:84092 就是这样）。
DEFAULT_RAW_FALLBACK_CHARS = 300


def _shorten_raw_text(text, limit=120):
    raw = str(text or "").strip()
    if not raw:
        return ""

    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    return raw if len(raw) <= limit else f"{raw[:limit]}..."


def build_error_response(reason=None, raw_text=None):
    message = str(reason or DEFAULT_ERROR_MESSAGE).strip() or DEFAULT_ERROR_MESSAGE
    raw_preview = _shorten_raw_text(raw_text)

    if raw_preview:
        message = f"{message}\n原始返回：{raw_preview}"

    return {"messages": [message], "animation": None}


def build_raw_response(text, reason=DEFAULT_ERROR_MESSAGE, limit=DEFAULT_RAW_FALLBACK_CHARS):
    """Send the model text when its JSON contract is invalid.

    安全折中：原始输出截断到 limit 字符（默认 300）。闲聊回复本来就很短，
    截断不影响；但可堵住提示词注入诱导模型全文外泄系统提示词/记忆的通道。
    limit <= 0 表示不截断。
    """
    raw = str(text or "").strip()
    if raw:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = DEFAULT_RAW_FALLBACK_CHARS
        if limit > 0 and len(raw) > limit:
            print(f"[PARSE] 模型原始输出 {len(raw)} 字，截断到 {limit} 字后发送", flush=True)
            raw = raw[:limit] + "…"
        return {"messages": [raw], "animation": None}
    return build_error_response(reason)


def is_insufficient_balance_error(error) -> bool:
    text = str(error or "").lower()
    # 词边界匹配：避免行号/端口/ID 里的子串 "402" 误判为余额不足
    return bool(re.search(r"\b402\b", text))


def build_balance_error_response():
    return {
        "messages": [BALANCE_ERROR_MESSAGE],
        "animation": None,
        "_balance_error": True,
        "_llm_ok": False,
    }


FALLBACK_RESPONSE = build_error_response()


def _strip_fences(text):
    """Strip one optional ```json ... ``` wrapper from model output."""
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        str(text or ""),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        return fenced.group(1).strip()
    return str(text or "").strip()


def _json_substring(text):
    """Return the substring from the first '{' to the last '}' if present."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    return text[start:end + 1]


def _repair_truncated_json(text):
    """Best-effort repair for JSON truncated mid-structure (e.g. cut off by
    the model at max output length). Appends the missing closers for any
    unclosed arrays/objects that started before the truncation point."""
    stack = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch in "[{":
                stack.append(ch)
            elif ch in "]}":
                if stack:
                    stack.pop()
        index += 1
    if in_string:
        # Unterminated string; do not try to guess the value.
        return None
    if not stack:
        return None
    closers = "".join("]" if opener == "[" else "}" for opener in reversed(stack))
    candidate = text.rstrip()
    while candidate.endswith(","):
        candidate = candidate[:-1].rstrip()
    try:
        return json.loads(candidate + closers)
    except Exception:
        return None


def _strip_trailing_commas(text):
    """Remove commas that directly precede a closing '}' or ']' (a common
    model mistake that strict json.loads rejects)."""
    result = []
    index = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if ch == '"':
            result.append(ch)
            index += 1
            while index < length:
                ch2 = text[index]
                result.append(ch2)
                if ch2 == "\\" and index + 1 < length:
                    result.append(text[index + 1])
                    index += 2
                    continue
                if ch2 == '"':
                    index += 1
                    break
                index += 1
            continue
        if ch == ",":
            probe = index + 1
            while probe < length and text[probe] in " \t\r\n":
                probe += 1
            if probe < length and text[probe] in "}]":
                index += 1
                continue
        result.append(ch)
        index += 1
    return "".join(result)


def load_json_lenient(text):
    """Parse model JSON tolerating fences, surrounding prose, trailing
    commas, and truncation.

    Order of attempts:
      1. exact json.loads
      2. after stripping a ```json``` fence
      3. after removing trailing commas before closers
      4. the substring between the first '{' and the last '}'
      5. truncated-JSON repair on that substring
    Raises ValueError when no attempt succeeds.
    """
    raw = _strip_fences(text)
    if not raw:
        raise ValueError("empty JSON response")
    try:
        return json.loads(raw)
    except Exception:
        pass
    raw = _strip_trailing_commas(raw)
    try:
        return json.loads(raw)
    except Exception:
        pass
    candidate = _json_substring(raw)
    if candidate is None:
        candidate = raw
    try:
        return json.loads(candidate)
    except Exception:
        pass
    repaired = _repair_truncated_json(candidate)
    if repaired is not None:
        return repaired
    raise ValueError(f"invalid JSON response: {_shorten_raw_text(text)}")


def _escape_stray_quotes(text):
    """Escape unescaped double quotes inside JSON string values.

    mimo 长回复经常在字符串内容里直接写 ASCII 双引号（如 是"每个点各自收敛"），
    严格 json.loads 直接报废。启发式：字符串内遇到 " 时向后看，跳过空白后
    下一个字符是 , ] } : 之一才视为结构结束引号，否则当作内容里的杂散引号转义掉。
    """
    result = []
    in_string = False
    escaped = False
    index = 0
    length = len(text)
    while index < length:
        ch = text[index]
        if in_string:
            if escaped:
                result.append(ch)
                escaped = False
            elif ch == "\\":
                result.append(ch)
                escaped = True
            elif ch == '"':
                probe = index + 1
                while probe < length and text[probe] in " \t\r\n":
                    probe += 1
                if probe >= length or text[probe] in ",]}:":
                    result.append(ch)
                    in_string = False
                else:
                    result.append('\\"')
            else:
                result.append(ch)
            index += 1
            continue
        result.append(ch)
        if ch == '"':
            in_string = True
        index += 1
    return "".join(result)


def _load_model_json(text):
    raw = _strip_fences(text)
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        return json.loads(_escape_stray_quotes(raw))
    except Exception:
        pass
    return load_json_lenient(text)


_SALVAGE_BLOCK_RE = re.compile(r'"messages"\s*:\s*\[((?:"(?:[^"\\]|\\.)*"[,]?\s*)+)', re.DOTALL)
_SALVAGE_ITEM_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _salvage_messages(text):
    """最后手段：JSON 结构彻底坏掉（如模型把 "]}" 写成 "}]"）时，
    用正则从 "messages": [...] 块里直接捞字符串项。捞不到返回 None。"""
    raw = str(text or "")
    items = []
    block = _SALVAGE_BLOCK_RE.search(raw)
    if block:
        for m in _SALVAGE_ITEM_RE.finditer(block.group(1)):
            try:
                items.append(json.loads('"' + m.group(1) + '"'))
            except Exception:
                items.append(m.group(1))
    if not items:
        # 字符串未闭合（输出被 max_tokens 截断）：捞已生成的部分内容，
        # 半截回复也比把 JSON 碎片发进群强
        tail = re.search(r'"messages"\s*:\s*\[\s*"((?:[^"\\]|\\.)*)$', raw, re.DOTALL)
        if tail:
            partial = tail.group(1)
            try:
                partial = json.loads('"' + partial + '"')
            except Exception:
                partial = partial.replace("\\n", "\n").replace('\\"', '"')
            partial = partial.strip()
            if partial:
                items.append(partial)
    items = [str(item).strip() for item in items if str(item).strip()]
    return items or None


_BLANK_LINES_RE = re.compile(r"\n(?:[ \t]*\n){2,}")


def normalize_reply_newlines(messages, pro_mode=False):
    """统一回复里的空行规则。

    pro 模式：不允许连续空两行及以上（压缩为一个空行），
    并去掉 markdown 加粗标记和 LaTeX 的 $ 符号（微信不渲染，会原样显示）。
    普通模式：连续空两行及以上视为分消息发送点，拆成独立消息；
    单个空行保留在原消息内照常发送。
    """
    out = []
    for item in messages or []:
        text = str(item or "").strip()
        if not text:
            continue
        if pro_mode:
            text = _BLANK_LINES_RE.sub("\n\n", text)
            text = text.replace("**", "").replace("__", "").replace("$", "").strip()
            if text:
                out.append(text)
        else:
            parts = [p.strip() for p in _BLANK_LINES_RE.split(text) if p.strip()]
            out.extend(parts or [text])
    return out


_CHAT_SPLIT_RE = re.compile(r"[,，;；。！？!?]+")


def _split_chat_message(text):
    """Turn a short multi-clause chat reply into independently sent turns.

    本部署禁用分句：用户要求每次回复只发一条消息，多逗号短句不再拆分。
    """
    value = str(text or "").strip()
    if len(value) <= 8 or "http://" in value or "https://" in value:
        return [value] if value else []
    return [value]


def _normalize_chat_messages(items):
    result = []
    for item in items:
        result.extend(_split_chat_message(item))
    return result


_STYLE_SWITCH_FIELD_LIMITS = (
    ("scene", 40),
    ("situation", 160),
    ("slang_type", 40),
    ("emotion", 40),
    ("pattern", 240),
    ("reason", 200),
)


def _normalize_style_switch(value):
    """Normalize the optional style_switch reply field.

    Returns None when absent/invalid/keep. "clear" requests an explicit reset,
    otherwise a "set" carries the scene/situation/type/emotion the model wants
    to hold for the rest of the cycle. Unknown fields are ignored so a bad
    switch never breaks the reply contract.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return None
    if bool(value.get("clear")):
        return {"action": "clear"}
    if bool(value.get("keep")):
        return None
    fields = {}
    for key, limit in _STYLE_SWITCH_FIELD_LIMITS:
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            fields[key] = raw.strip()[:limit]
    if not fields:
        return None
    fields["action"] = "set"
    return fields

def _normalize_idle_content_plan(value):
    """Validate the optional planner action without trusting free-form data."""
    if not isinstance(value, dict):
        return None
    action = str(value.get("action") or "skip").strip().lower()
    if action not in {"share", "skip"}:
        return None
    result = {"action": action}
    content_hash = str(value.get("content_hash") or "").strip()
    if content_hash:
        result["content_hash"] = content_hash[:80]
    return result


def parse_llm_response(text: str, emoji_list: list, raw_fallback_limit: int = DEFAULT_RAW_FALLBACK_CHARS) -> dict:
    """Parse the public response contract.

    Known keys are optional. Unknown keys are intentionally ignored. A known
    key with the wrong JSON type is rejected instead of being silently coerced.

    raw_fallback_limit：JSON 契约不合法时原始输出最多发多少字
    （/pro 传更大的值，避免长答案被砍成半截 + 省略号）。
    """
    if not str(text or "").strip():
        return {"messages": [], "animation": None}

    try:
        data = _load_model_json(text)
    except Exception:
        salvaged = _salvage_messages(text)
        if salvaged:
            joined = "\n\n".join(salvaged).strip()
            raw_len = len(str(text or ""))
            # 正则只捞回一小段时，别把模型已生成的内容丢掉：
            # 实测 2026-09-27 03:23 一次 /pro，模型 output=3450 token，
            # 正则只捞回 38 字（"…我们希望找到一个"断在半句），其余全丢。
            # 这时改用"整段原文剥掉 JSON 外壳"，宁可多带一点标点也别只剩碎片。
            if raw_len and len(joined) < raw_len * 0.5:
                full = re.sub(r'^\s*\{?\s*"?messages"?\s*:\s*\[\s*"?', '', str(text or "").strip(), flags=re.S)
                full = re.sub(r'"\s*\]?\s*\}?\s*(?:,?\s*"animation".*)?$', '', full, flags=re.S)
                full = full.replace("\\n", "\n").replace('\\"', '"').replace("\\t", "\t").strip()
                if len(full) > len(joined):
                    print(
                        f"[PARSE SALVAGE] 正则只捞回 {len(joined)}/{raw_len} 字，"
                        f"改用整段原文兜底 {len(full)} 字",
                        flush=True,
                    )
                    return {"messages": [full[:raw_fallback_limit]], "animation": None}
            print("[PARSE SALVAGE] JSON结构损坏，用正则捞出 messages 内容", flush=True)
            return {"messages": salvaged, "animation": None}
        return build_raw_response(text, "LLM转发失败：返回内容不是合法JSON", raw_fallback_limit)

    if not isinstance(data, dict):
        return build_raw_response(text, "LLM转发失败：返回JSON不是对象", raw_fallback_limit)

    if "messages" in data and not isinstance(data["messages"], list):
        return build_raw_response(text, "LLM转发失败：messages字段格式错误", raw_fallback_limit)

    if "animation" in data and data["animation"] is not None and not isinstance(data["animation"], str):
        return build_raw_response(text, "LLM转发失败：animation字段格式错误", raw_fallback_limit)

    normalized_messages = []
    for item in data.get("messages", []):
        if not isinstance(item, str):
            return build_raw_response(text, "LLM转发失败：messages必须是字符串数组", raw_fallback_limit)
        content = item.strip()
        if content:
            normalized_messages.extend(_split_chat_message(content))

    # mimo 偶尔把整个响应 JSON 再编码成字符串塞进 messages[0]（双重编码），
    # 检测到就解开一层，否则群里会看到 {"messages":[...]} 原文和字面 \n
    unwrapped = []
    for item in normalized_messages:
        stripped = item.strip()
        if stripped.startswith("{") and '"messages"' in stripped:
            try:
                inner = _load_model_json(stripped)
            except Exception:
                inner = None
            if (
                isinstance(inner, dict)
                and isinstance(inner.get("messages"), list)
                and all(isinstance(x, str) for x in inner["messages"])
            ):
                unwrapped.extend(x.strip() for x in inner["messages"] if x.strip())
                inner_animation = inner.get("animation")
                if data.get("animation") is None and isinstance(inner_animation, str) and inner_animation.strip():
                    data["animation"] = inner_animation.strip()
                continue
        unwrapped.append(item)
    normalized_messages = unwrapped

    valid_emoji_set = {
        str(item).strip()
        for item in (emoji_list or [])
        if str(item).strip()
    }

    animation = data.get("animation")
    if isinstance(animation, str):
        animation = animation.strip() or None
    if animation is not None and animation not in valid_emoji_set:
        animation = None

    if animation is None and normalized_messages:
        transferred_animation = None
        cleaned_messages = []

        for item in normalized_messages:
            if transferred_animation is None and item in valid_emoji_set:
                transferred_animation = item
                continue
            cleaned_messages.append(item)

        if transferred_animation is not None:
            animation = transferred_animation
            normalized_messages = cleaned_messages

    # Both known fields may be absent. This is a valid, empty response; the
    # worker will simply have nothing to send. Unknown keys were discarded by
    # constructing the return object below.
    # 本部署要求每次回复只发一条消息：LLM 请求时已限定1条，
    # 这里再强制截断，防止模型返回多条时全部发出
    result = {"messages": normalized_messages[:1], "animation": animation}
    style_switch = _normalize_style_switch(data.get("style_switch"))
    if style_switch:
        result["style_switch"] = style_switch
    return result


def parse_proactive_response(text: str, emoji_list: list, force_reply=False) -> dict:
    """Parse the batch decision returned by the proactive-reply prompt."""
    def _proactive_error(reason):
        # An empty final tool/assistant content is not useful group text.
        # Let forced requests use their normal fallback instead of sending
        # this parser diagnostic to the group.
        if not str(text or "").strip():
            return {
                "messages": [],
                "animation": None,
                "should_reply": bool(force_reply),
                "reply_to": [],
                "_valid": False,
            }
        result = build_raw_response(text, reason)
        result["_valid"] = False
        return result

    try:
        data = _load_model_json(text)
    except Exception:
        # 2026-09-27 起提示词改成**纯文本**（"要不要接话"由 2% 概率 / /btw 触发决定，
        # 不再让模型判断）。这里把整段话当成一条回复，补成最小结构走原校验路径。
        plain = str(text or "").strip()
        if plain and '"messages"' not in plain and '"should_reply"' not in plain:
            data = {"should_reply": True, "messages": [plain], "animation": None}
        else:
            return _proactive_error("LLM主动回复失败：返回内容不是合法JSON")

    if not isinstance(data, dict):
        return _proactive_error("LLM主动回复失败：返回JSON不是对象")

    if "should_reply" in data and not isinstance(data["should_reply"], bool):
        return _proactive_error("LLM主动回复失败：should_reply字段格式错误")

    if "reply_to" in data:
        reply_to = data["reply_to"]
        if (
            not isinstance(reply_to, list)
            or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in reply_to)
        ):
            return _proactive_error("LLM主动回复失败：reply_to字段格式错误")
    else:
        reply_to = []

    if "messages" in data and not isinstance(data["messages"], list):
        return _proactive_error("LLM主动回复失败：messages字段格式错误")
    if "messages" in data and any(not isinstance(item, str) for item in data["messages"]):
        return _proactive_error("LLM主动回复失败：messages必须是字符串数组")
    if "animation" in data and data["animation"] is not None and not isinstance(data["animation"], str):
        return _proactive_error("LLM主动回复失败：animation字段格式错误")

    core_data = {
        key: data[key]
        for key in ("messages", "animation")
        if key in data
    }
    normalized = parse_llm_response(json.dumps(core_data, ensure_ascii=False), emoji_list)
    should_reply = data.get(
        "should_reply",
        bool(normalized.get("messages") or normalized.get("animation")),
    )
    if force_reply:
        should_reply = True
    if not should_reply:
        normalized["messages"] = []
        normalized["animation"] = None

    result = {
        "messages": normalized.get("messages") or [],
        "animation": normalized.get("animation"),
        "should_reply": bool(should_reply),
        "reply_to": reply_to,
    }
    idle_content_plan = _normalize_idle_content_plan(data.get("share_idle_content"))
    if idle_content_plan:
        result["share_idle_content"] = idle_content_plan
    style_switch = _normalize_style_switch(data.get("style_switch"))
    if style_switch:
        result["style_switch"] = style_switch
    return result
