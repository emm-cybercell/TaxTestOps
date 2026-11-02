"""黑盒测试用例：只依据需求规格设计，不参考被测实现的内部结构。

对应文档
--------
- ``docs/04-黑盒测试用例.md`` —— 等价类划分表、边界值表、判定表与执行记录

期望值来源
----------
1. 需求文档中给出的**政策数值**（扣除标准、税率表）；
2. :class:`tax_service.core.oracle.TaxOracle` —— 与 SUT 实现方式不同的独立参考实现。

与 ``test_path_coverage.py`` 的分工
-----------------------------------
白盒用例断言「当前实现做了什么」，黑盒用例断言「需求要求做什么」。两者的差异就是
缺陷清单。已确认缺陷用 ``xfail(strict=True)`` 标记：修复前不计为失败，修复后会因
「意外通过」而报错，逼迫我们同步更新用例——这是缺陷回归的护栏。

扣除额类断言采用**相对口径**（与中性基准比较），从而不受缺陷 D1 常量偏移的影响，
可以干净地检验各专项附加扣除逻辑本身。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from support.marks import defect
from tax_service.core import IncomeTaxCalculator, Person, TaxOracle
from tax_service.core.policy import (
    CHILD_EDUCATION_PER_CHILD,
    CONTINUING_EDUCATION_ANNUAL,
    ELDERLY_CARE_MONTHLY,
    HOUSING_LOAN_MONTHLY,
    HOUSING_RENT_BY_CITY_TIER,
)

pytestmark = pytest.mark.blackbox

#: 用于相对口径断言的工资，使全部用例的应纳税所得额落在同一税率级次内。
BASELINE_SALARY = 20_000

#: 该工资下适用的边际税率（20%），用于把「多扣 1 元」换算为「少缴 0.2 元」。
MARGINAL_RATE = 0.20


def _tax_saving(deduction: int) -> int:
    """在 20% 边际税率下，额外扣除 ``deduction`` 元带来的税额下降额。"""
    return int(deduction * MARGINAL_RATE)


# --------------------------------------------------------------------------- #
# 一、需求文档自带样例（回归基线）
# --------------------------------------------------------------------------- #


@pytest.mark.smoke
def test_spec_example_without_special_deductions(sut: IncomeTaxCalculator) -> None:
    """需求样例：月收入 10000 元、三险一金 2000 元、无专项附加扣除。

    需求原文：(10000 − 5000 − 2000) × 3% = 90 元。
    ``cal`` 的 salary 参数约定为「已扣除三险一金」，故此处传入 8000 元。
    """
    assert sut.cal(Person(), 8_000) == 90


@pytest.mark.smoke
def test_spec_example_xiao_li(sut: IncomeTaxCalculator) -> None:
    """需求样例：小李，月收入 10000 元，三险一金 2000 元，租金 1500 元，一子女，
    父母 60 多岁并与姐姐分摊赡养扣除。

    需求原文：个税 = (10000 − 5000 − 2000 − 1500 − 1000 − 1000) × 3% = 0 元。
    """
    person = Person(
        children_num=1,
        is_keep_stu=0,
        house_loans=False,
        is_rent_house=1,
        supporting_elderly=True,
        sibling_num=1,
    )
    assert sut.cal(person, 8_000) == 0


# --------------------------------------------------------------------------- #
# 二、等价类划分
# --------------------------------------------------------------------------- #


class TestEquivalencePartitioning:
    """有效等价类与无效等价类的划分与验证。"""

    def test_valid_salary_equal_to_basic_deduction(
        self, sut: IncomeTaxCalculator, neutral: Person
    ) -> None:
        """有效等价类：工资恰等于免征额，应纳税所得额为 0，税额为 0。"""
        assert sut.cal(neutral, 5_000) == 0

    def test_valid_salary_below_basic_deduction(
        self, sut: IncomeTaxCalculator, neutral: Person
    ) -> None:
        """有效等价类：工资低于免征额，应纳税所得额为负，税额按 0 处理。"""
        assert sut.cal(neutral, 3_000) == 0

    def test_invalid_null_person(self, sut: IncomeTaxCalculator) -> None:
        """无效等价类：纳税人为空。"""
        with pytest.raises(TypeError):
            sut.cal(None, 10_000)  # type: ignore[arg-type]

    def test_invalid_negative_salary(self, sut: IncomeTaxCalculator) -> None:
        """无效等价类：工资为负。"""
        with pytest.raises(ValueError):
            sut.cal(Person(), -1)

    @defect("D5", "非法枚举值被静默忽略，需求要求显式拒绝")
    def test_invalid_continuing_education_state(self, sut: IncomeTaxCalculator) -> None:
        """无效等价类：继续教育状态不在 {0, 1, 2} 内。"""
        with pytest.raises(ValueError):
            sut.cal(Person(is_keep_stu=3), 20_000)

    @defect("D5", "非法枚举值被静默忽略，需求要求显式拒绝")
    def test_invalid_rent_tier(self, sut: IncomeTaxCalculator) -> None:
        """无效等价类：住房租金城市档次不在 {0, 1, 2, 3} 内。"""
        with pytest.raises(ValueError):
            sut.cal(Person(is_rent_house=4), 20_000)

    @defect("D3", "sibling_num = -1 时整数除零，进程崩溃")
    def test_invalid_negative_sibling_count(self, sut: IncomeTaxCalculator) -> None:
        """无效等价类：兄弟姐妹个数为负。"""
        with pytest.raises(ValueError):
            sut.cal(Person(supporting_elderly=True, sibling_num=-1), 20_000)


# --------------------------------------------------------------------------- #
# 三、边界值分析（数据驱动，用例表见 tests/data/boundary_cases.csv）
# --------------------------------------------------------------------------- #


def test_bracket_boundary_values(
    sut: IncomeTaxCalculator,
    neutral: Person,
    boundary_case: dict[str, Any],
    salary_for: Callable[[int], int],
) -> None:
    """逐个税率级次的分界点做「边界内侧 / 恰好边界」双点测试。

    用例数据从 CSV 注入（见 ``conftest.pytest_generate_tests``）。受缺陷 D1 影响的
    用例已自动标记为 ``xfail``，失败原因可在 pytest 报告中看到实际值与期望值。
    """
    salary = salary_for(boundary_case["taxable_income"])
    assert sut.cal(neutral, salary) == boundary_case["expected_tax"]


def test_no_tax_below_basic_deduction_boundary(sut: IncomeTaxCalculator, neutral: Person) -> None:
    """免征额下边界：工资 4999 元（差 1 元未达起征点）不产生税额。"""
    assert sut.cal(neutral, 4_999) == 0


# --------------------------------------------------------------------------- #
# 四、判定表：住房贷款利息 × 住房租金
# --------------------------------------------------------------------------- #

#  规则  房贷  租金档次   需求规定动作
#  R1    F     0         不扣除
#  R2    T     0         扣除 1000 元
#  R3    F     1/2/3     按档次扣除 1500 / 1100 / 800 元
#  R4    T     1/2/3     非法输入，应拒绝（两项不可同时享受）


class TestHousingDecisionTable:
    """住房贷款利息与住房租金互斥规则的判定表用例。

    扣除额用**相对口径**验证：以无扣除的纳税人为基准，多扣 ``d`` 元应使税额下降
    ``d × 20%``。该口径与缺陷 D1 的常量偏移无关，能干净地检验住房逻辑本身。
    """

    @pytest.mark.parametrize(
        ("person", "deduction", "rule"),
        [
            pytest.param(Person(house_loans=True), HOUSING_LOAN_MONTHLY, "R2", id="R2-loan-only"),
            pytest.param(
                Person(is_rent_house=1), HOUSING_RENT_BY_CITY_TIER[1], "R3", id="R3-rent-tier1"
            ),
            pytest.param(
                Person(is_rent_house=2), HOUSING_RENT_BY_CITY_TIER[2], "R3", id="R3-rent-tier2"
            ),
            pytest.param(
                Person(is_rent_house=3), HOUSING_RENT_BY_CITY_TIER[3], "R3", id="R3-rent-tier3"
            ),
        ],
    )
    def test_single_housing_deduction(
        self,
        sut: IncomeTaxCalculator,
        neutral: Person,
        person: Person,
        deduction: int,
        rule: str,
    ) -> None:
        """R2 / R3：单独享受房贷或租金时，扣除额与政策一致。"""
        baseline = sut.cal(neutral, BASELINE_SALARY)
        actual = sut.cal(person, BASELINE_SALARY)

        assert baseline - actual == _tax_saving(deduction), (
            f"{rule}：扣除 {deduction} 元应使税额下降 {_tax_saving(deduction)} 元，"
            f"实际下降 {baseline - actual} 元"
        )

    def test_r1_no_housing_deduction(self, sut: IncomeTaxCalculator, neutral: Person) -> None:
        """R1：既无房贷也无租金，不发生住房类扣除。"""
        assert sut.cal(neutral, BASELINE_SALARY) == sut.cal(Person(), BASELINE_SALARY)

    @defect("D2", "房贷与租金可同时扣除，需求规定二者互斥")
    def test_r4_loan_and_rent_are_mutually_exclusive(self, sut: IncomeTaxCalculator) -> None:
        """R4：房贷与租金同时提供——需求要求拒绝，SUT 静默双扣。"""
        person = Person(house_loans=True, is_rent_house=1)
        with pytest.raises(ValueError):
            sut.cal(person, BASELINE_SALARY)


# --------------------------------------------------------------------------- #
# 五、需求符合性对照（SUT vs 独立 Oracle）
# --------------------------------------------------------------------------- #


class TestRequirementConformance:
    """逐条核对专项附加扣除标准的实现是否符合需求。"""

    def test_child_education_standard(self, sut: IncomeTaxCalculator) -> None:
        """子女教育：每名子女每月 1000 元，扣除额随子女人数线性增长。

        在本用例选定的工资下，全部情形落在同一税率级次（20% 边际税率），因此
        「多一名子女」应稳定地使税额下降 1000 × 20% = 200 元。
        """
        assert CHILD_EDUCATION_PER_CHILD == 1_000

        none = sut.cal(Person(children_num=0), BASELINE_SALARY)
        one = sut.cal(Person(children_num=1), BASELINE_SALARY)
        two = sut.cal(Person(children_num=2), BASELINE_SALARY)

        assert none - one == _tax_saving(CHILD_EDUCATION_PER_CHILD)
        assert one - two == _tax_saving(CHILD_EDUCATION_PER_CHILD)

    def test_continuing_education_monthly_standard(self, sut: IncomeTaxCalculator) -> None:
        """继续教育（学历期间）：每月扣除 400 元。"""
        baseline = sut.cal(Person(is_keep_stu=0), BASELINE_SALARY)
        enrolled = sut.cal(Person(is_keep_stu=1), BASELINE_SALARY)

        assert baseline - enrolled == _tax_saving(400)

    @defect("D4", "3600 元为年度定额，被当作月度项逐月扣除")
    def test_continuing_education_certificate_is_annual(
        self, sut: IncomeTaxCalculator, oracle: TaxOracle
    ) -> None:
        """继续教育（取得证书当年）：3600 元属**年度**定额，月度计算中不应逐月扣除。

        若按月度处理，全年将扣除 43200 元，为政策值的 12 倍。
        """
        assert CONTINUING_EDUCATION_ANNUAL == 3_600
        person = Person(is_keep_stu=2)
        assert sut.cal(person, BASELINE_SALARY) == oracle.cal(person, BASELINE_SALARY)

    def test_elderly_care_standard(self, sut: IncomeTaxCalculator) -> None:
        """赡养老人：独生子女全额 2000 元，非独生子女按人数分摊。"""
        assert ELDERLY_CARE_MONTHLY == 2_000

        only_child = sut.cal(Person(supporting_elderly=True, sibling_num=0), BASELINE_SALARY)
        one_sibling = sut.cal(Person(supporting_elderly=True, sibling_num=1), BASELINE_SALARY)

        assert only_child < one_sibling, "独生子女扣除更多，税额应更低"
        assert one_sibling - only_child == _tax_saving(ELDERLY_CARE_MONTHLY // 2)

    def test_sut_and_oracle_agree_where_no_defect_exists(
        self, sut: IncomeTaxCalculator, oracle: TaxOracle
    ) -> None:
        """在不受 D1 影响的区间（应纳税所得额 < 12000 元）内，SUT 与需求完全一致。

        这条用例界定了缺陷的影响范围——它证明 SUT 并非「整体不可用」，而是有一个
        边界清晰的缺陷区间，这直接决定了修复方案与回归范围。
        """
        for taxable_income in range(0, 12_000, 250):
            salary = taxable_income + 5_000
            assert sut.cal(Person(), salary) == oracle.cal(Person(), salary), (
                f"应纳税所得额 {taxable_income} 元处出现非预期差异"
            )
