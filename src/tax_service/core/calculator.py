"""个税计算器的构造与选择。

服务层不直接依赖具体实现，而是通过 :func:`build_calculator` 按配置构造。
这样同一套接口/UI/性能测试可以在两种实现上运行：

- ``legacy``：实验给定的原实现，保留全部缺陷 —— 默认值；
- ``standard``：按需求规格修正后的实现。

**为什么要做这个开关**：只会在缺陷上「报红」的测试集，无法证明它真的在验证正确性。
切到 ``standard`` 后所有需求符合性用例必须真正通过，才能证明测试断言本身是有效的。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tax_service.core.legacy import IncomeTaxCalculator
from tax_service.core.oracle import TaxOracle
from tax_service.core.person import Person


@runtime_checkable
class Calculator(Protocol):
    """个税计算器接口。"""

    def cal(self, person: Person, salary: int) -> int:
        """计算月度应纳税额。"""
        ...


def build_calculator(impl: str = "legacy") -> Calculator:
    """按名称构造个税计算器。

    :param impl: ``"legacy"`` 或 ``"standard"``。
    :raises ValueError: 名称无法识别。
    """
    if impl == "legacy":
        return IncomeTaxCalculator()
    if impl == "standard":
        return TaxOracle()
    raise ValueError(f"未知的个税计算实现：{impl!r}（可选：legacy / standard）")


def expected_calculator() -> TaxOracle:
    """返回「需求期望」计算器。

    测试与诊断用途：用于判断当前实现是否符合需求规格。**不得**用于生产响应，
    否则等于把答案随响应一起返回。
    """
    return TaxOracle()
