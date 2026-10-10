import requests
import json
import time
import re

from core import placeholder


class WeFlowClient:
    def __init__(self, token, queue, dedup, api_base=None):
        self.api_base = str(api_base or "http://127.0.0.1:5031").rstrip("/")
        self.api_token = str(token or "").strip()
        self.url = f"{self.api_base}/api/v1/push/messages?access_token={self.api_token}"
        self.queue = queue
        self.dedup = dedup
        # 群显示名解析缓存：session_id -> (display_name or None, expire_ts)
        # 推送事件偶发把 groupName 推成原始 chatroom ID（weflow 会话缓存未就绪），
        # 导致路由 target_group 精确匹配失败。用 /api/v1/sessions 兜底解析。
        self._group_name_cache = {}
        self._group_name_ttl = 300
        self._group_name_neg_ttl = 30

    def _masked_url(self):
        """日志用 URL：token 只显示前 4 位，避免完整 token 写入日志。"""
        token = self.api_token
        masked = (token[:4] + "***") if token else "<empty>"
        return f"{self.api_base}/api/v1/push/messages?access_token={masked}"

    def get_original_message(self, session_id, local_id=None, server_id=None,
                             timeout=8):
        """Fetch one raw WeFlow message for LLM context hydration."""
        session_id = str(session_id or "").strip()
        if not session_id:
            return {"ok": False, "error": "missing session_id"}

        params = {"talker": session_id}
        if local_id is not None:
            try:
                parsed_local_id = int(local_id)
            except (TypeError, ValueError):
                parsed_local_id = 0
            if parsed_local_id > 0:
                params["localId"] = parsed_local_id
        if not params.get("localId") and str(server_id or "").strip():
            params["serverId"] = str(server_id).strip()
        if len(params) == 1:
            return {"ok": False, "error": "missing local_id or server_id"}

        try:
            response = requests.get(
                f"{self.api_base}/api/v1/messages/original",
                params=params,
                headers={"Authorization": f"Bearer {self.api_token}"},
                timeout=timeout,
            )
            if response.status_code == 404:
                return {"ok": False, "error": "original message not found"}
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                return {"ok": False, "error": "invalid WeFlow response"}
            return {"ok": True, "message": data.get("message"), "lookup": data.get("lookup")}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:500]}

    # =========================
    # 🧠 提取发送者 wxid
    # 来源：
    # 1. content 第一行 wxid_xxx:
    # 2. messageKey 倒数第二段
    # =========================
    def get_wxid(self, msg: dict):
        # SSE 推送直接包含 senderWxid
        sender_wxid = msg.get("senderWxid", "")
        if sender_wxid and sender_wxid.strip():
            return sender_wxid.strip()

        # 兜底：从 content 第一行提取
        content = msg.get("content", "")
        if content:
            first_line = content.split("\n")[0].strip()
            m = re.match(r"^(wxid_[^:]+):?$", first_line)
            if m:
                return m.group(1)

        return None

    # =========================
    # 🧠 群显示名兜底解析（@chatroom ID -> 群名）
    # =========================
    def _resolve_group_name(self, session_id):
        """把 @chatroom 会话 ID 解析成群显示名（/api/v1/sessions，TTL 缓存）。
        解析成功返回显示名；失败返回 None（30 秒内不重复请求），调用方保留原始 ID。"""
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        now = time.time()
        cached = self._group_name_cache.get(session_id)
        if cached and cached[1] > now:
            return cached[0]
        try:
            resp = requests.get(
                f"{self.api_base}/api/v1/sessions",
                params={"access_token": self.api_token},
                timeout=5,
            )
            data = resp.json()
            sessions = data.get("sessions") if isinstance(data, dict) else None
            if isinstance(sessions, list):
                for s in sessions:
                    uname = str((s or {}).get("username") or "").strip()
                    dname = str((s or {}).get("displayName") or "").strip()
                    if uname and dname:
                        self._group_name_cache[uname] = (dname, now + self._group_name_ttl)
        except Exception as e:
            print(f"[GROUP NAME] 会话名解析请求失败: {e}")
        cached = self._group_name_cache.get(session_id)
        if cached:
            return cached[0]
        # 负缓存：解析不到时短时间内不再重复打 API
        self._group_name_cache[session_id] = (None, now + self._group_name_neg_ttl)
        return None

    # =========================
    # 🧠 提取纯文本内容（去掉首行 wxid）
    # =========================
    def get_real_content(self, msg: dict):
        content = msg.get("content", "")

        lines = content.split("\n")

        if len(lines) >= 2 and lines[0].startswith("wxid_"):
            return "\n".join(lines[1:]).strip()

        return content

    # =========================
    # 🧠 判断是否群消息
    # =========================
    def is_group(self, msg: dict):
        return msg.get("sessionType") == "group"

    # =========================
    # 🧠 判断是否私聊
    # =========================
    def is_private(self, msg: dict):
        return msg.get("sessionType") == "private"

    # =========================
    # 🧠 消息协议层（关键）
    # =========================
    def _normalize_msg(self, msg: dict) -> dict:
        normalized = {
            # ===== 标准字段（插件只用这些）=====
            "user": msg.get("sourceName") or msg.get("sender") or "未知用户",
            "content": msg.get("content", ""),
            "group": msg.get("groupName"),
            "sessionId": msg.get("sessionId"),
            "type": msg.get("sessionType"),

            # ===== 新增实用字段 =====
            "wxid": self.get_wxid(msg),                    # 发送者微信ID
            "text": self.get_real_content(msg),           # 清洗后的正文内容
            "is_group": self.is_group(msg),               # 是否群聊
            "is_private": self.is_private(msg),           # 是否私聊
            "is_at": bool(msg.get("is_at") or msg.get("isAt") or msg.get("atBot")),
            "is_mentioned": bool(
                msg.get("is_mentioned")
                or msg.get("isMentioned")
                or msg.get("isMention")
            ),

            # ===== 原始JSON字段直通 =====
            "event": msg.get("event"),
            "sourceName": msg.get("sourceName"),
            "groupName": msg.get("groupName"),
            "sessionType": msg.get("sessionType"),

            # ===== 扩展字段 =====
            "messageKey": msg.get("messageKey"),
            "localId": msg.get("localId") or msg.get("local_id"),
            "serverId": msg.get("serverId") or msg.get("server_id"),
            "serverIdRaw": msg.get("serverIdRaw"),
            "svrid": msg.get("svrid") or msg.get("rawid") or msg.get("serverId"),
            "rawid": msg.get("rawid"),
            "timestamp": msg.get("timestamp"),
            "localType": msg.get("localType") or msg.get("local_type"),
            "avatarUrl": msg.get("avatarUrl"),

            # ===== debug =====
            "_ts": time.time(),
            "_raw": msg
        }

        # 群名兜底：推送事件偶发把 groupName 推成原始 chatroom ID
        # （weflow 会话缓存未就绪），导致 target_group 精确匹配失败被静默丢弃。
        sid = str(normalized.get("sessionId") or "").strip()
        gname = str(normalized.get("group") or "").strip()
        if sid.endswith("@chatroom") and (not gname or gname.endswith("@chatroom")):
            resolved = self._resolve_group_name(sid)
            if resolved and resolved != gname:
                print(f"[GROUP NAME] {sid} -> {resolved}")
                normalized["group"] = resolved
                normalized["groupName"] = resolved

        return normalized

    def start(self):
        print("[SSE] 客户端启动")

        while True:
            try:
                print(f"[SSE] 连接: {self._masked_url()}")

                with requests.get(
                    self.url,
                    stream=True,
                    timeout=60,
                    headers={
                        "Accept": "text/event-stream",
                        "Cache-Control": "no-cache"
                    }
                ) as r:

                    if r.status_code != 200:
                        print("[SSE] 非200，5秒重连")
                        time.sleep(5)
                        continue

                    for line in r.iter_lines():
                        if not line:
                            continue

                        decoded = line.decode(errors="ignore").strip()

                        if not decoded.startswith("data:"):
                            continue

                        try:
                            msg = json.loads(decoded[5:].strip())
                        except:
                            continue

                        # 丢弃过旧消息：服务端对新连接会重放最近约10分钟的缓存，
                        # 重启后若不拦截，会把重启前的@重新入队再回复一遍
                        ts = msg.get("timestamp") or 0
                        if ts and ts > 10**12:
                            ts = ts / 1000
                        if ts and (time.time() - ts) > 660:
                            continue

                        # 丢弃撤回通知：撤回提示不是正常聊天消息，不应触发回复
                        if "撤回了一条消息" in str(msg.get("content") or ""):
                            continue

                        # 丢弃系统占位消息："当前版本不支持展示该内容，请升级至最新版本。"
                        # 是微信对版本不支持消息的系统提示，不是群友发言：进群历史会污染
                        # LLM 上下文，还会按普通消息触发随机接话（实测事故）。
                        # 在入口丢掉 → 不入队、不进历史、不进上下文。
                        if placeholder.is_placeholder(msg.get("content")):
                            if placeholder.log_ok():
                                print(
                                    f"[FILTER] 忽略占位消息 group="
                                    f"{msg.get('groupName') or msg.get('sessionId')} "
                                    f"user={msg.get('sourceName') or msg.get('senderWxid') or '未知用户'} "
                                    f"content={str(msg.get('content'))[:30]}",
                                    flush=True,
                                )
                            continue

                        # 使用 rawid 去重（SSE 推送事件不含 messageKey）
                        rawid = msg.get("rawid") or msg.get("messageKey")
                        if not rawid:
                            continue

                        if self.dedup.exists(rawid):
                            continue
                        self.dedup.add(rawid)

                        # =========================
                        # 🧠 统一结构
                        # =========================
                        msg = self._normalize_msg(msg)

                        print(
                            f"[QUEUE] 入队 | "
                            f"group={msg['group']} "
                            f"user={msg['user']} "
                            f"content={msg['content']}"
                        )

                        self.queue.put(msg)

            except Exception as e:
                print("[SSE ERROR]", e)
                time.sleep(5)
