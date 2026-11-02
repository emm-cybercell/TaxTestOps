"""集成层 · 基础设施与配置。

本文件补齐质量门禁暴露出的覆盖缺口。这些代码不在业务主路径上，但都是**异常与边界
分支**——恰恰是最容易出错、又最难被发现的部分：

- 配置的默认值与环境变量解析；
- 计算器工厂的非法输入处理；
- 非 SQLite 数据库的引擎参数（生产环境走这条路）；
- 事务作用域的提交/回滚语义；
- Redis 缓存实现（默认不启用，因此最容易被遗漏）。

Redis 用替身对象测试：``RedisCache`` 的价值在于**序列化与键前缀逻辑**，
而这些不需要真实 Redis 就能验证；引入真实 Redis 只会让测试依赖外部服务。

数据库断言尽量走 ORM 模型与功能行为（例如用外键违约证明约束已启用），
而不是直接读 PRAGMA——功能断言比配置断言更接近真实故障场景。
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest
from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tax_service.config import (
    Settings,
    env_flag,
    get_settings,
    reset_settings_cache,
)
from tax_service.core.calculator import build_calculator, expected_calculator
from tax_service.core.legacy import IncomeTaxCalculator
from tax_service.core.oracle import TaxOracle
from tax_service.core.person import Person
from tax_service.db.base import (
    SQLITE_BUSY_TIMEOUT_MS,
    Base,
    create_engine_from_url,
    create_session_factory,
    is_sqlite_memory_url,
    session_scope,
)
from tax_service.infra.cache import InMemoryCache, RedisCache, build_cache

pytestmark = [pytest.mark.integration]


# --------------------------------------------------------------------------- #
# 测试专用 ORM 模型（避免在测试里手写 DDL/DML 字符串）
# --------------------------------------------------------------------------- #


class DemoParent(Base):
    """事务与约束测试用的父表。"""

    __tablename__ = "demo_parent"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    children: Mapped[list[DemoChild]] = relationship(
        back_populates="parent", cascade="all, delete-orphan"
    )


class DemoChild(Base):
    """事务与约束测试用的子表。"""

    __tablename__ = "demo_child"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    parent_id: Mapped[int] = mapped_column(ForeignKey("demo_parent.id", ondelete="CASCADE"))
    parent: Mapped[DemoParent] = relationship(back_populates="children")


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


class TestSettings:
    """配置解析与派生属性。"""

    def test_defaults_are_dependency_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """默认配置必须不依赖任何外部服务——保证新克隆的仓库开箱即跑。

        必须清掉 ``TAX_`` 前缀的环境变量：standard 模式的 CI 任务会设置
        ``TAX_CALCULATOR_IMPL=standard``，不隔离的话这里读到的就是环境值而非默认值。
        """
        monkeypatch.delenv("TAX_CALCULATOR_IMPL", raising=False)

        settings = Settings()

        assert settings.database_url.startswith("sqlite")
        assert settings.cache_backend == "memory"
        assert settings.calculator_impl == "legacy"

    def test_is_sqlite_flag(self) -> None:

        assert Settings(database_url="sqlite://").is_sqlite is True
        assert Settings(database_url="mysql+pymysql://u:p@host/db").is_sqlite is False

    def test_is_legacy_calculator_flag(self) -> None:

        assert Settings(calculator_impl="legacy").is_legacy_calculator is True
        assert Settings(calculator_impl="standard").is_legacy_calculator is False

    def test_reads_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``TAX_`` 前缀的环境变量必须能覆盖默认值。"""

        monkeypatch.setenv("TAX_CALCULATOR_IMPL", "standard")
        monkeypatch.setenv("TAX_BATCH_MAX_SIZE", "7")

        settings = Settings()

        assert settings.calculator_impl == "standard"
        assert settings.batch_max_size == 7

    def test_ignores_unknown_environment_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """无关的环境变量不应导致启动失败（``extra="ignore"``）。"""

        monkeypatch.setenv("TAX_NOT_A_REAL_SETTING", "whatever")

        assert Settings().app_name == "TaxTestOps"

    def test_get_settings_is_cached(self) -> None:
        """配置单例必须缓存，避免每次请求都重新解析环境变量。"""

        reset_settings_cache()
        assert get_settings() is get_settings()

    def test_reset_settings_cache_picks_up_new_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """清缓存后应读到新的环境变量——测试依赖这个能力切换运行模式。"""

        reset_settings_cache()
        monkeypatch.setenv("TAX_ENVIRONMENT", "production")
        assert get_settings().environment == "production"

        reset_settings_cache()
        monkeypatch.setenv("TAX_ENVIRONMENT", "test")
        assert get_settings().environment == "test"

        reset_settings_cache()


class TestEnvFlag:
    """布尔型环境变量解析。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            pytest.param("1", True, id="1"),
            pytest.param("true", True, id="true"),
            pytest.param("TRUE", True, id="大写 TRUE"),
            pytest.param("  yes  ", True, id="带空格的 yes"),
            pytest.param("on", True, id="on"),
            pytest.param("0", False, id="0"),
            pytest.param("false", False, id="false"),
            pytest.param("no", False, id="no"),
            pytest.param("", False, id="空字符串"),
            pytest.param("maybe", False, id="无法识别的值"),
        ],
    )
    def test_parses_common_truthy_values(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
    ) -> None:

        monkeypatch.setenv("TAX_TEST_FLAG", raw)

        assert env_flag("TAX_TEST_FLAG") is expected

    def test_returns_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:

        monkeypatch.delenv("TAX_TEST_FLAG", raising=False)

        assert env_flag("TAX_TEST_FLAG") is False
        assert env_flag("TAX_TEST_FLAG", default=True) is True


# --------------------------------------------------------------------------- #
# 计算器工厂
# --------------------------------------------------------------------------- #


class TestCalculatorFactory:
    """计算器工厂的选择与错误处理。"""

    def test_builds_legacy(self) -> None:
        assert isinstance(build_calculator("legacy"), IncomeTaxCalculator)

    def test_builds_standard(self) -> None:
        assert isinstance(build_calculator("standard"), TaxOracle)

    def test_rejects_unknown_implementation(self) -> None:
        """未知实现必须显式报错，而不是悄悄回退到某个默认实现。

        静默回退会让配置写错的环境「看起来正常」，但算出的是另一套口径的税额——
        这类问题在生产上极难定位。
        """
        with pytest.raises(ValueError, match="未知的个税计算实现"):
            build_calculator("quantum")

    def test_expected_calculator_is_oracle(self) -> None:
        """``expected_calculator`` 用于诊断，必须始终返回需求口径的实现。"""
        calculator = expected_calculator()

        assert isinstance(calculator, TaxOracle)
        assert calculator.cal(Person(), 20_000) == 1_590

    def test_both_implementations_satisfy_protocol(self) -> None:
        """两个实现都必须可被替换使用——这是分层测试能跑两遍的前提。"""
        for impl in ("legacy", "standard"):
            calculator = build_calculator(impl)
            assert callable(calculator.cal)
            assert isinstance(calculator.cal(Person(), 20_000), int)


# --------------------------------------------------------------------------- #
# 数据库引擎
# --------------------------------------------------------------------------- #


class TestEngineFactory:
    """引擎创建与 SQLite 参数。"""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            pytest.param("sqlite://", True, id="sqlite://"),
            pytest.param("sqlite:///:memory:", True, id="显式内存库"),
            pytest.param("sqlite:///./var/app.db", False, id="文件库"),
            pytest.param("mysql+pymysql://u:p@host/db", False, id="MySQL 不算内存库"),
        ],
    )
    def test_memory_url_detection(self, url: str, expected: bool) -> None:
        """内存库判定必须覆盖多种写法。

        漏判会让内存库退化成「每个连接一个独立空库」，表现为「建表成功但查不到表」，
        而且只在多线程下才暴露。
        """
        assert is_sqlite_memory_url(url) is expected

    def test_foreign_keys_are_enforced(self) -> None:
        """外键约束必须真正生效——用**功能行为**验证，而不是读配置项。

        SQLite 默认不启用外键，若忘记打开，``ondelete="CASCADE"`` 会静默失效，
        删除父记录后留下无法归属的孤儿数据。这里插入一个不存在的父 ID，
        期望数据库直接拒绝。

        这条用例同时证明了 ``_configure_sqlite`` 注册的 connect 监听器**确实执行了**：
        外键开关正是由该监听器设置的，约束生效即监听器生效。
        """
        engine = create_engine_from_url("sqlite://")
        factory = create_session_factory(engine)
        try:
            Base.metadata.create_all(bind=engine)

            with pytest.raises(IntegrityError):
                with factory() as session:
                    session.add(DemoChild(parent_id=999_999))
                    session.commit()
        finally:
            engine.dispose()

    def test_busy_timeout_constant_is_set(self) -> None:
        """写锁等待时间必须有明确取值。

        默认行为是「拿不到锁立刻报 database is locked」，在并发下表现为随机失败；
        测试套件里就是 flaky，生产上就是偶发 500。该常量由 connect 监听器应用到
        每个连接，监听器的实际生效已由 ``test_foreign_keys_are_enforced`` 证明
        （两者出自同一个监听器）。
        """

        assert SQLITE_BUSY_TIMEOUT_MS >= 1_000, "等待时间过短，并发下仍会随机失败"

    def test_non_sqlite_engine_uses_production_pool_settings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """非 SQLite 引擎必须开启连接健康检查与回收。

        长连接被中间件或数据库静默断开后，``pool_pre_ping`` 能让下一次取连接时
        先探活，避免把「连接已死」暴露成业务 500。
        """
        captured: dict[str, Any] = {}

        def fake_create_engine(url: str, **kwargs: Any) -> Any:
            captured["url"] = url
            captured["kwargs"] = kwargs
            return object()

        monkeypatch.setattr("tax_service.db.base.create_engine", fake_create_engine)

        create_engine_from_url("mysql+pymysql://user@host/db", echo=True)

        assert captured["url"] == "mysql+pymysql://user@host/db"
        assert captured["kwargs"]["echo"] is True
        assert captured["kwargs"]["pool_pre_ping"] is True
        assert captured["kwargs"]["pool_recycle"] == 1800
        # 不应给非 SQLite 引擎传 SQLite 专有参数
        assert "connect_args" not in captured["kwargs"]

    def test_sqlite_file_engine_is_usable(self, tmp_path) -> None:
        """文件库必须可正常建连并读写（线程检查已关闭）。"""
        engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'x.db').as_posix()}")
        factory = create_session_factory(engine)
        try:
            Base.metadata.create_all(bind=engine)

            with session_scope(factory) as session:
                session.add(DemoParent(name="file-backed"))

            with factory() as session:
                assert session.query(DemoParent).count() == 1
        finally:
            engine.dispose()


class TestSessionScope:
    """事务作用域语义。"""

    def test_commits_on_success(self) -> None:
        engine = create_engine_from_url("sqlite://")
        factory = create_session_factory(engine)
        try:
            Base.metadata.create_all(bind=engine)

            with session_scope(factory) as session:
                session.add(DemoParent(name="committed"))

            with factory() as session:
                names = [row.name for row in session.query(DemoParent).all()]
            assert names == ["committed"]
        finally:
            engine.dispose()

    def test_rolls_back_on_error(self) -> None:
        """异常必须触发回滚，不留下半成品数据。"""
        engine = create_engine_from_url("sqlite://")
        factory = create_session_factory(engine)
        try:
            Base.metadata.create_all(bind=engine)
            with factory() as session:
                session.add(DemoParent(name="existing"))
                session.commit()

            with pytest.raises(IntegrityError):
                with session_scope(factory) as session:
                    session.add(DemoParent(name="rollback-me"))
                    session.flush()
                    # 唯一约束冲突：整个作用域必须回滚
                    session.add(DemoParent(name="existing"))

            with factory() as session:
                names = sorted(row.name for row in session.query(DemoParent).all())
            assert names == ["existing"], "回滚后不应留下部分写入"
        finally:
            engine.dispose()

    def test_closes_session_even_on_error(self) -> None:
        """无论成功或失败，会话都必须关闭，否则连接池会被耗尽。"""
        engine = create_engine_from_url("sqlite://")
        factory = create_session_factory(engine)
        try:
            Base.metadata.create_all(bind=engine)

            with pytest.raises(RuntimeError):
                with session_scope(factory):
                    raise RuntimeError("业务异常")

            # 连接池未泄漏：还能正常取到新会话并执行查询
            with session_scope(factory) as session:
                assert session.query(DemoParent).count() == 0
        finally:
            engine.dispose()


# --------------------------------------------------------------------------- #
# Redis 缓存
# --------------------------------------------------------------------------- #


class FakeRedisClient:
    """极简 Redis 替身：只实现 ``RedisCache`` 用到的方法。"""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.url = ""
        self.options: dict[str, Any] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def setex(self, key: str, ttl: int, value: str) -> None:
        self.store[key] = value
        self.ttls[key] = ttl

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                removed += 1
            self.ttls.pop(key, None)
        return removed

    def keys(self, pattern: str) -> list[str]:
        prefix = pattern.rstrip("*")
        return [key for key in self.store if key.startswith(prefix)]


class FakeRedisFactory:
    """``redis.Redis`` 的替身入口。

    ``RedisCache`` 内部走 ``redis.Redis.from_url(...)``。若直接把它做成类方法，
    每次调用都会新建实例，测试就**看不到**缓存实际写入的数据。
    这个工厂把 ``from_url`` 固定到同一个实例上，让断言能检查内部状态。
    """

    def __init__(self) -> None:
        self.client = FakeRedisClient()

    def from_url(self, url: str, **kwargs: Any) -> FakeRedisClient:
        self.client.url = url
        self.client.options = kwargs
        return self.client


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedisClient:
    """把 ``redis`` 模块替换为替身，让 RedisCache 可被无依赖地测试。"""
    factory = FakeRedisFactory()
    monkeypatch.setitem(sys.modules, "redis", types.SimpleNamespace(Redis=factory))
    return factory.client


class TestRedisCache:
    """Redis 缓存实现的序列化与键管理。"""

    def test_stores_json_serializable_values(self, fake_redis: FakeRedisClient) -> None:
        cache = RedisCache("redis://localhost:6379/0")

        cache.set("policy", {"version": "2026-01", "value": 5_000})

        stored = fake_redis.store["taxtestops:policy"]
        assert json.loads(stored) == {"version": "2026-01", "value": 5_000}

    def test_round_trips_non_ascii(self, fake_redis: FakeRedisClient) -> None:
        """中文必须可读且可还原——``ensure_ascii=False`` 的实际效果。"""
        cache = RedisCache("redis://localhost:6379/0")

        cache.set("label", "子女教育")

        assert "子女教育" in fake_redis.store["taxtestops:label"]
        assert cache.get("label") == "子女教育"

    def test_get_returns_none_on_miss(self, fake_redis: FakeRedisClient) -> None:
        cache = RedisCache("redis://localhost:6379/0")

        assert cache.get("absent") is None

    def test_applies_ttl(self, fake_redis: FakeRedisClient) -> None:
        """带 TTL 的写入必须走 ``setex``，否则缓存永不失效。"""
        cache = RedisCache("redis://localhost:6379/0")

        cache.set("temp", {"a": 1}, ttl_seconds=120)

        assert fake_redis.ttls["taxtestops:temp"] == 120

    def test_without_ttl_uses_plain_set(self, fake_redis: FakeRedisClient) -> None:
        cache = RedisCache("redis://localhost:6379/0")

        cache.set("permanent", {"a": 1}, ttl_seconds=None)

        assert "taxtestops:permanent" not in fake_redis.ttls

    def test_key_prefix_isolates_namespaces(self, fake_redis: FakeRedisClient) -> None:
        """键前缀避免与其他应用共享 Redis 实例时互相覆盖。"""
        cache = RedisCache("redis://localhost:6379/0", prefix="other:")

        cache.set("k", 1)

        assert "other:k" in fake_redis.store
        assert "taxtestops:k" not in fake_redis.store

    def test_delete(self, fake_redis: FakeRedisClient) -> None:
        cache = RedisCache("redis://localhost:6379/0")
        cache.set("k", 1)

        assert cache.delete("k") is True
        assert cache.delete("k") is False

    def test_clear_removes_only_own_namespace(self, fake_redis: FakeRedisClient) -> None:
        """清空只能清自己的键，不能误删同实例下其他应用的数据。"""
        fake_redis.store["foreign:key"] = "keep-me"
        cache = RedisCache("redis://localhost:6379/0")
        cache.set("a", 1)
        cache.set("b", 2)

        cache.clear()

        assert fake_redis.store == {"foreign:key": "keep-me"}

    def test_statistics(self, fake_redis: FakeRedisClient) -> None:
        cache = RedisCache("redis://localhost:6379/0")
        cache.set("hit", 1)

        cache.get("hit")
        cache.get("hit")
        cache.get("miss")

        stats = cache.stats()
        assert stats["hits"] == 2
        assert stats["misses"] == 1
        assert stats["entries"] == 1
        assert stats["hit_rate_percent"] == 67

    def test_clear_resets_statistics(self, fake_redis: FakeRedisClient) -> None:
        cache = RedisCache("redis://localhost:6379/0")
        cache.set("k", 1)
        cache.get("k")

        cache.clear()

        assert cache.stats()["hits"] == 0

    def test_empty_store_statistics(self, fake_redis: FakeRedisClient) -> None:
        cache = RedisCache("redis://localhost:6379/0")

        assert cache.stats()["hit_rate_percent"] == 0

    def test_build_cache_selects_redis(self, fake_redis: FakeRedisClient) -> None:
        """工厂必须能构造 Redis 后端，并在多副本部署时被选中。"""
        cache = build_cache("redis", redis_url="redis://localhost:6379/0")

        assert isinstance(cache, RedisCache)


class TestCacheBackendParity:
    """两种缓存后端必须行为一致。"""

    @pytest.mark.parametrize("backend", ["memory", "redis"])
    def test_same_contract(self, backend: str, fake_redis: FakeRedisClient) -> None:
        """同一套断言必须对两种后端都成立。

        缓存是「可替换基础设施」，如果两种实现语义不同，切换后端就会引入
        难以察觉的行为变化（例如内存后端不序列化、Redis 后端序列化，
        传入不可序列化对象时只有后者会炸）。
        """
        cache = build_cache(backend, redis_url="redis://localhost:6379/0")

        cache.set("k", {"nested": [1, 2, 3]}, ttl_seconds=60)
        assert cache.get("k") == {"nested": [1, 2, 3]}
        assert cache.delete("k") is True
        assert cache.get("k") is None

        cache.set("x", "y")
        cache.clear()
        assert cache.get("x") is None

    def test_memory_backend_is_default(self) -> None:
        assert isinstance(build_cache("memory"), InMemoryCache)
