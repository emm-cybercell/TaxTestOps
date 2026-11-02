"""接口层 · 幂等性。

背景
----
这是本项目在服务化过程中**真实修出的一个缺陷**，因此单独成文件并保留完整回归用例。

缺陷表现
--------
``POST /api/v1/tax/calculate`` 携带相同 ``idempotency_key`` 重复提交时：

- ``record_id`` 正确返回了首次落库的 ID（说明去重生效）；
- 但响应体里的 ``tax_amount`` / ``taxable_income`` 是**本次重新计算**的结果。

于是同一个幂等键在不同时刻返回不同金额——调用方拿到「首次的 ID + 本次的金额」，
幂等语义形同虚设。若调用方据此对账，会产生无法解释的差额。

修复
----
幂等命中时用**已落库记录**重建整个响应，而不是复用本次计算结果。
"""

from __future__ import annotations

import threading

import pytest

from support.api_client import ApiClient

pytestmark = [pytest.mark.api, pytest.mark.regression]


class TestCalculateIdempotency:
    """单次测算的幂等语义。"""

    def test_same_key_returns_same_record_id(self, authed_api: ApiClient) -> None:
        first = authed_api.expect(authed_api.calculate(20_000, idempotency_key="k1"), 200)
        second = authed_api.expect(authed_api.calculate(20_000, idempotency_key="k1"), 200)

        assert first["record_id"] == second["record_id"]

    def test_same_key_creates_only_one_record(self, authed_api: ApiClient) -> None:
        authed_api.calculate(20_000, idempotency_key="k1")
        authed_api.calculate(20_000, idempotency_key="k1")
        authed_api.calculate(20_000, idempotency_key="k1")

        assert authed_api.expect(authed_api.records(), 200)["total"] == 1

    def test_replay_returns_original_amount_even_with_different_payload(
        self, authed_api: ApiClient
    ) -> None:
        """**缺陷回归用例**：幂等重放必须返回首次落库的金额，即使本次载荷不同。

        构造方式：首次提交工资 20000（税额 2890），随后用同一幂等键提交工资 99999。
        若实现返回本次重算结果，税额会变成 28889——这正是修复前的行为。
        """
        first = authed_api.expect(authed_api.calculate(20_000, idempotency_key="k1"), 200)
        replay = authed_api.expect(authed_api.calculate(99_999, idempotency_key="k1"), 200)

        assert replay["record_id"] == first["record_id"]
        assert replay["tax_amount"] == first["tax_amount"], "幂等重放返回了重新计算的结果"
        assert replay["taxable_income"] == first["taxable_income"]
        assert replay["created_at"] == first["created_at"]

    def test_replay_deductions_come_from_stored_person(self, authed_api: ApiClient) -> None:
        """幂等重放的扣除明细也必须来自已落库的纳税人信息，而非本次请求。"""
        first = authed_api.expect(
            authed_api.calculate(30_000, person={"children_num": 2}, idempotency_key="k1"), 200
        )
        replay = authed_api.expect(
            authed_api.calculate(30_000, person={"house_loans": True}, idempotency_key="k1"), 200
        )

        assert replay["deductions"] == first["deductions"]
        assert [item["code"] for item in replay["deductions"]] == ["child_education"]

    def test_replay_is_byte_identical(self, authed_api: ApiClient) -> None:
        """同一幂等键的重复请求，响应应完全一致（时间戳除外）。

        这是最强的幂等断言：不做字段级比对，直接比较整个响应体。
        """
        first = authed_api.expect(authed_api.calculate(20_000, idempotency_key="k1"), 200)
        replay = authed_api.expect(authed_api.calculate(20_000, idempotency_key="k1"), 200)

        assert replay == first

    def test_different_keys_create_separate_records(self, authed_api: ApiClient) -> None:
        first = authed_api.expect(authed_api.calculate(20_000, idempotency_key="k1"), 200)
        second = authed_api.expect(authed_api.calculate(20_000, idempotency_key="k2"), 200)

        assert first["record_id"] != second["record_id"]
        assert authed_api.expect(authed_api.records(), 200)["total"] == 2

    def test_without_key_repeated_submission_creates_multiple_records(
        self, authed_api: ApiClient
    ) -> None:
        """不传幂等键时不去重——契约必须明确，不能「默默帮用户去重」。"""
        authed_api.calculate(20_000)
        authed_api.calculate(20_000)

        assert authed_api.expect(authed_api.records(), 200)["total"] == 2

    def test_persist_false_ignores_idempotency_key(self, authed_api: ApiClient) -> None:
        """不落库时幂等键无意义，不应报错也不应写入任何数据。"""
        body = authed_api.expect(
            authed_api.calculate(20_000, persist=False, idempotency_key="k1"), 200
        )

        assert body["record_id"] is None
        assert authed_api.expect(authed_api.records(), 200)["total"] == 0

    @pytest.mark.parametrize(
        "key",
        [
            pytest.param("", id="空字符串"),
            pytest.param("x" * 65, id="超过长度上限"),
        ],
    )
    def test_rejects_invalid_key_format(self, authed_api: ApiClient, key: str) -> None:
        assert authed_api.calculate(20_000, idempotency_key=key).status_code == 422


class TestConcurrentIdempotency:
    """并发提交同一幂等键。

    这些用例跑在**真实 HTTP 服务 + 真实文件数据库**上，而不是进程内 TestClient：

    - 内存库的 ``StaticPool`` 让所有线程共享一个连接，「并发」实际是串行复用，
      既测不出竞态，还会因连接状态交叉而随机失败；
    - TestClient 的线程安全性也不是它承诺的能力。

    因此并发用例统一使用 ``live_authed_api``（uvicorn 后台线程 + 文件库）。
    """

    CONCURRENCY = 8

    def _submit_concurrently(self, api: ApiClient, keys: list[str]) -> list[int]:
        """并发提交多个请求，返回全部状态码。"""
        barrier = threading.Barrier(len(keys))
        statuses: list[int] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def submit(key: str) -> None:
            try:
                barrier.wait(timeout=15)
                response = api.calculate(20_000, idempotency_key=key)
                with lock:
                    statuses.append(response.status_code)
            except BaseException as exc:  # noqa: BLE001 - 收集后统一断言
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=submit, args=(key,)) for key in keys]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert not errors, f"并发提交出现异常：{errors}"
        return statuses

    def test_concurrent_same_key_yields_one_record(self, live_authed_api: ApiClient) -> None:
        """并发提交同一幂等键只应产生一条记录。

        兜底机制是数据库唯一约束，而不是「先查再写」——后者在并发下存在检查与写入
        之间的竞态窗口，多条请求可能同时通过检查，最终写入重复数据。
        """
        statuses = self._submit_concurrently(live_authed_api, ["race-key"] * self.CONCURRENCY)

        assert set(statuses) == {200}, f"存在非 200 响应：{sorted(statuses)}"

        page = live_authed_api.expect(live_authed_api.records(), 200)
        assert page["total"] == 1, f"并发提交产生了 {page['total']} 条记录，应只有 1 条"

    def test_concurrent_same_key_returns_identical_record_id(
        self, live_authed_api: ApiClient
    ) -> None:
        """并发重放必须都指向同一条记录。"""
        barrier = threading.Barrier(self.CONCURRENCY)
        record_ids: list[int] = []
        lock = threading.Lock()

        def submit() -> None:
            barrier.wait(timeout=15)
            body = live_authed_api.expect(
                live_authed_api.calculate(20_000, idempotency_key="race-key-2"), 200
            )
            with lock:
                record_ids.append(body["record_id"])

        threads = [threading.Thread(target=submit) for _ in range(self.CONCURRENCY)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

        assert len(set(record_ids)) == 1, f"返回了多个不同 record_id：{sorted(set(record_ids))}"

    def test_concurrent_different_keys_create_all_records(self, live_authed_api: ApiClient) -> None:
        """并发提交不同幂等键时，每条都应落库——确认唯一约束没有过度拦截。"""
        keys = [f"parallel-{index}" for index in range(self.CONCURRENCY)]

        statuses = self._submit_concurrently(live_authed_api, keys)

        assert set(statuses) == {200}
        page = live_authed_api.expect(live_authed_api.records(limit=100), 200)
        assert page["total"] == self.CONCURRENCY

    def test_sequential_replay_after_concurrency(self, live_authed_api: ApiClient) -> None:
        """并发风暴之后，顺序重放仍应命中同一条记录（约束没有留下脏状态）。"""
        self._submit_concurrently(live_authed_api, ["storm"] * self.CONCURRENCY)

        first = live_authed_api.expect(
            live_authed_api.calculate(20_000, idempotency_key="storm"), 200
        )
        second = live_authed_api.expect(
            live_authed_api.calculate(99_999, idempotency_key="storm"), 200
        )

        assert first == second
        assert live_authed_api.expect(live_authed_api.records(), 200)["total"] == 1
