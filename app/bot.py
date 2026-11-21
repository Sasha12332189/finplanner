from __future__ import annotations

import asyncio
import logging
import os
from decimal import Decimal

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from sqlalchemy import select

from .config import get_settings
from .database import SessionLocal, init_database
from .models import Goal, InvestmentPositionState, InvestmentTransaction, Transaction
from .portfolio import build_positions
from .services import ensure_user, monthly_totals, parse_expense_message

router = Router()

dispatcher = Dispatcher()
dispatcher.include_router(router)


def money(value: Decimal, currency: str) -> str:
    return f"{Decimal(value):,.2f} {currency}".replace(",", " ")


def app_url() -> str:
    return get_settings().public_app_url.rstrip("/")


def open_app_keyboard() -> InlineKeyboardMarkup | None:
    """Inline button that opens the Mini App at the exact current PUBLIC_APP_URL."""
    url = app_url()
    if not url.startswith("https://"):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть finplan", web_app=WebAppInfo(url=url))]]
    )



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
    settings = get_settings()
    await bot.set_my_commands([
        BotCommand(command="start", description="Start finplan"),
        BotCommand(command="app", description="Open Mini App"),
        BotCommand(command="summary", description="Monthly summary"),
        BotCommand(command="goal", description="Create a savings goal"),
        BotCommand(command="portfolio", description="View portfolio"),
    ])

    # Keep Telegram's blue Menu button synchronized with the current Railway URL.
    # This must use the exact public HTTPS URL from PUBLIC_APP_URL.
    url = settings.public_app_url.rstrip("/")
    if not url.startswith("https://"):
        # Do not crash the service over this: the bot still answers, only the
        # blue Menu button is left as it is until PUBLIC_APP_URL is fixed.
        logging.warning("PUBLIC_APP_URL is not an HTTPS URL (%r); Menu button not updated", url)
        return
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="finplan",
            web_app=WebAppInfo(url=url),
        )
    )



@router.message(CommandStart())
async def start(message: Message) -> None:
    try:
        await bot_user(message)
    except Exception:
        # A database hiccup must not leave /start without an answer.
        logging.exception("Failed to register user on /start")

    # A Menu button set earlier for this particular chat overrides the bot-wide one,
    # so an old URL would keep showing "Not Found". Point this chat at the current app.
    url = app_url()
    if url.startswith("https://"):
        try:
            await message.bot.set_chat_menu_button(
                chat_id=message.chat.id,
                menu_button=MenuButtonWebApp(text="finplan", web_app=WebAppInfo(url=url)),
            )
        except Exception:
            logging.exception("Failed to reset the chat Menu button")

    text = (
        "<b>finplan</b> — учёт личных финансов.\n\n"
        "Открой приложение кнопкой <b>finplan</b> внизу чата.\n"
        "Расход можно записать сообщением: <code>120 такси</code>"
    )
    await message.answer(text)


@router.message(Command("app"))
async def open_app(message: Message) -> None:
    keyboard = open_app_keyboard()
    if keyboard is not None:
        await message.answer("Нажми, чтобы открыть Mini App:", reply_markup=keyboard)
    else:
        await message.answer(
            "Mini App пока не получил публичный HTTPS-адрес. "
            "Проверь переменную PUBLIC_APP_URL в Railway."
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

    # Polling is for local runs only. getUpdates does not work while a webhook is set,
    # so drop it here; the Railway server sets it again on its next start.
    await bot.delete_webhook(drop_pending_updates=False)
    logging.info("finplan bot started (polling)")
    await dispatcher.start_polling(bot)


def run() -> None:
    """Entry point for `python -m app.bot`.

    On Railway (PORT is set) this starts the full web server: the Mini App plus
    the bot webhook, same as start_railway.py. Polling is only for local runs.
    """
    logging.basicConfig(level=logging.INFO)
    if os.getenv("PORT") or os.getenv("RAILWAY_ENVIRONMENT"):
        import uvicorn

        port = int(os.getenv("PORT", "8000"))
        logging.info("Starting FinPlan web server on 0.0.0.0:%s", port)
        uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info", access_log=True)
    else:
        asyncio.run(main())


if __name__ == "__main__":
    run()

