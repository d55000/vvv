"""MongoDB helpers – user tiers, tasks, admin list."""

from datetime import datetime, timedelta, timezone
from typing import Optional

import motor.motor_asyncio

from bot.config import (
    MONGO_URI,
    DB_NAME,
    OWNER_ID,
    DEFAULT_MAX_DURATION,
    DEFAULT_MAX_TASKS,
    VERIFIED_MAX_DURATION,
    VERIFIED_MAX_TASKS,
    PREMIUM_MAX_DURATION,
    PREMIUM_MAX_TASKS,
    ADMIN_MAX_TASKS,
    DEFAULT_VERIFY_HOURS,
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
        await _db.auth_groups.create_index("group_id", unique=True)
        await _db.settings.create_index("key", unique=True)
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
    # Owner and admins get elevated limits
    if user_id == OWNER_ID or await is_admin(user_id):
        return {
            "max_duration": PREMIUM_MAX_DURATION,
            "max_tasks": ADMIN_MAX_TASKS,
        }
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


async def delete_user_tasks(user_id: int) -> int:
    """Delete all active tasks for *user_id*. Returns the count removed."""
    db = get_db()
    result = await db.tasks.delete_many(
        {"user_id": user_id, "status": {"$in": ["queued", "recording"]}}
    )
    return result.deleted_count


async def cleanup_stale_tasks() -> int:
    """Remove all tasks still marked queued/recording (stale after restart)."""
    db = get_db()
    result = await db.tasks.delete_many(
        {"status": {"$in": ["queued", "recording"]}}
    )
    return result.deleted_count


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


# ── Auth‑group helpers ────────────────────────────────────────────────────


async def add_auth_group(group_id: int) -> None:
    db = get_db()
    await db.auth_groups.update_one(
        {"group_id": group_id},
        {"$set": {"group_id": group_id}},
        upsert=True,
    )


async def remove_auth_group(group_id: int) -> bool:
    db = get_db()
    result = await db.auth_groups.delete_one({"group_id": group_id})
    return result.deleted_count == 1


async def is_auth_group(group_id: int) -> bool:
    db = get_db()
    return await db.auth_groups.find_one({"group_id": group_id}) is not None


async def get_all_auth_groups() -> list[int]:
    db = get_db()
    cursor = db.auth_groups.find()
    docs = await cursor.to_list(length=200)
    return [d["group_id"] for d in docs]


# ── Bot‑wide settings helpers ─────────────────────────────────────────────


async def get_setting(key: str, default=None):
    db = get_db()
    doc = await db.settings.find_one({"key": key})
    if doc is None:
        return default
    return doc.get("value", default)


async def set_setting(key: str, value) -> None:
    db = get_db()
    await db.settings.update_one(
        {"key": key},
        {"$set": {"key": key, "value": value}},
        upsert=True,
    )


# ── Verification helpers ──────────────────────────────────────────────────


async def get_verify_hours() -> int:
    """Return the current verification validity period in hours."""
    val = await get_setting("verify_hours")
    if val is not None:
        return int(val)
    return DEFAULT_VERIFY_HOURS


async def set_user_verified(user_id: int) -> None:
    """Mark user as verified for the current configured interval."""
    hours = await get_verify_hours()
    until = datetime.now(timezone.utc) + timedelta(hours=hours)
    db = get_db()
    await db.users.update_one(
        {"user_id": user_id},
        {"$set": {"verified_until": until}},
        upsert=True,
    )


async def is_user_verified(user_id: int) -> bool:
    """Check if the user's verification is still valid."""
    user = await get_user(user_id)
    v = user.get("verified_until")
    if v is None:
        return False
    if isinstance(v, datetime):
        return v > datetime.now(timezone.utc)
    return False
