"""AI 层 · 失败归因。

测试重点
--------
1. **规则归因的路由正确性**——每类失败必须落到「该找谁」的类别上；
2. **安全相关的高置信判定**——「受保护接口放行未认证请求」必须被判为产品缺陷；
3. **模型输出的白名单校验**——模型编造类别时必须回落到 ``unknown``。
"""

from __future__ import annotations

import json

import pytest

from tax_service.ai.provider import ProviderError, RecordingProvider
from tax_service.ai.schemas import DiagnosisReport
from tax_service.ai.triager import FailureTriager

pytestmark = [pytest.mark.platform]


def diagnose(**kwargs) -> object:
    """便捷封装：默认用规则提供者。"""
    return FailureTriager().diagnose(case_name="测试用例", **kwargs)


class TestRuleBasedTriage:
    """确定性归因规则。"""

    def test_network_failure_is_environment_issue(self) -> None:
        diagnosis = diagnose(message="请求失败：ConnectError('连接被拒绝')")

        assert diagnosis.category == "environment_issue"
        assert diagnosis.confidence >= 0.8
        assert "目标服务" in diagnosis.root_cause

    def test_timeout_is_environment_issue(self) -> None:
        diagnosis = diagnose(message="请求失败：ReadTimeout('超时')")

        assert diagnosis.category == "environment_issue"

    def test_assertion_path_error_is_test_issue(self) -> None:
        """断言取值失败说明用例写错了，不是产品的错。"""
        diagnosis = diagnose(message="断言[0] 取值失败：路径 tax 中不存在字段 amount")

        assert diagnosis.category == "test_issue"
        assert diagnosis.confidence >= 0.85

    def test_status_mismatch_with_auth_expecting_401_is_defect(self) -> None:
        """受保护接口放行未认证请求——安全缺陷，必须路由到 product_defect。"""
        diagnosis = diagnose(
            message="状态码不符：期望 401，实际 200",
            status_code=200,
            expected_status=401,
        )

        assert diagnosis.category == "product_defect"
        assert diagnosis.confidence >= 0.9
        assert "安全缺陷" in "".join(diagnosis.evidence)

    def test_status_mismatch_401_with_auth_is_environment(self) -> None:
        """带鉴权却拿到 401 → 令牌失效，属环境问题。"""
        diagnosis = diagnose(
            message="状态码不符：期望 200，实际 401",
            status_code=401,
            expected_status=200,
            requires_auth=True,
        )

        assert diagnosis.category == "environment_issue"
        assert "令牌" in diagnosis.root_cause

    def test_unexpected_422_is_data_issue(self) -> None:
        """期望成功却拿到 422 → 用例数据违反契约。"""
        diagnosis = diagnose(
            message="状态码不符：期望 200，实际 422",
            status_code=422,
            expected_status=200,
        )

        assert diagnosis.category == "data_issue"
        assert "互斥" in diagnosis.suggested_action or "约束" in diagnosis.suggested_action

    def test_5xx_is_product_defect(self) -> None:
        diagnosis = diagnose(
            message="状态码不符：期望 200，实际 500",
            status_code=500,
            expected_status=200,
        )

        assert diagnosis.category == "product_defect"
        assert diagnosis.confidence >= 0.9

    def test_other_4xx_is_likely_test_issue(self) -> None:
        diagnosis = diagnose(
            message="状态码不符：期望 200，实际 404",
            status_code=404,
            expected_status=200,
        )

        assert diagnosis.category == "test_issue"

    def test_value_assertion_failure_is_product_defect(self) -> None:
        """状态码正确但值不对——先怀疑实现，再由人核对需求。"""
        diagnosis = diagnose(
            message="断言[0] tax_amount 期望 eq 1590，实际 2890",
            status_code=200,
            expected_status=200,
        )

        assert diagnosis.category == "product_defect"
        assert diagnosis.confidence >= 0.6

    def test_unknown_message_falls_back(self) -> None:
        diagnosis = diagnose(message="某种完全无法识别的失败")

        assert diagnosis.category == "unknown"
        assert diagnosis.suggested_action

    def test_every_diagnosis_carries_evidence(self) -> None:
        """归因必须附证据——不可核查的归因比没有归因更糟。"""
        diagnosis = diagnose(
            message="状态码不符：期望 200，实际 500",
            status_code=500,
            expected_status=200,
        )

        assert diagnosis.evidence
        assert any("500" in item for item in diagnosis.evidence)

    def test_action_mapping_covers_all_categories(self) -> None:
        """每个类别都必须有明确的下一步动作。

        样例消息必须带上对应的 ``status_code`` —— 规则归因依赖状态码分流，
        只给消息不给状态码会落到 ``unknown``（这正是另一条用例验证的行为）。
        """
        samples: dict[str, tuple[str, int | None]] = {
            "product_defect": ("状态码不符：期望 200，实际 500", 500),
            "test_issue": ("断言[0] 取值失败：路径 a 中不存在字段 b", None),
            "environment_issue": ("请求失败：ConnectError('x')", None),
            "data_issue": ("状态码不符：期望 200，实际 422", 422),
            "unknown": ("无法识别", None),
        }

        for category, (message, status_code) in samples.items():
            diagnosis = FailureTriager().diagnose(
                case_name="X",
                message=message,
                status_code=status_code,
                expected_status=200 if status_code else None,
            )

            assert diagnosis.category == category, (
                f"{category} 的样例消息被归因为 {diagnosis.category}"
            )
            assert diagnosis.suggested_action


class TestModelTriage:
    """模型归因（规则无法判定时启用）。"""

    def _unknown_first(self) -> str:
        return "某种完全无法识别的失败"

    def test_model_output_is_accepted(self) -> None:
        provider = RecordingProvider(
            json.dumps(
                {
                    "category": "product_defect",
                    "root_cause": "幂等键未按用户隔离",
                    "suggested_action": "检查唯一约束是否包含 user_id",
                    "confidence": 0.8,
                }
            )
        )
        triager = FailureTriager(provider)

        diagnosis = triager.diagnose(case_name="并发幂等", message=self._unknown_first())

        assert diagnosis.source == "llm"
        assert diagnosis.category == "product_defect"
        assert "幂等" in diagnosis.root_cause

    def test_fabricated_category_falls_back_to_unknown(self) -> None:
        """模型编造类别必须被白名单拦下——下游告警路由依赖枚举值合法。"""
        provider = RecordingProvider(
            json.dumps({"category": "cosmic_ray", "root_cause": "宇宙射线", "confidence": 0.9})
        )
        triager = FailureTriager(provider)

        diagnosis = triager.diagnose(case_name="X", message=self._unknown_first())

        assert diagnosis.category == "unknown"
        assert diagnosis.source == "llm"

    def test_model_failure_falls_back_to_unknown(self) -> None:
        class Broken(RecordingProvider):
            def complete(self, prompt: str, *, system: str = "") -> str:
                raise ProviderError("限流")

        diagnosis = FailureTriager(Broken()).diagnose(case_name="X", message=self._unknown_first())

        assert diagnosis.category == "unknown"
        assert diagnosis.confidence == 0.0

    def test_non_json_output_falls_back(self) -> None:
        provider = RecordingProvider("我觉得可能是网络问题吧")
        diagnosis = FailureTriager(provider).diagnose(case_name="X", message=self._unknown_first())

        assert diagnosis.category == "unknown"

    def test_rules_take_priority_over_model(self) -> None:
        """可判定的失败不应浪费模型调用。

        用 RecordingProvider 验证：可判定场景下提示词列表应为空。
        """
        provider = RecordingProvider(json.dumps({"category": "unknown"}))
        triager = FailureTriager(provider)

        diagnosis = triager.diagnose(
            case_name="X",
            message="状态码不符：期望 200，实际 500",
            status_code=500,
            expected_status=200,
        )

        assert provider.prompts == []
        assert diagnosis.source == "rule"


class TestRunDiagnosis:
    """整批失败的归因汇总。"""

    def test_by_category_routes_alerts(self) -> None:
        """汇总必须按类别分组——这是告警路由的数据基础。"""

        class FakeItem:
            def __init__(self, name: str, status: str, message: str) -> None:
                self.case_name = name
                self.status = type("S", (), {"value": status})()
                self.message = message
                self.status_code = None
                self.expected_status = None

        class FakeRun:
            id = 1

        items = [
            FakeItem("环境挂了", "error", "请求失败：ConnectError('x')"),
            FakeItem("值不对", "failed", "断言[0] tax_amount 期望 eq 1，实际 2"),
            FakeItem("断言路径错", "failed", "断言[0] 取值失败：路径 a 不存在"),
        ]

        report = FailureTriager().diagnose_run(FakeRun(), items)

        assert isinstance(report, DiagnosisReport)
        assert report.total_failures == 3
        assert report.by_category == {
            "environment_issue": 1,
            "product_defect": 1,
            "test_issue": 1,
        }

    def test_passing_items_are_ignored(self) -> None:
        class FakeItem:
            def __init__(self) -> None:
                self.case_name = "通过的用例"
                self.status = type("S", (), {"value": "passed"})()
                self.message = ""
                self.status_code = 200

        class FakeRun:
            id = 2

        report = FailureTriager().diagnose_run(FakeRun(), [FakeItem()])

        assert report.total_failures == 0
        assert report.diagnoses == []
