"""被测系统（SUT）：实验给定个税计算器的忠实移植。

本模块对实验给定的 Java 实现 ``incomeTaxCalculator.cal(Person, salary)``
做**逐行忠实移植**，包括：

- 保留原有的 if / else-if 分支结构，使控制流图与 ``docs/01`` 一一对应；
- 保留原有的硬编码常量，不修正任何已确认缺陷（见 ``docs/05``）。

这样做的目的是让白盒用例（``tests/unit/test_path_coverage.py``）能够逐条映射到
控制流图的独立路径上，也让黑盒用例暴露的偏差可以直接定位到具体源码行。

控制流图中的 17 个判定节点在本文件内以 ``# 判定节点 N`` 注释标出，圈复杂度
``V(G) = 17 + 1 = 18``。
"""

from __future__ import annotations

from tax_service.core.person import Person
from tax_service.core.policy import (
    BASIC_DEDUCTION,
    CHILD_EDUCATION_PER_CHILD,
    CONTINUING_EDUCATION_ANNUAL,
    CONTINUING_EDUCATION_MONTHLY,
    ELDERLY_CARE_MONTHLY,
    HOUSING_LOAN_MONTHLY,
    HOUSING_RENT_BY_CITY_TIER,
)


class IncomeTaxCalculator:
    """个税计算器（实验给定实现的忠实移植）。

    该类是测试的**对象**，不是测试的答案。需求期望值请使用
    :class:`tax_service.core.oracle.TaxOracle`。
    """

    @staticmethod
    def cal(person: Person, salary: int) -> int:
        """计算月度个人所得税。

        :param person: 纳税人。
        :param salary: 工资，已扣除三险一金。
        :return: 应纳税额，按 ``int`` 向零截断取整。
        :raises TypeError: ``person`` 为 ``None``（对应原实现的 NullPointerException）。
        :raises ValueError: ``salary`` 为负数（对应原实现的 IllegalArgumentException）。
        """
        # 判定节点 1
        if person is None:
            raise TypeError("person 为空")

        # 判定节点 2
        if salary < 0:
            raise ValueError("salary 不能为负数")

        # 应纳税所得额 = 月度收入 - 免征额 - 专项扣除 - 专项附加扣除
        taxable = salary - BASIC_DEDUCTION

        # 专项附加扣除：子女教育，每个子女每月 1000 元
        # 判定节点 3
        if person.children_num > 0:
            taxable -= person.children_num * CHILD_EDUCATION_PER_CHILD

        # 专项附加扣除：继续教育
        # 判定节点 4
        if person.is_keep_stu == 1:
            taxable -= CONTINUING_EDUCATION_MONTHLY
        # 判定节点 5
        elif person.is_keep_stu == 2:
            taxable -= CONTINUING_EDUCATION_ANNUAL

        # 专项附加扣除：住房贷款利息，每月 1000 元
        # 判定节点 6
        if person.house_loans:
            taxable -= HOUSING_LOAN_MONTHLY

        # 专项附加扣除：住房租金，按城市档次
        # 判定节点 7
        if person.is_rent_house == 1:
            taxable -= HOUSING_RENT_BY_CITY_TIER[1]
        # 判定节点 8
        elif person.is_rent_house == 2:
            taxable -= HOUSING_RENT_BY_CITY_TIER[2]
        # 判定节点 9
        elif person.is_rent_house == 3:
            taxable -= HOUSING_RENT_BY_CITY_TIER[3]

        # 专项附加扣除：赡养老人，每月 2000 元由兄弟姐妹分摊
        # 判定节点 10
        if person.supporting_elderly:
            taxable -= ELDERLY_CARE_MONTHLY // (person.sibling_num + 1)

        # 按阶梯税率计算应纳税额。
        # 下面各级的常量与源码硬编码值一致：第 3 级起的速算扣除数少计 1300 元（缺陷 D1）。
        tax = float(taxable)

        # 判定节点 11
        if taxable < 0:
            tax = 0.0
        # 判定节点 12
        elif taxable < 3_000:
            tax = taxable * 0.03
        # 判定节点 13
        elif taxable < 12_000:
            tax = 90 + (taxable - 3_000) * 0.1
        # 判定节点 14
        elif taxable < 25_000:
            tax = 2_290 + (taxable - 12_000) * 0.2
        # 判定节点 15
        elif taxable < 35_000:
            tax = 4_890 + (taxable - 25_000) * 0.25
        # 判定节点 16
        elif taxable < 55_000:
            tax = 7_390 + (taxable - 35_000) * 0.3
        # 判定节点 17
        elif taxable < 80_000:
            tax = 13_390 + (taxable - 55_000) * 0.35
        else:
            tax = 22_140 + (taxable - 80_000) * 0.45

        return int(tax)
