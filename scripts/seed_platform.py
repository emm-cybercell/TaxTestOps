"""向测试平台数据库导入内置的个税测试用例集。

用途
----
本地启动测试平台后，运行本脚本可一键灌入标准用例集（冒烟、边界值、负向校验、
专项附加扣除叠加等 15+ 条用例），打开平台 Web 界面（``http://localhost:8100``）
即可直接点击「执行全部用例」观察执行进度、趋势图表与失败归因。

用法::

    # 默认连接本地平台（http://127.0.0.1:8100）
    .venv/Scripts/python.exe scripts/seed_platform.py

    # 指定平台地址
    .venv/Scripts/python.exe scripts/seed_platform.py --platform-url http://127.0.0.1:8100
"""

from __future__ import annotations

import argparse
from typing import Any

import httpx

SEED_CASES: list[dict[str, Any]] = [
    {
        "name": "[冒烟] 服务健康检查探针",
        "description": "验证个税被测服务的健康状态接口正常响应",
        "method": "GET",
        "path": "/healthz",
        "expected_status": 200,
        "assertions": [{"path": "status", "op": "eq", "value": "ok"}],
        "tags": ["smoke", "ops"],
        "requires_auth": False,
    },
    {
        "name": "[冒烟] 需求文档小李算例",
        "description": "月收入 10000、租金 1500、子女 1000、赡养 1000，扣除后个税为 0",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {
            "salary": 8000,
            "person": {
                "children_num": 1,
                "is_rent_house": 1,
                "supporting_elderly": True,
                "sibling_num": 1,
            },
            "persist": False,
        },
        "expected_status": 200,
        "assertions": [
            {"path": "tax_amount", "op": "eq", "value": 0},
            {"path": "taxable_income", "op": "le", "value": 0},
        ],
        "tags": ["smoke", "regression"],
        "requires_auth": True,
    },
    {
        "name": "[边界] 免征额下边界（月薪 5000）",
        "description": "月薪恰等于免征额 5000 元，应纳税所得额为 0，税额为 0",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {"salary": 5000, "persist": False},
        "expected_status": 200,
        "assertions": [
            {"path": "tax_amount", "op": "eq", "value": 0},
            {"path": "taxable_income", "op": "eq", "value": 0},
        ],
        "tags": ["boundary"],
        "requires_auth": True,
    },
    {
        "name": "[边界] 低于免征额 1 元（月薪 4999）",
        "description": "月薪低于免征额 1 元，不产生应纳税所得额，税额为 0",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {"salary": 4999, "persist": False},
        "expected_status": 200,
        "assertions": [
            {"path": "tax_amount", "op": "eq", "value": 0},
            {"path": "taxable_income", "op": "eq", "value": -1},
        ],
        "tags": ["boundary"],
        "requires_auth": True,
    },
    {
        "name": "[边界] 第 1 级超额累进上界内侧（应纳税所得额 2999）",
        "description": "月薪 7999 元，应纳税所得额 2999 元，税率 3%，税额 89 元",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {"salary": 7999, "persist": False},
        "expected_status": 200,
        "assertions": [
            {"path": "tax_amount", "op": "eq", "value": 89},
            {"path": "taxable_income", "op": "eq", "value": 2999},
        ],
        "tags": ["boundary"],
        "requires_auth": True,
    },
    {
        "name": "[边界] 第 1/2 级分界点（应纳税所得额 3000）",
        "description": "月薪 8000 元，应纳税所得额 3000 元，税额 90 元",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {"salary": 8000, "persist": False},
        "expected_status": 200,
        "assertions": [
            {"path": "tax_amount", "op": "eq", "value": 90},
            {"path": "taxable_income", "op": "eq", "value": 3000},
        ],
        "tags": ["boundary"],
        "requires_auth": True,
    },
    {
        "name": "[边界] 第 2 级上界内侧（应纳税所得额 11999）",
        "description": "月薪 16999 元，应纳税所得额 11999 元，税额 989 元",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {"salary": 16999, "persist": False},
        "expected_status": 200,
        "assertions": [
            {"path": "tax_amount", "op": "eq", "value": 989},
            {"path": "taxable_income", "op": "eq", "value": 11999},
        ],
        "tags": ["boundary"],
        "requires_auth": True,
    },
    {
        "name": "[缺陷复现 D1] 第 2/3 级分界点速算扣除数核算",
        "description": "应纳税所得额 12000 元，国标期望 990 元；旧实现多计 1300 得到 2290 元",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {"salary": 17000, "persist": False},
        "expected_status": 200,
        "assertions": [
            {"path": "taxable_income", "op": "eq", "value": 12000},
            {"path": "tax_amount", "op": "eq", "value": 990},
        ],
        "tags": ["defect-d1", "regression"],
        "requires_auth": True,
    },
    {
        "name": "[专项扣除] 双子女教育扣除",
        "description": "2 名子女每月各扣除 1000 元，明细与所得额校验",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {
            "salary": 20000,
            "person": {"children_num": 2},
            "persist": False,
        },
        "expected_status": 200,
        "assertions": [
            {"path": "taxable_income", "op": "eq", "value": 13000},
            {"path": "deductions.0.code", "op": "eq", "value": "child_education"},
            {"path": "deductions.0.amount", "op": "eq", "value": 2000},
        ],
        "tags": ["deduction"],
        "requires_auth": True,
    },
    {
        "name": "[专项扣除] 独生子女全额赡养老人扣除",
        "description": "独生子女全额享受每月 2000 元赡养扣除，不受分摊上限限制",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {
            "salary": 20000,
            "person": {"supporting_elderly": True, "sibling_num": 0},
            "persist": False,
        },
        "expected_status": 200,
        "assertions": [
            {"path": "taxable_income", "op": "eq", "value": 13000},
            {"path": "deductions.0.code", "op": "eq", "value": "elderly_care"},
            {"path": "deductions.0.amount", "op": "eq", "value": 2000},
        ],
        "tags": ["deduction"],
        "requires_auth": True,
    },
    {
        "name": "[专项扣除] 非独生子女分摊赡养老人扣除封顶",
        "description": "兄弟姐妹 1 人（共 2 人分摊），每人 1000 元达分摊上限",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {
            "salary": 20000,
            "person": {"supporting_elderly": True, "sibling_num": 1},
            "persist": False,
        },
        "expected_status": 200,
        "assertions": [
            {"path": "taxable_income", "op": "eq", "value": 14000},
            {"path": "deductions.0.amount", "op": "eq", "value": 1000},
        ],
        "tags": ["deduction"],
        "requires_auth": True,
    },
    {
        "name": "[负向校验] 房贷利息与租金不可同时享受",
        "description": "政策规定房贷利息与租金互斥，同时提交必须返回 422 拒绝",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {
            "salary": 20000,
            "person": {"house_loans": True, "is_rent_house": 1},
            "persist": False,
        },
        "expected_status": 422,
        "assertions": [{"path": "detail", "op": "contains", "value": "不可同时扣除"}],
        "tags": ["negative", "defect-d2"],
        "requires_auth": True,
    },
    {
        "name": "[负向校验] 负数工资拒绝",
        "description": "工资为负数必须被请求参数校验拦截并返回 422",
        "method": "POST",
        "path": "/api/v1/tax/calculate",
        "payload": {"salary": -1},
        "expected_status": 422,
        "tags": ["negative"],
        "requires_auth": True,
    },
    {
        "name": "[安全鉴权] 受保护接口未携带令牌拒绝",
        "description": "申报历史查询接口在缺少 Authorization 头时必须返回 401",
        "method": "GET",
        "path": "/api/v1/tax/records",
        "expected_status": 401,
        "assertions": [{"path": "detail", "op": "contains", "value": "令牌"}],
        "tags": ["security"],
        "requires_auth": False,
    },
]


def seed(platform_url: str) -> int:
    base = platform_url.rstrip("/")
    print(f"正在向测试平台导入内置用例：{base}")

    with httpx.Client(base_url=base, timeout=10.0) as client:
        try:
            health = client.get("/healthz")
            health.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            print(f"✗ 无法连接测试平台（{base}/healthz）：{exc}")
            print("请先启动测试平台：")
            print(
                "  .venv/Scripts/python.exe -m uvicorn "
                "tax_service.platform.app:create_platform_app --factory --port 8100"
            )
            return 1

        success_count = 0
        for case in SEED_CASES:
            res = client.post("/api/v1/platform/cases", json=case)
            if res.status_code == 201:
                success_count += 1
                print(f"  ✓ [{case['method']}] {case['name']}")
            else:
                msg = res.text[:80]
                print(f"  ✗ [{case['method']}] {case['name']} 失败 ({res.status_code}): {msg}")

        print(f"\n导入完成：成功 {success_count} / 共 {len(SEED_CASES)} 条用例。")
        print(f"可打开浏览器访问平台界面：{base}/")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="TaxTestOps 平台数据初始化")
    parser.add_argument("--platform-url", default="http://127.0.0.1:8100", help="测试平台地址")
    args = parser.parse_args()
    return seed(args.platform_url)


if __name__ == "__main__":
    raise SystemExit(main())
