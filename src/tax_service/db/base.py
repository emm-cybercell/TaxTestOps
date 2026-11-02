"""数据库引擎与会话管理。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

SQLITE_BUSY_TIMEOUT_MS = 5_000
"""SQLite 写锁等待时间（毫秒）。

SQLite 默认行为是「拿不到写锁立刻报 database is locked」。并发写入场景下这会变成
随机失败——测试表现为 flaky，生产表现为偶发 500。显式设置等待时间是让并发行为
**可复现**的关键配置。
"""


class Base(DeclarativeBase):
    """ORM 基类。"""


def _configure_sqlite(engine: Engine) -> None:
    """为 SQLite 连接设置并发与约束相关参数。

    PRAGMA 语句不支持参数绑定，因此只能用字面量——三条语句内容在编译期固定，
    不接受任何外部输入。
    """

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_connection, _connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        try:
            # 遇到写锁时等待而不是立即失败（与 SQLITE_BUSY_TIMEOUT_MS 保持一致）
            cursor.execute("PRAGMA busy_timeout = 5000")
            # 写不阻塞读，显著降低并发下的锁冲突
            cursor.execute("PRAGMA journal_mode = WAL")
            # SQLite 默认**不启用**外键约束，必须显式打开，
            # 否则 ondelete="CASCADE" 之类的声明形同虚设
            cursor.execute("PRAGMA foreign_keys = ON")
        finally:
            cursor.close()


def is_sqlite_memory_url(url: str) -> bool:
    """判断是否为 SQLite 内存库连接串。

    需要覆盖 ``sqlite://``、``sqlite:///:memory:``、``sqlite+pysqlite:///:memory:``
    等多种写法，否则会退化成「每个连接一个独立空库」，表现为「建表成功但查不到表」。
    """
    return url.startswith("sqlite") and (
        ":memory:" in url or url.rstrip("/") in {"sqlite:", "sqlite"}
    )


def create_engine_from_url(url: str, *, echo: bool = False) -> Engine:
    """按 URL 创建引擎。

    SQLite 需要两处特殊处理：

    - ``check_same_thread=False``：FastAPI 的线程池与 SQLite 默认线程检查冲突；
    - 内存库必须用 ``StaticPool``，否则每个连接会拿到各自独立的空库。

    :param url: 数据库连接串。
    :param echo: 是否打印 SQL。
    """
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if is_sqlite_memory_url(url):
            # 内存库必须用 StaticPool，否则每个连接会拿到各自独立的空库。
            # 注意：StaticPool 会让所有线程共享同一个连接，因此**并发测试不能用内存库**，
            # 必须使用文件库（见 tests/conftest.py 的 file_db_app 夹具）。
            engine = create_engine(
                url,
                echo=echo,
                connect_args=connect_args,
                poolclass=StaticPool,
            )
        else:
            engine = create_engine(url, echo=echo, connect_args=connect_args)

        _configure_sqlite(engine)
        return engine

    return create_engine(url, echo=echo, pool_pre_ping=True, pool_recycle=1800)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """创建会话工厂。"""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_schema(engine: Engine) -> None:
    """建表（幂等）。

    生产环境应改用迁移工具（Alembic）；本项目为降低运行门槛直接建表，
    并在 ``docker-compose.yml`` 中说明了生产路径。
    """
    # 延迟导入是必需的：models 模块需要从本模块导入 Base，顶层导入会形成循环依赖。
    # 同时它的导入副作用（把模型注册进 Base.metadata）正是 create_all 生效的前提。
    from tax_service.db import models  # noqa: F401, PLC0415
    from tax_service.platform import models as platform_models  # noqa: F401, PLC0415

    Base.metadata.create_all(bind=engine)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """事务作用域：正常提交，异常回滚。"""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
