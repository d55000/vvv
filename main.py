"""M3U8 Recorder Bot – entry point."""

import logging
import os

from pyrogram import Client, idle

from bot.config import API_HASH, API_ID, BOT_TOKEN, DOWNLOAD_DIR, NUM_WORKERS
from bot.db.database import cleanup_stale_tasks, connect
from bot.handlers import admin, user
from bot.utils.worker import start_workers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)


app = Client(
    "m3u8bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


async def main() -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    # Database
    await connect()
    log.info("MongoDB connected.")

    # Remove stale tasks from a previous run
    stale = await cleanup_stale_tasks()
    if stale:
        log.info("Cleaned up %d stale task(s) from DB.", stale)

    # Register handlers
    user.register(app)
    admin.register(app)

    # Start workers
    async with app:
        start_workers(app, NUM_WORKERS)
        log.info("Bot is running. Press Ctrl+C to stop.")
        await idle()


if __name__ == "__main__":
    app.run(main())
