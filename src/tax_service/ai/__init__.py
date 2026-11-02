"""AI 增强：用例生成与失败归因。

设计立场
--------
本模块的定位不是「用大模型替代测试工程师」，而是**把重复劳动交出去、把判断权留下**：

- 模型负责**提议**（生成候选用例、给出根因假设）；
- 代码负责**校验**（结构合法性、是否符合 OpenAPI 契约、断言是否可求值）；
- 人负责**决策**（低置信度的候选标记为待评审，不自动进流水线）。

这样做的理由很实际：一个会生成「看起来像用例、但断言根本跑不通」的模型，
如果被无条件信任，只会把噪声灌进流水线，比没有更糟。因此
:mod:`tax_service.ai.case_generator` 里所有产出都必须过一遍校验器，
并且 :attr:`~tax_service.ai.schemas.GeneratedCase.needs_review` 会把
「结构合法但语义可疑」的候选单独标出来。

离线可用
--------
默认提供者是 :class:`RuleBasedProvider`：**不调用任何外部服务**，基于 OpenAPI 规范
做确定性的用例推导。它让 AI 功能的测试无需 API key、无需网络即可运行，
同时也是衡量「接入大模型到底带来了多少增益」的基线。
"""

from tax_service.ai.case_generator import TestCaseGenerator
from tax_service.ai.provider import (
    LlmProvider,
    OpenAiCompatibleProvider,
    RuleBasedProvider,
    build_provider,
)
from tax_service.ai.schemas import (
    CaseGenerationReport,
    FailureDiagnosis,
    GeneratedCase,
)
from tax_service.ai.triager import FailureTriager

__all__ = [
    "CaseGenerationReport",
    "FailureDiagnosis",
    "FailureTriager",
    "GeneratedCase",
    "LlmProvider",
    "OpenAiCompatibleProvider",
    "RuleBasedProvider",
    "TestCaseGenerator",
    "build_provider",
]
