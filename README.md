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

## Subscription: finplan Pro / Premium (Telegram Stars)

| | Free | Pro — 250 ⭐ / 30 days (≈ $5) | Premium — 350 ⭐ / 30 days (≈ $7) |
|---|---|---|---|
| Extra accounts | 1 | unlimited | unlimited |
| Goals | 3 | unlimited | unlimited |
| Category limits | 2 | unlimited | unlimited |
| Recurring payments | 3 | unlimited | unlimited |
| Analytics | this + last month | any month | any month |
| Monthly summary in the bot | — | ✓ | ✓ |
| Different assets in the portfolio | 3 | 3 | unlimited |
| Watchlist | 3 | 3 | unlimited |

Limits only block adding new items; nothing is deleted when a plan ends. Plans and limits live in `app/subscriptions.py`.

How payment works: the Mini App calls `POST /api/subscription/invoice`, the server creates a Stars invoice link with a 30-day `subscription_period`, and the app opens it with `Telegram.WebApp.openInvoice`. The bot approves the `pre_checkout_query`, and on `successful_payment` (also sent for every monthly renewal) it activates or extends the plan and records it in the `payments` table. Users can turn auto-renewal off in Settings (or in Telegram → Settings → My Stars); the plan stays active until the paid period ends. Switching Pro → Premium cancels the old subscription's renewal. The bot also answers `/premium`, `/terms` and `/paysupport`.

No payment provider setup is needed for Stars. Optional variables:

- `SUPPORT_CONTACT` — who to contact about payments, shown by `/paysupport` (for example `@your_username`). Telegram requires bots that sell to offer payment support.
- `ADMIN_TELEGRAM_IDS` — comma-separated Telegram user ids that always get Premium (you, testers).

To refund someone, use the Bot API `refundStarPayment` with their user id and the `telegram_charge_id` from the `payments` table.

**Never commit `.env` or a real bot token to GitHub.**

## Local Windows

Use the separate personal/local package if you want the ngrok launcher and local `.env` workflow.
