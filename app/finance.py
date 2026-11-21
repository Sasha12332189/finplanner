"""Money logic shared by the API and the bot: balances, analytics, recurring payments, summaries."""
from __future__ import annotations

import calendar
from datetime import date, datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Account, CategoryBudget, RecurringPayment, Transaction, User

# Month boundaries, recurring days and the summary are counted in the users' local time.
def _local_tz():
    for key in ("Europe/Kyiv", "Europe/Kiev"):
        try:
            return ZoneInfo(key)
        except Exception:
            continue
    # No tz database at all (the tzdata package is missing): fixed Kyiv winter offset.
    from datetime import timedelta
    return timezone(timedelta(hours=2), "Europe/Kyiv")


LOCAL_TZ = _local_tz()

INFLOW = {"income", "transfer_in"}
OUTFLOW = {"expense", "transfer_out"}

CATEGORY_NAMES = {
    "ru": {"Food": "Еда", "Transport": "Транспорт", "Home": "Дом", "Shopping": "Покупки", "Health": "Здоровье",
           "Fun": "Развлечения", "Bills": "Счета", "Subscriptions": "Подписки", "Clothing": "Одежда",
           "Education": "Образование", "Travel": "Путешествия", "Beauty": "Красота", "Sport": "Спорт",
           "Gifts": "Подарки", "Electronics": "Электроника", "Salary": "Зарплата", "Freelance": "Фриланс",
           "Business": "Бизнес", "Gift": "Подарок", "Bonus": "Бонус", "Refund": "Возврат", "Interest": "Проценты",
           "Dividends": "Дивиденды", "Sale": "Продажа", "Other": "Другое"},
    "uk": {"Food": "Їжа", "Transport": "Транспорт", "Home": "Дім", "Shopping": "Покупки", "Health": "Здоров'я",
           "Fun": "Розваги", "Bills": "Рахунки", "Subscriptions": "Підписки", "Clothing": "Одяг",
           "Education": "Освіта", "Travel": "Подорожі", "Beauty": "Краса", "Sport": "Спорт",
           "Gifts": "Подарунки", "Electronics": "Електроніка", "Salary": "Зарплата", "Freelance": "Фриланс",
           "Business": "Бізнес", "Gift": "Подарунок", "Bonus": "Бонус", "Refund": "Повернення", "Interest": "Відсотки",
           "Dividends": "Дивіденди", "Sale": "Продаж", "Other": "Інше"},
}


def category_name(category: str, language: str | None) -> str:
    return CATEGORY_NAMES.get(language or "ru", {}).get(category, category)


def money(value: Decimal | int | float, currency: str) -> str:
    return f"{Decimal(value):,.2f} {currency}".replace(",", " ")


def local_today() -> date:
    return datetime.now(LOCAL_TZ).date()


def month_key(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def parse_month(value: str | None) -> tuple[int, int]:
    if value:
        year, month = value.split("-")
        return int(year), int(month)
    today = local_today()
    return today.year, today.month


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def month_bounds(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1, tzinfo=LOCAL_TZ)
    next_year, next_month = shift_month(year, month, 1)
    end = datetime(next_year, next_month, 1, tzinfo=LOCAL_TZ)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def signed(row: Transaction) -> Decimal:
    amount = Decimal(row.amount)
    return amount if row.kind in INFLOW else -amount


# ---------- accounts ----------

async def account_balances(session: AsyncSession, user: User) -> list[dict]:
    """Main account (id None) first, then the extra accounts, each with its balance."""
    accounts = list((await session.scalars(
        select(Account).where(Account.user_id == user.id).order_by(Account.created_at)
    )).all())
    rows = list((await session.scalars(select(Transaction).where(Transaction.user_id == user.id))).all())
    known = {a.id for a in accounts}
    totals: dict[int | None, Decimal] = {None: Decimal(user.opening_balance or 0)}
    for account in accounts:
        totals[account.id] = Decimal(account.opening_balance or 0)
    for row in rows:
        key = row.account_id if row.account_id in known else None
        totals[key] += signed(row)
    result = [{"id": None, "name": None, "opening_balance": Decimal(user.opening_balance or 0), "balance": totals[None]}]
    result += [
        {"id": a.id, "name": a.name, "opening_balance": Decimal(a.opening_balance or 0), "balance": totals[a.id]}
        for a in accounts
    ]
    return result


async def total_balance(session: AsyncSession, user: User) -> Decimal:
    return sum((a["balance"] for a in await account_balances(session, user)), Decimal("0"))


async def owned_account_id(session: AsyncSession, user: User, account_id: int | None) -> int | None:
    if account_id is None:
        return None
    account = await session.scalar(select(Account).where(Account.id == account_id, Account.user_id == user.id))
    if account is None:
        raise ValueError("Account not found")
    return account.id


# ---------- analytics ----------

async def month_rows(session: AsyncSession, user_id: int, year: int, month: int) -> list[Transaction]:
    start, end = month_bounds(year, month)
    return list((await session.scalars(
        select(Transaction).where(
            Transaction.user_id == user_id,
            Transaction.occurred_at >= start,
            Transaction.occurred_at < end,
            Transaction.kind.in_(("expense", "income")),
        )
    )).all())


def by_category(rows: list[Transaction], kind: str) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for row in rows:
        if row.kind == kind:
            totals[row.category or "Other"] = totals.get(row.category or "Other", Decimal("0")) + Decimal(row.amount)
    return totals


async def analytics(session: AsyncSession, user: User, month: str | None) -> dict:
    year, mon = parse_month(month)
    prev_year, prev_mon = shift_month(year, mon, -1)
    rows = await month_rows(session, user.id, year, mon)
    prev_rows = await month_rows(session, user.id, prev_year, prev_mon)
    budgets = {
        b.category: Decimal(b.limit_amount)
        for b in (await session.scalars(select(CategoryBudget).where(CategoryBudget.user_id == user.id))).all()
    }
    expenses, prev_expenses = by_category(rows, "expense"), by_category(prev_rows, "expense")
    income, prev_income = by_category(rows, "income"), by_category(prev_rows, "income")
    categories = sorted(set(expenses) | set(budgets), key=lambda c: expenses.get(c, Decimal("0")), reverse=True)
    return {
        "month": f"{year:04d}-{mon:02d}",
        "currency": user.currency,
        "expenses_total": sum(expenses.values(), Decimal("0")),
        "income_total": sum(income.values(), Decimal("0")),
        "prev_expenses_total": sum(prev_expenses.values(), Decimal("0")),
        "prev_income_total": sum(prev_income.values(), Decimal("0")),
        "expenses": [
            {
                "category": c,
                "amount": expenses.get(c, Decimal("0")),
                "prev_amount": prev_expenses.get(c, Decimal("0")),
                "limit": budgets.get(c),
            }
            for c in categories
        ],
        "income": [
            {"category": c, "amount": a, "prev_amount": prev_income.get(c, Decimal("0"))}
            for c, a in sorted(income.items(), key=lambda kv: kv[1], reverse=True)
        ],
    }


# ---------- recurring payments ----------

def due_day(year: int, month: int, day_of_month: int) -> int:
    return min(day_of_month, calendar.monthrange(year, month)[1])


async def apply_due_recurring(session: AsyncSession, user_id: int, today: date | None = None) -> int:
    """Record this month's recurring payments whose day has come. Returns how many were added."""
    today = today or local_today()
    key = month_key(today)
    items = list((await session.scalars(
        select(RecurringPayment).where(RecurringPayment.user_id == user_id)
    )).all())
    added = 0
    for item in items:
        if item.last_applied_month == key:
            continue
        day = due_day(today.year, today.month, item.day_of_month)
        if today.day < day:
            continue
        occurred = datetime(today.year, today.month, day, 12, tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        session.add(Transaction(
            user_id=user_id, kind=item.kind, amount=item.amount, category=item.category,
            note=item.note, account_id=item.account_id, occurred_at=occurred,
        ))
        item.last_applied_month = key
        added += 1
    return added


def first_month_to_skip(day_of_month: int, today: date | None = None) -> str | None:
    """A payment created after its day this month starts next month (it was most likely paid already)."""
    today = today or local_today()
    if today.day > due_day(today.year, today.month, day_of_month):
        return month_key(today)
    return None


# ---------- monthly summary ----------

SUMMARY_TEXT = {
    "ru": {"title": "Итоги за {month}", "income": "Доходы", "expenses": "Расходы", "saved": "Осталось",
           "overspent": "Перерасход", "top": "Больше всего потрачено", "more": "на {pct}% больше, чем в прошлом месяце",
           "less": "на {pct}% меньше, чем в прошлом месяце", "same": "столько же, сколько в прошлом месяце",
           "limits": "Превышены лимиты", "empty": "В этом месяце операций не было."},
    "uk": {"title": "Підсумки за {month}", "income": "Доходи", "expenses": "Витрати", "saved": "Залишилось",
           "overspent": "Перевитрата", "top": "Найбільше витрачено", "more": "на {pct}% більше, ніж минулого місяця",
           "less": "на {pct}% менше, ніж минулого місяця", "same": "стільки ж, скільки минулого місяця",
           "limits": "Перевищено ліміти", "empty": "Цього місяця операцій не було."},
}
MONTH_NAMES = {
    "ru": ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"],
    "uk": ["січень", "лютий", "березень", "квітень", "травень", "червень", "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"],
}


def summary_language(user: User) -> str:
    return "uk" if user.language == "uk" else "ru"


async def month_summary_text(session: AsyncSession, user: User, year: int, month: int) -> str | None:
    """Short HTML summary of a month, or None when nothing was recorded."""
    data = await analytics(session, user, f"{year:04d}-{month:02d}")
    lang = summary_language(user)
    words = SUMMARY_TEXT[lang]
    income, expenses = data["income_total"], data["expenses_total"]
    if not income and not expenses:
        return None
    currency = user.currency
    lines = [f"<b>{words['title'].format(month=MONTH_NAMES[lang][month - 1])}</b>", ""]
    lines.append(f"{words['income']}: {money(income, currency)}")
    lines.append(f"{words['expenses']}: {money(expenses, currency)}")
    rest = income - expenses
    lines.append(f"{words['saved'] if rest >= 0 else words['overspent']}: <b>{money(abs(rest), currency)}</b>")

    prev = data["prev_expenses_total"]
    if prev > 0 and expenses > 0:
        pct = int(round(abs(expenses - prev) / prev * 100))
        comparison = words["same"] if pct == 0 else (words["more"] if expenses > prev else words["less"]).format(pct=pct)
        lines.append(f"{words['expenses']} {comparison}.")

    top = [row for row in data["expenses"] if row["amount"] > 0][:3]
    if top:
        lines += ["", f"{words['top']}:"]
        for row in top:
            share = int(round(row["amount"] / expenses * 100)) if expenses else 0
            lines.append(f"• {category_name(row['category'], lang)} — {money(row['amount'], currency)} ({share}%)")

    over = [row for row in data["expenses"] if row["limit"] and row["amount"] > row["limit"]]
    if over:
        lines += ["", f"{words['limits']}:"]
        for row in over:
            lines.append(f"• {category_name(row['category'], lang)}: {money(row['amount'], currency)} / {money(row['limit'], currency)}")
    return "\n".join(lines)
