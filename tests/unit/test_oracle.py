"""Oracle 自检：测试预言本身也必须被测试。

为什么需要这一层
----------------
Oracle 是所有黑盒用例的期望值来源。如果 Oracle 自己有错，黑盒用例就会把错误当作
「需求」来断言，缺陷反而被掩盖。这在测试工程里叫 **oracle problem**。

本文件的职责是守住 Oracle：输入校验、扣除计算、分摊规则、以及异常表配置下的防御。

> 实际收益：编写本文件时发现 Oracle 曾把「非独生子女每人不超过 1000 元」的分摊上限
> 错误地套用到独生子女身上，导致独生子女的 2000 元扣除被压到 1000 元。该缺陷在
> 依赖 Oracle 的用例中完全不可见，只有对 Oracle 自身写用例才暴露出来。
"""

from __future__ import annotations

import pytest

from tax_service.core import Person, TaxBracket, TaxOracle
from tax_service.core.policy import (
    BASIC_DEDUCTION,
    CONTINUING_EDUCATION_ANNUAL,
    CONTINUING_EDUCATION_MONTHLY,
    ELDERLY_CARE_MONTHLY,
    HOUSING_LOAN_MONTHLY,
    HOUSING_RENT_BY_CITY_TIER,
)

pytestmark = pytest.mark.blackbox


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #


class TestOracleValidation:
    """Oracle 必须拒绝一切非法输入，而不是静默忽略。"""

    def test_accepts_valid_input(self) -> None:
        """合法输入不抛异常。"""
        TaxOracle.validate(Person(), 10_000)

    def test_rejects_null_person(self) -> None:
        with pytest.raises(TypeError):
            TaxOracle.validate(None, 10_000)

    def test_rejects_negative_salary(self) -> None:
        with pytest.raises(ValueError, match="salary"):
            TaxOracle.validate(Person(), -1)

    def test_rejects_negative_children_count(self) -> None:
        with pytest.raises(ValueError, match="children_num"):
            TaxOracle.validate(Person(children_num=-1), 10_000)

    def test_rejects_negative_sibling_count(self) -> None:
        with pytest.raises(ValueError, match="sibling_num"):
            TaxOracle.validate(Person(sibling_num=-1), 10_000)

    @pytest.mark.parametrize("state", [-1, 3, 99])
    def test_rejects_illegal_continuing_education_state(self, state: int) -> None:
        with pytest.raises(ValueError, match="is_keep_stu"):
            TaxOracle.validate(Person(is_keep_stu=state), 10_000)

    @pytest.mark.parametrize("tier", [-1, 4, 99])
    def test_rejects_illegal_rent_tier(self, tier: int) -> None:
        with pytest.raises(ValueError, match="is_rent_house"):
            TaxOracle.validate(Person(is_rent_house=tier), 10_000)

    @pytest.mark.parametrize("tier", [1, 2, 3])
    def test_rejects_loan_combined_with_rent(self, tier: int) -> None:
        """住房贷款利息与住房租金不可同时享受。"""
        with pytest.raises(ValueError, match="不可同时扣除"):
            TaxOracle.validate(Person(house_loans=True, is_rent_house=tier), 10_000)


# --------------------------------------------------------------------------- #
# 应纳税所得额
# --------------------------------------------------------------------------- #


class TestOracleTaxableIncome:
    """逐项核对应纳税所得额的扣除计算。"""

    def test_only_basic_deduction(self) -> None:
        oracle = TaxOracle()
        assert oracle.taxable_income(Person(), 10_000) == 10_000 - BASIC_DEDUCTION

    def test_child_education_is_per_child(self) -> None:
        oracle = TaxOracle()
        assert oracle.taxable_income(Person(children_num=3), 10_000) == (
            10_000 - BASIC_DEDUCTION - 3 * 1_000
        )

    def test_no_child_education_when_zero_children(self) -> None:
        oracle = TaxOracle()
        assert oracle.taxable_income(Person(children_num=0), 10_000) == 10_000 - BASIC_DEDUCTION

    def test_continuing_education_monthly(self) -> None:
        oracle = TaxOracle()
        assert oracle.taxable_income(Person(is_keep_stu=1), 10_000) == (
            10_000 - BASIC_DEDUCTION - CONTINUING_EDUCATION_MONTHLY
        )

    def test_continuing_education_certificate_is_not_monthly(self) -> None:
        """取得证书当年的 3600 元是年度定额，月度计算中不扣除。"""
        oracle = TaxOracle()
        with_certificate = oracle.taxable_income(Person(is_keep_stu=2), 10_000)
        without = oracle.taxable_income(Person(is_keep_stu=0), 10_000)

        assert with_certificate == without
        assert CONTINUING_EDUCATION_ANNUAL == 3_600

    def test_housing_loan(self) -> None:
        oracle = TaxOracle()
        assert oracle.taxable_income(Person(house_loans=True), 10_000) == (
            10_000 - BASIC_DEDUCTION - HOUSING_LOAN_MONTHLY
        )

    @pytest.mark.parametrize("tier", [1, 2, 3])
    def test_housing_rent_by_tier(self, tier: int) -> None:
        oracle = TaxOracle()
        assert oracle.taxable_income(Person(is_rent_house=tier), 10_000) == (
            10_000 - BASIC_DEDUCTION - HOUSING_RENT_BY_CITY_TIER[tier]
        )

    def test_no_housing_rent_for_tier_zero(self) -> None:
        oracle = TaxOracle()
        assert oracle.taxable_income(Person(is_rent_house=0), 10_000) == 10_000 - BASIC_DEDUCTION

    def test_elderly_care_applied(self) -> None:
        oracle = TaxOracle()
        assert oracle.taxable_income(Person(supporting_elderly=True, sibling_num=1), 10_000) == (
            10_000 - BASIC_DEDUCTION - 1_000
        )

    def test_no_elderly_care_when_flag_false(self) -> None:
        oracle = TaxOracle()
        assert oracle.taxable_income(Person(supporting_elderly=False), 10_000) == (
            10_000 - BASIC_DEDUCTION
        )


# --------------------------------------------------------------------------- #
# 赡养老人分摊规则
# --------------------------------------------------------------------------- #


class TestOracleElderlyShare:
    """赡养老人分摊：独生子女定额全额，非独生子女分摊且每人封顶 1000 元。"""

    def test_only_child_gets_full_amount(self) -> None:
        """独生子女按 2000 元全额扣除，不受分摊上限约束。"""
        assert TaxOracle.elderly_share(0) == ELDERLY_CARE_MONTHLY == 2_000

    @pytest.mark.parametrize(
        ("sibling_num", "expected_share"),
        [
            pytest.param(1, 1_000, id="1-sibling"),
            pytest.param(2, 666, id="2-siblings"),
            pytest.param(3, 500, id="3-siblings"),
            pytest.param(9, 200, id="9-siblings"),
        ],
    )
    def test_shared_among_siblings(self, sibling_num: int, expected_share: int) -> None:
        assert TaxOracle.elderly_share(sibling_num) == expected_share

    def test_share_never_exceeds_cap(self) -> None:
        """任意兄弟姐妹数量下，分摊额都不超过每人 1000 元的上限。"""
        for sibling_num in range(0, 50):
            share = TaxOracle.elderly_share(sibling_num)
            if sibling_num == 0:
                continue  # 独生子女不适用上限
            assert share <= 1_000, f"{sibling_num} 个兄弟姐妹时分摊额 {share} 超过上限"


# --------------------------------------------------------------------------- #
# 税额计算与防御性检查
# --------------------------------------------------------------------------- #


class TestOracleCalculation:
    """Oracle 的税额计算与异常配置下的防御行为。"""

    def test_negative_taxable_income_yields_zero(self) -> None:
        oracle = TaxOracle()
        assert oracle.cal(Person(), 3_000) == 0

    def test_zero_taxable_income_yields_zero(self) -> None:
        oracle = TaxOracle()
        assert oracle.cal(Person(), BASIC_DEDUCTION) == 0

    @pytest.mark.parametrize(
        ("taxable_income", "expected_tax"),
        [
            pytest.param(1_500, 45, id="bracket1-3%"),
            pytest.param(7_000, 490, id="bracket2-10%"),
            pytest.param(18_000, 2_190, id="bracket3-20%"),
            pytest.param(30_000, 4_840, id="bracket4-25%"),
            pytest.param(45_000, 9_090, id="bracket5-30%"),
            pytest.param(65_000, 15_590, id="bracket6-35%"),
            pytest.param(120_000, 38_840, id="bracket7-45%"),
        ],
    )
    def test_each_bracket(self, taxable_income: int, expected_tax: int) -> None:
        """七个税率级次逐一验证（遍历覆盖全部级次）。"""
        oracle = TaxOracle()
        assert oracle.cal(Person(), taxable_income + BASIC_DEDUCTION) == expected_tax

    def test_malformed_bracket_table_is_rejected(self) -> None:
        """税率表缺少无上界的最高一级时，必须显式报错而不是静默返回错误结果。

        这是对 Oracle **配置错误**的防御：税率表一旦被改坏，应该立刻失败，
        而不是让所有下游用例基于错误的期望值继续跑。
        """
        truncated = (TaxBracket(3_000, 0.03, 0),)
        oracle = TaxOracle(brackets=truncated)

        with pytest.raises(AssertionError, match="缺少无上界的最高一级"):
            oracle.cal(Person(), 10_000)

    def test_custom_basic_deduction_is_respected(self) -> None:
        """免征额可配置——用于验证参数化能力，而非写死 5000。"""
        oracle = TaxOracle(basic_deduction=3_500)
        assert oracle.cal(Person(), 3_500) == 0
        assert oracle.taxable_income(Person(), 10_000) == 10_000 - 3_500


class TestOracleDeductionBreakdown:
    """扣除明细接口——服务层生成接口响应时依赖它。"""

    def test_returns_empty_for_person_without_deductions(self) -> None:
        assert TaxOracle.deduction_breakdown(Person()) == ()

    def test_returns_itemized_deductions(self) -> None:
        person = Person(children_num=2, house_loans=True, supporting_elderly=True, sibling_num=1)

        items = {item.code: item.amount for item in TaxOracle.deduction_breakdown(person)}

        assert items == {"child_education": 2_000, "housing_loan": 1_000, "elderly_care": 1_000}

    def test_breakdown_total_matches_taxable_income(self) -> None:
        """明细合计必须与应纳税所得额自洽——两者来自同一份口径。

        这是内部一致性断言：若哪天有人只改了一边（例如给明细加了一项但忘了从
        应纳税所得额里扣），这条用例会立刻失败。
        """
        person = Person(children_num=1, is_keep_stu=1, house_loans=True, sibling_num=0)
        salary = 40_000
        oracle = TaxOracle()

        total = sum(item.amount for item in TaxOracle.deduction_breakdown(person))

        assert oracle.taxable_income(person, salary) == salary - BASIC_DEDUCTION - total

    def test_breakdown_excludes_annual_continuing_education(self) -> None:
        """取得证书当年的 3600 元是年度项，不应出现在月度明细中（缺陷 D4 的需求口径）。"""
        items = TaxOracle.deduction_breakdown(Person(is_keep_stu=2))

        assert items == ()
