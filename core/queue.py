import threading
import time
from queue import Full, Queue

# SSE 端不过滤直接入队，消费端 LLM 槽位极少：队列必须有界，
# 满时丢弃新消息（不阻塞 put 方），防止消息洪水时内存无界增长
_QUEUE_MAXSIZE = 2000
# 丢弃日志节流：每 30 秒最多一条
_DROP_LOG_INTERVAL = 30.0


class BoundedTaskQueue(Queue):
    """满时丢弃新消息并打节流 warning，其余 API 与 queue.Queue 一致。"""

    def __init__(self, maxsize=_QUEUE_MAXSIZE):
        super().__init__(maxsize=maxsize)
        self._drop_log_lock = threading.Lock()
        self._last_drop_log_ts = 0.0
        self._dropped_count = 0

    def put(self, item, block=True, timeout=None):
        try:
            super().put(item, block=False)
        except Full:
            self._log_drop()

    def _log_drop(self):
        with self._drop_log_lock:
            self._dropped_count += 1
            now = time.monotonic()
            if now - self._last_drop_log_ts < _DROP_LOG_INTERVAL:
                return
            self._last_drop_log_ts = now
            dropped = self._dropped_count
            self._dropped_count = 0
        print(
            f"[QUEUE FULL] 队列已满(maxsize={self.maxsize})，丢弃新消息"
            f"（30秒内累计丢弃 {dropped} 条）",
            flush=True,
        )


task_queue = BoundedTaskQueue()
