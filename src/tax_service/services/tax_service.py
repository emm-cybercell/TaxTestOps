"""个税应用服务：编排计算、明细与持久化。

职责边界
--------
- **计算**：委托给 :class:`~tax_service.core.calculator.Calculator`（由配置决定实现）；
- **明细**：使用需求口径的 :func:`~tax_service.core.deductions.build_deduction_breakdown`；
- **持久化**：委托给 :class:`DeclarationRepository`，并在仓储层保证幂等。

服务层不直接持有 HTTP 或 ORM 细节，便于在接口测试与集成测试之间复用。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tax_service.core.calculator import Calculator
from tax_service.core.deductions import DeductionItem, build_deduction_breakdown
from tax_service.core.person import Person
from tax_service.core.policy import BASIC_DEDUCTION
from tax_service.db.models import TaxDeclaration, User
from tax_service.external.policy_client import PolicyParameters, PolicyServiceClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaxComputation:
    """一次测算的完整结果。"""

    taxable_income: int
    tax_amount: int
    calculator_impl: str
    deductions: tuple[DeductionItem, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "taxable_income": self.taxable_income,
            "tax_amount": self.tax_amount,
            "calculator_impl": self.calculator_impl,
            "deductions": [item.to_dict() for item in self.deductions],
        }


class TaxApplicationService:
    """个税测算的应用服务。"""

    def __init__(
        self,
        calculator: Calculator,
        *,
        calculator_impl: str = "legacy",
        policy_client: PolicyServiceClient | None = None,
        basic_deduction: int | None = None,
    ) -> None:
        self.calculator = calculator
        self.calculator_impl = calculator_impl
        self.policy_client = policy_client
        self._basic_deduction = basic_deduction

    # ------------------------------------------------------------------ #
    # 计算
    # ------------------------------------------------------------------ #

    def compute(self, person: Person, salary: int) -> TaxComputation:
        """计算税额并生成扣除明细。

        .. note::
           ``taxable_income`` 采用**需求口径**（见 ``core.deductions``），
           ``tax_amount`` 来自当前配置的计算器实现。两者在 ``legacy`` 模式下
           可能不自洽——这正是缺陷 D1 / D4 在接口层的可观测表现，由接口测试断言。
        """
        deductions = build_deduction_breakdown(person)
        taxable_income = (
            salary - self._resolved_basic_deduction() - sum(item.amount for item in deductions)
        )
        tax_amount = self.calculator.cal(person, salary)

        return TaxComputation(
            taxable_income=taxable_income,
            tax_amount=tax_amount,
            calculator_impl=self.calculator_impl,
            deductions=deductions,
        )

    def policy_parameters(self) -> PolicyParameters | None:
        """获取政策参数快照（未配置政策服务时返回 ``None``）。"""
        if self.policy_client is None:
            return None
        return self.policy_client.fetch_parameters()

    def _resolved_basic_deduction(self) -> int:
        return BASIC_DEDUCTION if self._basic_deduction is None else self._basic_deduction


class DeclarationRepository:
    """申报记录的仓储。

    幂等语义：``(user_id, idempotency_key)`` 唯一。重复提交同一键时返回已存在记录，
    ``created`` 为 ``False``；并发场景下依赖数据库唯一约束兜底。
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(
        self,
        *,
        user_id: int,
        salary: int,
        person: Person,
        computation: TaxComputation,
        idempotency_key: str | None = None,
        source: str = "api",
    ) -> tuple[TaxDeclaration, bool]:
        """落库一条申报记录，返回 ``(记录, 是否新建)``。"""
        if idempotency_key is not None:
            existing = self.find_by_idempotency_key(user_id, idempotency_key)
            if existing is not None:
                return existing, False

        session = self._session_factory()
        try:
            record = TaxDeclaration(
                user_id=user_id,
                salary=salary,
                children_num=person.children_num,
                is_keep_stu=person.is_keep_stu,
                house_loans=person.house_loans,
                is_rent_house=person.is_rent_house,
                supporting_elderly=person.supporting_elderly,
                sibling_num=person.sibling_num,
                taxable_income=computation.taxable_income,
                tax_amount=computation.tax_amount,
                calculator_impl=computation.calculator_impl,
                idempotency_key=idempotency_key,
                source=source,
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            return record, True
        except IntegrityError:
            # 并发提交同一幂等键：唯一约束拦下后回查已存在记录
            session.rollback()
            logger.info("幂等键并发冲突，回查已存在记录：%s", idempotency_key)
            existing = self.find_by_idempotency_key(user_id, idempotency_key)
            if existing is None:  # pragma: no cover - 极窄竞态窗口
                raise
            return existing, False
        finally:
            session.close()

    def find_by_idempotency_key(self, user_id: int, key: str) -> TaxDeclaration | None:
        with self._session_factory() as session:
            statement = select(TaxDeclaration).where(
                TaxDeclaration.user_id == user_id,
                TaxDeclaration.idempotency_key == key,
            )
            return session.scalars(statement).first()

    def get_for_user(self, user_id: int, record_id: int) -> TaxDeclaration | None:
        with self._session_factory() as session:
            statement = select(TaxDeclaration).where(
                TaxDeclaration.id == record_id,
                TaxDeclaration.user_id == user_id,
            )
            return session.scalars(statement).first()

    def list_for_user(
        self,
        user_id: int,
        *,
        limit: int = 20,
        offset: int = 0,
    ) -> list[TaxDeclaration]:
        with self._session_factory() as session:
            statement = (
                select(TaxDeclaration)
                .where(TaxDeclaration.user_id == user_id)
                .order_by(TaxDeclaration.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return list(session.scalars(statement))

    def count_for_user(self, user_id: int) -> int:
        with self._session_factory() as session:
            statement = (
                select(func.count())
                .select_from(TaxDeclaration)
                .where(TaxDeclaration.user_id == user_id)
            )
            return int(session.scalar(statement) or 0)


class UserRepository:
    """用户仓储。"""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def get_by_username(self, username: str) -> User | None:
        with self._session_factory() as session:
            statement = select(User).where(User.username == username)
            return session.scalars(statement).first()

    def create(self, username: str, password_hash: str, display_name: str = "") -> User:
        session = self._session_factory()
        try:
            user = User(
                username=username,
                password_hash=password_hash,
                display_name=display_name or username,
            )
            session.add(user)
            session.commit()
            session.refresh(user)
            return user
        finally:
            session.close()

    def count(self) -> int:
        with self._session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(User)) or 0)
