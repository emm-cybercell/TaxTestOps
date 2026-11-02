"""政策数据质量：税率表与扣除标准的数据契约测试。

为什么单独测数据
----------------
税率表是一张**常量表**，代码逻辑再对，表抄错一个数字照样算错税——本项目的缺陷 D1
正是这种情况。逐条写「某工资对应某税额」的用例只能覆盖到采样点；直接校验**表本身的
自洽性**（速算扣除数 = 该级上界全额计税 − 上界处累计税额）则能一次性守住全表。

这套校验的价值在于：它把「抄错一个常量」从「只有恰好采样的用例才能发现」变成
「无论如何都会失败」。
"""

from __future__ import annotations

import pytest

from tax_service.core.policy import (
    BASIC_DEDUCTION,
    CHILD_EDUCATION_PER_CHILD,
    CONTINUING_EDUCATION_ANNUAL,
    CONTINUING_EDUCATION_MONTHLY,
    ELDERLY_CARE_MONTHLY,
    HOUSING_LOAN_MONTHLY,
    HOUSING_RENT_BY_CITY_TIER,
    LEGACY_MONTHLY_BRACKETS,
    STANDARD_MONTHLY_BRACKETS,
    TaxBracket,
)

pytestmark = pytest.mark.blackbox


def find_inconsistent_brackets(
    brackets: tuple[TaxBracket, ...],
) -> list[int]:
    """返回速算扣除数与累计税额不自洽的级次下标。

    自洽性判据：对第 ``i`` 级，其速算扣除数应等于

        ``上界 × 税率 − 上界处的累计税额``

    这样 ``上界 × 税率 − 速算扣除数`` 才恰好等于累计税额，保证税额函数在级次
    分界点连续。
    """
    mismatches: list[int] = []
    cumulative_tax = 0.0
    previous_upper = 0

    for index, bracket in enumerate(brackets):
        if bracket.upper is None:
            expected = round(previous_upper * bracket.rate - cumulative_tax)
        else:
            cumulative_tax += (bracket.upper - previous_upper) * bracket.rate
            expected = round(bracket.upper * bracket.rate - cumulative_tax)
            previous_upper = bracket.upper

        if bracket.quick_deduction != expected:
            mismatches.append(index)

    return mismatches


# --------------------------------------------------------------------------- #
# 表结构契约
# --------------------------------------------------------------------------- #


class TestBracketTableStructure:
    """税率表的结构约束——违反任意一条都说明数据被改坏了。"""

    def test_has_seven_levels(self) -> None:
        assert len(STANDARD_MONTHLY_BRACKETS) == 7

    def test_upper_bounds_strictly_ascending(self) -> None:
        uppers = [b.upper for b in STANDARD_MONTHLY_BRACKETS if b.upper is not None]
        assert uppers == sorted(uppers)
        assert len(uppers) == len(set(uppers)), "上界出现重复"

    def test_only_top_bracket_is_unbounded(self) -> None:
        unbounded = [i for i, b in enumerate(STANDARD_MONTHLY_BRACKETS) if b.upper is None]
        assert unbounded == [len(STANDARD_MONTHLY_BRACKETS) - 1], (
            "只有最高一级可以无上界，否则会形成覆盖空洞"
        )

    def test_rates_are_ascending_and_within_range(self) -> None:
        rates = [b.rate for b in STANDARD_MONTHLY_BRACKETS]
        assert rates == sorted(rates), "税率必须随级次递增"
        assert all(0 < rate < 1 for rate in rates)

    def test_quick_deductions_are_non_negative(self) -> None:
        assert all(b.quick_deduction >= 0 for b in STANDARD_MONTHLY_BRACKETS)


# --------------------------------------------------------------------------- #
# 表数据自洽性
# --------------------------------------------------------------------------- #


def test_standard_table_is_self_consistent() -> None:
    """国标税率表完全自洽——这是 Oracle 可信的前提。"""
    assert find_inconsistent_brackets(STANDARD_MONTHLY_BRACKETS) == []


def test_legacy_table_is_inconsistent_in_exactly_five_brackets() -> None:
    """实验给定实现的税率表在第 3~7 级不自洽（缺陷 D1 的数据层证据）。

    与 ``test_defect_characterization.py`` 中基于行为比对的结论互相印证：
    一处常量写错，同时表现为「表不自洽」与「税额恒多 1300 元」。
    """
    assert find_inconsistent_brackets(LEGACY_MONTHLY_BRACKETS) == [2, 3, 4, 5, 6]


def test_legacy_offset_is_exactly_the_quick_deduction_difference() -> None:
    """逐级比对两表，确认差异就是速算扣除数少了 1300，且仅发生在第 3 级及以后。"""
    for index, (standard, legacy) in enumerate(
        zip(STANDARD_MONTHLY_BRACKETS, LEGACY_MONTHLY_BRACKETS, strict=True)
    ):
        assert standard.upper == legacy.upper
        assert standard.rate == legacy.rate

        difference = standard.quick_deduction - legacy.quick_deduction
        expected = 1_300 if index >= 2 else 0
        assert difference == expected, f"第 {index + 1} 级速算扣除数差额异常"


# --------------------------------------------------------------------------- #
# 扣除标准数值契约
# --------------------------------------------------------------------------- #


class TestDeductionStandardValues:
    """专项附加扣除标准的数值与需求文档一致。"""

    def test_basic_deduction(self) -> None:
        assert BASIC_DEDUCTION == 5_000

    def test_child_education(self) -> None:
        assert CHILD_EDUCATION_PER_CHILD == 1_000

    def test_continuing_education_monthly(self) -> None:
        assert CONTINUING_EDUCATION_MONTHLY == 400

    def test_continuing_education_annual(self) -> None:
        assert CONTINUING_EDUCATION_ANNUAL == 3_600

    def test_housing_loan(self) -> None:
        assert HOUSING_LOAN_MONTHLY == 1_000

    def test_housing_rent_tiers(self) -> None:
        assert HOUSING_RENT_BY_CITY_TIER == {1: 1_500, 2: 1_100, 3: 800}

    def test_housing_rent_tiers_are_descending(self) -> None:
        """城市档次越高（数字越小）扣除越多。"""
        amounts = [HOUSING_RENT_BY_CITY_TIER[tier] for tier in (1, 2, 3)]
        assert amounts == sorted(amounts, reverse=True)

    def test_elderly_care(self) -> None:
        assert ELDERLY_CARE_MONTHLY == 2_000
