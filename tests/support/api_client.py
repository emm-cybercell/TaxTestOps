"""接口测试客户端：对被测 HTTP 契约做一层语义化封装。

这是分层框架里的 **API Object 层**。测试用例只表达业务意图
（``api.calculate(salary=20000)``），不关心 URL、请求头、序列化细节——
URL 或字段名变化时只需改这一处，而不是散落在几十个测试里。

同时兼容两种传输：

- ``fastapi.testclient.TestClient``：接口层测试，进程内直连，最快；
- ``httpx.Client``：集成 / 契约 / 性能测试，走真实网络栈。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from support.factories import calculate_payload, person_payload

DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class ApiResponse:
    """对底层响应的统一包装。"""

    status_code: int
    body: Any
    headers: Mapping[str, str]
    raw_text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        return self.body

    def detail(self) -> str:
        """提取错误详情，用于断言与报错信息。"""
        if isinstance(self.body, dict):
            value = self.body.get("detail")
            if isinstance(value, str):
                return value
            if value is not None:
                return str(value)
        return self.raw_text[:300]

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<ApiResponse {self.status_code} {self.body!r:.160}>"


@dataclass
class UserSession:
    """一个已登录用户的凭据与令牌。"""

    username: str
    password: str
    token: str | None = None
    user_id: int | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}


class ApiClient:
    """语义化的接口客户端。"""

    def __init__(self, transport: Any, *, timeout: float | None = DEFAULT_TIMEOUT) -> None:
        """
        :param transport: 具备 ``request()`` 的传输对象（TestClient 或 httpx.Client）。
        :param timeout: 请求超时；传 ``None`` 表示不传该参数。
            进程内的 ``TestClient`` 不接受 ``timeout``（传了会触发 starlette 告警），
            因此测试夹具对它会显式传入 ``None``。
        """
        self._transport = transport
        self._timeout = timeout
        self.session: UserSession | None = None

    # ------------------------------------------------------------------ #
    # 底层请求
    # ------------------------------------------------------------------ #

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        authenticated: bool = True,
    ) -> ApiResponse:
        """发送请求并包装响应。"""
        merged_headers: dict[str, str] = {"Content-Type": "application/json"}
        if authenticated and self.session and self.session.token:
            merged_headers.update(self.session.headers)
        if headers:
            merged_headers.update(headers)

        kwargs: dict[str, Any] = {
            "json": json,
            "headers": merged_headers,
            "params": dict(params) if params else None,
        }
        if self._timeout is not None:
            kwargs["timeout"] = self._timeout

        response = self._transport.request(method, path, **kwargs)
        try:
            body = response.json()
        except Exception:  # noqa: BLE001 - 非 JSON 响应（如 HTML 错误页）
            body = None

        return ApiResponse(
            status_code=response.status_code,
            body=body,
            # 保留原始 headers 对象：httpx.Headers 大小写不敏感，
            # 转成普通 dict 后 "WWW-Authenticate" 会取不到值。
            headers=response.headers,
            raw_text=getattr(response, "text", "") or "",
        )

    def get(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> ApiResponse:
        return self.request("POST", path, **kwargs)

    # ------------------------------------------------------------------ #
    # 认证
    # ------------------------------------------------------------------ #

    def register(self, username: str, password: str) -> ApiResponse:
        return self.post(
            "/api/v1/auth/register",
            json={"username": username, "password": password},
            authenticated=False,
        )

    def login(self, username: str, password: str) -> ApiResponse:
        response = self.post(
            "/api/v1/auth/login",
            json={"username": username, "password": password},
            authenticated=False,
        )
        if response.ok and isinstance(response.body, dict):
            token = response.body.get("access_token")
            if self.session is None:
                self.session = UserSession(username=username, password=password)
            self.session.username = username
            self.session.password = password
            self.session.token = token
        return response

    def me(self) -> ApiResponse:
        return self.get("/api/v1/auth/me")

    def login_as(self, username: str, password: str) -> ApiClient:
        """登录并返回自身，便于链式调用。"""
        response = self.login(username, password)
        if not response.ok:
            raise AssertionError(f"登录失败：{response.status_code} {response.detail()}")
        return self

    def logout(self) -> None:
        """清除本地令牌（不调用服务端接口，JWT 无状态）。"""
        if self.session is not None:
            self.session.token = None

    # ------------------------------------------------------------------ #
    # 业务接口
    # ------------------------------------------------------------------ #

    def health(self) -> ApiResponse:
        return self.get("/healthz", authenticated=False)

    def calculate(
        self,
        salary: int = 20_000,
        *,
        person: dict[str, Any] | None = None,
        persist: bool = True,
        **overrides: Any,
    ) -> ApiResponse:
        """单次测算。"""
        payload = calculate_payload(salary, person=person, persist=persist, **overrides)
        return self.post("/api/v1/tax/calculate", json=payload)

    def batch(self, items: list[dict[str, Any]], **overrides: Any) -> ApiResponse:
        """批量申报。"""
        payload: dict[str, Any] = {"items": items}
        payload.update(overrides)
        return self.post("/api/v1/tax/batch", json=payload)

    def records(self, *, limit: int = 20, offset: int = 0) -> ApiResponse:
        return self.get("/api/v1/tax/records", params={"limit": limit, "offset": offset})

    def record(self, record_id: int) -> ApiResponse:
        return self.get(f"/api/v1/tax/records/{record_id}")

    def policy(self) -> ApiResponse:
        return self.get("/api/v1/tax/policy")

    def openapi(self) -> ApiResponse:
        return self.get("/openapi.json", authenticated=False)

    # ------------------------------------------------------------------ #
    # 断言助手
    # ------------------------------------------------------------------ #

    def expect(self, response: ApiResponse, status_code: int = 200) -> Any:
        """断言状态码并返回响应体；失败时抛出带响应内容的异常，便于定位。"""
        if response.status_code != status_code:
            raise AssertionError(
                f"期望 HTTP {status_code}，实际 {response.status_code}\n响应体：{response.body!r}"
            )
        return response.body


def build_person(**overrides: Any) -> dict[str, Any]:
    """便捷转发，供测试直接构造纳税人载荷。"""
    return person_payload(**overrides)
