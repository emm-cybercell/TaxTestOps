"""冒烟脚本：构造应用并跑一遍主要接口，用于开发期快速验证。

演示账号凭据从环境变量读取；未提供时**随机生成并打印**，避免把凭据写进源码。

用法::

    .venv/Scripts/python.exe scripts/smoke.py [--impl standard]

    # 或指定凭据
    TAX_DEMO_USERNAME=demo TAX_DEMO_PASSWORD=... .venv/Scripts/python.exe scripts/smoke.py
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from tax_service.api.app import create_app  # noqa: E402
from tax_service.api.security import hash_password  # noqa: E402
from tax_service.config import Settings  # noqa: E402

USERNAME_ENV = "TAX_DEMO_USERNAME"
PASSWORD_ENV = "TAX_DEMO_PASSWORD"


def resolve_credentials() -> tuple[str, str, bool]:
    """返回 ``(用户名, 密码, 是否为随机生成)``。"""
    username = os.getenv(USERNAME_ENV, "demo")
    password = os.getenv(PASSWORD_ENV)
    if password:
        return username, password, False
    return username, secrets.token_urlsafe(12), True


def build_app(calculator_impl: str):
    """构造一个使用内存库、不限流的应用实例。"""
    settings = Settings(
        environment="test",
        database_url="sqlite://",
        calculator_impl=calculator_impl,  # type: ignore[arg-type]
        rate_limit_per_minute=0,
        jwt_secret=secrets.token_urlsafe(32),
    )
    return create_app(settings)


def pretty(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="TaxTestOps 冒烟检查")
    parser.add_argument("--impl", default="legacy", choices=["legacy", "standard"])
    args = parser.parse_args()

    username, password, generated = resolve_credentials()
    app = build_app(args.impl)

    with TestClient(app) as client:
        app.state.user_repository.create(username, hash_password(password), "演示用户")
        if generated:
            print(f"[i] 未提供 {PASSWORD_ENV}，已生成随机演示密码：{password}\n")

        print("== /healthz ==")
        health = client.get("/healthz")
        print(health.status_code, pretty(health.json()))

        print("\n== 登录 ==")
        login = client.post("/api/v1/auth/login", json={"username": username, "password": password})
        print(login.status_code, pretty(login.json())[:160])
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        print("\n== 单次测算 ==")
        calc = client.post(
            "/api/v1/tax/calculate",
            headers=headers,
            json={
                "salary": 20000,
                "person": {
                    "children_num": 2,
                    "is_keep_stu": 1,
                    "house_loans": True,
                    "supporting_elderly": True,
                    "sibling_num": 1,
                },
                "idempotency_key": "smoke-1",
            },
        )
        print(calc.status_code, pretty(calc.json()))

        print("\n== 幂等重放（同 key，应返回同一 record_id） ==")
        replay = client.post(
            "/api/v1/tax/calculate",
            headers=headers,
            json={"salary": 99999, "idempotency_key": "smoke-1"},
        )
        print(
            replay.status_code,
            "record_id =",
            replay.json()["record_id"],
            "| tax_amount =",
            replay.json()["tax_amount"],
        )

        print("\n== 批量申报（含批内重复 ref） ==")
        batch = client.post(
            "/api/v1/tax/batch",
            headers=headers,
            json={
                "items": [
                    {"salary": 10000, "external_ref": "A001"},
                    {"salary": 30000, "external_ref": "A002"},
                    {"salary": 30000, "external_ref": "A002"},
                ]
            },
        )
        print(batch.status_code, pretty(batch.json())[:420])

        print("\n== 历史记录 ==")
        page = client.get("/api/v1/tax/records?limit=5", headers=headers)
        print(page.status_code, "total =", page.json()["total"])

        print("\n== 参数校验（非法枚举 is_keep_stu=9） ==")
        invalid = client.post(
            "/api/v1/tax/calculate",
            headers=headers,
            json={"salary": 20000, "person": {"is_keep_stu": 9}},
        )
        print(invalid.status_code, pretty(invalid.json())[:260])

        print("\n== 未认证访问 ==")
        print("records ->", client.get("/api/v1/tax/records").status_code)

        print("\n== 前端页面 ==")
        home = client.get("/")
        print(home.status_code, "content-type =", home.headers.get("content-type"))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
