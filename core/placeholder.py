# -*- coding: utf-8 -*-
"""微信系统占位消息判定（不在该条消息所属群展示的"版本不支持"提示）。

微信在新旧版本混用时，会把不支持渲染的消息替换成
"当前版本不支持展示该内容，请升级至最新版本。" 这类系统占位文本。
它不是群友发言：进群历史会污染 LLM 上下文，还会按普通消息触发随机接话
（实测事故：占位消息触发接话，机器人接了昨晚的旧话题）。

文案随微信版本变化，所以按键字模糊匹配，不做整句精确比较。
入口过滤（core/weflow_client.py）与随机接话（core/worker.py）共用本模块。
"""

import threading
import time

# 去空白后包含任一关键字即判定为占位消息
_PLACEHOLDER_MARKERS = (
    "当前版本不支持展示",
    "当前版本不支持查看",
    "请升级至最新版本",
    "请升级到最新版本",
    "暂不支持查看",
    "版本过低，请升级",
    "升级微信版本后查看",
)

_log_lock = threading.Lock()
_last_log_ts = 0.0


def is_placeholder(content):
    """是否为系统占位消息（去掉所有空白后再做关键字包含判断）。"""
    text = "".join(str(content or "").split())
    if not text:
        return False
    return any(marker in text for marker in _PLACEHOLDER_MARKERS)


def log_ok(interval=30.0):
    """占位消息过滤日志的节流阀：interval 秒内最多放行一条。"""
    global _last_log_ts
    with _log_lock:
        now = time.time()
        if now - _last_log_ts < interval:
            return False
        _last_log_ts = now
        return True
