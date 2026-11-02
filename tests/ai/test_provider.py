"""AI 层 · 提供者抽象与模型适配器测试。

覆盖三种提供者：
- ``RuleBasedProvider``：离线规则基线；
- ``RecordingProvider``：测试录制与回放替身；
- ``OpenAiCompatibleProvider``：真实接口适配层（通过 respx 模拟响应，不外呼公网）。
"""

from __future__ import annotations

import httpx
import pytest
import respx

from tax_service.ai.provider import (
    OpenAiCompatibleProvider,
    ProviderError,
    RecordingProvider,
    RuleBasedProvider,
    build_provider,
)

pytestmark = [pytest.mark.platform]

FAKE_URL = "https://llm.test.local"
FAKE_KEY = "test-only-key-sample"
FAKE_MODEL = "test-model-v1"


class TestRuleBasedProvider:
    """规则提供者行为。"""

    def test_name(self) -> None:
        assert RuleBasedProvider().name == "rule-based"

    def test_complete_returns_empty_json(self) -> None:
        assert RuleBasedProvider().complete("prompt") == "{}"


class TestRecordingProvider:
    """录制替身行为。"""

    def test_records_prompts_and_returns_preset(self) -> None:
        rec = RecordingProvider('{"answer": 1}')
        assert rec.name == "recording"

        result = rec.complete("user prompt", system="system prompt")
        assert result == '{"answer": 1}'
        assert len(rec.prompts) == 1
        assert rec.prompts[0] == {"system": "system prompt", "prompt": "user prompt"}

    def test_set_response(self) -> None:
        rec = RecordingProvider()
        rec.set_response('{"updated": true}')
        assert rec.complete("any") == '{"updated": true}'


class TestOpenAiCompatibleProvider:
    """OpenAI 兼容协议客户端。"""

    @respx.mock
    def test_successful_completion(self) -> None:
        route = respx.post(f"{FAKE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"role": "assistant", "content": '{"cases": []}'}}
                    ]
                },
            )
        )

        provider = OpenAiCompatibleProvider(
            base_url=FAKE_URL,
            api_key=FAKE_KEY,
            model=FAKE_MODEL,
        )

        assert provider.name == f"openai-compatible:{FAKE_MODEL}"

        output = provider.complete("生成用例", system="你是一个测试专家")

        assert output == '{"cases": []}'
        assert route.call_count == 1
        last_request = route.calls.last.request
        assert last_request.headers["Authorization"] == f"Bearer {FAKE_KEY}"

    @respx.mock
    def test_http_error_raises_provider_error(self) -> None:
        respx.post(f"{FAKE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )

        provider = OpenAiCompatibleProvider(
            base_url=FAKE_URL,
            api_key=FAKE_KEY,
            model=FAKE_MODEL,
        )

        with pytest.raises(ProviderError, match="模型调用失败"):
            provider.complete("prompt")

    @respx.mock
    def test_malformed_json_raises_provider_error(self) -> None:
        respx.post(f"{FAKE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, text="Not a JSON string")
        )

        provider = OpenAiCompatibleProvider(
            base_url=FAKE_URL,
            api_key=FAKE_KEY,
            model=FAKE_MODEL,
        )

        with pytest.raises(ProviderError, match="模型响应格式异常"):
            provider.complete("prompt")

    @respx.mock
    def test_missing_choices_raises_provider_error(self) -> None:
        respx.post(f"{FAKE_URL}/v1/chat/completions").mock(
            return_value=httpx.Response(200, json={"error": "no choices returned"})
        )

        provider = OpenAiCompatibleProvider(
            base_url=FAKE_URL,
            api_key=FAKE_KEY,
            model=FAKE_MODEL,
        )

        with pytest.raises(ProviderError, match="缺少 choices"):
            provider.complete("prompt")


class TestBuildProvider:
    """工厂方法构造测试。"""

    def test_builds_rule_based_when_no_api_key(self) -> None:
        class FakeSettings:
            ai_api_key = None

        provider = build_provider(FakeSettings())
        assert isinstance(provider, RuleBasedProvider)

    def test_builds_openai_compatible_when_api_key_set(self) -> None:
        class FakeSettings:
            ai_api_key = "sk-fake"
            ai_base_url = "https://api.openai.com"
            ai_model = "gpt-4o-mini"
            ai_timeout_seconds = 10.0

        provider = build_provider(FakeSettings())
        assert isinstance(provider, OpenAiCompatibleProvider)
        assert provider.model == "gpt-4o-mini"
