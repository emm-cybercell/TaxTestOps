"""失败归因。

为什么归因比「失败数」重要
--------------------------
一个批次报出 8 条失败，这个数字本身没有行动价值。真正决定下一步动作的是**它们属于
哪一类**：

- 被测系统缺陷 → 提缺陷单，找业务方；
- 用例本身的问题 → 改断言，找测试自己；
- 环境问题 → 查令牌、查网络，找运维；
- 数据问题 → 检查用例数据是否违反业务约束。

混在一起报「8 条失败」，结果是所有人都觉得该别人看。归因的价值就是把失败
**路由到正确的人**。

规则优先，模型补充
------------------
先跑确定性规则（:meth:`FailureTriager._classify_by_rules`），规则无法判定时才交给
模型。理由：规则可解释、可复现、零成本；模型擅长处理规则覆盖不到的边缘情况，
但不应该用它去做「状态码 401 说明什么」这种有确定答案的事。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from tax_service.ai.provider import LlmProvider, ProviderError, RuleBasedProvider
from tax_service.ai.schemas import (
    FAILURE_CATEGORY_ACTIONS,
    DiagnosisReport,
    FailureDiagnosis,
)

logger = logging.getLogger(__name__)

VALID_CATEGORIES = frozenset(
    {"product_defect", "test_issue", "environment_issue", "data_issue", "unknown"}
)

SYSTEM_PROMPT = (
    "你是一名资深测试开发工程师，擅长根据测试失败现象定位根因。"
    "只输出 JSON，不要输出解释文字。"
)

NETWORK_FAILURE_MARKERS = ("请求失败", "ConnectError", "TimeoutException", "ReadTimeout")
ASSERTION_VALUE_FAILURE_MARKERS = ("期望 eq", "期望 ne", "期望 gt", "期望 ge", "期望 lt")
PATH_RESOLUTION_MARKERS = ("取值失败",)

STATUS_MISMATCH_PREFIX = "状态码不符"


class FailureTriager:
    """失败归因器。"""

    def __init__(self, provider: LlmProvider | None = None) -> None:
        self.provider = provider or RuleBasedProvider()

    @property
    def source(self) -> str:
        return "rule" if isinstance(self.provider, RuleBasedProvider) else "llm"

    # ------------------------------------------------------------------ #
    # 对外入口
    # ------------------------------------------------------------------ #

    def diagnose(
        self,
        *,
        case_name: str,
        message: str,
        status_code: int | None = None,
        expected_status: int | None = None,
        requires_auth: bool = False,
    ) -> FailureDiagnosis:
        """对一条失败做归因。"""
        diagnosis = self._classify_by_rules(
            case_name=case_name,
            message=message,
            status_code=status_code,
            expected_status=expected_status,
            requires_auth=requires_auth,
        )
        if diagnosis.category != "unknown" or self.source == "rule":
            return diagnosis

        return self._classify_with_model(
            case_name=case_name,
            message=message,
            status_code=status_code,
            expected_status=expected_status,
            evidence=diagnosis.evidence,
        )

    def diagnose_run(self, run: Any, items: list[Any]) -> DiagnosisReport:
        """对一次执行的全部失败项做归因。"""
        failures = [item for item in items if item.status.value != "passed"]

        return DiagnosisReport(
            run_id=run.id,
            total_failures=len(failures),
            diagnoses=[
                self.diagnose(
                    case_name=item.case_name,
                    message=item.message,
                    status_code=item.status_code,
                    requires_auth=False,
                )
                for item in failures
            ],
        )

    # ------------------------------------------------------------------ #
    # 规则归因
    # ------------------------------------------------------------------ #

    def _classify_by_rules(
        self,
        *,
        case_name: str,
        message: str,
        status_code: int | None,
        expected_status: int | None,
        requires_auth: bool,
    ) -> FailureDiagnosis:
        evidence = [f"消息：{message}"]
        if status_code is not None:
            evidence.append(f"实际状态码：{status_code}")
        if expected_status is not None:
            evidence.append(f"期望状态码：{expected_status}")

        # 1. 网络类失败 → 环境问题
        if any(marker in message for marker in NETWORK_FAILURE_MARKERS):
            return self._make(
                case_name,
                "environment_issue",
                "请求未能到达目标服务",
                evidence
                + [
                    "可能原因：目标服务未启动、端口不通、网络策略拦截或超时阈值过短",
                ],
                confidence=0.85,
                evidence_only=evidence,
            )

        # 2. 断言取值失败 → 用例问题（断言路径写错）
        if any(marker in message for marker in PATH_RESOLUTION_MARKERS):
            return self._make(
                case_name,
                "test_issue",
                "断言引用的字段在响应中不存在，属于用例定义问题",
                ["可能原因：接口响应结构变更，或断言路径拼写错误"],
                confidence=0.9,
                evidence_only=evidence,
            )

        # 3. 状态码不符 → 按具体码分流
        if message.startswith(STATUS_MISMATCH_PREFIX):
            return self._classify_status_mismatch(
                case_name, status_code, expected_status, requires_auth, evidence
            )

        # 4. 断言值不符 → 被测系统行为与需求不一致
        if any(marker in message for marker in ASSERTION_VALUE_FAILURE_MARKERS):
            return self._make(
                case_name,
                "product_defect",
                "接口返回的字段值与期望不符",
                [
                    "接口可用（状态码正确），但业务结果与需求规格存在差异",
                    "建议对照需求文档确认期望值，再决定是用例问题还是实现缺陷",
                ],
                confidence=0.7,
                evidence_only=evidence,
            )

        return self._make(
            case_name,
            "unknown",
            "现有规则无法判定根因",
            ["需要补充响应快照、日志与复现输入后人工排查"],
            confidence=0.2,
            evidence_only=evidence,
        )

    def _classify_status_mismatch(
        self,
        case_name: str,
        status_code: int | None,
        expected_status: int | None,
        requires_auth: bool,
        evidence: list[str],
    ) -> FailureDiagnosis:
        """按状态码把失败路由到正确的责任人。"""
        # 期望 401 却拿到 2xx：鉴权没生效，属严重缺陷
        if expected_status in {401, 403} and status_code is not None and status_code < 400:
            return self._make(
                case_name,
                "product_defect",
                "受保护接口在缺少凭证时仍然放行",
                ["这是安全缺陷，优先级最高：未认证请求不应拿到业务数据"],
                confidence=0.95,
                evidence_only=evidence,
            )

        # 需要鉴权却拿到 401 → 环境问题（令牌失效）
        if status_code == 401 and requires_auth:
            return self._make(
                case_name,
                "environment_issue",
                "携带的访问令牌无效或已过期",
                [
                    "可能原因：令牌过期、平台未配置 platform_target_token、或目标服务密钥已轮换",
                ],
                confidence=0.9,
                evidence_only=evidence,
            )

        # 期望成功却拿到 422 → 用例数据违反契约
        if status_code == 422 and (expected_status is None or expected_status < 400):
            return self._make(
                case_name,
                "data_issue",
                "请求参数未通过契约校验",
                [
                    "可能原因：用例数据违反业务约束（例如互斥字段同时置真）、"
                    "字段类型不符，或接口契约已变更",
                ],
                confidence=0.85,
                evidence_only=evidence,
            )

        # 5xx → 被测系统缺陷
        if status_code is not None and status_code >= 500:
            return self._make(
                case_name,
                "product_defect",
                "目标服务返回服务端错误",
                ["未处理的异常穿透到了框架层，需要查看服务端日志与堆栈"],
                confidence=0.95,
                evidence_only=evidence,
            )

        # 其余 4xx → 先怀疑用例
        if status_code is not None and 400 <= status_code < 500:
            return self._make(
                case_name,
                "test_issue",
                "目标服务按预期拒绝了请求，但期望状态码与实际不符",
                ["建议核对用例的 expected_status 是否写错"],
                confidence=0.6,
                evidence_only=evidence,
            )

        return self._make(
            case_name,
            "unknown",
            "状态码组合无法由现有规则判定",
            ["请人工核对用例定义与接口文档"],
            confidence=0.3,
            evidence_only=evidence,
        )

    @staticmethod
    def _make(
        case_name: str,
        category: str,
        root_cause: str,
        extra_evidence: list[str],
        *,
        confidence: float,
        evidence_only: list[str] | None = None,
    ) -> FailureDiagnosis:
        evidence = list(evidence_only or []) + extra_evidence
        return FailureDiagnosis(
            case_name=case_name,
            category=category,  # type: ignore[arg-type]
            root_cause=root_cause,
            suggested_action=FAILURE_CATEGORY_ACTIONS.get(category, "人工排查"),
            confidence=confidence,
            evidence=evidence,
            source="rule",
        )

    # ------------------------------------------------------------------ #
    # 模型归因
    # ------------------------------------------------------------------ #

    def _classify_with_model(
        self,
        *,
        case_name: str,
        message: str,
        status_code: int | None,
        expected_status: int | None,
        evidence: list[str],
    ) -> FailureDiagnosis:
        """规则无法判定时交给模型。

        模型返回的类别会被**白名单校验**——它可能编造一个不在枚举内的类别，
        直接信任会让下游的告警路由崩掉。
        """
        prompt = self._build_prompt(case_name, message, status_code, expected_status)
        try:
            raw = self.provider.complete(prompt, system=SYSTEM_PROMPT)
        except ProviderError as exc:
            logger.warning("归因模型调用失败：%s", exc)
            return self._make(
                case_name, "unknown", "模型调用失败，无法归因", [], confidence=0.0
            )

        parsed = self._parse_diagnosis(raw)
        if parsed is None:
            return self._make(
                case_name, "unknown", "模型输出无法解析为归因结论", [], confidence=0.0
            )

        category = str(parsed.get("category", "unknown"))
        if category not in VALID_CATEGORIES:
            logger.warning("模型返回了非法类别 %s，已回落为 unknown", category)
            category = "unknown"

        return FailureDiagnosis(
            case_name=case_name,
            category=category,  # type: ignore[arg-type]
            root_cause=str(parsed.get("root_cause", "模型未给出根因")),
            suggested_action=str(
                parsed.get("suggested_action") or FAILURE_CATEGORY_ACTIONS.get(category, "人工排查")
            ),
            confidence=float(parsed.get("confidence", 0.5)),
            evidence=evidence,
            source="llm",
        )

    @staticmethod
    def _build_prompt(
        case_name: str, message: str, status_code: int | None, expected_status: int | None
    ) -> str:
        return "\n".join(
            [
                "测试失败信息：",
                f"用例名称：{case_name}",
                f"失败消息：{message}",
                f"实际状态码：{status_code}",
                f"期望状态码：{expected_status}",
                "",
                "请判断根因类别，只输出 JSON：",
                '{"category": "product_defect|test_issue|environment_issue|data_issue|unknown", '
                '"root_cause": "一句话说明根因", "suggested_action": "建议的下一步动作", '
                '"confidence": 0.0~1.0}',
                "",
                "类别含义：product_defect=被测系统缺陷，test_issue=用例本身的问题，"
                "environment_issue=测试环境问题，data_issue=测试数据问题。",
            ]
        )

    @staticmethod
    def _parse_diagnosis(raw: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None
