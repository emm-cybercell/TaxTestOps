"""FastAPI 应用工厂。

为什么用工厂而不是模块级 ``app``
--------------------------------
测试需要在同一进程内构造**多套互不干扰**的应用实例（不同数据库、不同缓存、
不同计算器实现），并覆盖依赖。工厂函数让这些变化都成为构造参数，
而不必依赖全局可变状态。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from tax_service import __version__
from tax_service.api.routers import auth, records, tax
from tax_service.api.schemas import HealthResponse
from tax_service.config import Settings, get_settings
from tax_service.core.calculator import build_calculator
from tax_service.db.base import create_engine_from_url, create_session_factory, init_schema
from tax_service.external.policy_client import PolicyServiceClient
from tax_service.infra.cache import build_cache
from tax_service.infra.ratelimit import RateLimiter
from tax_service.services.tax_service import (
    DeclarationRepository,
    TaxApplicationService,
    UserRepository,
)

logger = logging.getLogger(__name__)

RATE_LIMIT_EXEMPT_PATHS = frozenset({"/healthz", "/openapi.json", "/docs", "/redoc"})


def create_app(settings: Settings | None = None) -> FastAPI:
    """构造应用实例。"""
    settings = settings or get_settings()

    engine = create_engine_from_url(settings.database_url, echo=settings.db_echo)
    session_factory = create_session_factory(engine)

    cache = build_cache(settings.cache_backend, redis_url=settings.redis_url)
    policy_client = PolicyServiceClient(
        settings.policy_service_url,
        timeout_seconds=settings.policy_service_timeout_seconds,
        cache=cache,
        cache_ttl_seconds=settings.policy_cache_ttl_seconds,
    )

    calculator = build_calculator(settings.calculator_impl)
    tax_service = TaxApplicationService(
        calculator,
        calculator_impl=settings.calculator_impl,
        policy_client=policy_client,
    )
    declaration_repository = DeclarationRepository(session_factory)
    user_repository = UserRepository(session_factory)
    rate_limiter = RateLimiter(cache, settings.rate_limit_per_minute)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        init_schema(engine)
        logger.info(
            "TaxTestOps 启动：env=%s impl=%s db=%s cache=%s",
            settings.environment,
            settings.calculator_impl,
            settings.database_url,
            settings.cache_backend,
        )
        yield
        engine.dispose()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "个人所得税计算服务。\n\n"
            "本项目是一个**测试开发工程**：服务本身是被测对象，"
            "核心价值在于围绕它构建的分层自动化测试、质量门禁与缺陷分析。"
        ),
        lifespan=lifespan,
    )

    # 供依赖注入与测试读取
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.cache = cache
    app.state.policy_client = policy_client
    app.state.tax_service = tax_service
    app.state.declaration_repository = declaration_repository
    app.state.user_repository = user_repository
    app.state.rate_limiter = rate_limiter

    _install_rate_limit_middleware(app, rate_limiter)

    app.include_router(auth.router)
    app.include_router(tax.router)
    app.include_router(records.router)

    @app.get("/healthz", response_model=HealthResponse, tags=["运维"], summary="健康检查")
    def healthz() -> HealthResponse:
        """存活探针：返回关键依赖的当前配置。"""
        return HealthResponse(
            status="ok",
            app_name=settings.app_name,
            environment=settings.environment,
            calculator_impl=settings.calculator_impl,
            database="sqlite" if settings.is_sqlite else "mysql",
            cache=settings.cache_backend,
            version=__version__,
        )

    _mount_frontend(app, settings.web_dir)

    return app


def _install_rate_limit_middleware(app: FastAPI, rate_limiter: RateLimiter) -> None:
    """安装限流中间件。

    以客户端 IP 为限流维度。真实生产环境应改为「用户 + 接口」维度，
    并放在网关层；此处放在应用内是为了让限流行为可被自动化测试直接断言。
    """

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if request.url.path in RATE_LIMIT_EXEMPT_PATHS or request.url.path.startswith("/docs"):
            return await call_next(request)

        client_host = request.client.host if request.client else "unknown"
        decision = rate_limiter.check(client_host)

        if not decision.allowed:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "请求过于频繁，请稍后重试"},
                headers={"Retry-After": str(decision.retry_after_seconds)},
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(rate_limiter.limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        return response


def _mount_frontend(app: FastAPI, web_dir: Path) -> None:
    """挂载前端静态资源。

    必须在 API 路由注册**之后**挂载：Starlette 按注册顺序匹配，
    若先挂载 ``/`` 会把 API 路由一并吞掉。
    """
    if not web_dir.is_dir():
        logger.warning("前端目录不存在，跳过静态资源挂载：%s", web_dir)
        return

    app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")


app = create_app
"""``uvicorn tax_service.api.app:app --factory`` 使用。"""
