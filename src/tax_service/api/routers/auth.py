"""认证路由。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from tax_service.api import responses
from tax_service.api.deps import CurrentUserDep, SettingsDep, UserRepoDep
from tax_service.api.schemas import LoginRequest, TokenResponse
from tax_service.api.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/api/v1/auth", tags=["认证"])


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="注册用户",
    responses=responses.merge(responses.CONFLICT, responses.VALIDATION_FAILED),
)
def register(payload: LoginRequest, users: UserRepoDep, settings: SettingsDep) -> dict[str, object]:
    """注册新用户。

    用户名冲突返回 409 而不是静默覆盖——与 SUT 中「非法值静默忽略」的缺陷 D5 形成对照。
    """
    if users.get_by_username(payload.username) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"用户名已存在：{payload.username}",
        )

    user = users.create(
        payload.username,
        hash_password(payload.password, rounds=settings.bcrypt_rounds),
    )
    return {"id": user.id, "username": user.username, "display_name": user.display_name}


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="登录并获取访问令牌",
    responses=responses.merge(
        responses.UNAUTHORIZED, responses.FORBIDDEN, responses.VALIDATION_FAILED
    ),
)
def login(payload: LoginRequest, users: UserRepoDep, settings: SettingsDep) -> TokenResponse:
    """校验凭据并签发 JWT。"""
    user = users.get_by_username(payload.username)
    # 无论用户是否存在都执行一次哈希校验，避免通过响应时间枚举用户名
    password_hash = (
        user.password_hash
        if user is not None
        else hash_password("timing-equalization-placeholder", rounds=settings.bcrypt_rounds)
    )

    if user is None or not verify_password(payload.password, password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="账号已停用")

    token, expires_in = create_access_token(
        user.username,
        secret=settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
        expires_minutes=settings.jwt_expire_minutes,
        issuer=settings.app_name,
    )
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get(
    "/me",
    summary="获取当前用户信息",
    responses=responses.UNAUTHORIZED,
)
def me(user: CurrentUserDep) -> dict[str, object]:
    """返回当前登录用户。"""
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "created_at": user.created_at.isoformat(),
    }
