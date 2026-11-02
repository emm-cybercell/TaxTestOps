"""运行时配置。

所有配置项均可通过环境变量覆盖，前缀为 ``TAX_``，例如：

- ``TAX_DATABASE_URL``
- ``TAX_CACHE_BACKEND``
- ``TAX_CALCULATOR_IMPL``

**测试友好性设计**：默认值刻意选择「零外部依赖」的组合——SQLite 文件库 +
内存缓存。这样 `pytest` 在没有 MySQL / Redis 的机器上也能全量跑通，
而生产部署只需改环境变量即可切到 MySQL + Redis。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CalculatorImpl = Literal["legacy", "standard"]
"""个税计算实现的选择。

- ``legacy``：实验给定的原实现（保留全部缺陷）——**默认值**，用于缺陷复现与测试。
- ``standard``：按需求规格修正后的实现——用于验证「修复后测试全部转绿」。
"""

CacheBackend = Literal["memory", "redis"]


class Settings(BaseSettings):
    """应用配置。"""

    model_config = SettingsConfigDict(
        env_prefix="TAX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # 基础
    # ------------------------------------------------------------------ #
    app_name: str = "TaxTestOps"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False

    # ------------------------------------------------------------------ #
    # 个税计算实现
    # ------------------------------------------------------------------ #
    calculator_impl: CalculatorImpl = "legacy"

    # ------------------------------------------------------------------ #
    # 数据库
    # ------------------------------------------------------------------ #
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'var' / 'tax_testops.db').as_posix()}"
    db_echo: bool = False

    # ------------------------------------------------------------------ #
    # 缓存
    # ------------------------------------------------------------------ #
    cache_backend: CacheBackend = "memory"
    redis_url: str = "redis://localhost:6379/0"
    policy_cache_ttl_seconds: int = 300

    # ------------------------------------------------------------------ #
    # 认证
    # ------------------------------------------------------------------ #
    jwt_secret: str = "dev-only-secret-change-me"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 60
    bcrypt_rounds: int = 12
    """密码哈希轮数。测试环境建议降到 4，可把接口测试套件从 20 秒级压到秒级。"""

    # ------------------------------------------------------------------ #
    # 限流
    # ------------------------------------------------------------------ #
    rate_limit_per_minute: int = 120

    # ------------------------------------------------------------------ #
    # 批量申报
    # ------------------------------------------------------------------ #
    batch_max_size: int = 500

    # ------------------------------------------------------------------ #
    # 外部政策服务
    # ------------------------------------------------------------------ #
    policy_service_url: str = "https://policy.example.gov.cn/api/v1/parameters"
    policy_service_timeout_seconds: float = 3.0

    # ------------------------------------------------------------------ #
    # 测试平台
    # ------------------------------------------------------------------ #
    platform_self_base_url: str = "http://127.0.0.1:8000"
    """平台的默认被测目标（未显式配置 ``platform_target_base_url`` 时使用）。"""

    platform_target_base_url: str | None = None
    """被测服务地址；为空则回落到 ``platform_self_base_url``。"""

    platform_target_token: str | None = None
    """调用需要鉴权的被测接口时使用的令牌。为空时相关用例会因 401 而失败——
    这是**有意的**：平台不应假装「鉴权用例通过」。"""

    platform_max_workers: int = 4
    """平台执行并发度。"""

    platform_schedule_enabled: bool = False
    """是否开启定时执行。默认关闭，避免测试环境里意外产生后台流量。"""

    platform_schedule_interval_seconds: int = 300
    platform_schedule_tag: str | None = None

    # ------------------------------------------------------------------ #
    # AI 增强
    # ------------------------------------------------------------------ #
    ai_enabled: bool = False
    """是否启用模型能力。默认关闭——AI 是**可选增强**，不是运行前提。"""

    ai_api_key: str | None = None
    """模型 API 密钥。为空时自动回落到离线规则提供者，本地与 CI 无需任何准备。"""

    ai_base_url: str = "https://api.openai.com"
    """OpenAI 兼容端点。可指向 DeepSeek / 通义 / 本地 vLLM 等。"""

    ai_model: str = "gpt-4o-mini"
    ai_timeout_seconds: float = 30.0

    # ------------------------------------------------------------------ #
    # 静态资源
    # ------------------------------------------------------------------ #
    web_dir: Path = Field(default=PROJECT_ROOT / "web")

    # ------------------------------------------------------------------ #
    # 便捷属性
    # ------------------------------------------------------------------ #
    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_legacy_calculator(self) -> bool:
        """当前是否运行在「保留缺陷」模式。

        测试用例据此决定是否把需求符合性断言标记为 ``xfail``——
        切到 ``standard`` 后，这些用例必须真正通过。
        """
        return self.calculator_impl == "legacy"


@lru_cache
def get_settings() -> Settings:
    """进程内缓存的配置单例。"""
    return Settings()


def reset_settings_cache() -> None:
    """清除配置缓存（供测试在修改环境变量后调用）。"""
    get_settings.cache_clear()


def env_flag(name: str, default: bool = False) -> bool:
    """读取布尔型环境变量。"""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
