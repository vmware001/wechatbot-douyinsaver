import json
import os
import re
import tempfile
import threading
import time

from llm.config import get_llm_config


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GROUPS_DIR = os.path.join(BASE_DIR, "data", "groups")

# MemoryManager is instantiated by both Worker and LLMService. Keep the lock at
# module scope so those instances serialize access to the same JSON files.
_GROUP_FILE_LOCK = threading.RLock()


def server_id_match(stored, wanted):
    """历史里的 server_id 曾经过 JS Number 丢精度（只有前15位十进制可靠），
    精确匹配失败时退化为前15位前缀匹配。"""
    stored = str(stored or "")
    wanted = str(wanted or "")
    if not stored or not wanted:
        return False
    if stored == wanted:
        return True
    return len(wanted) > 15 and stored[:15] == wanted[:15]


class MemoryManager:
    def __init__(self):
        llm_config = get_llm_config()
        self.max_history_chars = int(
            llm_config.get("max_history_chars", llm_config.get("max_history", 5000))
        )
        self.history_expire_ms = int(llm_config.get("history_expire_ms", 600000))
        # 本部署：按条数而非时间截取上下文(最近50条)，0表示不按条数限制
        self.max_history_messages = int(llm_config.get("max_history_messages", 0))
        self.group_message_limit_chars = int(
            llm_config.get(
                "group_message_limit_chars",
                llm_config.get("group_message_limit", 2000),
            )
        )
        # Compatibility aliases for callers outside this module.
        self.max_history = self.max_history_chars
        self.group_message_limit = self.group_message_limit_chars

    @staticmethod
    def estimate_tokens(value) -> int:
        """Conservative tokenizer-free estimate used only for context safety."""
        text = str(value or "")
        cjk = sum("\u3400" <= char <= "\u9fff" for char in text)
        non_cjk = len(text) - cjk
        return cjk + max(0, (non_cjk + 3) // 4)

    def add_llm_message(self, group_id, nickname, content):
        """Compatibility wrapper; all new conversation messages use group history."""
        return self.add_group_message(
            group_id,
            nickname,
            content,
            is_bot=True,
        )

    def get_llm_history(self, group_id):
        # 历史统一走 group_messages.json，llm_history.json 已废弃
        return []

    def add_group_message(self, group_id, nickname, content, message_id=None,
                          local_id=None, server_id=None, session_id=None,
                          is_bot=False, role=None, batch_index=None,
                          prefix_used=False, is_at_bot=False,
                          is_mentioned=False, message_type=None,
                          timestamp=None):
        with _GROUP_FILE_LOCK:
            group_dir = self._ensure_group_dir(group_id)
            messages_path = os.path.join(group_dir, "group_messages.json")

            messages = self._load_json(messages_path, default=[])
            item = {
                "nickname": str(nickname or ""),
                # Keep sub-second precision so cycle cleanup can distinguish
                # messages arriving during curation from the cycle boundary.
                # 启动回填会传入微信原始时间戳，否则历史顺序会乱、24 小时统计也会错。
                "timestamp": self._resolve_timestamp(timestamp),
                "content": str(content or "")
            }
            for key, value in (
                ("message_id", message_id),
                ("local_id", local_id),
                ("server_id", server_id),
                ("session_id", session_id),
            ):
                if value not in (None, ""):
                    item[key] = value
            item["is_bot"] = bool(is_bot)
            item["role"] = str(role or ("assistant" if is_bot else "user"))
            if is_bot or prefix_used or is_at_bot or is_mentioned:
                item["prefix_used"] = bool(prefix_used)
                item["is_at_bot"] = bool(is_at_bot)
                item["is_mentioned"] = bool(is_mentioned)
            if message_type not in (None, ""):
                item["message_type"] = str(message_type)
            if batch_index is not None:
                item["batch_index"] = int(batch_index)

            identity = next(
                (
                    (key, str(item.get(key)))
                    for key in ("message_id", "local_id", "server_id")
                    if item.get(key) not in (None, "", 0, "0")
                ),
                None,
            )
            if identity:
                # 去重只看尾部窗口：全量 O(n) 扫描叠加每条消息全量重写是 O(n²) 磁盘 IO
                for previous in messages[-200:]:
                    if isinstance(previous, dict) and str(previous.get(identity[0])) == identity[1]:
                        return False
            messages.append(item)

            # 磁盘记录兜底截断：strict 前缀模式下没有 cycle 整理，
            # 文件会无限增长导致每条消息重写数百 KB + fsync
            if len(messages) > 500:
                messages = messages[-500:]

            self._write_json(messages_path, messages)
            return True

    def get_group_messages(self, group_id):
        with _GROUP_FILE_LOCK:
            group_dir = self._ensure_group_dir(group_id)
            messages_path = os.path.join(group_dir, "group_messages.json")
            messages = self._load_json(messages_path, default=[])
        # 只截取最近 N 条作为上下文（磁盘文件本身也在写入侧截断到 500 条）
        cap = self.max_history_messages
        if cap > 0 and isinstance(messages, list) and len(messages) > cap:
            messages = messages[-cap:]
        return messages

    def get_group_messages_with_quoted_window(self, group_id, local_id=None,
                                              server_id=None, before=25, after=25,
                                              latest=None):
        """引用消息上下文：被引用消息前后窗口 ∪ 最新 latest 条。

        latest 缺省时用 max_history_messages。
        返回 (merged_messages, anchor_item)。merged 按磁盘存储的时间序排列、
        按 _message_key 去重；anchor 为命中的被引用消息（未命中为 None，
        此时 merged 退化为最新 N 条）。
        """
        with _GROUP_FILE_LOCK:
            group_dir = self._ensure_group_dir(group_id)
            messages_path = os.path.join(group_dir, "group_messages.json")
            messages = self._load_json(messages_path, default=[])
        if not isinstance(messages, list) or not messages:
            return [], None
        cap = self.max_history_messages if latest is None else max(0, int(latest))
        latest_msgs = messages[-cap:] if cap > 0 and len(messages) > cap else list(messages)

        anchor_index = None
        if local_id not in (None, "", 0, "0"):
            for index, item in enumerate(messages):
                if isinstance(item, dict) and str(item.get("local_id") or "") == str(local_id):
                    anchor_index = index
        if anchor_index is None and server_id not in (None, "", 0, "0"):
            for index, item in enumerate(messages):
                if isinstance(item, dict) and server_id_match(item.get("server_id"), server_id):
                    anchor_index = index
        if anchor_index is None:
            return latest_msgs, None

        window = messages[max(0, anchor_index - before): anchor_index + after + 1]
        wanted = {self._message_key(item) for item in window if isinstance(item, dict)}
        wanted.update(self._message_key(item) for item in latest_msgs if isinstance(item, dict))
        merged = [
            item for item in messages
            if isinstance(item, dict) and self._message_key(item) in wanted
        ]
        return merged, messages[anchor_index]

    def get_all_group_messages(self, group_id):
        """完整磁盘历史（不受 max_history_messages 截断），/about 统计用。"""
        with _GROUP_FILE_LOCK:
            group_dir = self._ensure_group_dir(group_id)
            messages_path = os.path.join(group_dir, "group_messages.json")
            messages = self._load_json(messages_path, default=[])
        return messages if isinstance(messages, list) else []

    def replace_group_messages(self, group_id, messages, preserve_unseen_from=None):
        """Replace the transcript without losing messages written meanwhile.

        Context compression takes time because it calls the LLM outside this
        lock. When a snapshot is supplied, retain current records that were
        not part of that snapshot and append them after the compacted result.
        """
        with _GROUP_FILE_LOCK:
            group_dir = self._ensure_group_dir(group_id)
            messages_path = os.path.join(group_dir, "group_messages.json")
            clean = [item for item in (messages or []) if isinstance(item, dict)]
            if preserve_unseen_from is not None:
                snapshot_keys = {
                    self._message_key(item)
                    for item in (preserve_unseen_from or [])
                    if isinstance(item, dict)
                }
                current = self._load_json(messages_path, default=[])
                clean.extend(
                    item for item in current
                    if isinstance(item, dict) and self._message_key(item) not in snapshot_keys
                )
            self._write_json(messages_path, clean)
            return clean

    def prune_group_messages_through(self, group_id, boundary):
        """Atomically keep only messages newer than a cycle boundary."""
        try:
            boundary = float(boundary)
        except (TypeError, ValueError):
            boundary = 0
        with _GROUP_FILE_LOCK:
            group_dir = self._ensure_group_dir(group_id)
            messages_path = os.path.join(group_dir, "group_messages.json")
            current = self._load_json(messages_path, default=[])
            preserved = [
                item for item in current
                if isinstance(item, dict) and self._message_timestamp(item) > boundary
            ]
            self._write_json(messages_path, preserved)
            return preserved

    def group_context_tokens(self, group_id) -> int:
        return self.estimate_tokens(self.get_group_messages(group_id))

    def _ensure_group_dir(self, group_id):
        # group_id 是微信群显示名，可含路径分隔符等危险字符：
        # 净化后再拼路径，防止在 data/groups 之外创建/读写目录
        safe_group_id = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", str(group_id or "unknown")).strip(". ")
        if not safe_group_id:
            safe_group_id = "unknown"
        group_dir = os.path.join(GROUPS_DIR, safe_group_id)
        os.makedirs(group_dir, exist_ok=True)
        return group_dir

    @staticmethod
    def _message_timestamp(item):
        try:
            return float(item.get("timestamp") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _resolve_timestamp(timestamp=None):
        """回填消息带原始时间戳；缺省（或非法/非正数）用当前时间。"""
        try:
            value = float(timestamp)
        except (TypeError, ValueError):
            value = 0.0
        return value if value > 0 else time.time()

    @classmethod
    def _message_key(cls, item):
        for key in ("message_id", "local_id", "server_id"):
            value = item.get(key)
            if value not in (None, "", 0, "0"):
                return ("id", key, str(value))
        return (
            "content",
            cls._message_timestamp(item),
            str(item.get("nickname") or ""),
            str(item.get("content") or ""),
            str(item.get("role") or ""),
        )

    def _load_json(self, path, default):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return list(default) if isinstance(default, list) else dict(default)

    def _write_json(self, path, data):
        directory = os.path.dirname(path)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=directory,
                prefix=".group_messages.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_path = file.name
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temp_path, path)
        except Exception:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            raise

    def _clear_expired_history(self, history):
        if not history:
            return []
        if self.history_expire_ms <= 0:
            return history

        now_ms = int(time.time() * 1000)
        latest_timestamp = max(int(item.get("timestamp", 0)) for item in history)
        latest_timestamp_ms = latest_timestamp * 1000

        if now_ms - latest_timestamp_ms > self.history_expire_ms:
            return []

        return history

    @staticmethod
    def _trim_by_chars(messages, limit):
        """Keep newest messages until the character budget is reached.

        The budget counts message ``content`` only. The newest complete
        message that crosses the boundary is retained, so forwarding never
        splits a message and the total is just over the configured limit.
        """
        if not isinstance(messages, list) or limit <= 0:
            return messages

        selected = []
        total_chars = 0
        for item in reversed(messages):
            if not isinstance(item, dict):
                continue
            content_chars = len(str(item.get("content") or ""))
            selected.append(item)
            total_chars += content_chars
            if total_chars >= limit:
                break

        selected.reverse()
        return selected

    @staticmethod
    def trim_messages_by_chars(messages, limit):
        return MemoryManager._trim_by_chars(messages, limit)
