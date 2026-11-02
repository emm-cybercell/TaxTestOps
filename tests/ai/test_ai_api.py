"""AI 层 · 接口行为（生成 / 导入 / 归因 / 状态）。"""

from __future__ import annotations

import secrets

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from support.servers import LiveServer
from tax_service.config import Settings
from tax_service.platform.ai_api import validate_document_url
from tax_service.platform.app import create_platform_app

pytestmark = [pytest.mark.platform, pytest.mark.integration]


@pytest.fixture
def llm_platform(app_settings: Settings, tax_server: LiveServer) -> TestClient:
    """带假 API key 的平台实例（key 随机生成，仅用于验证模式切换，不会发出真实请求）。"""
    settings = app_settings.model_copy(
        update={
            "database_url": "sqlite://",
            "platform_target_base_url": tax_server.base_url,
            # 随机 token：只为让 build_provider 走 LLM 分支，不对应任何真实账号
            "ai_api_key": secrets.token_urlsafe(24),
            "ai_model": "test-model",
        }
    )
    application: FastAPI = create_platform_app(settings)
    with TestClient(application) as client:
        application.state.test_client = client
        yield client


class TestAiStatus:
    """``GET /ai/status`` 必须如实报告能力来源。"""

    def test_reports_rule_mode_without_key(self, ai_platform: TestClient) -> None:
        body = ai_platform.get("/api/v1/platform/ai/status").json()

        assert body["source"] == "rule"
        assert body["has_api_key"] is False
        assert body["model"] is None
        assert body["openapi_paths"] > 0

    def test_reports_llm_mode_with_key(self, llm_platform: TestClient) -> None:
        body = llm_platform.get("/api/v1/platform/ai/status").json()

        assert body["source"] == "llm"
        assert body["model"] == "test-model"
        assert body["has_api_key"] is True


class TestGenerateEndpoint:
    """``POST /ai/generate``。"""

    def test_generates_for_declared_operation(self, ai_platform: TestClient) -> None:
        response = ai_platform.post(
            "/api/v1/platform/ai/generate",
            params={"path": "/api/v1/tax/calculate", "method": "POST"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "rule"
        assert body["accepted_count"] > 0
        assert body["acceptance_rate"] == 100.0

        for case in body["generated"]:
            assert case["path"] == "/api/v1/tax/calculate"
            assert case["rationale"], "每条候选都必须附带生成理由"

    def test_generates_for_get_endpoint(self, ai_platform: TestClient) -> None:
        response = ai_platform.post(
            "/api/v1/platform/ai/generate", params={"path": "/healthz", "method": "GET"}
        )

        assert response.status_code == 200
        assert response.json()["accepted_count"] > 0

    def test_unknown_path_returns_404(self, ai_platform: TestClient) -> None:
        response = ai_platform.post(
            "/api/v1/platform/ai/generate",
            params={"path": "/api/v1/not/declared", "method": "GET"},
        )

        assert response.status_code == 404
        assert "不存在路径" in response.json()["detail"]

    def test_unknown_method_returns_404(self, ai_platform: TestClient) -> None:
        response = ai_platform.post(
            "/api/v1/platform/ai/generate", params={"path": "/healthz", "method": "DELETE"}
        )

        assert response.status_code == 404
        assert "未声明方法" in response.json()["detail"]

    def test_requirement_text_with_rule_provider_is_honest(
        self, ai_platform: TestClient
    ) -> None:
        """规则模式下传需求文本应明确告知无法处理，而不是返回空成功。"""
        response = ai_platform.post(
            "/api/v1/platform/ai/generate",
            params={"path": "/healthz", "method": "GET", "requirement": "支持批量导出"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["accepted_count"] == 0
        assert "ai_api_key" in body["rejected"][0]["reason"]


class TestGenerateImportEndpoint:
    """``POST /ai/generate/import``——AI 产出进入流水线的唯一入口。"""

    def test_imported_cases_are_disabled_by_default(self, ai_platform: TestClient) -> None:
        """AI 生成的内容必须经人确认才生效——这是信任边界，不是 UI 细节。"""
        response = ai_platform.post(
            "/api/v1/platform/ai/generate/import",
            params={"path": "/api/v1/tax/calculate", "method": "POST"},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["imported_count"] > 0

        cases = ai_platform.get(
            "/api/v1/platform/cases", params={"enabled_only": False}
        ).json()
        assert cases["total"] == body["imported_count"]
        assert all(not item["enabled"] for item in cases["items"])

    def test_imported_cases_carry_rationale_as_description(
        self, ai_platform: TestClient
    ) -> None:
        ai_platform.post(
            "/api/v1/platform/ai/generate/import",
            params={"path": "/healthz", "method": "GET"},
        )

        cases = ai_platform.get(
            "/api/v1/platform/cases", params={"enabled_only": False}
        ).json()

        assert all(item["description"] for item in cases["items"])

    def test_import_accepts_review_flag(self, ai_platform: TestClient) -> None:
        """``include_review_required`` 参数被接受且不改变高置信候选的导入行为。"""
        default_run = ai_platform.post(
            "/api/v1/platform/ai/generate/import",
            params={"path": "/healthz", "method": "GET"},
        ).json()
        explicit_run = ai_platform.post(
            "/api/v1/platform/ai/generate/import",
            params={"path": "/healthz", "method": "GET", "include_review_required": "true"},
        ).json()

        # 规则模式的候选置信度都很高，两种模式导入数应一致；
        # 该用例守住的是参数被接受、不报错这一契约
        assert default_run["imported_count"] == explicit_run["imported_count"]

    def test_imported_cases_can_be_enabled_and_run(self, ai_platform: TestClient) -> None:
        """导入 → 人工启用 → 执行通过的完整闭环。"""
        imported = ai_platform.post(
            "/api/v1/platform/ai/generate/import",
            params={"path": "/healthz", "method": "GET"},
        ).json()

        cases = ai_platform.get(
            "/api/v1/platform/cases", params={"enabled_only": False}
        ).json()
        for item in cases["items"]:
            editable = {
                key: value
                for key, value in item.items()
                if key not in {"id", "created_at", "updated_at"}
            }
            editable["enabled"] = True
            patched = ai_platform.patch(f"/api/v1/platform/cases/{item['id']}", json=editable)
            assert patched.status_code == 200, patched.text

        run = ai_platform.post("/api/v1/platform/runs", json={"wait": True}).json()

        assert run["total"] == imported["imported_count"]
        assert run["status"] == "passed"

    def test_import_unknown_path_returns_404(self, ai_platform: TestClient) -> None:
        response = ai_platform.post(
            "/api/v1/platform/ai/generate/import",
            params={"path": "/api/v1/no/such/api", "method": "GET"},
        )

        assert response.status_code == 404


class TestDiagnoseEndpoint:
    """``GET /ai/diagnose/{run_id}``。"""

    def test_diagnoses_environment_failure(self, ai_platform: TestClient) -> None:
        # 先造一条用例：目标不可达时它将逐条 ERROR，归因应落环境问题
        created = ai_platform.post(
            "/api/v1/platform/cases",
            json={
                "name": "目标探活",
                "method": "GET",
                "path": "/healthz",
                "expected_status": 200,
            },
        )
        assert created.status_code == 201, created.text

        run = ai_platform.post(
            "/api/v1/platform/runs", json={"wait": True, "base_url": "http://127.0.0.1:1"}
        ).json()

        response = ai_platform.get(f"/api/v1/platform/ai/diagnose/{run['id']}")

        assert response.status_code == 200
        body = response.json()
        assert body["run_id"] == run["id"]
        assert body["total_failures"] > 0
        assert body["by_category"], "失败必须被归类，不能全部落在 unknown"

        for diagnosis in body["diagnoses"]:
            assert diagnosis["evidence"], "每条归因都必须附证据"
            assert diagnosis["suggested_action"]

    def test_all_pass_run_has_empty_diagnosis(self, ai_platform: TestClient) -> None:
        run_id = ai_platform.post("/api/v1/platform/runs", json={"wait": True}).json()["id"]

        body = ai_platform.get(f"/api/v1/platform/ai/diagnose/{run_id}").json()

        assert body["total_failures"] == 0
        assert body["diagnoses"] == []
        assert body["by_category"] == {}

    def test_unknown_run_returns_404(self, ai_platform: TestClient) -> None:
        assert ai_platform.get("/api/v1/platform/ai/diagnose/999999").status_code == 404


class TestAiEndpointsInOpenapi:
    """AI 路由必须出现在平台文档里。"""

    def test_declares_ai_paths(self, ai_platform: TestClient) -> None:
        document = ai_platform.get("/openapi.json").json()

        expected = {
            "/api/v1/platform/ai/generate",
            "/api/v1/platform/ai/generate/import",
            "/api/v1/platform/ai/diagnose/{run_id}",
            "/api/v1/platform/ai/status",
        }
        assert expected <= set(document["paths"])


class TestValidateDocumentUrl:
    """防 SSRF 校验函数单测。"""

    def test_valid_http_in_test_env(self, app_settings: Settings) -> None:
        url = "http://127.0.0.1:8000/openapi.json"
        assert validate_document_url(url, settings=app_settings) == url

    def test_disallowed_scheme(self, app_settings: Settings) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_document_url("ftp://example.com/openapi.json", settings=app_settings)
        assert exc_info.value.status_code == 400
        assert "仅允许 http/https" in exc_info.value.detail

    def test_missing_hostname(self, app_settings: Settings) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_document_url("http:///openapi.json", settings=app_settings)
        assert exc_info.value.status_code == 400
        assert "缺少主机名" in exc_info.value.detail

    def test_forbidden_metadata_hosts(self, app_settings: Settings) -> None:
        with pytest.raises(HTTPException) as exc_info:
            validate_document_url("http://169.254.169.254/latest/meta-data", settings=app_settings)
        assert exc_info.value.status_code == 400
        assert "元数据服务" in exc_info.value.detail

    def test_prod_env_rejects_loopback(self, app_settings: Settings) -> None:
        prod_settings = app_settings.model_copy(update={"environment": "production"})
        with pytest.raises(HTTPException) as exc_info:
            validate_document_url("http://127.0.0.1:8000/openapi.json", settings=prod_settings)
        assert exc_info.value.status_code == 400
        assert "内网地址" in exc_info.value.detail

