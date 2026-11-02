"""平台的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]

AssertionOp = Literal["eq", "ne", "gt", "ge", "lt", "le", "contains", "in", "startswith"]


class AssertionIn(BaseModel):
    """一条断言。"""

    model_config = ConfigDict(extra="forbid")

    path: Annotated[
        str,
        Field(default="", description="响应体中的取值路径，如 deductions.0.amount；空表示整个响应"),
    ] = ""
    op: Annotated[AssertionOp, Field(default="eq", description="比较操作符")] = "eq"
    value: Annotated[Any, Field(default=None, description="期望值")] = None


class TestCaseIn(BaseModel):
    """新建/更新用例的请求体。"""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(min_length=1, max_length=128)]
    description: Annotated[str, Field(default="", max_length=2000)] = ""
    method: HttpMethod = "GET"
    path: Annotated[str, Field(min_length=1, max_length=256)]
    payload: Annotated[dict[str, Any] | None, Field(default=None)] = None
    headers: Annotated[dict[str, Any] | None, Field(default=None)] = None
    expected_status: Annotated[int, Field(ge=100, le=599, default=200)] = 200
    assertions: Annotated[list[AssertionIn] | None, Field(default=None)] = None
    tags: Annotated[list[str] | None, Field(default=None)] = None
    requires_auth: bool = False
    timeout_seconds: Annotated[float, Field(gt=0, le=120, default=10.0)] = 10.0
    retries: Annotated[int, Field(ge=0, le=5, default=0)] = 0
    enabled: bool = True


class TestCaseOut(BaseModel):
    """用例详情。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str
    method: str
    path: str
    payload: dict[str, Any] | None
    headers: dict[str, Any] | None
    expected_status: int
    assertions: list[dict[str, Any]] | None
    tags: list[str] | None
    requires_auth: bool
    timeout_seconds: float
    retries: int
    enabled: bool
    created_at: datetime
    updated_at: datetime


class TestCasePage(BaseModel):
    """用例分页。"""

    total: int
    items: list[TestCaseOut]


class RunCreateIn(BaseModel):
    """触发执行的请求体。"""

    model_config = ConfigDict(extra="forbid")

    tag: Annotated[str | None, Field(default=None, description="只执行带该标签的用例")] = None
    base_url: Annotated[
        str | None,
        Field(default=None, description="覆盖被测服务地址；为空则用平台默认值"),
    ] = None
    wait: Annotated[
        bool,
        Field(default=False, description="是否同步等待执行完成（用例少时可开）"),
    ] = False


class RunItemOut(BaseModel):
    """执行中的单条结果。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    case_id: int | None
    case_name: str
    status: str
    status_code: int | None
    latency_ms: float
    attempts: int
    message: str


class TestRunOut(BaseModel):
    """执行详情。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    trigger: str
    status: str
    target_base_url: str
    case_filter: str | None
    total: int
    passed: int
    failed: int
    errored: int
    started_at: datetime
    finished_at: datetime | None
    duration_ms: float | None
    pass_rate: float


class TestRunDetail(TestRunOut):
    """执行详情（含逐条结果）。"""

    items: list[RunItemOut] = Field(default_factory=list)


class RunPage(BaseModel):
    """执行历史分页。"""

    total: int
    items: list[TestRunOut]


class TrendPoint(BaseModel):
    """趋势曲线上的一个点。"""

    run_id: int
    status: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    duration_ms: float | None
    started_at: str | None


class FlakyCandidate(BaseModel):
    """疑似 flaky 的用例（被重试后通过）。"""

    case_name: str
    run_id: int
    attempts: int
    latency_ms: float


class PlatformSummary(BaseModel):
    """平台概览。"""

    total_cases: int
    enabled_cases: int
    total_runs: int
    latest_status: str | None
    latest_pass_rate: float
    average_pass_rate: float
    flaky_candidates: list[FlakyCandidate]


class SchedulerStatus(BaseModel):
    """定时调度状态。"""

    enabled: bool
    interval_seconds: int
    tag: str | None
    next_run_at: str | None
    last_run_id: int | None
    executions: int
