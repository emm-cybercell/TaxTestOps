"""测试支撑工具：接口客户端、数据工厂、断言助手。

本包被 `tests/` 下各层共用，避免每个测试文件重复实现「登录 → 拿 token → 发请求」
这类样板逻辑。分层框架的「API Object 二次封装」就落在这里。
"""

from support.api_client import ApiClient, ApiResponse, UserSession
from support.factories import (
    calculate_payload,
    person_payload,
    random_password,
    random_username,
)
from support.servers import LiveServer, serve_app

__all__ = [
    "ApiClient",
    "ApiResponse",
    "LiveServer",
    "UserSession",
    "calculate_payload",
    "person_payload",
    "random_password",
    "random_username",
    "serve_app",
]
