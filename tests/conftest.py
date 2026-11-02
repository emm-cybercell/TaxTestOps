"""全局测试夹具。

隔离策略
--------
每个测试函数获得一个**全新的应用实例**：独立的内存数据库（``StaticPool`` 保证
同一连接）、独立的内存缓存、独立的限流计数。因此用例之间不存在顺序依赖，
可以安全地用 ``pytest-xdist`` 并行执行。

这是分层框架的地基——如果夹具泄漏状态，上层用例会开始「偶尔失败」，
最终演变成没人敢改的 flaky 测试集。
"""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from support.api_client import ApiClient, UserSession
from support.factories import random_password, random_username
from support.marks import current_impl, is_legacy_mode
from support.servers import LiveServer, LiveServerContext
from tax_service.api.app import create_app
from tax_service.api.security import hash_password
from tax_service.config import Settings
from tax_service.db.base import init_schema

#: 指向一个必然拒绝连接的地址：让「政策服务不可用 → 降级」成为测试的默认前提，
#: 需要验证远程成功路径的用例显式 mock（见 tests/integration/test_policy_client.py）。
UNREACHABLE_POLICY_SERVICE = "http://127.0.0.1:1/api/v1/parameters"


# --------------------------------------------------------------------------- #
# 运行模式
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def calculator_impl() -> str:
    """本次运行使用的个税计算实现。"""
    return current_impl()


@pytest.fixture(scope="session")
def legacy_mode() -> bool:
    """是否运行在保留缺陷的模式下。"""
    return is_legacy_mode()


# --------------------------------------------------------------------------- #
# 应用与客户端
# --------------------------------------------------------------------------- #


@pytest.fixture
def app_settings() -> Settings:
    """测试配置：内存库 + 内存缓存 + 随机密钥 + 近乎关闭的限流 + 低成本哈希 + 默认离线测试。"""
    return Settings(
        environment="test",
        database_url="sqlite://",
        calculator_impl=current_impl(),  # type: ignore[arg-type]
        cache_backend="memory",
        jwt_secret=secrets.token_urlsafe(32),
        jwt_expire_minutes=30,
        bcrypt_rounds=4,
        rate_limit_per_minute=100_000,
        policy_service_url=UNREACHABLE_POLICY_SERVICE,
        policy_service_timeout_seconds=0.5,
        batch_max_size=100,
        # 测试套件默认隔离外部 API 密钥，保证测试离线、确定性且不受外部网络/限流干扰
        ai_enabled=False,
        ai_api_key=None,
    )


@pytest.fixture
def app(app_settings: Settings) -> Iterator[FastAPI]:
    """每个用例一个全新的应用实例（含独立数据库与缓存）。"""
    application = create_app(app_settings)
    with TestClient(application) as test_client:
        application.state.test_client = test_client
        yield application


@pytest.fixture
def app_factory(app_settings: Settings) -> Iterator[Callable[..., FastAPI]]:
    """按需构造带特殊配置的应用实例（如低限流阈值）。

    所有实例在用例结束后统一关闭，避免遗留后台线程或未释放的连接池。
    """
    created: list[FastAPI] = []

    def _build(**overrides: object) -> FastAPI:
        settings = app_settings.model_copy(update=overrides)
        application = create_app(settings)
        client = TestClient(application)
        client.__enter__()
        application.state.test_client = client
        created.append(application)
        return application

    yield _build

    for application in created:
        application.state.test_client.__exit__(None, None, None)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    """底层 TestClient。"""
    return app.state.test_client


@pytest.fixture
def api(client: TestClient) -> ApiClient:
    """未登录的语义化接口客户端。

    ``timeout=None``：TestClient 走进程内直连，不接受超时参数。
    """
    return ApiClient(client, timeout=None)


# --------------------------------------------------------------------------- #
# 用户
# --------------------------------------------------------------------------- #


@pytest.fixture
def user_password() -> str:
    return random_password()


@pytest.fixture
def user(app: FastAPI, user_password: str) -> UserSession:
    """一个已注册但未登录的用户。"""
    username = random_username()
    record = app.state.user_repository.create(
        username,
        hash_password(user_password, rounds=app.state.settings.bcrypt_rounds),
        username,
    )
    return UserSession(username=username, password=user_password, user_id=record.id)


@pytest.fixture
def other_user(app: FastAPI) -> UserSession:
    """第二个用户，用于验证数据隔离与越权防护。"""
    username = random_username("other")
    password = random_password()
    record = app.state.user_repository.create(
        username,
        hash_password(password, rounds=app.state.settings.bcrypt_rounds),
        username,
    )
    return UserSession(username=username, password=password, user_id=record.id)


@pytest.fixture
def authed_api(api: ApiClient, user: UserSession) -> ApiClient:
    """已登录的接口客户端。"""
    return api.login_as(user.username, user.password)


@pytest.fixture
def other_authed_api(client: TestClient, other_user: UserSession) -> ApiClient:
    """第二个用户的已登录客户端（独立实例，避免与主客户端串号）。"""
    other = ApiClient(client, timeout=None)
    return other.login_as(other_user.username, other_user.password)


# --------------------------------------------------------------------------- #
# 并发 / 真实 HTTP 场景
# --------------------------------------------------------------------------- #


@pytest.fixture
def file_db_app(app_settings: Settings, tmp_path: Path) -> Iterator[FastAPI]:
    """使用**真实文件库**的应用实例，支持多线程并发连接。

    为什么并发测试不能复用内存库夹具：内存库用 ``StaticPool``，所有线程共享同一个
    DBAPI 连接，所谓「并发」实际是串行复用同一连接——既测不出真实竞态，还会因
    连接状态交叉而随机失败（flaky）。

    文件库让每个线程拿到独立连接，配合 ``WAL`` + ``busy_timeout`` 才能得到稳定结果。
    """
    db_path = tmp_path / "concurrency.db"
    settings = app_settings.model_copy(update={"database_url": f"sqlite:///{db_path.as_posix()}"})
    application = create_app(settings)
    init_schema(application.state.engine)
    try:
        yield application
    finally:
        application.state.engine.dispose()


@pytest.fixture
def live_server(file_db_app: FastAPI) -> Iterator[LiveServer]:
    """在后台线程启动真实 HTTP 服务（真实网络栈、真实线程池）。"""
    with LiveServerContext(file_db_app) as server:
        yield server


@pytest.fixture
def live_api(live_server: LiveServer) -> Iterator[ApiClient]:
    """指向真实服务的接口客户端。"""
    with httpx.Client(base_url=live_server.base_url) as transport:
        yield ApiClient(transport, timeout=30.0)


@pytest.fixture
def live_authed_api(live_api: ApiClient) -> ApiClient:
    """在真实服务上注册并登录一个用户。

    刻意走 HTTP 注册接口而不是直接写数据库：这样并发用例的前置条件也经过了
    完整的接口校验，不会因为「测试绕过了真实路径」而掩盖问题。
    """
    username = random_username("live")
    password = random_password()

    response = live_api.register(username, password)
    if response.status_code != 201:
        raise AssertionError(f"注册失败：{response.status_code} {response.detail()}")

    return live_api.login_as(username, password)


# --------------------------------------------------------------------------- #
# 便捷访问器
# --------------------------------------------------------------------------- #


@pytest.fixture
def session_factory(app: FastAPI):
    return app.state.session_factory


@pytest.fixture
def cache(app: FastAPI):
    return app.state.cache


@pytest.fixture
def policy_client(app: FastAPI):
    return app.state.policy_client


@pytest.fixture
def declarations(app: FastAPI):
    return app.state.declaration_repository


@pytest.fixture
def users(app: FastAPI):
    return app.state.user_repository
