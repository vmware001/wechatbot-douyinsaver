import atexit
import json
import os
import threading
import time
from collections import OrderedDict

# 内存与磁盘都只保留最新 5000 条
_MAX_ENTRIES = 5000
# 落盘节流：间隔/新增计数双阈值，进程退出时 atexit 兜底落盘
_SAVE_INTERVAL = 5.0
_SAVE_EVERY = 100


class Dedup:
    """磁盘持久化的消息去重表。

    纯内存 set 会在进程重启后清空，而 weflow-core 的推送服务对新连接
    会重放最近约 10 分钟的消息缓存，导致重启后把重启前的 @ 再回复一遍。
    rawid 持久化到 data/dedup_cache.json 后，跨重启也不会重复回复。
    """

    def __init__(self, path=None):
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.path = path or os.path.join(base, "data", "dedup_cache.json")
        self._lock = threading.Lock()
        # OrderedDict 做有界 LRU：保留插入顺序，超限弹出最旧条目
        # （原实现用 set：内存无界增长，且 list(set)[-5000:] 截断的是任意条目而非最新）
        self.cache = OrderedDict()
        self._dirty = 0
        self._last_save = 0.0
        self._last_warn = 0.0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for key in data[-_MAX_ENTRIES:]:
                    self.cache[key] = None
        except Exception:
            self.cache = OrderedDict()
        atexit.register(self.flush)

    def exists(self, key):
        return key in self.cache

    def add(self, key):
        with self._lock:
            self.cache[key] = None
            self.cache.move_to_end(key)
            while len(self.cache) > _MAX_ENTRIES:
                self.cache.popitem(last=False)
            self._dirty += 1
            now = time.monotonic()
            if self._dirty >= _SAVE_EVERY or now - self._last_save >= _SAVE_INTERVAL:
                self._save_locked()

    def flush(self):
        """立即把脏数据落盘（atexit 兜底/手动调用）。"""
        with self._lock:
            self._save_locked(force=True)

    def _save_locked(self, force=False):
        """调用方需持有 self._lock。落盘失败时节流告警，不再静默吞掉。"""
        if not force and not self._dirty:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(list(self.cache.keys()), f)
            os.replace(tmp, self.path)
            self._dirty = 0
            self._last_save = time.monotonic()
        except Exception as exc:
            now = time.monotonic()
            if now - self._last_warn >= 60.0:
                self._last_warn = now
                print(f"[DEDUP] 去重缓存落盘失败（重启后可能重复回复）：{exc}", flush=True)
