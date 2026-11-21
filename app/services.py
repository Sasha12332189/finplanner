from __future__ import annotations

import re
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Transaction, User


EXPENSE_PATTERN = re.compile(r"^\s*(?P<prefix>[+-])?\s*(?P<amount>\d+(?:[.,]\d{1,2})?)\s*(?P<note>.+?)\s*$")

# Word stems (ru / uk / en) matched against the start of each word in the note.
# Keys are the category ids used by the Mini App.
EXPENSE_KEYWORDS = {
    "Food": ("еда", "їж", "їда", "кофе", "кава", "каву", "coffee", "cafe", "кафе", "ресторан", "restaurant", "продукт",
             "супермаркет", "сільпо", "сильпо", "атб", "novus", "новус", "ашан", "metro", "фора", "obed", "обед", "обід",
             "ужин", "вечер", "завтрак", "сніданок", "пицц", "піц", "pizza", "суши", "суші", "бургер", "burger",
             "шаурм", "шаверм", "mcdonald", "макдон", "kfc", "глово", "glovo", "bolt food", "доставк", "хлеб", "хліб",
             "молок", "мясо", "м'ясо", "фрукт", "овощ", "овоч", "чай", "пиво", "вино", "food", "lunch", "dinner",
             "breakfast", "grocer", "snack", "перекус"),
    "Transport": ("такси", "таксі", "taxi", "uber", "убер", "bolt", "болт", "uklon", "уклон", "метро", "автобус",
                  "bus", "маршрут", "трамва", "тролейб", "троллейб", "бензин", "fuel", "gas", "заправ", "дизел",
                  "парков", "parking", "транспорт", "проезд", "проїзд", "поезд", "потяг", "укрзал", "train", "ticket"),
    "Home": ("аренд", "оренд", "rent", "квартир", "flat", "коммун", "комун", "ремонт", "мебел", "меблі", "посуд",
             "household", "хоз", "госп"),
    "Bills": ("свет", "світло", "электр", "електр", "газ", "вода", "интернет", "інтернет", "internet", "телефон",
              "мобил", "мобіл", "связь", "зв'язок", "киевстар", "київстар", "kyivstar", "lifecell", "vodafone",
              "водафон", "счет", "рахун", "bill", "налог", "подат", "tax", "штраф", "кредит", "credit", "loan"),
    "Subscriptions": ("подписк", "підписк", "subscription", "netflix", "нетфликс", "spotify", "спотифай", "youtube",
                      "ютуб", "apple", "icloud", "google", "chatgpt", "openai", "claude", "megogo", "мегого",
                      "sweet.tv", "telegram premium", "premium"),
    "Fun": ("кино", "кіно", "cinema", "movie", "игр", "ігр", "game", "steam", "playstation", "xbox", "бар", "bar",
            "клуб", "club", "концерт", "concert", "театр", "theatre", "theater", "кальян", "боулинг", "развлеч",
            "розваг", "fun", "party", "вечеринк", "вечірк"),
    "Health": ("аптек", "pharmacy", "лекарств", "ліки", "таблет", "врач", "лікар", "доктор", "doctor", "стомат",
               "зуб", "dentist", "анализ", "аналіз", "клиник", "клінік", "больниц", "лікарн", "здоров", "health",
               "витамин", "вітамін", "массаж", "масаж"),
    "Shopping": ("покупк", "shopping", "rozetka", "розетк", "prom", "olx", "amazon", "aliexpress", "алиэкспресс",
                 "temu", "маркетплейс", "магазин"),
    "Clothing": ("одежд", "одяг", "clothes", "обувь", "взуття", "shoes", "кроссов", "кросів", "куртк", "джинс",
                 "футболк", "платье", "сукн", "zara", "h&m", "reserved", "sinsay"),
    "Education": ("курс", "course", "обучен", "навчан", "учеб", "школ", "school", "универ", "універ", "книг",
                  "book", "репетит", "урок", "lesson", "education", "english", "англ"),
    "Travel": ("отель", "готел", "hotel", "airbnb", "booking", "билет на самол", "авиа", "авіа", "flight", "самол",
               "літак", "отпуск", "відпуст", "travel", "путешеств", "подорож", "тур", "виза", "віза"),
    "Beauty": ("стрижк", "барбер", "barber", "парикмах", "перукар", "маникюр", "манікюр", "косметик", "beauty",
               "салон", "spa", "спа", "брови", "ресниц", "вії"),
    "Sport": ("спортзал", "зал", "gym", "фитнес", "фітнес", "fitness", "тренир", "тренув", "бассейн", "басейн",
              "pool", "абонем", "йога", "yoga", "спорт", "sport"),
    "Gifts": ("подар", "gift", "present", "цвет", "квіт", "букет", "flowers", "донат", "donat", "благотвор"),
    "Electronics": ("телефон новый", "iphone", "айфон", "samsung", "ноутбук", "laptop", "macbook", "наушник",
                    "навушник", "airpods", "техник", "техні", "electronics", "гаджет", "зарядк", "charger"),
}
INCOME_KEYWORDS = {
    "Salary": ("зарплат", "зп", "salary", "аванс", "оклад", "payroll"),
    "Freelance": ("фриланс", "фріланс", "freelance", "заказ", "замовлен", "upwork", "fiverr", "проект", "проєкт"),
    "Business": ("бизнес", "бізнес", "business", "продаж", "выручк", "виручк", "клиент", "клієнт"),
    "Gift": ("подар", "gift", "present"),
    "Bonus": ("бонус", "bonus", "преми", "премі", "кешбек", "кэшбек", "cashback"),
    "Refund": ("возврат", "поверн", "refund", "компенсац"),
    "Interest": ("процент", "відсот", "interest", "депозит", "deposit"),
    "Dividends": ("дивиденд", "дивіденд", "dividend"),
    "Sale": ("продал", "продав", "sold", "sale", "olx"),
}

WORD_RE = re.compile(r"[\w'.&]+", re.UNICODE)


def infer_category(note: str, kind: str = "expense") -> str:
    lower_note = note.lower()
    words = WORD_RE.findall(lower_note)
    table = INCOME_KEYWORDS if kind == "income" else EXPENSE_KEYWORDS
    for category, stems in table.items():
        for stem in stems:
            if " " in stem:
                if stem in lower_note:
                    return category
            elif any(word.startswith(stem) for word in words):
                return category
    return "Other"


def parse_expense_message(text: str) -> tuple[str, Decimal, str, str] | None:
    match = EXPENSE_PATTERN.match(text)
    if not match:
        return None
    amount = Decimal(match.group("amount").replace(",", "."))
    note = match.group("note").strip()
    if amount <= 0 or not note:
        return None
    kind = "income" if match.group("prefix") == "+" else "expense"
    return kind, amount, note, infer_category(note, kind)


async def ensure_user(session: AsyncSession, telegram_id: int, first_name: str, username: str | None) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user:
        user.first_name = first_name or user.first_name
        user.username = username
        return user
    user = User(telegram_id=telegram_id, first_name=first_name or "friend", username=username)
    session.add(user)
    await session.flush()
    return user


async def monthly_totals(session: AsyncSession, user_id: int) -> tuple[Decimal, Decimal]:
    from .finance import local_today, month_rows

    today = local_today()
    transactions = await month_rows(session, user_id, today.year, today.month)
    expenses = sum((Decimal(item.amount) for item in transactions if item.kind == "expense"), Decimal("0"))
    income = sum((Decimal(item.amount) for item in transactions if item.kind == "income"), Decimal("0"))
    return income, expenses

