# -*- coding: utf-8 -*-
"""按到达顺序发放的 LLM 并发闸门（FIFO + 优先级）。

替代原来的 `threading.BoundedSemaphore`：信号量不保证顺序（后到的线程可能
先抢到），而且抢不到就直接丢弃请求——被 @ 的消息会永久不被回复
（2026-09-21 前 run.log 里累计丢弃 10 次）。

这里改成"排队等待"：
- 同优先级严格按到达顺序发放（先来先服务）
- 高优先级（被 @ / 命令）插到低优先级（随机接话、后台批量）前面
- 排队超时才放弃，调用方据此决定是给兜底话术还是静默
"""

import threading
import time

# 优先级：数字越小越优先
PRIORITY_USER = 0      # 被 @、命令、/pro、/btw
PRIORITY_BACKGROUND = 1  # 随机接话、注意力检查、后台批量

# 最近一次拿到槽位的闸门实例。热重载（core/project_reloader.py）要在
# os._exit 之前等这条在途请求收尾，否则生成到一半的回复会被硬杀丢掉。
_gate_lock = threading.Lock()
_active_gate = None


def set_active_gate(gate):
    """登记最新一次 acquire 成功的闸门实例。

    每次 acquire 成功都覆盖：Dispatcher 会在 _init_llm 里按并发数重建
    LlmGate，只在构造时登记会让热重载去问已经作废的旧实例。
    """
    global _active_gate
    with _gate_lock:
        _active_gate = gate


def get_active_gate():
    """返回最近一次有请求拿到槽位的闸门；从未有过（或 LLM 未启用）返回 None。"""
    with _gate_lock:
        return _active_gate


class LlmGate:
    """有界并发 + 排队 + 优先级。接口与 BoundedSemaphore 兼容（acquire/release）。"""

    def __init__(self, slots=1):
        self._cond = threading.Condition()
        self._slots = max(1, int(slots or 1))
        self._in_use = 0
        self._seq = 0
        self._waiters = []  # [[priority_rank, 序号, 已放行标志], ...]

    @property
    def slots(self):
        return self._slots

    def depth(self):
        """返回 (占用中, 排队中)。"""
        with self._cond:
            return self._in_use, len(self._waiters)

    def _grant_locked(self):
        """有空闲槽位时，按 (优先级, 到达顺序) 放行等待者（调用方需持锁）。"""
        while self._in_use < self._slots and self._waiters:
            self._waiters.sort(key=lambda ticket: (ticket[0], ticket[1]))
            ticket = self._waiters.pop(0)
            ticket[2] = True
            self._in_use += 1
            self._cond.notify_all()

    def acquire(self, priority_rank=PRIORITY_BACKGROUND, timeout=None):
        """排队获取一个槽位。返回 True=拿到，False=超时放弃。"""
        try:
            rank = int(priority_rank)
        except (TypeError, ValueError):
            rank = PRIORITY_BACKGROUND
        granted = False
        with self._cond:
            # 无人排队且有空位 → 直接进，不用排队
            if not self._waiters and self._in_use < self._slots:
                self._in_use += 1
                granted = True
            else:
                self._seq += 1
                ticket = [rank, self._seq, False]
                self._waiters.append(ticket)
                deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
                granted = True
                while not ticket[2]:
                    remaining = None
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            try:
                                self._waiters.remove(ticket)
                            except ValueError:
                                # 已被放行（与超时竞争）→ 算成功
                                break
                            granted = False
                            break
                    self._cond.wait(remaining)
        if granted:
            # 在锁外登记，避免 _cond 与 _gate_lock 嵌套
            set_active_gate(self)
        return granted

    def release(self):
        with self._cond:
            if self._in_use > 0:
                self._in_use -= 1
            else:
                # 重复释放说明有 bug，但不该让调用方炸掉
                print("[LLM GATE] release 次数多于 acquire（忽略）", flush=True)
            self._grant_locked()

    # 兼容旧调用点可能用的写法
    def __enter__(self):
        self.acquire(PRIORITY_BACKGROUND, None)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False
