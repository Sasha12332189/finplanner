import os

import uvicorn

# The web server also runs the Telegram bot through its webhook (see app/main.py).
# Do not start polling here: polling deletes the webhook and the two fight over updates.
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info", access_log=True)
