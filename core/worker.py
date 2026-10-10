import random
import re
import threading
import sys
import os
import time
import queue as queue_lib

import requests

from core.sender import preview_delay_seconds, send
from core import bot_stats, chimein, placeholder
from core.message_split import normalize_max_message_chars, split_reply_for_wechat
from llm.memory import LongTermMemory, MemoryManager
from llm.memory.memory_manager import server_id_match
from llm.learning import StyleLearner
from llm.security import get_emoji_path

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)


class Worker:
    def __init__(self, queue, router, dispatcher, config=None):
        self.queue = queue
        self.router = router
        self.dispatcher = dispatcher
        self.config = config or {}
        # 规范化 prefix 为列表：配置成字符串时，下游 [0]/迭代会拿到单个字符
        prefix = self.config.get("prefix") or ["@bot"]
        self.prefixes = [prefix] if isinstance(prefix, str) else list(prefix)
        self._task_local = threading.local()
        self._random_reply_lock = threading.Lock()
        self._last_random_reply_ts = 0.0
        rr_cfg = ((self.config.get("llm") or {}).get("random_reply") or {})
        print(
            f"[RANDOM REPLY] enabled={bool(rr_cfg.get('enabled'))} "
            f"probability={rr_cfg.get('probability', 0.01)} "
            f"cooldown_minutes={rr_cfg.get('cooldown_minutes', 15)}",
            flush=True,
        )
        self.memory_manager = self._build_memory_manager()
        self.long_term_memory = self._build_long_term_memory()
        try:
            worker_llm_config = (self.config or {}).get("llm") if isinstance(self.config, dict) else {}
            worker_llm_config = dict(worker_llm_config or {})
            learning_config = dict(worker_llm_config.get("learning") or {})
            weflow_config = (self.config or {}).get("weflow") or {}
            learning_config["bot_wxids"] = list(learning_config.get("bot_wxids") or [])
            learning_config["prefixes"] = list(self.config.get("prefix") or []) if isinstance(self.config.get("prefix"), list) else [self.config.get("prefix")]
            if weflow_config.get("myWxid"):
                learning_config["bot_wxids"].append(weflow_config.get("myWxid"))
            worker_llm_config["learning"] = learning_config
            self.style_learner = StyleLearner(worker_llm_config)
        except Exception as exc:
            print(f"[WORKER LEARNING INIT ERROR] {exc}")
            self.style_learner = None

    # =========================================================
    # 🚀 主线程
    # =========================================================
    def run(self):
        """Keep the consumer alive when one malformed message/plugin dies."""
        restart_delay = 2.0
        while True:
            started_at = time.monotonic()
            try:
                self._run_loop()
                return
            except Exception as exc:
                if getattr(self._task_local, "active", False):
                    try:
                        self.queue.task_done()
                    except Exception:
                        pass
                    self._task_local.active = False
                print(f"[WORKER FATAL] consumer restarted: {type(exc).__name__}: {exc}", flush=True)
                # 退避防崩溃循环空转：正常运行超过60秒后重置，连续崩溃指数退避到30s封顶
                if time.monotonic() - started_at > 60:
                    restart_delay = 2.0
                time.sleep(restart_delay)
                restart_delay = min(restart_delay * 2, 30.0)

    def _run_loop(self):
        print("[WORKER] 线程启动")

        while True:
            try:
                msg = self.queue.get(timeout=1)
            except queue_lib.Empty:
                continue

            self._task_local.active = True

            print("[WORKER] 收到任务")
            self._listen_group_message(msg)
            msg = self._resolve_trailing_mention(msg)
            msg = self._resolve_quote_trigger(msg)
            msg = self._resolve_quoted_image(msg)
            msg = self._resolve_quoted_context(msg)

            parsed = self.router.parse(msg)

            if not parsed:
                fabricated = self._roll_random_chimein(msg)
                if fabricated is not None:
                    fabricated_parsed = self.router.parse(fabricated)
                    if fabricated_parsed:
                        print(
                            f"[RANDOM REPLY] 概率命中，主动插话 (触发消息: {msg.get('user')}: {str(msg.get('content'))[:30]})",
                            flush=True,
                        )
                        msg = fabricated
                        parsed = fabricated_parsed
                    else:
                        print("[RANDOM REPLY] 概率命中但被路由拦截(黑名单/限流/时间段)，本次跳过", flush=True)
                        # 抽中概率但没走通，冷却同样回滚（这次接话等于没发生）
                        self._rollback_chimein_cooldown(fabricated, {"sent": False, "reason": "被路由拦截"})

            if not parsed:
                self._finish_task()
                print("[WORKER] 未匹配命令")
                continue

            parsed_list = parsed if isinstance(parsed, list) else [parsed]

            for item in parsed_list:

                command = item.get("command")
                args = item.get("args")

                print(f"[WORKER] 命中命令: {command} {args}")

                # =========================
                # 📦 context
                # =========================
                raw_flags = msg.get("_raw") if isinstance(msg.get("_raw"), dict) else {}
                context = {
                    "user": item.get("user", "未知用户"),
                    "group": msg.get("group"),
                    "sessionId": msg.get("sessionId"),
                    "messageKey": msg.get("messageKey"),
                    "localId": msg.get("localId"),
                    "serverId": msg.get("serverId") or msg.get("svrid") or msg.get("rawid"),
                    "svrid": msg.get("svrid"),
                    "rawid": msg.get("rawid"),
                    "wxid": item.get("wxid") or msg.get("wxid"),
                    "is_bot": bool(
                        msg.get("is_bot")
                        or raw_flags.get("is_bot")
                        or (
                            (item.get("wxid") or msg.get("wxid"))
                            and (item.get("wxid") or msg.get("wxid"))
                            == ((self.config.get("weflow") or {}).get("myWxid"))
                        )
                    ),
                    "type": msg.get("type"),
                    "is_group": msg.get("is_group", msg.get("type") == "group"),
                    "is_private": msg.get("is_private", msg.get("type") == "private"),
                    "is_at": bool(
                        msg.get("is_at")
                        or msg.get("isAt")
                        or raw_flags.get("is_at")
                        or raw_flags.get("isAt")
                    ),
                    "is_mentioned": bool(
                        msg.get("is_mentioned")
                        or msg.get("isMentioned")
                        or raw_flags.get("is_mentioned")
                        or raw_flags.get("isMentioned")
                    ),
                    "is_picture": msg.get("is_picture", False),
                    "is_emoji": msg.get("is_emoji", False),
                    "is_voice": msg.get("is_voice", False),
                    "command": command,
                    "args": args,
                    "content": item.get("content"),
                    "raw": msg,
                    "auto_target": item.get("auto_target"),
                    "followup_payload": item.get("followup_payload") or {},
                    "prefix_used": bool(item.get("prefix_used")),
                    "proactive_candidate": bool(item.get("proactive_candidate")),
                    "_quoted_image_local_id": msg.get("_quoted_image_local_id"),
                    "_quoted_image_server_id": msg.get("_quoted_image_server_id"),
                    "_quoted_context": msg.get("_quoted_context"),
                    "planned_send_delay_seconds": preview_delay_seconds("wechat_text")
                }

                # =========================
                # ⚙️ 执行 dispatcher
                # =========================
                # deliver 记录本轮实际送出情况：随机接话被出站过滤/发送失败时要回滚冷却
                deliver = {
                    "chimein": bool(msg.get("_random_chimein")),
                    "sent": False,
                    "reason": None,
                }
                result = self.dispatcher.dispatch(command, args, context)

                print("[WORKER DEBUG] raw result =", result)

                if not result:
                    self._rollback_chimein_cooldown(msg, deliver)
                    self._finish_task()
                    print("[WORKER] 空返回，跳过")
                    continue

                # =========================================================
                # 🧠 1. 统一 dict 协议（推荐）
                # =========================================================
                if isinstance(result, dict):
                    target = result.get("target") or context.get("group")
                    mode = result.get("mode", "wechat_text")
                    delay_seconds = result.get("delay_seconds")
                    mention = self._mention_prefix(msg, context)

                    if isinstance(result.get("messages"), list):
                        self._send_structured_result(target, result, mention=mention, deliver=deliver)
                        self._rollback_chimein_cooldown(msg, deliver)
                        self._finish_task()
                        continue

                    content = result.get("content")

                    if content is None or content == "":
                        self._rollback_chimein_cooldown(msg, deliver)
                        self._finish_task()
                        print("[WORKER] content 为空，丢弃")
                        continue

                    # 语音/文件等模式的 content 不是纯文本，不能加@前缀
                    if mention and mode == "wechat_text":
                        content = mention + str(content)

                    print(f"[WORKER] 发送结果: target={target}, content={content}")

                    self._send_via_task(
                        deliver,
                        target=target,
                        content=content,
                        mode=mode,
                        delay_seconds=delay_seconds
                    )
                    self._rollback_chimein_cooldown(msg, deliver)
                    self._finish_task()
                    continue

                # =========================================================
                # 🧠 2. 兼容 string 返回（旧插件）
                # =========================================================
                if isinstance(result, str):

                    print(f"[WORKER] string result: {result}")

                    self._send_via_task(
                        deliver,
                        target=context.get("group") or context.get("user"),
                        content=self._mention_prefix(msg, context) + result,
                        mode="wechat_text",
                        delay_seconds=context.get("planned_send_delay_seconds")
                    )
                    self._rollback_chimein_cooldown(msg, deliver)
                    self._finish_task()
                    continue

                # =========================================================
                # 🧠 3. 其他类型直接丢弃
                # =========================================================
                print("[WORKER] unknown result type:", type(result))
                self._rollback_chimein_cooldown(msg, deliver)
                self._finish_task()

    def _send_via_task(self, deliver, **kwargs):
        """统一发送入口：随机接话额外要求"被出站过滤时不要兜底话术、什么都别发"，
        并把发送结果记进 deliver，供冷却回滚判定。返回 (ok, reason)。"""
        if deliver.get("chimein"):
            kwargs.setdefault("silent_on_filter", True)
        # send() 自身已兜住异常（失败返回 (False, msg)），异常逃逸行为与改动前一致
        ok, reason = send(**kwargs)
        if ok:
            deliver["sent"] = True
        elif deliver.get("chimein") and not deliver.get("reason"):
            deliver["reason"] = reason
        return ok, reason

    def _rollback_chimein_cooldown(self, msg, deliver):
        """随机接话最终没送达（被出站过滤拦下/发送失败）：把该群冷却恢复成接话前的值，
        相当于这次接话没发生过（间隔到了还能再试）。被@的正常回复不走这里。"""
        if not msg.get("_random_chimein") or deliver.get("sent"):
            return
        prev_ts = msg.get("_chimein_prev_ts")
        if prev_ts is None:
            return
        group = msg.get("_chimein_group")
        chimein.restore(group, prev_ts)
        print(
            f"[RANDOM REPLY] 接话未送达（{deliver.get('reason') or '未知原因'}），"
            f"已回滚群 {group!r} 的接话冷却",
            flush=True,
        )

    def _finish_task(self):
        if not getattr(self._task_local, "active", False):
            return
        try:
            self.queue.task_done()
        finally:
            self._task_local.active = False

    @staticmethod
    def _mention_prefix(msg, context):
        """回复开头带上触发者名字：@名字 + 空格。

        仅作标识用途（告诉群里这条回复是回谁的），不追求真正@提醒。
        随机插话等无明确触发者的回复不加。
        """
        if msg.get("_random_chimein") or context.get("_random_chimein"):
            return ""
        if context.get("_no_mention"):
            return ""
        user = str(context.get("user") or "").strip()
        if not user or user in ("旁白", "未知用户"):
            return ""
        return f"@{user} "

    def _build_memory_manager(self):
        try:
            if not isinstance(self.config.get("llm"), dict):
                return None
            return MemoryManager()
        except Exception:
            return None

    def _build_long_term_memory(self):
        try:
            llm_config = self.config.get("llm")
            if not isinstance(llm_config, dict):
                return None
            memory_config = dict(llm_config.get("memory") or {})
            bot_wxids = list(llm_config.get("bot_wxids") or [])
            my_wxid = (self.config.get("weflow") or {}).get("myWxid")
            if my_wxid:
                bot_wxids.append(my_wxid)
            return LongTermMemory({"memory": memory_config, "bot_wxids": bot_wxids})
        except Exception as exc:
            print(f"[WORKER MEMORY INIT ERROR] {exc}")
            return None

    # =========================================================
    # 🎲 随机插话：没人@时按概率主动回复，独立于@触发。
    # 命中后伪造一条带前缀的"内心独白"消息，走正常LLM回复链路
    # （延迟/分段粘贴/敏感词过滤全部继承）。
    # =========================================================
    def _roll_random_chimein(self, msg):
        try:
            cfg = ((self.config.get("llm") or {}).get("random_reply") or {})
            if not cfg.get("enabled"):
                return None
            if not self.router._is_target_group(msg.get("group")):
                return None
            content = str(msg.get("content") or "").strip()
            if not content or self.router._has_prefix(content):
                return None
            # 系统占位消息（"当前版本不支持展示该内容…"）不是群友发言，
            # 不进历史（入口已过滤），这里再兜一层：绝不因它抽概率接话
            if placeholder.is_placeholder(content):
                if placeholder.log_ok():
                    print(
                        f"[RANDOM REPLY] 占位消息不接话 (group={msg.get('group')} content={content[:30]})",
                        flush=True,
                    )
                return None
            my_wxid = str(((self.config.get("weflow") or {}).get("myWxid")) or "")
            sender = str(msg.get("wxid") or "")
            if msg.get("is_bot") or (my_wxid and sender == my_wxid):
                return None
            if self.router._is_blacklisted(sender):
                return None
            if self.router.is_bot_nickname_collision(msg.get("user")):
                return None
            try:
                probability = float(cfg.get("probability", 0.01))
            except (TypeError, ValueError):
                probability = 0.01
            probability = max(0.0, min(probability, 1.0))
            try:
                cooldown_seconds = float(cfg.get("cooldown_minutes", 15)) * 60
            except (TypeError, ValueError):
                cooldown_seconds = 900.0
            with self._random_reply_lock:
                if chimein.cooldown_active(cooldown_seconds, msg.get("group")):
                    return None
                if random.random() >= probability:
                    return None
                # 先记下接话前的冷却时间戳：回复真的发出去才算数，
                # 被出站敏感词过滤/发送失败时由 _rollback_chimein_cooldown 回滚
                prev_fired_ts = chimein.snapshot(msg.get("group"))
                chimein.mark_fired(msg.get("group"))
                bot_stats.record_chimein("random", msg.get("group"))
            prefix = self.prefixes[0]
            hint = (
                "（没人@你，是你自己想插话。优先围绕最末尾的几条新消息顺着话题自然地说一句，"
                "不要翻已经聊完的旧话题；就像普通群友冒泡一样，别解释你为什么说话，别复述这条提示）"
            )
            new_msg = dict(msg)
            new_msg["content"] = f"{prefix} {hint}"
            new_msg["user"] = "旁白"
            new_msg["_random_chimein"] = True
            new_msg["_chimein_group"] = msg.get("group")
            new_msg["_chimein_prev_ts"] = prev_fired_ts
            return new_msg
        except Exception as exc:
            print(f"[RANDOM REPLY ERROR] {exc}", flush=True)
            return None

    # =========================================================
    # 🖼 引用图片解析：引用了别人的图片并@bot时，
    # 反查被引用图片的 local_id 交给自动附图显式取图。
    # 被引用的图往往超出"最近10条"附图窗口，必须单独解析。
    # =========================================================
    def _resolve_quoted_image(self, msg):
        try:
            if not self.router._is_target_group(msg.get("group")):
                return msg
            content = str(msg.get("content") or "").strip()
            if not content or not self.router._has_prefix(content):
                return msg
            local_type = msg.get("localType")
            try:
                local_type = int(local_type)
            except (TypeError, ValueError):
                return msg
            if local_type not in (49, 244813135921):
                return msg

            message = self._fetch_original_message(msg)
            if not isinstance(message, dict):
                return msg

            quoted_content = str(message.get("quotedContent") or "").strip()
            raw = str(message.get("rawContent") or "")
            ref_type, svrid = self._parse_refermsg(raw)
            # weflow 的 quotedContent 对非文本消息经常误报为 [链接]，
            # 以 refermsg 里的真实类型为准（3=图片），quotedContent 仅作兜底
            if ref_type != "3" and "[图片]" not in quoted_content:
                return msg
            if not svrid:
                return msg
            msg["_quoted_image_server_id"] = svrid

            # 在50条群历史里按 server_id 反查被引用图片的 local_id；
            # 查不到（如消息在bot重启间隙丢失）则退回按 svrid 直接取图
            try:
                history = self.memory_manager.get_group_messages(msg.get("group")) or []
            except Exception:
                history = []
            for item in reversed(history):
                if not isinstance(item, dict):
                    continue
                if not server_id_match(item.get("server_id"), svrid):
                    continue
                if "[图片]" not in str(item.get("content") or ""):
                    continue
                if item.get("local_id"):
                    msg["_quoted_image_local_id"] = item.get("local_id")
                break
            print(
                f"[QUOTED IMAGE] 引用图片: svrid={svrid} "
                f"local_id={msg.get('_quoted_image_local_id') or '(历史未收录,按svrid取图)'}",
                flush=True,
            )
        except Exception as exc:
            print(f"[QUOTED IMAGE ERROR] {exc}", flush=True)
        return msg

    def _fetch_original_message(self, msg):
        """从 weflow 取消息原始详情（含引用信息），同一条消息缓存复用。"""
        cached = msg.get("_original_message")
        if isinstance(cached, dict):
            return cached
        session_id = str(msg.get("sessionId") or "").strip()
        local_id = msg.get("localId")
        if not session_id or not local_id:
            return None
        weflow_cfg = (self.config or {}).get("weflow") or {}
        api_base = f"http://{weflow_cfg.get('apiHost') or '127.0.0.1'}:{weflow_cfg.get('apiPort') or 5031}"
        token = str(self.config.get("token") or weflow_cfg.get("apiToken") or "").strip()
        resp = requests.get(
            f"{api_base}/api/v1/messages/original",
            params={"talker": session_id, "localId": int(local_id)},
            headers={"Authorization": f"Bearer {token}"},
            timeout=8,
        )
        data = resp.json()
        message = data.get("message") if isinstance(data, dict) else None
        if isinstance(message, dict):
            msg["_original_message"] = message
            return message
        return None

    # =========================================================
    # 🔗 引用上下文：被@的消息引用了别的消息时，解析被引用消息，
    # 供 LLM 构建「被引用消息前后窗口 ∪ 最新消息」的上下文
    # =========================================================
    def _resolve_quoted_context(self, msg):
        try:
            if not self.router._is_target_group(msg.get("group")):
                return msg
            content = str(msg.get("content") or "").strip()
            if not content or not self.router._has_prefix(content):
                return msg
            try:
                local_type = int(msg.get("localType"))
            except (TypeError, ValueError):
                return msg
            if local_type not in (49, 244813135921):
                return msg

            message = self._fetch_original_message(msg)
            if not isinstance(message, dict):
                return msg
            ref_type, svrid = self._parse_refermsg(str(message.get("rawContent") or ""))
            quoted_sender = str(message.get("quotedSender") or "").strip()
            quoted_content = str(message.get("quotedContent") or "").strip()
            if not svrid and not quoted_content:
                return msg
            msg["_quoted_context"] = {
                "server_id": svrid,
                "sender": quoted_sender,
                "content": quoted_content,
            }
            print(
                f"[QUOTED CONTEXT] 引用消息: svrid={svrid or '?'} "
                f"sender={quoted_sender or '?'} content={quoted_content[:40]}",
                flush=True,
            )
        except Exception as exc:
            print(f"[QUOTED CONTEXT ERROR] {exc}", flush=True)
        return msg

    @staticmethod
    def _parse_refermsg(raw):
        """从引用消息 rawContent 提取被引用消息的 (type, svrid)。"""
        block = re.search(r"<refermsg>([\s\S]*?)</refermsg>", raw or "")
        inner = block.group(1) if block else ""
        type_match = re.search(r"<type>(\d+)</type>", inner)
        svrid_match = re.search(r"<svrid>(\d+)</svrid>", inner)
        return (
            type_match.group(1) if type_match else "",
            svrid_match.group(1) if svrid_match else "",
        )

    # =========================================================
    # 💬 引用触发：引用了bot的消息时，当作@bot处理
    # =========================================================
    def _resolve_trailing_mention(self, msg):
        """@bot 不在开头时（如"这个怎么样 @bot"），挪为前缀触发。

        早期仅限引用消息（localType 49/244813135921）；2026-09-10 起按用户要求
        对所有消息生效——中间/末尾 @bot 也算召唤。被引号包裹的字面量不算。
        """
        try:
            if not self.router._is_target_group(msg.get("group")):
                return msg
            # bot 自己的消息不参与挪动，避免自触发
            my_wxid = str(((self.config.get("weflow") or {}).get("myWxid")) or "")
            if msg.get("is_bot") or (my_wxid and str(msg.get("wxid") or "") == my_wxid):
                return msg
            content = str(msg.get("content") or "").strip()
            if not content or self.router._has_prefix(content):
                return msg
            for prefix in self.prefixes:
                if not prefix:
                    continue
                # 被引号（英文单/双）包裹的 '@bot' 是字符串字面量，不算召唤
                literal_re = re.compile(
                    r"'" + re.escape(prefix) + r"'|\"" + re.escape(prefix) + r"\""
                )
                masked = literal_re.sub(lambda m: "\x00" * len(m.group(0)), content)
                idx = masked.find(prefix)
                if idx >= 0:
                    stripped = (content[:idx] + content[idx + len(prefix):]).strip(" ，,。")
                    msg["content"] = f"{prefix} {stripped}".strip()
                    print(f"[TRAILING @] 引用消息内@触发: {content[:40]}", flush=True)
                    break
        except Exception as exc:
            print(f"[TRAILING @ ERROR] {exc}", flush=True)
        return msg

    def _resolve_quote_trigger(self, msg):
        try:
            if not self.router._is_target_group(msg.get("group")):
                return msg
            content = str(msg.get("content") or "").strip()
            if not content or self.router._has_prefix(content):
                return msg
            # 引用消息：localType=49(appmsg) 或 244813135921(微信4.x引用类型)
            local_type = msg.get("localType")
            try:
                local_type = int(local_type)
            except (TypeError, ValueError):
                return msg
            if local_type not in (49, 244813135921):
                return msg

            message = self._fetch_original_message(msg)
            if not isinstance(message, dict):
                return msg

            quoted_sender = str(message.get("quotedSender") or "").strip()
            quoted_content = str(message.get("quotedContent") or "").strip()
            if not quoted_sender and not quoted_content:
                return msg

            if not self._is_quoting_bot(quoted_sender, quoted_content, msg):
                return msg

            prefix = self.prefixes[0]
            quoted_hint = f"（对方引用了我说的：{quoted_content[:60]}）" if quoted_content else ""
            msg["content"] = f"{prefix} {content}{quoted_hint}"
            msg["_quote_trigger"] = True
            print(f"[QUOTE TRIGGER] {msg.get('user')} 引用了bot的消息，按@处理 | 引用: {quoted_content[:40]}", flush=True)
        except Exception as exc:
            print(f"[QUOTE TRIGGER ERROR] {exc}", flush=True)
        return msg

    def _is_quoting_bot(self, quoted_sender, quoted_content, msg):
        bot_name = str(((self.config.get("llm") or {}).get("identity") or {}).get("name") or "bot").strip()
        if quoted_sender and quoted_sender == bot_name:
            return True
        my_wxid = str(((self.config or {}).get("weflow") or {}).get("myWxid") or "").strip()
        if quoted_sender and my_wxid and quoted_sender == my_wxid:
            return True
        # 昵称可能拿不到（被wxid过滤），退而匹配bot最近发过的内容
        if quoted_content and self.memory_manager:
            try:
                history = self.memory_manager.get_group_messages(msg.get("group")) or []
            except Exception:
                history = []
            for item in reversed(history[-50:]):
                if not isinstance(item, dict):
                    continue
                if str(item.get("nickname") or "") != "LLM":
                    continue
                bot_text = str(item.get("content") or "").strip()
                if bot_text and (bot_text == quoted_content or bot_text.startswith(quoted_content) or quoted_content.startswith(bot_text)):
                    return True
        return False

    def _listen_group_message(self, msg):
        if self.memory_manager is None and self.long_term_memory is None:
            return

        try:
            info = self.router.inspect_message(msg)
        except Exception:
            return

        if not info.get("is_target_group"):
            return

        content = str(info.get("content") or "").strip()
        if not content:
            return

        # [图片]必须进历史：自动附图功能需要它的message_id来取图。
        # 其余[表情]/[文件]等占位消息跳过，避免刷屏历史。
        if content.startswith("[") and content.endswith("]") and content != "[图片]":
            return

        # 监听历史不过滤@前缀：上下文需要完整群聊记录（最近50条），
        # 是否回复由后续路由的 prefix 判断决定，与入历史无关。

        group_id = info.get("group")
        nickname = info.get("user", "未知用户")
        if not group_id:
            return

        if self.memory_manager:
            self.memory_manager.add_group_message(
                group_id=group_id,
                nickname=nickname,
                content=content,
                message_id=info.get("message_id"),
                local_id=info.get("local_id"),
                server_id=info.get("server_id"),
                session_id=info.get("sessionId") or msg.get("sessionId"),
                prefix_used=bool(info.get("has_prefix")),
                is_at_bot=bool(info.get("is_at") or info.get("is_at_bot")),
                is_mentioned=bool(info.get("is_mentioned")),
                message_type=info.get("message_type") or msg.get("type"),
            )
        if self.long_term_memory:
            sender_wxid = info.get("wxid") or msg.get("wxid") or ""
            self.long_term_memory.record_message({
                "group": group_id,
                "sessionId": info.get("sessionId") or msg.get("sessionId"),
                "wxid": sender_wxid,
                "user": nickname,
                "content": content,
                "messageKey": info.get("message_id"),
                "serverId": info.get("server_id"),
                "prefix_used": bool(info.get("has_prefix")),
                "is_bot": bool(sender_wxid and sender_wxid == ((self.config.get("weflow") or {}).get("myWxid"))),
            })
        if self.style_learner:
            self.style_learner.record_message({
                "group": group_id,
                "sessionId": info.get("sessionId") or msg.get("sessionId"),
                "user": nickname,
                "wxid": info.get("wxid") or msg.get("wxid"),
                "content": content,
                "messageKey": info.get("message_id"),
                "localId": info.get("local_id"),
                "serverId": info.get("server_id"),
                "replyToMessageId": msg.get("replyToMessageId"),
                "is_picture": msg.get("is_picture", False),
                "is_emoji": msg.get("is_emoji", False),
                "is_voice": msg.get("is_voice", False),
                "raw": msg,
                "is_bot": bool(
                    (info.get("wxid") or msg.get("wxid"))
                    and (info.get("wxid") or msg.get("wxid")) == ((self.config.get("weflow") or {}).get("myWxid"))
                ),
            })

    def _pro_max_message_chars(self):
        """单条微信消息的字数上限（config.json: llm.pro.max_message_chars）。

        超过就按语义边界拆成多条发送；取不到用 1200（见 core/message_split.py
        里对默认值的说明），非法值回退默认、并夹在 200~4000 之间。
        """
        pro = ((self.config or {}).get("llm") or {}).get("pro") or {}
        if not isinstance(pro, dict):
            pro = {}
        return normalize_max_message_chars(pro.get("max_message_chars"))

    def _split_gap_seconds(self):
        """同一轮回复拆出的相邻两条之间的停顿（config.json: llm.pro.message_gap_seconds）。

        发送端本身已把消息串行化（core/sender.py 的 _SEND_LOCK）并且每条
        粘贴+Alt+S 后固定 sleep 1.5s，所以即使这里给 0 也不会两条同时发；
        这个额外停顿是留给微信客户端和风控的余地，默认 1.5s。
        """
        pro = ((self.config or {}).get("llm") or {}).get("pro") or {}
        if not isinstance(pro, dict):
            pro = {}
        try:
            gap = float(pro.get("message_gap_seconds", 1.5))
        except (TypeError, ValueError):
            gap = 1.5
        return max(0.0, min(gap, 30.0))

    def _send_structured_result(self, target, result, mention="", deliver=None):
        messages = result.get("messages") or []
        animation = result.get("animation")
        delay_seconds = result.get("delay_seconds")
        pro_mode = bool(result.get("pro_mode"))
        if deliver is None:
            deliver = {"chimein": False, "sent": False, "reason": None}
        first_send = True

        for item in messages:
            content = str(item or "").strip()
            if not content:
                continue

            if pro_mode:
                # 专业模式的回复通常很长：先把空两行及以上压缩为空一行，
                # 再按段落 → 句子 → 硬切的优先级拆成多条发送。
                # 以前这里整条不拆，长回复只能塞进一条微信消息，
                # 逼得提示词去限制字数，模型压缩省略甚至被输出上限截断。
                parts = split_reply_for_wechat(
                    re.sub(r"(\n\s*){3,}", "\n\n", content),
                    self._pro_max_message_chars(),
                )
            else:
                # 中间空一行的内容拆成多条发送，更像真人分段说话。
                # 再对每个超长段按"单条消息上限"细分：普通模式以前**没有长度上限**，
                # 一次回几百上千字会被微信在粘贴/发送时截断（实测问题）。
                # 这里复用 pro 的分段函数与同一个上限配置。
                parts = []
                for block in re.split(r"\n\s*\n", content):
                    block = block.strip()
                    if block:
                        parts.extend(
                            split_reply_for_wechat(block, self._pro_max_message_chars())
                        )

            # 同一轮回复拆出的相邻两条之间的停顿：pro 用可配置的分段间隔，
            # 普通模式保持原样（0，实际间隔由发送端内部的 sleep 提供）。
            gap_seconds = self._split_gap_seconds() if pro_mode else 0

            for part in parts:
                # @触发者只加在本轮回复的第一条文本上，拆分出的后续段不加。
                # 但模型自己也常在开头写 @昵称（它把"称呼对方"当成了任务的一部分），
                # 两处叠加就变成 "@root @root …"——实测 2026-09-27 03:20 那条。
                # 所以首条已经是同名 @ 时不再重复加。
                if first_send and mention:
                    head = str(part or "").lstrip()
                    name = mention.strip().lstrip("@")
                    already_mentioned = bool(
                        name
                        and head.startswith("@")
                        and re.split(r"[\s,，。:：;；!！?？~～]", head[1:], 1)[0] == name
                    )
                    if not already_mentioned:
                        part = mention + part
                self._send_via_task(
                    deliver,
                    target=target,
                    content=part,
                    mode="wechat_text",
                    delay_seconds=delay_seconds if first_send else gap_seconds
                )
                first_send = False

        if not animation:
            return

        file_path = get_emoji_path(animation)
        if not file_path:
            print(f"[WORKER] animation 未找到: {animation}")
            return

        self._send_via_task(
            deliver,
            target=target,
            file_path=file_path,
            mode="wechat_file",
            delay_seconds=delay_seconds if first_send else 0
        )

    # =========================================================
    # 🚀 启动线程池
    # =========================================================
    def start(self, n=4):
        print(f"[WORKER] 启动 {n} 个线程")

        for i in range(n):
            t = threading.Thread(target=self.run, daemon=True)
            t.start()
            print(f"[WORKER] 线程 {i} 已启动")
