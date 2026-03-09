"""
database.py — Async MongoDB helpers (motor) for users and recording tasks.

Collections:
  • users — user profiles, tier (default / verified / premium), stats
  • tasks — recording jobs and their lifecycle state
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

import config

logger = logging.getLogger(__name__)

# Module-level singletons set by connect_db()
_client: Optional[AsyncIOMotorClient] = None
db: Optional[AsyncIOMotorDatabase] = None


# ─────────────────────────────────────────────────────────────────────────────
# Connection helpers
# ─────────────────────────────────────────────────────────────────────────────

async def connect_db() -> None:
    """Initialise the Motor client and create indexes."""
    global _client, db
    _client = AsyncIOMotorClient(config.MONGO_URL)
    db = _client[config.DB_NAME]

    # Ensure unique index on user_id and task_id to prevent duplicates
    await db.users.create_index("user_id", unique=True)
    await db.tasks.create_index("task_id", unique=True)
    await db.tasks.create_index("user_id")
    await db.tasks.create_index("status")
    logger.info("MongoDB connected: %s / %s", config.MONGO_URL, config.DB_NAME)


async def close_db() -> None:
    """Close the Motor client."""
    global _client
    if _client:
        _client.close()
        logger.info("MongoDB connection closed.")


# ─────────────────────────────────────────────────────────────────────────────
# User operations
# ─────────────────────────────────────────────────────────────────────────────

async def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Return the user document or None."""
    return await db.users.find_one({"user_id": user_id}, {"_id": 0})


async def get_or_create_user(
    user_id: int,
    username: Optional[str] = None,
    full_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Return an existing user or insert a new default-tier user."""
    user = await get_user(user_id)
    if user:
        return user

    user = {
        "user_id": user_id,
        "username": username,
        "full_name": full_name,
        "tier": "default",
        "created_at": datetime.now(timezone.utc),
        "verified_until": None,
        "total_recordings": 0,
    }
    await db.users.insert_one(user)
    user.pop("_id", None)
    return user


async def get_user_tier(user_id: int) -> str:
    """Return the user's effective tier, expiring 'verified' if overdue."""
    user = await get_user(user_id)
    if not user:
        return "default"

    tier = user.get("tier", "default")

    # Auto-downgrade expired verified tier
    if tier == "verified":
        exp = user.get("verified_until")
        if exp and datetime.now(timezone.utc) > exp:
            await set_user_tier(user_id, "default")
            return "default"

    return tier


async def set_user_tier(user_id: int, tier: str) -> None:
    """Set a user's tier. Creates the user document if it doesn't exist."""
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"tier": tier, "updated_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


async def count_all_users() -> int:
    return await db.users.count_documents({})


async def get_premium_users() -> List[Dict[str, Any]]:
    cursor = db.users.find({"tier": "premium"}, {"_id": 0})
    return await cursor.to_list(length=500)


async def get_all_users(limit: int = 1000) -> List[Dict[str, Any]]:
    cursor = db.users.find({}, {"_id": 0})
    return await cursor.to_list(length=limit)


# ─────────────────────────────────────────────────────────────────────────────
# Task operations
# ─────────────────────────────────────────────────────────────────────────────

async def create_task(task_data: Dict[str, Any]) -> str:
    """Insert a new task document and return its task_id."""
    await db.tasks.insert_one(task_data)
    return task_data["task_id"]


async def get_task(task_id: str) -> Optional[Dict[str, Any]]:
    return await db.tasks.find_one({"task_id": task_id}, {"_id": 0})


async def update_task(task_id: str, updates: Dict[str, Any]) -> None:
    updates["updated_at"] = datetime.now(timezone.utc)
    await db.tasks.update_one({"task_id": task_id}, {"$set": updates})


async def get_user_active_tasks(user_id: int) -> List[Dict[str, Any]]:
    cursor = db.tasks.find(
        {"user_id": user_id, "status": {"$in": ["queued", "recording", "uploading"]}},
        {"_id": 0},
    )
    return await cursor.to_list(length=100)


async def get_user_all_tasks(user_id: int, limit: int = 20) -> List[Dict[str, Any]]:
    cursor = db.tasks.find({"user_id": user_id}, {"_id": 0}).sort("created_at", -1).limit(limit)
    return await cursor.to_list(length=limit)


async def count_user_active_tasks(user_id: int) -> int:
    return await db.tasks.count_documents(
        {"user_id": user_id, "status": {"$in": ["queued", "recording", "uploading"]}}
    )


async def get_all_active_tasks(skip: int = 0, limit: int = 10) -> List[Dict[str, Any]]:
    cursor = (
        db.tasks.find(
            {"status": {"$in": ["queued", "recording", "uploading"]}},
            {"_id": 0},
        )
        .sort("created_at", 1)
        .skip(skip)
        .limit(limit)
    )
    return await cursor.to_list(length=limit)


async def count_all_active_tasks() -> int:
    return await db.tasks.count_documents(
        {"status": {"$in": ["queued", "recording", "uploading"]}}
    )


async def get_task_ffmpeg_log(task_id: str) -> Optional[str]:
    task = await get_task(task_id)
    if task:
        return task.get("ffmpeg_log", "")
    return None


async def increment_user_recordings(user_id: int) -> None:
    """Increment total_recordings counter for a user."""
    await db.users.update_one(
        {"user_id": user_id}, {"$inc": {"total_recordings": 1}}
    )
