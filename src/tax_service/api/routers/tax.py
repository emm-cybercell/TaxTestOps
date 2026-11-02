"""个税测算路由。"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status

from tax_service.api import responses
from tax_service.api.deps import CurrentUserDep, DeclarationRepoDep, SettingsDep, TaxServiceDep
from tax_service.api.schemas import (
    BatchItemResult,
    BatchRequest,
    BatchResponse,
    CalculateRequest,
    CalculateResponse,
    DeductionItemOut,
    PolicyParametersOut,
)
from tax_service.core.deductions import build_deduction_breakdown
from tax_service.core.person import Person
from tax_service.services.tax_service import TaxApplicationService, TaxComputation

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tax", tags=["个税测算"])


def _to_person(payload) -> Person:
    """把接口模型转换为领域模型。"""
    return Person(
        children_num=payload.children_num,
        is_keep_stu=payload.is_keep_stu,
        house_loans=payload.house_loans,
        is_rent_house=payload.is_rent_house,
        supporting_elderly=payload.supporting_elderly,
        sibling_num=payload.sibling_num,
    )


def _compute_or_422(service: TaxApplicationService, person: Person, salary: int) -> TaxComputation:
    """执行计算，把领域层的业务校验错误映射为 422。

    **为什么需要这一层**：Pydantic 只校验单个字段的格式（枚举、范围），
    而**跨字段业务约束**（如住房贷款利息与住房租金互斥）在领域层校验——
    standard 实现会抛 ``ValueError``。不在这里接住的话，异常会穿透成 500，
    让调用方误以为是服务端故障，而实际上只是请求违反了业务规则。
    该缺陷由 schemathesis 模糊测试在 standard 模式下发现（legacy 不校验互斥，
    因此只在双模式运行时暴露）。
    """
    try:
        return service.compute(person, salary)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


def _response_from_record(record) -> CalculateResponse:
    """用**已落库记录**重建响应。

    幂等重放必须返回首次落库的结果，而不是本次重新计算的结果——否则调用方拿到
    「首次的 record_id + 本次的金额」，同一幂等键在不同时刻会看到不同数据，
    幂等语义形同虚设。该缺陷由 ``tests/api/test_idempotency.py`` 守住。
    """
    person = Person(
        children_num=record.children_num,
        is_keep_stu=record.is_keep_stu,
        house_loans=record.house_loans,
        is_rent_house=record.is_rent_house,
        supporting_elderly=record.supporting_elderly,
        sibling_num=record.sibling_num,
    )
    return CalculateResponse(
        record_id=record.id,
        taxable_income=record.taxable_income,
        tax_amount=record.tax_amount,
        calculator_impl=record.calculator_impl,
        deductions=[
            DeductionItemOut(**item.to_dict()) for item in build_deduction_breakdown(person)
        ],
        created_at=record.created_at,
    )


@router.post(
    "/calculate",
    response_model=CalculateResponse,
    summary="单次个税测算",
    responses=responses.merge(responses.UNAUTHORIZED, responses.VALIDATION_FAILED),
)
def calculate(
    payload: CalculateRequest,
    user: CurrentUserDep,
    service: TaxServiceDep,
    declarations: DeclarationRepoDep,
) -> CalculateResponse:
    """计算月度个税，可选落库。

    同一用户携带相同 ``idempotency_key`` 重复提交时，返回**首次**落库的记录内容，
    不会产生第二条数据，也不会返回本次重新计算的结果。
    """
    person = _to_person(payload.person)
    computation = _compute_or_422(service, person, payload.salary)

    if not payload.persist:
        return CalculateResponse(
            record_id=None,
            taxable_income=computation.taxable_income,
            tax_amount=computation.tax_amount,
            calculator_impl=computation.calculator_impl,
            deductions=[DeductionItemOut(**item.to_dict()) for item in computation.deductions],
            created_at=None,
        )

    record, created = declarations.create(
        user_id=user.id,
        salary=payload.salary,
        person=person,
        computation=computation,
        idempotency_key=payload.idempotency_key,
    )

    if not created:
        return _response_from_record(record)

    return CalculateResponse(
        record_id=record.id,
        taxable_income=computation.taxable_income,
        tax_amount=computation.tax_amount,
        calculator_impl=computation.calculator_impl,
        deductions=[DeductionItemOut(**item.to_dict()) for item in computation.deductions],
        created_at=record.created_at,
    )


@router.post(
    "/batch",
    response_model=BatchResponse,
    summary="批量申报",
    responses=responses.merge(responses.UNAUTHORIZED, responses.VALIDATION_FAILED),
)
def batch_calculate(
    payload: BatchRequest,
    user: CurrentUserDep,
    service: TaxServiceDep,
    declarations: DeclarationRepoDep,
    settings: SettingsDep,
) -> BatchResponse:
    """批量测算并逐项落库。

    设计取舍：**逐项独立事务，允许部分成功**。批量申报场景下，一条脏数据不应让
    整批回滚——响应逐项返回成功/失败，由调用方决定补偿策略。
    """
    if len(payload.items) > settings.batch_max_size:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"批量条数 {len(payload.items)} 超过上限 {settings.batch_max_size}",
        )

    seen_refs: set[str] = set()
    results: list[BatchItemResult] = []
    succeeded = 0

    for index, item in enumerate(payload.items):
        if item.external_ref is not None:
            if item.external_ref in seen_refs:
                results.append(
                    BatchItemResult(
                        index=index,
                        external_ref=item.external_ref,
                        ok=False,
                        error=f"批内 external_ref 重复：{item.external_ref}",
                    )
                )
                continue
            seen_refs.add(item.external_ref)

        person = _to_person(item.person)
        try:
            computation = service.compute(person, item.salary)
            record, _created = declarations.create(
                user_id=user.id,
                salary=item.salary,
                person=person,
                computation=computation,
                idempotency_key=(
                    f"{payload.idempotency_key}:{index}" if payload.idempotency_key else None
                ),
                source="batch",
            )
        except Exception as exc:  # noqa: BLE001 - 逐项兜底，保证整批不中断
            logger.exception("批量申报第 %s 项失败", index)
            results.append(
                BatchItemResult(
                    index=index, external_ref=item.external_ref, ok=False, error=str(exc)
                )
            )
            continue

        succeeded += 1
        results.append(
            BatchItemResult(
                index=index,
                external_ref=item.external_ref,
                ok=True,
                record_id=record.id,
                taxable_income=computation.taxable_income,
                tax_amount=computation.tax_amount,
            )
        )

    return BatchResponse(
        total=len(payload.items),
        succeeded=succeeded,
        failed=len(payload.items) - succeeded,
        items=results,
    )


@router.get(
    "/policy",
    response_model=PolicyParametersOut,
    summary="获取政策参数",
    responses=responses.merge(responses.UNAUTHORIZED, responses.SERVICE_UNAVAILABLE),
)
def policy_parameters(service: TaxServiceDep) -> PolicyParametersOut:
    """返回政策参数快照。

    响应中的 ``source`` 字段如实标注参数来源（``remote`` / ``cache`` / ``fallback``），
    使降级行为可被监控发现，而不是被伪装成正常结果。
    """
    parameters = service.policy_parameters()
    if parameters is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="政策服务未配置",
        )
    return PolicyParametersOut(**parameters.to_dict())
