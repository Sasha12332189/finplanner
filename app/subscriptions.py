"""finplan Pro / Premium: plans, limits and Telegram Stars subscription state.

Payments use Telegram Stars (currency "XTR") with a 30-day subscription period, so
Telegram renews them automatically and sends a new successful_payment each month.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import get_settings
from .models import Account, CategoryBudget, Goal, InvestmentTransaction, Payment, RecurringPayment, User, WatchlistItem

SUBSCRIPTION_PERIOD = 30 * 24 * 60 * 60  # the only period Telegram allows for Stars subscriptions
GRACE = timedelta(hours=12)  # renewals can arrive a little after the period ends

# Buyers pay roughly $0.02 per Star, so 250 ⭐ ≈ $5 and 350 ⭐ ≈ $7.
PLANS = {
    "pro": {"stars": 250, "usd": 5, "rank": 1},
    "premium": {"stars": 350, "usd": 7, "rank": 2},
}

UNLIMITED = None
# None = no limit. "months_back" = how many previous months analytics can open.
LIMITS = {
    "free":    {"accounts": 1, "goals": 3, "budgets": 2, "recurring": 3, "months_back": 1, "positions": 3, "watchlist": 3, "summary": False},
    "pro":     {"accounts": UNLIMITED, "goals": UNLIMITED, "budgets": UNLIMITED, "recurring": UNLIMITED, "months_back": UNLIMITED, "positions": 3, "watchlist": 3, "summary": True},
    "premium": {"accounts": UNLIMITED, "goals": UNLIMITED, "budgets": UNLIMITED, "recurring": UNLIMITED, "months_back": UNLIMITED, "positions": UNLIMITED, "watchlist": UNLIMITED, "summary": True},
}

TITLES = {
    "pro": ("finplan Pro", "Бюджет без ограничений: счета, цели, лимиты, регулярные платежи, вся история аналитики и итоги месяца в боте."),
    "premium": ("finplan Premium", "Всё из Pro и инвестиции без ограничений: любое число активов и избранное."),
}


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def effective_plan(user: User, now: datetime | None = None) -> str:
    if user.telegram_id in get_settings().admin_ids:
        return "premium"
    expires = as_utc(user.plan_expires_at)
    if user.plan in PLANS and expires and expires + GRACE > (now or datetime.now(timezone.utc)):
        return user.plan
    return "free"


def limits_for(user: User) -> dict:
    return LIMITS[effective_plan(user)]


def plan_needed(feature: str) -> str:
    """The cheapest plan that lifts a limit."""
    for plan in ("pro", "premium"):
        if LIMITS[plan][feature] != LIMITS["free"][feature]:
            return plan
    return "premium"


def premium_required(user: User, feature: str, limit: int | None = None) -> HTTPException:
    current = effective_plan(user)
    needed = plan_needed(feature)
    if current != "free" and PLANS[needed]["rank"] <= PLANS[current]["rank"]:
        needed = "premium"
    return HTTPException(status_code=402, detail={
        "code": "premium_required", "feature": feature, "limit": limit,
        "plan": current, "plan_needed": needed,
        "message": f"Upgrade to {needed} to use more {feature}.",
    })


async def count(session: AsyncSession, model, user_id: int) -> int:
    return int(await session.scalar(select(func.count()).select_from(model).where(model.user_id == user_id)) or 0)


async def check_limit(session: AsyncSession, user: User, feature: str, model) -> None:
    limit = limits_for(user)[feature]
    if limit is not None and await count(session, model, user.id) >= limit:
        raise premium_required(user, feature, limit)


async def usage(session: AsyncSession, user: User) -> dict:
    from .portfolio import build_positions

    trades = list((await session.scalars(select(InvestmentTransaction).where(InvestmentTransaction.user_id == user.id))).all())
    try:
        open_positions = sum(1 for p in build_positions(trades) if p.quantity > 0)
    except ValueError:
        open_positions = 0
    return {
        "accounts": await count(session, Account, user.id),
        "goals": await count(session, Goal, user.id),
        "budgets": await count(session, CategoryBudget, user.id),
        "recurring": await count(session, RecurringPayment, user.id),
        "positions": open_positions,
        "watchlist": await count(session, WatchlistItem, user.id),
    }


async def status(session: AsyncSession, user: User) -> dict:
    plan = effective_plan(user)
    return {
        "plan": plan,
        "is_admin": user.telegram_id in get_settings().admin_ids,
        "expires_at": as_utc(user.plan_expires_at) if plan != "free" else None,
        "auto_renew": bool(user.plan_charge_id) and not user.plan_canceled and plan != "free",
        "can_manage": bool(user.plan_charge_id) and plan != "free",
        "limits": LIMITS[plan],
        "usage": await usage(session, user),
        "plans": [{"id": k, "stars": v["stars"], "usd": v["usd"], "limits": LIMITS[k]} for k, v in PLANS.items()],
        "free_limits": LIMITS["free"],
    }


def parse_payload(payload: str) -> tuple[str, int] | None:
    """Invoice payload looks like "sub:pro:12345" (plan, Telegram user id)."""
    parts = (payload or "").split(":")
    if len(parts) != 3 or parts[0] != "sub" or parts[1] not in PLANS:
        return None
    try:
        return parts[1], int(parts[2])
    except ValueError:
        return None


async def record_payment(
    session: AsyncSession, user: User, plan: str, stars: int, charge_id: str,
    expires_unix: int | None, is_recurring: bool,
) -> tuple[bool, str | None]:
    """Activate or extend a plan. Returns (is_new_payment, previous charge id to cancel)."""
    existing = await session.scalar(select(Payment).where(Payment.telegram_charge_id == charge_id))
    if existing:
        return False, None
    now = datetime.now(timezone.utc)
    expires = datetime.fromtimestamp(expires_unix, timezone.utc) if expires_unix else now + timedelta(seconds=SUBSCRIPTION_PERIOD)
    session.add(Payment(user_id=user.id, plan=plan, stars=stars, telegram_charge_id=charge_id,
                        is_recurring=is_recurring, expires_at=expires))
    same_plan = user.plan == plan
    previous_charge = None
    if user.plan_charge_id and user.plan_charge_id != charge_id and not same_plan and effective_plan(user, now) != "free":
        previous_charge = user.plan_charge_id  # switching Pro <-> Premium: stop the old one renewing
    # A renewal keeps the first charge id: Telegram identifies the subscription by it.
    if not (is_recurring and same_plan and user.plan_charge_id):
        user.plan_charge_id = charge_id
    current = as_utc(user.plan_expires_at)
    user.plan_expires_at = max(expires, current) if current and same_plan and is_recurring else expires
    user.plan = plan
    user.plan_canceled = False
    return True, previous_charge
