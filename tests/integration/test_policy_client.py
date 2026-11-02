"""集成层 · 政策服务客户端（外部依赖）。

被测行为
--------
政策服务是本项目唯一的**外部依赖**，它的三种状态必须都被覆盖：

====================  ======================  ====================
场景                  行为                    响应中的 source
====================  ======================  ====================
缓存命中              直接返回缓存             ``cache``
缓存未命中 + 远程成功  写缓存并返回             ``remote``
缓存未命中 + 远程失败  返回内置兜底参数         ``fallback``
====================  ======================  ====================

用 ``respx`` 拦截 httpx 请求来构造远程成功/失败，不需要真实外部服务。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tax_service.core.policy import BASIC_DEDUCTION
from tax_service.external.policy_client import (
    CACHE_KEY,
    FALLBACK_VERSION,
    PolicyServiceClient,
    default_parameters,
)
from tax_service.infra.cache import InMemoryCache

pytestmark = [pytest.mark.integration, pytest.mark.regression]

SERVICE_URL = "https://policy.test.local"
PARAMETERS_PATH = "/api/v1/parameters"

REMOTE_PAYLOAD = {
    "version": "2026-01",
    "basic_deduction": 6_000,
    "effective_from": "2026-01-01",
}


@pytest.fixture
def cache() -> InMemoryCache:
    return InMemoryCache()


@pytest.fixture
def client(cache: InMemoryCache) -> PolicyServiceClient:
    return PolicyServiceClient(
        SERVICE_URL,
        timeout_seconds=1.0,
        cache=cache,
        cache_ttl_seconds=300,
    )


class TestRemoteSuccess:
    """远程可用时的行为。"""

    @respx.mock
    def test_fetches_from_remote(self, client: PolicyServiceClient) -> None:
        respx.get(f"{SERVICE_URL}{PARAMETERS_PATH}").mock(
            return_value=httpx.Response(200, json=REMOTE_PAYLOAD)
        )

        parameters = client.fetch_parameters()

        assert parameters.source == "remote"
        assert parameters.version == "2026-01"
        assert parameters.basic_deduction == 6_000

    @respx.mock
    def test_writes_through_to_cache(
        self, client: PolicyServiceClient, cache: InMemoryCache
    ) -> None:
        respx.get(f"{SERVICE_URL}{PARAMETERS_PATH}").mock(
            return_value=httpx.Response(200, json=REMOTE_PAYLOAD)
        )

        client.fetch_parameters()

        assert cache.get(CACHE_KEY) is not None
        assert client.remote_call_count == 1

    @respx.mock
    def test_second_call_is_served_from_cache(self, client: PolicyServiceClient) -> None:
        """缓存的核心价值：第二次调用不再打远程。

        断言的是**远程调用次数**，而不是返回值——只看返回值无法区分「缓存生效」
        与「每次都在请求远程但结果恰好一样」。
        """
        route = respx.get(f"{SERVICE_URL}{PARAMETERS_PATH}").mock(
            return_value=httpx.Response(200, json=REMOTE_PAYLOAD)
        )

        first = client.fetch_parameters()
        second = client.fetch_parameters()

        assert first.source == "remote"
        assert second.source == "cache"
        assert route.call_count == 1, f"远程被调用了 {route.call_count} 次，缓存未生效"
        assert client.remote_call_count == 1

    @respx.mock
    def test_repeated_calls_hit_remote_only_once(self, client: PolicyServiceClient) -> None:
        route = respx.get(f"{SERVICE_URL}{PARAMETERS_PATH}").mock(
            return_value=httpx.Response(200, json=REMOTE_PAYLOAD)
        )

        for _ in range(10):
            client.fetch_parameters()

        assert route.call_count == 1


class TestDegradation:
    """远程不可用时的降级行为。"""

    @respx.mock
    def test_falls_back_on_connection_error(self, client: PolicyServiceClient) -> None:
        respx.get(f"{SERVICE_URL}{PARAMETERS_PATH}").mock(
            side_effect=httpx.ConnectError("连接被拒绝")
        )

        parameters = client.fetch_parameters()

        assert parameters.source == "fallback"
        assert parameters.version == FALLBACK_VERSION
        assert parameters.basic_deduction == BASIC_DEDUCTION

    @respx.mock
    def test_falls_back_on_server_error(self, client: PolicyServiceClient) -> None:
        respx.get(f"{SERVICE_URL}{PARAMETERS_PATH}").mock(
            return_value=httpx.Response(500, json={"error": "内部错误"})
        )

        assert client.fetch_parameters().source == "fallback"

    @respx.mock
    def test_falls_back_on_timeout(self, client: PolicyServiceClient) -> None:
        respx.get(f"{SERVICE_URL}{PARAMETERS_PATH}").mock(side_effect=httpx.ReadTimeout("超时"))

        assert client.fetch_parameters().source == "fallback"

    @respx.mock
    def test_falls_back_on_malformed_payload(self, client: PolicyServiceClient) -> None:
        """响应格式非法（缺字段）时也必须降级，而不是抛异常穿透到接口层。"""
        respx.get(f"{SERVICE_URL}{PARAMETERS_PATH}").mock(
            return_value=httpx.Response(200, json={"unexpected": True})
        )

        assert client.fetch_parameters().source == "fallback"

    @respx.mock
    def test_falls_back_on_non_json_body(self, client: PolicyServiceClient) -> None:
        respx.get(f"{SERVICE_URL}{PARAMETERS_PATH}").mock(
            return_value=httpx.Response(200, text="<html>维护中</html>")
        )

        assert client.fetch_parameters().source == "fallback"

    @respx.mock
    def test_degradation_is_visible_not_silent(self, client: PolicyServiceClient) -> None:
        """降级必须**可观测**：``source`` 字段如实标注来源。

        这是与 SUT 缺陷 D5（静默忽略非法值）的关键区别——降级可以接受，
        但把降级伪装成正常结果不可以。监控系统靠这个字段发现上游故障。
        """
        respx.get(f"{SERVICE_URL}{PARAMETERS_PATH}").mock(
            side_effect=httpx.ConnectError("连接被拒绝")
        )

        assert client.fetch_parameters().source == "fallback"

    @respx.mock
    def test_recovers_after_remote_comes_back(self, client: PolicyServiceClient) -> None:
        """远程恢复后应重新走远程路径——降级不能把缓存「毒化」。"""
        route = respx.get(f"{SERVICE_URL}{PARAMETERS_PATH}").mock(
            side_effect=httpx.ConnectError("连接被拒绝")
        )

        assert client.fetch_parameters().source == "fallback"

        route.mock(return_value=httpx.Response(200, json=REMOTE_PAYLOAD))
        assert client.fetch_parameters().source == "remote"


class TestCacheRobustness:
    """缓存内容异常时的防御。"""

    def test_ignores_corrupted_cache_entry(
        self, client: PolicyServiceClient, cache: InMemoryCache
    ) -> None:
        """缓存被写坏（缺字段/类型错）时应忽略并回源，而不是抛异常。"""
        cache.set(CACHE_KEY, {"version": "broken"})

        parameters = client.fetch_parameters()

        assert parameters.source == "fallback"
        assert parameters.basic_deduction == BASIC_DEDUCTION

    def test_ignores_non_dict_cache_entry(
        self, client: PolicyServiceClient, cache: InMemoryCache
    ) -> None:
        cache.set(CACHE_KEY, "not-a-dict")

        assert client.fetch_parameters().source == "fallback"

    def test_works_without_cache(self) -> None:
        """未配置缓存时仍可用（每次都回源），不应崩溃。"""
        client = PolicyServiceClient(SERVICE_URL, cache=None)

        assert client.fetch_parameters().source == "fallback"

    def test_ttl_expiry_forces_refetch(self) -> None:
        """缓存过期后必须回源，不能一直用旧参数。"""
        cache = InMemoryCache()
        client = PolicyServiceClient(SERVICE_URL, cache=cache, cache_ttl_seconds=300)

        cache.set(
            CACHE_KEY, {"version": "old", "basic_deduction": 5_000, "effective_from": "2019-01-01"}
        )
        assert client.fetch_parameters().version == "old"

        cache.clear()
        assert client.fetch_parameters().source == "fallback"


class TestDefaultParameters:
    """内置兜底参数。"""

    def test_matches_requirement_document(self) -> None:
        """兜底参数必须与需求文档一致，否则降级会导致系统性算错。"""
        parameters = default_parameters()

        assert parameters.basic_deduction == BASIC_DEDUCTION == 5_000
        assert parameters.source == "fallback"

    def test_serializable(self) -> None:
        payload = default_parameters().to_dict()

        assert set(payload) == {"version", "basic_deduction", "effective_from", "source"}
