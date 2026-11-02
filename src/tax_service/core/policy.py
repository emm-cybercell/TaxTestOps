"""个税政策常量与税率表。

本模块把**国标月度税率表**与**实验给定实现实际使用的税率表**并列存放。
二者唯一的差异由 :data:`LEGACY_QUICK_DEDUCTION_OFFSET` 表达——缺陷 D1 的根因
就落在这一处常量上，详见 ``docs/05-缺陷分析报告.md``。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TaxBracket:
    """一级超额累进税率。

    计算公式：``税额 = 应纳税所得额 × rate − quick_deduction``。

    :param upper: 该级应纳税所得额上界（不含）。``None`` 表示最高一级，无上界。
    :param rate: 该级税率。
    :param quick_deduction: 速算扣除数。它等于「按全额累进计算多收的部分」，
        作用是让超额累进可以用乘法一步算出。
    """

    upper: int | None
    rate: float
    quick_deduction: int


# --------------------------------------------------------------------------- #
# 专项附加扣除标准（元 / 月，另有标注者除外）
# --------------------------------------------------------------------------- #

BASIC_DEDUCTION = 5000
"""月度免征额（起征点）。"""

CHILD_EDUCATION_PER_CHILD = 1000
"""子女教育：每个子女每月。"""

CONTINUING_EDUCATION_MONTHLY = 400
"""继续教育（学历/学位教育期间）：每月。"""

CONTINUING_EDUCATION_ANNUAL = 3600
"""继续教育（取得相关证书的当年）：年度定额，**非月度项**。"""

HOUSING_LOAN_MONTHLY = 1000
"""住房贷款利息：每月。政策规定与住房租金不可同时享受。"""

HOUSING_RENT_BY_CITY_TIER = {1: 1500, 2: 1100, 3: 800}
"""住房租金：按城市档次每月扣除。"""

ELDERLY_CARE_MONTHLY = 2000
"""赡养老人：每月，由兄弟姐妹分摊。"""


# --------------------------------------------------------------------------- #
# 国标月度税率表（2019 年起施行的综合所得税率表按月换算）
# --------------------------------------------------------------------------- #

STANDARD_MONTHLY_BRACKETS: tuple[TaxBracket, ...] = (
    TaxBracket(3_000, 0.03, 0),
    TaxBracket(12_000, 0.10, 210),
    TaxBracket(25_000, 0.20, 1_410),
    TaxBracket(35_000, 0.25, 2_660),
    TaxBracket(55_000, 0.30, 4_410),
    TaxBracket(80_000, 0.35, 7_160),
    TaxBracket(None, 0.45, 15_160),
)


# --------------------------------------------------------------------------- #
# 实验给定实现所用的税率表（缺陷 D1）
# --------------------------------------------------------------------------- #

LEGACY_QUICK_DEDUCTION_OFFSET = 1_300
"""实验给定实现在受影响级别上少计的速算扣除数。

速算扣除数少计 1300 元，等价于该级**多计税 1300 元**。
"""

LEGACY_FIRST_AFFECTED_INDEX = 2
"""首个受影响的级别下标（0-based）。下标 2 即第 3 级，应纳税所得额 ≥ 12000 元。"""


def _build_legacy_brackets() -> tuple[TaxBracket, ...]:
    """按实验给定实现的常量构造税率表，供 SUT 与文档比对使用。"""
    return tuple(
        TaxBracket(
            bracket.upper,
            bracket.rate,
            bracket.quick_deduction
            - (LEGACY_QUICK_DEDUCTION_OFFSET if index >= LEGACY_FIRST_AFFECTED_INDEX else 0),
        )
        for index, bracket in enumerate(STANDARD_MONTHLY_BRACKETS)
    )


LEGACY_MONTHLY_BRACKETS: tuple[TaxBracket, ...] = _build_legacy_brackets()
"""实验给定实现实际使用的税率表，第 3~7 级速算扣除数各少 1300 元。"""


# 展开后与源码中的硬编码常量一一对应，便于人工核对：
#
#   级  应纳税所得额            源码常量   应为    差额
#   1   [0, 3000)              0          0        0
#   2   [3000, 12000)          90         90       0
#   3   [12000, 25000)         2290       990     +1300
#   4   [25000, 35000)         4890       3590    +1300
#   5   [35000, 55000)         7390       6090    +1300
#   6   [55000, 80000)         13390      12090   +1300
#   7   [80000, +∞)            22140      20840   +1300
#
# 说明：源码采用「累进起算额 + 增量」写法，上表已换算为等效的「速算扣除数」写法，
# 两种写法在每一级内部给出完全相同的税额（已在 docs/05 中逐步验证）。
