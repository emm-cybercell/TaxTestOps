"""平台测试用例数据。

放在 ``support/`` 而不是 ``conftest.py`` 里，是为了让测试模块能用普通的
``from support.platform_cases import ...`` 引用——从 ``conftest`` 导入依赖
pytest 的 sys.path 插入行为，一旦换导入模式就会失效。
"""

from __future__ import annotations

from typing import Any


def case_payload(
    name: str,
    *,
    method: str = "GET",
    path: str = "/healthz",
    payload: dict[str, Any] | None = None,
    expected_status: int = 200,
    assertions: list[dict[str, Any]] | None = None,
    tags: list[str] | None = None,
    requires_auth: bool = False,
    retries: int = 0,
) -> dict[str, Any]:
    """构造一条平台用例定义。"""
    return {
        "name": name,
        "method": method,
        "path": path,
        "payload": payload,
        "expected_status": expected_status,
        "assertions": assertions,
        "tags": tags,
        "requires_auth": requires_auth,
        "retries": retries,
    }


#: 全部应通过的用例集。
#: 涉及测算接口的用例必须带 ``requires_auth``——它确实需要令牌，
#: 把这一点写进用例定义（而不是靠测试环境碰巧有令牌）才是诚实的。
HEALTHY_CASES: list[dict[str, Any]] = [
    case_payload(
        "健康检查可用",
        path="/healthz",
        assertions=[{"path": "status", "op": "eq", "value": "ok"}],
        tags=["smoke"],
    ),
    case_payload(
        "免征额样例税额为 90",
        method="POST",
        path="/api/v1/tax/calculate",
        payload={"salary": 8000, "persist": False},
        assertions=[{"path": "tax_amount", "op": "eq", "value": 90}],
        tags=["smoke", "regression"],
        requires_auth=True,
    ),
    case_payload(
        "应纳税所得额随扣除下降",
        method="POST",
        path="/api/v1/tax/calculate",
        payload={"salary": 20000, "person": {"children_num": 2}, "persist": False},
        assertions=[
            {"path": "taxable_income", "op": "eq", "value": 13000},
            {"path": "deductions.0.amount", "op": "eq", "value": 2000},
        ],
        tags=["regression"],
        requires_auth=True,
    ),
    case_payload(
        "未认证访问被拒",
        path="/api/v1/tax/records",
        expected_status=401,
        assertions=[{"path": "detail", "op": "contains", "value": "令牌"}],
        tags=["security"],
    ),
]
