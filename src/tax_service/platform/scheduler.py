"""定时执行调度。

实现选择
--------
用**后台线程 + 可中断等待**，而不是引入 APScheduler / Celery：

- 平台只需「每 N 秒跑一次」这一种调度语义，引入调度框架是过度设计；
- 依赖越少，部署与测试越简单；
- 关键是让行为**可被确定性测试**——``run_once`` 与 ``tick`` 都是显式方法，
  测试直接调用即可，不必等真实的定时器触发。

单进程假设：调度状态在进程内存里，多副本部署会各跑一份。生产环境应改为
分布式锁 + 独立调度进程；这一点在 ``docs/06-测试平台.md`` 中说明。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 300


class RunScheduler:
    """周期性地触发一次平台执行。"""

    def __init__(
        self,
        *,
        run_once: Callable[[str | None], int | None],
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        tag: str | None = None,
    ) -> None:
        """
        :param run_once: 执行一次的回调，接收标签并返回 run_id。由调用方注入，
            使调度器不依赖具体的服务实现，便于单测。
        :param interval_seconds: 周期间隔。
        :param tag: 每次执行只跑带该标签的用例。
        """
        self._run_once = run_once
        self.interval_seconds = max(1, interval_seconds)
        self.tag = tag

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._executions = 0
        self._last_run_id: int | None = None
        self._last_started_at: datetime | None = None

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        """启动后台调度线程（重复调用无副作用）。"""
        with self._lock:
            if self.running:
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._loop, name="tax-testops-scheduler", daemon=True
            )
            self._thread.start()
            logger.info("定时调度已启动：间隔 %d 秒，标签 %s", self.interval_seconds, self.tag)

    def stop(self, *, timeout: float = 5.0) -> None:
        """请求停止并等待线程退出。"""
        with self._lock:
            thread = self._thread
        if thread is None:
            return

        self._stop_event.set()
        thread.join(timeout=timeout)
        self._thread = None
        logger.info("定时调度已停止")

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #

    def tick(self) -> int | None:
        """执行一次并记录状态；异常被吞掉，避免调度线程静默死掉。"""
        try:
            run_id = self._run_once(self.tag)
        except Exception:  # noqa: BLE001 - 调度线程不能因单次失败而退出
            logger.exception("定时执行失败，等待下一个周期")
            return None

        with self._lock:
            self._executions += 1
            self._last_run_id = run_id
            self._last_started_at = datetime.now(UTC)
        return run_id

    def _loop(self) -> None:
        # 先等一个周期再执行：服务刚启动时立刻压一轮没有意义，
        # 反而会与初始化/建表竞争资源。
        while not self._stop_event.wait(self.interval_seconds):
            self.tick()

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #

    @property
    def executions(self) -> int:
        with self._lock:
            return self._executions

    def status(self) -> dict[str, Any]:
        """返回调度状态快照，供运维接口与监控消费。"""
        with self._lock:
            last_started = self._last_started_at
            next_run = (
                last_started + timedelta(seconds=self.interval_seconds)
                if self.running and last_started
                else (datetime.now(UTC) + timedelta(seconds=self.interval_seconds))
                if self.running
                else None
            )
            return {
                "enabled": self.running,
                "interval_seconds": self.interval_seconds,
                "tag": self.tag,
                "next_run_at": next_run.isoformat() if next_run else None,
                "last_run_id": self._last_run_id,
                "executions": self._executions,
            }


def wait_for_scheduler_ready(scheduler: RunScheduler, timeout: float = 5.0) -> bool:
    """等待调度线程真正起来（供测试与启动脚本使用）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if scheduler.running:
            return True
        time.sleep(0.01)
    return False
