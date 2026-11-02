"""FastAPI 依赖注入。"""

from __future__ import annotations

from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from tax_service.api.security import decode_access_token
from tax_service.config import Settings
from tax_service.db.models import User
from tax_service.services.tax_service import (
    DeclarationRepository,
    TaxApplicationService,
    UserRepository,
)

bearer_scheme = HTTPBearer(
    auto_error=False,
    description="在请求头中携带 `Authorization: Bearer <access_token>`",
)
"""HTTP Bearer 安全方案。

使用 :class:`~fastapi.security.HTTPBearer` 而非裸 ``Header()`` 参数，是因为只有前者
会把安全方案写进 OpenAPI 文档的 ``components.securitySchemes`` 与各操作的 ``security``
字段——调用方据此才能自动生成正确的请求。该约束由
``tests/api/test_health.py::test_protected_paths_declare_bearer_security`` 守住。
"""


def get_app_settings(request: Request) -> Settings:
    """当前应用的配置。"""
    return request.app.state.settings


def get_tax_service(request: Request) -> TaxApplicationService:
    """个税应用服务。"""
    return request.app.state.tax_service


def get_declaration_repository(request: Request) -> DeclarationRepository:
    """申报记录仓储。"""
    return request.app.state.declaration_repository


def get_user_repository(request: Request) -> UserRepository:
    """用户仓储。"""
    return request.app.state.user_repository


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
TaxServiceDep = Annotated[TaxApplicationService, Depends(get_tax_service)]
DeclarationRepoDep = Annotated[DeclarationRepository, Depends(get_declaration_repository)]
UserRepoDep = Annotated[UserRepository, Depends(get_user_repository)]


def get_current_user(
    user_repository: UserRepoDep,
    settings: SettingsDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    """从 ``Authorization: Bearer <token>`` 解析当前用户。

    :raises HTTPException: 401 —— 缺少令牌、令牌非法/过期、用户不存在或已停用。
    """
    credentials_error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无效或缺失的访问令牌",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None or not credentials.credentials:
        raise credentials_error

    try:
        payload = decode_access_token(
            credentials.credentials,
            secret=settings.jwt_secret,
            algorithm=settings.jwt_algorithm,
            issuer=settings.app_name,
        )
    except jwt.InvalidTokenError as exc:
        raise credentials_error from exc

    username = payload.get("sub")
    if not isinstance(username, str) or not username:
        raise credentials_error

    user = user_repository.get_by_username(username)
    if user is None or not user.is_active:
        raise credentials_error

    return user


CurrentUserDep = Annotated[User, Depends(get_current_user)]
