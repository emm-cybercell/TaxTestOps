"""可复用的 OpenAPI 响应声明。

为什么需要集中声明
------------------
FastAPI 只会自动生成成功响应（200/201）与 422。像 401、403、404、409、429 这类
**业务上必然会发生**的状态码，必须在路由上显式声明，否则：

- 基于 OpenAPI 生成 SDK 的调用方不知道这些状态码存在，无法编写处理分支；
- 契约测试无法校验「声明过的状态码确实会发生」；
- 文档读者会误以为接口只会成功或参数错误。

该约束由 ``tests/contract/test_openapi_contract.py::test_error_responses_are_declared`` 守住。
"""

from __future__ import annotations

from typing import Any

UNAUTHORIZED: dict[int | str, dict[str, Any]] = {
    401: {"description": "缺少、过期或无效的访问令牌"},
}
"""需要认证的接口通用声明。"""

CONFLICT: dict[int | str, dict[str, Any]] = {
    409: {"description": "资源冲突（如用户名已存在）"},
}

NOT_FOUND: dict[int | str, dict[str, Any]] = {
    404: {"description": "资源不存在，或不属于当前用户"},
}

FORBIDDEN: dict[int | str, dict[str, Any]] = {
    403: {"description": "已认证但无权限"},
}

TOO_MANY_REQUESTS: dict[int | str, dict[str, Any]] = {
    429: {"description": "请求超过限流阈值，响应头 Retry-After 给出建议等待秒数"},
}

SERVICE_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    503: {"description": "依赖服务不可用"},
}

VALIDATION_FAILED: dict[int | str, dict[str, Any]] = {
    422: {"description": "请求参数校验失败"},
}


def merge(*groups: dict[int | str, dict[str, Any]]) -> dict[int | str, dict[str, Any]]:
    """合并多组响应声明，后者覆盖同键。"""
    merged: dict[int | str, dict[str, Any]] = {}
    for group in groups:
        merged.update(group)
    return merged
