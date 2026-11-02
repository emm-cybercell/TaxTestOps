"""测试预言（Oracle）：按需求规格独立实现的参考个税计算器。

为什么需要它
------------
只用 SUT 自己的输出做断言，无法判断「算错了」还是「本来就这样」。测试需要一个
**来源独立**的期望值提供者，这就是 Oracle。

本实现刻意与 SUT 采取**不同的实现方式**（表驱动 + 集中校验 vs 硬编码分支），因此当
两者不一致时，可以判定为 SUT 缺陷，而不是「两边都写错」。

与 SUT 的三处有意差异（每一处对应一个已确认缺陷）
--------------------------------------------------
======================  ====================  ==========================================
维度                    SUT 行为              Oracle 行为（依据需求）
======================  ====================  ==========================================
输入合法性              不校验，静默忽略      非法枚举值 / 负数抛 ``ValueError``
房贷与租金              可同时扣除            互斥，同时提供抛 ``ValueError``
继续教育 3600 元        逐月扣除              年度定额，月度函数中不适用
======================  ====================  ==========================================

税率表使用国标值（:data:`STANDARD_MONTHLY_BRACKETS`），因此 SUT 第 3~7 级
多计 1300 元的问题会直接体现为断言失败。
"""

from __future__ import annotations

from tax_service.core.deductions import (
    ELDERLY_SHARE_CAP,
    build_deduction_breakdown,
    elderly_share,
    total_deduction,
)
from tax_service.core.person import Person
from tax_service.core.policy import (
    BASIC_DEDUCTION,
    HOUSING_RENT_BY_CITY_TIER,
    STANDARD_MONTHLY_BRACKETS,
    TaxBracket,
)

VALID_CONTINUING_EDUCATION_STATES = frozenset({0, 1, 2})
"""继续教育状态的合法取值。"""

VALID_RENT_TIERS = frozenset({0, *HOUSING_RENT_BY_CITY_TIER})
"""住房租金城市档次的合法取值。"""

__all__ = [
    "ELDERLY_SHARE_CAP",
    "VALID_CONTINUING_EDUCATION_STATES",
    "VALID_RENT_TIERS",
    "TaxOracle",
]


class TaxOracle:
    """按需求规格计算的参考实现，用作测试期望值来源。"""

    def __init__(
        self,
        brackets: tuple[TaxBracket, ...] = STANDARD_MONTHLY_BRACKETS,
        basic_deduction: int = BASIC_DEDUCTION,
    ) -> None:
        self.brackets = brackets
        self.basic_deduction = basic_deduction

    # ------------------------------------------------------------------ #
    # 输入校验
    # ------------------------------------------------------------------ #

    @staticmethod
    def validate(person: Person | None, salary: int) -> None:
        """校验输入合法性，非法输入一律拒绝而不是静默忽略。"""
        if person is None:
            raise TypeError("person 为空")
        if salary < 0:
            raise ValueError("salary 不能为负数")
        if person.children_num < 0:
            raise ValueError(f"children_num 不能为负数：{person.children_num}")
        if person.sibling_num < 0:
            raise ValueError(f"sibling_num 不能为负数：{person.sibling_num}")
        if person.is_keep_stu not in VALID_CONTINUING_EDUCATION_STATES:
            raise ValueError(f"is_keep_stu 取值非法：{person.is_keep_stu}")
        if person.is_rent_house not in VALID_RENT_TIERS:
            raise ValueError(f"is_rent_house 取值非法：{person.is_rent_house}")
        if person.house_loans and person.is_rent_house != 0:
            raise ValueError("住房贷款利息与住房租金不可同时扣除")

    # ------------------------------------------------------------------ #
    # 扣除与应纳税所得额
    # ------------------------------------------------------------------ #

    @staticmethod
    def elderly_share(sibling_num: int) -> int:
        """赡养老人扣除的分摊额（详见 :func:`tax_service.core.deductions.elderly_share`）。"""
        return elderly_share(sibling_num)

    @staticmethod
    def deduction_breakdown(person: Person) -> tuple:
        """专项附加扣除明细（需求口径）。"""
        return build_deduction_breakdown(person)

    def taxable_income(self, person: Person, salary: int) -> int:
        """计算应纳税所得额。

        需求：应纳税所得额 = 月度收入 − 5000 元免征额 − 专项扣除 − 专项附加扣除。
        """
        return salary - self.basic_deduction - total_deduction(person)

    # ------------------------------------------------------------------ #
    # 税额
    # ------------------------------------------------------------------ #

    def cal(self, person: Person, salary: int) -> int:
        """按需求规格计算月度个人所得税。

        :raises TypeError: ``person`` 为 ``None``。
        :raises ValueError: 任一输入不合法，或房贷与租金同时提供。
        """
        self.validate(person, salary)
        taxable = self.taxable_income(person, salary)

        if taxable < 0:
            return 0

        for bracket in self.brackets:
            if bracket.upper is None or taxable < bracket.upper:
                tax = taxable * bracket.rate - bracket.quick_deduction
                # 需求未规定取整规则（缺陷 D7），沿用向零截断以便与 SUT 对比。
                return int(tax)

        raise AssertionError("税率表缺少无上界的最高一级，配置有误")
