"""AI 能力的数据模型。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

GenerationSource = Literal["rule", "llm"]

FailureCategory = Literal[
    "product_defect",
    "test_issue",
    "environment_issue",
    "data_issue",
    "unknown",
]

FAILURE_CATEGORY_LABELS: dict[str, str] = {
    "product_defect": "被测系统缺陷",
    "test_issue": "用例本身的问题",
    "environment_issue": "测试环境问题",
    "data_issue": "测试数据问题",
    "unknown": "无法判定",
}

FAILURE_CATEGORY_ACTIONS: dict[str, str] = {
    "product_defect": "提缺陷单给业务方，附上复现输入与期望值",
    "test_issue": "修正断言或等待条件，确认是否属于特征化断言",
    "environment_issue": "检查目标服务可用性、令牌有效期与网络策略",
    "data_issue": "检查用例数据是否满足业务约束（如互斥字段同时置真）",
    "unknown": "补充日志与响应快照后人工排查",
}


class Assertion(BaseModel):
    """一条断言（与平台用例定义同构）。"""

    model_config = ConfigDict(extra="forbid")

    path: str = ""
    op: str = "eq"
    value: Any = None


class GeneratedCase(BaseModel):
    """一条候选用例。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    method: str = "GET"
    path: str
    payload: dict[str, Any] | None = None
    expected_status: int = 200
    assertions: list[Assertion] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    requires_auth: bool = False

    rationale: str = ""
    """为什么生成这条用例——评审时最需要看的就是这一项。"""

    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    source: GenerationSource = "rule"
    needs_review: bool = False
    """是否需要人工确认后才能启用。低置信度或含推导假设的候选会被标记。"""


class CaseGenerationReport(BaseModel):
    """一次生成的结果汇总。"""

    model_config = ConfigDict(extra="forbid")

    source: GenerationSource
    target: str
    """生成依据，例如 ``POST /api/v1/tax/calculate`` 或需求文本摘要。"""

    generated: list[GeneratedCase] = Field(default_factory=list)
    rejected: list[dict[str, str]] = Field(default_factory=list)
    """被校验器拒绝的候选，附拒绝原因——保留它们才能评估生成质量。"""

    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def accepted_count(self) -> int:
        return len(self.generated)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def rejected_count(self) -> int:
        return len(self.rejected)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def review_count(self) -> int:
        return sum(1 for case in self.generated if case.needs_review)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def acceptance_rate(self) -> float:
        """校验通过率。

        这是评估生成器质量最直接的一个指标：低通过率说明提示词或模型不合适，
        而不只是「生成得少」。
        """
        total = self.accepted_count + self.rejected_count
        return round(self.accepted_count * 100 / total, 1) if total else 0.0


class FailureDiagnosis(BaseModel):
    """一次失败归因的结论。"""

    model_config = ConfigDict(extra="forbid")

    case_name: str
    category: FailureCategory
    root_cause: str
    suggested_action: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)
    """支撑结论的原始证据（状态码、响应片段、消息）。归因必须可核查。"""

    source: GenerationSource = "rule"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def category_label(self) -> str:
        return FAILURE_CATEGORY_LABELS.get(self.category, self.category)


class DiagnosisReport(BaseModel):
    """一次执行的归因汇总。"""

    model_config = ConfigDict(extra="forbid")

    run_id: int
    total_failures: int
    diagnoses: list[FailureDiagnosis] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def by_category(self) -> dict[str, int]:
        """按类别统计失败数。

        这是告警路由的依据：``product_defect`` 找业务方，``environment_issue`` 找运维，
        ``test_issue`` 找测试自己。混在一起报只会让所有人都不看。
        """
        counts: dict[str, int] = {}
        for diagnosis in self.diagnoses:
            counts[diagnosis.category] = counts.get(diagnosis.category, 0) + 1
        return counts
