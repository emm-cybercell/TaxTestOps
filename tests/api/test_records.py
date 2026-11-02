"""接口层 · 申报记录查询与数据隔离。"""

from __future__ import annotations

import pytest

from support.api_client import ApiClient

pytestmark = [pytest.mark.api, pytest.mark.regression]


class TestRecordListing:
    """分页查询。"""

    def test_empty_for_new_user(self, authed_api: ApiClient) -> None:
        body = authed_api.expect(authed_api.records(), 200)

        assert body == {"total": 0, "limit": 20, "offset": 0, "items": []}

    def test_returns_created_records(self, authed_api: ApiClient) -> None:
        for salary in (10_000, 20_000, 30_000):
            authed_api.calculate(salary)

        body = authed_api.expect(authed_api.records(), 200)

        assert body["total"] == 3
        assert {item["salary"] for item in body["items"]} == {10_000, 20_000, 30_000}

    def test_sorted_by_id_descending(self, authed_api: ApiClient) -> None:
        """默认按时间倒序——用户最关心最近一次申报。"""
        for salary in (10_000, 20_000, 30_000):
            authed_api.calculate(salary)

        body = authed_api.expect(authed_api.records(), 200)
        ids = [item["id"] for item in body["items"]]

        assert ids == sorted(ids, reverse=True)

    def test_pagination_is_stable(self, authed_api: ApiClient) -> None:
        """分页不得重复或遗漏：逐页取回后并集应恰好等于全量。"""
        for salary in range(10_000, 10_000 + 7 * 1_000, 1_000):
            authed_api.calculate(salary)

        first = authed_api.expect(authed_api.records(limit=3, offset=0), 200)
        second = authed_api.expect(authed_api.records(limit=3, offset=3), 200)
        third = authed_api.expect(authed_api.records(limit=3, offset=6), 200)

        collected = [item["id"] for page in (first, second, third) for item in page["items"]]

        assert len(collected) == 7
        assert len(set(collected)) == 7, "分页出现重复记录"
        assert first["total"] == second["total"] == third["total"] == 7

    def test_offset_beyond_total_returns_empty(self, authed_api: ApiClient) -> None:
        authed_api.calculate(20_000)

        body = authed_api.expect(authed_api.records(limit=10, offset=100), 200)

        assert body["items"] == []
        assert body["total"] == 1

    @pytest.mark.parametrize(
        ("limit", "offset"),
        [
            pytest.param(0, 0, id="limit 为 0"),
            pytest.param(-1, 0, id="limit 为负"),
            pytest.param(101, 0, id="limit 超上限"),
            pytest.param(10, -1, id="offset 为负"),
        ],
    )
    def test_rejects_invalid_pagination(
        self, authed_api: ApiClient, limit: int, offset: int
    ) -> None:
        """分页参数必须设上限，否则一次请求可能拖垮服务。"""
        response = authed_api.records(limit=limit, offset=offset)

        assert response.status_code == 422

    def test_limit_upper_bound_is_inclusive(self, authed_api: ApiClient) -> None:
        assert authed_api.records(limit=100).status_code == 200


class TestRecordDetail:
    """单条查询。"""

    def test_returns_own_record(self, authed_api: ApiClient) -> None:
        created = authed_api.expect(authed_api.calculate(20_000), 200)

        body = authed_api.expect(authed_api.record(created["record_id"]), 200)

        assert body["id"] == created["record_id"]
        assert body["salary"] == 20_000
        assert body["tax_amount"] == created["tax_amount"]

    def test_detail_matches_list_item(self, authed_api: ApiClient) -> None:
        """列表项与详情必须一致，避免调用方看到两份不同的数据。"""
        created = authed_api.expect(authed_api.calculate(20_000), 200)
        listed = authed_api.expect(authed_api.records(), 200)["items"][0]

        detail = authed_api.expect(authed_api.record(created["record_id"]), 200)

        assert detail == listed

    def test_unknown_id_returns_404(self, authed_api: ApiClient) -> None:
        assert authed_api.record(999_999).status_code == 404

    def test_rejects_non_integer_id(self, authed_api: ApiClient) -> None:
        assert authed_api.get("/api/v1/tax/records/abc").status_code == 422


class TestDataIsolation:
    """多用户数据隔离——这是安全底线，也是并发场景下的数据正确性基础。"""

    def test_user_cannot_see_others_records(
        self, authed_api: ApiClient, other_authed_api: ApiClient
    ) -> None:
        authed_api.calculate(20_000)
        authed_api.calculate(30_000)

        body = other_authed_api.expect(other_authed_api.records(), 200)

        assert body["total"] == 0
        assert body["items"] == []

    def test_user_cannot_read_others_record_by_id(
        self, authed_api: ApiClient, other_authed_api: ApiClient
    ) -> None:
        """越权访问返回 404 而不是 403。

        403 会泄露「该 ID 确实存在」这一信息，攻击者可据此枚举有效 ID；
        404 让「不存在」与「不属于你」无法区分。
        """
        created = authed_api.expect(authed_api.calculate(20_000), 200)

        response = other_authed_api.record(created["record_id"])

        assert response.status_code == 404

    def test_total_only_counts_own_records(
        self, authed_api: ApiClient, other_authed_api: ApiClient
    ) -> None:
        authed_api.calculate(20_000)
        other_authed_api.calculate(30_000)
        other_authed_api.calculate(40_000)

        assert authed_api.expect(authed_api.records(), 200)["total"] == 1
        assert other_authed_api.expect(other_authed_api.records(), 200)["total"] == 2

    def test_idempotency_key_is_scoped_per_user(
        self, authed_api: ApiClient, other_authed_api: ApiClient
    ) -> None:
        """幂等键按用户隔离：两个用户用同一个键互不干扰。"""
        first = authed_api.expect(authed_api.calculate(20_000, idempotency_key="shared-key"), 200)
        second = other_authed_api.expect(
            other_authed_api.calculate(20_000, idempotency_key="shared-key"), 200
        )

        assert first["record_id"] != second["record_id"]
        assert other_authed_api.expect(other_authed_api.records(), 200)["total"] == 1


class TestRecordFields:
    """记录字段的完整性。"""

    @pytest.mark.parametrize(
        "person",
        [
            pytest.param(
                {
                    "children_num": 2,
                    "is_keep_stu": 1,
                    "house_loans": True,
                    "is_rent_house": 0,
                    "supporting_elderly": True,
                    "sibling_num": 1,
                },
                id="含住房贷款利息",
            ),
            pytest.param(
                {
                    "children_num": 1,
                    "is_keep_stu": 2,
                    "house_loans": False,
                    "is_rent_house": 3,
                    "supporting_elderly": False,
                    "sibling_num": 0,
                },
                id="含住房租金",
            ),
            pytest.param(
                {
                    "children_num": 0,
                    "is_keep_stu": 0,
                    "house_loans": False,
                    "is_rent_house": 0,
                    "supporting_elderly": True,
                    "sibling_num": 2,
                },
                id="仅赡养老人",
            ),
        ],
    )
    def test_echoes_input_parameters(self, authed_api: ApiClient, person: dict) -> None:
        """记录必须完整保存输入，否则历史记录无法复现计算结果。

        注意：住房贷款利息与住房租金**互斥**，因此用例数据必须分开放置。
        把两项同时置为生效会让请求本身非法——``standard`` 实现会返回 422，
        而 ``legacy`` 不校验，这个测试数据错误只在双模式运行时才暴露。
        """
        created = authed_api.expect(authed_api.calculate(30_000, person=person), 200)
        record = authed_api.expect(authed_api.record(created["record_id"]), 200)

        for key, value in person.items():
            assert record[key] == value, f"字段 {key} 未被正确保存"

    def test_stores_calculator_impl(self, authed_api: ApiClient, calculator_impl: str) -> None:
        """记录中保存实现标识，便于日后追溯「这条数据是哪个版本算出来的」。"""
        created = authed_api.expect(authed_api.calculate(20_000), 200)
        record = authed_api.expect(authed_api.record(created["record_id"]), 200)

        assert record["calculator_impl"] == calculator_impl
