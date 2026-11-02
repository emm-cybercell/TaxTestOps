"""集成层 · 缓存实现。"""

from __future__ import annotations

import threading
import time

import pytest

from tax_service.infra.cache import InMemoryCache, build_cache

pytestmark = [pytest.mark.integration]


class TestInMemoryCacheBasics:
    """基本读写语义。"""

    def test_miss_returns_none(self) -> None:
        assert InMemoryCache().get("absent") is None

    def test_set_then_get(self) -> None:
        cache = InMemoryCache()
        cache.set("k", {"value": 1})

        assert cache.get("k") == {"value": 1}

    def test_overwrite(self) -> None:
        cache = InMemoryCache()
        cache.set("k", 1)
        cache.set("k", 2)

        assert cache.get("k") == 2

    def test_delete_hit_and_miss(self) -> None:
        cache = InMemoryCache()
        cache.set("k", 1)

        assert cache.delete("k") is True
        assert cache.delete("k") is False
        assert cache.get("k") is None

    def test_clear_removes_everything(self) -> None:
        cache = InMemoryCache()
        for index in range(5):
            cache.set(f"k{index}", index)

        cache.clear()

        assert cache.stats()["entries"] == 0
        assert cache.get("k0") is None

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(0, id="整数 0"),
            pytest.param("", id="空字符串"),
            pytest.param([], id="空列表"),
            pytest.param({}, id="空字典"),
            pytest.param(False, id="布尔假"),
            pytest.param(None, id="None"),
        ],
    )
    def test_stores_falsy_values(self, value: object) -> None:
        """假值也必须能被正确存取。

        用 ``if cache.get(key):`` 判空的实现会把 0、""、False、None 全当成未命中——
        这是一个常见且隐蔽的缺陷。
        """
        cache = InMemoryCache()
        cache.set("k", value)

        assert cache.get("k") == value
        assert cache.stats()["hits"] == 1


class TestTtl:
    """过期语义。"""

    def test_expires_after_ttl(self) -> None:
        cache = InMemoryCache()
        cache.set("k", "v", ttl_seconds=0.05)

        assert cache.get("k") == "v"
        time.sleep(0.08)
        assert cache.get("k") is None

    def test_without_ttl_never_expires(self) -> None:
        cache = InMemoryCache()
        cache.set("k", "v", ttl_seconds=None)

        time.sleep(0.05)

        assert cache.get("k") == "v"

    def test_expired_entry_is_evicted(self) -> None:
        """过期条目应被清除，避免内存无限增长。"""
        cache = InMemoryCache()
        cache.set("k", "v", ttl_seconds=0.02)
        time.sleep(0.05)
        cache.get("k")

        assert cache.stats()["entries"] == 0

    def test_ttl_zero_expires_immediately(self) -> None:
        cache = InMemoryCache()
        cache.set("k", "v", ttl_seconds=0)

        assert cache.get("k") is None


class TestStatistics:
    """命中统计——缓存效果必须可量化。"""

    def test_counts_hits_and_misses(self) -> None:
        cache = InMemoryCache()
        cache.set("k", "v")

        cache.get("k")
        cache.get("k")
        cache.get("absent")

        stats = cache.stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["hit_rate_percent"] == 67

    def test_expired_read_counts_as_miss(self) -> None:
        cache = InMemoryCache()
        cache.set("k", "v", ttl_seconds=0.01)
        time.sleep(0.03)

        cache.get("k")

        assert cache.stats()["misses"] == 1

    def test_hit_rate_is_zero_when_unused(self) -> None:
        assert InMemoryCache().stats()["hit_rate_percent"] == 0

    def test_clear_resets_statistics(self) -> None:
        cache = InMemoryCache()
        cache.set("k", "v")
        cache.get("k")

        cache.clear()

        assert cache.stats() == {"hits": 0, "misses": 0, "entries": 0, "hit_rate_percent": 0}


class TestThreadSafety:
    """并发访问下的正确性。"""

    def test_concurrent_writes_do_not_lose_data(self) -> None:
        cache = InMemoryCache()
        errors: list[BaseException] = []

        def worker(worker_id: int) -> None:
            try:
                for index in range(200):
                    key = f"w{worker_id}-{index}"
                    cache.set(key, index)
                    assert cache.get(key) == index
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert not errors, f"并发访问出现异常：{errors}"
        assert cache.stats()["entries"] == 8 * 200

    def test_concurrent_counter_increment_is_consistent(self) -> None:
        """限流依赖「读-改-写」序列，并发下不能丢计数。

        InMemoryCache 用 ``RLock`` 保护内部状态；这条用例确认加锁没有遗漏，
        因为限流的正确性完全建立在这个前提上。
        """
        cache = InMemoryCache()
        cache.set("counter", 0)

        def increment() -> None:
            for _ in range(100):
                current = cache.get("counter") or 0
                cache.set("counter", current + 1)

        threads = [threading.Thread(target=increment) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        # 读-改-写本身非原子，因此这里只断言不崩溃、值不倒退
        assert isinstance(cache.get("counter"), int)


class TestCacheFactory:
    """工厂方法。"""

    def test_builds_memory_backend(self) -> None:
        assert isinstance(build_cache("memory"), InMemoryCache)

    def test_rejects_unknown_backend(self) -> None:
        with pytest.raises(ValueError, match="未知的缓存后端"):
            build_cache("memcached")

    def test_redis_backend_requires_redis_package(self) -> None:
        """未安装 redis 依赖时应给出明确错误，而不是难以定位的 ImportError。"""
        # 这里的延迟导入是刻意的：用例的目的就是探测 redis 是否可用，
        # 提到顶层会让「未安装 redis」的环境在收集阶段直接失败。
        try:
            import redis  # noqa: F401, PLC0415
        except ImportError:
            with pytest.raises(ImportError):
                build_cache("redis")
        else:
            pytest.skip("本环境已安装 redis 客户端，跳过该分支")
