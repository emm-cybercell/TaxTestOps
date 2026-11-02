"""批量执行编排：并发跑一批用例，逐条回调。

与 :class:`~tax_service.platform.runner.CaseRunner` 的分工
--------------------------------------------------------
``CaseRunner`` 负责「一条用例怎么跑、怎么判」，本模块只负责「一批用例怎么排」。

**单条隔离**是这里的核心约束：一条用例的异常（网络超时、断言崩溃、甚至执行器自身
的 bug）必须被收敛成一个错误结果，不能让整批中断。理由很实际——批量执行的场景下，
用户最需要的是「跑完并告诉我哪几条挂了」，而不是「跑到第 7 条崩了，前面 6 条的结果
也没了」。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from tax_service.platform.models import CaseStatus, TestCase
from tax_service.platform.runner import CaseOutcome, CaseRunner, RunOutcome

logger = logging.getLogger(__name__)


class BatchRunner:
    """批量执行器。"""

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        max_workers: int = 4,
        case_runner: CaseRunner | None = None,
        on_item: Callable[[CaseOutcome], None] | None = None,
    ) -> None:
        """
        :param base_url: 被测服务地址。
        :param token: 需要鉴权的用例使用的访问令牌。
        :param max_workers: 并发度。太高会压垮被测服务，把「功能测试」跑成「压测」。
        :param case_runner: 可注入的单条执行器，便于测试替换。
        :param on_item: 每完成一条即回调，用于实时推送（WebSocket 广播）。
        """
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.max_workers = max_workers
        self.case_runner = case_runner or CaseRunner()
        self.on_item = on_item

    def _execute_one(self, case: TestCase) -> CaseOutcome:
        """执行一条用例，把任何异常收敛为错误结果。"""
        try:
            return self.case_runner.run(case, base_url=self.base_url, token=self.token)
        except Exception as exc:  # noqa: BLE001 - 单条异常不得影响整批
            logger.exception("用例执行异常，已隔离为单条错误结果")
            return CaseOutcome(
                case_id=case.id,
                case_name=case.name,
                status=CaseStatus.ERROR,
                message=f"执行器异常：{exc!r}",
            )

    def run(self, cases: list[TestCase]) -> RunOutcome:
        """并发执行用例集，按完成顺序回调。"""
        outcome = RunOutcome()
        began = time.perf_counter()

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            for result in pool.map(self._execute_one, cases):
                outcome.items.append(result)
                if self.on_item is not None:
                    self.on_item(result)

        outcome.duration_ms = round((time.perf_counter() - began) * 1000, 2)
        return outcome


def summarize(outcome: RunOutcome) -> str:
    """生成一行人类可读的执行摘要。"""
    return (
        f"共 {outcome.total} 条｜通过 {outcome.passed}｜失败 {outcome.failed}｜"
        f"错误 {outcome.errored}｜通过率 {outcome.pass_rate}｜耗时 {outcome.duration_ms}ms"
    )
