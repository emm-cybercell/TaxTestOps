"""执行过程的实时事件广播（WebSocket 用）。

为什么需要它
------------
批量执行可能持续几十秒。如果只能等执行结束再刷新页面，用户会盯着一个没有反馈的
界面，无法判断「是在跑还是卡住了」。实时推送把执行进度变成可观测的——
这是平台相对脚本最直观的价值之一。

线程模型
--------
执行发生在**线程池**里（同步 httpx），而 WebSocket 推送发生在 **asyncio 事件循环**里。
跨线程往 ``asyncio.Queue`` 投递消息必须用 ``loop.call_soon_threadsafe``，
否则会静默丢消息或在极端情况下破坏事件循环。这一点很容易写错且难以复现，
因此单独成模块并配了并发用例。
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

logger = logging.getLogger(__name__)

QUEUE_MAX_SIZE = 256
"""单个订阅者的缓冲上限。

超过上限时丢弃**最旧**的事件而不是阻塞发布方——执行线程绝不能因为某个慢客户端
而被拖住。丢事件比拖垮执行可接受得多，而且订阅者可以从数据库补齐最终结果。
"""


class RunEventBroker:
    """按执行 ID 分组的事件广播器（进程内）。"""

    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定事件循环，使执行线程可以安全投递事件。

        在应用启动（lifespan）时调用。未绑定循环时 ``publish`` 会降级为直接入队，
        这样纯同步测试无需启动事件循环也能验证事件内容。
        """
        self._loop = loop

    # ------------------------------------------------------------------ #
    # 订阅
    # ------------------------------------------------------------------ #

    def subscribe(self, run_id: str) -> asyncio.Queue:
        """订阅某个执行的事件流。"""
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._subscribers[run_id].add(queue)
        logger.debug("新增订阅者：run_id=%s，当前订阅数=%d", run_id, len(self._subscribers[run_id]))
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue) -> None:
        """取消订阅；无订阅者时清理该分组，避免字典无限增长。"""
        group = self._subscribers.get(run_id)
        if group is None:
            return

        group.discard(queue)
        if not group:
            del self._subscribers[run_id]

    def subscriber_count(self, run_id: str) -> int:
        return len(self._subscribers.get(run_id, ()))

    def clear(self) -> None:
        """清空全部订阅（测试与运维用）。"""
        self._subscribers.clear()

    # ------------------------------------------------------------------ #
    # 发布
    # ------------------------------------------------------------------ #

    def publish(self, run_id: str, event: dict[str, Any]) -> None:
        """向某个执行的所有订阅者投递事件。

        线程安全：可从执行线程调用。没有订阅者时立即返回，不产生任何开销。
        """
        group = self._subscribers.get(run_id)
        if not group:
            return

        if self._loop is not None and self._loop.is_running():
            for queue in list(group):
                self._loop.call_soon_threadsafe(self._offer, queue, event)
        else:
            for queue in list(group):
                self._offer(queue, event)

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict[str, Any]) -> None:
        """入队；队列满时丢弃最旧事件，保证不阻塞发布方。"""
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.put_nowait(event)
            except (asyncio.QueueEmpty, asyncio.QueueFull):  # pragma: no cover - 竞态兜底
                logger.warning("事件队列拥塞且丢弃失败，本条事件被跳过")
