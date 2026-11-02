"""接口层 · 认证。"""

from __future__ import annotations

import secrets

import jwt
import pytest
from fastapi import FastAPI

from support.api_client import ApiClient, UserSession
from support.factories import random_password, random_username
from tax_service.api.security import create_access_token
from tax_service.config import Settings
from tax_service.db.models import User

pytestmark = [pytest.mark.api, pytest.mark.regression]


def _deactivate(app: FastAPI, user_id: int) -> None:
    """把用户置为停用（走 ORM 对象模型，不手写 SQL）。"""
    with app.state.session_factory() as session:
        record = session.get(User, user_id)
        if record is not None:
            record.is_active = False
            session.commit()


class TestRegistration:
    """注册接口。"""

    def test_creates_user(self, api: ApiClient) -> None:
        username = random_username()

        body = api.expect(api.register(username, random_password()), 201)

        assert body["username"] == username
        assert body["id"] > 0

    def test_rejects_duplicate_username(self, api: ApiClient, user: UserSession) -> None:
        """重名必须显式冲突（409），而不是静默覆盖。

        与 SUT 中「非法枚举值被静默忽略」的缺陷 D5 形成对照：接口层对边界条件
        一律显式拒绝。
        """
        response = api.register(user.username, random_password())

        assert response.status_code == 409
        assert user.username in response.detail()

    @pytest.mark.parametrize(
        ("username", "password"),
        [
            pytest.param("ab", "valid-password", id="用户名过短"),
            pytest.param("a" * 65, "valid-password", id="用户名过长"),
            pytest.param("valid-username", "short", id="密码过短"),
        ],
    )
    def test_rejects_invalid_credentials_format(
        self, api: ApiClient, username: str, password: str
    ) -> None:
        assert api.register(username, password).status_code == 422

    def test_rejects_unknown_fields(self, api: ApiClient) -> None:
        """额外字段必须被拒绝，避免调用方以为参数生效了。"""
        response = api.post(
            "/api/v1/auth/register",
            json={
                "username": random_username(),
                "password": random_password(),
                "is_admin": True,
            },
            authenticated=False,
        )

        assert response.status_code == 422


class TestLogin:
    """登录接口。"""

    def test_returns_bearer_token(self, api: ApiClient, user: UserSession) -> None:
        body = api.expect(api.login(user.username, user.password), 200)

        assert body["token_type"] == "bearer"
        assert body["expires_in"] > 0
        assert body["access_token"]

    def test_rejects_wrong_password(self, api: ApiClient, user: UserSession) -> None:
        response = api.login(user.username, random_password())

        assert response.status_code == 401
        assert response.headers.get("WWW-Authenticate") == "Bearer"

    def test_rejects_unknown_user(self, api: ApiClient) -> None:
        assert api.login(random_username(), random_password()).status_code == 401

    def test_does_not_leak_which_field_is_wrong(self, api: ApiClient, user: UserSession) -> None:
        """用户名错误与密码错误的响应必须一致，否则可被用来枚举用户名。"""
        wrong_password = api.login(user.username, random_password())
        unknown_user = api.login(random_username(), random_password())

        assert wrong_password.status_code == unknown_user.status_code == 401
        assert wrong_password.detail() == unknown_user.detail()

    def test_token_payload_is_well_formed(self, api: ApiClient, user: UserSession) -> None:
        """令牌应携带标准声明，便于网关与审计系统消费。"""
        token = api.expect(api.login(user.username, user.password), 200)["access_token"]
        claims = jwt.decode(token, options={"verify_signature": False})

        assert claims["sub"] == user.username
        assert claims["iss"] == "TaxTestOps"
        assert claims["exp"] > claims["iat"]

    def test_password_is_never_returned(self, api: ApiClient, user: UserSession) -> None:
        """响应中不得出现密码或哈希——这是接口契约的硬性要求。"""
        body = api.expect(api.login(user.username, user.password), 200)
        serialized = str(body)

        assert user.password not in serialized
        assert "password" not in serialized


class TestAuthenticatedAccess:
    """受保护接口的鉴权行为。"""

    def test_me_returns_current_user(self, authed_api: ApiClient, user: UserSession) -> None:
        body = authed_api.expect(authed_api.me(), 200)

        assert body["username"] == user.username
        assert body["id"] == user.user_id

    @pytest.mark.parametrize(
        "headers",
        [
            pytest.param({}, id="缺少 Authorization 头"),
            pytest.param({"Authorization": "Bearer"}, id="Bearer 后无令牌"),
            pytest.param({"Authorization": "Bearer not-a-jwt"}, id="令牌格式非法"),
            pytest.param({"Authorization": "Basic dXNlcjpwYXNz"}, id="认证方案不支持"),
            pytest.param({"Authorization": "Bearer a.b.c"}, id="签名非法"),
        ],
    )
    def test_rejects_bad_authorization_header(
        self, api: ApiClient, headers: dict[str, str]
    ) -> None:
        response = api.get("/api/v1/auth/me", headers=headers, authenticated=False)

        assert response.status_code == 401

    def test_rejects_token_signed_with_another_secret(
        self, api: ApiClient, user: UserSession
    ) -> None:
        """用错误密钥签发的令牌必须被拒绝——这是签名校验的核心断言。"""
        forged = jwt.encode(
            {"sub": user.username, "iss": "TaxTestOps", "exp": 9_999_999_999},
            secrets.token_urlsafe(48),
            algorithm="HS256",
        )

        response = api.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {forged}"},
            authenticated=False,
        )

        assert response.status_code == 401

    def test_rejects_expired_token(
        self, api: ApiClient, user: UserSession, app_settings: Settings
    ) -> None:
        """过期令牌必须失效，不能无限期可用。"""
        expired, _ = create_access_token(
            user.username,
            secret=app_settings.jwt_secret,
            expires_minutes=-1,
            issuer=app_settings.app_name,
        )

        response = api.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {expired}"},
            authenticated=False,
        )

        assert response.status_code == 401

    def test_rejects_token_from_another_issuer(
        self, api: ApiClient, user: UserSession, app_settings: Settings
    ) -> None:
        """签发方不符的令牌必须被拒绝，防止跨系统令牌复用。"""
        foreign, _ = create_access_token(
            user.username,
            secret=app_settings.jwt_secret,
            issuer="another-system",
        )

        response = api.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {foreign}"},
            authenticated=False,
        )

        assert response.status_code == 401

    def test_rejects_token_of_deactivated_user(
        self, api: ApiClient, user: UserSession, app: FastAPI
    ) -> None:
        """账号停用后，此前签发的令牌必须立即失效。"""
        authed = api.login_as(user.username, user.password)
        assert authed.me().status_code == 200

        _deactivate(app, user.user_id)

        assert authed.me().status_code == 401
