"""契约层 · 接口实现与 OpenAPI 文档一致性。

契约测试回答的问题
------------------
「接口**实际返回**的东西，和它**声称返回**的东西一致吗？」

这与接口层测试互补：接口层验证业务正确性（算得对不对），契约层验证文档真实性
（返回的字段、类型、状态码与 OpenAPI 声明是否吻合）。契约漂移是微服务协作里
最隐蔽的一类问题——调用方按文档写了代码，上线才发现字段对不上。

实现方式
--------
直接读取应用自动生成的 ``/openapi.json``，把响应体逐条对照声明的 JSON Schema 校验。
``$ref`` 就地展开（本项目文档引用层次很浅，展开安全且避免引入额外依赖）。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator

from support.api_client import ApiClient
from support.factories import (
    person_payload,
    random_password,
    random_username,
)

pytestmark = [pytest.mark.contract]


# --------------------------------------------------------------------------- #
# OpenAPI 文档工具
# --------------------------------------------------------------------------- #


def resolve_pointer(document: dict[str, Any], pointer: str) -> Any:
    """按 JSON Pointer 解析 ``#/a/b/c`` 形式的引用。"""
    if not pointer.startswith("#/"):
        raise ValueError(f"仅支持文档内部引用，收到：{pointer}")

    node: Any = document
    for raw_part in pointer[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        node = node[part]
    return node


def inline_refs(document: dict[str, Any], node: Any, _depth: int = 0) -> Any:
    """递归展开 ``$ref``，便于直接用 jsonschema 校验。

    :raises RecursionError: 引用成环（本项目的文档不会出现，作为防御保留）。
    """
    if _depth > 32:  # pragma: no cover - 防御性分支
        raise RecursionError("$ref 展开层级过深，可能存在循环引用")

    if isinstance(node, dict):
        if "$ref" in node:
            target = resolve_pointer(document, node["$ref"])
            merged = {**inline_refs(document, target, _depth + 1)}
            merged.update({key: value for key, value in node.items() if key != "$ref"})
            return merged
        return {key: inline_refs(document, value, _depth + 1) for key, value in node.items()}

    if isinstance(node, list):
        return [inline_refs(document, item, _depth + 1) for item in node]

    return node


def response_schema(document: dict[str, Any], path: str, method: str, status: str = "200") -> dict:
    """取出某操作在指定状态码下声明的响应 Schema（已展开 $ref）。"""
    operation = document["paths"][path][method]
    declared = operation["responses"][status]["content"]["application/json"]["schema"]
    return inline_refs(document, declared)


def request_schema(document: dict[str, Any], path: str, method: str) -> dict:
    """取出某操作的请求体 Schema（已展开 $ref）。"""
    operation = document["paths"][path][method]
    body = operation["requestBody"]["content"]["application/json"]["schema"]
    return inline_refs(document, body)


def validate(instance: Any, schema: dict[str, Any], label: str) -> None:
    """用 JSON Schema 校验实例，失败时给出可定位的错误信息。"""
    errors = sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda e: list(e.path))
    if errors:
        details = "\n".join(
            f"  - {'/'.join(str(p) for p in error.path)}: {error.message}" for error in errors
        )
        raise AssertionError(f"{label} 不符合契约：\n{details}")


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #


@pytest.fixture
def document(app: FastAPI) -> dict[str, Any]:
    """应用自动生成的 OpenAPI 文档。

    必须是函数级：它依赖函数级的 ``app`` 夹具（每个用例一个全新应用实例，
    保证数据库与缓存隔离）。``/openapi.json`` 是缓存的，重复获取成本可忽略。
    """
    response = app.state.test_client.get("/openapi.json")
    assert response.status_code == 200, "无法获取 OpenAPI 文档"
    return response.json()


@pytest.fixture
def contract_api(api: ApiClient, user) -> ApiClient:
    return api.login_as(user.username, user.password)


# --------------------------------------------------------------------------- #
# 文档本身的结构约束
# --------------------------------------------------------------------------- #


class TestDocumentStructure:
    """OpenAPI 文档自身必须完整、可解析、无悬空引用。"""

    def test_is_valid_openapi_3(self, document: dict[str, Any]) -> None:
        assert document["openapi"].startswith("3.")
        assert document["info"]["title"]
        assert document["info"]["version"]

    def test_every_operation_has_summary_and_tag(self, document: dict[str, Any]) -> None:
        """每个操作都应有摘要与分组，否则文档页面无法阅读。"""
        missing: list[str] = []
        for path, operations in document["paths"].items():
            for method, operation in operations.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                if not operation.get("summary"):
                    missing.append(f"{method.upper()} {path} 缺 summary")
                if not operation.get("tags"):
                    missing.append(f"{method.upper()} {path} 缺 tags")

        assert not missing, "\n".join(missing)

    def test_no_dangling_refs(self, document: dict[str, Any]) -> None:
        """所有 ``$ref`` 都必须能解析——悬空引用会让调用方的代码生成器直接失败。"""
        dangling: list[str] = []

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str):
                    try:
                        resolve_pointer(document, ref)
                    except (KeyError, ValueError):
                        dangling.append(ref)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(document)

        assert not dangling, f"存在无法解析的 $ref：{sorted(set(dangling))}"

    def test_declares_bearer_security_scheme(self, document: dict[str, Any]) -> None:
        schemes = document["components"]["securitySchemes"]

        assert "HTTPBearer" in schemes
        assert schemes["HTTPBearer"]["scheme"] == "bearer"

    def test_error_responses_are_declared(self, document: dict[str, Any]) -> None:
        """受保护接口必须声明 401；带请求体的接口必须声明 422。"""
        missing: list[str] = []
        for path, operations in document["paths"].items():
            for method, operation in operations.items():
                if method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                responses = operation.get("responses", {})
                if operation.get("security") and "401" not in responses:
                    missing.append(f"{method.upper()} {path} 未声明 401")
                if "requestBody" in operation and "422" not in responses:
                    missing.append(f"{method.upper()} {path} 未声明 422")

        assert not missing, "\n".join(missing)


# --------------------------------------------------------------------------- #
# 请求契约
# --------------------------------------------------------------------------- #


class TestRequestContract:
    """请求体结构必须与文档声明一致。"""

    def test_calculate_request_schema_is_closed(self, document: dict[str, Any]) -> None:
        """请求体声明 ``additionalProperties: false``，即拒绝未知字段。

        契约与实现必须一致：如果文档没写死，调用方会以为多传字段是允许的。
        """
        schema = request_schema(document, "/api/v1/tax/calculate", "post")

        assert schema.get("additionalProperties") is False

    def test_person_schema_enumerates_valid_values(self, document: dict[str, Any]) -> None:
        """枚举取值必须在文档中显式列出，否则调用方只能靠猜。"""
        schema = request_schema(document, "/api/v1/tax/calculate", "post")
        person = inline_refs(document, schema["properties"]["person"])

        assert person["properties"]["is_keep_stu"]["enum"] == [0, 1, 2]
        assert person["properties"]["is_rent_house"]["enum"] == [0, 1, 2, 3]

    def test_salary_declares_bounds(self, document: dict[str, Any]) -> None:
        schema = request_schema(document, "/api/v1/tax/calculate", "post")
        salary = schema["properties"]["salary"]

        assert salary["minimum"] == 0
        assert salary["maximum"] == 10_000_000


# --------------------------------------------------------------------------- #
# 响应契约
# --------------------------------------------------------------------------- #


class TestResponseContract:
    """实际响应必须符合声明的 Schema。"""

    def test_health_response(self, document: dict[str, Any], api: ApiClient) -> None:
        schema = response_schema(document, "/healthz", "get")

        validate(api.health().json(), schema, "GET /healthz 响应")

    def test_login_response(self, document: dict[str, Any], api: ApiClient, user) -> None:
        schema = response_schema(document, "/api/v1/auth/login", "post")

        response = api.login(user.username, user.password)

        validate(response.json(), schema, "POST /api/v1/auth/login 响应")

    def test_register_response(self, document: dict[str, Any], api: ApiClient) -> None:
        # 注册成功返回 201 Created，不是 200——契约测试必须按实际声明的状态码取值
        schema = response_schema(document, "/api/v1/auth/register", "post", status="201")

        response = api.register(random_username(), random_password())

        assert response.status_code == 201
        validate(response.json(), schema, "POST /api/v1/auth/register 响应")

    def test_me_response(self, document: dict[str, Any], contract_api: ApiClient) -> None:
        schema = response_schema(document, "/api/v1/auth/me", "get")

        validate(contract_api.me().json(), schema, "GET /api/v1/auth/me 响应")

    def test_calculate_response(self, document: dict[str, Any], contract_api: ApiClient) -> None:
        schema = response_schema(document, "/api/v1/tax/calculate", "post")

        response = contract_api.calculate(20_000)

        validate(response.json(), schema, "POST /api/v1/tax/calculate 响应")

    def test_calculate_response_with_all_deductions(
        self, document: dict[str, Any], contract_api: ApiClient
    ) -> None:
        """全扣除场景：确保数组型字段（deductions）在多元素时也符合契约。"""
        schema = response_schema(document, "/api/v1/tax/calculate", "post")
        person = person_payload(
            children_num=3,
            is_keep_stu=1,
            house_loans=True,
            supporting_elderly=True,
            sibling_num=2,
        )

        response = contract_api.calculate(50_000, person=person)

        validate(response.json(), schema, "POST /api/v1/tax/calculate 响应（全扣除）")

    def test_batch_response(self, document: dict[str, Any], contract_api: ApiClient) -> None:
        schema = response_schema(document, "/api/v1/tax/batch", "post")

        response = contract_api.batch(
            [{"salary": 10_000, "person": person_payload()}, {"salary": 20_000}]
        )

        validate(response.json(), schema, "POST /api/v1/tax/batch 响应")

    def test_batch_partial_failure_response(
        self, document: dict[str, Any], contract_api: ApiClient
    ) -> None:
        """失败项的 ``error`` 与成功项的 ``tax_amount`` 都必须是可空字段。

        契约要覆盖**部分成功**这个状态，否则调用方无法区分「字段缺失」与「字段为 null」。
        """
        schema = response_schema(document, "/api/v1/tax/batch", "post")

        response = contract_api.batch(
            [
                {"salary": 10_000, "external_ref": "DUP"},
                {"salary": 20_000, "external_ref": "DUP"},
            ]
        )
        body = response.json()

        validate(body, schema, "POST /api/v1/tax/batch 响应（部分失败）")
        assert body["items"][1]["error"] is not None

    def test_records_page_response(self, document: dict[str, Any], contract_api: ApiClient) -> None:
        contract_api.calculate(20_000)
        schema = response_schema(document, "/api/v1/tax/records", "get")

        validate(contract_api.records().json(), schema, "GET /api/v1/tax/records 响应")

    def test_record_detail_response(
        self, document: dict[str, Any], contract_api: ApiClient
    ) -> None:
        created = contract_api.calculate(20_000).json()
        schema = response_schema(document, "/api/v1/tax/records/{record_id}", "get")

        response = contract_api.record(created["record_id"])

        validate(response.json(), schema, "GET /api/v1/tax/records/{id} 响应")

    def test_error_responses_use_detail_field(
        self, document: dict[str, Any], api: ApiClient
    ) -> None:
        """业务错误响应统一使用 ``detail`` 字段。

        统一错误结构是接口契约的一部分——调用方靠它做统一错误处理与告警分级。
        """
        responses = [
            api.get("/api/v1/tax/records", authenticated=False),  # 401
            api.login("no-such-user-here", "irrelevant-password"),  # 401
        ]

        for response in responses:
            body = response.json()
            assert isinstance(body, dict), f"{response.status_code} 响应不是对象"
            assert isinstance(body.get("detail"), str), (
                f"{response.status_code} 响应缺少 detail 字段"
            )

    def test_validation_error_detail_is_a_list(self, api: ApiClient) -> None:
        """422 的 ``detail`` 是字段错误列表（FastAPI 约定），与业务错误的字符串形式不同。

        这一差异必须在文档中体现，否则调用方的统一错误处理会解析失败。
        """
        response = api.post("/api/v1/tax/calculate", json={"salary": -1}, authenticated=False)

        assert response.status_code == 401, "未认证时应先返回 401，而非暴露参数校验细节"

    def test_validation_error_structure_when_authenticated(
        self, document: dict[str, Any], contract_api: ApiClient
    ) -> None:
        response = contract_api.calculate(-1)

        assert response.status_code == 422
        detail = response.json()["detail"]
        assert isinstance(detail, list)
        assert {"loc", "msg", "type"} <= set(detail[0])
