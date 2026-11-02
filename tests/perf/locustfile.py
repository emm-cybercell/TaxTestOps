"""Locust 负载场景。

用法::

    # 有界面（浏览器打开 http://localhost:8089）
    .venv/Scripts/python.exe -m locust -f tests/perf/locustfile.py --host http://127.0.0.1:8000

    # 无界面（CI / 快速验证）
    .venv/Scripts/python.exe -m locust -f tests/perf/locustfile.py \
        --host http://127.0.0.1:8000 --headless -u 20 -r 5 -t 30s \
        --html artifacts/perf/report.html --csv artifacts/perf/stats

先启动服务::

    .venv/Scripts/python.exe -m uvicorn tax_service.api.app:app --factory --port 8000

与 ``test_load.py`` 的分工
--------------------------
``test_load.py`` 是**断言型**负载测试（跑在 pytest 里、有明确 SLO 阈值、进 CI 门禁）；
本文件是**探索型**负载工具（供人工压测、找拐点、出报告），不参与自动化断言。
"""

from __future__ import annotations

import itertools
import secrets

from locust import HttpUser, between, task

CALCULATE_WEIGHT = 4
READ_WEIGHT = 4
AUTH_WEIGHT = 1
HEALTH_WEIGHT = 1

_id_counter = itertools.count()
_run_suffix = secrets.token_hex(3)


def randint(low: int, high: int) -> int:
    """闭区间随机整数。

    统一走 ``secrets`` 而不是 ``random``：压测数据本身不涉及安全，
    但保持「项目内不出现非加密随机源」的一致性，可以避免安全扫描产生噪声告警。
    """
    return low + secrets.randbelow(high - low + 1)


def choice(options: list):
    """从列表中随机取值。"""
    return options[secrets.randbelow(len(options))]


def chance(probability: float) -> bool:
    """以给定概率返回 ``True``。"""
    return secrets.randbelow(1_000) < int(probability * 1_000)


class TaxServiceUser(HttpUser):
    """模拟普通申报用户的流量构成。"""

    wait_time = between(0.1, 0.6)

    def on_start(self) -> None:
        """注册并登录，取得令牌。

        用户名带随机后缀，避免并发压测时相互冲突；密码同样随机生成，
        不把任何凭据写进源码。
        """
        self.username = f"locust-{_run_suffix}-{next(_id_counter)}"
        self.password = secrets.token_urlsafe(16)
        self.idempotency_seq = itertools.count()
        self.token: str | None = None

        with self.client.post(
            "/api/v1/auth/register",
            json={"username": self.username, "password": self.password},
            catch_response=True,
            name="POST /api/v1/auth/register",
        ) as response:
            if response.status_code not in (201, 409):
                response.failure(f"注册失败：{response.status_code} {response.text[:120]}")

        with self.client.post(
            "/api/v1/auth/login",
            json={"username": self.username, "password": self.password},
            catch_response=True,
            name="POST /api/v1/auth/login",
        ) as response:
            if response.status_code != 200:
                response.failure(f"登录失败：{response.status_code} {response.text[:120]}")
                return
            self.token = response.json()["access_token"]

    # ------------------------------------------------------------------ #

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def _random_person(self) -> dict[str, object]:
        """随机生成合法纳税人——覆盖各扣除分支，而不是反复打同一条路径。

        固定输入会让服务端缓存与数据库页命中率虚高，压出来的数字没有参考价值。
        """
        house_loans = chance(0.3)
        return {
            "children_num": choice([0, 0, 1, 2, 3]),
            "is_keep_stu": choice([0, 0, 1, 2]),
            "house_loans": house_loans,
            # 住房贷款与住房租金互斥，同时为真会被 422 拒绝
            "is_rent_house": 0 if house_loans else choice([0, 1, 2, 3]),
            "supporting_elderly": chance(0.4),
            "sibling_num": choice([0, 0, 1, 2]),
        }

    # ------------------------------------------------------------------ #
    # 任务
    # ------------------------------------------------------------------ #

    @task(CALCULATE_WEIGHT)
    def calculate(self) -> None:
        """单次测算并落库。"""
        if not self.token:
            return

        payload = {
            "salary": randint(3_000, 120_000),
            "person": self._random_person(),
            "idempotency_key": f"{self.username}-{next(self.idempotency_seq)}",
        }
        with self.client.post(
            "/api/v1/tax/calculate",
            json=payload,
            headers=self.auth_headers,
            catch_response=True,
            name="POST /api/v1/tax/calculate",
        ) as response:
            if response.status_code != 200:
                response.failure(f"{response.status_code} {response.text[:120]}")

    @task(1)
    def batch_calculate(self) -> None:
        """批量申报——更接近企业客户的真实用法。"""
        if not self.token:
            return

        payload = {
            "items": [
                {"salary": randint(3_000, 80_000), "person": self._random_person()}
                for _ in range(randint(1, 10))
            ]
        }
        with self.client.post(
            "/api/v1/tax/batch",
            json=payload,
            headers=self.auth_headers,
            catch_response=True,
            name="POST /api/v1/tax/batch",
        ) as response:
            if response.status_code != 200:
                response.failure(f"{response.status_code} {response.text[:120]}")

    @task(READ_WEIGHT)
    def list_records(self) -> None:
        if not self.token:
            return

        with self.client.get(
            "/api/v1/tax/records",
            params={"limit": choice([10, 20, 50])},
            headers=self.auth_headers,
            catch_response=True,
            name="GET /api/v1/tax/records",
        ) as response:
            if response.status_code != 200:
                response.failure(f"{response.status_code} {response.text[:120]}")

    @task(AUTH_WEIGHT)
    def me(self) -> None:
        if not self.token:
            return

        with self.client.get(
            "/api/v1/auth/me",
            headers=self.auth_headers,
            catch_response=True,
            name="GET /api/v1/auth/me",
        ) as response:
            if response.status_code != 200:
                response.failure(f"{response.status_code} {response.text[:120]}")

    @task(HEALTH_WEIGHT)
    def health(self) -> None:
        with self.client.get("/healthz", catch_response=True, name="GET /healthz") as response:
            if response.status_code != 200:
                response.failure(f"{response.status_code}")


class AnonymousUser(HttpUser):
    """未认证流量：验证鉴权拒绝路径在负载下不成为瓶颈或漏洞。"""

    weight = 1
    wait_time = between(0.5, 1.5)

    @task
    def rejected_access(self) -> None:
        with self.client.get(
            "/api/v1/tax/records",
            catch_response=True,
            name="GET /api/v1/tax/records（未认证）",
        ) as response:
            # 401 是预期结果，不算失败
            if response.status_code != 401:
                response.failure(f"未认证请求应返回 401，实际 {response.status_code}")

    @task
    def health(self) -> None:
        self.client.get("/healthz", name="GET /healthz（匿名）")
