"""限流：固定窗口计数器。

用缓存后端存储计数，因此切换到 Redis 后即可在多副本间共享配额。

.. note::
   固定窗口在窗口切换处最多允许 2 倍突发流量。生产环境若需要更平滑的控制，
   应改用滑动窗口或令牌桶。此处选择固定窗口是为了让行为**可被精确断言**——
   测试可以构造出确定的边界场景。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from tax_service.infra.cache import Cache

WINDOW_SECONDS = 60


@dataclass(frozen=True)
class RateLimitDecision:
    """限流判定结果。"""

    allowed: bool
    remaining: int
    retry_after_seconds: int


class RateLimiter:
    """按 key 限流的固定窗口计数器。"""

    def __init__(self, cache: Cache, limit_per_minute: int) -> None:
        self._cache = cache
        self._limit = limit_per_minute

    @property
    def limit(self) -> int:
        return self._limit

    def check(self, key: str, *, now: float | None = None) -> RateLimitDecision:
        """记录一次访问并返回是否放行。"""
        if self._limit <= 0:
            # 限额 <= 0 视为关闭限流
            return RateLimitDecision(allowed=True, remaining=0, retry_after_seconds=0)

        timestamp = time.time() if now is None else now
        window_start = int(timestamp // WINDOW_SECONDS) * WINDOW_SECONDS
        cache_key = f"ratelimit:{key}:{window_start}"

        current = self._cache.get(cache_key)
        count = int(current) + 1 if isinstance(current, int) else 1
        self._cache.set(cache_key, count, WINDOW_SECONDS + 1)

        remaining = max(0, self._limit - count)
        if count > self._limit:
            # 用 max(1, ...) 而不是 `or 1`：后者在计算结果为 0 或负数时
            # 会原样返回负值（Python 中负数是真值），导致 Retry-After 为负。
            retry_after = max(1, int(window_start + WINDOW_SECONDS - timestamp))
            return RateLimitDecision(allowed=False, remaining=0, retry_after_seconds=retry_after)

        return RateLimitDecision(allowed=True, remaining=remaining, retry_after_seconds=0)
