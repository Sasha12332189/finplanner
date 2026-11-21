# FINPLAN

Telegram Mini App + bot for personal finance tracking.

## Railway

This repository runs the FastAPI Mini App and the Telegram bot in one Railway service. The bot receives updates through a webhook at `PUBLIC_APP_URL/telegram/webhook`, which the server sets on every start.

Do not run `python -m app.bot` (polling) at the same time as the Railway service: it removes the webhook and the deployed bot stops answering until Railway restarts.

The "Open" button in the bot's profile and chat list comes from BotFather, not from this code. If it opens a dead link, set it in @BotFather → /mybots → your bot → Bot Settings → Configure Mini App → Edit Mini App URL, using the same URL as `PUBLIC_APP_URL`.

Set these Railway Variables:

- `BOT_TOKEN` — Telegram bot token
- `PUBLIC_APP_URL` — the exact public HTTPS Railway URL, for example `https://your-service.up.railway.app`
- `FINNHUB_API_KEY` — optional
- `DATABASE_URL` — **set this.** In Railway: New → Database → PostgreSQL, then in this service's Variables add `DATABASE_URL` = `${{Postgres.DATABASE_URL}}`. The app converts the URL for its async driver and creates/updates the tables itself.

Without `DATABASE_URL` the app uses a SQLite file inside the container, and **Railway erases it on every deploy**. If you prefer SQLite, attach a Volume to the service; the app then stores `finplan.db` on it automatically (`RAILWAY_VOLUME_MOUNT_PATH`). The deploy log warns `DATA IS NOT PERSISTENT` while neither is set.

The server also runs a background job every 30 minutes: it records due recurring payments and, on the 1st of each month after 10:00 (Kyiv time), sends each user a summary of the previous month in the bot.

Railway starts the service with `start_railway.py`, which runs Uvicorn on Railway's `PORT`.

**Never commit `.env` or a real bot token to GitHub.**

## Local Windows

Use the separate personal/local package if you want the ngrok launcher and local `.env` workflow.
