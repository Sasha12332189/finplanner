import logging
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import get_settings


class Base(DeclarativeBase):
    pass


DEFAULT_SQLITE_URL = "sqlite+aiosqlite:///./finplan.db"


def resolve_database_url(raw: str) -> str:
    """Pick a database that survives Railway redeploys.

    * Railway Postgres gives `postgres://` / `postgresql://` URLs; SQLAlchemy's async
      engine needs the `postgresql+asyncpg://` driver, and asyncpg rejects `sslmode`.
    * Without Postgres, a SQLite file inside the container is wiped on every deploy,
      so it is moved onto a Railway Volume when one is mounted.
    """
    url = (raw or "").strip() or DEFAULT_SQLITE_URL
    if url.startswith(("postgres://", "postgresql://")):
        url = "postgresql+asyncpg://" + url.split("://", 1)[1]
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "sslmode"]
        url = urlunsplit(parts._replace(query=urlencode(query)))
    volume = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if url == DEFAULT_SQLITE_URL and volume:
        url = f"sqlite+aiosqlite:///{volume.rstrip('/')}/finplan.db"
    return url


settings = get_settings()
DATABASE_URL = resolve_database_url(settings.database_url)
engine = create_async_engine(DATABASE_URL, future=True, pool_pre_ping=not DATABASE_URL.startswith("sqlite"))
SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# Columns added after the first release. create_all() creates missing tables but never
# alters existing ones, so these are added by hand (works for SQLite and Postgres).
COLUMN_MIGRATIONS = {
    "users": {
        "language": "VARCHAR(8) DEFAULT 'uk'",
        "theme": "VARCHAR(16) DEFAULT 'system'",
        "opening_balance": "NUMERIC(16, 2) DEFAULT 0",
        "investment_capital": "NUMERIC(16, 2) DEFAULT 0",
        "last_summary_month": "VARCHAR(7)",
    },
    "investment_transactions": {
        "account_amount": "NUMERIC(20, 8)",
    },
    "transactions": {
        "account_id": "INTEGER",
    },
}


def storage_warning() -> str | None:
    if DATABASE_URL.startswith("sqlite") and (os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID")):
        if not os.getenv("RAILWAY_VOLUME_MOUNT_PATH"):
            return (
                "DATA IS NOT PERSISTENT: SQLite lives inside the container and is erased on every "
                "Railway deploy. Add a Postgres database (DATABASE_URL) or a Volume to this service."
            )
    return None


async def init_database() -> None:
    from . import models  # noqa: F401

    warning = storage_warning()
    if warning:
        logging.warning(warning)
    logging.info("Database: %s", DATABASE_URL.split("://", 1)[0])

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

        def existing_columns(sync_connection) -> dict[str, set[str]]:
            inspector = inspect(sync_connection)
            return {
                table: {column["name"] for column in inspector.get_columns(table)}
                for table in COLUMN_MIGRATIONS
                if inspector.has_table(table)
            }

        columns = await connection.run_sync(existing_columns)
        for table, wanted in COLUMN_MIGRATIONS.items():
            for name, ddl in wanted.items():
                if table in columns and name not in columns[table]:
                    await connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


async def close_database() -> None:
    await engine.dispose()
