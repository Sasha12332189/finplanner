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
- `DATABASE_URL` — optional; the default is SQLite

Railway starts the service with `start_railway.py`, which runs Uvicorn on Railway's `PORT`.

**Never commit `.env` or a real bot token to GitHub.**

## Local Windows

Use the separate personal/local package if you want the ngrok launcher and local `.env` workflow.
