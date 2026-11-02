"""AI 层夹具与共享工具。"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from support.servers import LiveServer, LiveServerContext
from tax_service.ai.provider import RecordingProvider, RuleBasedProvider
from tax_service.api.app import create_app
from tax_service.config import Settings
from tax_service.platform.app import create_platform_app


@pytest.fixture
def openapi_document(app_settings: Settings) -> dict[str, Any]:
    """从真实的被测应用取 OpenAPI 文档——生成依据必须是真契约，不是手写片段。"""
    application = create_app(app_settings)
    with TestClient(application) as client:
        return client.get("/openapi.json").json()


@pytest.fixture
def rule_provider(openapi_document: dict[str, Any]) -> RuleBasedProvider:
    return RuleBasedProvider(openapi_document)


@pytest.fixture
def llm_provider() -> RecordingProvider:
    """录制型替身：记录提示词、回放预设输出。"""
    return RecordingProvider()


@pytest.fixture
def tax_server(app_settings: Settings) -> Iterator[LiveServer]:
    with LiveServerContext(create_app(app_settings), log_level="error") as server:
        yield server


@pytest.fixture
def ai_platform(app_settings: Settings, tax_server: LiveServer) -> TestClient:
    """带 AI 路由的平台（离线规则提供者）。"""
    settings = app_settings.model_copy(
        update={
            "database_url": "sqlite://",
            "platform_target_base_url": tax_server.base_url,
            "platform_max_workers": 2,
        }
    )
    application: FastAPI = create_platform_app(settings)
    with TestClient(application) as client:
        application.state.test_client = client
        yield client
