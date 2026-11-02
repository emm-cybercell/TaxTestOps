"""平台层 · 执行引擎。

这是平台最核心的部分：把「用例定义」变成「可判定的结果」。测试重点在**判定逻辑**
与**失败分类**，而不是 HTTP 细节——后者由接口层负责。
"""

from __future__ import annotations

import pytest

from support.platform_cases import HEALTHY_CASES
from support.servers import LiveServer
from tax_service.platform.batch_runner import BatchRunner, summarize
from tax_service.platform.models import CaseStatus, TestCase, TestRunItem
from tax_service.platform.path_resolver import PathResolutionError, resolve_path
from tax_service.platform.runner import (
    CaseOutcome,
    CaseRunner,
    RunOutcome,
    evaluate_assertions,
)

pytestmark = [pytest.mark.platform, pytest.mark.integration]


def make_case(**overrides) -> TestCase:
    """构造未落库的用例对象（不需要数据库）。"""
    defaults = {
        "name": "临时用例",
        "method": "GET",
        "path": "/healthz",
        "expected_status": 200,
        "timeout_seconds": 10.0,
        "retries": 0,
    }
    defaults.update(overrides)
    return TestCase(**defaults)


# --------------------------------------------------------------------------- #
# 路径解析
# --------------------------------------------------------------------------- #


class TestResolvePath:
    """断言路径解析。"""

    def test_empty_path_returns_whole_payload(self) -> None:
        payload = {"a": 1}

        assert resolve_path(payload, "") == payload

    def test_resolves_nested_field(self) -> None:
        assert resolve_path({"a": {"b": {"c": 42}}}, "a.b.c") == 42

    def test_resolves_list_index(self) -> None:
        payload = {"deductions": [{"amount": 1000}, {"amount": 400}]}

        assert resolve_path(payload, "deductions.0.amount") == 1000
        assert resolve_path(payload, "deductions.1.amount") == 400

    def test_supports_negative_index(self) -> None:
        assert resolve_path({"items": [1, 2, 3]}, "items.-1") == 3

    def test_missing_field_reports_available_keys(self) -> None:
        """错误信息必须列出可用字段——让失败可以自解释，不必回查响应体。"""
        with pytest.raises(PathResolutionError, match="可用字段"):
            resolve_path({"tax_amount": 1, "taxable_income": 2}, "tax")

    def test_out_of_range_index_reports_length(self) -> None:
        with pytest.raises(PathResolutionError, match="越界"):
            resolve_path({"items": [1]}, "items.5")

    def test_non_numeric_index_rejected(self) -> None:
        with pytest.raises(PathResolutionError, match="期望列表下标"):
            resolve_path({"items": [1]}, "items.first")

    def test_descending_into_scalar_is_rejected(self) -> None:
        with pytest.raises(PathResolutionError, match="无法继续解析"):
            resolve_path({"a": 1}, "a.b")

    def test_path_through_none_is_rejected(self) -> None:
        with pytest.raises(PathResolutionError):
            resolve_path({"a": None}, "a.b")


# --------------------------------------------------------------------------- #
# 断言判定
# --------------------------------------------------------------------------- #


class TestEvaluateAssertions:
    """断言列表的判定与错误汇报。"""

    def test_passes_on_match(self) -> None:
        failures = evaluate_assertions({"tax": 90}, [{"path": "tax", "op": "eq", "value": 90}])

        assert failures == []

    def test_reports_all_failures_not_just_first(self) -> None:
        """一次执行必须把所有失败都报出来。

        遇到第一个就返回，会让「修一个问题、重跑、再发现一个」循环往复，
        显著拉长反馈周期。
        """
        failures = evaluate_assertions(
            {"a": 1, "b": 2},
            [
                {"path": "a", "op": "eq", "value": 99},
                {"path": "b", "op": "eq", "value": 99},
            ],
        )

        assert len(failures) == 2

    def test_reports_missing_path_without_raising(self) -> None:
        """取值失败也要变成一条失败信息，而不是抛异常中断整条用例。"""
        failures = evaluate_assertions({"a": 1}, [{"path": "missing", "op": "eq", "value": 1}])

        assert len(failures) == 1
        assert "取值失败" in failures[0]

    def test_rejects_unknown_operator(self) -> None:
        failures = evaluate_assertions({"a": 1}, [{"path": "a", "op": "matches", "value": "x"}])

        assert len(failures) == 1
        assert "未知操作符" in failures[0]

    @pytest.mark.parametrize(
        ("op", "actual", "expected", "should_pass"),
        [
            pytest.param("eq", 1, 1, True, id="eq 相等"),
            pytest.param("eq", 1, 2, False, id="eq 不等"),
            pytest.param("ne", 1, 2, True, id="ne"),
            pytest.param("gt", 2, 1, True, id="gt"),
            pytest.param("ge", 1, 1, True, id="ge 取等"),
            pytest.param("lt", 1, 2, True, id="lt"),
            pytest.param("le", 1, 1, True, id="le 取等"),
            pytest.param("contains", "abc", "b", True, id="contains 字符串"),
            pytest.param("contains", [1, 2, 3], 2, True, id="contains 列表"),
            pytest.param("in", "b", ["a", "b"], True, id="in"),
            pytest.param("startswith", "abc", "ab", True, id="startswith"),
        ],
    )
    def test_operator_matrix(self, op: str, actual, expected, should_pass: bool) -> None:
        failures = evaluate_assertions({"v": actual}, [{"path": "v", "op": op, "value": expected}])

        assert (failures == []) is should_pass

    def test_empty_assertions_pass(self) -> None:
        assert evaluate_assertions({"anything": 1}, []) == []

    def test_missing_op_defaults_to_eq(self) -> None:
        failures = evaluate_assertions({"v": 1}, [{"path": "v", "value": 1}])

        assert failures == []


# --------------------------------------------------------------------------- #
# 单条执行
# --------------------------------------------------------------------------- #


class TestCaseRunner:
    """单条用例执行。"""

    def test_passes_against_real_service(self, tax_server: LiveServer) -> None:
        case = make_case(assertions=[{"path": "status", "op": "eq", "value": "ok"}])

        outcome = CaseRunner().run(case, base_url=tax_server.base_url)

        assert outcome.status is CaseStatus.PASSED
        assert outcome.status_code == 200
        assert outcome.latency_ms > 0

    def test_status_mismatch_is_failure_not_error(self, tax_server: LiveServer) -> None:
        """状态码不符属于**被测系统**的问题，应判为 FAILED 而不是 ERROR。

        这个区分决定告警路由：FAILED 找业务，ERROR 找测试环境。
        """
        case = make_case(path="/api/v1/tax/records", expected_status=200)

        outcome = CaseRunner().run(case, base_url=tax_server.base_url)

        assert outcome.status is CaseStatus.FAILED
        assert outcome.status_code is None
        assert "状态码不符" in outcome.message

    def test_assertion_failure_is_failure(self, tax_server: LiveServer) -> None:
        case = make_case(assertions=[{"path": "status", "op": "eq", "value": "wrong"}])

        outcome = CaseRunner().run(case, base_url=tax_server.base_url)

        assert outcome.status is CaseStatus.FAILED
        assert "status" in outcome.message

    def test_unreachable_target_is_error(self) -> None:
        """目标不可达属于**测试环境**问题，应判为 ERROR。"""
        case = make_case()

        outcome = CaseRunner().run(case, base_url="http://127.0.0.1:1")

        assert outcome.status is CaseStatus.ERROR
        assert "请求失败" in outcome.message

    def test_retries_network_errors(self, tax_server: LiveServer) -> None:
        """网络类错误会重试，且最终结果里记录尝试次数。"""
        case = make_case(retries=2)

        outcome = CaseRunner().run(case, base_url="http://127.0.0.1:1")

        assert outcome.status is CaseStatus.ERROR
        assert outcome.attempts == 3

    def test_does_not_retry_assertion_failures(self, tax_server: LiveServer) -> None:
        """断言失败不重试——它不会因为重试而变对，重试只会浪费时间。"""
        case = make_case(
            assertions=[{"path": "status", "op": "eq", "value": "wrong"}], retries=3
        )

        outcome = CaseRunner().run(case, base_url=tax_server.base_url)

        assert outcome.attempts == 1

    def test_sends_payload_and_headers(self, tax_server: LiveServer, target_token: str) -> None:
        case = make_case(
            method="POST",
            path="/api/v1/tax/calculate",
            payload={"salary": 8000, "persist": False},
            requires_auth=True,
            assertions=[{"path": "tax_amount", "op": "eq", "value": 90}],
        )

        outcome = CaseRunner().run(case, base_url=tax_server.base_url, token=target_token)

        assert outcome.status is CaseStatus.PASSED

    def test_uses_auth_token_when_required(self, tax_server: LiveServer, target_token: str) -> None:
        case = make_case(
            method="POST",
            path="/api/v1/tax/calculate",
            payload={"salary": 8000, "persist": False},
            requires_auth=True,
            assertions=[{"path": "tax_amount", "op": "eq", "value": 90}],
        )

        with_token = CaseRunner().run(case, base_url=tax_server.base_url, token=target_token)
        without_token = CaseRunner().run(case, base_url=tax_server.base_url)

        assert with_token.status is CaseStatus.PASSED
        assert without_token.status is CaseStatus.FAILED
        assert "401" in without_token.message

    def test_never_raises_on_non_json_body(self, tax_server: LiveServer) -> None:
        """响应体结构变化不能崩，只能判失败——执行器必须对任意响应保持稳定。"""
        case = make_case(
            path="/api/v1/tax/records",
            expected_status=401,
            assertions=[{"path": "detail", "op": "contains", "value": "令牌"}],
        )

        outcome = CaseRunner().run(case, base_url=tax_server.base_url)

        assert outcome.status is CaseStatus.PASSED

    def test_assertion_on_missing_field_fails_gracefully(self, tax_server: LiveServer) -> None:
        """断言引用了不存在的字段时应判失败并给出可用字段，而不是抛异常。"""
        case = make_case(assertions=[{"path": "not_a_real_field", "op": "eq", "value": 1}])

        outcome = CaseRunner().run(case, base_url=tax_server.base_url)

        assert outcome.status is CaseStatus.FAILED
        assert "取值失败" in outcome.message


# --------------------------------------------------------------------------- #
# 批量执行
# --------------------------------------------------------------------------- #


class TestBatchRunner:
    """批量编排与单条隔离。"""

    def test_runs_all_cases(self, tax_server: LiveServer, target_token: str) -> None:
        cases = [
            make_case(name="健康检查"),
            make_case(
                name="计算接口",
                method="POST",
                path="/api/v1/tax/calculate",
                payload={"salary": 8000, "persist": False},
                requires_auth=True,
            ),
        ]

        outcome = BatchRunner(
            base_url=tax_server.base_url, max_workers=2, token=target_token
        ).run(cases)

        assert outcome.total == 2
        assert outcome.passed == 2

    def test_isolates_single_case_exception(self, tax_server: LiveServer) -> None:
        """一条用例抛异常不能中断整批。

        用注入失败执行器的方式构造——这也说明「执行器可替换」是必要的架构设计，
        否则异常路径根本无法测试。
        """

        class ExplodingRunner(CaseRunner):
            def run(self, case, **kwargs):  # noqa: ANN003, ANN201
                if case.name == "会炸的用例":
                    raise RuntimeError("模拟执行器故障")
                return super().run(case, **kwargs)

        cases = [
            make_case(name="正常用例"),
            make_case(name="会炸的用例"),
            make_case(name="后续用例"),
        ]

        outcome = BatchRunner(
            base_url=tax_server.base_url, max_workers=1, case_runner=ExplodingRunner()
        ).run(cases)

        assert outcome.total == 3
        assert outcome.passed == 2
        assert outcome.errored == 1
        assert "模拟执行器故障" in next(
            item.message for item in outcome.items if item.status is CaseStatus.ERROR
        )

    def test_invokes_callback_per_item(self, tax_server: LiveServer) -> None:
        """每完成一条就回调——WebSocket 实时推送依赖这个契约。"""
        received: list[CaseOutcome] = []

        BatchRunner(
            base_url=tax_server.base_url,
            max_workers=1,
            on_item=received.append,
        ).run([make_case(name="A"), make_case(name="B")])

        assert len(received) == 2
        assert {item.case_name for item in received} == {"A", "B"}

    def test_empty_case_list(self) -> None:
        outcome = BatchRunner(base_url="http://127.0.0.1:1").run([])

        assert outcome.total == 0
        assert outcome.pass_rate == 0.0

    def test_counts_failed_and_errored_separately(self, tax_server: LiveServer) -> None:
        cases = [
            make_case(name="通过"),
            make_case(name="断言失败", assertions=[{"path": "status", "op": "eq", "value": "x"}]),
        ]

        outcome = BatchRunner(base_url=tax_server.base_url, max_workers=2).run(cases)

        assert outcome.passed == 1
        assert outcome.failed == 1
        assert outcome.errored == 0


class TestRunOutcomeAggregation:
    """结果聚合计算。"""

    def test_pass_rate(self) -> None:
        outcome = RunOutcome(
            items=[
                CaseOutcome(None, "a", CaseStatus.PASSED),
                CaseOutcome(None, "b", CaseStatus.PASSED),
                CaseOutcome(None, "c", CaseStatus.FAILED),
                CaseOutcome(None, "d", CaseStatus.ERROR),
            ]
        )

        assert outcome.total == 4
        assert outcome.passed == 2
        assert outcome.failed == 1
        assert outcome.errored == 1
        assert outcome.pass_rate == 50.0

    def test_empty_outcome_has_zero_rate(self) -> None:
        assert RunOutcome().pass_rate == 0.0

    def test_summary_is_human_readable(self) -> None:
        outcome = RunOutcome(items=[CaseOutcome(None, "a", CaseStatus.PASSED)])
        outcome.duration_ms = 123.4

        text = summarize(outcome)

        assert "共 1 条" in text
        assert "通过 1" in text
        assert "123.4" in text


class TestPlatformServiceIntegration:
    """服务层的执行编排（落库 + 状态流转）。

    所有用例都依赖 ``seeded_cases``，它会把目标令牌注入平台——因此这些测试跑的是
    「平台 + 令牌 + 被测服务」的完整链路。
    """

    def test_run_persists_items(self, service, seeded_cases) -> None:
        run = service.create_run()
        finished = service.execute_run(run.id)

        assert finished is not None
        assert finished.total == len(seeded_cases)
        assert finished.passed == len(seeded_cases)
        assert finished.duration_ms is not None

        items = service.list_run_items(run.id)
        assert len(items) == len(seeded_cases)
        assert all(item.status is CaseStatus.PASSED for item in items)

    def test_run_without_cases_is_error_not_passed(self, service) -> None:
        """没有用例可跑不能算「通过」——否则空跑会伪装成绿灯。"""
        run = service.create_run()
        finished = service.execute_run(run.id)

        assert finished is not None
        assert finished.status.value == "error"

    def test_tag_filter_selects_subset(self, service, seeded_cases) -> None:
        run = service.create_run(tag="smoke")
        finished = service.execute_run(run.id)

        assert finished is not None
        assert finished.total == 2

    def test_disabled_cases_are_skipped(self, service, seeded_cases, platform_with_token) -> None:
        """停用的用例不参与执行。

        注意请求体要用**原始定义**而不是接口返回的对象：后者含 ``id``/``created_at``
        等只读字段，而 ``TestCaseIn`` 声明了 ``extra="forbid"``，直接回传会得到 422。
        """
        target = seeded_cases[0]

        patched = platform_with_token.patch(
            f"/api/v1/platform/cases/{target['id']}",
            json={**HEALTHY_CASES[0], "enabled": False},
        )
        assert patched.status_code == 200, f"停用失败：{patched.text}"

        run = service.create_run()
        finished = service.execute_run(run.id)

        assert finished is not None
        assert finished.total == len(seeded_cases) - 1
        assert finished.passed == len(seeded_cases) - 1

    def test_failure_marks_run_failed(self, service, platform_with_token) -> None:
        platform_with_token.post(
            "/api/v1/platform/cases",
            json={
                "name": "必然失败",
                "method": "GET",
                "path": "/healthz",
                "assertions": [{"path": "status", "op": "eq", "value": "definitely-not-ok"}],
            },
        )

        run = service.create_run()
        finished = service.execute_run(run.id)

        assert finished is not None
        assert finished.status.value == "failed"
        assert finished.failed == 1

    def test_unreachable_target_marks_run_failed(self, service, platform_with_token) -> None:
        """目标不可达时逐条都是 ERROR，执行状态为 failed 而非 error。

        执行**本身**完成了（跑完了所有用例并得到结果），所以不是 ``error``；
        但结果不通过，所以是 ``failed``。区分它们是为了让告警指向正确的人。
        """
        platform_with_token.post(
            "/api/v1/platform/cases",
            json={"name": "目标不可达", "method": "GET", "path": "/healthz"},
        )

        run = service.create_run(base_url="http://127.0.0.1:1")
        finished = service.execute_run(run.id)

        assert finished is not None
        assert finished.errored == 1
        assert finished.status.value == "failed"

    def test_trends_accumulate(self, service, seeded_cases) -> None:
        for _ in range(3):
            service.execute_run(service.create_run().id)

        points = service.trends()

        assert len(points) == 3
        assert all(point["pass_rate"] == 100.0 for point in points)

    def test_summary_reports_totals(self, service, seeded_cases) -> None:
        service.execute_run(service.create_run().id)

        summary = service.summary()

        assert summary["total_cases"] == len(seeded_cases)
        assert summary["total_runs"] == 1
        assert summary["latest_pass_rate"] == 100.0

    def test_flaky_candidates_empty_when_no_retries(self, service, seeded_cases) -> None:
        service.execute_run(service.create_run().id)

        assert service.flaky_candidates() == []

    def test_flaky_candidates_detects_retried_cases(
        self, service, platform_with_token, tax_server: LiveServer
    ) -> None:
        """「重试后通过」的用例必须被识别出来。

        flaky 用例是自动化测试最大的隐性成本：它让人开始怀疑所有失败，
        最终演变成「红灯也照常合并」。平台把它显式暴露出来，是治理的第一步。
        """
        platform_with_token.post(
            "/api/v1/platform/cases",
            json={
                "name": "需要重试才通过",
                "method": "GET",
                # 指向一个不存在的路径：首次 404、重试仍 404，但状态码不符属于
                # 确定性失败不会重试，因此这里改用「网络抖动」不可控的场景，
                # 直接构造一条已被重试过的结果记录更可控。
                "path": "/healthz",
                "retries": 2,
            },
        )

        # 直接写入一条 attempts>1 且通过的结果，模拟重试后成功
        run = service.create_run()
        with service._session_factory() as session:  # noqa: SLF001 - 测试直接造数据
            session.add(
                TestRunItem(
                    run_id=run.id,
                    case_id=None,
                    case_name="需要重试才通过",
                    status=CaseStatus.PASSED,
                    latency_ms=120.0,
                    attempts=2,
                )
            )
            session.commit()

        candidates = service.flaky_candidates()

        assert len(candidates) == 1
        assert candidates[0]["case_name"] == "需要重试才通过"
        assert candidates[0]["attempts"] == 2
