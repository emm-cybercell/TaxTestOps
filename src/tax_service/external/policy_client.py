"""外部政策服务客户端。

这个模块存在的意义是**给测试提供一个真实的外部依赖**。纯函数式的个税计算器没有
任何网络调用，也就无法演示「缓存、超时、降级、mock」这些接口与集成测试的核心场景。

行为契约（也是测试断言的依据）：

====================  ======================  ====================
场景                  行为                    响应中的 source
====================  ======================  ====================
缓存命中              直接返回缓存             ``cache``
缓存未命中 + 远程成功  写缓存并返回             ``remote``
缓存未命中 + 远程失败  返回内置兜底参数         ``fallback``
====================  ======================  ====================
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass

import httpx

from tax_service.core.policy import BASIC_DEDUCTION
from tax_service.infra.cache import Cache

logger = logging.getLogger(__name__)

CACHE_KEY = "policy:parameters:v1"

FALLBACK_VERSION = "fallback-2019-01"


class PolicyServiceError(RuntimeError):
    """政策服务不可用且无可用兜底时抛出。"""


@dataclass(frozen=True)
class PolicyParameters:
    """政策参数快照。"""

    version: str
    basic_deduction: int
    effective_from: str
    source: str
    """``remote`` / ``cache`` / ``fallback`` 之一。"""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def default_parameters() -> PolicyParameters:
    """内置兜底参数（与需求文档一致）。

    兜底不是「假装成功」——``source`` 字段会把降级事实如实暴露给调用方与监控。
    """
    return PolicyParameters(
        version=FALLBACK_VERSION,
        basic_deduction=BASIC_DEDUCTION,
        effective_from="2019-01-01",
        source="fallback",
    )


class PolicyServiceClient:
    """带缓存与降级的政策服务客户端。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 3.0,
        cache: Cache | None = None,
        cache_ttl_seconds: int = 300,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.cache = cache
        self.cache_ttl_seconds = cache_ttl_seconds
        self._transport = transport
        self._remote_calls = 0

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #

    def fetch_parameters(self) -> PolicyParameters:
        """获取政策参数，依次尝试缓存 → 远程 → 兜底。"""
        cached = self._read_cache()
        if cached is not None:
            return cached

        try:
            parameters = self._fetch_remote()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            logger.warning("政策服务不可用，降级为内置参数：%s", exc)
            return default_parameters()

        self._write_cache(parameters)
        return parameters

    @property
    def remote_call_count(self) -> int:
        """远程调用次数（测试用于断言缓存是否真的生效）。"""
        return self._remote_calls

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _read_cache(self) -> PolicyParameters | None:
        if self.cache is None:
            return None

        raw = self.cache.get(CACHE_KEY)
        if not isinstance(raw, dict):
            return None

        try:
            return PolicyParameters(
                version=str(raw["version"]),
                basic_deduction=int(raw["basic_deduction"]),
                effective_from=str(raw["effective_from"]),
                source="cache",
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("缓存中的政策参数格式非法，已忽略")
            return None

    def _write_cache(self, parameters: PolicyParameters) -> None:
        if self.cache is None:
            return
        payload = parameters.to_dict()
        payload["source"] = "remote"
        self.cache.set(CACHE_KEY, payload, self.cache_ttl_seconds)

    def _fetch_remote(self) -> PolicyParameters:
        self._remote_calls += 1
        with httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout_seconds,
            transport=self._transport,
        ) as client:
            response = client.get("/api/v1/parameters")
            response.raise_for_status()
            payload = response.json()

        return PolicyParameters(
            version=str(payload["version"]),
            basic_deduction=int(payload["basic_deduction"]),
            effective_from=str(payload["effective_from"]),
            source="remote",
        )
