"""测试平台应用。

与主服务的分工
--------------
本应用是**独立服务**，复用同一份配置与数据库，但职责完全不同：

- 主服务（``tax_service.api``）是**被测对象**；
- 平台（``tax_service.platform``）是**测试执行与观测工具**。

把两者放在同一个仓库、共用同一套数据库，是因为平台需要读取被测服务的状态做断言；
但它们的部署、发布节奏、可用性要求都不同，因此进程独立。

启动::

    uvicorn tax_service.platform.app:create_platform_app --factory --port 8100
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.staticfiles import StaticFiles

from tax_service import __version__
from tax_service.config import Settings, get_settings
from tax_service.db.base import create_engine_from_url, create_session_factory, init_schema
from tax_service.platform import schemas
from tax_service.platform.ai_api import router as ai_router
from tax_service.platform.scheduler import RunScheduler
from tax_service.platform.service import PlatformService
from tax_service.platform.streaming import RunEventBroker

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/platform", tags=["测试平台"])

HEARTBEAT_SECONDS = 15.0
"""WebSocket 心跳间隔：连接空闲时定期发心跳，让客户端能识别断线。"""


def get_platform_service(request: Request) -> PlatformService:
    """从应用状态取平台服务（依赖注入）。"""
    return request.app.state.platform_service


ServiceDep = Annotated[PlatformService, Depends(get_platform_service)]


# --------------------------------------------------------------------------- #
# 用例管理
# --------------------------------------------------------------------------- #


@router.post(
    "/cases",
    response_model=schemas.TestCaseOut,
    status_code=status.HTTP_201_CREATED,
    summary="新建用例",
)
def create_case(payload: schemas.TestCaseIn, service: ServiceDep) -> schemas.TestCaseOut:
    """用例以数据形式存储，可在界面上维护，无需改代码、提 PR。"""
    return schemas.TestCaseOut.model_validate(service.create_case(payload.model_dump()))


@router.get("/cases", response_model=schemas.TestCasePage, summary="查询用例")
def list_cases(
    service: ServiceDep,
    tag: Annotated[str | None, Query(description="只返回带该标签的用例")] = None,
    enabled_only: Annotated[bool, Query(description="是否只返回启用的用例")] = True,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> schemas.TestCasePage:
    cases = service.list_cases(tag=tag, enabled_only=enabled_only, limit=limit)
    return schemas.TestCasePage(
        total=len(cases),
        items=[schemas.TestCaseOut.model_validate(case) for case in cases],
    )


@router.get("/cases/{case_id}", response_model=schemas.TestCaseOut, summary="查询单条用例")
def get_case(case_id: int, service: ServiceDep) -> schemas.TestCaseOut:
    case = service.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用例不存在")
    return schemas.TestCaseOut.model_validate(case)


@router.patch("/cases/{case_id}", response_model=schemas.TestCaseOut, summary="更新用例")
def update_case(
    case_id: int, payload: schemas.TestCaseIn, service: ServiceDep
) -> schemas.TestCaseOut:
    case = service.update_case(case_id, payload.model_dump())
    if case is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用例不存在")
    return schemas.TestCaseOut.model_validate(case)


@router.delete("/cases/{case_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除用例")
def delete_case(case_id: int, service: ServiceDep) -> None:
    if not service.delete_case(case_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="用例不存在")


# --------------------------------------------------------------------------- #
# 执行
# --------------------------------------------------------------------------- #


def _spawn_execution(app: FastAPI, run_id: int) -> None:
    """在后台线程执行一次批次。

    刻意不用 FastAPI 的 ``BackgroundTasks``：后者跑在事件循环的线程池里，
    长时间阻塞会挤占请求处理能力。用独立线程把「执行」与「服务响应」彻底解耦。
    """
    service: PlatformService = app.state.platform_service

    def worker() -> None:
        try:
            service.execute_run(run_id)
        except Exception:  # noqa: BLE001 - 后台线程异常必须记录，否则静默消失
            logger.exception("后台执行失败：run_id=%s", run_id)

    threading.Thread(target=worker, name=f"run-{run_id}", daemon=True).start()


@router.post(
    "/runs",
    response_model=schemas.TestRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    summary="触发执行",
)
def create_run(
    payload: schemas.RunCreateIn, request: Request, service: ServiceDep
) -> schemas.TestRunOut:
    """创建并触发一次执行。

    默认**异步**返回 202 + run_id，客户端通过 WebSocket 或轮询查询进度；
    ``wait=true`` 时同步执行（仅建议在用例很少时使用）。
    """
    run = service.create_run(trigger="manual", tag=payload.tag, base_url=payload.base_url)

    if payload.wait:
        finished = service.execute_run(run.id)
        if finished is None:  # pragma: no cover - 刚创建不可能查不到
            raise HTTPException(status_code=500, detail="执行失败")
        return schemas.TestRunOut.model_validate(finished)

    _spawn_execution(request.app, run.id)
    return schemas.TestRunOut.model_validate(run)


@router.get("/runs", response_model=schemas.RunPage, summary="执行历史")
def list_runs(
    service: ServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> schemas.RunPage:
    runs = service.list_runs(limit=limit, offset=offset)
    return schemas.RunPage(
        total=service.count_runs(),
        items=[schemas.TestRunOut.model_validate(run) for run in runs],
    )


@router.get("/runs/{run_id}", response_model=schemas.TestRunDetail, summary="执行详情")
def get_run(run_id: int, service: ServiceDep) -> schemas.TestRunDetail:
    run = service.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="执行记录不存在")

    # 先转成不含 items 的模型，再单独装配逐条结果。
    # 直接 model_validate(run) 会让 pydantic 去读 run.items —— 此时会话已关闭，
    # 触发 DetachedInstanceError（ORM 惰性加载 + 已关闭会话的经典组合）。
    base = schemas.TestRunOut.model_validate(run)
    items = [
        schemas.RunItemOut.model_validate(item) for item in service.list_run_items(run_id)
    ]
    return schemas.TestRunDetail(**base.model_dump(), items=items)


# --------------------------------------------------------------------------- #
# 度量
# --------------------------------------------------------------------------- #


@router.get("/trends", response_model=list[schemas.TrendPoint], summary="通过率与耗时趋势")
def trends(
    service: ServiceDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
) -> list[schemas.TrendPoint]:
    return [schemas.TrendPoint(**point) for point in service.trends(limit=limit)]


@router.get("/summary", response_model=schemas.PlatformSummary, summary="平台概览")
def summary(service: ServiceDep) -> schemas.PlatformSummary:
    return schemas.PlatformSummary(**service.summary())


@router.get("/flaky", response_model=list[schemas.FlakyCandidate], summary="疑似 flaky 用例")
def flaky_candidates(service: ServiceDep) -> list[schemas.FlakyCandidate]:
    """列出「被重试后通过」的用例。

    flaky 用例是自动化测试最大的隐性成本：它让人开始怀疑所有失败，最终导致
    「红灯也照常合并」。把它们显式暴露出来，是治理的第一步。
    """
    return [schemas.FlakyCandidate(**item) for item in service.flaky_candidates()]


@router.get("/scheduler", response_model=schemas.SchedulerStatus, summary="定时调度状态")
def scheduler_status(request: Request) -> schemas.SchedulerStatus:
    return schemas.SchedulerStatus(**request.app.state.scheduler.status())


# --------------------------------------------------------------------------- #
# 实时事件
# --------------------------------------------------------------------------- #


@router.websocket("/runs/{run_id}/stream")
async def stream_run_events(websocket: WebSocket, run_id: int) -> None:
    """推送某个执行的实时进度。

    连接建立后立即回一条 ``connected`` 事件，让客户端确认订阅成功——否则在长任务
    开始前，客户端无法区分「连接成功但暂无事件」与「连接失败」。
    """
    await websocket.accept()
    broker: RunEventBroker = websocket.app.state.run_event_broker
    queue = broker.subscribe(str(run_id))

    try:
        await websocket.send_json({"type": "connected", "run_id": run_id})
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
            except TimeoutError:
                await websocket.send_json({"type": "heartbeat", "run_id": run_id})
                continue
            await websocket.send_json(event)
    except WebSocketDisconnect:
        logger.debug("客户端断开：run_id=%s", run_id)
    finally:
        broker.unsubscribe(str(run_id), queue)


# --------------------------------------------------------------------------- #
# 应用工厂
# --------------------------------------------------------------------------- #


def create_platform_app(settings: Settings | None = None) -> FastAPI:
    """构造测试平台应用。"""
    settings = settings or get_settings()

    engine = create_engine_from_url(settings.database_url, echo=settings.db_echo)
    session_factory = create_session_factory(engine)

    broker = RunEventBroker()
    service = PlatformService(
        session_factory,
        target_base_url=settings.platform_target_base_url or settings.platform_self_base_url,
        target_token=settings.platform_target_token,
        max_workers=settings.platform_max_workers,
        on_run_event=broker.publish,
    )

    scheduler = RunScheduler(
        run_once=lambda tag: _scheduled_run(service, tag),
        interval_seconds=settings.platform_schedule_interval_seconds,
        tag=settings.platform_schedule_tag,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        init_schema(engine)
        broker.bind_loop(asyncio.get_running_loop())
        if settings.platform_schedule_enabled:
            scheduler.start()
        logger.info(
            "测试平台启动：target=%s，定时调度=%s",
            service.target_base_url,
            "开启" if settings.platform_schedule_enabled else "关闭",
        )
        yield
        scheduler.stop()
        engine.dispose()

    app = FastAPI(
        title=f"{settings.app_name} 测试平台",
        version=__version__,
        description=(
            "轻量测试平台：用例以数据形式管理、可手动或定时触发执行、"
            "结果结构化落库并支持趋势分析与实时进度推送。"
        ),
        lifespan=lifespan,
    )

    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.platform_service = service
    app.state.run_event_broker = broker
    app.state.scheduler = scheduler
    # OpenAPI 文档由 AI 路由按需懒加载并缓存（见 platform/ai_api.py），
    # 这里只初始化为空：被测服务可能比平台更晚启动。
    app.state.openapi_document = {}

    app.include_router(router)
    app.include_router(ai_router)

    @app.get("/healthz", tags=["运维"], summary="健康检查")
    def healthz() -> dict[str, object]:
        return {
            "status": "ok",
            "app_name": settings.app_name,
            "version": __version__,
            "target_base_url": service.target_base_url,
            "scheduler": scheduler.status(),
        }

    platform_web = settings.web_dir / "platform"
    if platform_web.is_dir():
        app.mount("/", StaticFiles(directory=str(platform_web), html=True), name="platform-web")
    else:  # pragma: no cover - 部署时目录必然存在
        logger.warning("平台前端目录不存在，跳过挂载：%s", platform_web)

    return app


def _scheduled_run(service: PlatformService, tag: str | None) -> int | None:
    """定时触发一次执行并同步等待完成（调度线程内执行）。"""
    run = service.create_run(trigger="schedule", tag=tag)
    finished = service.execute_run(run.id)
    return finished.id if finished is not None else run.id


__all__ = ["create_platform_app", "router"]
