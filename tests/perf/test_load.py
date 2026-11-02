"""性能层 · 负载与延迟基线。

断言什么
--------
性能用例的断言必须**可复现且有意义**，否则只会变成 CI 里的噪声源。这里断言三类：

1. **正确性不因并发而降级**——负载下错误率必须为 0（这是性能测试里最容易被忽略、
   也最致命的一类问题：吞吐上去了但结果错了）；
2. **延迟有上界**——P95 不超过阈值，且阈值取自本机实测后留出的余量；
3. **无并发缺陷**——同一幂等键在并发下仍只落一条记录。

绝对数值会随机器变化，因此阈值通过环境变量可调；默认值按开发机实测结果放宽，
目标是「能发现数量级退化」，而不是「精确比较」。

放大负载::

    TAX_PERF_REQUESTS=2000 TAX_PERF_WORKERS=32 python -m pytest tests/perf -q -s
"""

from __future__ import annotations

import os

import httpx
import pytest

from support.factories import calculate_payload
from support.load import DEFAULT_REQUESTS, DEFAULT_WORKERS, LoadResult, run_load
from support.servers import LiveServer

pytestmark = [pytest.mark.perf, pytest.mark.slow]

#: 各接口的 P95 延迟上限（毫秒）。按开发机实测值放宽约 5~10 倍，
#: 目的是发现数量级退化，而不是做精确基准。
P95_BUDGET_MS = {
    "health": float(os.getenv("TAX_PERF_P95_HEALTH_MS", "150")),
    "calculate": float(os.getenv("TAX_PERF_P95_CALCULATE_MS", "800")),
    "records": float(os.getenv("TAX_PERF_P95_RECORDS_MS", "600")),
    "mixed": float(os.getenv("TAX_PERF_P95_MIXED_MS", "900")),
}


def report(result: LoadResult, label: str) -> None:
    """打印一行可读的负载摘要，便于人工比对历史数据。"""
    print(f"\n[perf] {label}：{result.summary()}")


class TestReadPathPerformance:
    """只读路径（健康检查、记录查询）。"""

    def test_health_under_load(self, perf_server: LiveServer) -> None:
        result = run_load(
            base_url=perf_server.base_url,
            request_factory=lambda client, _index: client.get("/healthz"),
            requests_count=DEFAULT_REQUESTS,
            workers=DEFAULT_WORKERS,
        )
        report(result, "GET /healthz")

        assert result.error_rate == 0, f"只读路径出现错误：{result.errors[:3]}"
        assert result.p95 < P95_BUDGET_MS["health"], result.summary()

    def test_records_under_load(self, perf_server: LiveServer, token_pool: list[str]) -> None:
        result = run_load(
            base_url=perf_server.base_url,
            request_factory=lambda client, _index: client.get("/api/v1/tax/records"),
            requests_count=DEFAULT_REQUESTS,
            workers=DEFAULT_WORKERS,
            tokens=token_pool,
        )
        report(result, "GET /api/v1/tax/records")

        assert result.error_rate == 0, f"查询路径出现错误：{result.errors[:3]}"
        assert result.p95 < P95_BUDGET_MS["records"], result.summary()


class TestWritePathPerformance:
    """写入路径（测算并落库）——SQLite 写锁是主要瓶颈。"""

    def test_calculate_under_load(self, perf_server: LiveServer, token_pool: list[str]) -> None:
        def factory(client: httpx.Client, index: int):
            # 每次用不同幂等键，避免压测被幂等去重「优化」掉
            return client.post(
                "/api/v1/tax/calculate",
                json=calculate_payload(20_000 + index % 1_000, idempotency_key=f"perf-{index}"),
            )

        result = run_load(
            base_url=perf_server.base_url,
            request_factory=factory,
            requests_count=DEFAULT_REQUESTS,
            workers=DEFAULT_WORKERS,
            tokens=token_pool,
        )
        report(result, "POST /api/v1/tax/calculate")

        assert result.error_rate == 0, f"写入路径出现错误：{result.errors[:3]}"
        assert result.p95 < P95_BUDGET_MS["calculate"], result.summary()

    def test_all_writes_are_persisted(self, perf_server: LiveServer, token_pool: list[str]) -> None:
        """并发写入的正确性断言：落库条数必须等于请求数。

        这是性能测试里最重要的断言——高吞吐但丢数据的服务毫无价值，
        而「丢数据」恰恰是并发场景最典型的缺陷。

        注意要**跨全部用户求和**：``run_load`` 把请求分摊到令牌池中的每个用户，
        只查其中一个用户会得到 1/N 的条数（并误判为数据丢失）。
        """
        count = min(DEFAULT_REQUESTS, 60)

        def factory(client: httpx.Client, index: int):
            return client.post(
                "/api/v1/tax/calculate",
                json=calculate_payload(30_000, idempotency_key=f"persist-{index}"),
            )

        result = run_load(
            base_url=perf_server.base_url,
            request_factory=factory,
            requests_count=count,
            workers=DEFAULT_WORKERS,
            tokens=token_pool,
        )
        assert result.error_rate == 0, result.summary()

        with httpx.Client(base_url=perf_server.base_url, timeout=30.0) as client:
            totals = [
                client.get(
                    "/api/v1/tax/records",
                    headers={"Authorization": f"Bearer {token}"},
                    params={"limit": 1},
                ).json()["total"]
                for token in token_pool
            ]

        assert sum(totals) == count, (
            f"并发写入 {count} 条，各用户落库合计 {sum(totals)} 条（分布 {totals}）——存在数据丢失"
        )


class TestMixedWorkload:
    """混合负载：更接近真实流量构成。"""

    def test_mixed_traffic(self, perf_server: LiveServer, token_pool: list[str]) -> None:
        """按 4:4:1:1 的比例混合读、写、鉴权、健康检查。"""

        def factory(client: httpx.Client, index: int):
            bucket = index % 10
            if bucket < 4:
                return client.post(
                    "/api/v1/tax/calculate",
                    json=calculate_payload(20_000, idempotency_key=f"mixed-{index}"),
                )
            if bucket < 8:
                return client.get("/api/v1/tax/records")
            if bucket < 9:
                return client.get("/api/v1/auth/me")
            return client.get("/healthz")

        result = run_load(
            base_url=perf_server.base_url,
            request_factory=factory,
            requests_count=DEFAULT_REQUESTS,
            workers=DEFAULT_WORKERS,
            tokens=token_pool,
        )
        report(result, "混合负载 4:4:1:1")

        assert result.error_rate == 0, f"混合负载出现错误：{result.errors[:3]}"
        assert result.p95 < P95_BUDGET_MS["mixed"], result.summary()


class TestConcurrencyCorrectness:
    """并发下的正确性——性能测试中优先级最高的一类断言。"""

    def test_same_idempotency_key_under_load(
        self, perf_server: LiveServer, token_pool: list[str]
    ) -> None:
        """并发提交同一幂等键，只应落一条记录且全部返回 200。

        这是「吞吐与正确性同时成立」的联合断言：如果为了性能去掉唯一约束，
        这条用例会立刻失败。
        """

        def factory(client: httpx.Client, _index: int):
            return client.post(
                "/api/v1/tax/calculate",
                json=calculate_payload(20_000, idempotency_key="perf-shared-key"),
            )

        count = min(DEFAULT_REQUESTS, 40)
        result = run_load(
            base_url=perf_server.base_url,
            request_factory=factory,
            requests_count=count,
            workers=DEFAULT_WORKERS,
            tokens=token_pool[:1],
        )

        assert result.error_rate == 0, f"并发幂等提交出现错误：{result.errors[:3]}"
        assert set(result.status_codes) == {200}, (
            f"出现非 200 响应：{sorted(set(result.status_codes))}"
        )

        with httpx.Client(base_url=perf_server.base_url, timeout=30.0) as client:
            headers = {"Authorization": f"Bearer {token_pool[0]}"}
            page = client.get("/api/v1/tax/records", headers=headers).json()

        assert page["total"] == 1, f"并发提交同一幂等键产生了 {page['total']} 条记录"

    def test_user_isolation_under_load(
        self, perf_server: LiveServer, token_pool: list[str]
    ) -> None:
        """多用户并发时，各用户只能看到自己的数据。

        并发 + 多租户是最容易出数据串号的组合，因此单独断言。
        """
        if len(token_pool) < 2:
            pytest.skip("需要至少 2 个用户令牌")

        def factory(client: httpx.Client, index: int):
            return client.post(
                "/api/v1/tax/calculate",
                json=calculate_payload(20_000, idempotency_key=f"iso-{index}"),
            )

        count = min(DEFAULT_REQUESTS, 40)
        result = run_load(
            base_url=perf_server.base_url,
            request_factory=factory,
            requests_count=count,
            workers=DEFAULT_WORKERS,
            tokens=token_pool,
        )
        assert result.error_rate == 0, result.summary()

        with httpx.Client(base_url=perf_server.base_url, timeout=30.0) as client:
            totals = []
            for token in token_pool:
                headers = {"Authorization": f"Bearer {token}"}
                totals.append(client.get("/api/v1/tax/records", headers=headers).json()["total"])

        assert sum(totals) == count, f"各用户记录数之和 {sum(totals)} 与请求数 {count} 不符"
        assert all(total > 0 for total in totals), f"存在用户完全没有收到数据：{totals}"
