"""模型提供者抽象。

三个实现，各有明确用途
----------------------

============================  ==================  ====================================
提供者                        是否联网            用途
============================  ==================  ====================================
``RuleBasedProvider``         否                  默认值。确定性、可测、作为基线
``OpenAiCompatibleProvider``  是                  接真实模型（OpenAI / DeepSeek / 通义 / 本地 vLLM）
``RecordingProvider``         否                  测试用：记录提示词并回放固定输出
============================  ==================  ====================================

**默认不联网**是有意为之：AI 功能的测试如果依赖外部模型，就会变成「今天绿明天红」
且原因不可复现的噪声源。把模型调用隔离在接口之后，测试用替身、生产用真模型，
是让 AI 能力可工程化的前提。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 30.0

#: 常见模型名称别名归一化（部分网关对大小写敏感，如微信 chatapi 严格匹配 Deepseek-v4-flash）
MODEL_ALIASES: dict[str, str] = {
    "deepseek": "Deepseek-v4-flash",
    "deepseek-v4-flash": "Deepseek-v4-flash",
    "glm": "GLM-5.2",
    "glm-5.2": "GLM-5.2",
}


def resolve_chat_endpoint(base_url: str) -> str:
    """根据 base_url 智能拼装完整的 chat/completions 请求地址。

    兼容两种常见的 OpenAI base_url 规范：
    - 带 /v1 后缀（如 https://chatapi.weixin.qq.com/openai/v1 或 https://api.openai.com/v1）
    - 不带 /v1 后缀（如 https://api.deepseek.com 或 https://api.openai.com）

    直接使用完整 URL 请求，避免 httpx 处理相对/绝对路径时意外覆盖中间路径。
    """
    cleaned = base_url.rstrip("/")
    if cleaned.endswith("/v1"):
        return f"{cleaned}/chat/completions"
    return f"{cleaned}/v1/chat/completions"


@runtime_checkable
class LlmProvider(Protocol):
    """文本补全提供者。"""

    @property
    def name(self) -> str:
        """提供者标识，用于记录与展示。"""
        ...

    def complete(self, prompt: str, *, system: str = "") -> str:
        """给定提示词返回模型输出文本。

        :raises ProviderError: 调用失败（网络、鉴权、限流等）。
        """
        ...


class ProviderError(RuntimeError):
    """模型调用失败。"""


class RuleBasedProvider:
    """离线确定性提供者。

    不调用任何模型，而是基于 OpenAPI 规范做规则推导。它承担两个职责：

    1. **让 AI 功能可测**：用例生成与归因的测试不需要 API key、不需要网络；
    2. **作为基线**：接入大模型后，用同一批接口跑一遍，就能量化「模型到底好在哪」——
       没有基线的「AI 提升」只是感觉。
    """

    def __init__(self, document: dict[str, Any] | None = None) -> None:
        self.document = document or {}

    @property
    def name(self) -> str:
        return "rule-based"

    def complete(self, prompt: str, *, system: str = "") -> str:  # noqa: ARG002
        """规则提供者不使用提示词，返回空 JSON 对象。

        调用方（:class:`~tax_service.ai.case_generator.TestCaseGenerator`）会识别
        这是规则模式并走确定性推导分支。
        """
        return json.dumps({})


class RecordingProvider:
    """测试替身：记录提示词并返回预设输出。"""

    def __init__(self, response: str = "{}", *, name: str = "recording") -> None:
        self._response = response
        self._name = name
        self.prompts: list[dict[str, str]] = []

    @property
    def name(self) -> str:
        return self._name

    def complete(self, prompt: str, *, system: str = "") -> str:
        self.prompts.append({"system": system, "prompt": prompt})
        return self._response

    def set_response(self, response: str) -> None:
        self._response = response


class OpenAiCompatibleProvider:
    """OpenAI 兼容接口的提供者。

    兼容面覆盖 OpenAI 官方、DeepSeek、通义千问、以及本地 vLLM / Ollama 的
    ``/v1/chat/completions`` 端点，因此只需配置 ``base_url`` 与 ``model`` 即可切换。

    .. warning::
       API key 只从配置读取，**不落库、不写日志**。日志里只记录模型名与耗时。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        temperature: float = 0.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.endpoint = resolve_chat_endpoint(base_url)
        self._api_key = api_key
        self.model = MODEL_ALIASES.get(model.lower(), model)
        self.timeout_seconds = timeout_seconds
        # 温度默认 0：用例生成与归因需要**可复现**，不需要创造力
        self.temperature = temperature

    @property
    def name(self) -> str:
        return f"openai-compatible:{self.model}"

    def complete(self, prompt: str, *, system: str = "") -> str:
        """调用 chat/completions 接口。

        ``httpx`` 延迟导入：AI 是可选增强，未启用时不应付出导入成本；
        同时避免在只做单元测试时加载整个 HTTP 栈。
        """
        import time  # noqa: PLC0415
        import httpx  # noqa: PLC0415 - 可选依赖延迟加载

        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        last_error: Exception | None = None
        payload: dict[str, Any] | None = None

        for attempt in range(2):
            try:
                with httpx.Client(timeout=self.timeout_seconds) as client:
                    response = client.post(
                        self.endpoint,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                        json={
                            "model": self.model,
                            "messages": messages,
                            "temperature": self.temperature,
                            "response_format": {"type": "json_object"},
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    break
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt == 0:
                    time.sleep(1.0)
                    continue
                raise ProviderError(f"模型调用超时或网络异常（已重试）：{exc!r}") from exc
            except httpx.HTTPError as exc:
                # 状态码错误（如 4xx/5xx）不重试，直接抛出
                raise ProviderError(f"模型调用失败：{exc!r}") from exc
            except (KeyError, ValueError) as exc:
                raise ProviderError(f"模型响应格式异常：{exc!r}") from exc
        else:
            raise ProviderError(f"模型调用失败：{last_error!r}")

        try:
            return str(payload["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"模型响应缺少 choices[0].message.content：{payload!r}") from exc


def build_provider(settings: Any, document: dict[str, Any] | None = None) -> LlmProvider:
    """按配置构造提供者。

    未配置 ``ai_api_key`` 时回落到 :class:`RuleBasedProvider`——**没有密钥不是错误**，
    只是意味着不启用模型能力。这样本地开发与 CI 都不需要任何额外准备。
    """
    api_key = getattr(settings, "ai_api_key", None)
    if not api_key:
        logger.info("未配置 ai_api_key，AI 能力使用规则提供者（离线）")
        return RuleBasedProvider(document)

    return OpenAiCompatibleProvider(
        base_url=getattr(settings, "ai_base_url", "https://api.openai.com"),
        api_key=api_key,
        model=getattr(settings, "ai_model", "gpt-4o-mini"),
        timeout_seconds=getattr(settings, "ai_timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
    )
