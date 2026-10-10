# -*- coding: utf-8 -*-
"""Bot 运行统计：/about 指令的数据源。持久化在 data/bot_stats.json。"""

import atexit
import json
import os
import threading
import time

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATS_PATH = os.path.join(_BASE_DIR, "data", "bot_stats.json")
_LOCK = threading.Lock()

# record_tokens 节流落盘：内存累计，距上次写盘 <60 秒跳过，atexit 兜底
_TOKEN_SAVE_INTERVAL = 60.0
_pending_tokens = {}
_last_token_save = 0.0

# 小米 MiMo 官方定价（国内，元/百万 tokens，实时推理）
# 来源：https://mimo.mi.com/docs/zh-CN/price/pay-as-you-go （2026-09-27 抓取，已核实）
#   mimo-v2.6-pro             缓存命中 0.025 / 未命中输入 3.00  / 输出 6.00
#   mimo-v2.6-flash           缓存命中 0.02  / 未命中输入 1.00  / 输出 2.00
#   mimo-v2.6-pro-ultraspeed  缓存命中 0.25  / 未命中输入 30.00 / 输出 60.00  ← 比 pro 贵 10 倍
#   （v2.5 / v2.5-pro 与同名 v2.6 档同价，且将于 2026-10-21 下线）
# deepseek 端点没有公开费率表，仍借用小米的档位估算，金额仅供参考。
_PRICING = {
    "pro": {"input": 3.0, "cached_input": 0.025, "output": 6.0},
    "default": {"input": 1.0, "cached_input": 0.02, "output": 2.0},
}

# 精确到模型名的覆盖表（先查这里，再退回 "pro"/"default" 两档）。
# 存在的意义：ultraspeed 名字里也含 "pro"，按分档规则会被算成 pro 档，
# 而它实际贵 10 倍——不单独列出就会把费用少报一个数量级。
_PRICING_BY_MODEL = {
    "mimo-v2.6-pro-ultraspeed": {"input": 30.0, "cached_input": 0.25, "output": 60.0},
}


def _default_data():
    return {
        "first_launch_ts": None,
        "tokens": {},
        "chimein_events": [],
        # 按群 token 事件：[ts, group, model, input, cached_input, output]
        # 只保留最近 7 天，供 /about 输出"本群最近 24 小时"用量
        "group_token_events": [],
    }


def _load():
    try:
        with open(_STATS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            merged = _default_data()
            merged.update(data)
            return merged
    except Exception:
        pass
    return _default_data()


def _save(data):
    tmp = _STATS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _STATS_PATH)


def first_launch_ts():
    with _LOCK:
        data = _load()
        ts = data.get("first_launch_ts")
        if isinstance(ts, (int, float)) and ts > 0:
            return float(ts)
        # 首次运行：用 run.log 的文件创建时间作为部署时间，没有就记现在
        try:
            ts = os.path.getctime(os.path.join(_BASE_DIR, "run.log"))
        except OSError:
            ts = time.time()
        data["first_launch_ts"] = ts
        try:
            _save(data)
        except OSError:
            pass
        return float(ts)


def record_tokens(model="", input_tokens=0, cached_input=0, output_tokens=0):
    try:
        input_tokens = int(input_tokens or 0)
        cached_input = int(cached_input or 0)
        output_tokens = int(output_tokens or 0)
    except (TypeError, ValueError):
        return
    if input_tokens <= 0 and output_tokens <= 0:
        return
    key = str(model or "unknown").strip() or "unknown"
    global _last_token_save
    with _LOCK:
        bucket = _pending_tokens.setdefault(
            key, {"input": 0, "cached_input": 0, "output": 0}
        )
        bucket["input"] += input_tokens
        bucket["cached_input"] += cached_input
        bucket["output"] += output_tokens
        now = time.time()
        if now - _last_token_save < _TOKEN_SAVE_INTERVAL:
            return
        _last_token_save = now
        _flush_tokens_locked()


def _flush_tokens_locked():
    """把内存累计的 token 合并进 stats 文件（调用方需持有 _LOCK）。

    写盘失败时保留 _pending_tokens，下次 flush 重试，不丢数据。
    """
    if not _pending_tokens:
        return
    data = _load()
    for key, pending in _pending_tokens.items():
        bucket = data["tokens"].setdefault(
            key, {"input": 0, "cached_input": 0, "output": 0}
        )
        bucket["input"] += pending["input"]
        bucket["cached_input"] += pending["cached_input"]
        bucket["output"] += pending["output"]
    try:
        _save(data)
    except OSError:
        return
    _pending_tokens.clear()


def flush_tokens():
    """立即把内存中累计的 token 用量落盘（退出兜底/手动调用）。"""
    global _last_token_save
    with _LOCK:
        _last_token_save = time.time()
        _flush_tokens_locked()


atexit.register(flush_tokens)


def record_chimein(kind="random", group=""):
    """记录一次接话（kind: random=2%随机接话 / btw=/btw 手动触发）。

    事件带群名，供 /about 按群统计（多群下不能拿全局接话数去减本群消息数）。
    """
    with _LOCK:
        data = _load()
        events = data["chimein_events"]
        events.append([time.time(), str(kind or "random"), str(group or "")])
        # 只保留最近 7 天的事件，文件不会无限膨胀
        cutoff = time.time() - 7 * 86400
        data["chimein_events"] = [
            e for e in events
            if isinstance(e, list) and e and float(e[0]) > cutoff
        ]
        try:
            _save(data)
        except OSError:
            pass


def chimein_counts(hours=24, group=None):
    """过去 hours 小时的接话次数。返回 (总次数, 其中 /btw 次数)。

    group 非空时只统计该群的接话（旧格式无群信息的事件不计入）。
    """
    cutoff = time.time() - hours * 3600
    with _LOCK:
        events = list(_load().get("chimein_events") or [])
    want = str(group).strip() if group else ""
    total = btw = 0
    for e in events:
        if not isinstance(e, list) or len(e) < 2:
            continue
        try:
            ts, kind = float(e[0]), str(e[1])
        except (TypeError, ValueError):
            continue
        if ts <= cutoff:
            continue
        if want and (len(e) < 3 or str(e[2] or "").strip() != want):
            continue
        total += 1
        if kind == "btw":
            btw += 1
    return total, btw


def _model_cost_cny(model, inp, cached, out):
    """按模型单价估算费用（元）。input 含缓存部分，未命中 = input - cached。"""
    name = str(model or "").strip().lower()
    rates = _PRICING_BY_MODEL.get(name)
    if rates is None:
        # 精确名没命中时按档位估；"pro" 分档对 v2.6-pro / v2.5-pro / deepseek-*-pro 都成立
        rates = _PRICING["pro"] if "pro" in name else _PRICING["default"]
    return (
        (inp - cached) / 1e6 * rates["input"]
        + cached / 1e6 * rates["cached_input"]
        + out / 1e6 * rates["output"]
    )


_GROUP_TOKEN_RETENTION_DAYS = 7


def record_group_tokens(group, model="", input_tokens=0, cached_input=0, output_tokens=0):
    """按群记录一次 LLM 用量，供 /about 输出"本群最近 24 小时"。

    与全局 tokens 分开存储，避免和累计口径互相污染。
    """
    name = str(group or "").strip()
    if not name:
        return
    try:
        inp = int(input_tokens or 0)
        cached = int(cached_input or 0)
        out = int(output_tokens or 0)
    except (TypeError, ValueError):
        return
    if inp <= 0 and out <= 0:
        return
    cutoff = time.time() - _GROUP_TOKEN_RETENTION_DAYS * 86400
    with _LOCK:
        data = _load()
        events = data.get("group_token_events") or []
        events.append([time.time(), name, str(model or "unknown"), inp, cached, out])
        data["group_token_events"] = [
            e
            for e in events
            if isinstance(e, list) and len(e) >= 6 and float(e[0]) > cutoff
        ]
        try:
            _save(data)
        except OSError:
            pass


def group_token_usage(group, hours=24):
    """某个群过去 hours 小时的 token 用量。

    返回 (每模型明细[(model, input, cached, output, cost)], 合计(输入, 缓存, 输出), 总费用)。
    """
    name = str(group or "").strip()
    if not name:
        return [], (0, 0, 0), 0.0
    cutoff = time.time() - max(1.0, float(hours)) * 3600
    with _LOCK:
        events = list(_load().get("group_token_events") or [])
    buckets = {}
    for e in events:
        if not isinstance(e, list) or len(e) < 6:
            continue
        try:
            ts = float(e[0])
            if ts <= cutoff or str(e[1]) != name:
                continue
            model = str(e[2] or "unknown")
            inp = int(e[3] or 0)
            cached = int(e[4] or 0)
            out = int(e[5] or 0)
        except (TypeError, ValueError):
            continue
        bucket = buckets.setdefault(model, [0, 0, 0])
        bucket[0] += inp
        bucket[1] += cached
        bucket[2] += out
    lines = []
    total_in = total_cached = total_out = 0
    total_cost = 0.0
    for model, (inp, cached, out) in sorted(buckets.items()):
        cached = min(cached, inp)
        cost = _model_cost_cny(model, inp, cached, out)
        total_cost += cost
        total_in += inp
        total_cached += cached
        total_out += out
        lines.append((model, inp, cached, out, cost))
    return lines, (total_in, total_cached, total_out), total_cost


def estimate_cost_cny():
    """累计 token 费用（元）。

    返回 (总费用, 每模型明细[(model, input, cached, output, cost)], 合计(输入, 缓存, 输出))。
    OpenAI 风格的 prompt_tokens 包含缓存部分，未命中部分 = input - cached_input。
    """
    with _LOCK:
        tokens = dict(_load().get("tokens") or {})
    total_cost = 0.0
    lines = []
    total_in = total_cached = total_out = 0
    for model, bucket in sorted(tokens.items()):
        try:
            inp = int(bucket.get("input") or 0)
            cached = min(int(bucket.get("cached_input") or 0), inp)
            out = int(bucket.get("output") or 0)
        except (TypeError, ValueError, AttributeError):
            continue
        cost = _model_cost_cny(model, inp, cached, out)
        total_cost += cost
        total_in += inp
        total_cached += cached
        total_out += out
        lines.append((model, inp, cached, out, cost))
    return total_cost, lines, (total_in, total_cached, total_out)
