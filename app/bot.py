from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ReplyKeyboardRemove
from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal, init_database
from .models import Goal, InvestmentPositionState, InvestmentTransaction, Transaction
from .portfolio import build_positions
from .services import ensure_user, monthly_totals, parse_expense_message

router = Router()


def money(value: Decimal, currency: str) -> str:
    return f"{Decimal(value):,.2f} {currency}".replace(",", " ")


def app_url() -> str:
    return get_settings().public_app_url.rstrip("/")



async def bot_user(message: Message):
    async with SessionLocal() as session:
        user = await ensure_user(
            session,
            message.from_user.id,
            message.from_user.first_name,
            message.from_user.username,
        )
        await session.commit()
        return user


async def configure_commands(bot: Bot) -> None:
    from aiogram.types import BotCommand
    await bot.set_my_commands([
        BotCommand(command="start", description="Start finplan"),
        BotCommand(command="app", description="Open Mini App"),
        BotCommand(command="summary", description="Monthly summary"),
        BotCommand(command="goal", description="Create a savings goal"),
        BotCommand(command="portfolio", description="View portfolio"),
    ])



@router.message(CommandStart())
async def start(message: Message) -> None:
    await bot_user(message)
    text = (
        "<b>Привет! Это finplan.</b>\n\n"
        "Деньги без шума — простой способ держать личные финансы под контролем.\n\n"
        "Здесь ты можешь записывать расходы и доходы, следить за бюджетом, "
        "создавать финансовые цели и управлять инвестиционным портфелем. "
        "В Mini App также доступны поиск активов и watchlist.\n\n"
        "Открывай приложение через синюю кнопку <b>finplan</b> внизу чата. "
        "А быстрые расходы можно записывать прямо сообщением, например: "
        "<code>120 coffee</code>."
    )
    await message.answer(text, reply_markup=ReplyKeyboardRemove())


@router.message(Command("app"))
async def open_app(message: Message) -> None:
    url = app_url()
    if url.startswith("https://"):
        await message.answer("Открывай Mini App через синюю кнопку <b>finplan</b> внизу чата.")
    else:
        await message.answer(
            "Mini App пока не получил публичный HTTPS-адрес. "
            "Запусти START_FINPLAN.bat — он настроит туннель автоматически."
        )


@router.message(Command("summary"))
async def summary(message: Message) -> None:
    async with SessionLocal() as session:
        user = await ensure_user(
            session, message.from_user.id, message.from_user.first_name, message.from_user.username
        )
        income, expenses = await monthly_totals(session, user.id)
        balance = Decimal(user.opening_balance or 0) + income - expenses
        budget = (
            f"\nОсталось бюджета: <b>{money(Decimal(user.monthly_budget) - expenses, user.currency)}</b>"
            if user.monthly_budget is not None
            else ""
        )
        await message.answer(
            f"<b>Этот месяц</b>\n"
            f"Доходы: {money(income, user.currency)}\n"
            f"Расходы: {money(expenses, user.currency)}\n"
            f"Баланс: <b>{money(balance, user.currency)}</b>{budget}"
        )


@router.message(Command("goal"))
async def goal(message: Message) -> None:
    payload = (message.text or "").removeprefix("/goal").strip()
    if not payload or " " not in payload:
        await message.answer("Формат: <code>/goal Ноутбук 50000</code>")
        return
    title, raw_amount = payload.rsplit(" ", 1)
    try:
        amount = Decimal(raw_amount.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except Exception:
        await message.answer("Сумма должна быть положительным числом.")
        return

    async with SessionLocal() as session:
        user = await ensure_user(
            session, message.from_user.id, message.from_user.first_name, message.from_user.username
        )
        session.add(Goal(user_id=user.id, title=title, target_amount=amount))
        await session.commit()
        currency = user.currency

    await message.answer(f"Цель «{title}» на {money(amount, currency)} создана.")


@router.message(Command("portfolio"))
async def portfolio(message: Message) -> None:
    async with SessionLocal() as session:
        user = await ensure_user(
            session, message.from_user.id, message.from_user.first_name, message.from_user.username
        )
        trades = list(
            (
                await session.scalars(
                    select(InvestmentTransaction).where(
                        InvestmentTransaction.user_id == user.id
                    )
                )
            ).all()
        )

    hidden_rows = list((await session.scalars(
        select(InvestmentPositionState).where(InvestmentPositionState.user_id == user.id, InvestmentPositionState.hidden.is_(True))
    )).all())
    hidden_keys = {(row.symbol.upper(), row.currency.upper()) for row in hidden_rows}
    positions = [position for position in build_positions(trades) if position.quantity > 0 and (position.symbol.upper(), position.currency.upper()) not in hidden_keys]
    if not positions:
        await message.answer(
            "Портфель пока пуст.\nДобавь покупку через Mini App → Invest."
        )
        return

    lines = [
        f"<b>{item.symbol}</b> · {item.quantity.normalize()} · "
        f"средняя {money(item.average_cost, item.currency)}"
        for item in positions
    ]
    await message.answer(
        "<b>Портфель</b>\n"
        + "\n".join(lines)
        + "\n\nКотировки могут быть задержаны. Не инвестиционная рекомендация."
    )


@router.message(F.text & ~F.text.startswith("/"))
async def quick_expense(message: Message) -> None:
    parsed = parse_expense_message(message.text or "")
    if not parsed:
        return

    kind, amount, note, category = parsed
    async with SessionLocal() as session:
        user = await ensure_user(
            session, message.from_user.id, message.from_user.first_name, message.from_user.username
        )
        session.add(
            Transaction(
                user_id=user.id,
                kind=kind,
                amount=amount,
                note=note,
                category=category,
            )
        )
        await session.commit()
        currency = user.currency

    await message.answer(
        f"Записал <b>{money(amount, currency)}</b>\n{category} · {note}"
    )


async def main() -> None:
    settings = get_settings()
    if not settings.bot_token:
        raise RuntimeError("BOT_TOKEN is missing in .env")

    await init_database()
    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    await configure_commands(bot)

    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    logging.info("finplan bot started")
    await dispatcher.start_polling(bot)


async def run_railway() -> None:
    """Run both the HTTP app and Telegram polling when Railway invokes bot.py directly."""
    import os
    import uvicorn

    port = int(os.getenv("PORT", "8000"))

    async def run_bot_safe() -> None:
        try:
            await main()
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Telegram bot stopped with an error; web server will remain running")

    config = uvicorn.Config(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=True,
    )
    server = uvicorn.Server(config)
    bot_task = asyncio.create_task(run_bot_safe(), name="telegram-bot")
    try:
        logging.info("Starting FinPlan web server on 0.0.0.0:%s", port)
        await server.serve()
    finally:
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_railway())
