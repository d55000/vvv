"""
config.py — Environment variable loader for M3U8 Recorder Bot.
All settings are read from .env or the OS environment. Missing required values
will raise an error at import time, so misconfiguration fails fast.
"""

import os
from dotenv import load_dotenv

# Load .env from the same directory as this file
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Telegram ───────────────────────────────────────────────────────────────
API_ID: int = int(os.environ["API_ID"])
API_HASH: str = os.environ["API_HASH"]
BOT_TOKEN: str = os.environ["BOT_TOKEN"]
OWNER_ID: int = int(os.environ["OWNER_ID"])

# ── MongoDB ────────────────────────────────────────────────────────────────
MONGO_URL: str = os.environ["MONGO_URL"]
DB_NAME: str = os.environ["DB_NAME"]

# ── Worker pool ────────────────────────────────────────────────────────────
NUM_WORKERS: int = int(os.environ.get("NUM_WORKERS", "3"))

# ── Recording paths ────────────────────────────────────────────────────────
OUTPUT_DIR: str = os.environ.get("OUTPUT_DIR", "/tmp/recordings")
CHANNELS_DIR: str = os.environ.get("CHANNELS_DIR", "/app/channels")

# ── FFmpeg tuning ──────────────────────────────────────────────────────────
# How often (seconds) to edit the Telegram progress message
PROGRESS_UPDATE_INTERVAL: int = int(os.environ.get("PROGRESS_UPDATE_INTERVAL", "10"))

# Max segment length in seconds (45 min keeps files well under 2 GB for most bitrates)
MAX_SEGMENT_SECS: int = int(os.environ.get("MAX_SEGMENT_SECS", "2700"))

# ── User tier limits ───────────────────────────────────────────────────────
# Each tier defines: max recording duration (s), max simultaneous tasks
TIER_LIMITS: dict = {
    "default":  {"max_duration": 1800,  "max_tasks": 1},   # 30 min
    "verified": {"max_duration": 7200,  "max_tasks": 2},   # 2 hours
    "premium":  {"max_duration": 43200, "max_tasks": 3},   # 12 hours
}

# ── Admin IDs ─────────────────────────────────────────────────────────────
# Comma-separated list of extra admin IDs (OWNER_ID is always admin)
_extra_admins = [
    int(x) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]
ADMIN_IDS: list = list(set([OWNER_ID] + _extra_admins))

# ── HTTP headers for FFmpeg / FFprobe ──────────────────────────────────────
FFMPEG_USER_AGENT: str = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
FFMPEG_REFERER: str = os.environ.get("FFMPEG_REFERER", "https://www.google.com/")
