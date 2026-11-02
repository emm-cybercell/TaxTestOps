"""测试数据工厂。

所有凭据与标识符**随机生成**，不写死在源码里：

- 避免安全扫描把测试凭据误判为泄露的密钥；
- 避免用例之间因用户名冲突而相互污染；
- 让「用户名唯一性」这类约束被真实地检验。
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from tax_service.core.policy import BASIC_DEDUCTION


def random_username(prefix: str = "u") -> str:
    """生成合法且唯一的用户名（长度满足接口的 3~64 约束）。"""
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def random_password(length: int = 16) -> str:
    """生成满足长度约束的随机密码。"""
    return secrets.token_urlsafe(length)


def person_payload(**overrides: Any) -> dict[str, Any]:
    """构造纳税人请求体，默认全部为「无专项附加扣除」。"""
    payload: dict[str, Any] = {
        "children_num": 0,
        "is_keep_stu": 0,
        "house_loans": False,
        "is_rent_house": 0,
        "supporting_elderly": False,
        "sibling_num": 0,
    }
    payload.update(overrides)
    return payload


def calculate_payload(
    salary: int = 20_000,
    *,
    person: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """构造单次测算请求体。"""
    payload: dict[str, Any] = {
        "salary": salary,
        "person": person if person is not None else person_payload(),
    }
    payload.update(overrides)
    return payload


def salary_for_taxable_income(taxable_income: int) -> int:
    """把目标应纳税所得额换算为工资（无专项附加扣除时）。"""
    return taxable_income + BASIC_DEDUCTION
