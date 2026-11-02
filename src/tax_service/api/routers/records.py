"""申报记录路由。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from tax_service.api import responses
from tax_service.api.deps import CurrentUserDep, DeclarationRepoDep
from tax_service.api.schemas import RecordOut, RecordPage

router = APIRouter(prefix="/api/v1/tax/records", tags=["申报记录"])

LimitQuery = Annotated[int, Query(ge=1, le=100, description="每页条数")]
OffsetQuery = Annotated[int, Query(ge=0, description="偏移量")]


@router.get(
    "",
    response_model=RecordPage,
    summary="分页查询当前用户的申报记录",
    responses=responses.merge(responses.UNAUTHORIZED, responses.VALIDATION_FAILED),
)
def list_records(
    user: CurrentUserDep,
    declarations: DeclarationRepoDep,
    limit: LimitQuery = 20,
    offset: OffsetQuery = 0,
) -> RecordPage:
    """按倒序分页返回当前用户的申报记录。

    只返回当前用户的数据——越权访问他人记录返回 404 而不是 403，
    避免通过状态码差异推断记录是否存在。
    """
    total = declarations.count_for_user(user.id)
    records = declarations.list_for_user(user.id, limit=limit, offset=offset)
    return RecordPage(
        total=total,
        limit=limit,
        offset=offset,
        items=[RecordOut.model_validate(record) for record in records],
    )


@router.get(
    "/{record_id}",
    response_model=RecordOut,
    summary="查询单条申报记录",
    responses=responses.merge(responses.UNAUTHORIZED, responses.NOT_FOUND),
)
def get_record(record_id: int, user: CurrentUserDep, declarations: DeclarationRepoDep) -> RecordOut:
    """按 ID 查询记录，仅限本人。"""
    record = declarations.get_for_user(user.id, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    return RecordOut.model_validate(record)
