# -*- coding: utf-8 -*-
"""接话冷却的共享状态：worker 的 2% 随机接话和 dispatcher 的 /btw 都用它。

按群独立计时：多群部署时 A 群的接话冷却不阻塞 B 群。
group 传 None/空串时归入 "_global" 桶（兼容旧调用）。
"""

import threading
import time

_LOCK = threading.Lock()
_last_fired_by_group = {}


def _key(group):
    key = str(group or "").strip()
    return key or "_global"


def cooldown_active(cooldown_seconds, group=None):
    with _LOCK:
        last = _last_fired_by_group.get(_key(group), 0.0)
        return (time.time() - last) < cooldown_seconds


def mark_fired(group=None):
    """接话实际发出后调用，该群的冷却从这一刻重新计算。"""
    with _LOCK:
        _last_fired_by_group[_key(group)] = time.time()


def snapshot(group=None):
    """记录当前冷却时间戳（接话前调用，配合 restore 回滚）。"""
    with _LOCK:
        return _last_fired_by_group.get(_key(group), 0.0)


def restore(group=None, ts=0.0):
    """把冷却时间戳写回接话前的快照：接话没真正发出去时调用，等于这次没发生。"""
    with _LOCK:
        try:
            value = float(ts)
        except (TypeError, ValueError):
            value = 0.0
        _last_fired_by_group[_key(group)] = max(0.0, value)
