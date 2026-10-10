import os
import importlib
import json
import re
import threading
import sys
import time

from core.auto_registry import register_raw_message_target
from core.llm_gate import PRIORITY_BACKGROUND, PRIORITY_USER, LlmGate
from core.sender import preview_delay_seconds, prepare_group_chat, register_send_listener, send
from core import bot_stats, chimein
from llm.core import LLMService

BASE_DIR = os.path.dirname(os.path.dirname(__file__))
sys.path.insert(0, BASE_DIR)

PLUGIN_DIR = "plugins"
AUTO_DIR = "auto"


class Dispatcher:

    def __init__(self):
        self.plugins = {}   # command -> module
        self.modules = {}   # all modules

        self.inited = False
        self.reload_lock = threading.Lock()

        self.debug = True
        self.config = {}
        self.llm_service = None
        self.llm_intercept_auto_plugins = set()
        self.llm_prefix_bypass_wxids = set()
        self.auto_modules = {}
        self.prefix_mode = "strict"
        # 有界并发闸门：保护 provider 与同步工具循环不被 worker/后台线程放大。
        # 用 LlmGate 而非 BoundedSemaphore——后者不保证顺序，且抢不到就丢请求，
        # 被 @ 的消息会永久没有回复。闸门改成排队等待 + 优先级。
        self._llm_slots = LlmGate(1)
        self._llm_direct_wait_seconds = 30.0
        # 抢不到并发槽/看门狗超时时的兜底话术。2026-09-27 起用户要求：
        # 这句话绝不发进群——抢不到槽/超时的用户请求一律静默返回 None。
        # 配置字段 busy_fallback_message 保留（空字符串＝静默），但已无发送分支。
        self._llm_busy_fallback = ""
        # 硬看门狗：单次 LLM 派发允许占槽的最长时间（秒）。
        # 工具循环超时只在两次 provider 调用之间检查，provider 调用本身卡死
        # （如 API 侧事故挂连接）会永久占槽、全 bot 的 LLM 功能瘫痪——
        # 2026-09-13 实测卡死 11+ 分钟。看门狗在 dispatcher 层强制释放槽位。
        self._llm_hard_timeout_seconds = 300.0

    # =========================================================
    # per-group feature switches
    # =========================================================
    def _group_feature_settings(self, group_id):
        """Return per-group switches while keeping router payloads unchanged."""
        group_key = str(group_id or "").strip()
        all_settings = self.config.get("group_features") or {}
        if not isinstance(all_settings, dict):
            all_settings = {}
        raw = all_settings.get(group_key)
        if not isinstance(raw, dict):
            raw = all_settings.get("default") if isinstance(all_settings.get("default"), dict) else {}

        def read(name, alias):
            return bool(raw.get(name, raw.get(alias, True)))

        return {
            "plugins_enabled": read("plugins_enabled", "plugins"),
            "auto_plugins_enabled": read("auto_plugins_enabled", "auto_plugins"),
            "llm_enabled": read("llm_enabled", "llm"),
        }

    def _group_feature_enabled(self, group_id, feature):
        return bool(self._group_feature_settings(group_id).get(feature, True))

    def _context_feature_enabled(self, context, feature):
        return self._group_feature_enabled((context or {}).get("group"), feature)

    # =========================================================
    # init plugins
    # =========================================================
    def init_plugins(self, config):
        if self.inited:
            return

        print("[PLUGIN] 初始化配置...")
        self.config = config or {}
        configured_prefix_mode = str(
            self.config.get("prefix_mode", "strict") or "strict"
        ).strip().lower()
        self.prefix_mode = "strict" if configured_prefix_mode == "only" else configured_prefix_mode
        if self.prefix_mode not in {"strict", "mixed"}:
            self.prefix_mode = "strict"
        target_groups = self._normalize_target_groups(config.get("target_group"))
        self._init_llm()

        for module in self.modules.values():
            set_batch_callback = getattr(module, "set_batch_callback", None)
            if callable(set_batch_callback):
                try:
                    set_batch_callback(self._dispatch_background_batch)
                except Exception as e:
                    print("[AUTO CALLBACK ERROR]", module.__name__, e)

        for module in self.modules.values():
            if hasattr(module, "init"):
                try:
                    module.init(config)
                except Exception as e:
                    print("[PLUGIN INIT ERROR]", module.__name__, e)

            if hasattr(module, "start"):
                try:
                    feature = (
                        "auto_plugins_enabled"
                        if module in self.auto_modules.values()
                        else "plugins_enabled"
                    )
                    module.start(
                        lambda content, target_groups=target_groups, feature=feature: self._broadcast_text(
                            target_groups,
                            content,
                            feature=feature,
                        )
                    )
                except Exception as e:
                    print("[PLUGIN START ERROR]", module.__name__, e)

        self.inited = True

    # =========================================================
    # load plugins
    # =========================================================
    def load_plugins(self):
        self._load_command_plugins()
        self._load_auto_plugins()

        print("\n[PLUGIN REGISTER TABLE]")
        for k in self.plugins:
            print("  -", k)

    # =========================================================
    # command plugins
    # =========================================================
    def _load_command_plugins(self):
        if not os.path.exists(PLUGIN_DIR):
            return

        for fname in os.listdir(PLUGIN_DIR):
            if fname.endswith(".py"):
                self._load_plugin(fname)

    def _load_plugin(self, fname):
        name = fname[:-3]
        path = f"{PLUGIN_DIR}.{name}"

        try:
            module = importlib.import_module(path)
            self.modules[name] = module

            if not hasattr(module, "handle"):
                return

            command = getattr(module, "COMMAND", None) or f"/{name}"
            self.plugins[command] = module

            # 注册别名
            aliases = getattr(module, "ALIASES", None)
            if isinstance(aliases, (list, tuple, set)):
                for alias in aliases:
                    alias_str = str(alias).strip()
                    if alias_str:
                        self.plugins[alias_str] = module

            print(f"[PLUGIN REGISTER] {name} -> {command}")

        except Exception as e:
            print(f"[PLUGIN ERROR] {fname}: {e}")

    # =========================================================
    # auto plugins（已改为 handle_auto 模式）
    # =========================================================
    def _load_auto_plugins(self):
        if not os.path.exists(AUTO_DIR):
            return

        for fname in os.listdir(AUTO_DIR):
            if fname.endswith(".py"):
                self._load_auto(fname)

    def _load_auto(self, fname):
        name = fname[:-3]
        path = f"{AUTO_DIR}.{name}"

        try:
            module = importlib.import_module(path)
            self.modules[name] = module
            self.auto_modules[name] = module

            if getattr(module, "MATCH_RAW_MESSAGE", False):
                register_raw_message_target(name)

            print(f"[AUTO REGISTER] {name}")

        except Exception as e:
            print(f"[AUTO LOAD ERROR] {fname}: {e}")

    # =========================================================
    # 🔥 核心 dispatch（融合 auto + command）
    # =========================================================
    def dispatch(self, command, args, context):

        if self.debug:
            print("\n[DISPATCH]")
            print("content =", context.get("content"))

        # =====================================================
        # 1. AUTO 阶段（统一 context 直通）
        # =====================================================
        auto_results = []
        auto_target = context.get("auto_target")
        auto_command_handled = False
        auto_plugins_enabled = self._context_feature_enabled(context, "auto_plugins_enabled")
        plugins_enabled = self._context_feature_enabled(context, "plugins_enabled")
        llm_enabled = self._context_feature_enabled(context, "llm_enabled")

        for name, module in self.modules.items():
            if not auto_plugins_enabled:
                break
            if getattr(module, "FALLBACK_ONLY", False):
                continue
            if auto_target:
                if isinstance(auto_target, (list, tuple, set)):
                    if name not in auto_target:
                        continue
                elif name != auto_target:
                    continue
            auto_commands = getattr(module, "AUTO_COMMANDS", ()) or ()
            if command and command in {str(item).strip() for item in auto_commands}:
                auto_command_handled = True
            if hasattr(module, "handle_auto"):
                try:
                    res = module.handle_auto(context)
                    if res is not None:
                        auto_results.append(res)
                except Exception as e:
                    print("[AUTO ERROR]", name, e)

        if self.debug:
            print("[AUTO RESULT]", auto_results)

        # =====================================================
        # 2. 无 command → 直接走 auto
        # =====================================================
        if not command:
            if auto_results:
                auto_result = auto_results[-1]
                if isinstance(auto_result, dict) and auto_result.get("forward_batch_to_llm"):
                    return self._dispatch_llm(
                        context,
                        batch_messages=auto_result.get("batch_messages") or [],
                        force_reply=bool(auto_result.get("force_reply")),
                        trigger_source=auto_result.get("trigger_source"),
                        cycle_id=auto_result.get("cycle_id"),
                        conversation_priority=auto_result.get("conversation_priority"),
                    )
                if isinstance(auto_result, dict) and auto_result.get("forward_to_llm"):
                    return self._dispatch_llm(context)
                return auto_result

            if context.get("proactive_candidate") and self.llm_service and llm_enabled:
                proactive_result = self.llm_service.handle_proactive_message(context)
                if isinstance(proactive_result, dict) and proactive_result.get("forward_batch_to_llm"):
                    return self._dispatch_llm(
                        context,
                        batch_messages=proactive_result.get("batch_messages") or [],
                        force_reply=bool(proactive_result.get("force_reply")),
                        trigger_source=proactive_result.get("trigger_source"),
                        attention_check=bool(proactive_result.get("attention_check")),
                        cycle_id=proactive_result.get("cycle_id"),
                        conversation_priority=proactive_result.get("conversation_priority"),
                    )

                # When non-prefix autonomous mode is enabled, the proactive
                # manager owns the message even while it is idle/attending or
                # waiting for the ten-second flush. Do not let prefix-bypass
                # rules turn the same message into an immediate one-message
                # LLM request and defeat batch context.
                if not context.get("prefix_used"):
                    return self._dispatch_fallback_auto(context)

            if llm_enabled and self._can_forward_to_llm(context):
                # 用户@触发 → 需要兜底反馈；随机接话(伪造前缀消息) → 保持静默
                raw_msg = context.get("raw") if isinstance(context.get("raw"), dict) else {}
                return self._dispatch_llm(
                    context,
                    user_facing=not bool(raw_msg.get("_random_chimein")),
                )

            return self._dispatch_fallback_auto(context) if auto_plugins_enabled else None

        # =====================================================
        # 2.5 /pro 专业模式：专业提示词 + pro 模型，直接转 LLM
        # =====================================================
        if command == "/pro":
            if not llm_enabled or not self.llm_service:
                return "LLM 未启用，无法使用 /pro"
            question = str(args or context.get("content") or "").strip()
            if not question:
                return "在 /pro 后面加上你的问题，例如：@bot /pro 解释一下傅里叶变换"
            context["pro_mode"] = True
            context["content"] = question
            print(f"[PRO MODE] 专业回复请求: {question[:60]}", flush=True)
            return self._dispatch_llm(context, user_facing=True)

        # =====================================================
        # 2.6 /btw 手动接话：和 2% 随机接话同一套提示词，绕过冷却；
        # 执行成功后 15 分钟冷却重新开始计时
        # =====================================================
        if command == "/btw":
            if not llm_enabled or not self.llm_service:
                return "LLM 未启用，无法使用 /btw"
            quoted = context.get("_quoted_context")
            if isinstance(quoted, dict) and str(quoted.get("content") or "").strip():
                context["content"] = (
                    "（对方引用了一条消息让你来插话。重点围绕被引用的内容说一两句，"
                    "就像普通群友冒泡一样自然，别解释你为什么说话，别复述这条提示）"
                )
            else:
                context["content"] = (
                    "（没人@你，是你自己想插话。优先围绕最末尾的几条新消息顺着话题自然地说一句，"
                    "不要翻已经聊完的旧话题；就像普通群友冒泡一样，别解释你为什么说话，别复述这条提示）"
                )
            context["user"] = "旁白"
            context["_random_chimein"] = True
            print("[BTW] 手动接话触发", flush=True)
            result = self._dispatch_llm(context, user_facing=True)
            if isinstance(result, dict) and (result.get("messages") or result.get("animation")):
                chimein.mark_fired(context.get("group"))
                bot_stats.record_chimein("btw", context.get("group"))
            return result

        # =====================================================
        # 2.7 /about 运行报告：不走 LLM，本地统计直接回复
        # =====================================================
        if command == "/about":
            context["_no_mention"] = True
            # 本地统计秒回太机械，加 4~8 秒拟人延迟再走 dict 协议发送
            import random
            return {
                "content": self._build_about_text(context),
                "delay_seconds": random.uniform(4, 8),
            }

        # =====================================================
        # 2.8 /summary 手动触发每日总结（定时任务见 core/daily_summary.py，
        # 每天 23:56 自动请求、00:00 准点发送）
        # =====================================================
        if command == "/summary":
            context["_no_mention"] = True
            from core import daily_summary
            return daily_summary.run_now(self, context.get("group"))

        # =====================================================
        # 3. command debug
        # =====================================================
        if self.debug:
            print("\n[COMMAND]")
            print(command, args)

        plugin = self.plugins.get(command)
        if plugin and not plugins_enabled:
            print(f"[PLUGIN BLOCK] group={context.get('group')} command={command} disabled by group_features")
            plugin = None

        # =====================================================
        # 4. 未找到 command → fallback auto
        # =====================================================
        if not plugin:
            # Auto command-like plugins may consume a command to create a
            # follow-up state. Do not turn that interception into an unknown
            # command reply.
            if auto_command_handled:
                return auto_results[-1] if auto_results else None
            if not plugins_enabled:
                return None
            return auto_results[-1] if auto_results else f"未知命令: {command}"

        # =====================================================
        # 5. 执行 command plugin
        # =====================================================
        try:
            result = plugin.handle(args, context)

            if result is None:
                return auto_results[-1] if auto_results else None

            return result

        except Exception as e:
            print("[PLUGIN ERROR]", command, e)
            return "插件执行异常"

    def _init_llm(self):
        try:
            llm_config = (self.config or {}).get("llm", {}) or {}
            intercept_list = llm_config.get("intercept_auto_plugins", []) or []
            if isinstance(intercept_list, str):
                intercept_list = [intercept_list]

            prefix_bypass_wxids = llm_config.get("prefix_bypass_wxids", []) or []
            if isinstance(prefix_bypass_wxids, str):
                prefix_bypass_wxids = [prefix_bypass_wxids]

            self.llm_intercept_auto_plugins = {
                str(item).strip()
                for item in intercept_list
                if str(item).strip()
            }
            self.llm_prefix_bypass_wxids = {
                str(item).strip()
                for item in prefix_bypass_wxids
                if str(item).strip()
            }

            if llm_config.get("enabled"):
                self.llm_service = LLMService()
                max_concurrent = max(
                    1,
                    min(int(self.llm_service.config.get("max_concurrent_requests", 1) or 1), 6),
                )
                self._llm_slots = LlmGate(max_concurrent)
                try:
                    self._llm_direct_wait_seconds = max(
                        0.0,
                        min(float(self.llm_service.config.get("direct_request_wait_seconds", 30) or 30), 120.0),
                    )
                except (TypeError, ValueError):
                    self._llm_direct_wait_seconds = 30.0
                # 字段保留：读进来只为配置兼容（语义：空＝静默）。
                # 发送分支已删除，非空也不会发进群，见 _dispatch_llm / _run_llm_with_watchdog。
                self._llm_busy_fallback = str(
                    self.llm_service.config.get("busy_fallback_message", "") or ""
                )
                try:
                    self._llm_hard_timeout_seconds = max(
                        60.0,
                        min(float(self.llm_service.config.get("dispatch_hard_timeout_seconds", 300) or 300), 900.0),
                    )
                except (TypeError, ValueError):
                    self._llm_hard_timeout_seconds = 300.0
                self.llm_service.set_proactive_callback(self._dispatch_background_batch)
                self.llm_service.proactive_reply.set_cycle_end_callback(self._curate_cycle)
                register_send_listener(
                    "llm-proactive-web-message-counter",
                    self._on_successful_bot_send,
                )
            else:
                self.llm_service = None
        except Exception as e:
            print("[LLM INIT ERROR]", e)
            self.llm_service = None
            self.llm_intercept_auto_plugins = set()
            self.llm_prefix_bypass_wxids = set()

    def _on_successful_bot_send(self, target=None, mode=None, content=None, file_path=None):
        """Count each successfully sent Bot text for proactive-web timing."""
        if mode != "wechat_text" or not self.llm_service:
            return
        group_id = str(target or "").strip()
        if not group_id:
            return
        self.llm_service.proactive_reply.record_sent_bot_message(group_id, 1)

    def _curate_cycle(self, group_id, context, messages):
        if not self.llm_service or not self._group_feature_enabled(group_id, "llm_enabled"):
            return False
        # Cycle curation also performs synchronous LLM/tool work. Keep it in
        # the same provider budget as foreground and proactive batches.
        # 后台整理：低优先级、不排队等待（抢不到就跳过，下次周期再来）
        if not self._llm_slots.acquire(PRIORITY_BACKGROUND, timeout=0.0):
            in_use, waiting = self._llm_slots.depth()
            print(
                f"[LLM QUEUE] 跳过 cycle curation group={group_id} "
                f"占用={in_use}/{self._llm_slots.slots} 排队={waiting}",
                flush=True,
            )
            return False
        try:
            return self.llm_service.curate_cycle(
                group_id,
                context=context,
                cycle_messages=messages,
            )
        finally:
            self._llm_slots.release()

    def _can_forward_to_llm(self, context):
        if not self.llm_service:
            return False

        wxid = str(context.get("wxid") or "").strip()
        bypass_prefix_required = wxid in self.llm_prefix_bypass_wxids

        if not context.get("prefix_used") and not bypass_prefix_required:
            return False

        content = str(context.get("content") or "").strip()
        if not content:
            return False

        if content.startswith("/"):
            return False

        configured = self.llm_intercept_auto_plugins
        if not configured:
            return True

        for name in configured:
            module = self.auto_modules.get(name)
            if not module:
                print(f"[LLM BLOCK] missing auto intercept module: {name}")
                return False

            if not getattr(module, "INTERCEPT_LLM", False):
                print(f"[LLM BLOCK] auto plugin not declared for llm intercept: {name}")
                return False

            allow_fn = getattr(module, "allow_llm", None)
            if not callable(allow_fn):
                print(f"[LLM BLOCK] auto plugin missing allow_llm(): {name}")
                return False

            try:
                allow = bool(allow_fn(context))
            except Exception as e:
                print(f"[LLM BLOCK] allow_llm error: {name} {e}")
                return False

            if not allow:
                print(f"[LLM BLOCK] auto plugin denied llm forward: {name}")
                return False

        return True

    def _build_about_text(self, context):
        """/about 运行报告：本地统计，不走 LLM。"""
        group_id = context.get("group") or ""
        now = time.time()

        first_ts = bot_stats.first_launch_ts()
        days = max(0.0, (now - first_ts) / 86400.0)
        deploy_str = time.strftime("%Y-%m-%d", time.localtime(first_ts))
        now_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))

        cutoff = now - 86400
        member_counts = {}
        bot_msgs_24h = 0
        mm = getattr(self.llm_service, "memory_manager", None) if self.llm_service else None
        messages = []
        if mm:
            try:
                messages = mm.get_all_group_messages(group_id) or []
            except Exception:
                messages = []
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
                bot_msgs_24h += 1
            elif nickname:
                member_counts[nickname] = member_counts.get(nickname, 0) + 1

        top_members = "暂无"
        if member_counts:
            ranked = sorted(member_counts.items(), key=lambda kv: kv[1], reverse=True)[:3]
            top_members = "、".join(f"{name}（{count} 条）" for name, count in ranked)

        chimein_total, chimein_btw = bot_stats.chimein_counts(24, group_id)
        reply_count = max(0, bot_msgs_24h - chimein_total)
        # 只报本群最近 24 小时的用量（用户要求），不再展示全局累计
        cost_lines, (total_in, total_cached, total_out), total_cost = bot_stats.group_token_usage(
            group_id, 24
        )

        lines = [
            "bot运行报告",
            f"已稳定运行 {days:.1f} 天（部署于 {deploy_str}）",
            self._forwarded_stats_line(group_id),
            f"当前时间：{now_str}",
            "—— 过去 24 小时 ——",
            f"最活跃群成员：{top_members}",
            f"接话：{chimein_total} 次（其中 /btw {chimein_btw} 次）",
            f"回复：{reply_count} 次",
            "—— 本群 Token 用量（近 24 小时） ——",
        ]
        # 非搬运目标群不显示搬运统计行（_forwarded_stats_line 返回 None）
        lines = [line for line in lines if line is not None]
        if cost_lines:
            for model, inp, cached, out, cost in cost_lines:
                lines.append(
                    f"{model}：输入 {inp:,}（缓存命中 {cached:,}）/ 输出 {out:,} ≈ ¥{cost:.4f}"
                )
        else:
            lines.append("本群近 24 小时暂无记录")
        lines.append(f"合计：输入 {total_in:,} / 输出 {total_out:,} ≈ ¥{total_cost:.4f}")
        lines.append("—— 技术栈 ——")
        lines.append(f"模型：{self._about_model_text()}")
        lines.append("基于开源项目：weflow-core（微信前端）")
        return "\n".join(lines)

    def _forwarded_stats_line(self, group_id):
        """搬运统计行：仅搬运目标群显示，且只统计本群过去 24 小时。

        目标群读 config/douyin_forwarder.json 的 target（与 forwarder 实际
        发送目标保持一致，不写死）。历史记录没有 group 字段，全部发生在
        配置目标群，按目标群计入。
        """
        targets = self._forward_target_groups()
        group = str(group_id or "").strip()
        if not targets or group not in targets:
            return None
        path = os.path.join(BASE_DIR, "data", "douyin_saved", "forwarded_history.jsonl")
        cutoff = time.time() - 86400
        count = 0
        seconds = 0.0
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    rec_group = str(rec.get("group") or "").strip() or (targets[0] if targets else "")
                    if rec_group != group:
                        continue
                    try:
                        ts = time.mktime(time.strptime(str(rec.get("time") or ""), "%Y-%m-%dT%H:%M:%S"))
                    except (TypeError, ValueError, OverflowError):
                        continue
                    if ts <= cutoff:
                        continue
                    count += int(rec.get("count") or 0)
                    seconds += float(rec.get("seconds") or 0)
        except OSError:
            pass
        minutes = seconds / 60.0
        return f"过去 24 小时已在本群搬运 {count} 个视频，总时长 {minutes:.1f} 分钟"

    def _forward_target_groups(self):
        """搬运目标群列表：config/douyin_forwarder.json 的 target，str 或 list。"""
        path = os.path.join(BASE_DIR, "config", "douyin_forwarder.json")
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f).get("target")
        except (OSError, ValueError):
            return []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple)):
            return []
        return [str(item).strip() for item in raw if str(item).strip()]

    def _about_model_text(self):
        """/about 里那行模型说明：按实际配置拼，别写死（写死会和配置不一致）。"""
        config = getattr(self, "config", {}) or {}
        llm_config = config.get("llm") or {}
        apis = llm_config.get("apis") or []
        main = ""
        for api in apis:
            if isinstance(api, dict) and str(api.get("name") or "") == "mimo":
                main = str(api.get("model") or "")
                break
        if not main:
            for api in apis:
                if isinstance(api, dict):
                    main = str(api.get("model") or "")
                    break
        text = main or "未配置"
        return text

    def _dispatch_llm(
        self,
        context,
        batch_messages=None,
        force_reply=False,
        trigger_source=None,
        attention_check=False,
        cycle_id=None,
        conversation_priority=None,
        user_facing=False,
    ):
        if not self._context_feature_enabled(context, "llm_enabled"):
            return None
        # 用户触发的请求（被 @ / 命令 / /pro / /btw）走高优先级并**排队等待**，
        # 不再因为抢不到槽就丢掉——那会让这条 @ 永久没有回复。
        # 后台任务（随机接话、注意力检查、批量）低优先级、不等待。
        priority = str(conversation_priority or context.get("_conversation_priority") or context.get("conversation_priority") or "MAY_REPLY").upper()
        direct = batch_messages is None or bool(force_reply) or priority in {"MUST_REPLY", "SHOULD_REPLY", "UNCERTAIN_DIRECT"}
        high_priority = bool(direct or user_facing)
        wait_seconds = self._llm_direct_wait_seconds if high_priority else 0.0
        wait_started = time.monotonic()
        acquired = self._llm_slots.acquire(
            PRIORITY_USER if high_priority else PRIORITY_BACKGROUND,
            timeout=wait_seconds,
        )
        if not acquired:
            in_use, waiting = self._llm_slots.depth()
            print(
                f"[LLM QUEUE] 排队超时放弃 batch={batch_messages is not None} "
                f"attention={attention_check} trigger={trigger_source} "
                f"占用={in_use}/{self._llm_slots.slots} 排队={waiting} 等待={wait_seconds:.0f}s",
                flush=True,
            )
            # 用户明确要求：这句话不发进群。抢不到槽就静默放弃（后台任务原本就静默），
            # 群里的表现是"这条 @ 暂时没回"，而不是看见一句系统兜底话术。
            return None
        waited = time.monotonic() - wait_started
        if waited > 1.0:
            in_use, waiting = self._llm_slots.depth()
            print(
                f"[LLM QUEUE] 排队 {waited:.1f}s 后拿到槽位（占用={in_use}/"
                f"{self._llm_slots.slots} 排队={waiting} trigger={trigger_source}）",
                flush=True,
            )
        # 预切群：LLM 生成期间让前端提前把微信窗口切到目标群，
        # 回复生成后发送命中缓存免搜索（节省 4~5s 切群时间）。
        # 仅预热：发送路径仍按缓存校验目标群，预热错了发送时会自动纠正。
        prepare_group_chat(context.get("group"))
        try:
            return self._run_llm_with_watchdog(
                context,
                user_facing=user_facing,
                batch_messages=batch_messages,
                force_reply=force_reply,
                trigger_source=trigger_source,
                attention_check=attention_check,
                cycle_id=cycle_id,
                conversation_priority=priority,
            )
        finally:
            self._llm_slots.release()

    def _run_llm_with_watchdog(self, context, user_facing=False, **kwargs):
        """在独立线程里执行 LLM 派发，超过硬超时仍不返回则强制放弃。

        工具循环超时只在两次 provider 调用之间检查；provider 调用本身卡死
        （API 侧事故挂住连接）会绕过所有内层超时永久占槽。看门狗到点后
        释放槽位让后续请求继续，卡死线程作为 daemon 线程遗弃（不发群、
        不写结果）。超时一律静默返回 None——用户要求不把兜底话术发进群。
        """
        box = {}

        def _target():
            try:
                box["result"] = self._dispatch_llm_unlimited(context, **kwargs)
            except Exception as exc:
                box["error"] = exc

        worker = threading.Thread(target=_target, daemon=True, name="llm-dispatch")
        worker.start()
        worker.join(self._llm_hard_timeout_seconds)
        if worker.is_alive():
            print(
                f"[LLM WATCHDOG] 请求超过 {self._llm_hard_timeout_seconds:.0f}s 未返回，"
                f"强制释放槽位 (group={context.get('group')} "
                f"trigger={kwargs.get('trigger_source')} batch={kwargs.get('batch_messages') is not None})",
                flush=True,
            )
            return None
        if "error" in box:
            raise box["error"]
        return box.get("result")

    def _dispatch_llm_unlimited(
        self,
        context,
        batch_messages=None,
        force_reply=False,
        trigger_source=None,
        attention_check=False,
        cycle_id=None,
        conversation_priority=None,
    ):
        try:
            if batch_messages is not None:
                result = self.llm_service.handle_batch_message(
                    group_id=context.get("group") or context.get("sessionId") or "unknown",
                    messages=batch_messages,
                    force_reply=force_reply,
                    trigger_source=trigger_source,
                    attention_check=attention_check,
                    session_id=context.get("sessionId"),
                    allow_memory_update=False,
                    cycle_id=cycle_id or context.get("_cycle_id"),
                    attention_source=context.get("_attention_source") or "idle",
                    nonsense_opportunity=bool(context.get("_nonsense_opportunity")),
                    slang_emotional_opportunity=bool(context.get("_slang_emotional_opportunity")),
                    tieba_opportunity=bool(context.get("_tieba_opportunity")),
                    proactive_web_opportunity=bool(context.get("_proactive_web_opportunity")),
                    idle_content_opportunity=bool(context.get("_idle_content_opportunity")),
                    idle_content_probability=context.get("_idle_content_probability", 0.0),
                    idle_content_reason=context.get("_idle_content_reason") or "",
                    conversation_priority=conversation_priority or context.get("_conversation_priority") or "MAY_REPLY",
                )
            else:
                result = self.llm_service.handle_message(
                    group_id=context.get("group") or context.get("sessionId") or "unknown",
                    nickname=context.get("user", "未知用户"),
                    content=context.get("content", ""),
                    wxid=context.get("wxid", ""),
                    session_id=context.get("sessionId"),
                    message_context=context,
                    force_reply=bool(context.get("prefix_used")),
                )
        except Exception as e:
            print("[LLM ERROR]", e)
            return None

        # 按群记一次 LLM 用量，供 /about 输出"本群最近 24 小时"（与全局累计分开）
        try:
            provider = getattr(self.llm_service, "provider", None)
            usage = dict(getattr(provider, "last_usage", None) or {})
            if usage:
                bot_stats.record_group_tokens(
                    context.get("group") or context.get("sessionId") or "",
                    model=getattr(provider, "last_model", "") or "",
                    input_tokens=usage.get("input"),
                    cached_input=usage.get("cached_input"),
                    output_tokens=usage.get("output"),
                )
        except Exception as e:
            print("[LLM GROUP TOKENS ERROR]", e)

        if not isinstance(result, dict):
            return None

        if batch_messages is not None:
            try:
                self.llm_service.on_proactive_result(
                    context,
                    result,
                    attention_check=attention_check,
                )
            except Exception as e:
                print("[LLM PROACTIVE RESULT ERROR]", e)
        else:
            try:
                self.llm_service.proactive_reply.record_conversation_reply(context, result)
            except Exception as e:
                print("[LLM CONVERSATION LEASE ERROR]", e)

        return {
            "target": context.get("group") or context.get("user"),
            "messages": self._sanitize_outgoing_messages(result.get("messages") or []),
            "animation": result.get("animation"),
            "should_reply": result.get("should_reply"),
            "reply_to": result.get("reply_to") or [],
            "mode": "wechat_text",
            "pro_mode": bool(context.get("pro_mode")),
            "delay_seconds": context.get("planned_send_delay_seconds")
        }

    def _sanitize_outgoing_messages(self, messages):
        """出站净化：内部错误文案不许当聊天消息发进群。

        llm_service / response_parser 在多处用 "LLM转发失败：…" 作为错误回复，
        这些是给日志看的，发进群里像故障播报。2026-09-27 起用户要求整条丢弃
        （不再替换成任何兜底话术）：返回的列表可能为空，上层据此不发消息。

        另外修掉模型偶发的重复 @：实测出现过 `@root @root 简单说就是…`
        （代码里不主动加 @，所以是模型自己写了两遍）。这里把**相邻重复的
        同一 @昵称**折叠成一个，其它内容原样保留。
        """
        if isinstance(messages, str):
            messages = [messages]
        cleaned = []
        for item in messages or []:
            text = str(item or "")
            if "LLM转发失败" in text or "LLM 功能未启用" in text:
                continue
            cleaned.append(self._collapse_duplicate_mentions(item))
        return cleaned

    # 连续的同一个 @昵称（允许中间只有一个空格）折叠成一个：
    #   "@root @root 简单说就是…" -> "@root 简单说就是…"
    #   "@A7 @A7@A7 你好"        -> "@A7 你好"
    _DUP_MENTION_RE = re.compile(r"(@[^\s@,，。:：!！?？]{1,20})(?:[\s]*(?:\1))+\s*")

    def _collapse_duplicate_mentions(self, item):
        if not isinstance(item, str):
            return item
        try:
            return self._DUP_MENTION_RE.sub(lambda m: m.group(1) + " ", item)
        except Exception:
            return item

    def _dispatch_background_batch(
        self,
        context,
        batch_messages,
        force_reply=False,
        trigger_source=None,
        attention_check=False,
    ):
        """Flush a timer-owned proactive batch without waiting for a new event."""
        context = dict(context or {})
        if not self._context_feature_enabled(context, "llm_enabled"):
            return
        context["planned_send_delay_seconds"] = preview_delay_seconds("wechat_text")
        result = self._dispatch_llm(
            context,
            batch_messages=batch_messages,
            force_reply=force_reply,
            trigger_source=trigger_source,
            attention_check=attention_check,
        )
        if not isinstance(result, dict):
            return

        target = result.get("target") or context.get("group") or context.get("user")
        messages = result.get("messages") or []
        delay_seconds = result.get("delay_seconds")
        first_send = True
        for item in messages:
            content = str(item or "").strip()
            if not content:
                continue
            send(
                target=target,
                content=content,
                mode="wechat_text",
                delay_seconds=delay_seconds if first_send else 0,
            )
            first_send = False

        animation = result.get("animation")
        if animation:
            try:
                from llm.security import get_emoji_path
                file_path = get_emoji_path(animation)
                if file_path:
                    send(
                        target=target,
                        file_path=file_path,
                        mode="wechat_file",
                        delay_seconds=delay_seconds if first_send else 0,
                    )
            except Exception as e:
                print("[AUTO ANIMATION ERROR]", e)

    def _dispatch_fallback_auto(self, context):
        if not self._context_feature_enabled(context, "auto_plugins_enabled"):
            return None
        for name, module in self.auto_modules.items():
            if not getattr(module, "FALLBACK_ONLY", False):
                continue

            try:
                result = module.handle_auto(context)
            except Exception as e:
                print("[AUTO FALLBACK ERROR]", name, e)
                continue

            if result is None:
                continue

            if isinstance(result, dict) and result.get("forward_to_llm"):
                return self._dispatch_llm(context)

            if isinstance(result, dict) and result.get("forward_batch_to_llm"):
                return self._dispatch_llm(
                    context,
                    batch_messages=result.get("batch_messages") or [],
                    force_reply=bool(result.get("force_reply")),
                    trigger_source=result.get("trigger_source"),
                )

            return result

        return None

    # =========================================================
    # debug
    # =========================================================
    def debug_state(self):
        print("\n===== DISPATCHER STATE =====")
        print("plugins:", list(self.plugins.keys()))
        print("modules:", list(self.modules.keys()))
        print("===========================\n")

    def _normalize_target_groups(self, target_group):
        if isinstance(target_group, str):
            group = target_group.strip()
            return [group] if group else []

        if isinstance(target_group, (list, tuple, set)):
            return [
                str(group).strip()
                for group in target_group
                if str(group).strip()
            ]

        return []

    def _broadcast_text(self, target_groups, content, feature="plugins_enabled"):
        result = False

        for target in target_groups:
            if not self._group_feature_enabled(target, feature):
                continue
            ok, _ = send(
                target=target,
                content=content,
                mode="wechat_text"
            )
            result = result or ok

        return result
