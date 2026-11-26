import os
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: str = ""
    public_app_url: str = ""

    @field_validator("bot_token", mode="before")
    @classmethod
    def _strip_token(cls, value: str | None) -> str:
        return (value or "").strip().strip('"').strip("'")

    @field_validator("public_app_url", mode="before")
    @classmethod
    def _normalize_public_url(cls, value: str | None) -> str:
        """Accept `my-app.up.railway.app`, `http://…`, quotes or a trailing slash.

        Falls back to RAILWAY_PUBLIC_DOMAIN, which Railway sets automatically
        once the service has a public domain.
        """
        url = (value or "").strip().strip('"').strip("'").rstrip("/")
        if not url:
            url = os.getenv("RAILWAY_PUBLIC_DOMAIN", "").strip().rstrip("/")
        if not url:
            return ""
        if url.startswith("http://") and not url.startswith(("http://localhost", "http://127.0.0.1")):
            url = "https://" + url.removeprefix("http://")
        elif not url.startswith(("https://", "http://")):
            url = "https://" + url
        return url
    finnhub_api_key: str = ""
    database_url: str = "sqlite+aiosqlite:///./finplan.db"
    debug: bool = False
    dev_telegram_user_id: int | None = None
    webapp_auth_max_age_seconds: int = 86_400
    # Comma-separated Telegram user ids that always get Premium (the owner, testers).
    admin_telegram_ids: str = ""
    # Shown by /paysupport, e.g. "@my_username". Telegram requires bots that sell to offer payment support.
    support_contact: str = ""

    @property
    def admin_ids(self) -> set[int]:
        return {int(x) for x in self.admin_telegram_ids.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}


@lru_cache
def get_settings() -> Settings:
    return Settings()

