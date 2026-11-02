"""接口层 · 单次个税测算。

本文件是「需求符合性」在接口层的落点。它同时回答两个问题：

1. **算得对不对**——期望值来自与实现无关的 :class:`~tax_service.core.oracle.TaxOracle`；
2. **缺陷能不能从接口观测到**——D1 与 D4 在 HTTP 层面可见，D3 与 D5 被边界校验挡住。
"""

from __future__ import annotations

from typing import Any

import pytest

from support.api_client import ApiClient
from support.factories import person_payload
from support.marks import defect
from tax_service.core.oracle import TaxOracle
from tax_service.core.person import Person
from tax_service.core.policy import BASIC_DEDUCTION

pytestmark = [pytest.mark.api, pytest.mark.regression]

ORACLE = TaxOracle()
"""期望值来源：按需求规格独立实现的参考计算器。"""


def to_person(payload: dict[str, Any]) -> Person:
    """把请求体里的 person 字典转成领域模型，供 Oracle 计算期望值。"""
    return Person(**payload)


# --------------------------------------------------------------------------- #
# 一、需求符合性（期望值来自独立 Oracle）
# --------------------------------------------------------------------------- #

#: 用例表：(用例名, person 载荷, 工资, 已知缺陷编号或 None)
CONFORMANCE_CASES: list[tuple[str, dict[str, Any], int, str | None]] = [
    ("工资低于免征额", person_payload(), 4_000, None),
    ("应纳税所得额为零", person_payload(), BASIC_DEDUCTION, None),
    ("第 1 级 3%", person_payload(), 7_000, None),
    ("第 2 级 10%", person_payload(), 10_000, None),
    ("第 2 级上界内侧", person_payload(), 16_999, None),
    ("第 2/3 级分界", person_payload(), 17_000, "D1"),
    ("第 3 级 20%", person_payload(), 20_000, "D1"),
    ("第 4 级 25%", person_payload(), 35_000, "D1"),
    ("第 5 级 30%", person_payload(), 45_000, "D1"),
    ("第 6 级 35%", person_payload(), 65_000, "D1"),
    ("第 7 级 45%", person_payload(), 105_000, "D1"),
    ("子女教育", person_payload(children_num=2), 20_000, "D1"),
    ("继续教育·学历期间", person_payload(is_keep_stu=1), 20_000, "D1"),
    ("继续教育·取得证书当年", person_payload(is_keep_stu=2), 20_000, "D4"),
    ("住房贷款利息", person_payload(house_loans=True), 20_000, "D1"),
    ("住房租金 1 档", person_payload(is_rent_house=1), 20_000, "D1"),
    ("住房租金 2 档", person_payload(is_rent_house=2), 20_000, "D1"),
    ("住房租金 3 档", person_payload(is_rent_house=3), 20_000, "D1"),
    ("赡养老人·独生子女", person_payload(supporting_elderly=True), 20_000, "D1"),
    ("赡养老人·1 名兄弟姐妹", person_payload(supporting_elderly=True, sibling_num=1), 20_000, "D1"),
    (
        "需求样例·小李",
        person_payload(children_num=1, is_rent_house=1, supporting_elderly=True, sibling_num=1),
        8_000,
        None,
    ),
    (
        "多扣除叠加",
        person_payload(
            children_num=2,
            is_keep_stu=1,
            house_loans=True,
            is_rent_house=0,
            supporting_elderly=True,
            sibling_num=1,
        ),
        20_000,
        # 扣除合计 4400 元 → 应纳税所得额 10600 元 < 12000，落在第 2 级，不触发 D1
        None,
    ),
]

DEFECT_SUMMARY = {
    "D1": "税率阶梯第 3 级起速算扣除数少计 1300 元",
    "D4": "继续教育 3600 元年度定额被按月扣除",
}


def _conformance_param(name: str, person: dict[str, Any], salary: int, defect_id: str | None):
    marks = [defect(defect_id, DEFECT_SUMMARY[defect_id])] if defect_id else []
    return pytest.param(person, salary, defect_id, id=name, marks=marks)


class TestRequirementConformance:
    """接口返回的税额必须与需求规格一致。"""

    @pytest.mark.parametrize(
        ("person", "salary", "defect_id"),
        [_conformance_param(*case) for case in CONFORMANCE_CASES],
    )
    def test_tax_amount_matches_requirement(
        self,
        authed_api: ApiClient,
        person: dict[str, Any],
        salary: int,
        defect_id: str | None,
    ) -> None:
        """逐场景比对接口输出与需求期望值。

        标记为缺陷的用例在 ``legacy`` 模式下 ``xfail``；切到 ``standard`` 实现后
        必须全部通过——这同时是缺陷回归护栏与修复验证。
        """
        expected = ORACLE.cal(to_person(person), salary)

        body = authed_api.expect(authed_api.calculate(salary, person=person), 200)

        assert body["tax_amount"] == expected, (
            f"工资 {salary} 元、扣除 {person} 的应纳税额应为 {expected}，"
            f"实际 {body['tax_amount']}（缺陷 {defect_id or '无'}）"
        )

    def test_spec_example_without_deductions(self, authed_api: ApiClient) -> None:
        """需求原文样例：(10000 − 5000 − 2000) × 3% = 90 元。"""
        body = authed_api.expect(authed_api.calculate(8_000), 200)

        assert body["tax_amount"] == 90

    def test_spec_example_xiao_li(self, authed_api: ApiClient) -> None:
        """需求原文样例：小李各项扣除叠加后应纳税额为 0。"""
        person = person_payload(
            children_num=1,
            is_rent_house=1,
            supporting_elderly=True,
            sibling_num=1,
        )

        body = authed_api.expect(authed_api.calculate(8_000, person=person), 200)

        assert body["tax_amount"] == 0

    def test_taxable_income_follows_requirement(self, authed_api: ApiClient) -> None:
        """应纳税所得额采用需求口径：工资 − 免征额 − 专项附加扣除。

        该字段**不受** D1 影响（D1 只错在税额计算的速算扣除数），因此可以直接断言。
        """
        person = person_payload(children_num=2, house_loans=True)

        body = authed_api.expect(authed_api.calculate(20_000, person=person), 200)

        assert body["taxable_income"] == 20_000 - BASIC_DEDUCTION - 2_000 - 1_000


class TestDefectObservability:
    """哪些缺陷能从接口观测到，哪些被边界校验挡住。"""

    def test_d1_is_observable_through_api(self, authed_api: ApiClient) -> None:
        """D1：应纳税所得额 ≥ 12000 元时，接口返回的税额比需求多 1300 元。

        这是一个**接口级**的实证——缺陷不只是单元层的问题，它会一路穿透到 HTTP 响应，
        最终体现在用户的申报单上。
        """
        salary = 20_000
        expected = ORACLE.cal(Person(), salary)

        body = authed_api.expect(authed_api.calculate(salary), 200)

        delta = body["tax_amount"] - expected
        if delta == 0:
            # standard 模式：缺陷已修复
            return

        assert delta == 1_300, f"偏差应为恒定的 1300 元，实际 {delta}"
        assert body["taxable_income"] >= 12_000, "缺陷仅在应纳税所得额 ≥ 12000 元时出现"

    def test_d1_offset_is_constant_across_brackets(self, authed_api: ApiClient) -> None:
        """D1 在接口层的表现同样是**恒定偏移**，而非随机误差。

        遍历第 3~7 级，逐级比对偏差，全部为 1300 元说明根因是同一处常量。
        """
        offsets = set()
        for salary in (20_000, 35_000, 45_000, 65_000, 105_000):
            expected = ORACLE.cal(Person(), salary)
            actual = authed_api.expect(authed_api.calculate(salary, persist=False), 200)[
                "tax_amount"
            ]
            offsets.add(actual - expected)

        assert offsets <= {0, 1_300}, f"偏差集合异常：{sorted(offsets)}"

    @defect("D4", DEFECT_SUMMARY["D4"])
    def test_d4_is_observable_through_api(self, authed_api: ApiClient) -> None:
        """D4：继续教育 3600 元被当作月度项，接口层可观测。"""
        person = person_payload(is_keep_stu=2)
        expected = ORACLE.cal(Person(**person), 20_000)

        body = authed_api.expect(authed_api.calculate(20_000, person=person, persist=False), 200)

        assert body["tax_amount"] == expected, (
            f"取得证书当年应为 {expected} 元（年度定额，不计入月度），实际 {body['tax_amount']}"
        )

    @pytest.mark.parametrize(
        ("person", "defect_id"),
        [
            pytest.param(person_payload(is_keep_stu=9), "D5", id="D5-继续教育枚举越界"),
            pytest.param(person_payload(is_rent_house=9), "D5", id="D5-租金档次越界"),
            pytest.param(
                person_payload(supporting_elderly=True, sibling_num=-1),
                "D3",
                id="D3-兄弟姐妹数为负",
            ),
        ],
    )
    def test_unit_defects_are_masked_by_boundary_validation(
        self, authed_api: ApiClient, person: dict[str, Any], defect_id: str
    ) -> None:
        """D3 与 D5 在接口层**不可观测**——被 Pydantic 边界校验提前拒绝（422）。

        这是一个重要的分层结论：

        - 边界校验挡得住**输入格式缺陷**（D3 除零、D5 静默忽略）；
        - 挡不住**数值语义缺陷**（D1 常量错、D4 单位错），因为这类请求的输入完全合法。

        因此「接口层全绿」不能替代「单元层的需求验证」。
        """
        response = authed_api.calculate(20_000, person=person)

        assert response.status_code == 422, f"{defect_id} 应被边界校验拦下，实际放行"
        assert response.body is not None


# --------------------------------------------------------------------------- #
# 二、边界校验
# --------------------------------------------------------------------------- #


class TestInputValidation:
    """输入格式校验。"""

    @pytest.mark.parametrize(
        ("payload", "case"),
        [
            pytest.param({"salary": -1}, "工资为负", id="salary-negative"),
            pytest.param({"salary": 10_000_001}, "工资超过上限", id="salary-too-large"),
            pytest.param({"salary": "abc"}, "工资非数字", id="salary-not-a-number"),
            pytest.param({"person": {"children_num": -1}}, "子女个数为负", id="children-negative"),
            pytest.param(
                {"person": {"children_num": 21}}, "子女个数超上限", id="children-too-many"
            ),
            pytest.param({"person": {"sibling_num": -1}}, "兄弟姐妹数为负", id="sibling-negative"),
            pytest.param({"person": {"is_keep_stu": 9}}, "继续教育状态非法", id="keep-stu-invalid"),
            pytest.param({"person": {"is_rent_house": 9}}, "租金档次非法", id="rent-invalid"),
            pytest.param({"person": {"unknown_field": 1}}, "未知字段", id="unknown-person-field"),
            pytest.param({"unknown_field": 1}, "未知顶层字段", id="unknown-top-field"),
        ],
    )
    def test_rejects_invalid_payload(
        self, authed_api: ApiClient, payload: dict[str, Any], case: str
    ) -> None:
        body: dict[str, Any] = {"salary": 20_000}
        body.update(payload)

        response = authed_api.post("/api/v1/tax/calculate", json=body)

        assert response.status_code == 422, f"{case} 应被拒绝，实际 {response.status_code}"

    def test_rejects_missing_salary(self, authed_api: ApiClient) -> None:
        assert authed_api.post("/api/v1/tax/calculate", json={}).status_code == 422

    def test_rejects_empty_body(self, authed_api: ApiClient) -> None:
        assert authed_api.post("/api/v1/tax/calculate", json=None).status_code == 422

    def test_error_response_is_structured(self, authed_api: ApiClient) -> None:
        """422 响应必须给出可定位的字段路径，而不是笼统的「参数错误」。"""
        response = authed_api.calculate(20_000, person={"is_keep_stu": 9})

        assert response.status_code == 422
        detail = response.body["detail"]
        assert isinstance(detail, list) and detail, "错误详情应为列表"
        assert any(
            "is_keep_stu" in ".".join(str(part) for part in item.get("loc", [])) for item in detail
        )

    @defect("D2", "房贷与租金可同时扣除，需求规定二者互斥")
    def test_cross_field_business_rule_maps_to_422(self, authed_api: ApiClient) -> None:
        """跨字段业务约束（房贷与租金互斥）违反时必须是 422，不能是 500。

        这类约束在**领域层**校验（Pydantic 只管单字段格式），标准实现在互斥违规时
        抛 ``ValueError``——路由层若不接住，异常会穿透成 500，让调用方误判为服务端
        故障。该缺陷由 schemathesis 模糊测试在 standard 模式下发现：
        legacy 实现不校验互斥（缺陷 D2），因此只有双模式运行才能暴露它。
        """
        response = authed_api.calculate(
            20_000, person={"house_loans": True, "is_rent_house": 1}, persist=False
        )

        assert response.status_code == 422, (
            f"互斥违规应返回 422，实际 {response.status_code}：{response.body!r}"
        )
        if isinstance(response.body, dict) and isinstance(response.body.get("detail"), str):
            assert "不可同时扣除" in response.body["detail"]

    def test_cross_field_rule_is_accepted_when_satisfied(self, authed_api: ApiClient) -> None:
        """满足互斥约束的请求必须正常通过——防止上面的修复把合法请求一并拦下。"""
        ok_loan = authed_api.calculate(
            20_000, person={"house_loans": True, "is_rent_house": 0}, persist=False
        )
        ok_rent = authed_api.calculate(
            20_000, person={"house_loans": False, "is_rent_house": 1}, persist=False
        )

        assert ok_loan.status_code == 200
        assert ok_rent.status_code == 200


# --------------------------------------------------------------------------- #
# 三、响应契约
# --------------------------------------------------------------------------- #


class TestResponseContract:
    """响应结构与字段语义。"""

    def test_returns_all_declared_fields(self, authed_api: ApiClient) -> None:
        body = authed_api.expect(authed_api.calculate(20_000), 200)

        expected = {
            "record_id",
            "taxable_income",
            "tax_amount",
            "calculator_impl",
            "deductions",
            "created_at",
        }
        assert expected <= set(body)

    def test_numeric_fields_are_integers(self, authed_api: ApiClient) -> None:
        """金额必须是整数（元），不能返回浮点数——避免前端与对账出现分位误差。"""
        body = authed_api.expect(authed_api.calculate(20_000), 200)

        assert isinstance(body["tax_amount"], int)
        assert isinstance(body["taxable_income"], int)

    def test_reports_calculator_impl(self, authed_api: ApiClient, calculator_impl: str) -> None:
        """响应必须标明是哪个实现算出的结果，便于灰度与问题定位。"""
        body = authed_api.expect(authed_api.calculate(20_000), 200)

        assert body["calculator_impl"] == calculator_impl

    def test_deductions_are_itemized(self, authed_api: ApiClient) -> None:
        """扣除必须逐项返回，便于用户核对与对账。

        分两次请求覆盖全部五类扣除：住房贷款利息与住房租金**互斥**，不能放在同一次
        请求里——同时置为生效会让请求本身非法（``standard`` 实现返回 422）。
        """
        with_loan = person_payload(
            children_num=2,
            is_keep_stu=1,
            house_loans=True,
            is_rent_house=0,
            supporting_elderly=True,
            sibling_num=1,
        )
        with_rent = person_payload(is_rent_house=1)

        loan_items = {
            item["code"]: item
            for item in authed_api.expect(
                authed_api.calculate(30_000, person=with_loan), 200
            )["deductions"]
        }
        rent_items = {
            item["code"]: item
            for item in authed_api.expect(
                authed_api.calculate(30_000, person=with_rent), 200
            )["deductions"]
        }

        assert loan_items["child_education"]["amount"] == 2_000
        assert loan_items["continuing_education"]["amount"] == 400
        assert loan_items["housing_loan"]["amount"] == 1_000
        assert loan_items["elderly_care"]["amount"] == 1_000
        assert rent_items["housing_rent"]["amount"] == 1_500

    def test_no_deductions_returns_empty_list(self, authed_api: ApiClient) -> None:
        """无任何扣除时返回空列表，而不是 ``null``——避免调用方多做一次判空。"""
        body = authed_api.expect(authed_api.calculate(20_000), 200)

        assert body["deductions"] == []

    def test_deductions_sum_matches_taxable_income(self, authed_api: ApiClient) -> None:
        """明细合计必须与应纳税所得额自洽：工资 − 免征额 − 明细合计 = 应纳税所得额。

        这是一条**内部一致性断言**——它不依赖外部期望值，仅靠响应自身就能发现
        「明细与实际参与计算的扣除不一致」这类缺陷。
        """
        person = person_payload(children_num=1, house_loans=True, supporting_elderly=True)
        salary = 30_000

        body = authed_api.expect(authed_api.calculate(salary, person=person), 200)

        total_deduction = sum(item["amount"] for item in body["deductions"])
        assert body["taxable_income"] == salary - BASIC_DEDUCTION - total_deduction

    def test_persist_false_does_not_create_record(self, authed_api: ApiClient) -> None:
        """``persist=false`` 时只算不存，``record_id`` 为 ``null``。"""
        body = authed_api.expect(authed_api.calculate(20_000, persist=False), 200)

        assert body["record_id"] is None
        assert body["created_at"] is None

        page = authed_api.expect(authed_api.records(), 200)
        assert page["total"] == 0

    def test_persist_true_creates_record(self, authed_api: ApiClient) -> None:
        body = authed_api.expect(authed_api.calculate(20_000), 200)

        assert isinstance(body["record_id"], int)
        assert body["created_at"] is not None

        page = authed_api.expect(authed_api.records(), 200)
        assert page["total"] == 1


class TestAuthenticationRequired:
    """测算接口必须要求认证。"""

    @pytest.mark.parametrize(
        ("method", "path", "payload"),
        [
            pytest.param("POST", "/api/v1/tax/calculate", {"salary": 20_000}, id="calculate"),
            pytest.param("POST", "/api/v1/tax/batch", {"items": [{"salary": 20_000}]}, id="batch"),
            pytest.param("GET", "/api/v1/tax/records", None, id="records"),
            pytest.param("GET", "/api/v1/tax/records/1", None, id="record-detail"),
        ],
    )
    def test_anonymous_is_rejected(
        self, api: ApiClient, method: str, path: str, payload: dict[str, Any] | None
    ) -> None:
        response = api.request(method, path, json=payload, authenticated=False)

        assert response.status_code == 401
