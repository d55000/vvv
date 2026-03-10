"""M3U8 Recorder Telegram Bot – Configuration."""

import os
from dotenv import load_dotenv

load_dotenv()

# Telegram
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# MongoDB
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "m3u8_recorder")

# Bot owner / admin IDs (comma‑separated)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

# Worker queue
NUM_WORKERS = int(os.getenv("NUM_WORKERS", "3"))

# Paths
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
CHANNEL_LIST_DIR = os.getenv("CHANNEL_LIST_DIR", "channel_lists")

# Telegram upload hard‑limit safety margin (bytes)
MAX_FILE_SIZE = float(os.getenv("MAX_FILE_SIZE_GB", "1.95")) * 1024 ** 3

# Duration caps per tier (in minutes → seconds)
DEFAULT_MAX_DURATION = int(os.getenv("DEFAULT_MAX_DURATION", "30")) * 60
VERIFIED_MAX_DURATION = int(os.getenv("VERIFIED_MAX_DURATION", "120")) * 60
PREMIUM_MAX_DURATION = int(os.getenv("PREMIUM_MAX_DURATION", "720")) * 60

# Parallel task caps per tier
DEFAULT_MAX_TASKS = 2
VERIFIED_MAX_TASKS = 2
PREMIUM_MAX_TASKS = 3
ADMIN_MAX_TASKS = int(os.getenv("ADMIN_MAX_TASKS", "10"))

# Progress update interval (seconds)
PROGRESS_INTERVAL = 7

# Shortlink verification
SHORTLINK_API_URL = os.getenv("SHORTLINK_API_URL", "")
SHORTLINK_API_KEY = os.getenv("SHORTLINK_API_KEY", "")

# Default verification validity period (hours) – overridden by /setverify
DEFAULT_VERIFY_HOURS = int(os.getenv("DEFAULT_VERIFY_HOURS", "24"))
