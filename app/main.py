from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
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
from .models import Goal, InvestmentPositionState, InvestmentTransaction, Transaction, User, WatchlistItem
from .portfolio import build_positions, totals_by_currency
from .schemas import FXConvertCreate, GoalCreate, InvestmentCapitalTransfer, InvestmentTransactionCreate, TransactionCreate, UserSettingsUpdate, WatchlistCreate
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

        bot = Bot(settings.bot_token)
        telegram_bot = bot
        webhook_url = settings.public_app_url.rstrip("/") + "/telegram/webhook"

        # Webhook is the critical part. Configure it first so a failure in
        # optional Telegram UI configuration cannot prevent /start from working.
        try:
            await bot.set_webhook(webhook_url, drop_pending_updates=False)
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

    try:
        yield
    finally:
        if telegram_bot is not None:
            try:
                await telegram_bot.delete_webhook(drop_pending_updates=False)
            except Exception:
                logging.exception("Failed to delete Telegram webhook during shutdown")
            try:
                await telegram_bot.session.close()
            finally:
                telegram_bot = None
        await close_database()


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


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    if telegram_bot is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    payload = await request.json()
    update = Update.model_validate(payload, context={"bot": telegram_bot})
    await dispatcher.feed_update(telegram_bot, update)
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
    income, expenses = await monthly_totals(session, user.id)
    all_rows = list((await session.scalars(select(Transaction).where(Transaction.user_id == user.id))).all())
    all_income = sum((Decimal(row.amount) for row in all_rows if row.kind == "income"), Decimal("0"))
    all_expenses = sum((Decimal(row.amount) for row in all_rows if row.kind == "expense"), Decimal("0"))
    raw_account_balance = Decimal(user.opening_balance or 0) + all_income - all_expenses
    account_balance = raw_account_balance - Decimal(user.investment_capital or 0)
    goals = list((await session.scalars(select(Goal).where(Goal.user_id == user.id).order_by(Goal.created_at.desc()))).all())
    budget_left = Decimal(user.monthly_budget) - expenses if user.monthly_budget is not None else None
    return {
        "currency": user.currency,
        "income": income,
        "expenses": expenses,
        "balance": account_balance,
        "opening_balance": user.opening_balance,
        "budget_left": budget_left,
        "goals": [
            {"id": g.id, "title": g.title, "target_amount": g.target_amount, "saved_amount": g.saved_amount, "target_date": g.target_date}
            for g in goals
        ],
    }


@app.get("/api/transactions")
async def transactions(user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> list[dict]:
    rows = list((await session.scalars(select(Transaction).where(Transaction.user_id == user.id).order_by(Transaction.occurred_at.desc()).limit(200))).all())
    return [{"id": r.id, "kind": r.kind, "amount": r.amount, "category": r.category, "note": r.note, "occurred_at": r.occurred_at} for r in rows]


@app.post("/api/transactions", status_code=201)
async def create_transaction(payload: TransactionCreate, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    row = Transaction(user_id=user.id, kind=payload.kind, amount=payload.amount, category=payload.category, note=payload.note, occurred_at=payload.occurred_at or datetime.now(timezone.utc))
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return {"id": row.id}


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
async def update_goal(goal_id: int, saved_amount: Decimal, user: User = Depends(owned_user), session: AsyncSession = Depends(db_session)) -> dict:
    goal = await session.scalar(select(Goal).where(Goal.id == goal_id, Goal.user_id == user.id))
    if not goal:
        raise HTTPException(status_code=404, detail="Goal not found")
    if saved_amount < 0:
        raise HTTPException(status_code=400, detail="Saved amount cannot be negative")
    goal.saved_amount = saved_amount
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
    rows = list((await session.scalars(select(Transaction).where(Transaction.user_id == user.id))).all())
    income = sum((Decimal(row.amount) for row in rows if row.kind == "income"), Decimal("0"))
    expenses = sum((Decimal(row.amount) for row in rows if row.kind == "expense"), Decimal("0"))
    return Decimal(user.opening_balance or 0) + income - expenses


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

