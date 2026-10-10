"""把一条过长的回复按语义边界切成多条微信消息。

为什么单独放一个模块：微信单条消息其实能塞下几千字，但一条要滚好几屏的
消息在群聊里既难读、也更容易触发风控。此前 /pro 的做法是"整条不拆分 +
提示词里限制总字数"（见 core/worker.py 的 _send_structured_result），
结果是模型被逼着压缩省略，或者干脆在句子中途被输出上限截断，群里看到的
是残句。

现在的分工：
  * 提示词只要求"写完整"，不再逼模型压字数；
  * 发送前在这里按语义边界把长回复拆成多条，每条不超过
    llm.pro.max_message_chars（默认 1200 字）。

拆分优先级（越靠前越优先）：
  1. 段落边界（空行，即两个及以上换行）；
  2. 句子边界（。！？；! ? ; 以及单个换行）；
  3. 硬切（只有单个"原子段"本身就超过上限时才会走到，切完打日志）。

不丢内容的保证：所有分片按顺序拼接后，忽略空白差异应与原文一致，即
``re.sub(r"\\s+", "", "".join(parts)) == re.sub(r"\\s+", "", 原文)``。
"""

import re

# 单条微信消息的字数上限：保守取 1200 字。微信本身能容纳几千字，但
# 群聊里一条 2000+ 字的消息要滚三四屏，阅读体验差，也更容易被风控盯上；
# 1200 字大约是手机上一屏半，接近正常人连发消息的体量。
DEFAULT_MAX_MESSAGE_CHARS = 1200
# 上下限：太小会把一句话反复切碎（下限 200），太大就失去分段的意义（上限 4000）。
MIN_MAX_MESSAGE_CHARS = 200
MAX_MAX_MESSAGE_CHARS = 4000

# 段落边界：一个空行（两个及以上换行，允许行间有空白字符）。
# 与 llm/core/response_parser.py 里 pro 模式"空两行压缩为空一行"的口径一致：
# 拆分的第一个切点就落在这里。
_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n[ \t\n]*")

# 句子边界：中文句末标点（含省略号）或英文句末标点，后面允许跟收尾引号/括号。
_SENTENCE_END_RE = re.compile(r"[。！？；!?;…]+[”’\"'）)】」』]*")

# 句子内部的换行也算句子级边界（模型经常用单换行代替分段）。
_LINE_BREAK_RE = re.compile(r"\n")


def normalize_max_message_chars(value, default=DEFAULT_MAX_MESSAGE_CHARS):
    """把配置里的单条上限夹到合理区间。取不到/非法时回退到默认值。"""
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    if limit <= 0:
        return default
    return max(MIN_MAX_MESSAGE_CHARS, min(limit, MAX_MAX_MESSAGE_CHARS))


def split_at_sentence_boundaries(text):
    """按句子边界（句末标点、换行）切开，返回原子片段列表。

    标点保留在前一片的末尾，调用方直接拼接即可，不会丢字符。
    """
    sentences = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        end = match.end()
        if end > start:
            sentences.append(text[start:end])
            start = end
    if start < len(text):
        sentences.append(text[start:])
    if not sentences:
        sentences = [text]

    # 再在每片内部按换行切一刀（换行属于句子级边界）
    atoms = []
    for sentence in sentences:
        if "\n" not in sentence:
            atoms.append(sentence)
            continue
        pos = 0
        for match in _LINE_BREAK_RE.finditer(sentence):
            end = match.end()
            if end > pos:
                atoms.append(sentence[pos:end])
                pos = end
        if pos < len(sentence):
            atoms.append(sentence[pos:])
    return [atom for atom in atoms if atom.strip()]


def split_reply_for_wechat(text, max_chars=DEFAULT_MAX_MESSAGE_CHARS):
    """把一条回复切成多条微信消息，每片长度 <= max_chars（硬切时除外）。

    返回去掉首尾空白的非空分片列表；原文不超过上限时原样返回单元素列表。
    """
    value = str(text or "").strip()
    if not value:
        return []

    limit = normalize_max_message_chars(max_chars)
    if len(value) <= limit:
        return [value]

    parts = []
    current = ""

    for paragraph in _PARAGRAPH_RE.split(value):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        if len(paragraph) <= limit:
            current = _push(parts, current, paragraph, limit, sep="\n\n")
            continue

        # 单个段落自己就超长：退到句子/换行边界，段落内部允许被切开
        current = _flush(parts, current)
        for atom in split_at_sentence_boundaries(paragraph):
            if len(atom) <= limit:
                current = _push(parts, current, atom, limit, sep="")
                continue
            # 一句话本身超过上限：只能在字数上限处硬切，并留下日志
            current = _flush(parts, current)
            pieces = _hard_cut(atom, limit)
            print(
                f"[SPLIT] 单个语义段 {len(atom)} 字超过 {limit} 字上限，"
                f"按最大长度硬切为 {len(pieces)} 段（无法在句子边界断开）",
                flush=True,
            )
            parts.extend(piece.strip() for piece in pieces[:-1] if piece.strip())
            current = pieces[-1]

    _flush(parts, current)
    return [part for part in parts if part]


def _push(parts, current, chunk, limit, sep):
    """把 chunk 接到 current 后面；接不下就把 current 收尾、chunk 另起一片。"""
    if not current:
        return chunk
    if len(current) + len(sep) + len(chunk) <= limit:
        return current + sep + chunk
    parts.append(current.strip())
    return chunk


def _flush(parts, current):
    """收尾当前片，返回空串（方便写成 current = _flush(parts, current)）。"""
    text = current.strip()
    if text:
        parts.append(text)
    return ""


def _hard_cut(text, limit):
    return [text[i:i + limit] for i in range(0, len(text), limit)]
