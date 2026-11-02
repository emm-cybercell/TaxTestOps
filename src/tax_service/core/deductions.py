"""专项附加扣除的明细计算。

把「扣除计算」从个税计算器中拆出来，作为**需求口径的唯一实现**，供三处复用：

1. 测试预言 :class:`~tax_service.core.oracle.TaxOracle` 计算应纳税所得额；
2. 服务层生成接口响应中的扣除明细；
3. 报告与对账。

这样「需求口径」只有一份实现，不会出现「Oracle 一套、接口另一套」的漂移。
"""

from __future__ import annotations

from dataclasses import dataclass

from tax_service.core.person import Person
from tax_service.core.policy import (
    CHILD_EDUCATION_PER_CHILD,
    CONTINUING_EDUCATION_MONTHLY,
    ELDERLY_CARE_MONTHLY,
    HOUSING_LOAN_MONTHLY,
    HOUSING_RENT_BY_CITY_TIER,
)

ELDERLY_SHARE_CAP = 1_000
"""赡养老人分摊后每人每月上限（仅适用于非独生子女）。"""


@dataclass(frozen=True)
class DeductionItem:
    """一条专项附加扣除明细。"""

    code: str
    label: str
    amount: int

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "label": self.label, "amount": self.amount}


def elderly_share(sibling_num: int) -> int:
    """赡养老人扣除的分摊额。

    - 独生子女：全额 2000 元，**不受分摊上限约束**；
    - 非独生子女：2000 元按人数分摊，每人不超过 1000 元。

    分摊额取整方式需求未明确（缺陷 D6），此处沿用整除以保持与历史行为一致。
    """
    if sibling_num == 0:
        return ELDERLY_CARE_MONTHLY

    return min(ELDERLY_CARE_MONTHLY // (sibling_num + 1), ELDERLY_SHARE_CAP)


def build_deduction_breakdown(person: Person) -> tuple[DeductionItem, ...]:
    """按需求规格生成扣除明细。

    .. note::
       继续教育「取得证书当年 3600 元定额」属**年度**项，月度计算中不适用，
       因此不出现在明细中。这与 SUT 的行为差异即缺陷 D4。
    """
    items: list[DeductionItem] = []

    if person.children_num > 0:
        items.append(
            DeductionItem(
                code="child_education",
                label="子女教育",
                amount=person.children_num * CHILD_EDUCATION_PER_CHILD,
            )
        )

    if person.is_keep_stu == 1:
        items.append(
            DeductionItem(
                code="continuing_education",
                label="继续教育（学历期间）",
                amount=CONTINUING_EDUCATION_MONTHLY,
            )
        )

    if person.house_loans:
        items.append(
            DeductionItem(code="housing_loan", label="住房贷款利息", amount=HOUSING_LOAN_MONTHLY)
        )

    if person.is_rent_house in HOUSING_RENT_BY_CITY_TIER:
        items.append(
            DeductionItem(
                code="housing_rent",
                label=f"住房租金（{person.is_rent_house} 档）",
                amount=HOUSING_RENT_BY_CITY_TIER[person.is_rent_house],
            )
        )

    if person.supporting_elderly:
        items.append(
            DeductionItem(
                code="elderly_care",
                label="赡养老人",
                amount=elderly_share(person.sibling_num),
            )
        )

    return tuple(items)


def total_deduction(person: Person) -> int:
    """专项附加扣除合计。"""
    return sum(item.amount for item in build_deduction_breakdown(person))
