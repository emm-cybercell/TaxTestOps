"""性能层夹具。

性能测试需要**真实网络栈 + 真实并发**，因此使用文件数据库 + 后台 uvicorn 服务
（与接口层的进程内 ``TestClient`` 不同——后者测不出网络与线程池开销）。

负载规模刻意保持很小，让性能用例能在几秒内跑完、随主套件一起执行。
需要压测时用环境变量放大：

    TAX_PERF_REQUESTS=2000 TAX_PERF_WORKERS=32 python -m pytest tests/perf -q -s
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from support.factories import random_password, random_username
from support.load import DEFAULT_TOKEN_POOL_SIZE
from support.servers import LiveServer, LiveServerContext
from tax_service.api.app import create_app
from tax_service.db.base import init_schema


@pytest.fixture
def perf_app(app_settings) -> Iterator[FastAPI]:
    """性能测试专用应用：关闭限流，避免 429 干扰延迟测量。

    限流本身的行为由 ``tests/integration/test_rate_limit.py`` 覆盖；
    混在性能测试里会让「延迟高」与「被限流」两种现象无法区分。
    """
    db_path = Path(os.environ.get("TEMP", ".")) / f"tax_perf_{os.getpid()}.db"
    settings = app_settings.model_copy(
        update={
            "database_url": f"sqlite:///{db_path.as_posix()}",
            "rate_limit_per_minute": 0,
        }
    )
    application = create_app(settings)
    init_schema(application.state.engine)
    try:
        yield application
    finally:
        application.state.engine.dispose()
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            candidate.unlink(missing_ok=True)


@pytest.fixture
def perf_server(perf_app: FastAPI) -> Iterator[LiveServer]:
    with LiveServerContext(perf_app, log_level="error") as server:
        yield server


@pytest.fixture
def token_pool(perf_server: LiveServer) -> list[str]:
    """预注册若干用户并取得令牌，用于多用户并发压测。

    通过真实接口注册与登录，而不是直接写库——前置条件也走真实路径，
    避免「测试绕过了被测代码」而掩盖问题。
    """
    tokens: list[str] = []

    with httpx.Client(base_url=perf_server.base_url, timeout=30.0) as client:
        for index in range(DEFAULT_TOKEN_POOL_SIZE):
            username = random_username(f"perf{index}")
            password = random_password()

            created = client.post(
                "/api/v1/auth/register", json={"username": username, "password": password}
            )
            assert created.status_code == 201, f"注册压测用户失败：{created.text}"

            logged_in = client.post(
                "/api/v1/auth/login", json={"username": username, "password": password}
            )
            assert logged_in.status_code == 200, f"压测用户登录失败：{logged_in.text}"

            tokens.append(logged_in.json()["access_token"])

    return tokens
