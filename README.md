# FINPLAN

Telegram Mini App + bot for personal finance tracking.

## Railway

This repository is configured to run both the FastAPI Mini App and Telegram polling bot in one Railway service.

Set these Railway Variables:

- `BOT_TOKEN` — Telegram bot token
- `PUBLIC_APP_URL` — the exact public HTTPS Railway URL, for example `https://your-service.up.railway.app`
- `FINNHUB_API_KEY` — optional
- `DATABASE_URL` — optional; the default is SQLite

Railway starts the service with `start_railway.py`. The process runs Uvicorn on Railway's `PORT` and the Telegram bot polling loop at the same time.

**Never commit `.env` or a real bot token to GitHub.**

## Local Windows

Use the separate personal/local package if you want the ngrok launcher and local `.env` workflow.
