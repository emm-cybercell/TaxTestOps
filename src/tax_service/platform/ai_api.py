"""AI 增强接口。

三个端点，对应 AI 在测试流程里的三个落点：

- ``POST /ai/generate``：为某个接口生成候选用例（**只生成，不落库**）；
- ``POST /ai/generate/import``：生成并导入用例库（导入的用例默认**停用**，需人工启用）；
- ``GET /ai/diagnose/{run_id}``：对一次执行的失败做归因。

为什么「导入的用例默认停用」
----------------------------
AI 生成的用例即使通过了结构校验，也不代表断言语义正确。让它默认停用、由人过一眼再
启用，是把「模型提议」与「进入流水线」之间那道闸门保留下来。全自动导入看起来高效，
但会把噪声直接灌进回归集，最终让人不再相信红灯。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, status

from tax_service.ai.case_generator import TestCaseGenerator
from tax_service.ai.provider import build_provider
from tax_service.ai.schemas import CaseGenerationReport, DiagnosisReport
from tax_service.ai.triager import FailureTriager
from tax_service.config import Settings
from tax_service.platform import schemas
from tax_service.platform.service import PlatformService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/platform/ai", tags=["AI 增强"])

DOCUMENT_CACHE_TTL_SECONDS = 300.0
"""OpenAPI 文档缓存时长。

文档可能随被测服务发布而变化，因此带 TTL 的懒加载比启动时拉一次更可靠——
被测服务比平台晚启动时也能拿到文档。
"""

ALLOWED_DOCUMENT_SCHEMES = frozenset({"http", "https"})
"""拉取文档允许的协议。"""

FORBIDDEN_DOCUMENT_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal"})
"""明确禁止的元数据服务地址（云环境 SSRF 的经典目标）。"""


def validate_document_url(url: str, *, settings: Settings) -> str:
    """校验文档拉取地址，防 SSRF。

    平台的目标地址虽由配置决定，但 ``POST /runs`` 允许请求方传入 ``base_url``
    覆盖默认值——也就是说这个 URL 有**外部输入成分**，必须校验：

    - 仅允许 HTTP(S)；
    - 拒绝云元数据地址；
    - 非测试环境拒绝环回/私网地址（测试环境必须允许，否则本地的
      ``127.0.0.1`` 被测服务无法拉取文档）。
    """
    parsed = urlparse(url)

    if parsed.scheme not in ALLOWED_DOCUMENT_SCHEMES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"目标地址仅允许 http/https，收到：{parsed.scheme or '（空）'}",
        )
    if not parsed.hostname:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="目标地址缺少主机名",
        )
    if parsed.hostname.lower() in FORBIDDEN_DOCUMENT_HOSTS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="目标地址指向云元数据服务，已拒绝",
        )

    if settings.environment != "test":
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        try:
            resolved = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, port)}
        except socket.gaierror as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"目标主机无法解析：{parsed.hostname}",
            ) from exc

        for ip_text in resolved:
            ip = ipaddress.ip_address(ip_text)
            if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"非测试环境禁止访问内网地址（解析到 {ip}）",
                )

    return url


def _fetch_openapi_document(service: PlatformService, *, settings: Settings) -> dict[str, Any]:
    """从被测服务拉取 OpenAPI 文档。

    AI 生成用例的依据必须是**被测服务实际暴露的契约**，而不是平台自己的文档——
    平台与被测服务是两个应用，接口集完全不同。

    刻意关闭重定向跟随：否则「校验的是 A、实际请求的是重定向后的内网 B」
    会成为 SSRF 绕过路径。
    """
    url = validate_document_url(
        f"{service.target_base_url.rstrip('/')}/openapi.json", settings=settings
    )
    try:
        response = httpx.get(url, timeout=10.0, follow_redirects=False)
        response.raise_for_status()
        document = response.json()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"无法获取被测服务的 OpenAPI 文档（{url}）：{exc!r}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"被测服务返回的文档不是合法 JSON：{exc!r}",
        ) from exc

    if not isinstance(document, dict) or not document.get("paths"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="被测服务的 OpenAPI 文档不含任何路径，无法生成用例",
        )

    logger.debug("已加载被测服务 OpenAPI 文档：%d 个路径", len(document["paths"]))
    return document


def _build_generator(request: Request) -> TestCaseGenerator:
    """按配置构造生成器，带 TTL 缓存的文档懒加载。

    文档缓存挂在 ``app.state`` 上：被测服务比平台晚启动时，第一次请求会拉取并缓存；
    此后 5 分钟内的生成请求不再打被测服务。
    """
    app = request.app
    service: PlatformService = app.state.platform_service
    settings: Settings = app.state.settings

    cache: dict[str, Any] = getattr(app.state, "openapi_cache", {})
    fetched_at: float = cache.get("fetched_at", 0.0)
    document: dict[str, Any] | None = cache.get("document")

    if document is None or (time.monotonic() - fetched_at) > DOCUMENT_CACHE_TTL_SECONDS:
        document = _fetch_openapi_document(service, settings=settings)
        app.state.openapi_cache = {"document": document, "fetched_at": time.monotonic()}
        app.state.openapi_document = document

    provider = build_provider(app.state.settings, document)
    return TestCaseGenerator(provider, document)


def _build_triager(request: Request) -> FailureTriager:
    app = request.app
    document: dict[str, Any] = getattr(app.state, "openapi_document", {})
    provider = build_provider(app.state.settings, document)
    return FailureTriager(provider)


@router.post(
    "/generate",
    response_model=CaseGenerationReport,
    summary="按接口生成候选用例",
    responses={404: {"description": "接口未在 OpenAPI 文档中声明"}},
)
def generate_cases(
    request: Request,
    path: Annotated[str, Query(description="OpenAPI 路径模板")],
    method: Annotated[str, Query(description="HTTP 方法")] = "POST",
    requirement: Annotated[
        str | None, Query(description="可选的需求描述，接入模型时会作为生成依据")
    ] = None,
) -> CaseGenerationReport:
    """为指定接口生成候选用例，返回报告（含被拒绝的候选与原因）。"""
    generator = _build_generator(request)

    try:
        if requirement:
            return generator.from_requirement(requirement, path=path, method=method)
        return generator.from_openapi(path, method)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post(
    "/generate/import",
    response_model=dict,
    summary="生成并导入用例库",
)
def generate_and_import(
    request: Request,
    path: Annotated[str, Query(description="OpenAPI 路径模板")],
    method: Annotated[str, Query(description="HTTP 方法")] = "POST",
    requirement: Annotated[str | None, Query()] = None,
    include_review_required: Annotated[
        bool, Query(description="是否连同「待评审」的候选一起导入")
    ] = False,
) -> dict:
    """生成用例并写入用例库。

    导入的用例一律 ``enabled=False``——必须由人确认后再启用。
    """
    generator = _build_generator(request)
    service = request.app.state.platform_service

    try:
        report = (
            generator.from_requirement(requirement, path=path, method=method)
            if requirement
            else generator.from_openapi(path, method)
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    imported: list[dict] = []
    skipped: list[str] = []
    for case in report.generated:
        if case.needs_review and not include_review_required:
            skipped.append(case.name)
            continue

        payload = TestCaseGenerator.to_platform_payload(case)
        # 强制停用：AI 生成的内容必须经人确认才进流水线
        payload["enabled"] = False
        created = service.create_case(payload)
        imported.append({"id": created.id, "name": created.name, "needs_review": case.needs_review})

    return {
        "source": report.source,
        "target": report.target,
        "imported": imported,
        "imported_count": len(imported),
        "skipped_for_review": skipped,
        "rejected": report.rejected,
        "acceptance_rate": report.acceptance_rate,
    }


@router.get(
    "/diagnose/{run_id}",
    response_model=DiagnosisReport,
    summary="对执行的失败做归因",
    responses={404: {"description": "执行记录不存在"}},
)
def diagnose_run(request: Request, run_id: int) -> DiagnosisReport:
    """按类别对失败归因，供告警路由使用。

    ``by_category`` 把失败数拆到「该找谁」的维度上：``product_defect`` 找业务方，
    ``environment_issue`` 找运维，``test_issue`` 找测试自己。
    """
    service = request.app.state.platform_service
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="执行记录不存在")

    items = service.list_run_items(run_id)
    return _build_triager(request).diagnose_run(run, items)


@router.get("/status", response_model=dict, summary="AI 能力状态")
def ai_status(request: Request) -> dict:
    """如实报告当前 AI 能力是「接了模型」还是「离线规则」。

    不把规则模式伪装成模型模式——否则使用者会误以为得到的是模型判断。
    """
    settings = request.app.state.settings
    generator = _build_generator(request)
    return {
        "enabled": bool(settings.ai_enabled),
        "source": generator.source,
        "model": settings.ai_model if generator.source == "llm" else None,
        "has_api_key": bool(settings.ai_api_key),
        "openapi_paths": len(request.app.state.openapi_document.get("paths", {})),
    }


__all__ = ["router", "schemas"]
