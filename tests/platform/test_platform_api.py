"""平台层 · 接口、实时推送与定时调度。"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from support.platform_cases import case_payload
from tax_service.platform.scheduler import RunScheduler, wait_for_scheduler_ready
from tax_service.platform.streaming import QUEUE_MAX_SIZE, RunEventBroker

pytestmark = [pytest.mark.platform, pytest.mark.integration]


# --------------------------------------------------------------------------- #
# 用例管理
# --------------------------------------------------------------------------- #


class TestCaseManagement:
    """用例 CRUD——平台相对脚本的核心差异。"""

    def test_creates_case(self, platform: TestClient) -> None:
        response = platform.post(
            "/api/v1/platform/cases",
            json=case_payload("新建用例", path="/healthz", tags=["smoke"]),
        )

        assert response.status_code == 201
        body = response.json()
        assert body["name"] == "新建用例"
        assert body["tags"] == ["smoke"]
        assert body["enabled"] is True
        assert body["created_at"]

    def test_lists_cases(self, platform: TestClient) -> None:
        for index in range(3):
            platform.post("/api/v1/platform/cases", json=case_payload(f"用例{index}"))

        body = platform.get("/api/v1/platform/cases").json()

        assert body["total"] == 3
        assert len(body["items"]) == 3

    def test_filters_by_tag(self, platform: TestClient) -> None:
        platform.post("/api/v1/platform/cases", json=case_payload("A", tags=["smoke"]))
        platform.post("/api/v1/platform/cases", json=case_payload("B", tags=["regression"]))

        body = platform.get("/api/v1/platform/cases", params={"tag": "smoke"}).json()

        assert body["total"] == 1
        assert body["items"][0]["name"] == "A"

    def test_filters_disabled_cases(self, platform: TestClient) -> None:
        created = platform.post(
            "/api/v1/platform/cases", json=case_payload("将停用")
        ).json()
        platform.patch(
            f"/api/v1/platform/cases/{created['id']}",
            json={**case_payload("将停用"), "enabled": False},
        )

        assert platform.get("/api/v1/platform/cases").json()["total"] == 0
        assert (
            platform.get(
                "/api/v1/platform/cases", params={"enabled_only": False}
            ).json()["total"]
            == 1
        )

    def test_updates_case(self, platform: TestClient) -> None:
        created = platform.post("/api/v1/platform/cases", json=case_payload("原名")).json()

        response = platform.patch(
            f"/api/v1/platform/cases/{created['id']}",
            json={**case_payload("新名"), "expected_status": 201},
        )

        assert response.status_code == 200
        assert response.json()["name"] == "新名"
        assert response.json()["expected_status"] == 201

    def test_deletes_case(self, platform: TestClient) -> None:
        created = platform.post("/api/v1/platform/cases", json=case_payload("待删")).json()

        assert platform.delete(f"/api/v1/platform/cases/{created['id']}").status_code == 204
        assert platform.get(f"/api/v1/platform/cases/{created['id']}").status_code == 404

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            pytest.param("name", "", id="空名称"),
            pytest.param("method", "TRACE", id="不支持的方法"),
            pytest.param("path", "", id="空路径"),
            pytest.param("expected_status", 99, id="状态码过小"),
            pytest.param("expected_status", 600, id="状态码过大"),
            pytest.param("timeout_seconds", 0, id="超时为零"),
            pytest.param("retries", 99, id="重试次数超上限"),
            pytest.param(
                "assertions",
                [{"path": "a", "op": "matches", "value": 1}],
                id="未知操作符",
            ),
        ],
    )
    def test_rejects_invalid_definition(
        self, platform: TestClient, field: str, value: object
    ) -> None:
        """用例定义必须严格校验——一条坏用例会污染整个批次的可信度。"""
        payload = {**case_payload("非法用例"), field: value}

        assert platform.post("/api/v1/platform/cases", json=payload).status_code == 422

    def test_rejects_unknown_fields(self, platform: TestClient) -> None:
        payload = {**case_payload("带多余字段"), "unknown": 1}

        assert platform.post("/api/v1/platform/cases", json=payload).status_code == 422

    def test_missing_case_returns_404(self, platform: TestClient) -> None:
        assert platform.get("/api/v1/platform/cases/999999").status_code == 404
        assert platform.delete("/api/v1/platform/cases/999999").status_code == 404
        assert (
            platform.patch(
                "/api/v1/platform/cases/999999",
                json=case_payload("不存在"),
            ).status_code
            == 404
        )


# --------------------------------------------------------------------------- #
# 执行接口
# --------------------------------------------------------------------------- #


class TestRunApi:
    """执行相关的接口行为。"""

    def test_sync_run_returns_final_result(self, platform_with_token, seeded_cases) -> None:
        response = platform_with_token.post("/api/v1/platform/runs", json={"wait": True})

        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "passed"
        assert body["total"] == len(seeded_cases)
        assert body["pass_rate"] == 100.0
        assert body["finished_at"] is not None

    def test_async_run_returns_pending(self, platform_with_token, seeded_cases) -> None:
        """默认异步：立刻返回 202 + run_id，由客户端订阅进度。"""
        response = platform_with_token.post("/api/v1/platform/runs", json={})

        assert response.status_code == 202
        assert response.json()["status"] in {"pending", "running"}

    def test_async_run_eventually_completes(self, platform_with_token, seeded_cases) -> None:
        run_id = platform_with_token.post("/api/v1/platform/runs", json={}).json()["id"]

        deadline = time.monotonic() + 60
        status = "pending"
        while time.monotonic() < deadline:
            status = platform_with_token.get(f"/api/v1/platform/runs/{run_id}").json()["status"]
            if status in {"passed", "failed", "error"}:
                break
            time.sleep(0.1)

        assert status == "passed", f"后台执行未在超时内完成，最终状态 {status}"

    def test_run_detail_contains_items(self, platform_with_token, seeded_cases) -> None:
        run_id = platform_with_token.post(
            "/api/v1/platform/runs", json={"wait": True}
        ).json()["id"]

        detail = platform_with_token.get(f"/api/v1/platform/runs/{run_id}").json()

        assert len(detail["items"]) == len(seeded_cases)
        assert all(item["status"] == "passed" for item in detail["items"])
        assert all(item["latency_ms"] > 0 for item in detail["items"])

    def test_run_history_is_paginated(self, platform_with_token, seeded_cases) -> None:
        for _ in range(3):
            platform_with_token.post("/api/v1/platform/runs", json={"wait": True})

        page = platform_with_token.get("/api/v1/platform/runs", params={"limit": 2}).json()

        assert page["total"] == 3
        assert len(page["items"]) == 2

    def test_unknown_run_returns_404(self, platform: TestClient) -> None:
        assert platform.get("/api/v1/platform/runs/999999").status_code == 404

    def test_rejects_invalid_pagination(self, platform: TestClient) -> None:
        assert platform.get("/api/v1/platform/runs", params={"limit": 0}).status_code == 422
        assert platform.get("/api/v1/platform/runs", params={"offset": -1}).status_code == 422


class TestMetricsApi:
    """度量接口。"""

    def test_trends_reflect_runs(self, platform_with_token, seeded_cases) -> None:
        for _ in range(2):
            platform_with_token.post("/api/v1/platform/runs", json={"wait": True})

        points = platform_with_token.get("/api/v1/platform/trends").json()

        assert len(points) == 2
        assert [point["pass_rate"] for point in points] == [100.0, 100.0]
        assert all(point["duration_ms"] is not None for point in points)

    def test_trends_are_chronological(self, platform_with_token, seeded_cases) -> None:
        """趋势必须按时间正序——倒序会让图表看起来「通过率在倒退」。"""
        for _ in range(3):
            platform_with_token.post("/api/v1/platform/runs", json={"wait": True})

        trends = platform_with_token.get("/api/v1/platform/trends").json()
        ids = [point["run_id"] for point in trends]

        assert ids == sorted(ids)

    def test_summary_shape(self, platform_with_token, seeded_cases) -> None:
        body = platform_with_token.get("/api/v1/platform/summary").json()

        assert body["total_cases"] == len(seeded_cases)
        assert body["enabled_cases"] == len(seeded_cases)
        assert body["flaky_candidates"] == []

    def test_flaky_endpoint(self, platform: TestClient) -> None:
        assert platform.get("/api/v1/platform/flaky").json() == []

    def test_healthz_reports_target(self, platform: TestClient, tax_server) -> None:
        body = platform.get("/healthz").json()

        assert body["status"] == "ok"
        assert body["target_base_url"] == tax_server.base_url


# --------------------------------------------------------------------------- #
# 实时推送
# --------------------------------------------------------------------------- #


class TestRunEventBroker:
    """事件广播器（含跨线程投递）。"""

    def test_delivers_to_subscriber(self) -> None:
        broker = RunEventBroker()
        queue = broker.subscribe("1")

        broker.publish("1", {"type": "hello"})

        assert queue.get_nowait() == {"type": "hello"}

    def test_publish_without_subscribers_is_noop(self) -> None:
        broker = RunEventBroker()

        broker.publish("nobody", {"type": "hello"})  # 不应抛异常

    def test_fans_out_to_all_subscribers(self) -> None:
        broker = RunEventBroker()
        first = broker.subscribe("1")
        second = broker.subscribe("1")

        broker.publish("1", {"type": "broadcast"})

        assert first.get_nowait() == second.get_nowait() == {"type": "broadcast"}

    def test_isolates_different_runs(self) -> None:
        broker = RunEventBroker()
        mine = broker.subscribe("1")
        theirs = broker.subscribe("2")

        broker.publish("1", {"type": "only-mine"})

        assert mine.qsize() == 1
        assert theirs.qsize() == 0

    def test_unsubscribe_stops_delivery(self) -> None:
        broker = RunEventBroker()
        queue = broker.subscribe("1")
        broker.unsubscribe("1", queue)

        broker.publish("1", {"type": "dropped"})

        assert queue.qsize() == 0
        assert broker.subscriber_count("1") == 0

    def test_unsubscribe_is_idempotent(self) -> None:
        broker = RunEventBroker()
        queue = broker.subscribe("1")

        broker.unsubscribe("1", queue)
        broker.unsubscribe("1", queue)  # 不应抛异常

    def test_drops_oldest_when_queue_is_full(self) -> None:
        """队列满时丢最旧事件而不是阻塞发布方。

        执行线程绝不能因为某个慢客户端而被拖住——丢事件比拖垮执行可接受得多。
        """
        broker = RunEventBroker()
        queue = broker.subscribe("1")

        for index in range(QUEUE_MAX_SIZE + 10):
            broker.publish("1", {"seq": index})

        assert queue.qsize() == QUEUE_MAX_SIZE
        # 最新的事件一定在队列里
        last = None
        while not queue.empty():
            last = queue.get_nowait()
        assert last == {"seq": QUEUE_MAX_SIZE + 9}

    def test_publishes_across_threads(self) -> None:
        """执行发生在工作线程，推送发生在事件循环——跨线程投递必须安全。

        未绑定循环时 ``publish`` 直接入队，因此这里验证的是「多线程并发发布不丢事件、
        不抛异常」。
        """
        broker = RunEventBroker()
        queue = broker.subscribe("1")
        errors: list[BaseException] = []
        total = 40

        def publish_from_thread(worker: int) -> None:
            try:
                for index in range(10):
                    broker.publish("1", {"worker": worker, "index": index})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=publish_from_thread, args=(i,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        assert not errors, f"并发发布出现异常：{errors}"
        assert queue.qsize() == total


class TestWebSocketStream:
    """通过真实 WebSocket 连接验证实时推送。"""

    def test_sends_connected_ack(self, platform: TestClient) -> None:
        """连接后立即回 ``connected``，让客户端确认订阅成功。

        否则在长任务开始前，客户端无法区分「连接成功但暂无事件」与「连接失败」。
        """
        run_id = platform.post("/api/v1/platform/runs", json={"wait": True}).json()["id"]

        with platform.websocket_connect(f"/api/v1/platform/runs/{run_id}/stream") as socket:
            first = socket.receive_json()

        assert first["type"] == "connected"
        assert first["run_id"] == run_id

    def test_receives_events_for_matching_run(self, platform_app: FastAPI) -> None:
        """订阅者应收到指定执行的事件。"""
        broker: RunEventBroker = platform_app.state.run_event_broker
        client = TestClient(platform_app)

        with client:
            with client.websocket_connect("/api/v1/platform/runs/42/stream") as socket:
                assert socket.receive_json()["type"] == "connected"

                # 从「执行线程」侧发布，模拟真实调用路径
                threading.Thread(
                    target=broker.publish, args=("42", {"type": "case_finished", "case": "X"})
                ).start()

                event = socket.receive_json()

        assert event["type"] == "case_finished"
        assert event["case"] == "X"

    def test_unsubscribes_on_disconnect(self, platform_app: FastAPI) -> None:
        """断开后必须清理订阅，否则字典会随执行次数无限增长。"""
        broker: RunEventBroker = platform_app.state.run_event_broker
        client = TestClient(platform_app)

        with client:
            with client.websocket_connect("/api/v1/platform/runs/77/stream"):
                assert broker.subscriber_count("77") == 1

        assert broker.subscriber_count("77") == 0


# --------------------------------------------------------------------------- #
# 定时调度
# --------------------------------------------------------------------------- #


class TestRunScheduler:
    """定时调度（不依赖真实定时器，直接驱动 ``tick``）。"""

    def test_tick_invokes_callback(self) -> None:
        calls: list[str | None] = []

        scheduler = RunScheduler(run_once=lambda tag: calls.append(tag) or 1, tag="smoke")

        assert scheduler.tick() == 1
        assert calls == ["smoke"]
        assert scheduler.executions == 1

    def test_tick_swallows_exceptions(self) -> None:
        """单次执行失败不能让调度线程退出——否则定时任务会静默停摆。"""

        def explode(_tag: str | None) -> int:
            raise RuntimeError("目标服务不可达")

        scheduler = RunScheduler(run_once=explode)

        assert scheduler.tick() is None
        assert scheduler.executions == 0

    def test_start_and_stop(self) -> None:
        scheduler = RunScheduler(run_once=lambda _tag: 1, interval_seconds=3600)

        scheduler.start()
        assert wait_for_scheduler_ready(scheduler)
        assert scheduler.status()["enabled"] is True

        scheduler.stop()
        assert scheduler.running is False
        assert scheduler.status()["enabled"] is False

    def test_start_is_idempotent(self) -> None:
        scheduler = RunScheduler(run_once=lambda _tag: 1, interval_seconds=3600)

        scheduler.start()
        first_thread = scheduler._thread  # noqa: SLF001 - 验证未重复启动
        scheduler.start()
        try:
            assert scheduler._thread is first_thread  # noqa: SLF001
        finally:
            scheduler.stop()

    def test_waits_one_interval_before_first_run(self) -> None:
        """启动后先等一个周期再执行。

        服务刚起来时立刻压一轮没有意义，反而会与初始化/建表竞争资源。
        """
        calls: list[int] = []
        scheduler = RunScheduler(run_once=lambda _tag: calls.append(1) or 1, interval_seconds=3600)

        scheduler.start()
        try:
            time.sleep(0.2)
            assert calls == []
        finally:
            scheduler.stop()

    def test_status_shape(self) -> None:
        scheduler = RunScheduler(run_once=lambda _tag: 7, interval_seconds=60, tag="regression")

        status = scheduler.status()

        assert status["enabled"] is False
        assert status["interval_seconds"] == 60
        assert status["tag"] == "regression"
        assert status["last_run_id"] is None
        assert status["next_run_at"] is None

    def test_enforces_minimum_interval(self) -> None:
        """间隔必须为正，否则 ``Event.wait(0)`` 会退化成忙循环。"""
        scheduler = RunScheduler(run_once=lambda _tag: 1, interval_seconds=0)

        assert scheduler.interval_seconds == 1

    def test_api_reports_scheduler_status(self, platform: TestClient) -> None:
        body = platform.get("/api/v1/platform/scheduler").json()

        assert body["enabled"] is False
        assert body["interval_seconds"] > 0

    def test_scheduler_can_drive_platform_service(self, platform_with_token, seeded_cases) -> None:
        """调度器与平台服务的集成：一次 tick 完成一轮真实执行。"""
        service = platform_with_token.app.state.platform_service
        scheduler = RunScheduler(
            run_once=lambda tag: _run_and_wait(service, tag), interval_seconds=3600
        )

        run_id = scheduler.tick()

        assert run_id is not None
        finished = service.get_run(run_id)
        assert finished is not None
        assert finished.status.value == "passed"
        assert scheduler.executions == 1


def _run_and_wait(service, tag: str | None) -> int:  # noqa: ANN001
    """同步执行一轮，返回 run_id（与 app 内的调度回调同构）。"""
    run = service.create_run(trigger="schedule", tag=tag)
    finished = service.execute_run(run.id)
    return finished.id if finished is not None else run.id


class TestAsyncioQueueSemantics:
    """补充验证 asyncio.Queue 在绑定事件循环后的行为。"""

    def test_bind_loop_and_publish_from_thread(self) -> None:
        """绑定循环后，从其他线程发布必须通过 ``call_soon_threadsafe`` 投递。

        直接 ``put_nowait`` 在跨线程场景下不是线程安全的；这条用例确认绑定循环后
        事件仍能到达消费者。
        """

        async def scenario() -> dict:
            broker = RunEventBroker()
            broker.bind_loop(asyncio.get_running_loop())
            queue = broker.subscribe("1")

            threading.Thread(target=broker.publish, args=("1", {"type": "from-thread"})).start()

            return await asyncio.wait_for(queue.get(), timeout=5)

        event = asyncio.run(scenario())

        assert event == {"type": "from-thread"}
