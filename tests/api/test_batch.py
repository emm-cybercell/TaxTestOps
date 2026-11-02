"""接口层 · 批量申报。"""

from __future__ import annotations

from typing import Any

import pytest

from support.api_client import ApiClient
from support.factories import person_payload
from support.marks import defect
from tax_service.core.oracle import TaxOracle
from tax_service.core.person import Person

pytestmark = [pytest.mark.api, pytest.mark.regression]


def batch_item(salary: int, external_ref: str | None = None, **person: Any) -> dict[str, Any]:
    """构造批量申报的一项。"""
    item: dict[str, Any] = {"salary": salary, "person": person_payload(**person)}
    if external_ref is not None:
        item["external_ref"] = external_ref
    return item


class TestBatchHappyPath:
    """批量申报的正常路径。"""

    def test_processes_all_items(self, authed_api: ApiClient) -> None:
        response = authed_api.batch(
            [batch_item(10_000, "A001"), batch_item(20_000, "A002"), batch_item(30_000, "A003")]
        )

        body = authed_api.expect(response, 200)

        assert body["total"] == 3
        assert body["succeeded"] == 3
        assert body["failed"] == 0
        assert [item["ok"] for item in body["items"]] == [True, True, True]

    def test_preserves_item_order_and_index(self, authed_api: ApiClient) -> None:
        """结果必须按请求顺序返回并带原始下标——调用方靠 index 与 external_ref 对账。"""
        items = [batch_item(10_000, f"A{i:03d}") for i in range(5)]

        body = authed_api.expect(authed_api.batch(items), 200)

        assert [item["index"] for item in body["items"]] == [0, 1, 2, 3, 4]
        assert [item["external_ref"] for item in body["items"]] == [f"A{i:03d}" for i in range(5)]

    def test_persists_every_item(self, authed_api: ApiClient) -> None:
        authed_api.batch([batch_item(10_000), batch_item(20_000)])

        page = authed_api.expect(authed_api.records(), 200)

        assert page["total"] == 2
        assert {record["salary"] for record in page["items"]} == {10_000, 20_000}

    def test_single_item_batch_is_allowed(self, authed_api: ApiClient) -> None:
        """批量接口应容忍单项批次，避免调用方为「只有一条」写两条分支。"""
        body = authed_api.expect(authed_api.batch([batch_item(20_000)]), 200)

        assert body["total"] == 1
        assert body["succeeded"] == 1

    def test_external_ref_is_optional(self, authed_api: ApiClient) -> None:
        body = authed_api.expect(authed_api.batch([batch_item(20_000)]), 200)

        assert body["items"][0]["external_ref"] is None


class TestBatchPartialFailure:
    """逐项独立事务：一条脏数据不应让整批回滚。"""

    def test_duplicate_external_ref_fails_only_that_item(self, authed_api: ApiClient) -> None:
        """批内重复的外部单号：只判失败该项，其余照常处理。"""
        body = authed_api.expect(
            authed_api.batch(
                [batch_item(10_000, "A001"), batch_item(20_000, "A002"), batch_item(30_000, "A002")]
            ),
            200,
        )

        assert body["total"] == 3
        assert body["succeeded"] == 2
        assert body["failed"] == 1

        failed = [item for item in body["items"] if not item["ok"]]
        assert len(failed) == 1
        assert failed[0]["index"] == 2
        assert "重复" in failed[0]["error"]
        assert failed[0]["record_id"] is None

    def test_successful_items_are_still_persisted(self, authed_api: ApiClient) -> None:
        """部分失败时，成功项必须已落库——这正是「逐项独立事务」的含义。"""
        authed_api.batch([batch_item(10_000, "A001"), batch_item(10_000, "A001")])

        page = authed_api.expect(authed_api.records(), 200)

        assert page["total"] == 1

    def test_calculator_failure_is_isolated(self, authed_api: ApiClient, app) -> None:
        """计算器抛异常时，该项被标记失败，整批仍返回 200。

        用注入失败计算器的方式构造该场景——这本身说明「依赖可替换」是架构设计的要求：
        如果服务层硬编码具体实现，这类异常路径根本无法测试。
        """

        class ExplodingCalculator:
            def cal(self, person, salary):  # noqa: ANN001, ANN201
                raise RuntimeError(f"模拟计算器故障：salary={salary}")

        original = app.state.tax_service.calculator
        app.state.tax_service.calculator = ExplodingCalculator()
        try:
            body = authed_api.expect(
                authed_api.batch([batch_item(10_000), batch_item(20_000)]), 200
            )
        finally:
            app.state.tax_service.calculator = original

        assert body["succeeded"] == 0
        assert body["failed"] == 2
        assert all("模拟计算器故障" in item["error"] for item in body["items"])


class TestBatchLimits:
    """批量规模约束。"""

    def test_rejects_empty_items(self, authed_api: ApiClient) -> None:
        assert authed_api.batch([]).status_code == 422

    def test_rejects_oversized_batch(self, authed_api: ApiClient, app) -> None:
        """超过上限必须显式拒绝，而不是静默截断——静默截断会让调用方以为全部处理了。"""
        limit = app.state.settings.batch_max_size
        items = [batch_item(10_000) for _ in range(limit + 1)]

        response = authed_api.batch(items)

        assert response.status_code == 422
        assert str(limit) in response.detail()

    def test_accepts_batch_at_exact_limit(self, authed_api: ApiClient, app) -> None:
        """边界值：恰好等于上限应当通过。"""
        limit = app.state.settings.batch_max_size
        items = [batch_item(10_000) for _ in range(limit)]

        body = authed_api.expect(authed_api.batch(items), 200)

        assert body["total"] == limit
        assert body["succeeded"] == limit

    def test_rejects_invalid_item_payload(self, authed_api: ApiClient) -> None:
        """批内任一项格式非法时整批拒绝（格式错误在进入业务逻辑前就该拦下）。"""
        response = authed_api.batch([batch_item(10_000), {"salary": -1}])

        assert response.status_code == 422


class TestBatchIdempotency:
    """批量申报的幂等语义。"""

    def test_same_key_replays_without_duplicates(self, authed_api: ApiClient) -> None:
        """同一幂等键重复提交整批，不产生新记录。"""
        items = [batch_item(10_000, "A001"), batch_item(20_000, "A002")]

        first = authed_api.expect(authed_api.batch(items, idempotency_key="batch-1"), 200)
        second = authed_api.expect(authed_api.batch(items, idempotency_key="batch-1"), 200)

        assert [item["record_id"] for item in first["items"]] == [
            item["record_id"] for item in second["items"]
        ]

        page = authed_api.expect(authed_api.records(), 200)
        assert page["total"] == 2

    def test_different_keys_create_new_records(self, authed_api: ApiClient) -> None:
        items = [batch_item(10_000, "A001")]

        authed_api.batch(items, idempotency_key="batch-1")
        authed_api.batch(items, idempotency_key="batch-2")

        page = authed_api.expect(authed_api.records(), 200)
        assert page["total"] == 2

    def test_without_key_repeated_submission_duplicates(self, authed_api: ApiClient) -> None:
        """未提供幂等键时不做去重——这是明确的契约，避免调用方误以为默认幂等。"""
        items = [batch_item(10_000)]

        authed_api.batch(items)
        authed_api.batch(items)

        page = authed_api.expect(authed_api.records(), 200)
        assert page["total"] == 2


class TestBatchCalculationConsistency:
    """批量与单次接口必须给出一致结果。"""

    @defect("D1", "税率阶梯第 3 级起速算扣除数少计 1300 元")
    def test_matches_single_calculate(self, authed_api: ApiClient) -> None:
        """同一输入在批量与单次接口下的税额必须相同。

        这是一条**跨接口一致性断言**：它不依赖外部期望值，靠两个接口互相校验，
        能发现「两条代码路径用了不同实现」这类隐蔽缺陷。
        """
        payload = person_payload(children_num=1, house_loans=True)

        single = authed_api.expect(authed_api.calculate(30_000, person=payload, persist=False), 200)
        batch = authed_api.expect(authed_api.batch([batch_item(30_000, **payload)]), 200)

        assert batch["items"][0]["tax_amount"] == single["tax_amount"]
        assert single["tax_amount"] == TaxOracle().cal(Person(**payload), 30_000)
