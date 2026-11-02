"""白盒测试用例：按控制流图的 18 条独立路径设计。

对应文档
--------
- ``docs/01-控制流图与圈复杂度.md`` —— 控制流图与 ``V(G) = 18`` 的推导
- ``docs/02-独立路径分析.md`` —— 三种方法求出的独立路径集合
- ``docs/03-白盒测试用例.md`` —— 用例表与执行记录

设计原则
--------
本文件的断言**锁定当前实现的行为**（特征化断言），其中标注 ``⚠D1`` 的期望值包含
已确认的计税缺陷。这样做有两个目的：

1. 修复缺陷前先固定基线——18 条独立路径全部可达、且输出可复现；
2. 修复缺陷后这些断言会立刻失败，提示我们同步更新用例，避免「静默改行为」。

需求期望值不在这里断言，而在 ``tests/unit/test_requirements.py``。这一分工本身
就是本项目的核心结论：**路径覆盖 100% ≠ 需求被满足**。
"""

from __future__ import annotations

import pytest

from support.marks import legacy_only
from tax_service.core import IncomeTaxCalculator, Person

pytestmark = [
    pytest.mark.whitebox,
    legacy_only("白盒用例锁定的是实验给定实现的行为，缺陷修复后应改写而非沿用"),
]


# --------------------------------------------------------------------------- #
# 路径 P1–P2：输入合法性守卫
# --------------------------------------------------------------------------- #


@pytest.mark.smoke
def test_p01_null_person_is_rejected(sut: IncomeTaxCalculator) -> None:
    """P1：判定节点 1 为真——person 为空，立即抛出异常。"""
    with pytest.raises(TypeError):
        sut.cal(None, 10_000)  # type: ignore[arg-type]


@pytest.mark.smoke
def test_p02_negative_salary_is_rejected(sut: IncomeTaxCalculator) -> None:
    """P2：判定节点 2 为真——工资为负，抛出参数异常。"""
    with pytest.raises(ValueError):
        sut.cal(Person(), -1)


# --------------------------------------------------------------------------- #
# 路径 P3–P10：税率阶梯的 8 个分支（覆盖判定节点 11–17）
# --------------------------------------------------------------------------- #


@pytest.mark.smoke
def test_p03_taxable_income_below_zero(sut: IncomeTaxCalculator, neutral: Person) -> None:
    """P3：判定节点 11 为真——应纳税所得额为负，税额归零。

    工资 4000 元低于 5000 元免征额，应纳税所得额 −1000 元。
    """
    assert sut.cal(neutral, 4_000) == 0


def test_p04_first_bracket(sut: IncomeTaxCalculator, neutral: Person) -> None:
    """P4：判定节点 12 为真——应纳税所得额 2000 元，适用 3% 税率。

    2000 × 3% = 60 元。
    """
    assert sut.cal(neutral, 7_000) == 60


def test_p05_second_bracket(sut: IncomeTaxCalculator, neutral: Person) -> None:
    """P5：判定节点 13 为真——应纳税所得额 5000 元，适用 10% 税率。

    90 + (5000 − 3000) × 10% = 290 元。
    """
    assert sut.cal(neutral, 10_000) == 290


def test_p06_third_bracket(sut: IncomeTaxCalculator, neutral: Person) -> None:
    """P6：判定节点 14 为真——应纳税所得额 15000 元，适用 20% 税率。

    当前实现：2290 + (15000 − 12000) × 20% = 2890 元。

    ⚠D1：需求期望值为 1590 元，当前实现多计 1300 元。
    """
    assert sut.cal(neutral, 20_000) == 2_890


def test_p07_fourth_bracket(sut: IncomeTaxCalculator, neutral: Person) -> None:
    """P7：判定节点 15 为真——应纳税所得额 25000 元，适用 25% 税率。

    ⚠D1：当前实现 4890 元，需求期望值 3590 元。
    """
    assert sut.cal(neutral, 30_000) == 4_890


def test_p08_fifth_bracket(sut: IncomeTaxCalculator, neutral: Person) -> None:
    """P8：判定节点 16 为真——应纳税所得额 35000 元，适用 30% 税率。

    ⚠D1：当前实现 7390 元，需求期望值 6090 元。
    """
    assert sut.cal(neutral, 40_000) == 7_390


def test_p09_sixth_bracket(sut: IncomeTaxCalculator, neutral: Person) -> None:
    """P9：判定节点 17 为真——应纳税所得额 55000 元，适用 35% 税率。

    ⚠D1：当前实现 13390 元，需求期望值 12090 元。
    """
    assert sut.cal(neutral, 60_000) == 13_390


def test_p10_top_bracket(sut: IncomeTaxCalculator, neutral: Person) -> None:
    """P10：判定节点 17 为假——应纳税所得额 85000 元，适用最高级 45% 税率。

    ⚠D1：当前实现 24390 元，需求期望值 23090 元。
    """
    assert sut.cal(neutral, 90_000) == 24_390


# --------------------------------------------------------------------------- #
# 路径 P11–P18：五类专项附加扣除各自的「生效」分支（判定节点 3–10）
# --------------------------------------------------------------------------- #


def test_p11_child_education_deduction(sut: IncomeTaxCalculator) -> None:
    """P11：判定节点 3 为真——2 名子女，每月扣除 2000 元。

    ⚠D1：当前实现 2490 元，需求期望值 1190 元。
    """
    person = Person(children_num=2)
    assert sut.cal(person, 20_000) == 2_490


def test_p12_continuing_education_without_certificate(
    sut: IncomeTaxCalculator,
) -> None:
    """P12：判定节点 4 为真——接受继续教育且当年未取证，每月扣除 400 元。

    ⚠D1：当前实现 2810 元，需求期望值 1510 元。
    """
    person = Person(is_keep_stu=1)
    assert sut.cal(person, 20_000) == 2_810


def test_p13_continuing_education_with_certificate(
    sut: IncomeTaxCalculator,
) -> None:
    """P13：判定节点 5 为真——接受继续教育且当年取证，扣除 3600 元。

    应纳税所得额 15000 − 3600 = 11400 元，落在第 2 级：
    90 + (11400 − 3000) × 10% = 930 元。

    该路径不受 D1 影响（结果落在第 2 级），但与需求存在差异：3600 元是**年度**
    定额，被当作月度项扣除（缺陷 D4）。
    """
    person = Person(is_keep_stu=2)
    assert sut.cal(person, 20_000) == 930


def test_p14_housing_loan_interest_deduction(sut: IncomeTaxCalculator) -> None:
    """P14：判定节点 6 为真——发生住房贷款利息，每月扣除 1000 元。

    ⚠D1：当前实现 2690 元，需求期望值 1390 元。
    """
    person = Person(house_loans=True)
    assert sut.cal(person, 20_000) == 2_690


@pytest.mark.parametrize(
    ("rent_tier", "deduction", "expected_tax"),
    [
        pytest.param(1, 1_500, 2_590, id="P15-tier1-1500"),
        pytest.param(2, 1_100, 2_670, id="P16-tier2-1100"),
        pytest.param(3, 800, 2_730, id="P17-tier3-800"),
    ],
)
def test_p15_p17_housing_rent_deduction(
    sut: IncomeTaxCalculator,
    rent_tier: int,
    deduction: int,
    expected_tax: int,
) -> None:
    """P15–P17：判定节点 7/8/9 分别为真——住房租金按城市档次扣除。

    三档扣除额分别为 1500 / 1100 / 800 元，分别对应判定节点 7/8/9 的三个独立路径。

    ⚠D1：三个期望值均含 +1300 元缺陷，需求期望值分别为 1290 / 1370 / 1430 元。
    """
    person = Person(is_rent_house=rent_tier)
    assert sut.cal(person, 20_000) == expected_tax


def test_p18_elderly_care_deduction(sut: IncomeTaxCalculator) -> None:
    """P18：判定节点 10 为真——赡养老人，2000 元由兄弟姐妹分摊。

    1 名兄弟姐妹，分摊额 2000 ÷ 2 = 1000 元。

    ⚠D1：当前实现 2690 元，需求期望值 1390 元。
    """
    person = Person(supporting_elderly=True, sibling_num=1)
    assert sut.cal(person, 20_000) == 2_690


# --------------------------------------------------------------------------- #
# 路径集合完整性自检
# --------------------------------------------------------------------------- #


def test_all_eighteen_independent_paths_are_covered() -> None:
    """自检：本文件声明的路径编号恰好覆盖 P1–P18，无缺号、无重号。

    这是一条**元测试**——它校验的不是被测系统，而是测试集本身是否完整，
    避免后续重构时误删某条路径用例而无人察觉。
    """
    declared = {
        "P01",
        "P02",
        "P03",
        "P04",
        "P05",
        "P06",
        "P07",
        "P08",
        "P09",
        "P10",
        "P11",
        "P12",
        "P13",
        "P14",
        # P15–P17 由同一条参数化用例的三个参数承担
        "P15",
        "P16",
        "P17",
        "P18",
    }
    expected = {f"P{index:02d}" for index in range(1, 19)}

    assert declared == expected
