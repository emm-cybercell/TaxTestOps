"""控制流度量：用 AST 静态分析机器校验文档中的圈复杂度结论。

为什么要有这个文件
------------------
``docs/01`` 与 ``docs/02`` 声称「判定节点 17 个、V(G) = 18、基本路径集 18 条」。
这是人工分析的结果，一旦有人修改 ``legacy.py`` 的结构，文档就会悄悄失真。

本文件把该结论变成**可执行的断言**：用 Python ``ast`` 模块解析被测源码，统计判定
节点数与圈复杂度，与文档数字比对。文档写错或代码被改动，CI 立刻报错。

计数规则（与 ``docs/01`` 第 4 节一致）：

- ``if`` / ``elif``：各计 1 个判定节点（``elif`` 在 AST 中表现为 ``orelse`` 里的嵌套 ``If``）；
- ``for`` / ``while``：各计 1 个判定节点；
- ``and`` / ``or``：按操作数个数减 1 计（短路求值构成额外分支）。

圈复杂度 ``V(G) = P + 1``。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = PROJECT_ROOT / "src" / "tax_service"

#: ``docs/01`` 与 ``docs/02`` 中给出的分析结论。
EXPECTED_SUT_DECISION_POINTS = 17
EXPECTED_SUT_CYCLOMATIC_COMPLEXITY = 18

#: 业界常用的圈复杂度上限。超过则视为需要重构的高复杂度方法。
COMPLEXITY_THRESHOLD = 10

pytestmark = pytest.mark.blackbox


# --------------------------------------------------------------------------- #
# AST 度量工具
# --------------------------------------------------------------------------- #


def parse_module(relative_path: str) -> ast.Module:
    """解析 ``src/tax_service`` 下的模块源码。"""
    return ast.parse((SRC_ROOT / relative_path).read_text(encoding="utf-8"))


def collect_functions(tree: ast.Module) -> dict[str, ast.AST]:
    """收集模块内所有函数定义，以函数名索引。"""
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def count_decision_points(function: ast.AST) -> int:
    """统计函数内的判定节点数。"""
    count = 0
    for node in ast.walk(function):
        if isinstance(node, (ast.If, ast.For, ast.While, ast.AsyncFor)):
            count += 1
        elif isinstance(node, ast.BoolOp):
            count += len(node.values) - 1
    return count


def cyclomatic_complexity(function: ast.AST) -> int:
    """按 ``V(G) = P + 1`` 计算圈复杂度。"""
    return count_decision_points(function) + 1


def count_loops(function: ast.AST) -> int:
    """统计函数内的循环语句数量。"""
    return sum(
        1 for node in ast.walk(function) if isinstance(node, (ast.For, ast.While, ast.AsyncFor))
    )


@pytest.fixture(scope="module")
def sut_function() -> ast.AST:
    """被测方法 ``IncomeTaxCalculator.cal`` 的 AST 节点。"""
    functions = collect_functions(parse_module("core/legacy.py"))
    return functions["cal"]


@pytest.fixture(scope="module")
def oracle_functions() -> dict[str, ast.AST]:
    """Oracle 模块的全部函数 AST 节点。"""
    return collect_functions(parse_module("core/oracle.py"))


# --------------------------------------------------------------------------- #
# SUT 的复杂度结论（校验 docs/01 与 docs/02）
# --------------------------------------------------------------------------- #


class TestSutComplexityMatchesDocumentation:
    """``docs/01`` 第 4 节给出的分析结论必须与源码一致。"""

    def test_decision_point_count(self, sut_function: ast.AST) -> None:
        """判定节点恰为 17 个（对应流图节点 ①–⑰）。"""
        assert count_decision_points(sut_function) == EXPECTED_SUT_DECISION_POINTS

    def test_cyclomatic_complexity(self, sut_function: ast.AST) -> None:
        """圈复杂度 V(G) = 17 + 1 = 18，即基本路径集需要 18 条独立路径。"""
        assert cyclomatic_complexity(sut_function) == EXPECTED_SUT_CYCLOMATIC_COMPLEXITY

    def test_is_acyclic(self, sut_function: ast.AST) -> None:
        """方法内不含循环，故控制流图是有向无环图——这是 docs/02 用「判定组合乘积」
        计算全路径数（768 条）的前提。"""
        assert count_loops(sut_function) == 0

    def test_exceeds_complexity_threshold(self, sut_function: ast.AST) -> None:
        """V(G) = 18 高于常用上限 10，属高复杂度方法，是重构建议的量化依据。

        本条断言的是「当前确实超标」这一事实。重构完成后，该断言应连同文档一起更新。
        """
        complexity = cyclomatic_complexity(sut_function)
        assert complexity > COMPLEXITY_THRESHOLD


# --------------------------------------------------------------------------- #
# 重构收益：Oracle 的复杂度分布
# --------------------------------------------------------------------------- #


class TestOracleComplexityIsWithinThreshold:
    """表驱动 + 职责拆分后的复杂度收益（docs/01 第 5 节、docs/02 第 2.4 节的量化依据）。"""

    def test_every_method_is_within_threshold(self, oracle_functions: dict[str, ast.AST]) -> None:
        """Oracle 的每个方法圈复杂度都低于阈值 10。"""
        measured = {name: cyclomatic_complexity(node) for name, node in oracle_functions.items()}
        over_limit = {
            name: value for name, value in measured.items() if value > COMPLEXITY_THRESHOLD
        }

        assert over_limit == {}, f"以下方法仍超过阈值 {COMPLEXITY_THRESHOLD}：{over_limit}"

    def test_max_complexity_is_nine(self, oracle_functions: dict[str, ast.AST]) -> None:
        """Oracle 中最高单方法复杂度为 9（``validate``）。

        与 SUT 的 18 相比下降一半——把 17 个顺序判定拆到 4 个职责单一的方法后，
        单方法复杂度全部回到可控区间。
        """
        measured = {name: cyclomatic_complexity(node) for name, node in oracle_functions.items()}
        assert max(measured.values()) == 9, f"实际分布：{measured}"

    def test_validate_has_the_highest_complexity(
        self, oracle_functions: dict[str, ast.AST]
    ) -> None:
        """复杂度最高的是 ``validate``（9）——集中式输入校验的代价。

        这是有意接受的取舍：把分散在业务逻辑里的 8 个校验判定集中到一处，虽然让
        ``validate`` 成为最复杂的单方法，但使业务计算方法的复杂度大幅下降，且校验
        规则可以独立测试。
        """
        measured = {name: cyclomatic_complexity(node) for name, node in oracle_functions.items()}
        assert max(measured, key=lambda name: measured[name]) == "validate"
