"""单元测试层的公共夹具与数据驱动钩子。"""

from __future__ import annotations

import csv
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from support.marks import current_impl, is_legacy_mode
from tax_service.core import BASIC_DEDUCTION, Person, TaxOracle
from tax_service.core.calculator import Calculator, build_calculator

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

DEFECT_D1_REASON = "D1：应纳税所得额 ≥ 12000 元时速算扣除数少计 1300 元，需求期望值差 1300"


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@pytest.fixture
def sut() -> Calculator:
    """被测系统。

    由 ``TAX_CALCULATOR_IMPL`` 决定用哪个实现：

    - ``legacy``（默认）：实验给定的原实现，保留全部缺陷；
    - ``standard``：按需求规格修正后的实现。

    这样同一套用例在两种模式下都成立——legacy 下暴露缺陷，standard 下证明修复有效。
    """
    return build_calculator(current_impl())


@pytest.fixture
def oracle() -> TaxOracle:
    """测试预言：按需求规格独立实现的参考计算器。"""
    return TaxOracle()


@pytest.fixture
def neutral() -> Person:
    """无任何专项附加扣除的纳税人，用于隔离税率阶梯本身的行为。"""
    return Person()


@pytest.fixture
def salary_for() -> Callable[[int], int]:
    """返回「应纳税所得额 -> 税前工资」换算函数（无专项附加扣除时）。"""

    def _salary_for(taxable_income: int) -> int:
        return taxable_income + BASIC_DEDUCTION

    return _salary_for


# --------------------------------------------------------------------------- #
# 数据驱动：把 CSV 用例表注入到声明了 ``boundary_case`` 参数的测试中
# --------------------------------------------------------------------------- #


def load_boundary_cases() -> list[dict[str, Any]]:
    """从 ``tests/data/boundary_cases.csv`` 读取边界值用例表。"""
    path = DATA_DIR / "boundary_cases.csv"
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    return [
        {
            "case_id": row["case_id"],
            "taxable_income": int(row["taxable_income"]),
            "expected_tax": int(row["expected_tax"]),
            "affected_by_defect": row["affected_by_defect"] == "yes",
            "description": row["description"],
        }
        for row in rows
    ]


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """把外部用例表参数化进测试函数，实现「用例数据与代码分离」。

    任何声明了 ``boundary_case`` 形参的测试都会自动按 CSV 中的每一行展开为一条
    独立用例；受缺陷 D1 影响的行在 legacy 模式下自动打上 ``xfail(strict=True)``——
    缺陷修复后这些用例会由 xfail 转为「意外通过」而失败，从而强制我们同步更新用例表。

    在 standard 模式下不加任何标记，这些行必须**真正通过**。
    """
    if "boundary_case" not in metafunc.fixturenames:
        return

    legacy = is_legacy_mode()
    metafunc.parametrize(
        "boundary_case",
        [
            pytest.param(
                case,
                id=f"{case['case_id']}-{case['description']}",
                marks=(
                    [pytest.mark.xfail(strict=True, reason=DEFECT_D1_REASON)]
                    if case["affected_by_defect"] and legacy
                    else []
                ),
            )
            for case in load_boundary_cases()
        ],
    )
