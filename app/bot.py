from __future__ import annotations

import asyncio
import html
import logging
import os
from decimal import Decimal

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
    WebAppInfo,
)
from sqlalchemy import select

from . import subscriptions as subs
from .config import get_settings
from .finance import LOCAL_TZ
from .database import SessionLocal, init_database
from .finance import apply_due_recurring, category_name, local_today, month_summary_text, total_balance
from .models import Goal, InvestmentPositionState, InvestmentTransaction, Transaction, User
from .portfolio import build_positions
from .services import ensure_user, parse_expense_message

router = Router()

dispatcher = Dispatcher()
dispatcher.include_router(router)


BOT_WORDS = {
    "ru": {"saved": "Записал", "undo": "Отменить", "undone": "Отменено", "gone": "Уже удалено",
           "balance": "Баланс", "empty": "В этом месяце операций пока нет."},
    "uk": {"saved": "Записав", "undo": "Скасувати", "undone": "Скасовано", "gone": "Вже видалено",
           "balance": "Баланс", "empty": "Цього місяця операцій поки немає."},
}


def bot_language(language: str | None) -> str:
    return "uk" if language == "uk" else "ru"


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
        BotCommand(command="premium", description="finplan Pro / Premium"),
        BotCommand(command="paysupport", description="Help with payments"),
        BotCommand(command="terms", description="Subscription terms"),
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
        await apply_due_recurring(session, user.id)
        await session.commit()
        today = local_today()
        text = await month_summary_text(session, user, today.year, today.month)
        balance = await total_balance(session, user) - Decimal(user.investment_capital or 0)
        currency = user.currency
        words = BOT_WORDS[bot_language(user.language)]
    if not text:
        text = words["empty"]
    await message.answer(f"{text}\n\n{words['balance']}: <b>{money(balance, currency)}</b>")


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
        limit = subs.limits_for(user)["goals"]
        if limit is not None and await subs.count(session, Goal, user.id) >= limit:
            await session.commit()
            await message.answer(
                f"В бесплатной версии можно до {limit} целей. Больше — в finplan Pro: /premium"
            )
            return
        session.add(Goal(user_id=user.id, title=title, target_amount=amount))
        await session.commit()
        currency = user.currency

    await message.answer(f"Цель «{html.escape(title)}» на {money(amount, currency)} создана.")


# ---------- finplan Pro / Premium (Telegram Stars) ----------

PLAN_NAMES = {"pro": "finplan Pro", "premium": "finplan Premium"}


async def premium_keyboard(bot: Bot, telegram_id: int) -> InlineKeyboardMarkup:
    rows = []
    for plan, info in subs.PLANS.items():
        title, description = subs.TITLES[plan]
        link = await bot.create_invoice_link(
            title=title, description=description, payload=f"sub:{plan}:{telegram_id}", currency="XTR",
            prices=[LabeledPrice(label=title, amount=info["stars"])], subscription_period=subs.SUBSCRIPTION_PERIOD,
        )
        rows.append([InlineKeyboardButton(text=f"{title} — {info['stars']} ⭐ / месяц", url=link)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("premium"))
async def premium(message: Message) -> None:
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user.id, message.from_user.first_name, message.from_user.username)
        await session.commit()
        plan = subs.effective_plan(user)
        expires = subs.as_utc(user.plan_expires_at)
    if plan != "free":
        until = f" до {expires.astimezone(LOCAL_TZ):%d.%m.%Y}" if expires else ""
        await message.answer(
            f"У тебя активен <b>{PLAN_NAMES[plan]}</b>{until}.\n"
            "Управлять подпиской можно в Mini App → Настройки или в Telegram: Настройки → Мои звёзды."
        )
        return
    text = (
        "<b>finplan Pro</b> — 250 ⭐ в месяц (≈ $5)\n"
        "Счета, цели, лимиты и регулярные платежи без ограничений, вся история аналитики, итоги месяца в боте.\n\n"
        "<b>finplan Premium</b> — 350 ⭐ в месяц (≈ $7)\n"
        "Всё из Pro и инвестиции без ограничений: любое число активов и избранное.\n\n"
        "Подписка продлевается каждые 30 дней, отменить можно в любой момент."
    )
    try:
        keyboard = await premium_keyboard(message.bot, message.from_user.id)
    except Exception:
        logging.exception("Failed to create invoice links")
        await message.answer(text + "\n\nОплата сейчас недоступна, попробуй позже.")
        return
    await message.answer(text, reply_markup=keyboard)


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    """Telegram asks to confirm within 10 seconds before charging Stars."""
    parsed = subs.parse_payload(query.invoice_payload)
    if (
        parsed is None
        or query.currency != "XTR"
        or parsed[1] != query.from_user.id
        or query.total_amount != subs.PLANS[parsed[0]]["stars"]
    ):
        await query.answer(ok=False, error_message="Этот счёт устарел. Открой оплату заново в finplan.")
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    payment = message.successful_payment
    parsed = subs.parse_payload(payment.invoice_payload)
    if parsed is None or payment.currency != "XTR":
        logging.warning("Unexpected payment payload %r", payment.invoice_payload)
        return
    plan = parsed[0]
    async with SessionLocal() as session:
        user = await ensure_user(session, message.from_user.id, message.from_user.first_name, message.from_user.username)
        is_new, previous_charge = await subs.record_payment(
            session, user, plan, payment.total_amount, payment.telegram_payment_charge_id,
            payment.subscription_expiration_date, bool(payment.is_recurring and not payment.is_first_recurring),
        )
        await session.commit()
        expires = subs.as_utc(user.plan_expires_at)
    if previous_charge:
        # Switched between Pro and Premium: stop the old subscription from renewing.
        try:
            await message.bot.edit_user_star_subscription(
                user_id=message.from_user.id, telegram_payment_charge_id=previous_charge, is_canceled=True,
            )
        except Exception:
            logging.exception("Failed to cancel the previous subscription")
    if not is_new:
        return
    until = f"{expires.astimezone(LOCAL_TZ):%d.%m.%Y}" if expires else ""
    if payment.is_recurring and not payment.is_first_recurring:
        await message.answer(f"{PLAN_NAMES[plan]} продлён до {until}. Спасибо!")
    else:
        await message.answer(
            f"Готово! <b>{PLAN_NAMES[plan]}</b> активен до {until}.\n"
            "Подписка продлится автоматически через 30 дней. Отменить можно в Mini App → Настройки."
        )


@router.message(Command("paysupport"))
async def paysupport(message: Message) -> None:
    contact = get_settings().support_contact.strip()
    where = f"Напиши {html.escape(contact)}" if contact else "Свяжись с владельцем бота (контакт в описании бота)"
    await message.answer(
        "<b>Помощь с оплатой</b>\n"
        f"{where} — укажи дату платежа и что пошло не так. Если подписка не включилась "
        "после оплаты, мы проверим платёж и включим её или вернём звёзды."
    )


@router.message(Command("terms"))
async def terms(message: Message) -> None:
    await message.answer(
        "<b>Условия подписки finplan</b>\n"
        "• Pro — 250 ⭐, Premium — 350 ⭐ за 30 дней. Оплата через Telegram Stars.\n"
        "• Подписка продлевается автоматически каждые 30 дней, пока ты её не отменишь.\n"
        "• После отмены доступ сохраняется до конца оплаченного периода.\n"
        "• Твои данные не удаляются: если подписка закончится, записи останутся, "
        "просто нельзя будет добавлять новые сверх бесплатных лимитов.\n"
        "• finplan — инструмент учёта, а не финансовый или инвестиционный совет.\n"
        "Вопросы по оплате: /paysupport"
    )


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
        row = Transaction(
            user_id=user.id,
            kind=kind,
            amount=amount,
            note=note,
            category=category,
        )
        session.add(row)
        await session.commit()
        currency = user.currency
        language = user.language
        row_id = row.id

    words = BOT_WORDS[bot_language(language)]
    sign = "+" if kind == "income" else "−"
    undo = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=words["undo"], callback_data=f"undo:{row_id}")]]
    )
    await message.answer(
        f"{words['saved']} <b>{sign}{money(amount, currency)}</b>\n"
        f"{category_name(category, language)} · {html.escape(note)}",
        reply_markup=undo,
    )


@router.callback_query(F.data.startswith("undo:"))
async def undo_quick_entry(callback: CallbackQuery) -> None:
    try:
        row_id = int(callback.data.split(":", 1)[1])
    except ValueError:
        await callback.answer()
        return
    async with SessionLocal() as session:
        user = await session.scalar(select(User).where(User.telegram_id == callback.from_user.id))
        words = BOT_WORDS[bot_language(user.language if user else None)]
        row = None
        if user is not None:
            row = await session.scalar(
                select(Transaction).where(Transaction.id == row_id, Transaction.user_id == user.id)
            )
        if row is not None:
            await session.delete(row)
            await session.commit()
    if row is None:
        await callback.answer(words["gone"])
        return
    await callback.answer(words["undone"])
    if callback.message is not None:
        try:
            await callback.message.edit_text("<s>" + (callback.message.html_text or "") + "</s>\n" + words["undone"] + ".")
        except Exception:
            logging.exception("Failed to mark the entry as undone")


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

