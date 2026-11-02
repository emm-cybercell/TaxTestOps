"""个税计算核心域。

模块分工（这是本项目「白盒 vs 黑盒」叙事的基础）：

- :mod:`tax_service.core.legacy` —— **被测系统（SUT）**。对实验给定 Java 实现的
  逐行忠实移植，刻意不修复任何缺陷，控制结构与 ``docs/01`` 的控制流图一一对应。
- :mod:`tax_service.core.oracle` —— **测试预言（Oracle）**。按国标月度税率表
  以表驱动方式独立实现，用于判定 SUT 输出是否符合需求。
- :mod:`tax_service.core.policy` —— 政策常量与国标税率表。
- :mod:`tax_service.core.person` —— 纳税人数据模型。
"""

from tax_service.core.legacy import IncomeTaxCalculator
from tax_service.core.oracle import TaxOracle
from tax_service.core.person import Person
from tax_service.core.policy import (
    BASIC_DEDUCTION,
    LEGACY_MONTHLY_BRACKETS,
    STANDARD_MONTHLY_BRACKETS,
    TaxBracket,
)

__all__ = [
    "BASIC_DEDUCTION",
    "IncomeTaxCalculator",
    "LEGACY_MONTHLY_BRACKETS",
    "Person",
    "STANDARD_MONTHLY_BRACKETS",
    "TaxBracket",
    "TaxOracle",
]
