"""MongoDB helpers – user tiers, tasks, admin list."""

from datetime import datetime, timezone
from typing import Optional

import motor.motor_asyncio

from bot.config import (
    MONGO_URI,
    DB_NAME,
    DEFAULT_MAX_DURATION,
    DEFAULT_MAX_TASKS,
    VERIFIED_MAX_DURATION,
    VERIFIED_MAX_TASKS,
    PREMIUM_MAX_DURATION,
    PREMIUM_MAX_TASKS,
)

_client: Optional[motor.motor_asyncio.AsyncIOMotorClient] = None
_db: Optional[motor.motor_asyncio.AsyncIOMotorDatabase] = None


async def connect() -> motor.motor_asyncio.AsyncIOMotorDatabase:
    """Initialise and return the database handle."""
    global _client, _db
    if _db is None:
        _client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
        _db = _client[DB_NAME]
        await _db.users.create_index("user_id", unique=True)
        await _db.tasks.create_index("user_id")
        await _db.tasks.create_index("task_id", unique=True)
        await _db.admins.create_index("user_id", unique=True)
        await _db.tokens.create_index("token", unique=True)
    return _db


def get_db() -> motor.motor_asyncio.AsyncIOMotorDatabase:
    """Return the cached database handle (call *connect* first)."""
    if _db is None:
        raise RuntimeError("Database not connected. Call connect() first.")
    return _db


# ── Tier helpers ──────────────────────────────────────────────────────────


TIER_CONFIG = {
    "default": {
        "max_duration": DEFAULT_MAX_DURATION,
        "max_tasks": DEFAULT_MAX_TASKS,
    },
    "verified": {
        "max_duration": VERIFIED_MAX_DURATION,
        "max_tasks": VERIFIED_MAX_TASKS,
    },
    "premium": {
        "max_duration": PREMIUM_MAX_DURATION,
        "max_tasks": PREMIUM_MAX_TASKS,
    },
}


async def get_user(user_id: int) -> dict:
    """Return user document, creating a *default* one if missing."""
    db = get_db()
    user = await db.users.find_one({"user_id": user_id})
    if user is None:
        user = {
            "user_id": user_id,
            "tier": "default",
            "created_at": datetime.now(timezone.utc),
            "verified_until": None,
        }
        await db.users.insert_one(user)
    return user


async def set_tier(user_id: int, tier: str) -> None:
    db = get_db()
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"tier": tier}},
        upsert=True,
    )


async def get_tier(user_id: int) -> str:
    user = await get_user(user_id)
    return user.get("tier", "default")


async def tier_limits(user_id: int) -> dict:
    tier = await get_tier(user_id)
    return TIER_CONFIG.get(tier, TIER_CONFIG["default"])


# ── Admin helpers ─────────────────────────────────────────────────────────


async def is_admin(user_id: int) -> bool:
    db = get_db()
    return await db.admins.find_one({"user_id": user_id}) is not None


async def add_admin(user_id: int) -> None:
    db = get_db()
    await db.admins.update_one(
        {"user_id": user_id},
        {"$set": {"user_id": user_id}},
        upsert=True,
    )


async def remove_admin(user_id: int) -> None:
    db = get_db()
    await db.admins.delete_one({"user_id": user_id})


# ── Task helpers ──────────────────────────────────────────────────────────


async def save_task(task: dict) -> None:
    db = get_db()
    await db.tasks.update_one(
        {"task_id": task["task_id"]},
        {"$set": task},
        upsert=True,
    )


async def get_task(task_id: str) -> Optional[dict]:
    db = get_db()
    return await db.tasks.find_one({"task_id": task_id})


async def delete_task(task_id: str) -> None:
    db = get_db()
    await db.tasks.delete_one({"task_id": task_id})


async def user_active_tasks(user_id: int) -> list[dict]:
    db = get_db()
    cursor = db.tasks.find(
        {"user_id": user_id, "status": {"$in": ["queued", "recording"]}}
    )
    return await cursor.to_list(length=100)


async def all_active_tasks(skip: int = 0, limit: int = 10) -> list[dict]:
    db = get_db()
    cursor = (
        db.tasks.find({"status": {"$in": ["queued", "recording"]}})
        .skip(skip)
        .limit(limit)
    )
    return await cursor.to_list(length=limit)


async def count_active_tasks() -> int:
    db = get_db()
    return await db.tasks.count_documents(
        {"status": {"$in": ["queued", "recording"]}}
    )


# ── Token helpers (shortlink verification) ────────────────────────────────


async def store_token(token: str, user_id: int) -> None:
    db = get_db()
    await db.tokens.update_one(
        {"token": token},
        {"$set": {"token": token, "user_id": user_id, "used": False}},
        upsert=True,
    )


async def consume_token(token: str) -> bool:
    """Mark a token as used. Returns True on success."""
    db = get_db()
    result = await db.tokens.update_one(
        {"token": token, "used": False},
        {"$set": {"used": True}},
    )
    return result.modified_count == 1
