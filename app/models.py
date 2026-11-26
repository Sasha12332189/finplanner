from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    first_name: Mapped[str] = mapped_column(String(128), default="friend")
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="UAH")
    language: Mapped[str] = mapped_column(String(8), default="uk")
    theme: Mapped[str] = mapped_column(String(16), default="system")
    monthly_budget: Mapped[Decimal | None] = mapped_column(Numeric(16, 2), nullable=True)
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    investment_capital: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    # "YYYY-MM" of the last month whose summary the bot has sent.
    last_summary_month: Mapped[str | None] = mapped_column(String(7), nullable=True)
    # Paid plan: "pro" or "premium" while plan_expires_at is in the future, otherwise free.
    plan: Mapped[str | None] = mapped_column(String(16), nullable=True)
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Telegram Stars subscription charge id, needed to cancel or resume auto-renewal.
    plan_charge_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    plan_canceled: Mapped[bool | None] = mapped_column(default=False, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Payment(Base):
    """Every successful Telegram Stars payment, including monthly renewals."""
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    plan: Mapped[str] = mapped_column(String(16))
    stars: Mapped[int] = mapped_column(Integer)
    telegram_charge_id: Mapped[str] = mapped_column(String(255), unique=True)
    is_recurring: Mapped[bool] = mapped_column(default=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refunded: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Transaction(Base):
    """A money movement. kind: expense | income | transfer_out | transfer_in.

    account_id is None for the main account (the user's opening_balance).
    """
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    category: Mapped[str] = mapped_column(String(64), default="Other")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Account(Base):
    """An extra wallet (cash, savings, a second card). The main account is implicit."""
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(60))
    opening_balance: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CategoryBudget(Base):
    __tablename__ = "category_budgets"
    __table_args__ = (UniqueConstraint("user_id", "category", name="uq_category_budget"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    category: Mapped[str] = mapped_column(String(64))
    limit_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2))


class RecurringPayment(Base):
    """Rent, subscriptions, salary: added automatically once a month on day_of_month."""
    __tablename__ = "recurring_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    amount: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    category: Mapped[str] = mapped_column(String(64), default="Other")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    day_of_month: Mapped[int] = mapped_column(Integer)
    # "YYYY-MM" of the last month this payment was recorded.
    last_applied_month: Mapped[str | None] = mapped_column(String(7), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Goal(Base):
    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(120))
    target_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2))
    saved_amount: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    target_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InvestmentTransaction(Base):
    __tablename__ = "investment_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    instrument_name: Mapped[str] = mapped_column(String(180))
    asset_type: Mapped[str] = mapped_column(String(32), default="Stock")
    side: Mapped[str] = mapped_column(String(8))
    quantity: Mapped[Decimal] = mapped_column(Numeric(20, 8))
    price: Mapped[Decimal] = mapped_column(Numeric(20, 6))
    fee: Mapped[Decimal] = mapped_column(Numeric(16, 2), default=Decimal("0"))
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    account_amount: Mapped[Decimal | None] = mapped_column(Numeric(20, 8), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class InvestmentPositionState(Base):
    """Persistent UI state for a derived investment position.

    Investment positions are calculated from the immutable trade ledger. This table
    only stores whether a position was intentionally hidden from the Positions view.
    """
    __tablename__ = "investment_position_states"
    __table_args__ = (UniqueConstraint("user_id", "symbol", "currency", name="uq_investment_position_state"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    currency: Mapped[str] = mapped_column(String(8), index=True)
    hidden: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WatchlistItem(Base):
    __tablename__ = "watchlist_items"
    __table_args__ = (UniqueConstraint("user_id", "symbol", name="uq_watchlist_user_symbol"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    symbol: Mapped[str] = mapped_column(String(32))
    instrument_name: Mapped[str] = mapped_column(String(180))
    asset_type: Mapped[str] = mapped_column(String(32), default="Stock")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
