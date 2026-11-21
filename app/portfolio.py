from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

from .models import InvestmentTransaction


ZERO = Decimal("0")

# BUY / SELL change the quantity. INCOME is a dividend or coupon: quantity is 1,
# price is the gross amount and fee is the tax withheld.
SIDES = ("BUY", "SELL", "INCOME")


@dataclass
class Position:
    symbol: str
    name: str
    asset_type: str
    currency: str
    quantity: Decimal = ZERO
    average_cost: Decimal = ZERO
    realised_pnl: Decimal = ZERO
    income: Decimal = ZERO
    trade_pnl: dict[int, Decimal] = field(default_factory=dict)

    @property
    def cost_basis(self) -> Decimal:
        return self.quantity * self.average_cost


def cash_movement(side: str, quantity: Decimal, price: Decimal, fee: Decimal) -> Decimal:
    """Money leaving investment capital (positive) or returning to it (negative)."""
    gross = Decimal(quantity) * Decimal(price)
    fee = Decimal(fee or 0)
    if side == "BUY":
        return gross + fee
    return -(gross - fee)


def _order_key(trade: InvestmentTransaction) -> tuple:
    """SQLite returns naive datetimes (stored in UTC); new trades are timezone-aware."""
    when = trade.occurred_at or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    # Unsaved trades (id None) go after saved ones at the same moment.
    return (when, trade.id if trade.id is not None else float("inf"))


def build_positions(transactions: Iterable[InvestmentTransaction]) -> list[Position]:
    """Weighted-average cost basis. Sells cannot make a position negative."""
    positions: dict[tuple[str, str], Position] = {}
    for trade in sorted(transactions, key=_order_key):
        key = (trade.symbol.upper(), trade.currency.upper())
        position = positions.setdefault(
            key,
            Position(
                symbol=trade.symbol.upper(),
                name=trade.instrument_name,
                asset_type=trade.asset_type,
                currency=trade.currency.upper(),
            ),
        )
        quantity = Decimal(trade.quantity)
        price = Decimal(trade.price)
        fee = Decimal(trade.fee or 0)
        if trade.side == "INCOME":
            position.income += quantity * price - fee
            continue
        if trade.side == "BUY":
            total_cost = position.cost_basis + (quantity * price) + fee
            position.quantity += quantity
            position.average_cost = total_cost / position.quantity
            position.name = trade.instrument_name or position.name
            position.asset_type = trade.asset_type or position.asset_type
        else:
            if quantity > position.quantity:
                raise ValueError(f"Cannot sell {quantity.normalize()} {position.symbol}: only {position.quantity.normalize()} recorded at that date")
            proceeds = (quantity * price) - fee
            pnl = proceeds - (quantity * position.average_cost)
            position.realised_pnl += pnl
            if trade.id is not None:
                position.trade_pnl[trade.id] = pnl
            position.quantity -= quantity
            if position.quantity == ZERO:
                position.average_cost = ZERO
    return list(positions.values())


def totals_by_currency(positions: Iterable[Position]) -> dict[str, dict[str, Decimal]]:
    totals: dict[str, dict[str, Decimal]] = defaultdict(lambda: {"cost_basis": ZERO, "realised_pnl": ZERO, "income": ZERO})
    for position in positions:
        totals[position.currency]["cost_basis"] += position.cost_basis
        totals[position.currency]["realised_pnl"] += position.realised_pnl
        totals[position.currency]["income"] += position.income
    return dict(totals)
