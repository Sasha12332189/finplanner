# Railway startup fix

This build supports Railway configurations that start either `start_railway.py` or `app/bot.py` directly.

Both entry points start the FastAPI/Uvicorn HTTP server on Railway's `PORT` and run Telegram polling separately. A Telegram polling conflict is caught so it cannot prevent the HTTP server from serving the Mini App.

Expected deploy log:

- `Starting FinPlan web server on 0.0.0.0:<PORT>`
- `Uvicorn running on http://0.0.0.0:<PORT>`
- `finplan bot started`

A `TelegramConflictError` can still indicate another polling instance, but it should no longer make the Mini App URL fail to respond.
