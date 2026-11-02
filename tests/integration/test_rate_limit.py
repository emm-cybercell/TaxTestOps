"""集成层 · 限流。

限流是「接口层防御」的一部分，但它的正确性完全依赖缓存的原子性，因此放在集成层。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI

from support.api_client import ApiClient
from tax_service.infra.cache import InMemoryCache
from tax_service.infra.ratelimit import RateLimiter

pytestmark = [pytest.mark.integration]


class TestRateLimiterUnit:
    """限流器本身的判定逻辑（用显式时间戳保证确定性）。"""

    def test_allows_up_to_limit(self) -> None:
        limiter = RateLimiter(InMemoryCache(), limit_per_minute=3)

        decisions = [limiter.check("client", now=1000.0) for _ in range(3)]

        assert all(decision.allowed for decision in decisions)
        assert [decision.remaining for decision in decisions] == [2, 1, 0]

    def test_blocks_over_limit(self) -> None:
        limiter = RateLimiter(InMemoryCache(), limit_per_minute=2)
        limiter.check("client", now=1000.0)
        limiter.check("client", now=1000.0)

        decision = limiter.check("client", now=1000.0)

        assert decision.allowed is False
        assert decision.remaining == 0
        assert decision.retry_after_seconds > 0

    def test_window_resets(self) -> None:
        """固定窗口到期后计数归零，请求重新放行。"""
        limiter = RateLimiter(InMemoryCache(), limit_per_minute=1)
        assert limiter.check("client", now=1000.0).allowed is True
        assert limiter.check("client", now=1000.0).allowed is False

        assert limiter.check("client", now=1061.0).allowed is True

    def test_keys_are_isolated(self) -> None:
        """不同客户端互不影响——否则一个用户的突发会拖垮所有人。"""
        limiter = RateLimiter(InMemoryCache(), limit_per_minute=1)
        limiter.check("alice", now=1000.0)

        assert limiter.check("bob", now=1000.0).allowed is True
        assert limiter.check("alice", now=1000.0).allowed is False

    def test_zero_limit_disables_throttling(self) -> None:
        """限额 <= 0 视为关闭限流（测试环境常用）。"""
        limiter = RateLimiter(InMemoryCache(), limit_per_minute=0)

        for _ in range(50):
            assert limiter.check("client", now=1000.0).allowed is True

    def test_retry_after_shrinks_toward_window_end(self) -> None:
        """窗口内越晚被拒，等待时间越短。

        注意必须取**同一窗口内**的时间戳：固定窗口的起点是 ``floor(now / 60) * 60``，
        跨过窗口边界后计数会重置，判定结果完全不同。
        """
        limiter = RateLimiter(InMemoryCache(), limit_per_minute=1)
        limiter.check("client", now=965.0)  # 建立窗口 [960, 1020)

        early = limiter.check("client", now=970.0)
        late = limiter.check("client", now=1015.0)

        assert early.allowed is False and late.allowed is False
        assert early.retry_after_seconds == 50
        assert late.retry_after_seconds == 5
        assert early.retry_after_seconds > late.retry_after_seconds

    def test_retry_after_is_never_negative(self) -> None:
        """``Retry-After`` 必须为正数。

        边界情形：窗口末尾被拒时，``window_end - now`` 可能算出 0 或负值。
        用 ``or 1`` 兜底是错的——Python 中负数也是真值，负的 ``Retry-After``
        会被客户端解析成「立即可重试」，反而放大请求量。
        """
        limiter = RateLimiter(InMemoryCache(), limit_per_minute=1)
        limiter.check("client", now=1_019.999)

        decision = limiter.check("client", now=1_019.999)

        assert decision.allowed is False
        assert decision.retry_after_seconds >= 1


class TestRateLimitOverHttp:
    """通过真实接口验证限流生效。

    **注意**：``/healthz`` 是豁免路径（否则编排系统的存活探针会被限流挡住，
    误判实例不健康并触发重启风暴）。因此本类的用例一律使用业务接口
    ``/api/v1/tax/records`` 来消耗配额——它对未认证请求返回 401，
    但限流计数发生在中间件层，401 同样计入配额。
    """

    COUNTED_PATH = "/api/v1/tax/records"
    LIMIT = 5

    @pytest.fixture
    def throttled_app(self, app_factory) -> FastAPI:
        return app_factory(rate_limit_per_minute=self.LIMIT)

    @pytest.fixture
    def throttled_api(self, throttled_app: FastAPI) -> ApiClient:
        return ApiClient(throttled_app.state.test_client, timeout=None)

    def _hit(self, api: ApiClient):
        """访问一个会计入限流的接口。"""
        return api.get(self.COUNTED_PATH, authenticated=False)

    def test_allows_requests_under_limit(self, throttled_api: ApiClient) -> None:
        statuses = [self._hit(throttled_api).status_code for _ in range(self.LIMIT)]

        assert set(statuses) == {401}, "配额内的请求应正常到达业务层（未认证返回 401）"

    def test_blocks_request_over_limit(self, throttled_api: ApiClient) -> None:
        for _ in range(self.LIMIT):
            self._hit(throttled_api)

        response = self._hit(throttled_api)

        assert response.status_code == 429
        assert "频繁" in response.detail()

    def test_returns_retry_after_header(self, throttled_api: ApiClient) -> None:
        """429 必须带 ``Retry-After``，否则调用方只能盲目重试。"""
        for _ in range(self.LIMIT + 1):
            response = self._hit(throttled_api)

        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) >= 1

    def test_exposes_remaining_quota(self, throttled_api: ApiClient) -> None:
        """正常响应应回传剩余额度，便于调用方自适应降速。"""
        response = self._hit(throttled_api)

        assert response.headers["X-RateLimit-Limit"] == str(self.LIMIT)
        assert int(response.headers["X-RateLimit-Remaining"]) == self.LIMIT - 1

    def test_quota_decreases_monotonically(self, throttled_api: ApiClient) -> None:
        remaining = [
            int(self._hit(throttled_api).headers["X-RateLimit-Remaining"])
            for _ in range(self.LIMIT)
        ]

        assert remaining == list(range(self.LIMIT - 1, -1, -1))

    def test_health_endpoint_is_exempt(self, throttled_api: ApiClient) -> None:
        """健康检查不受限流影响——这是限流实现里最常见的自伤场景。"""
        for _ in range(self.LIMIT + 5):
            self._hit(throttled_api)

        statuses = [throttled_api.health().status_code for _ in range(20)]

        assert set(statuses) == {200}

    def test_exempt_paths_do_not_consume_quota(self, throttled_api: ApiClient) -> None:
        """豁免路径既不限流也不消耗配额。"""
        for _ in range(20):
            throttled_api.health()

        response = self._hit(throttled_api)

        assert response.status_code == 401
        assert int(response.headers["X-RateLimit-Remaining"]) == self.LIMIT - 1

    def test_documentation_paths_are_exempt(self, throttled_api: ApiClient) -> None:
        """API 文档页面不应被限流挡住，否则运维排查会很麻烦。"""
        for _ in range(self.LIMIT + 1):
            self._hit(throttled_api)

        assert throttled_api.get("/openapi.json", authenticated=False).status_code == 200
