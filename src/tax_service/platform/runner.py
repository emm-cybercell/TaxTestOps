"""单条用例的执行器。

设计要点
--------
1. **断言是数据，不是代码**：``assertions`` 用 ``{"path": ..., "op": ..., "value": ...}``
   描述，因此用例可以在界面上维护、可以存库、可以跨版本对比；
2. **可重试**：``retries`` 针对偶发网络抖动，不掩盖稳定失败——重试次数与最终结果
   都记录在案，便于识别 flaky 用例；
3. **不抛异常**：执行器只返回结果对象。异常穿透会让一条用例的失败演变成整批中断。

动态取值集中在 :mod:`tax_service.platform.path_resolver`，本模块只负责编排。
批量并发编排见 :mod:`tax_service.platform.batch_runner`。
"""

from __future__ import annotations

import logging
import operator
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import httpx

from tax_service.platform.models import CaseStatus, TestCase
from tax_service.platform.path_resolver import PathResolutionError, resolve_path

logger = logging.getLogger(__name__)

#: 支持的断言操作符。新增操作符只需在这里加一行，用例即可直接使用。
OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "eq": operator.eq,
    "ne": operator.ne,
    "gt": operator.gt,
    "ge": operator.ge,
    "lt": operator.lt,
    "le": operator.le,
    "contains": lambda actual, expected: expected in actual,
    "in": lambda actual, expected: actual in expected,
    "startswith": lambda actual, expected: str(actual).startswith(str(expected)),
}

STATUS_MISMATCH_PREFIX = "状态码不符"
ASSERTION_FAILURE_PREFIX = "断言["


@dataclass
class CaseOutcome:
    """单条用例的执行结果。"""

    case_id: int | None
    case_name: str
    status: CaseStatus
    status_code: int | None = None
    latency_ms: float = 0.0
    attempts: int = 1
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status is CaseStatus.PASSED


@dataclass
class RunOutcome:
    """一次执行的整体结果。"""

    items: list[CaseOutcome] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def total(self) -> int:
        return len(self.items)

    def _count(self, status: CaseStatus) -> int:
        return sum(1 for item in self.items if item.status is status)

    @property
    def passed(self) -> int:
        return self._count(CaseStatus.PASSED)

    @property
    def failed(self) -> int:
        return self._count(CaseStatus.FAILED)

    @property
    def errored(self) -> int:
        return self._count(CaseStatus.ERROR)

    @property
    def pass_rate(self) -> float:
        return round(self.passed * 100 / self.total, 1) if self.total else 0.0


def evaluate_assertions(payload: Any, assertions: Iterable[dict[str, Any]]) -> list[str]:
    """执行断言列表，返回失败描述（全部通过时为空列表）。

    刻意**不抛异常**：一次执行要把所有失败的断言都报出来，而不是遇到第一个就停——
    否则修复一个问题后重跑才发现还有第二个，来回拉长反馈周期。
    """
    failures: list[str] = []

    for index, assertion in enumerate(assertions):
        path = str(assertion.get("path", ""))
        op_name = str(assertion.get("op", "eq"))
        expected = assertion.get("value")

        if op_name not in OPERATORS:
            failures.append(f"断言[{index}] 使用了未知操作符 {op_name}")
            continue

        try:
            actual = resolve_path(payload, path)
        except (PathResolutionError, IndexError, TypeError, ValueError) as exc:
            failures.append(f"断言[{index}] 取值失败：{exc}")
            continue

        if not OPERATORS[op_name](actual, expected):
            failures.append(
                f"断言[{index}] {path or '整个响应'} 期望 {op_name} {expected!r}，实际 {actual!r}"
            )

    return failures


def build_headers(case: TestCase, token: str | None) -> dict[str, str]:
    """组装请求头。用例自定义头优先于默认头，鉴权头最后注入。"""
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if case.headers:
        headers.update({str(key): str(value) for key, value in case.headers.items()})
    if case.requires_auth and token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class CaseRunner:
    """单条用例的执行器。"""

    def __init__(self, *, default_timeout: float = 10.0) -> None:
        self.default_timeout = default_timeout

    def run(
        self,
        case: TestCase,
        *,
        base_url: str,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> CaseOutcome:
        """执行一条用例，返回结果（不抛异常）。"""
        headers = build_headers(case, token)
        attempts = 0
        last_message = ""
        began = time.perf_counter()

        for attempt in range(1, case.retries + 2):
            attempts = attempt
            try:
                with httpx.Client(
                    base_url=base_url,
                    timeout=case.timeout_seconds or self.default_timeout,
                    transport=transport,
                ) as client:
                    response = client.request(
                        case.method.upper(),
                        case.path,
                        json=case.payload,
                        headers=headers,
                    )
            except httpx.HTTPError as exc:
                # 网络类错误值得重试；断言失败不值得
                last_message = f"请求失败：{exc!r}"
                continue

            latency_ms = (time.perf_counter() - began) * 1000

            if response.status_code != case.expected_status:
                last_message = (
                    f"{STATUS_MISMATCH_PREFIX}：期望 {case.expected_status}，"
                    f"实际 {response.status_code}（响应体：{response.text[:200]}）"
                )
                # 状态码不符是**确定性**结果，重试不会改变它，只会浪费时间
                break

            try:
                body = response.json()
            except ValueError:
                body = None

            failures = evaluate_assertions(body, case.assertions or [])
            if failures:
                last_message = "；".join(failures)
                # 同理：断言失败是确定性的，不重试
                break

            return CaseOutcome(
                case_id=case.id,
                case_name=case.name,
                status=CaseStatus.PASSED,
                status_code=response.status_code,
                latency_ms=round(latency_ms, 2),
                attempts=attempts,
                message="",
            )

        # 重试耗尽：区分「断言/状态码不符」与「请求本身出错」
        is_assertion_failure = last_message.startswith(
            (STATUS_MISMATCH_PREFIX, ASSERTION_FAILURE_PREFIX)
        )
        return CaseOutcome(
            case_id=case.id,
            case_name=case.name,
            status=CaseStatus.FAILED if is_assertion_failure else CaseStatus.ERROR,
            latency_ms=round((time.perf_counter() - began) * 1000, 2),
            attempts=attempts,
            message=last_message,
        )
