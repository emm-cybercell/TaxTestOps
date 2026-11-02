"""测试平台层夹具。

平台测试需要**两个服务**同时在线：被测的个税服务，以及平台自身。
因此夹具链是：起一个真实 HTTP 的个税服务 → 用它作为平台的目标地址 → 起平台。

用真实 HTTP 而不是 ``TestClient`` 直连，是因为平台的核心行为就是「发 HTTP 请求并
判定结果」——把传输层替换掉，等于把被测行为换掉了。

夹具一致性
----------
``service``、``seeded_cases`` 都派生自**同一个** ``platform`` 实例。
早期版本里 ``seeded_cases`` 用了另一个应用实例，结果「导入的用例」与「执行时查的
用例」在两个库里，测试报出难以理解的空结果。夹具共享同一个实例是硬约束。
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from support.factories import random_password, random_username
from support.platform_cases import HEALTHY_CASES
from support.servers import LiveServer, LiveServerContext
from tax_service.api.app import create_app
from tax_service.config import Settings
from tax_service.platform.app import create_platform_app
from tax_service.platform.service import PlatformService


@pytest.fixture
def tax_app(app_settings: Settings) -> FastAPI:
    """被测的个税服务。"""
    return create_app(app_settings)


@pytest.fixture
def tax_server(tax_app: FastAPI) -> Iterator[LiveServer]:
    """以真实 HTTP 暴露被测服务。"""
    with LiveServerContext(tax_app, log_level="error") as server:
        yield server


@pytest.fixture
def target_token(tax_server: LiveServer) -> str:
    """在被测服务上注册并登录，取得平台调用受保护接口所需的令牌。"""
    username = random_username("platform")
    password = random_password()

    with httpx.Client(base_url=tax_server.base_url, timeout=30.0) as client:
        created = client.post(
            "/api/v1/auth/register", json={"username": username, "password": password}
        )
        assert created.status_code == 201, f"注册失败：{created.text}"

        logged_in = client.post(
            "/api/v1/auth/login", json={"username": username, "password": password}
        )
        assert logged_in.status_code == 200, f"登录失败：{logged_in.text}"

        return logged_in.json()["access_token"]


@pytest.fixture
def platform_settings(app_settings: Settings, tax_server: LiveServer) -> Settings:
    """平台配置：独立数据库，目标指向被测服务。"""
    return app_settings.model_copy(
        update={
            "database_url": "sqlite://",
            "platform_target_base_url": tax_server.base_url,
            "platform_max_workers": 4,
            "platform_schedule_enabled": False,
        }
    )


@pytest.fixture
def platform_app(platform_settings: Settings) -> FastAPI:
    return create_platform_app(platform_settings)


@pytest.fixture
def platform(platform_app: FastAPI) -> Iterator[TestClient]:
    """平台的接口客户端（进程内直连）。进入上下文以触发建表。"""
    with TestClient(platform_app) as client:
        platform_app.state.test_client = client
        yield client


@pytest.fixture
def service(platform: TestClient) -> PlatformService:
    """平台服务。

    依赖 ``platform``（TestClient 上下文）而不是裸 ``platform_app``：
    建表发生在 lifespan 里，不进入上下文就没有表——这正是「夹具依赖关系写错会
    让测试报出莫名其妙的 no such table」的典型场景。
    """
    return platform.app.state.platform_service


@pytest.fixture
def platform_with_token(platform: TestClient, target_token: str) -> TestClient:
    """给平台注入目标令牌。

    直接改 ``target_token`` 而不是重建一个应用实例，是为了让用例定义与执行器
    共享同一个数据库——否则「导入了用例」与「执行时查的用例」会落在两个库里。
    """
    platform.app.state.platform_service.target_token = target_token
    return platform


@pytest.fixture
def seeded_cases(platform_with_token: TestClient) -> list[dict]:
    """导入一组全部应通过的用例（平台已带目标令牌）。"""
    created = []
    for definition in HEALTHY_CASES:
        response = platform_with_token.post("/api/v1/platform/cases", json=definition)
        assert response.status_code == 201, f"导入用例失败：{response.text}"
        created.append(response.json())
    return created
