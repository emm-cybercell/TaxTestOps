"""AI 层 · 用例生成。

测试重点
--------
1. **规则推导的确定性**——同一输入必须产出完全相同的用例集；
2. **校验器的把关能力**——模型编造的路径、方法、状态码、操作符必须被拦下；
3. **低置信度标记**——语义可疑的候选要进人工评审队列，不能静默进流水线。
"""

from __future__ import annotations

import json

import pytest

from support.platform_cases import case_payload  # noqa: F401 - 供 conftest 引用约定一致
from tax_service.ai.case_generator import CaseValidationError, TestCaseGenerator
from tax_service.ai.provider import ProviderError, RecordingProvider, RuleBasedProvider
from tax_service.ai.schemas import CaseGenerationReport

pytestmark = [pytest.mark.platform]


# --------------------------------------------------------------------------- #
# 规则推导
# --------------------------------------------------------------------------- #


class TestRuleBasedGeneration:
    """离线规则推导（默认模式，CI 里跑的就是它）。"""

    def test_generates_happy_path_and_negative_cases(
        self, openapi_document: dict, openapi_document_copy: None = None
    ) -> None:
        generator = TestCaseGenerator(RuleBasedProvider(openapi_document), openapi_document)

        report = generator.from_openapi("/api/v1/tax/calculate", "POST")

        assert isinstance(report, CaseGenerationReport)
        assert report.source == "rule"
        assert report.accepted_count > 0

        names = [case.name for case in report.generated]
        assert any("正常请求" in name for name in names), "缺少正常路径用例"
        assert any("缺少必填字段" in name for name in names), "缺少必填缺失用例"

    def test_is_deterministic(self, openapi_document: dict) -> None:
        """同一输入必须产出完全相同的用例集。

        确定性是规则提供者的核心价值：CI 里跑两次结果不同，这套生成器就没有
        存在意义了。
        """
        first = TestCaseGenerator(RuleBasedProvider(openapi_document), openapi_document)
        second = TestCaseGenerator(RuleBasedProvider(openapi_document), openapi_document)

        report_a = first.from_openapi("/api/v1/tax/calculate", "POST")
        report_b = second.from_openapi("/api/v1/tax/calculate", "POST")

        assert [c.model_dump() for c in report_a.generated] == [
            c.model_dump() for c in report_b.generated
        ]

    def test_generates_boundary_cases_for_bounded_fields(self, openapi_document: dict) -> None:
        generator = TestCaseGenerator(RuleBasedProvider(openapi_document), openapi_document)

        report = generator.from_openapi("/api/v1/tax/calculate", "POST")
        names = [case.name for case in report.generated]

        assert any("边界值" in name for name in names), "有界数值字段应产生边界值用例"
        assert any("低于下界" in name for name in names), "应产生低于下界的负向用例"

    def test_generates_invalid_enum_case(self, openapi_document: dict) -> None:
        generator = TestCaseGenerator(RuleBasedProvider(openapi_document), openapi_document)

        report = generator.from_openapi("/api/v1/tax/calculate", "POST")
        enum_cases = [c for c in report.generated if "非法枚举" in c.name]

        assert enum_cases, "枚举字段应产生非法值用例"
        assert enum_cases[0].expected_status == 422

    def test_generates_unknown_field_case_for_closed_schema(
        self, openapi_document: dict
    ) -> None:
        generator = TestCaseGenerator(RuleBasedProvider(openapi_document), openapi_document)

        report = generator.from_openapi("/api/v1/tax/calculate", "POST")
        unknown_cases = [c for c in report.generated if "未知字段" in c.name]

        assert unknown_cases, "封闭 schema 应产生未知字段用例"
        assert "__unexpected__" in (unknown_cases[0].payload or {})

    def test_generates_unauthenticated_case_for_protected_operation(
        self, openapi_document: dict
    ) -> None:
        generator = TestCaseGenerator(RuleBasedProvider(openapi_document), openapi_document)

        report = generator.from_openapi("/api/v1/auth/me", "GET")
        auth_cases = [c for c in report.generated if "未认证" in c.name]

        assert auth_cases, "受保护接口应产生未认证访问用例"
        assert auth_cases[0].expected_status == 401
        assert auth_cases[0].requires_auth is False

    def test_respects_max_cases_limit(self, openapi_document: dict) -> None:
        generator = TestCaseGenerator(
            RuleBasedProvider(openapi_document), openapi_document, max_cases_per_operation=3
        )

        report = generator.from_openapi("/api/v1/tax/calculate", "POST")

        assert report.accepted_count <= 3

    def test_unknown_path_is_rejected(self, openapi_document: dict) -> None:
        generator = TestCaseGenerator(RuleBasedProvider(openapi_document), openapi_document)

        with pytest.raises(CaseValidationError, match="不存在路径"):
            generator.from_openapi("/api/v1/not/declared", "GET")

    def test_unknown_method_is_rejected(self, openapi_document: dict) -> None:
        generator = TestCaseGenerator(RuleBasedProvider(openapi_document), openapi_document)

        with pytest.raises(CaseValidationError, match="未声明方法"):
            generator.from_openapi("/healthz", "POST")

    def test_requirement_text_is_rejected_by_rule_provider(
        self, openapi_document: dict
    ) -> None:
        """规则提供者无法理解自然语言——必须诚实说明，而不是假装生成成功。"""
        generator = TestCaseGenerator(RuleBasedProvider(openapi_document), openapi_document)

        report = generator.from_requirement("支持批量导出申报记录", path="/api/v1/tax/calculate")

        assert report.accepted_count == 0
        assert report.rejected_count == 1
        assert "ai_api_key" in report.rejected[0]["reason"]


# --------------------------------------------------------------------------- #
# 模型提议 + 校验器把关
# --------------------------------------------------------------------------- #


def _model_output(cases: list[dict]) -> str:
    return json.dumps(cases, ensure_ascii=False)


class TestModelGenerationWithValidation:
    """模型输出必须过校验器——这条链路是「AI 增强」的信任边界。"""

    def test_valid_model_output_is_accepted(self, openapi_document: dict) -> None:
        provider = RecordingProvider(
            _model_output(
                [
                    {
                        "name": "工资为零不产生税额",
                        "method": "POST",
                        "path": "/api/v1/tax/calculate",
                        "payload": {"salary": 0, "persist": False},
                        "expected_status": 200,
                        "assertions": [{"path": "tax_amount", "op": "eq", "value": 0}],
                        "tags": ["llm", "boundary"],
                        "requires_auth": True,
                        "rationale": "下边界工资应免税",
                        "confidence": 0.9,
                    }
                ]
            )
        )
        generator = TestCaseGenerator(provider, openapi_document)

        report = generator.from_openapi("/api/v1/tax/calculate", "POST")

        assert report.source == "llm"
        assert report.accepted_count == 1
        assert report.generated[0].needs_review is False

    def test_fabricated_path_is_rejected(self, openapi_document: dict) -> None:
        """模型编造不存在的路径必须被拦下——这是校验器最重要的职责。"""
        provider = RecordingProvider(
            _model_output(
                [
                    {
                        "name": "调用一个不存在的接口",
                        "method": "GET",
                        "path": "/api/v1/admin/secret",
                        "expected_status": 200,
                        "rationale": "尝试越权",
                        "confidence": 0.8,
                    }
                ]
            )
        )
        generator = TestCaseGenerator(provider, openapi_document)

        report = generator.from_openapi("/api/v1/tax/calculate", "POST")

        assert report.accepted_count == 0
        assert report.rejected_count == 1
        assert "未在 OpenAPI 文档中声明" in report.rejected[0]["reason"]

    def test_fabricated_method_is_rejected(self, openapi_document: dict) -> None:
        provider = RecordingProvider(
            _model_output(
                [
                    {
                        "name": "用 DELETE 打只读接口",
                        "method": "DELETE",
                        "path": "/healthz",
                        "expected_status": 200,
                        "rationale": "错误方法",
                        "confidence": 0.8,
                    }
                ]
            )
        )
        generator = TestCaseGenerator(provider, openapi_document)

        report = generator.from_openapi("/healthz", "GET")

        assert report.accepted_count == 0
        assert "未声明方法" in report.rejected[0]["reason"]

    def test_undeclared_status_code_is_rejected(self, openapi_document: dict) -> None:
        provider = RecordingProvider(
            _model_output(
                [
                    {
                        "name": "期望一个从未声明的状态码",
                        "method": "GET",
                        "path": "/healthz",
                        "expected_status": 418,
                        "rationale": "我是茶壶",
                        "confidence": 0.8,
                    }
                ]
            )
        )
        generator = TestCaseGenerator(provider, openapi_document)

        report = generator.from_openapi("/healthz", "GET")

        assert report.accepted_count == 0
        assert "未在该操作中声明" in report.rejected[0]["reason"]

    def test_unknown_assertion_operator_is_rejected(self, openapi_document: dict) -> None:
        provider = RecordingProvider(
            _model_output(
                [
                    {
                        "name": "用了不存在的操作符",
                        "method": "GET",
                        "path": "/healthz",
                        "expected_status": 200,
                        "assertions": [{"path": "status", "op": "regex", "value": "ok"}],
                        "rationale": "正则匹配",
                        "confidence": 0.8,
                    }
                ]
            )
        )
        generator = TestCaseGenerator(provider, openapi_document)

        report = generator.from_openapi("/healthz", "GET")

        assert report.accepted_count == 0
        assert "未知断言操作符" in report.rejected[0]["reason"]

    def test_low_confidence_requires_review(self, openapi_document: dict) -> None:
        provider = RecordingProvider(
            _model_output(
                [
                    {
                        "name": "低置信度候选",
                        "method": "GET",
                        "path": "/healthz",
                        "expected_status": 200,
                        "rationale": "不太确定",
                        "confidence": 0.4,
                    }
                ]
            )
        )
        generator = TestCaseGenerator(provider, openapi_document)

        report = generator.from_openapi("/healthz", "GET")

        assert report.accepted_count == 1
        assert report.generated[0].needs_review is True
        assert report.review_count == 1

    def test_missing_rationale_requires_review(self, openapi_document: dict) -> None:
        """没有理由的候选同样要人工过目——说不出为什么的用例不值得自动信任。"""
        provider = RecordingProvider(
            _model_output(
                [
                    {
                        "name": "没有理由的候选",
                        "method": "GET",
                        "path": "/healthz",
                        "expected_status": 200,
                        "confidence": 0.95,
                    }
                ]
            )
        )
        generator = TestCaseGenerator(provider, openapi_document)

        report = generator.from_openapi("/healthz", "GET")

        assert report.accepted_count == 1
        assert report.generated[0].needs_review is True

    def test_malformed_json_is_rejected_not_raised(self, openapi_document: dict) -> None:
        """模型输出不是 JSON 时应记为拒绝，而不是让整个请求 500。"""
        provider = RecordingProvider("抱歉，我无法输出 JSON。")
        generator = TestCaseGenerator(provider, openapi_document)

        report = generator.from_openapi("/healthz", "GET")

        assert report.accepted_count == 0
        assert report.rejected_count == 1
        assert "不是合法 JSON" in report.rejected[0]["reason"]

    def test_non_array_output_is_rejected(self, openapi_document: dict) -> None:
        provider = RecordingProvider(json.dumps({"oops": "single object"}))
        generator = TestCaseGenerator(provider, openapi_document)

        report = generator.from_openapi("/healthz", "GET")

        assert report.accepted_count == 0

    def test_wrapped_object_with_cases_key_is_accepted(self, openapi_document: dict) -> None:
        """模型常见输出形态 ``{"cases": [...]}`` 也应被接受。"""
        provider = RecordingProvider(
            json.dumps(
                {
                    "cases": [
                        {
                            "name": "包装在对象里的输出",
                            "method": "GET",
                            "path": "/healthz",
                            "expected_status": 200,
                            "rationale": "标准格式变体",
                            "confidence": 0.9,
                        }
                    ]
                }
            )
        )
        generator = TestCaseGenerator(provider, openapi_document)

        report = generator.from_openapi("/healthz", "GET")

        assert report.accepted_count == 1

    def test_model_failure_is_reported_not_raised(self, openapi_document: dict) -> None:
        class BrokenProvider(RecordingProvider):
            def complete(self, prompt: str, *, system: str = "") -> str:
                raise ProviderError("网络超时")

        generator = TestCaseGenerator(BrokenProvider(), openapi_document)

        report = generator.from_openapi("/healthz", "GET")

        assert report.accepted_count == 0
        assert "模型调用失败" in report.rejected[0]["reason"]

    def test_prompt_contains_operation_definition(self, openapi_document: dict) -> None:
        """提示词必须包含接口定义，否则模型是在盲猜。"""
        provider = RecordingProvider(_model_output([]))
        generator = TestCaseGenerator(provider, openapi_document)

        generator.from_openapi("/api/v1/tax/calculate", "POST")

        assert len(provider.prompts) == 1
        prompt = provider.prompts[0]["prompt"]
        assert "/api/v1/tax/calculate" in prompt
        assert "operationId" in prompt or "requestBody" in prompt

    def test_from_requirement_with_model(self, openapi_document: dict) -> None:
        """模型模式下从需求文本成功生成用例。"""
        provider = RecordingProvider(
            _model_output(
                [
                    {
                        "name": "按需求文档小李算例",
                        "method": "POST",
                        "path": "/api/v1/tax/calculate",
                        "payload": {"salary": 8000, "persist": False},
                        "expected_status": 200,
                        "assertions": [{"path": "tax_amount", "op": "eq", "value": 0}],
                        "rationale": "小李多项扣除后税额为 0",
                        "confidence": 0.9,
                    }
                ]
            )
        )
        generator = TestCaseGenerator(provider, openapi_document)

        report = generator.from_requirement(
            "小李月薪 10000 租房教育赡养后税额为 0",
            path="/api/v1/tax/calculate",
            method="POST",
        )

        assert report.source == "llm"
        assert report.accepted_count == 1
        assert report.generated[0].name == "按需求文档小李算例"
        assert "需求描述" in provider.prompts[0]["prompt"]

    def test_to_platform_payload_format(self, openapi_document: dict) -> None:
        """转换为平台用例 payload 格式。"""
        provider = RecordingProvider(
            _model_output(
                [
                    {
                        "name": "待导入用例",
                        "method": "GET",
                        "path": "/healthz",
                        "expected_status": 200,
                        "assertions": [{"path": "status", "op": "eq", "value": "ok"}],
                        "rationale": "验证服务健康",
                        "confidence": 0.9,
                    }
                ]
            )
        )
        generator = TestCaseGenerator(provider, openapi_document)
        report = generator.from_openapi("/healthz", "GET")
        case = report.generated[0]

        payload = TestCaseGenerator.to_platform_payload(case)

        assert payload["name"] == "待导入用例"
        assert payload["method"] == "GET"
        assert payload["path"] == "/healthz"
        assert payload["expected_status"] == 200
        assert len(payload["assertions"]) == 1
        assert "confidence" not in payload  # 不泄露生成侧元数据

