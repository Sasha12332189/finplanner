import asyncio
import logging
import os

import uvicorn

from app.bot import main as bot_main

logging.basicConfig(level=logging.INFO)


async def run_bot() -> None:
    try:
        await bot_main()
    except asyncio.CancelledError:
        raise
    except Exception:
        logging.exception("Telegram bot stopped with an error; web server will remain running")


async def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    config = uvicorn.Config(
        "app.main:app",
        host="0.0.0.0",
        port=port,
        log_level="info",
        access_log=True,
    )
    server = uvicorn.Server(config)
    bot_task = asyncio.create_task(run_bot(), name="telegram-bot")
    try:
        logging.info("Starting FinPlan web server on 0.0.0.0:%s", port)
        await server.serve()
    finally:
        bot_task.cancel()
        await asyncio.gather(bot_task, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(main())
