"""平台服务层：用例管理、执行编排、结果与趋势。

状态机
------
一次执行的生命周期是明确的四态流转，而不是一个布尔值：

    PENDING ──► RUNNING ──┬──► PASSED   （全部用例通过）
                          └──► FAILED   （有用例失败，但执行本身完成）
                          └──► ERROR    （执行过程本身出错，例如目标不可达）

区分 ``FAILED`` 与 ``ERROR`` 很关键：前者说明**被测系统**有问题，后者说明
**测试执行环境**有问题。告警路由完全不同——把它们混为一谈会让值班同学
去查一个根本没挂的服务。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from tax_service.platform.batch_runner import BatchRunner
from tax_service.platform.models import (
    CaseStatus,
    TestCase,
    TestRun,
    TestRunItem,
    TestRunStatus,
)

logger = logging.getLogger(__name__)

#: 用例可被更新/删除的字段白名单。
CASE_EDITABLE_FIELDS = frozenset(
    {
        "name",
        "description",
        "method",
        "path",
        "payload",
        "headers",
        "expected_status",
        "assertions",
        "tags",
        "requires_auth",
        "timeout_seconds",
        "retries",
        "enabled",
    }
)


class PlatformService:
    """测试平台的核心服务。"""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        target_base_url: str,
        target_token: str | None = None,
        max_workers: int = 4,
        on_run_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        """
        :param target_base_url: 被测服务地址。
        :param target_token: 调用需要鉴权的接口时使用的令牌。
        :param max_workers: 执行并发度。
        :param on_run_event: 执行过程事件回调，签名 ``(run_id_str, event)``。
            用于向 WebSocket 订阅者实时推送进度。
        """
        self._session_factory = session_factory
        self.target_base_url = target_base_url.rstrip("/")
        self.target_token = target_token
        self.max_workers = max_workers
        self.on_run_event = on_run_event

    # ------------------------------------------------------------------ #
    # 用例管理
    # ------------------------------------------------------------------ #

    def create_case(self, data: dict[str, Any]) -> TestCase:
        """新建用例。"""
        session = self._session_factory()
        try:
            editable = {key: value for key, value in data.items() if key in CASE_EDITABLE_FIELDS}
            case = TestCase(**editable)
            session.add(case)
            session.commit()
            session.refresh(case)
            return case
        finally:
            session.close()

    def get_case(self, case_id: int) -> TestCase | None:
        with self._session_factory() as session:
            return session.get(TestCase, case_id)

    def list_cases(
        self,
        *,
        tag: str | None = None,
        enabled_only: bool = True,
        limit: int = 200,
    ) -> list[TestCase]:
        """按标签筛选用例。标签存为 JSON 数组，这里在应用层过滤。

        生产环境若用例规模大，应改为独立的关联表以支持数据库层筛选；
        当前规模下应用层过滤更简单，且 SQLite/MySQL 的 JSON 查询语法差异不必处理。
        """
        with self._session_factory() as session:
            statement = select(TestCase).order_by(TestCase.id)
            if enabled_only:
                statement = statement.where(TestCase.enabled.is_(True))
            cases = list(session.scalars(statement.limit(limit)))

        if tag:
            cases = [case for case in cases if tag in (case.tags or [])]
        return cases

    def update_case(self, case_id: int, data: dict[str, Any]) -> TestCase | None:
        session = self._session_factory()
        try:
            case = session.get(TestCase, case_id)
            if case is None:
                return None
            for key, value in data.items():
                if key in CASE_EDITABLE_FIELDS:
                    setattr(case, key, value)
            session.commit()
            session.refresh(case)
            return case
        finally:
            session.close()

    def delete_case(self, case_id: int) -> bool:
        session = self._session_factory()
        try:
            case = session.get(TestCase, case_id)
            if case is None:
                return False
            session.delete(case)
            session.commit()
            return True
        finally:
            session.close()

    def count_cases(self, *, enabled_only: bool = False) -> int:
        with self._session_factory() as session:
            statement = select(func.count()).select_from(TestCase)
            if enabled_only:
                statement = statement.where(TestCase.enabled.is_(True))
            return int(session.scalar(statement) or 0)

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #

    def create_run(
        self,
        *,
        trigger: str = "manual",
        tag: str | None = None,
        base_url: str | None = None,
    ) -> TestRun:
        """创建一个待执行批次（不执行）。"""
        session = self._session_factory()
        try:
            run = TestRun(
                trigger=trigger,
                status=TestRunStatus.PENDING,
                target_base_url=(base_url or self.target_base_url).rstrip("/"),
                case_filter=tag,
            )
            session.add(run)
            session.commit()
            session.refresh(run)
            return run
        finally:
            session.close()

    def execute_run(self, run_id: int) -> TestRun | None:
        """执行指定批次并落库结果。

        该方法可能耗时较长（取决于用例数量），调用方应放在后台线程执行。
        """
        run = self.get_run(run_id)
        if run is None:
            return None

        self._set_run_status(run_id, TestRunStatus.RUNNING)
        self._emit(
            run_id,
            {"type": "run_started", "run_id": run_id, "base_url": run.target_base_url},
        )

        cases = self.list_cases(tag=run.case_filter, enabled_only=True)

        def on_item(outcome) -> None:  # noqa: ANN001 - 回调签名由 BatchRunner 定义
            self._persist_item(run_id, outcome)
            self._emit(
                run_id,
                {
                    "type": "case_finished",
                    "run_id": run_id,
                    "case": outcome.case_name,
                    "status": outcome.status.value,
                    "latency_ms": outcome.latency_ms,
                    "message": outcome.message,
                },
            )

        runner = BatchRunner(
            base_url=run.target_base_url,
            token=self.target_token,
            max_workers=self.max_workers,
            on_item=on_item,
        )
        outcome = runner.run(cases)

        all_passed = outcome.failed == 0 and outcome.errored == 0
        status = TestRunStatus.PASSED if all_passed else TestRunStatus.FAILED
        if outcome.total == 0:
            # 没有用例可跑不算「通过」——否则空跑会伪装成绿灯
            status = TestRunStatus.ERROR

        finished = self._finalize_run(run_id, outcome, status)
        self._emit(
            run_id,
            {
                "type": "run_finished",
                "run_id": run_id,
                "status": status.value,
                "passed": outcome.passed,
                "failed": outcome.failed,
                "errored": outcome.errored,
                "total": outcome.total,
            },
        )
        return finished

    def get_run(self, run_id: int) -> TestRun | None:
        with self._session_factory() as session:
            return session.get(TestRun, run_id)

    def list_runs(self, *, limit: int = 20, offset: int = 0) -> list[TestRun]:
        with self._session_factory() as session:
            statement = select(TestRun).order_by(TestRun.id.desc()).limit(limit).offset(offset)
            return list(session.scalars(statement))

    def count_runs(self) -> int:
        with self._session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(TestRun)) or 0)

    def list_run_items(self, run_id: int) -> list[TestRunItem]:
        with self._session_factory() as session:
            statement = (
                select(TestRunItem).where(TestRunItem.run_id == run_id).order_by(TestRunItem.id)
            )
            return list(session.scalars(statement))

    # ------------------------------------------------------------------ #
    # 趋势
    # ------------------------------------------------------------------ #

    def trends(self, *, limit: int = 30) -> list[dict[str, Any]]:
        """返回最近若干次执行的关键指标，用于绘制通过率与耗时曲线。"""
        runs = sorted(self.list_runs(limit=limit), key=lambda run: run.id)

        return [
            {
                "run_id": run.id,
                "status": run.status.value,
                "total": run.total,
                "passed": run.passed,
                "failed": run.failed + run.errored,
                "pass_rate": run.pass_rate,
                "duration_ms": run.duration_ms,
                "started_at": run.started_at.isoformat() if run.started_at else None,
            }
            for run in runs
        ]

    def summary(self) -> dict[str, Any]:
        """平台概览指标。"""
        runs = self.list_runs(limit=200)
        finished = [run for run in runs if run.total > 0]
        latest = runs[0] if runs else None

        average_pass_rate = (
            round(sum(run.pass_rate for run in finished) / len(finished), 1) if finished else 0.0
        )

        return {
            "total_cases": self.count_cases(),
            "enabled_cases": self.count_cases(enabled_only=True),
            "total_runs": self.count_runs(),
            "latest_status": latest.status.value if latest else None,
            "latest_pass_rate": latest.pass_rate if latest else 0.0,
            "average_pass_rate": average_pass_rate,
            "flaky_candidates": self.flaky_candidates(),
        }

    def flaky_candidates(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """识别「同一次执行内被重试过、但最终通过」的用例。

        这类用例是 flaky 的强信号：它说明用例本身不稳定，而不是被测系统稳定失败。
        把它们单独列出来，是「flaky 治理」的第一步——先看见，再决定是修用例还是
        加等待条件，而不是长期靠重试掩盖。
        """
        with self._session_factory() as session:
            statement = (
                select(TestRunItem)
                .where(TestRunItem.attempts > 1, TestRunItem.status == CaseStatus.PASSED)
                .order_by(TestRunItem.id.desc())
                .limit(limit)
            )
            items = list(session.scalars(statement))

        return [
            {
                "case_name": item.case_name,
                "run_id": item.run_id,
                "attempts": item.attempts,
                "latency_ms": item.latency_ms,
            }
            for item in items
        ]

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    def _emit(self, run_id: int, event: dict[str, Any]) -> None:
        if self.on_run_event is not None:
            self.on_run_event(str(run_id), event)

    def _set_run_status(self, run_id: int, status: TestRunStatus) -> None:
        session = self._session_factory()
        try:
            run = session.get(TestRun, run_id)
            if run is not None:
                run.status = status
                session.commit()
        finally:
            session.close()

    def _persist_item(self, run_id: int, outcome) -> None:  # noqa: ANN001
        session = self._session_factory()
        try:
            session.add(
                TestRunItem(
                    run_id=run_id,
                    case_id=outcome.case_id,
                    case_name=outcome.case_name,
                    status=outcome.status,
                    status_code=outcome.status_code,
                    latency_ms=outcome.latency_ms,
                    attempts=outcome.attempts,
                    message=outcome.message,
                )
            )
            session.commit()
        finally:
            session.close()

    def _finalize_run(self, run_id: int, outcome, status: TestRunStatus) -> TestRun | None:  # noqa: ANN001
        session = self._session_factory()
        try:
            run = session.get(TestRun, run_id)
            if run is None:
                return None
            run.status = status
            run.total = outcome.total
            run.passed = outcome.passed
            run.failed = outcome.failed
            run.errored = outcome.errored
            run.duration_ms = outcome.duration_ms
            run.finished_at = datetime.now(UTC)
            session.commit()
            session.refresh(run)
            return run
        finally:
            session.close()


def select_cases_by_tag(cases: Sequence[TestCase], tag: str | None) -> list[TestCase]:
    """按标签筛选用例（纯函数，便于单测）。"""
    if tag is None:
        return list(cases)
    return [case for case in cases if tag in (case.tags or [])]
