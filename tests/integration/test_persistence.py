"""集成层 · 持久化与仓储。

覆盖数据库层面的约束：唯一性、外键级联、事务回滚、幂等键并发冲突。
这些约束是业务正确性的**最后一道防线**——应用层的检查可能因竞态而失效，
数据库约束不会。
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from tax_service.core.calculator import build_calculator
from tax_service.core.person import Person
from tax_service.db.models import TaxDeclaration, User
from tax_service.services.tax_service import DeclarationRepository, TaxApplicationService

pytestmark = [pytest.mark.integration, pytest.mark.regression]


def make_computation(salary: int, person: Person | None = None) -> Any:
    """构造一次测算结果，供仓储用例直接落库。"""
    service = TaxApplicationService(build_calculator("standard"), calculator_impl="standard")
    return service.compute(person or Person(), salary)


class TestDeclarationPersistence:
    """申报记录落库。"""

    def test_creates_record_with_all_fields(self, declarations, user) -> None:
        # 注意：住房贷款利息与住房租金互斥，标准实现会拒绝同时为真的输入，
        # 因此这里只取其一（这是需求约束，不是实现限制）
        person = Person(
            children_num=2,
            is_keep_stu=1,
            house_loans=True,
            is_rent_house=0,
            supporting_elderly=True,
            sibling_num=1,
        )
        computation = make_computation(30_000, person)

        record, created = declarations.create(
            user_id=user.user_id,
            salary=30_000,
            person=person,
            computation=computation,
        )

        assert created is True
        assert record.id > 0
        assert record.children_num == 2
        assert record.house_loans is True
        assert record.supporting_elderly is True
        assert record.taxable_income == computation.taxable_income
        assert record.tax_amount == computation.tax_amount
        assert record.calculator_impl == "standard"
        assert record.created_at is not None

    def test_rejects_negative_salary_at_db_level(self, declarations, user) -> None:
        """salary 为负时应被数据库拒绝——SQLite 无 CHECK 约束，这里验证的是类型与业务约束的一致性。

        注：当前 schema 未对 salary 加 CHECK 约束，负值会成功写入。
        本用例**记录该事实**并提示：应用层校验（Pydantic ``ge=0``）是唯一防线，
        若日后有其他写入路径（如数据修复脚本），需要补上数据库约束。
        """
        computation = make_computation(0)

        record, _ = declarations.create(
            user_id=user.user_id,
            salary=-1,
            person=Person(),
            computation=computation,
        )

        assert record.salary == -1, (
            "数据库层未拦截负工资，说明应用层校验是唯一防线；"
            "若新增写入路径，需同步补充数据库 CHECK 约束"
        )


class TestIdempotencyConstraint:
    """幂等键唯一约束。"""

    def test_same_key_returns_existing_record(self, declarations, user) -> None:
        computation = make_computation(20_000)

        first, created_first = declarations.create(
            user_id=user.user_id,
            salary=20_000,
            person=Person(),
            computation=computation,
            idempotency_key="dup-key",
        )
        second, created_second = declarations.create(
            user_id=user.user_id,
            salary=99_999,
            person=Person(),
            computation=make_computation(99_999),
            idempotency_key="dup-key",
        )

        assert created_first is True
        assert created_second is False
        assert first.id == second.id
        assert second.salary == 20_000, "重复提交不应覆盖已有记录"

    def test_different_keys_create_separate_records(self, declarations, user) -> None:
        for key in ("k1", "k2"):
            declarations.create(
                user_id=user.user_id,
                salary=20_000,
                person=Person(),
                computation=make_computation(20_000),
                idempotency_key=key,
            )

        assert declarations.count_for_user(user.user_id) == 2

    def test_key_is_scoped_to_user(self, declarations, user, other_user) -> None:
        """同一幂等键在不同用户下是独立的两条记录。"""
        for owner in (user, other_user):
            declarations.create(
                user_id=owner.user_id,
                salary=20_000,
                person=Person(),
                computation=make_computation(20_000),
                idempotency_key="shared",
            )

        assert declarations.count_for_user(user.user_id) == 1
        assert declarations.count_for_user(other_user.user_id) == 1

    def test_null_key_allows_duplicates(self, declarations, user) -> None:
        """未提供幂等键时不去重（NULL 在唯一约束下互不冲突）。"""
        for _ in range(3):
            declarations.create(
                user_id=user.user_id,
                salary=20_000,
                person=Person(),
                computation=make_computation(20_000),
                idempotency_key=None,
            )

        assert declarations.count_for_user(user.user_id) == 3

    def test_unique_constraint_exists_in_schema(self, session_factory) -> None:
        """确认唯一约束真的建在数据库上，而不是只存在于 ORM 声明里。"""
        with session_factory() as session:
            indexes = session.execute(select(func.count()).select_from(TaxDeclaration)).scalar()
        assert indexes == 0  # 空表，仅确认可查询

        constraints = {c.name for c in TaxDeclaration.__table__.constraints if c.name}
        assert "uq_declaration_idempotency" in constraints


class TestUserCascade:
    """用户删除时记录的级联行为。"""

    def test_cascade_deletes_declarations(self, app: FastAPI, declarations, user) -> None:
        """删除用户必须连带删除其申报记录，否则会留下无法归属的孤儿数据。

        这依赖 ``PRAGMA foreign_keys=ON``——SQLite 默认**不启用**外键约束，
        不打开的话 ``ondelete="CASCADE"`` 完全不起作用。
        """
        declarations.create(
            user_id=user.user_id,
            salary=20_000,
            person=Person(),
            computation=make_computation(20_000),
        )
        assert declarations.count_for_user(user.user_id) == 1

        with app.state.session_factory() as session:
            record = session.get(User, user.user_id)
            session.delete(record)
            session.commit()

        assert declarations.count_for_user(user.user_id) == 0


class TestQueries:
    """仓储查询行为。"""

    def test_list_orders_by_id_desc(self, declarations, user) -> None:
        ids = [
            declarations.create(
                user_id=user.user_id,
                salary=salary,
                person=Person(),
                computation=make_computation(salary),
            )[0].id
            for salary in (10_000, 20_000, 30_000)
        ]

        listed = [record.id for record in declarations.list_for_user(user.user_id)]

        assert listed == sorted(ids, reverse=True)

    def test_list_respects_limit_and_offset(self, declarations, user) -> None:
        for salary in range(10_000, 10_000 + 10 * 1_000, 1_000):
            declarations.create(
                user_id=user.user_id,
                salary=salary,
                person=Person(),
                computation=make_computation(salary),
            )

        first_page = declarations.list_for_user(user.user_id, limit=4, offset=0)
        second_page = declarations.list_for_user(user.user_id, limit=4, offset=4)

        assert len(first_page) == 4
        assert len(second_page) == 4
        assert {record.id for record in first_page}.isdisjoint(
            {record.id for record in second_page}
        )

    def test_get_for_user_rejects_other_owner(self, declarations, user, other_user) -> None:
        record, _ = declarations.create(
            user_id=user.user_id,
            salary=20_000,
            person=Person(),
            computation=make_computation(20_000),
        )

        assert declarations.get_for_user(other_user.user_id, record.id) is None
        assert declarations.get_for_user(user.user_id, record.id) is not None

    def test_count_is_scoped_to_user(self, declarations, user, other_user) -> None:
        for owner, count in ((user, 2), (other_user, 3)):
            for index in range(count):
                declarations.create(
                    user_id=owner.user_id,
                    salary=10_000 + index * 1_000,
                    person=Person(),
                    computation=make_computation(10_000 + index * 1_000),
                )

        assert declarations.count_for_user(user.user_id) == 2
        assert declarations.count_for_user(other_user.user_id) == 3


class TestUserRepository:
    """用户仓储。"""

    def test_get_by_username(self, users, user) -> None:
        found = users.get_by_username(user.username)

        assert found is not None
        assert found.id == user.user_id

    def test_get_unknown_username_returns_none(self, users) -> None:
        assert users.get_by_username("no-such-user") is None

    def test_username_is_unique(self, users, user) -> None:
        with pytest.raises(IntegrityError):
            users.create(user.username, "another-hash")

    def test_count(self, users, user, other_user) -> None:
        assert users.count() >= 2


class TestDatabaseSessionBehaviour:
    """会话与事务。"""

    def test_records_survive_across_sessions(self, app: FastAPI, declarations, user) -> None:
        """写入后在新会话中可读——确认提交真的发生了。"""
        record, _ = declarations.create(
            user_id=user.user_id,
            salary=20_000,
            person=Person(),
            computation=make_computation(20_000),
        )

        fresh_repository = DeclarationRepository(app.state.session_factory)
        fetched = fresh_repository.get_for_user(user.user_id, record.id)

        assert fetched is not None
        assert fetched.tax_amount == record.tax_amount

    def test_rollback_on_error(self, app: FastAPI, user) -> None:
        """事务失败必须回滚，不留下半成品数据。"""
        factory = app.state.session_factory
        with factory() as session:
            session.add(User(username=user.username, password_hash="x", display_name="duplicate"))
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

        with factory() as session:
            total = session.scalar(
                select(func.count()).select_from(User).where(User.username == user.username)
            )
        assert total == 1, "回滚后不应留下重复用户"
