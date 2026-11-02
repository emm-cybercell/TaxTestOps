"""缺陷特征化用例：把已确认缺陷的表现固化为可执行证据。

用途
----
特征化测试（characterization test）断言的是**当前实现的实际行为**，而不是需求。
它在本项目中承担三个作用：

1. **证据**：每条缺陷都有可复现的输入、实际输出与期望输出，不是口头描述；
2. **范围界定**：证明缺陷影响面是精确可控的（例如 D1 只影响 5 个税率级次），
   从而指导修复方案与回归范围；
3. **回归护栏**：缺陷修复后这些用例会失败，提示我们移除特征化断言、改断言需求值。

与 ``test_requirements.py`` 的关系
----------------------------------
``test_requirements.py`` 里的缺陷用例用 ``xfail`` 标记「需求未被满足」；
本文件则正面描述「缺陷长什么样」。两者互补：一个说结论，一个给证据。
"""

from __future__ import annotations

import pytest

from support.marks import legacy_only
from tax_service.core import IncomeTaxCalculator, Person, TaxOracle
from tax_service.core.policy import (
    CONTINUING_EDUCATION_ANNUAL,
    ELDERLY_CARE_MONTHLY,
    HOUSING_LOAN_MONTHLY,
    HOUSING_RENT_BY_CITY_TIER,
    STANDARD_MONTHLY_BRACKETS,
)

pytestmark = [
    pytest.mark.characterization,
    legacy_only("特征化用例描述的是实验给定实现的当前行为，修复后应移除"),
]

SALARY = 20_000

#: 每个税率级次取一个代表性应纳税所得额，用于逐级比对 SUT 与 Oracle。
BRACKET_SAMPLES = (1_500, 7_000, 18_000, 30_000, 45_000, 65_000, 120_000)


# --------------------------------------------------------------------------- #
# D1：税率阶梯速算扣除数少计 1300 元（严重）
# --------------------------------------------------------------------------- #


def test_d1_offset_is_exactly_1300_above_threshold(
    sut: IncomeTaxCalculator, oracle: TaxOracle
) -> None:
    """D1 的特征：应纳税所得额 ≥ 12000 元时，SUT 恒比需求多计 **恰好** 1300 元。

    遍历 12000~90000 元的采样点，差额全程恒定，说明这不是随机算错，而是速算
    扣除数这一处常量系统性写错。
    """
    differences = {
        sut.cal(Person(), salary) - oracle.cal(Person(), salary)
        for salary in range(12_000 + 5_000, 90_000 + 5_000, 500)
    }

    assert differences == {1_300}, f"差额并非恒定为 1300 元：{sorted(differences)}"


def test_d1_offset_is_absent_below_threshold(sut: IncomeTaxCalculator, oracle: TaxOracle) -> None:
    """D1 的影响下界：应纳税所得额 < 12000 元时完全无偏差。"""
    differences = {
        sut.cal(Person(), salary) - oracle.cal(Person(), salary)
        for salary in range(5_000, 12_000 + 5_000, 100)
    }

    assert differences == {0}, f"未受影响的区间出现了偏差：{sorted(differences)}"


def test_d1_affects_exactly_five_brackets(sut: IncomeTaxCalculator, oracle: TaxOracle) -> None:
    """D1 精确影响第 3~7 级（下标 2~6），共 5 个税率级次。

    第 1、2 级的速算扣除数正确，说明缺陷是「从某一级开始漏加」而非逐级独立写错，
    这对定位根因很关键。
    """
    affected = [
        index
        for index, taxable_income in enumerate(BRACKET_SAMPLES)
        if sut.cal(Person(), taxable_income + 5_000) != oracle.cal(Person(), taxable_income + 5_000)
    ]

    assert len(STANDARD_MONTHLY_BRACKETS) == 7
    assert affected == [2, 3, 4, 5, 6]


def test_d1_creates_a_discontinuity_at_the_bracket_boundary(
    sut: IncomeTaxCalculator, oracle: TaxOracle
) -> None:
    """D1 的次生现象：应纳税所得额 11999 → 12000 元处税额跳变 1301 元。

    税额函数本应连续（收入多 1 元，税额多 0.2 元）。跳变的存在说明该处存在
    「税率级次切换 + 速算扣除数错误」的叠加，也是黑盒边界值测试能直接发现它的原因。
    """
    just_below = sut.cal(Person(), 11_999 + 5_000)
    at_boundary = sut.cal(Person(), 12_000 + 5_000)

    assert just_below == 989
    assert at_boundary == 2_290
    assert at_boundary - just_below == 1_301

    # 需求实现下该处连续：989 → 990
    assert oracle.cal(Person(), 11_999 + 5_000) == 989
    assert oracle.cal(Person(), 12_000 + 5_000) == 990


# --------------------------------------------------------------------------- #
# D2：住房贷款利息与住房租金同时扣除（严重）
# --------------------------------------------------------------------------- #


def test_d2_loan_and_rent_are_both_deducted(sut: IncomeTaxCalculator) -> None:
    """D2 的特征：同时提供房贷与租金时，两笔扣除都被生效。

    以税额降幅反推扣除额：中性基准 → 仅房贷 → 仅租金 → 两者同时，税额单调下降，
    说明 1000 元与 1500 元被**叠加**扣除，而政策要求二者只能选其一。
    """
    neutral = sut.cal(Person(), SALARY)
    loan_only = sut.cal(Person(house_loans=True), SALARY)
    rent_only = sut.cal(Person(is_rent_house=1), SALARY)
    both = sut.cal(Person(house_loans=True, is_rent_house=1), SALARY)

    assert both == 2_390
    assert both < rent_only < loan_only < neutral, "扣除项越多税额越低，说明两项都生效了"

    total_deducted = HOUSING_LOAN_MONTHLY + HOUSING_RENT_BY_CITY_TIER[1]
    assert total_deducted == 2_500
    assert neutral - both == int(total_deducted * 0.20)


# --------------------------------------------------------------------------- #
# D3：兄弟姐妹个数为 -1 时整数除零（严重）
# --------------------------------------------------------------------------- #


def test_d3_division_by_zero_crashes(sut: IncomeTaxCalculator) -> None:
    """D3 的特征：``sibling_num = -1`` 且赡养老人为真时，除数为 0 直接崩溃。

    该输入未被任何校验拦截，会以异常形式穿透到调用方。在服务化之后，这意味着
    一个畸形请求即可造成 500 错误。
    """
    person = Person(supporting_elderly=True, sibling_num=-1)

    with pytest.raises(ZeroDivisionError):
        sut.cal(person, SALARY)


# --------------------------------------------------------------------------- #
# D4：继续教育 3600 元年度定额被按月扣除（中）
# --------------------------------------------------------------------------- #


def test_d4_annual_deduction_is_applied_monthly(
    sut: IncomeTaxCalculator, oracle: TaxOracle
) -> None:
    """D4 的特征：3600 元年度定额被直接从**当月**所得中扣除。

    若按政策以年度定额处理，该工资下的应纳税额应为 1590 元；实际为 930 元，
    差额来自 3600 元被当作月度项。折算到全年，扣除额达 43200 元，是政策值的 12 倍。
    """
    person = Person(is_keep_stu=2)

    assert sut.cal(person, SALARY) == 930
    assert oracle.cal(person, SALARY) == 1_590

    implied_annual_deduction = CONTINUING_EDUCATION_ANNUAL * 12
    assert implied_annual_deduction == 43_200
    assert implied_annual_deduction - CONTINUING_EDUCATION_ANNUAL == 39_600


# --------------------------------------------------------------------------- #
# D5：非法枚举值被静默忽略（中）
# --------------------------------------------------------------------------- #


def test_d5_illegal_enum_values_are_silently_ignored(
    sut: IncomeTaxCalculator,
) -> None:
    """D5 的特征：越界枚举值与「未享受该扣除」返回完全相同的结果。

    调用方无法区分「传了非法值」和「确实没有这项扣除」，非法数据会被静默吞掉。
    """
    baseline = sut.cal(Person(is_keep_stu=0, is_rent_house=0), SALARY)

    assert sut.cal(Person(is_keep_stu=3), SALARY) == baseline
    assert sut.cal(Person(is_keep_stu=-1), SALARY) == baseline
    assert sut.cal(Person(is_rent_house=4), SALARY) == baseline
    assert sut.cal(Person(is_rent_house=-1), SALARY) == baseline


# --------------------------------------------------------------------------- #
# D6：赡养老人分摊额整数截断（轻）
# --------------------------------------------------------------------------- #


def test_d6_elderly_share_is_truncated(sut: IncomeTaxCalculator) -> None:
    """D6 的特征：3 名兄弟姐妹时每人分摊 666 元，合计 1998 元，比政策少扣 2 元。

    需求未规定分摊额的取整方式，属**需求歧义**而非纯粹的实现错误，因此需要产品
    确认后再决定是向上取整、四舍五入还是按人约定。
    """
    share = ELDERLY_CARE_MONTHLY // (2 + 1)

    assert share == 666
    assert share * 3 == 1_998
    assert ELDERLY_CARE_MONTHLY - share * 3 == 2


# --------------------------------------------------------------------------- #
# D7：取整规则未定义，实现按向零截断（轻）
# --------------------------------------------------------------------------- #


def test_d7_rounding_policy_is_undefined_and_truncates(
    sut: IncomeTaxCalculator,
) -> None:
    """D7 的特征：需求未规定税额取整规则，实现按 ``int()`` 向零截断。

    应纳税所得额 2999 元的精确税额为 89.97 元，截断得 89 元，四舍五入应为 90 元。
    该差异虽小，但会随批量申报规模放大，且不同实现（Java 的 ``(int)``、Python 的
    ``int()``、数据库的 ``ROUND``）会给出不同结果，必须由需求方明确。
    """
    exact_tax = 2_999 * 0.03

    assert exact_tax == pytest.approx(89.97)
    assert sut.cal(Person(), 2_999 + 5_000) == 89
    assert round(exact_tax) == 90
