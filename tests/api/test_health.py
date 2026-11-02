"""接口层 · 健康检查与元信息。"""

from __future__ import annotations

import pytest

from support.api_client import ApiClient
from support.marks import current_impl

pytestmark = [pytest.mark.api, pytest.mark.smoke]


class TestHealthEndpoint:
    """``GET /healthz`` 是运维契约，必须稳定。"""

    def test_returns_ok(self, api: ApiClient) -> None:
        body = api.expect(api.health(), 200)

        assert body["status"] == "ok"
        assert body["version"]
        assert body["app_name"]

    def test_reports_active_calculator_impl(self, api: ApiClient) -> None:
        """健康检查必须如实报告当前计算实现。

        这是可观测性要求：排查「同一个工资为什么两个环境算出不同税额」时，
        第一件要看的就是这个字段。
        """
        body = api.expect(api.health(), 200)

        assert body["calculator_impl"] == current_impl()

    def test_reports_dependencies(self, api: ApiClient) -> None:
        body = api.expect(api.health(), 200)

        assert body["database"] == "sqlite"
        assert body["cache"] == "memory"
        assert body["environment"] == "test"

    def test_does_not_require_authentication(self, api: ApiClient) -> None:
        """存活探针不能被鉴权挡住，否则编排系统无法判断实例状态。"""
        assert api.health().status_code == 200


class TestOpenApiDocument:
    """OpenAPI 文档是契约测试的输入，必须可访问且结构完整。"""

    def test_is_available(self, api: ApiClient) -> None:
        body = api.expect(api.openapi(), 200)

        assert body["openapi"].startswith("3.")
        assert body["info"]["title"]

    def test_declares_all_business_paths(self, api: ApiClient) -> None:
        paths = api.expect(api.openapi(), 200)["paths"]

        expected = {
            "/healthz",
            "/api/v1/auth/register",
            "/api/v1/auth/login",
            "/api/v1/auth/me",
            "/api/v1/tax/calculate",
            "/api/v1/tax/batch",
            "/api/v1/tax/policy",
            "/api/v1/tax/records",
            "/api/v1/tax/records/{record_id}",
        }
        assert expected <= set(paths), f"缺少路径：{expected - set(paths)}"

    def test_protected_paths_declare_bearer_security(self, api: ApiClient) -> None:
        """受保护接口应在文档中声明鉴权要求，便于调用方生成正确请求。"""
        document = api.expect(api.openapi(), 200)

        protected = ["/api/v1/auth/me", "/api/v1/tax/calculate", "/api/v1/tax/records"]
        for path in protected:
            operations = document["paths"][path]
            operation = next(iter(operations.values()))
            assert "security" in operation, f"{path} 未声明 security"
