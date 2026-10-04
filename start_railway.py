import asyncio
import os

import uvicorn

from app.bot import main as bot_main


async def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    server = uvicorn.Server(
        uvicorn.Config(
            "app.main:app",
            host="0.0.0.0",
            port=port,
            log_level="info",
        )
    )
    await asyncio.gather(server.serve(), bot_main())


if __name__ == "__main__":
    asyncio.run(main())
