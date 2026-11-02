"""契约层 · 基于 OpenAPI 自动生成用例的模糊测试。

与 ``test_openapi_contract.py`` 的分工
-------------------------------------
后者用**手写的具体请求**校验响应是否符合声明；本文件用 **schemathesis 从 OpenAPI
规范自动生成**请求，覆盖手写用例想不到的输入组合。

断言的是**服务健壮性底线**：任何自动生成的输入都不应导致 5xx。参数校验失败返回
4xx 是正确行为；返回 500 说明存在未处理的异常路径。

关于确定性
----------
默认 ``derandomize=True``，让 CI 结果可复现——随机化的模糊测试在 CI 里会产生
「这次红下次绿」的噪声，反而降低信号价值。需要探索性运行时设置
``TAX_FUZZ_RANDOM=1``。
"""

from __future__ import annotations

import os

import pytest
import schemathesis
from fastapi import FastAPI
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from support.api_client import ApiClient

pytestmark = [pytest.mark.contract, pytest.mark.slow]

#: 期望被 OpenAPI 文档暴露的操作清单。
#: 该清单由 ``test_operation_catalog_matches`` 与实际文档比对，防止新增接口后漏测。
EXPECTED_OPERATIONS = (
    "GET /healthz",
    "POST /api/v1/auth/register",
    "POST /api/v1/auth/login",
    "GET /api/v1/auth/me",
    "POST /api/v1/tax/calculate",
    "POST /api/v1/tax/batch",
    "GET /api/v1/tax/policy",
    "GET /api/v1/tax/records",
    "GET /api/v1/tax/records/{record_id}",
)

MAX_EXAMPLES = int(os.getenv("TAX_FUZZ_EXAMPLES", "15"))
DERANDOMIZE = os.getenv("TAX_FUZZ_RANDOM", "").strip() not in {"1", "true", "yes"}

HYPOTHESIS_SETTINGS = settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    derandomize=DERANDOMIZE,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.function_scoped_fixture,
        HealthCheck.filter_too_much,
    ],
)


@pytest.fixture
def openapi_schema(app: FastAPI) -> schemathesis.openapi.OpenApiSchema:
    """从 ASGI 应用加载 OpenAPI 规范。"""
    return schemathesis.openapi.from_asgi("/openapi.json", app)


@pytest.fixture
def operations(openapi_schema) -> dict[str, object]:
    """``{操作标签: APIOperation}``。

    schemathesis 4.x 的 ``get_all_operations()`` 返回 ``Result`` 包装，
    需要调用 ``.ok()`` 取出实际对象。
    """
    collected: dict[str, object] = {}
    for result in openapi_schema.get_all_operations():
        operation = result.ok()
        collected[operation.label] = operation
    return collected


class TestOperationCatalog:
    """先确认「测的是不是全部接口」，再谈测得好不好。"""

    def test_operation_catalog_matches(self, operations: dict[str, object]) -> None:
        """文档暴露的操作必须与预期清单完全一致。

        新增接口却忘记补契约测试，是契约测试最典型的失效方式——这条用例把它变成
        一个明确的失败，而不是静默漏测。
        """
        assert set(operations) == set(EXPECTED_OPERATIONS), (
            f"未预期的操作：{sorted(set(operations) - set(EXPECTED_OPERATIONS))}；"
            f"缺失的操作：{sorted(set(EXPECTED_OPERATIONS) - set(operations))}"
        )

    def test_every_operation_has_strategy(self, operations: dict[str, object]) -> None:
        """每个操作都应能生成测试用例，否则该接口实际上没被模糊测试覆盖。"""
        without_strategy = [
            label for label, operation in operations.items() if operation.as_strategy() is None
        ]

        assert not without_strategy, f"以下操作无法生成用例：{without_strategy}"


class TestFuzzedRequestsNeverReturn5xx:
    """自动生成的请求不得触发服务端错误。"""

    @staticmethod
    def _header_value(response, name: str) -> str:
        """读取响应头。

        schemathesis 的 ``Response.headers`` 是 multidict，取值返回**列表**而非字符串，
        因此必须先归一化——直接 ``"json" in headers.get(...)`` 会得到反直觉的结果。
        """
        raw = response.headers.get(name, "")
        if isinstance(raw, (list, tuple)):
            return " ".join(str(part) for part in raw)
        return str(raw)

    @pytest.mark.parametrize("label", EXPECTED_OPERATIONS)
    @HYPOTHESIS_SETTINGS
    @given(data=st.data())
    def test_no_server_error(self, operations: dict[str, object], label: str, data) -> None:
        """任意自动生成的输入都不得导致 5xx。

        4xx 是**预期**结果（参数非法本就该拒绝）；5xx 说明有异常穿透到了框架层，
        属于未处理的缺陷。
        """
        operation = operations[label]
        case = data.draw(operation.as_strategy())

        response = case.call()

        assert response.status_code < 500, (
            f"{label} 在输入 {case} 下返回了 {response.status_code}，响应体：{response.text[:300]}"
        )

    @pytest.mark.parametrize("label", EXPECTED_OPERATIONS)
    @HYPOTHESIS_SETTINGS
    @given(data=st.data())
    def test_response_is_json_or_empty(
        self, operations: dict[str, object], label: str, data
    ) -> None:
        """响应必须是 JSON 或空体。

        返回 HTML 错误页会让 JSON 客户端直接解析失败——这是「框架兜底页泄漏到
        接口契约」的典型症状。
        """
        operation = operations[label]
        case = data.draw(operation.as_strategy())

        response = case.call()

        if not response.content:
            return

        content_type = self._header_value(response, "content-type")
        assert "json" in content_type.lower(), (
            f"{label} 返回了非 JSON 内容：content-type={content_type!r}，"
            f"body={response.text[:200]!r}"
        )

    @pytest.mark.parametrize("label", EXPECTED_OPERATIONS)
    @HYPOTHESIS_SETTINGS
    @given(data=st.data())
    def test_returned_status_is_declared(
        self, operations: dict[str, object], label: str, data
    ) -> None:
        """实际返回的状态码必须已在文档中声明。

        未声明的状态码（例如实现返回 403 但文档只写了 401）会让调用方的错误处理
        分支失效。5xx 单独由上面的用例负责，这里排除以免重复报错。
        """
        operation = operations[label]
        case = data.draw(operation.as_strategy())

        response = case.call()
        if response.status_code >= 500:
            return

        declared = {int(code) for code in operation.responses.status_codes if str(code).isdigit()}

        assert response.status_code in declared, (
            f"{label} 返回了未声明的状态码 {response.status_code}，已声明：{sorted(declared)}"
        )


class TestAuthenticatedFuzzing:
    """带认证的模糊测试：覆盖业务逻辑主体。"""

    @pytest.mark.parametrize(
        "label",
        [
            "POST /api/v1/tax/calculate",
            "POST /api/v1/tax/batch",
            "GET /api/v1/tax/records",
            "GET /api/v1/auth/me",
        ],
    )
    @HYPOTHESIS_SETTINGS
    @given(data=st.data())
    def test_authenticated_requests_never_return_5xx(
        self, operations: dict[str, object], authed_api: ApiClient, label: str, data
    ) -> None:
        """带上有效令牌后，自动生成的请求同样不得触发 5xx。

        未认证的模糊测试大部分请求会被 401 挡在业务逻辑之外，因此必须补一轮
        认证后的模糊测试才能真正覆盖业务代码。
        """
        operation = operations[label]
        case = data.draw(operation.as_strategy())
        case.headers = {**(case.headers or {}), **authed_api.session.headers}

        response = case.call()

        assert response.status_code < 500, (
            f"{label}（已认证）在输入 {case} 下返回了 {response.status_code}，"
            f"响应体：{response.text[:300]}"
        )

    @HYPOTHESIS_SETTINGS
    @given(data=st.data())
    def test_calculate_with_valid_person_always_succeeds(
        self, operations: dict[str, object], authed_api: ApiClient, data
    ) -> None:
        """在**合法输入域**内随机取值时，测算接口必须稳定返回 200。

        与上面「不得 5xx」不同，这条断言的是「不该拒绝的请求不会被拒绝」——
        边界校验过严（例如误把合法值判为非法）会让正常业务失败，
        而这类问题不会表现为 5xx，只看状态码小于 500 是发现不了的。
        """
        operation = operations["POST /api/v1/tax/calculate"]

        body = data.draw(
            st.fixed_dictionaries(
                {
                    "salary": st.integers(min_value=0, max_value=10_000_000),
                    "person": st.fixed_dictionaries(
                        {
                            "children_num": st.integers(min_value=0, max_value=20),
                            "is_keep_stu": st.sampled_from([0, 1, 2]),
                            "house_loans": st.booleans(),
                            "is_rent_house": st.sampled_from([0, 1, 2, 3]),
                            "supporting_elderly": st.booleans(),
                            "sibling_num": st.integers(min_value=0, max_value=20),
                        }
                    ),
                }
            )
        )
        # 住房贷款与住房租金互斥：同时为真时请求本身非法，不属于「合法输入域」
        if body["person"]["house_loans"] and body["person"]["is_rent_house"] != 0:
            body["person"]["is_rent_house"] = 0

        case = operation.Case(body=body)
        case.headers = {**(case.headers or {}), **authed_api.session.headers}

        response = case.call()

        assert response.status_code == 200, (
            f"合法输入 {body} 被拒绝：{response.status_code} {response.text[:300]}"
        )
