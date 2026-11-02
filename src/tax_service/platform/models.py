"""平台数据模型。"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tax_service.db.base import Base
from tax_service.db.models import utcnow


class TestRunStatus(enum.StrEnum):
    """一次执行的整体状态。"""

    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class CaseStatus(enum.StrEnum):
    """单条用例的执行结果。"""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class TestCase(Base):
    """一条接口测试用例。

    用例以**数据**形式存储（而不是代码），这正是平台与脚本的核心差异：
    产品、运营或测试同学可以在界面上维护用例，不需要改代码、提 PR、等发布。
    """

    __tablename__ = "platform_test_cases"

    # 类名以 Test 开头，pytest 会把它误当成测试类尝试收集并报 CollectionWarning。
    # __test__ = False 是 pytest 官方的退出机制。
    __test__ = False

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")

    method: Mapped[str] = mapped_column(String(8), nullable=False, default="GET")
    path: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    headers: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    expected_status: Mapped[int] = mapped_column(Integer, nullable=False, default=200)
    assertions: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    """断言列表，每项形如 ``{"path": "tax_amount", "op": "eq", "value": 1590}``。"""

    tags: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    requires_auth: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timeout_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=10.0)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<TestCase {self.id} {self.method} {self.path}>"


class TestRun(Base):
    """一次执行（一个批次）。"""

    __tablename__ = "platform_test_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    """触发来源：``manual`` / ``schedule`` / ``api``。"""

    status: Mapped[TestRunStatus] = mapped_column(
        Enum(TestRunStatus, native_enum=False, length=16),
        nullable=False,
        default=TestRunStatus.PENDING,
        index=True,
    )
    target_base_url: Mapped[str] = mapped_column(String(256), nullable=False)
    case_filter: Mapped[str | None] = mapped_column(String(64), nullable=True)
    """本次执行选中的标签过滤条件。"""

    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    passed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    errored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)

    items: Mapped[list[TestRunItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    @property
    def pass_rate(self) -> float:
        """通过率（百分比）。无用例时按 0 处理，避免除零。"""
        return round(self.passed * 100 / self.total, 1) if self.total else 0.0

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<TestRun {self.id} {self.status.value} {self.passed}/{self.total}>"


class TestRunItem(Base):
    """执行中的单条用例结果。"""

    __tablename__ = "platform_test_run_items"

    # 同上：避免被 pytest 误认为测试类
    __test__ = False

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("platform_test_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    case_id: Mapped[int | None] = mapped_column(
        ForeignKey("platform_test_cases.id", ondelete="SET NULL"), nullable=True
    )
    case_name: Mapped[str] = mapped_column(String(128), nullable=False)

    status: Mapped[CaseStatus] = mapped_column(
        Enum(CaseStatus, native_enum=False, length=16), nullable=False
    )
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")

    run: Mapped[TestRun] = relationship(back_populates="items")

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<TestRunItem {self.case_name} {self.status.value}>"
