"""缓存适配层：内存实现与 Redis 实现共存。

设计意图
--------
税率表是典型的「读多写少 + 外部依赖」数据：每次测算都去请求政策服务既慢又脆弱。
缓存层解决两个问题——**降低外部依赖调用量**、**在外部服务不可用时提供降级**。

测试友好性：默认使用 :class:`InMemoryCache`，因此 `pytest` 无需 Redis 即可运行；
生产环境把 ``TAX_CACHE_BACKEND`` 设为 ``redis`` 即可切换。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Cache(Protocol):
    """最小缓存接口。"""

    def get(self, key: str) -> Any | None:
        """读取缓存，未命中或已过期返回 ``None``。"""
        ...

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """写入缓存。"""
        ...

    def delete(self, key: str) -> bool:
        """删除键，返回是否命中。"""
        ...

    def clear(self) -> None:
        """清空全部键（测试与运维用）。"""
        ...

    def stats(self) -> dict[str, int]:
        """返回命中/未命中统计。"""
        ...


class InMemoryCache:
    """进程内缓存，带 TTL 与命中统计。

    .. warning::
       仅适用于单进程部署。多副本部署必须切换到 Redis，否则各副本缓存不一致。
    """

    def __init__(self) -> None:
        self._store: dict[str, tuple[float | None, Any]] = {}
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def get(self, key: str) -> Any | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                self._misses += 1
                return None

            expires_at, value = entry
            if expires_at is not None and expires_at <= time.monotonic():
                del self._store[key]
                self._misses += 1
                return None

            self._hits += 1
            return value

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        expires_at = None if ttl_seconds is None else time.monotonic() + ttl_seconds
        with self._lock:
            self._store[key] = (expires_at, value)

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._store.pop(key, None) is not None

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._hits = 0
            self._misses = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "entries": len(self._store),
                "hit_rate_percent": round(self._hits * 100 / total) if total else 0,
            }


class RedisCache:
    """基于 Redis 的缓存，用于多副本部署。

    值以 JSON 序列化，保证可读且可跨语言还原。
    """

    def __init__(self, url: str, *, prefix: str = "taxtestops:") -> None:
        # redis 是可选依赖（pyproject 的 [redis] extra）：默认部署用内存缓存，
        # 只有切到多副本时才需要它。因此延迟导入，让未安装 redis 的环境照常运行。
        import json  # noqa: PLC0415

        import redis  # noqa: PLC0415

        self._json = json
        self._client = redis.Redis.from_url(url, decode_responses=True)
        self._prefix = prefix
        self._hits = 0
        self._misses = 0

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def get(self, key: str) -> Any | None:
        raw = self._client.get(self._key(key))
        if raw is None:
            self._misses += 1
            return None
        self._hits += 1
        return self._json.loads(raw)

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        payload = self._json.dumps(value, ensure_ascii=False)
        if ttl_seconds is None:
            self._client.set(self._key(key), payload)
        else:
            self._client.setex(self._key(key), ttl_seconds, payload)

    def delete(self, key: str) -> bool:
        return bool(self._client.delete(self._key(key)))

    def clear(self) -> None:
        keys = self._client.keys(f"{self._prefix}*")
        if keys:
            self._client.delete(*keys)
        self._hits = self._misses = 0

    def stats(self) -> dict[str, int]:
        total = self._hits + self._misses
        return {
            "hits": self._hits,
            "misses": self._misses,
            "entries": len(self._client.keys(f"{self._prefix}*")),
            "hit_rate_percent": round(self._hits * 100 / total) if total else 0,
        }


def build_cache(backend: str = "memory", *, redis_url: str = "redis://localhost:6379/0") -> Cache:
    """按名称构造缓存实例。"""
    if backend == "memory":
        return InMemoryCache()
    if backend == "redis":
        return RedisCache(redis_url)
    raise ValueError(f"未知的缓存后端：{backend!r}（可选：memory / redis）")
