from __future__ import annotations

import hashlib
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .auth import get_current_user
from .bot import bot_user, configure_commands, dispatcher
from aiogram import Bot
from aiogram.types import Update
from .config import get_settings
from .database import SessionLocal, close_database, init_database
from .market_data import FinnhubClient, MarketDataUnavailable
import asyncio

from .finance import (
    LOCAL_TZ,
    month_summary_text,
    shift_month,
    account_balances,
    analytics,
    apply_due_recurring,
    first_month_to_skip,
    owned_account_id,
    total_balance,
)
from .models import Account, CategoryBudget, Goal, InvestmentPositionState, InvestmentTransaction, RecurringPayment, Transaction, User, WatchlistItem
from .portfolio import build_positions, totals_by_currency
from .schemas import (
    AccountCreate,
    CategoryBudgetSet,
    FXConvertCreate,
    GoalCreate,
    GoalUpdate,
    InvestmentCapitalTransfer,
    InvestmentTransactionCreate,
    RecurringCreate,
    TransactionCreate,
    TransferCreate,
    UserSettingsUpdate,
    WatchlistCreate,
)
from .services import monthly_totals

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"


telegram_bot: Bot | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start the HTTP app first; Telegram configuration must never block it."""
    global telegram_bot
    import asyncio
    import logging

    await init_database()
    settings = get_settings()

    async def setup_telegram() -> None:
        global telegram_bot
        if not settings.bot_token or not settings.public_app_url.startswith("https://"):
            logging.warning("Telegram webhook disabled: BOT_TOKEN/PUBLIC_APP_URL is missing or invalid")
            return

        bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        telegram_bot = bot
        webhook_url = settings.public_app_url.rstrip("/") + "/telegram/webhook"

        # Webhook is the critical part. Configure it first so a failure in
        # optional Telegram UI configuration cannot prevent /start from working.
        try:
            await bot.set_webhook(
                webhook_url,
                secret_token=webhook_secret(settings.bot_token),
                allowed_updates=dispatcher.resolve_used_update_types(),
                drop_pending_updates=False,
            )
            logging.info("Telegram webhook set: %s", webhook_url)
        except Exception:
            logging.exception("Failed to set Telegram webhook")
            return

        try:
            await configure_commands(bot)
            logging.info("Telegram commands/menu configured")
        except Exception:
            logging.exception("Telegram commands/menu configuration failed; webhook remains active")

    # Do not await Telegram API setup before serving HTTP.
    asyncio.create_task(setup_telegram())
    jobs = asyncio.create_task(background_jobs())

    try:
        yield
    finally:
        jobs.cancel()
        if telegram_bot is not None:
            # Never delete the webhook here. On a Railway redeploy the new container
            # sets the webhook first and the old one shuts down afterwards; deleting it
            # on shutdown left the bot silent (no reply to /start) until the next deploy.
            try:
                await telegram_bot.session.close()
            finally:
                telegram_bot = None
        await close_database()


SUMMARY_HOUR = 10  # local time on the 1st of the month


async def run_background_once(now: datetime | None = None) -> None:
    """Record due recurring payments for everyone and send last month's summaries."""
    local_now = (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ)
    today = local_now.date()
    prev_year, prev_month = shift_month(today.year, today.month, -1)
    prev_key = f"{prev_year:04d}-{prev_month:02d}"
    async with SessionLocal() as session:
        users = list((await session.scalars(select(User))).all())
        for user in users:
            await apply_due_recurring(session, user.id, today)
            if user.last_summary_month is None:
                # First run after this feature shipped: start with the next month,
                # do not send everyone a summary of an old month at once.
                user.last_summary_month = prev_key
                continue
            if user.last_summary_month >= prev_key or local_now.hour < SUMMARY_HOUR and today.day == 1:
                continue
            text = await month_summary_text(session, user, prev_year, prev_month)
            if text and telegram_bot is not None:
                try:
                    await telegram_bot.send_message(user.telegram_id, text)
                except Exception:
                    logging.exception("Failed to send monthly summary to user %s", user.id)
                    continue
            user.last_summary_month = prev_key
        await session.commit()


async def background_jobs() -> None:
    await asyncio.sleep(20)  # let the webhook and bot come up first
    while True:
        try:
            await run_background_once()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Background job failed")
        await asyncio.sleep(30 * 60)


app = FastAPI(title="finplan", version="2.0.0", lifespan=lifespan)
app.mount("/assets", StaticFiles(directory=STATIC), name="assets")


@app.middleware("http")
async def prevent_stale_mini_app_cache(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/assets/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


async def db_session():
    async with SessionLocal() as session:
        yield session


async def owned_user(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(db_session),
) -> User:
    return await session.merge(user)


def webhook_secret(bot_token: str) -> str:
    """Stable secret derived from the token, so Telegram's calls can be told apart from strangers'."""
    return hashlib.sha256(("finplan-webhook:" + bot_token).encode()).hexdigest()


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, bool]:
    if telegram_bot is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    if x_telegram_bot_api_secret_token != webhook_secret(get_settings().bot_token):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    try:
        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": telegram_bot})
        await dispatcher.feed_update(telegram_bot, update)
    except Exception:
        # Answer 200 anyway: a 5xx makes Telegram retry the same update forever
        # and hold back every newer message (including /start) behind it.
        logging.exception("Failed to handle Telegram update")
    return {"ok": True}


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "telegram": "configured" if telegram_bot is not None else "not_configured"}


@app.get("/")
async def mini_app() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/me")
async def me(user: User = Depends(owned_user)) -> dict:
    return {
        "first_name": user.first_name,
        "username": user.username,
        "currency": user.currency,
        "monthly_budget": user.monthly_budget,
        "opening_balance": user.opening_balance,
        "investment_capital": user.investment_capital,
        "language": user.language or "uk",
        "theme": user.theme or "system",
    }


@app.patch("/api/me")
async def update_me(
    payload: UserSettingsUpdate,
    user: User = Depends(owned_user),
    session: AsyncSession = Depends(db_session),
) -> dict:
    user.currency = payload.currency
    user.monthly_budget = payload.monthly_budget
    user.language = payload.language
    user.theme = payload.theme
    user.opening_balance = payload.opening_balance
    await session.commit()
    return {
        "currency": user.currency,
        "monthly_budget": user.monthly_budget,
        "opening_balance": user.opening_balance,
        "investment_capital": user.investment_capital,
        "language": user.language,
        "theme": user.theme,
    }


@app.get("/api/dashboard")
async def dashboard(
    user: User = Depends(owned_user),
    session: AsyncSession = Depends(db_session),
) -> dict:
    if await apply_due_recurring(session, user.id):
        await session.commit()
    income, expenses = await monthly_totals(session, user.id)
    accounts = await account_balances(session, user)
    raw_balance = sum((a["balance"] for a in accounts), Decimal("0"))
    account_balance = raw_balance - Decimal(user.investment_capital or 0)
    goals = list((await session.scalars(select(Goal).where(Goal.user_id == user.id).order_by(Goal.created_at.desc()))).all())
    budget_left = Decimal(user.monthly_budget) - expenses if user.monthly_budget is not None else None
    return {
        "currency": user.currency,
        "income": income,
        "expenses": expenses,
        "balance": account_balance,
        "accounts": accounts,
        "opening_balance": user.opening_balance,
        "budget_left": budget_left,
        "goals": [
            {"id": g.id, "title": g.title, "target_amount": g.target_amount, "saved_amount": g.saved_amount, "target_date": g.target_date}
            for g in goals
        ],
    }


@app.get("/api/transactions")
async def transactions(user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> list[dict]:
    rows = list((await session.scalars(select(Transaction).where(Transaction.user_id == user.id).order_by(Transaction.occurred_at.desc(), Transaction.id.desc()).limit(300))).all())
    return [{"id": r.id, "kind": r.kind, "amount": r.amount, "category": r.category, "note": r.note, "account_id": r.account_id, "occurred_at": as_utc(r.occurred_at)} for r in rows]


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; they are stored in UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def checked_account(session: AsyncSession, user: User, account_id: int | None) -> int | None:
    try:
        return await owned_account_id(session, user, account_id)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@app.post("/api/transactions", status_code=201)
async def create_transaction(payload: TransactionCreate, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    account_id = await checked_account(session, user, payload.account_id)
    row = Transaction(user_id=user.id, kind=payload.kind, amount=payload.amount, category=payload.category, note=payload.note, account_id=account_id, occurred_at=as_utc(payload.occurred_at) or datetime.now(timezone.utc))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"id": row.id}


@app.patch("/api/transactions/{transaction_id}")
async def update_transaction(transaction_id: int, payload: TransactionCreate, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    row = await session.scalar(select(Transaction).where(Transaction.id == transaction_id, Transaction.user_id == user.id))
    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if row.kind not in ("expense", "income"):
        raise HTTPException(status_code=400, detail="Transfers cannot be edited; delete and create a new one")
    row.kind = payload.kind
    row.amount = payload.amount
    row.category = payload.category
    row.note = payload.note
    row.account_id = await checked_account(session, user, payload.account_id)
    if payload.occurred_at is not None:
        row.occurred_at = as_utc(payload.occurred_at)
    await session.commit()
    return {"ok": True}


# ---------- accounts ----------

@app.get("/api/accounts")
async def accounts(user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> list[dict]:
    return await account_balances(session, user)


@app.post("/api/accounts", status_code=201)
async def create_account(payload: AccountCreate, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    count = len((await session.scalars(select(Account.id).where(Account.user_id == user.id))).all())
    if count >= 10:
        raise HTTPException(status_code=400, detail="Too many accounts")
    account = Account(user_id=user.id, name=payload.name.strip(), opening_balance=payload.opening_balance)
    session.add(account)
    await session.commit()
    await session.refresh(account)
    return {"id": account.id}


@app.patch("/api/accounts/{account_id}")
async def update_account(account_id: int, payload: AccountCreate, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    account = await session.scalar(select(Account).where(Account.id == account_id, Account.user_id == user.id))
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    account.name = payload.name.strip()
    account.opening_balance = payload.opening_balance
    await session.commit()
    return {"ok": True}


@app.delete("/api/accounts/{account_id}")
async def delete_account(account_id: int, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    """Operations of a deleted account move to the main account; its starting balance is dropped."""
    account = await session.scalar(select(Account).where(Account.id == account_id, Account.user_id == user.id))
    if not account:
        raise HTTPException(status_code=404, detail="Account not found")
    for row in (await session.scalars(select(Transaction).where(Transaction.user_id == user.id, Transaction.account_id == account_id))).all():
        row.account_id = None
    for item in (await session.scalars(select(RecurringPayment).where(RecurringPayment.user_id == user.id, RecurringPayment.account_id == account_id))).all():
        item.account_id = None
    await session.delete(account)
    await session.commit()
    return {"ok": True}


@app.post("/api/accounts/transfer", status_code=201)
async def transfer_between_accounts(payload: TransferCreate, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    source = await checked_account(session, user, payload.from_account_id)
    target = await checked_account(session, user, payload.to_account_id)
    if source == target:
        raise HTTPException(status_code=400, detail="Choose two different accounts")
    now = datetime.now(timezone.utc)
    note = (payload.note or "").strip() or None
    session.add(Transaction(user_id=user.id, kind="transfer_out", amount=payload.amount, category="Transfer", note=note, account_id=source, occurred_at=now))
    session.add(Transaction(user_id=user.id, kind="transfer_in", amount=payload.amount, category="Transfer", note=note, account_id=target, occurred_at=now))
    await session.commit()
    return {"ok": True}


# ---------- analytics & category limits ----------

@app.get("/api/analytics")
async def analytics_view(month: str | None = None, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    if month is not None and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(status_code=400, detail="month must look like 2026-11")
    return await analytics(session, user, month)


@app.put("/api/budgets/{category}")
async def set_category_budget(category: str, payload: CategoryBudgetSet, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    category = category.strip()[:64] or "Other"
    budget = await session.scalar(select(CategoryBudget).where(CategoryBudget.user_id == user.id, CategoryBudget.category == category))
    if not payload.limit_amount:
        if budget:
            await session.delete(budget)
    elif budget:
        budget.limit_amount = payload.limit_amount
    else:
        session.add(CategoryBudget(user_id=user.id, category=category, limit_amount=payload.limit_amount))
    await session.commit()
    return {"ok": True}


# ---------- recurring payments ----------

@app.get("/api/recurring")
async def recurring(user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> list[dict]:
    rows = list((await session.scalars(select(RecurringPayment).where(RecurringPayment.user_id == user.id).order_by(RecurringPayment.day_of_month, RecurringPayment.id))).all())
    return [
        {"id": r.id, "kind": r.kind, "amount": r.amount, "category": r.category, "note": r.note,
         "account_id": r.account_id, "day_of_month": r.day_of_month, "last_applied_month": r.last_applied_month}
        for r in rows
    ]


@app.post("/api/recurring", status_code=201)
async def create_recurring(payload: RecurringCreate, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    account_id = await checked_account(session, user, payload.account_id)
    item = RecurringPayment(
        user_id=user.id, kind=payload.kind, amount=payload.amount, category=payload.category,
        note=(payload.note or "").strip() or None, account_id=account_id, day_of_month=payload.day_of_month,
        last_applied_month=first_month_to_skip(payload.day_of_month),
    )
    session.add(item)
    await session.flush()
    await apply_due_recurring(session, user.id)
    await session.commit()
    return {"id": item.id}


@app.delete("/api/recurring/{item_id}")
async def delete_recurring(item_id: int, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    item = await session.scalar(select(RecurringPayment).where(RecurringPayment.id == item_id, RecurringPayment.user_id == user.id))
    if not item:
        raise HTTPException(status_code=404, detail="Recurring payment not found")
    await session.delete(item)
    await session.commit()
    return {"ok": True}


@app.delete("/api/transactions/{transaction_id}")
async def delete_transaction(transaction_id: int, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    row = await session.scalar(select(Transaction).where(Transaction.id == transaction_id, Transaction.user_id == user.id))
    if not row:
        raise HTTPException(status_code=404, detail="Transaction not found")
    await session.delete(row)
    await session.commit()
    return {"ok": True}


@app.get("/api/goals")
async def goals(user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> list[dict]:
    rows = list((await session.scalars(select(Goal).where(Goal.user_id == user.id).order_by(Goal.created_at.desc()))).all())
    return [{"id": g.id, "title": g.title, "target_amount": g.target_amount, "saved_amount": g.saved_amount, "target_date": g.target_date} for g in rows]


@app.post("/api/goals", status_code=201)
async def create_goal(payload: GoalCreate, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    goal = Goal(user_id=user.id, **payload.model_dump())
    session.add(goal)
    await session.commit()
    await session.refresh(goal)
    return {"id": goal.id}


@app.patch("/api/goals/{goal_id}")
async def update_goal(
    goal_id: int,
    payload: GoalUpdate | None = None,
    saved_amount: Decimal | None = None,
    user: User = Depends(owned_user),
    session: AsyncSession = Depends(db_session),
) -> dict:
    """Accepts a JSON body; the old `?saved_amount=` query form still works."""
    goal = await session.scalar(select(Goal).where(Goal.id == goal_id, Goal.user_id == user.id))
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    payload = payload or GoalUpdate()
    new_saved = payload.saved_amount if payload.saved_amount is not None else saved_amount
    if new_saved is not None:
        if new_saved < 0:
            raise HTTPException(status_code=400, detail="Saved amount cannot be negative")
        goal.saved_amount = new_saved
    if payload.clear_target_date:
        goal.target_date = None
    elif payload.target_date is not None:
        goal.target_date = as_utc(payload.target_date)
    await session.commit()
    return {"ok": True}


@app.delete("/api/goals/{goal_id}")
async def delete_goal(goal_id: int, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    goal = await session.scalar(select(Goal).where(Goal.id == goal_id, Goal.user_id == user.id))
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    await session.delete(goal)
    await session.commit()
    return {"ok": True}


@app.get("/api/markets/search")
async def market_search(q: str, _: User = Depends(owned_user)) -> list[dict]:
    if len(q.strip()) < 2:
        return []
    try:
        return await FinnhubClient().search(q.strip())
    except MarketDataUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/markets/instruments/{symbol}")
async def instrument(symbol: str, _: User = Depends(owned_user)) -> dict:
    try:
        return await FinnhubClient().instrument(symbol.strip().upper())
    except MarketDataUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.get("/api/markets/instruments/{symbol}/history")
async def instrument_history(symbol: str, range: str = "1mo", _: User = Depends(owned_user)) -> dict:
    try:
        return await FinnhubClient().history(symbol.strip().upper(), range)
    except MarketDataUnavailable as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/api/fx/convert")
async def fx_convert(payload: FXConvertCreate, _: User = Depends(owned_user)) -> dict:
    if payload.base == payload.quote:
        return {"base": payload.base, "quote": payload.quote, "amount": payload.amount, "rate": Decimal("1"), "result": payload.amount, "source": "same currency"}
    try:
        async with __import__("httpx").AsyncClient(timeout=10, headers={"User-Agent": "finplan/1.0"}) as client:
            response = await client.get(f"https://api.frankfurter.app/latest?from={payload.base}&to={payload.quote}")
            response.raise_for_status()
            data = response.json()
        rate = Decimal(str(data.get("rates", {}).get(payload.quote)))
        if not rate:
            raise ValueError("rate missing")
        return {"base": payload.base, "quote": payload.quote, "amount": payload.amount, "rate": rate, "result": payload.amount * rate, "date": data.get("date"), "source": "Frankfurter / ECB"}
    except Exception:
        try:
            async with __import__("httpx").AsyncClient(timeout=10, headers={"User-Agent": "finplan/1.0"}) as client:
                response = await client.get(f"https://open.er-api.com/v6/latest/{payload.base}")
                response.raise_for_status()
                data = response.json()
            rate = Decimal(str(data.get("rates", {}).get(payload.quote)))
            if not rate:
                raise ValueError("rate missing")
            return {"base": payload.base, "quote": payload.quote, "amount": payload.amount, "rate": rate, "result": payload.amount * rate, "source": "ExchangeRate API"}
        except Exception as error:
            raise HTTPException(status_code=503, detail="Exchange rate provider is temporarily unavailable") from error


@app.post("/api/watchlist", status_code=201)
async def add_watchlist(payload: WatchlistCreate, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    symbol = payload.symbol.upper()
    exists = await session.scalar(select(WatchlistItem).where(WatchlistItem.user_id == user.id, WatchlistItem.symbol == symbol))
    if exists:
        return {"id": exists.id, "already_saved": True}
    item = WatchlistItem(user_id=user.id, symbol=symbol, instrument_name=payload.instrument_name, asset_type=payload.asset_type)
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return {"id": item.id}


@app.delete("/api/watchlist/{item_id}")
async def delete_watchlist(item_id: int, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    item = await session.scalar(select(WatchlistItem).where(WatchlistItem.id == item_id, WatchlistItem.user_id == user.id))
    if not item:
        raise HTTPException(status_code=404, detail="Watchlist item not found")
    await session.delete(item)
    await session.commit()
    return {"ok": True}


@app.get("/api/watchlist")
async def watchlist(user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> list[dict]:
    rows = list((await session.scalars(select(WatchlistItem).where(WatchlistItem.user_id == user.id).order_by(WatchlistItem.created_at.desc()))).all())
    return [{"id": r.id, "symbol": r.symbol, "name": r.instrument_name, "type": r.asset_type} for r in rows]


async def raw_account_balance(session: AsyncSession, user: User) -> Decimal:
    """All accounts together, before money set aside for investing."""
    return await total_balance(session, user)


async def investment_cash_flow(session: AsyncSession, user: User) -> Decimal:
    """Net investment cash movement, always converted to the user's account currency.

    Positive values consume investment capital (BUY); negative values release it (SELL).
    New trades store the converted amount so historical FX changes cannot rewrite the
    amount that was actually committed. Older rows without account_amount are converted
    on read as a backwards-compatible fallback.
    """
    trades = list((await session.scalars(select(InvestmentTransaction).where(InvestmentTransaction.user_id == user.id))).all())
    currency = user.currency.upper()
    net = Decimal("0")
    for trade in trades:
        if trade.account_amount is not None:
            net += Decimal(trade.account_amount)
            continue
        gross = Decimal(trade.quantity) * Decimal(trade.price)
        fee = Decimal(trade.fee or 0)
        movement = gross + fee if trade.side == "BUY" else -(gross - fee)
        if trade.currency.upper() != currency:
            rate = await fx_rate(trade.currency, currency)
            if rate <= 0:
                raise HTTPException(status_code=503, detail=f"Unable to convert {trade.currency} to {currency} for investment capital.")
            movement *= rate
        net += movement
    return net


@app.get("/api/investments/capital")
async def investment_capital(user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    raw_balance = await raw_account_balance(session, user)
    capital = Decimal(user.investment_capital or 0)
    net_invested = await investment_cash_flow(session, user)
    available = max(Decimal("0"), capital - net_invested)
    return {
        "currency": user.currency.upper(),
        "capital": capital,
        "available": available,
        "committed": max(Decimal("0"), net_invested),
        "main_balance": raw_balance - capital,
        "raw_balance": raw_balance,
    }


@app.post("/api/investments/capital")
async def transfer_investment_capital(payload: InvestmentCapitalTransfer, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    raw_balance = await raw_account_balance(session, user)
    current_capital = Decimal(user.investment_capital or 0)
    amount = Decimal(payload.amount)
    if payload.direction == "allocate":
        main_available = raw_balance - current_capital
        if amount > main_available:
            raise HTTPException(status_code=400, detail=f"Not enough available balance. Available: {main_available.quantize(Decimal('0.01'))}")
        user.investment_capital = current_capital + amount
    else:
        available = max(Decimal("0"), current_capital - await investment_cash_flow(session, user))
        if amount > available:
            raise HTTPException(status_code=400, detail=f"Not enough free investment capital. Available: {available.quantize(Decimal('0.01'))}")
        user.investment_capital = current_capital - amount
    await session.commit()
    return await investment_capital(user, session)


@app.post("/api/investments/transactions", status_code=201)
async def create_investment_transaction(payload: InvestmentTransactionCreate, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    trades = list((await session.scalars(select(InvestmentTransaction).where(InvestmentTransaction.user_id == user.id))).all())
    account_currency = user.currency.upper()
    trade_currency = payload.currency.upper()
    gross = Decimal(payload.quantity) * Decimal(payload.price)
    fee = Decimal(payload.fee or 0)
    cash_movement = gross + fee if payload.side == "BUY" else -(gross - fee)

    if trade_currency != account_currency:
        rate = await fx_rate(trade_currency, account_currency)
        if rate <= 0:
            raise HTTPException(status_code=503, detail=f"Unable to get the {trade_currency}/{account_currency} exchange rate. Try again in a moment.")
        account_amount = cash_movement * rate
    else:
        account_amount = cash_movement

    if payload.side == "BUY":
        capital = Decimal(user.investment_capital or 0)
        net_invested = await investment_cash_flow(session, user)
        available = max(Decimal("0"), capital - net_invested)
        if account_amount > available:
            raise HTTPException(
                status_code=400,
                detail=f"Not enough free investment capital. Available: {available.quantize(Decimal('0.01'))} {account_currency}."
            )
    else:
        try:
            positions = build_positions(trades)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        current = next((p for p in positions if p.symbol == payload.symbol.upper() and p.currency == trade_currency), None)
        if current is None or payload.quantity > current.quantity:
            available = current.quantity if current else Decimal("0")
            raise HTTPException(status_code=400, detail=f"Not enough recorded quantity. Available: {available}")

    trade_data = payload.model_dump(exclude_none=True)
    trade_data["symbol"] = trade_data["symbol"].upper()
    trade_data["currency"] = trade_currency
    trade = InvestmentTransaction(
        user_id=user.id,
        account_amount=account_amount,
        **trade_data,
    )
    session.add(trade)
    if payload.side == "BUY":
        hidden_state = await session.scalar(
            select(InvestmentPositionState).where(
                InvestmentPositionState.user_id == user.id,
                func.upper(InvestmentPositionState.symbol) == trade_data["symbol"].upper(),
                func.upper(InvestmentPositionState.currency) == trade_currency,
            )
        )
        if hidden_state:
            hidden_state.hidden = False
            hidden_state.updated_at = datetime.now(timezone.utc)
    await session.commit()
    await session.refresh(trade)
    return {"id": trade.id}


@app.get("/api/investments/transactions")
async def investment_transactions(user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> list[dict]:
    rows = list((await session.scalars(select(InvestmentTransaction).where(InvestmentTransaction.user_id == user.id).order_by(InvestmentTransaction.occurred_at.desc(), InvestmentTransaction.id.desc()))).all())
    return [{
        "id": row.id,
        "symbol": row.symbol,
        "instrument_name": row.instrument_name,
        "asset_type": row.asset_type,
        "side": row.side,
        "quantity": row.quantity,
        "price": row.price,
        "fee": row.fee,
        "currency": row.currency,
        "occurred_at": row.occurred_at,
    } for row in rows]


@app.delete("/api/investments/transactions/{trade_id}")
async def delete_investment_transaction(trade_id: int, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    """The investment ledger is immutable. Trades are permanent history records."""
    raise HTTPException(status_code=405, detail="Investment trade history cannot be deleted")


@app.delete("/api/investments/positions/{symbol}/{currency}")
async def delete_investment_position(symbol: str, currency: str, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    """Hide a derived position without touching its permanent trade history."""
    normalized_symbol = symbol.upper()
    normalized_currency = currency.upper()
    rows = list((await session.scalars(
        select(InvestmentTransaction).where(
            InvestmentTransaction.user_id == user.id,
            func.upper(InvestmentTransaction.symbol) == normalized_symbol,
            func.upper(InvestmentTransaction.currency) == normalized_currency,
        )
    )).all())
    if not rows:
        raise HTTPException(status_code=404, detail="Investment position not found")

    state = await session.scalar(
        select(InvestmentPositionState).where(
            InvestmentPositionState.user_id == user.id,
            func.upper(InvestmentPositionState.symbol) == normalized_symbol,
            func.upper(InvestmentPositionState.currency) == normalized_currency,
        )
    )
    if state:
        state.hidden = True
        state.updated_at = datetime.now(timezone.utc)
    else:
        state = InvestmentPositionState(
            user_id=user.id, symbol=normalized_symbol, currency=normalized_currency, hidden=True
        )
        session.add(state)
    await session.commit()
    return {"ok": True, "hidden": True}


async def fx_rate(base: str, quote: str) -> Decimal:
    if base.upper() == quote.upper():
        return Decimal("1")
    try:
        async with __import__("httpx").AsyncClient(timeout=8, headers={"User-Agent": "finplan/1.0"}) as client:
            response = await client.get(f"https://api.frankfurter.app/latest?from={base.upper()}&to={quote.upper()}")
            response.raise_for_status()
            rate = response.json().get("rates", {}).get(quote.upper())
            if rate is not None:
                return Decimal(str(rate))
    except Exception:
        pass
    try:
        async with __import__("httpx").AsyncClient(timeout=8, headers={"User-Agent": "finplan/1.0"}) as client:
            response = await client.get(f"https://open.er-api.com/v6/latest/{base.upper()}")
            response.raise_for_status()
            rate = response.json().get("rates", {}).get(quote.upper())
            if rate is not None:
                return Decimal(str(rate))
    except Exception:
        pass
    return Decimal("1") if base.upper() == quote.upper() else Decimal("0")


@app.get("/api/portfolio")
async def portfolio(user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    trades = list((await session.scalars(select(InvestmentTransaction).where(InvestmentTransaction.user_id == user.id).order_by(InvestmentTransaction.occurred_at, InvestmentTransaction.id))).all())
    try:
        positions = build_positions(trades)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    hidden_rows = list((await session.scalars(
        select(InvestmentPositionState).where(InvestmentPositionState.user_id == user.id, InvestmentPositionState.hidden.is_(True))
    )).all())
    hidden_keys = {(row.symbol.upper(), row.currency.upper()) for row in hidden_rows}
    positions = [p for p in positions if (p.symbol.upper(), p.currency.upper()) not in hidden_keys]
    open_positions = [p for p in positions if p.quantity > 0]
    valued = []
    client = FinnhubClient()
    for p in open_positions:
        current_price = None
        change_percent = None
        try:
            quote = await client.quote(p.symbol)
            current_price = Decimal(str(quote.get("price"))) if quote.get("price") is not None else None
            change_percent = quote.get("change_percent")
        except MarketDataUnavailable:
            pass
        current_value = (p.quantity * current_price) if current_price is not None else None
        unrealised = (current_value - p.cost_basis) if current_value is not None else None
        valued.append({"symbol": p.symbol, "name": p.name, "asset_type": p.asset_type, "currency": p.currency, "quantity": p.quantity, "average_cost": p.average_cost, "cost_basis": p.cost_basis, "realised_pnl": p.realised_pnl, "current_price": current_price, "current_value": current_value, "unrealised_pnl": unrealised, "change_percent": change_percent})
    totals = totals_by_currency(positions)
    for row in valued:
        bucket = totals.setdefault(row["currency"], {"cost_basis": Decimal("0"), "realised_pnl": Decimal("0")})
        if row["current_value"] is not None:
            bucket["current_value"] = bucket.get("current_value", Decimal("0")) + row["current_value"]
            bucket["unrealised_pnl"] = bucket.get("unrealised_pnl", Decimal("0")) + (row["unrealised_pnl"] or Decimal("0"))

    account_currency = user.currency.upper()
    total_current = Decimal("0")
    total_cost = Decimal("0")
    total_unrealised = Decimal("0")
    conversion = {}
    for row in valued:
        rate = await fx_rate(row["currency"], account_currency)
        conversion[row["currency"]] = rate
        if row["current_value"] is not None:
            total_current += row["current_value"] * rate
        total_cost += row["cost_basis"] * rate
        total_unrealised += (row["unrealised_pnl"] or Decimal("0")) * rate
    return {"positions": valued, "totals_by_currency": totals, "account_currency": account_currency, "total_current_value": total_current, "total_cost_basis": total_cost, "total_unrealised_pnl": total_unrealised, "conversion_rates": conversion}


# Must stay the last route. Old Menu-button / BotFather links may point at paths such as
# /app or /webapp; open the Mini App for them instead of answering "Not Found".
@app.get("/{path:path}", include_in_schema=False)
async def mini_app_fallback(path: str) -> FileResponse:
    if path.startswith(("api/", "assets/", "telegram/")):
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})

