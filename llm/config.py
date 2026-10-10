import json
import os


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_api_entries(llm_config, default_timeout):
    """Normalize the multi-LLM API pool list from ``llm.apis``.

    Each entry: {name, protocol, model, api_key, base_url, timeout_seconds}.
    Falls back to the legacy single ``model``/``api_key``/``api_base`` fields
    when ``apis`` is absent or empty.
    """
    entries = []
    raw_apis = llm_config.get("apis")
    if isinstance(raw_apis, list):
        for item in raw_apis:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip() or f"api{len(entries) + 1}"
            protocol = str(item.get("protocol") or "openai").strip().lower()
            if protocol not in {"openai", "responses", "anthropic"}:
                protocol = "openai"
            model = str(item.get("model") or "").strip()
            api_key = str(item.get("api_key") or "").strip()
            base_url = str(item.get("base_url") or item.get("api_base") or "").strip().rstrip("/")
            if not model or not api_key or not base_url:
                continue
            timeout = _safe_float(item.get("timeout_seconds", default_timeout), default_timeout)
            # 上限从 120 放宽到 300：长 /pro 回答单次就要 125~191 秒（带推理的模型）
            timeout = max(10.0, min(timeout, 300.0))
            max_tokens = _safe_int(item.get("max_tokens", 0), 0)
            # 显式调用优先级：越小越先调用；缺省按数组顺序（位置+1）。
            priority = _safe_int(item.get("priority", 0), 0)
            if priority <= 0:
                priority = len(entries) + 1
            entries.append({
                "name": name,
                "protocol": protocol,
                "model": model,
                "api_key": api_key,
                "base_url": base_url,
                "timeout_seconds": timeout,
                "max_tokens": max(1, max_tokens) if max_tokens > 0 else 0,
                "priority": priority,
                # /pro 等模式的模型名：provider 只在端点声明了 pro_model 时才
                # 采用 model_override，否则退回本端点默认模型（见
                # DeepSeekProvider._endpoint_model_override）。漏掉这个字段
                # 会让 /pro 静默地用普通模型，配置看起来生效其实没有。
                "pro_model": str(item.get("pro_model") or "").strip(),
                # Explicit opt-in for multimodal image input.
                "supports_images": bool(
                    item.get("supports_images", item.get("image_enabled", False))
                ),
                # 协议缓存开关：anthropic 走 cache_control；responses 走 prompt_cache_key。
                "cache": bool(item.get("cache", True)),
                "prompt_cache_key": str(item.get("prompt_cache_key") or "").strip(),
                # 缓存范围：full=完整前缀缓存；system=仅系统提示词可缓存；
                # none=不可缓存。缺省按协议在 provider 内决定。
                "cache_scope": str(item.get("cache_scope") or "").strip().lower(),
                # Anthropic cache_control 的 TTL：空=默认5分钟；"1h"=1小时。
                "cache_ttl": str(item.get("cache_ttl") or "").strip().lower(),
                # Anthropic cache_mode：auto=网关自动缓存（仅声明 TTL，请求顶层 auto_cached=true）；
                # manual=消息内注入 cache_control 断点；off=关闭缓存。
                "cache_mode": str(item.get("cache_mode") or "").strip().lower(),
            })
    if not entries:
        model = str(llm_config.get("model") or "").strip()
        api_key = str(llm_config.get("api_key") or "").strip()
        base_url = str(llm_config.get("api_base") or llm_config.get("base_url") or "").strip().rstrip("/")
        if model and api_key:
            timeout = _safe_float(llm_config.get("request_timeout_seconds", default_timeout), default_timeout)
            timeout = max(10.0, min(timeout, 120.0))
            entries.append({
                "name": "default",
                "protocol": "openai",
                "model": model,
                "api_key": api_key,
                "base_url": base_url or "https://api.deepseek.com",
                "timeout_seconds": timeout,
                "max_tokens": 0,
                "cache": True,
                "prompt_cache_key": "",
                "cache_scope": "full",
                "cache_mode": "auto",
                "cache_ttl": "",
                "priority": 1,
                "pro_model": "",
                "supports_images": bool(
                    llm_config.get("supports_images", llm_config.get("image_enabled", False))
                ),
            })
    return entries


def _normalize_identity(identity, fallback=None):
    fallback = fallback if isinstance(fallback, dict) else {}
    identity = identity if isinstance(identity, dict) else {}
    raw_rules = identity.get("rules", fallback.get("rules", []))
    if isinstance(raw_rules, str):
        raw_rules = [raw_rules]
    if not isinstance(raw_rules, list):
        raw_rules = []
    return {
        "name": str(identity.get("name", fallback.get("name", "LLM")) or "LLM").strip() or "LLM",
        "role": str(identity.get("role", fallback.get("role", "微信群聊助手")) or "微信群聊助手").strip() or "微信群聊助手",
        "style": str(identity.get("style", fallback.get("style", "自然、简短、像真人微信聊天")) or "自然、简短、像真人微信聊天").strip() or "自然、简短、像真人微信聊天",
        "rules": [str(item).strip() for item in raw_rules if str(item).strip()],
    }


def _normalize_auto_reply(settings, fallback=None, prefix_mode="strict"):
    merged = dict(fallback) if isinstance(fallback, dict) else {}
    if isinstance(settings, dict):
        merged.update(settings)
    if "enabled" not in merged:
        merged["enabled"] = prefix_mode == "mixed"
    merged["reply_trigger_mode"] = str(
        merged.get("reply_trigger_mode", "conversation_pulse") or "conversation_pulse"
    ).strip().lower()
    if merged["reply_trigger_mode"] not in {"frequency", "reply_necessity", "conversation_pulse"}:
        merged["reply_trigger_mode"] = "conversation_pulse"
    try:
        merged["reply_necessity_threshold"] = max(
            0, min(200, int(merged.get("reply_necessity_threshold", 35)))
        )
    except (TypeError, ValueError):
        merged["reply_necessity_threshold"] = 35
    return merged


def _normalize_group_profiles(llm_config, base_config, prefix_mode):
    raw_profiles = llm_config.get("group_profiles")
    if not isinstance(raw_profiles, dict):
        return {}

    profiles = {}
    for raw_name, raw_profile in raw_profiles.items():
        group_name = str(raw_name or "").strip()
        if not group_name or not isinstance(raw_profile, dict):
            continue
        profile = dict(raw_profile)
        profile["identity"] = _normalize_identity(
            raw_profile.get("identity"), base_config.get("identity")
        )
        for field in ("reply_style", "behavior_style", "group_prompt", "private_prompt"):
            profile[field] = str(
                raw_profile.get(field, base_config.get(field, "")) or ""
            ).strip()
        raw_prefixes = raw_profile.get("prefixes", base_config.get("prefixes") or [])
        if isinstance(raw_prefixes, str):
            raw_prefixes = [raw_prefixes]
        profile["prefixes"] = [
            str(item).strip()
            for item in (raw_prefixes if isinstance(raw_prefixes, list) else [])
            if str(item).strip()
        ]
        profile["prompt"] = dict(base_config.get("prompt") or {})
        if isinstance(raw_profile.get("prompt"), dict):
            profile["prompt"].update(raw_profile["prompt"])
        profile["auto_reply"] = _normalize_auto_reply(
            raw_profile.get("auto_reply"), base_config.get("auto_reply"), prefix_mode
        )
        profile["proactive"] = dict(base_config.get("proactive") or {})
        if isinstance(raw_profile.get("proactive"), dict):
            profile["proactive"].update(raw_profile["proactive"])
        profiles[group_name] = profile
    return profiles


def _normalize_time_slots(config):
    """Expose the top-level ``time_slots`` list to LLM modules (e.g. the pool's
    daily reset uses the start of the no-response window)."""
    slots = config.get("time_slots") or []
    if isinstance(slots, dict):
        slots = [slots]
    if not isinstance(slots, list):
        return []
    return [dict(item) for item in slots if isinstance(item, dict)]


def _load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get_llm_config():
    config = _load_config()
    llm_config = config.get("llm")

    if not isinstance(llm_config, dict):
        raise ValueError("Missing llm config")

    required_fields = [
        "enabled",
        "history_expire_ms",
    ]

    missing_fields = [field for field in required_fields if field not in llm_config]
    if missing_fields:
        raise ValueError(f"Missing llm config fields: {', '.join(missing_fields)}")

    prefix_mode = str(config.get("prefix_mode", "strict") or "strict").strip().lower()
    if prefix_mode == "only":
        prefix_mode = "strict"
    if prefix_mode not in {"strict", "mixed"}:
        prefix_mode = "strict"

    max_history_chars = _safe_int(
        llm_config.get("max_history_chars", llm_config.get("max_history", 5000)),
        5000,
    )
    group_message_limit_chars = _safe_int(
        llm_config.get(
            "group_message_limit_chars",
            llm_config.get("group_message_limit", 2000),
        ),
        2000,
    )

    result = {
        "enabled": llm_config["enabled"],
        # ``provider`` used to select a single built-in client. API selection is
        # now entirely driven by ``apis[*].protocol`` and priority.
        "provider": str(llm_config.get("provider") or "multi_api_pool").strip(),
        "model": str(llm_config.get("model") or "").strip(),
        "prefix_mode": prefix_mode,
        "max_history_chars": max(0, max_history_chars),
        "max_history_messages": max(
            0, _safe_int(llm_config.get("max_history_messages", 0), 0)
        ),
        "group_message_limit_chars": max(0, group_message_limit_chars),
        "context_window_tokens": max(
            10000, _safe_int(llm_config.get("context_window_tokens", 200000), 200000)
        ),
        "context_compression_target_tokens": max(
            5000, _safe_int(llm_config.get("context_compression_target_tokens", 160000), 160000)
        ),
        # 聊天上下文缓存前缀预算（token）：把历史拆成「稳定前缀(缓存断点) + 动态尾部」。
        "cache_prefix_tokens": max(
            0, _safe_int(llm_config.get("cache_prefix_tokens", 24000), 24000)
        ),
        # Backward-compatible aliases for modules that still read the old names.
        "max_history": max(0, max_history_chars),
        "history_expire_ms": llm_config["history_expire_ms"],
        "group_message_limit": max(0, group_message_limit_chars),
        "web_fetch_enabled": bool(llm_config.get("web_fetch_enabled", True)),
        "web_fetch_max_calls": _safe_int(llm_config.get("web_fetch_max_calls", 3), 3),
        "web_fetch_timeout_seconds": _safe_float(
            llm_config.get("web_fetch_timeout_seconds", 15), 15.0
        ),
        "web_fetch_max_chars": _safe_int(llm_config.get("web_fetch_max_chars", 24000), 24000),
        "original_message_enabled": bool(llm_config.get("original_message_enabled", True)),
        "original_message_max_calls": max(1, min(_safe_int(llm_config.get("original_message_max_calls", 2), 2), 4)),
        "original_message_timeout_seconds": _safe_float(
            llm_config.get("original_message_timeout_seconds", 8), 8.0
        ),
        "original_message_max_chars": _safe_int(
            llm_config.get("original_message_max_chars", 16000), 16000
        ),
        "image_tool_enabled": bool(llm_config.get("image_tool_enabled", True)),
        "image_tool_max_calls": max(
            1, min(_safe_int(llm_config.get("image_tool_max_calls", 1), 1), 3)
        ),
        "image_tool_max_bytes": max(
            256 * 1024,
            min(
                _safe_int(
                    llm_config.get("image_tool_max_bytes", 8 * 1024 * 1024),
                    8 * 1024 * 1024,
                ),
                16 * 1024 * 1024,
            ),
        ),
        # 自动附图（_auto_attach_recent_images）：这两个键原先没进白名单，
        # config.json 里改了也不生效，一直走硬编码默认值
        "image_attach_recent_window": max(
            1, min(_safe_int(llm_config.get("image_attach_recent_window", 10), 10), 50)
        ),
        "image_attach_max": max(
            1, min(_safe_int(llm_config.get("image_attach_max", 4), 4), 8)
        ),
        # 单张图取图超时：媒体导出可能较慢，10s 太紧会把引用图取丢
        "image_fetch_timeout_seconds": max(
            5.0, min(_safe_float(llm_config.get("image_fetch_timeout_seconds", 20), 20.0), 120.0)
        ),
        # 只取到微信缩略图时等一会儿重试（微信全图常晚几秒落盘）
        "image_thumbnail_retry_delay_seconds": max(
            0.0,
            min(_safe_float(llm_config.get("image_thumbnail_retry_delay_seconds", 4), 4.0), 30.0),
        ),
        "image_thumbnail_retry_max": max(
            0, min(_safe_int(llm_config.get("image_thumbnail_retry_max", 2), 2), 4)
        ),
        # 调试：把实际发给模型的图片落盘到 logs/image_debug/
        "image_debug_dump": bool(llm_config.get("image_debug_dump", False)),
        "image_debug_keep": max(
            10, min(_safe_int(llm_config.get("image_debug_keep", 100), 100), 2000)
        ),
        # Keep provider work bounded across the Worker pool and proactive
        # timer threads. The dispatcher applies the actual semaphore.
        "max_concurrent_requests": max(
            1, min(_safe_int(llm_config.get("max_concurrent_requests", 1), 1), 6)
        ),
        "direct_request_wait_seconds": max(
            0.0, min(_safe_float(llm_config.get("direct_request_wait_seconds", 30), 30.0), 120.0)
        ),
        # 抢不到并发槽/看门狗超时时的兜底话术。2026-09-27 起用户要求静默：
        # 默认空字符串＝什么都不发（dispatcher 已不再有发送分支）。
        # 字段保留仅为配置兼容，将来若要恢复提示语才需要同时改 dispatcher。
        "busy_fallback_message": str(
            llm_config.get("busy_fallback_message") or ""
        ),
        # 工具循环总预算 / 单次 HTTP 超时 / 单次派发看门狗，三者必须从小到大：
        #   API 超时(240) < 工具循环(300) < 看门狗(420)
        # 放宽的原因：mimo-v2.6-pro 是带隐藏推理的模型（实测 6938 输出 token 里
        # 4240 是推理 token），一次长回答（1500 字以上）实测耗时 125~191 秒，
        # 旧的 45s/120s 会让长 /pro 回答直接超时、一条都发不出去。
        "tool_loop_timeout_seconds": max(
            10.0, min(_safe_float(llm_config.get("tool_loop_timeout_seconds", 300), 300.0), 300.0)
        ),
        "request_timeout_seconds": max(
            10.0, min(_safe_float(llm_config.get("request_timeout_seconds", 240), 240.0), 300.0)
        ),
        # 单次 LLM 派发的硬看门狗（秒）：覆盖 provider 调用+工具循环整体，
        # 超时后 dispatcher 强制释放并发槽，防止单个卡死请求瘫痪全部 LLM 功能。
        "dispatch_hard_timeout_seconds": max(
            60.0, min(_safe_float(llm_config.get("dispatch_hard_timeout_seconds", 420), 420.0), 900.0)
        ),
        # 缓存成本模型：命中:未命中价格比（如 DeepSeek 约 1:5.428）。
        "cache_cost_ratio": max(1.0, _safe_float(llm_config.get("cache_cost_ratio", 5.428), 5.428)),
        # 缓存命中率估计（用于压缩成本决策）。
        "cache_hit_rate": min(0.99, max(0.0, _safe_float(llm_config.get("cache_hit_rate", 0.85), 0.85))),
        # 上下文压缩回收成本的最长请求数（回本周期）。
        "cache_break_even_horizon": max(1, _safe_int(llm_config.get("cache_break_even_horizon", 40), 40)),
    }


    # ---- 多 LLM API 池 ----
    request_timeout = result["request_timeout_seconds"]
    result["apis"] = _normalize_api_entries(llm_config, request_timeout)
    if result["apis"]:
        # 向后兼容：主入口指向优先级最高的 API（priority 越小越先）。
        _primary = sorted(result["apis"], key=lambda e: int(e.get("priority") or 0))[0]
        result["api_key"] = _primary["api_key"]
        result["api_base"] = _primary["base_url"]
        result["model"] = _primary["model"]
    else:
        if result["enabled"]:
            raise ValueError(
                "Missing a valid LLM API: configure llm.apis or the legacy "
                "llm.model/llm.api_key/llm.api_base fields"
            )
        result["api_key"] = ""
        result["api_base"] = ""
    # 每日重置时刻依赖顶部 time_slots（bot 不响应时间段起点）。
    result["time_slots"] = _normalize_time_slots(config)

    learning = llm_config.get("learning")
    if not isinstance(learning, dict):
        learning = {}
    result["learning"] = {
        "enabled": bool(learning.get("enabled", True)),
        "db_path": str(learning.get("db_path") or "data/llm_learning.sqlite3").strip(),
        "queue_max": max(100, _safe_int(learning.get("queue_max", 2000), 2000)),
        "min_term_count": max(2, _safe_int(learning.get("min_term_count", 2), 2)),
        "prompt_max_chars": max(400, _safe_int(learning.get("prompt_max_chars", 1800), 1800)),
        "style_card_max_chars": max(600, _safe_int(learning.get("style_card_max_chars", 1800), 1800)),
        "expression_recall_scan_limit": max(200, _safe_int(learning.get("expression_recall_scan_limit", 2000), 2000)),
        "expression_pool_size": max(4, min(_safe_int(learning.get("expression_pool_size", 12), 12), 24)),
        "expression_selector_enabled": bool(learning.get("expression_selector_enabled", True)),
        "expression_selector_max_items": max(1, min(_safe_int(learning.get("expression_selector_max_items", 4), 4), 8)),
        "expression_selector_max_chars": max(400, min(_safe_int(learning.get("expression_selector_max_chars", 1400), 1400), 4000)),
        "expression_eval_enabled": bool(learning.get("expression_eval_enabled", False)),
        "expression_eval_max_items": max(1, min(_safe_int(learning.get("expression_eval_max_items", 6), 6), 12)),
        "slang_emotional_pool_rotation": bool(learning.get("slang_emotional_pool_rotation", True)),
        "style_switch_enabled": bool(learning.get("style_switch_enabled", True)),
        "style_switch_cooldown_seconds": max(0, _safe_int(learning.get("style_switch_cooldown_seconds", 120), 120)),
        "bot_names": [
            str(item).strip()
            for item in (([learning.get("bot_names")] if isinstance(learning.get("bot_names"), str) else learning.get("bot_names")) or [])
            if str(item).strip()
        ],
    }

    memory = llm_config.get("memory")
    if not isinstance(memory, dict):
        memory = {}
    result["memory"] = {
        "enabled": bool(memory.get("enabled", True)),
        "db_path": str(memory.get("db_path") or "data/memory.sqlite3").strip(),
        "context_max_chars": max(0, _safe_int(memory.get("context_max_chars", 0), 0)),
        "person_fact_limit": max(1, _safe_int(memory.get("person_fact_limit", 8), 8)),
        "group_knowledge_limit": max(1, _safe_int(memory.get("group_knowledge_limit", 10), 10)),
        "candidate_batch_size": max(10, _safe_int(memory.get("candidate_batch_size", 30), 30)),
        "short_memory_max_tokens": max(50, _safe_int(memory.get("short_memory_max_tokens", 1000), 1000)),
    }

    weflow_config = config.get("weflow")
    if not isinstance(weflow_config, dict):
        weflow_config = {}
    api_base = str(weflow_config.get("apiBase") or "").strip()
    if not api_base:
        api_host = str(weflow_config.get("apiHost") or "127.0.0.1").strip() or "127.0.0.1"
        api_port = _safe_int(weflow_config.get("apiPort", 5031), 5031)
        api_base = f"http://{api_host}:{api_port}"
    result["weflow_api_base"] = api_base.rstrip("/")
    result["weflow_api_token"] = str(
        config.get("token") or weflow_config.get("apiToken") or ""
    ).strip()
    bot_wxids = llm_config.get("bot_wxids") or []
    if isinstance(bot_wxids, str):
        bot_wxids = [bot_wxids]
    if weflow_config.get("myWxid"):
        bot_wxids = list(bot_wxids) + [weflow_config.get("myWxid")]
    result["bot_wxids"] = sorted({str(item).strip() for item in bot_wxids if str(item).strip()})

    target_groups = config.get("target_group", [])
    if isinstance(target_groups, str):
        target_groups = [target_groups]
    result["target_groups"] = [
        str(item).strip()
        for item in (target_groups if isinstance(target_groups, list) else [])
        if str(item).strip()
    ]

    prefixes = config.get("prefix", [])
    if isinstance(prefixes, str):
        prefixes = [prefixes]
    result["prefixes"] = [
        str(item).strip()
        for item in (prefixes if isinstance(prefixes, list) else [])
        if str(item).strip()
    ]

    result["auto_reply"] = _normalize_auto_reply(
        llm_config.get("auto_reply"), prefix_mode=prefix_mode
    )

    intercept_auto_plugins = llm_config.get("intercept_auto_plugins", [])
    if isinstance(intercept_auto_plugins, str):
        intercept_auto_plugins = [intercept_auto_plugins]
    if not isinstance(intercept_auto_plugins, list):
        intercept_auto_plugins = []
    result["intercept_auto_plugins"] = [
        str(item).strip()
        for item in intercept_auto_plugins
        if str(item).strip()
    ]

    prefix_bypass_wxids = llm_config.get("prefix_bypass_wxids", [])
    if isinstance(prefix_bypass_wxids, str):
        prefix_bypass_wxids = [prefix_bypass_wxids]
    if not isinstance(prefix_bypass_wxids, list):
        prefix_bypass_wxids = []
    result["prefix_bypass_wxids"] = [
        str(item).strip()
        for item in prefix_bypass_wxids
        if str(item).strip()
    ]

    admin_wxids = llm_config.get("admin_wxids", [])
    if isinstance(admin_wxids, str):
        admin_wxids = [admin_wxids]
    if not isinstance(admin_wxids, list):
        admin_wxids = []
    result["admin_wxids"] = [
        str(item).strip()
        for item in admin_wxids
        if str(item).strip()
    ]

    emoji_dir = str(
        llm_config.get("emoji_dir")
        or ""
    ).strip()
    result["emoji_dir"] = emoji_dir

    assistant_nickname = str(llm_config.get("assistant_nickname") or "LLM").strip()
    result["assistant_nickname"] = assistant_nickname or "LLM"

    result["identity"] = _normalize_identity(llm_config.get("identity"))

    prompt = llm_config.get("prompt")
    if not isinstance(prompt, dict):
        prompt = {}

    result["prompt"] = {
        "max_messages": int(prompt.get("max_messages", 3) or 3),
        "max_emoji_items": int(prompt.get("max_emoji_items", 50) or 50),
        "allow_animation": bool(prompt.get("allow_animation", True)),
        "prefer_short_reply": bool(prompt.get("prefer_short_reply", True)),
        "forbid_markdown": bool(prompt.get("forbid_markdown", True)),
        "forbid_explanation": bool(prompt.get("forbid_explanation", True)),
        "emoji_hint_text": str(prompt.get("emoji_hint_text") or "喏").strip() or "喏",
        "fallback_message": str(prompt.get("fallback_message") or "我在").strip() or "我在",
        "special_rules": [
            str(item).strip()
            for item in (prompt.get("special_rules") or [])
            if str(item).strip()
        ],
        "topic_redirect_rules": [
            str(item).strip()
            for item in (prompt.get("topic_redirect_rules") or [])
            if str(item).strip()
        ]
    }
    result["reply_style"] = str(llm_config.get("reply_style") or "").strip()
    result["behavior_style"] = str(llm_config.get("behavior_style") or "").strip()
    result["group_prompt"] = str(llm_config.get("group_prompt") or "").strip()
    result["private_prompt"] = str(llm_config.get("private_prompt") or "").strip()
    proactive = llm_config.get("proactive")
    result["proactive"] = dict(proactive) if isinstance(proactive, dict) else {}
    # /pro 专业模式。以前这里没归一化，result 里根本没有 "pro" 键，
    # llm_service 的 _pro_config() 永远拿到空 dict：pro.model / pro.api /
    # pro.max_reply_chars / pro.system_prompt 改了也不生效（静默走硬编码默认值）。
    # 注意：pro.max_message_chars / pro.message_gap_seconds 是发送端（分段发送）
    # 的参数，由 core/worker.py 直接读原始 config.json，不在这里做归一化。
    pro = llm_config.get("pro")
    if not isinstance(pro, dict):
        pro = {}
    result["pro"] = {
        # 模型名只是"是否启用 pro 覆盖"的开关兼展示，真实模型取端点的 pro_model
        # （DeepSeekProvider._endpoint_model_override）。
        "model": str(pro.get("model") or "").strip(),
        "api": str(pro.get("api") or "").strip(),
        # 2026-09-27：不再是"必须塞进一条微信消息"的硬限制（发送端会分段），
        # 只是提示词里的极端兜底上限，上限从 8000 放宽到 16000。
        "max_reply_chars": max(
            100, min(_safe_int(pro.get("max_reply_chars", 12000), 12000), 16000)
        ),
        "system_prompt": str(pro.get("system_prompt") or ""),
    }
    result["group_profiles"] = _normalize_group_profiles(llm_config, result, prefix_mode)
    return result


def get_api_key():
    """Return the highest-priority LLM API entry's key (backward compatible)."""
    llm_config = get_llm_config()
    apis = llm_config.get("apis") or []
    if not apis:
        raise ValueError("Missing llm api entries")
    primary = sorted(apis, key=lambda e: int(e.get("priority") or 0))[0]
    api_key = str(primary.get("api_key") or "").strip()
    if not api_key:
        raise ValueError("Missing llm api_key")
    return api_key
