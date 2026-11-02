"""ORM 模型。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from tax_service.db.base import Base


def utcnow() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(UTC)


class User(Base):
    """申报用户。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    password_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    declarations: Mapped[list[TaxDeclaration]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<User id={self.id} username={self.username!r}>"


class TaxDeclaration(Base):
    """一次个税测算记录。

    保存原始输入与计算结果，用于历史查询、对账与回归比对。
    """

    __tablename__ = "tax_declarations"
    __table_args__ = (
        # 幂等键在用户维度唯一：同一用户重复提交同一键只落库一次
        UniqueConstraint("user_id", "idempotency_key", name="uq_declaration_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # 输入
    salary: Mapped[int] = mapped_column(Integer, nullable=False)
    children_num: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_keep_stu: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    house_loans: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_rent_house: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    supporting_elderly: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sibling_num: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # 输出
    taxable_income: Mapped[int] = mapped_column(Integer, nullable=False)
    tax_amount: Mapped[int] = mapped_column(Integer, nullable=False)

    # 元数据
    calculator_impl: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="api")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, index=True
    )

    user: Mapped[User] = relationship(back_populates="declarations")

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return f"<TaxDeclaration id={self.id} tax={self.tax_amount}>"
