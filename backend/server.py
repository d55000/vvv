"""
server.py — FastAPI entry point.

Uvicorn starts this file. The lifespan context manager:
  1. Connects to MongoDB
  2. Starts the Pyrogram Telegram bot client
  3. Launches the asyncio worker pool
  4. Tears everything down cleanly on shutdown

A /api/health endpoint is exposed so platform health checks stay green.
"""

import asyncio
import logging
import os

from contextlib import asynccontextmanager
from fastapi import FastAPI

# Ensure .env is loaded before importing any project modules
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import database
import worker
from bot import pyro_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup → yield → shutdown lifecycle."""
    logger.info("Starting M3U8 Recorder Bot …")

    # 1. MongoDB
    await database.connect_db()

    # 2. Pyrogram client (non-blocking start within the uvicorn event loop)
    await pyro_app.start()
    logger.info("Pyrogram bot started: @%s", (await pyro_app.get_me()).username)

    # 3. Worker pool
    await worker.start_workers(pyro_app)

    yield  # ← server is live

    # Shutdown
    logger.info("Shutting down …")
    await worker.stop_workers()
    await pyro_app.stop()
    await database.close_db()
    logger.info("Shutdown complete.")


app = FastAPI(
    title="M3U8 Recorder Bot",
    description="HLS stream recorder Telegram bot powered by FFmpeg.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/api/health")
async def health_check():
    """Platform health-check endpoint."""
    active = await database.count_all_active_tasks()
    return {
        "status": "ok",
        "active_tasks": active,
        "workers": worker.task_queue.qsize(),
    }


@app.get("/api/stats")
async def bot_stats():
    """Quick stats endpoint."""
    users = await database.count_all_users()
    active = await database.count_all_active_tasks()
    return {
        "total_users": users,
        "active_tasks": active,
        "queue_depth": worker.task_queue.qsize(),
    }
