"""
bot.py — Pyrogram client + all command and callback handlers.

User Commands:
  /start            Welcome message
  /rec <url>        Analyse stream and show track selection UI
  /cancel <task_id> Cancel a running recording
  /mytasks          List active/queued tasks
  /status           Bot uptime and worker stats
  /verify <token>   Placeholder — shortlink verification
  /search <query>   Search loaded JSON channel lists
  /channel <name>   Record a channel directly from the JSON list

Admin Commands (OWNER_ID / ADMIN_IDS only):
  /auth <user_id>           Grant Premium
  /deauth <user_id>         Remove Premium
  /tasks                    Paginated view of all active tasks
  /add_m3u8                 (Reply to .json) Save channel list
  /remove_m3u8 <filename>   Delete channel list
  /pull <m3u8|log|premium|admin>  Export data as file
  /flog <file|msg> <task_id>       FFmpeg log for a task
  /admin_panel              Inline control panel
"""

import asyncio
import glob
import io
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pyrogram import Client, filters
from pyrogram.errors import FloodWait, MessageNotModified, RPCError
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

import config
import database as db
from ffmpeg_utils import build_track_keyboard, format_duration, run_ffprobe

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Pyrogram client (module-level so server.py can import and start it)
# ─────────────────────────────────────────────────────────────────────────────

pyro_app = Client(
    "sessions/m3u8_bot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir=os.path.dirname(__file__),
)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory state
# ─────────────────────────────────────────────────────────────────────────────

# Pending track selections: user_id → dict
pending_selections: Dict[int, Dict[str, Any]] = {}

# Short task_id prefix → full task_id (for callbacks that truncate to 12 chars)
tid_map: Dict[str, str] = {}

# Search sessions: user_id → {"results": [...], "page": int, "query": str}
search_sessions: Dict[int, Dict[str, Any]] = {}

# Channel URL cache: short_int → url (for /search inline buttons)
_channel_id_counter = 0
channel_cache: Dict[int, str] = {}

# Bot start timestamp
_start_time = time.time()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


def new_task_id() -> str:
    full = str(uuid.uuid4()).replace("-", "")
    tid_map[full[:12]] = full
    return full


def resolve_tid(short: str) -> Optional[str]:
    """Resolve 12-char prefix back to full task_id, or return full id if found."""
    if short in tid_map:
        return tid_map[short]
    # Maybe the caller passed the full id
    if len(short) == 32:
        return short
    # Try prefix lookup in tid_map values
    for k, v in tid_map.items():
        if v.startswith(short) or k == short:
            return v
    return short  # Return as-is for DB lookup


def load_all_channels() -> List[Dict[str, Any]]:
    """Load all JSON channel files from CHANNELS_DIR."""
    channels: List[Dict[str, Any]] = []
    pattern = os.path.join(config.CHANNELS_DIR, "*.json")
    for filepath in glob.glob(pattern):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    channels.extend(data)
        except Exception as exc:
            logger.warning("Failed to load %s: %s", filepath, exc)
    return channels


def register_channel_url(url: str) -> int:
    global _channel_id_counter
    _channel_id_counter += 1
    channel_cache[_channel_id_counter] = url
    return _channel_id_counter


async def check_user_limits(user_id: int, tier: str) -> Optional[str]:
    """Return an error string if the user has exceeded their task limit, else None."""
    limits = config.TIER_LIMITS[tier]
    active = await db.count_user_active_tasks(user_id)
    if active >= limits["max_tasks"]:
        return (
            f"⚠️ You have reached your task limit ({limits['max_tasks']}) for tier **{tier}**.\n"
            "Use /mytasks to see your active recordings."
        )
    return None


WELCOME_TEXT = (
    "👋 **Welcome to M3U8 Recorder Bot!**\n\n"
    "I can record live M3U8 / HLS streams and deliver them directly to you.\n\n"
    "**Quick Start:**\n"
    "• `/rec <url>` — Record any M3U8/HLS stream\n"
    "• `/search <query>` — Search built-in channel list\n"
    "• `/channel <name>` — Record a saved channel\n"
    "• `/mytasks` — View your active recordings\n"
    "• `/status` — Bot health & stats\n\n"
    "**Tiers:**\n"
    "🔹 Default — 30 min, 1 task\n"
    "🔸 Verified — 2 hours, 2 tasks\n"
    "💎 Premium — 12 hours, 3 tasks\n\n"
    "_Send /rec followed by an M3U8 URL to get started!_"
)


# ─────────────────────────────────────────────────────────────────────────────
# ── USER COMMANDS ────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@pyro_app.on_message(filters.command("start") & filters.private)
async def cmd_start(client: Client, message: Message) -> None:
    await db.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name,
    )
    await message.reply_text(WELCOME_TEXT)


@pyro_app.on_message(filters.command("status"))
async def cmd_status(client: Client, message: Message) -> None:
    from worker import task_queue, active_cancel_events

    uptime_secs = int(time.time() - _start_time)
    total_users = await db.count_all_users()
    active_tasks = await db.count_all_active_tasks()
    queue_size = task_queue.qsize()
    running_procs = len(active_cancel_events)

    text = (
        "📊 **Bot Status**\n\n"
        f"⏱ Uptime: `{format_duration(uptime_secs)}`\n"
        f"👥 Total Users: `{total_users}`\n"
        f"📋 Active Tasks: `{active_tasks}`\n"
        f"🔄 Running Processes: `{running_procs}`\n"
        f"📥 Queue Depth: `{queue_size}`\n"
        f"⚙️ Max Workers: `{config.NUM_WORKERS}`"
    )
    await message.reply_text(text)


@pyro_app.on_message(filters.command("verify"))
async def cmd_verify(client: Client, message: Message) -> None:
    await message.reply_text(
        "🔒 **Token Verification**\n\n"
        "Shortlink verification is not configured on this bot instance.\n"
        "Contact an admin to get Premium access via `/auth`."
    )


@pyro_app.on_message(filters.command("mytasks"))
async def cmd_mytasks(client: Client, message: Message) -> None:
    user_id = message.from_user.id
    tasks = await db.get_user_active_tasks(user_id)

    if not tasks:
        await message.reply_text("📭 You have no active or queued recordings.")
        return

    lines = ["📋 **Your Active Tasks:**\n"]
    for t in tasks:
        tid = t["task_id"]
        status = t.get("status", "?")
        status_icon = {
            "queued": "⏳",
            "recording": "🔴",
            "uploading": "📤",
        }.get(status, "❓")
        url_short = t.get("url", "")[:50] + ("…" if len(t.get("url", "")) > 50 else "")
        dur = format_duration(t.get("elapsed_secs", 0))
        lines.append(
            f"{status_icon} `{tid[:8]}` — **{status.upper()}**\n"
            f"   🔗 `{url_short}`\n"
            f"   ⏱ `{dur}`\n"
        )

    rows = [[InlineKeyboardButton(f"❌ Cancel {t['task_id'][:8]}", callback_data=f"ct_{t['task_id'][:12]}")]
            for t in tasks]
    await message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


@pyro_app.on_message(filters.command("cancel"))
async def cmd_cancel(client: Client, message: Message) -> None:
    from worker import cancel_task

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: `/cancel <task_id>`")
        return

    task_id = resolve_tid(parts[1].strip())
    task = await db.get_task(task_id)

    if not task or task.get("user_id") != message.from_user.id:
        await message.reply_text("❌ Task not found or does not belong to you.")
        return

    if task.get("status") not in ("queued", "recording", "uploading"):
        await message.reply_text(f"ℹ️ Task `{task_id[:8]}` is already finished.")
        return

    ok = await cancel_task(task_id)
    if ok:
        await message.reply_text(f"🛑 Cancellation signal sent for task `{task_id[:8]}`.")
    else:
        # Task may be queued but not yet running; mark it cancelled directly
        await db.update_task(task_id, {
            "status": "cancelled",
            "ended_at": datetime.now(timezone.utc),
        })
        await message.reply_text(f"✅ Queued task `{task_id[:8]}` removed from queue.")


# ─────────────────────────────────────────────────────────────────────────────
# /rec — Analyse stream and show track selection keyboard
# ─────────────────────────────────────────────────────────────────────────────

@pyro_app.on_message(filters.command("rec"))
async def cmd_rec(client: Client, message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip().startswith("http"):
        await message.reply_text(
            "Usage: `/rec <m3u8_url>`\n\nExample:\n`/rec https://example.com/live/stream.m3u8`"
        )
        return
    await _start_rec(client, message, parts[1].strip())


# ─────────────────────────────────────────────────────────────────────────────
# /search — Search channel list
# ─────────────────────────────────────────────────────────────────────────────

@pyro_app.on_message(filters.command("search"))
async def cmd_search(client: Client, message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: `/search <channel name>`")
        return

    query = parts[1].strip().lower()
    channels = load_all_channels()
    results = [ch for ch in channels if query in ch.get("name", "").lower()]

    if not results:
        await message.reply_text(f"🔍 No channels found matching **'{query}'**.")
        return

    # Store search session
    search_sessions[message.from_user.id] = {
        "results": results,
        "page": 0,
        "query": query,
    }

    await _send_search_page(message.from_user.id, message)


async def _send_search_page(
    user_id: int,
    message_or_cq: Any,
    edit: bool = False,
) -> None:
    session = search_sessions.get(user_id)
    if not session:
        return

    results = session["results"]
    page = session["page"]
    page_size = 5
    total_pages = (len(results) + page_size - 1) // page_size
    slice_ = results[page * page_size: (page + 1) * page_size]

    text_lines = [f"🔍 **Search Results** (page {page + 1}/{total_pages})\n"]
    rows: List[List[InlineKeyboardButton]] = []

    for ch in slice_:
        name = ch.get("name", "Unknown")
        url = ch.get("url", "")
        cat = ch.get("category", "")
        cid = register_channel_url(url)
        text_lines.append(f"📺 **{name}**" + (f"  _({cat})_" if cat else ""))
        rows.append([InlineKeyboardButton(f"▶️ Record {name}", callback_data=f"rch_{cid}")])

    # Navigation row
    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sp_{user_id}_p"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"sp_{user_id}_n"))
    if nav:
        rows.append(nav)

    text = "\n".join(text_lines)
    keyboard = InlineKeyboardMarkup(rows)

    if edit and hasattr(message_or_cq, "edit_message_text"):
        await message_or_cq.edit_message_text(text, reply_markup=keyboard)
    else:
        await message_or_cq.reply_text(text, reply_markup=keyboard)


@pyro_app.on_message(filters.command("channel"))
async def cmd_channel(client: Client, message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: `/channel <channel name>`")
        return

    query = parts[1].strip().lower()
    channels = load_all_channels()
    match = next((ch for ch in channels if ch.get("name", "").lower() == query), None)

    if not match:
        # Partial match fallback
        match = next((ch for ch in channels if query in ch.get("name", "").lower()), None)

    if not match:
        await message.reply_text(f"❌ Channel **'{parts[1].strip()}'** not found.")
        return

    # Directly invoke the record flow with the matched URL
    await _start_rec(client, message, match["url"])


# ─────────────────────────────────────────────────────────────────────────────
# ── ADMIN COMMANDS ───────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def admin_only(func):
    """Decorator that checks admin status before running handler."""
    async def wrapper(client: Client, message: Message) -> None:
        if not is_admin(message.from_user.id):
            await message.reply_text("⛔ This command is restricted to admins.")
            return
        await func(client, message)
    return wrapper


@pyro_app.on_message(filters.command("auth"))
@admin_only
async def cmd_auth(client: Client, message: Message) -> None:
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply_text("Usage: `/auth <user_id>`")
        return
    uid = int(parts[1])
    await db.set_user_tier(uid, "premium")
    await message.reply_text(f"✅ User `{uid}` upgraded to **Premium**.")


@pyro_app.on_message(filters.command("deauth"))
@admin_only
async def cmd_deauth(client: Client, message: Message) -> None:
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.reply_text("Usage: `/deauth <user_id>`")
        return
    uid = int(parts[1])
    await db.set_user_tier(uid, "default")
    await message.reply_text(f"✅ User `{uid}` downgraded to **Default**.")


@pyro_app.on_message(filters.command("tasks"))
@admin_only
async def cmd_admin_tasks(client: Client, message: Message) -> None:
    await _send_admin_tasks_page(0, message)


async def _send_admin_tasks_page(page: int, target: Any, edit: bool = False) -> None:
    limit = 5
    tasks = await db.get_all_active_tasks(skip=page * limit, limit=limit)
    total = await db.count_all_active_tasks()
    total_pages = max(1, (total + limit - 1) // limit)

    if not tasks:
        text = "📭 No active tasks at the moment."
        if edit:
            await target.edit_message_text(text)
        else:
            await target.reply_text(text)
        return

    lines = [f"📋 **All Active Tasks** (page {page + 1}/{total_pages})\n"]
    rows: List[List[InlineKeyboardButton]] = []

    for t in tasks:
        tid = t["task_id"]
        status = t.get("status", "?")
        uid = t.get("user_id", "?")
        url_short = t.get("url", "")[:40] + "…"
        lines.append(f"• `{tid[:8]}` [{status}] uid:{uid}\n  `{url_short}`")
        rows.append([
            InlineKeyboardButton(f"❌ Force Cancel {tid[:8]}", callback_data=f"fc_{tid[:12]}")
        ])

    nav: List[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"at_{page - 1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("➡️ Next", callback_data=f"at_{page + 1}"))
    if nav:
        rows.append(nav)

    text = "\n".join(lines)
    keyboard = InlineKeyboardMarkup(rows)

    if edit:
        await target.edit_message_text(text, reply_markup=keyboard)
    else:
        await target.reply_text(text, reply_markup=keyboard)


@pyro_app.on_message(filters.command("add_m3u8"))
@admin_only
async def cmd_add_m3u8(client: Client, message: Message) -> None:
    replied = message.reply_to_message
    if not replied or not replied.document:
        await message.reply_text(
            "Reply to a `.json` file with this command to save it as a channel list."
        )
        return
    doc = replied.document
    if not doc.file_name.endswith(".json"):
        await message.reply_text("Only `.json` files are supported.")
        return

    save_path = os.path.join(config.CHANNELS_DIR, doc.file_name)
    await replied.download(file_name=save_path)
    await message.reply_text(f"✅ Channel list saved as `{doc.file_name}`.")


@pyro_app.on_message(filters.command("remove_m3u8"))
@admin_only
async def cmd_remove_m3u8(client: Client, message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: `/remove_m3u8 <filename>`")
        return
    filename = os.path.basename(parts[1].strip())
    target = os.path.join(config.CHANNELS_DIR, filename)
    if os.path.exists(target):
        os.remove(target)
        await message.reply_text(f"✅ Deleted `{filename}`.")
    else:
        await message.reply_text(f"❌ File `{filename}` not found.")


@pyro_app.on_message(filters.command("pull"))
@admin_only
async def cmd_pull(client: Client, message: Message) -> None:
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply_text("Usage: `/pull <m3u8|log|premium|admin>`")
        return

    mode = parts[1].strip().lower()

    if mode == "m3u8":
        import zipfile, tempfile
        tmp = tempfile.mktemp(suffix=".zip")
        with zipfile.ZipFile(tmp, "w") as zf:
            for f in glob.glob(os.path.join(config.CHANNELS_DIR, "*.json")):
                zf.write(f, os.path.basename(f))
        await message.reply_document(tmp, caption="📦 Channel list archive")
        os.remove(tmp)

    elif mode == "premium":
        users = await db.get_premium_users()
        data = json.dumps(users, indent=2, default=str)
        await message.reply_document(
            io.BytesIO(data.encode()),
            file_name="premium_users.json",
            caption=f"💎 {len(users)} Premium users",
        )

    elif mode == "admin":
        data = json.dumps({"admin_ids": config.ADMIN_IDS}, indent=2)
        await message.reply_document(
            io.BytesIO(data.encode()),
            file_name="admin_ids.json",
            caption="🛠 Admin IDs",
        )

    elif mode == "log":
        # Export last 50 failed/done tasks with their logs
        cursor = db.db.tasks.find(
            {"status": {"$in": ["failed", "done", "cancelled"]}},
            {"_id": 0, "task_id": 1, "status": 1, "ffmpeg_log": 1, "error": 1},
        ).sort("updated_at", -1).limit(50)
        tasks = await cursor.to_list(length=50)
        data = json.dumps(tasks, indent=2, default=str)
        await message.reply_document(
            io.BytesIO(data.encode()),
            file_name="ffmpeg_logs.json",
            caption=f"📋 Last {len(tasks)} completed tasks with logs",
        )

    else:
        await message.reply_text("Valid options: `m3u8`, `log`, `premium`, `admin`")


@pyro_app.on_message(filters.command("flog"))
@admin_only
async def cmd_flog(client: Client, message: Message) -> None:
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.reply_text("Usage: `/flog <file|msg> <task_id>`")
        return

    mode = parts[1].lower()
    task_id = resolve_tid(parts[2].strip())
    log = await db.get_task_ffmpeg_log(task_id)

    if log is None:
        await message.reply_text(f"❌ Task `{task_id[:8]}` not found.")
        return
    if not log:
        await message.reply_text(f"ℹ️ No FFmpeg log for task `{task_id[:8]}`.")
        return

    if mode == "file":
        await message.reply_document(
            io.BytesIO(log.encode()),
            file_name=f"ffmpeg_{task_id[:8]}.log",
            caption=f"📋 FFmpeg log for task `{task_id[:8]}`",
        )
    else:
        # Send as message (truncated)
        await message.reply_text(
            f"**FFmpeg Log** `{task_id[:8]}`:\n```\n{log[-3000:]}\n```"
        )


@pyro_app.on_message(filters.command("admin_panel"))
@admin_only
async def cmd_admin_panel(client: Client, message: Message) -> None:
    await _send_admin_panel(message)


async def _start_rec(client: Client, message: Message, url: str) -> None:
    """
    Shared recording flow — called by /rec and /channel.
    Analyses the stream and shows the track selection keyboard.
    """
    user_id = message.from_user.id
    await db.get_or_create_user(user_id, message.from_user.username, message.from_user.full_name)
    tier = await db.get_user_tier(user_id)
    limit_error = await check_user_limits(user_id, tier)
    if limit_error:
        await message.reply_text(limit_error)
        return

    status_msg = await message.reply_text(
        "🔍 **Analysing stream…**\nRunning FFprobe, this may take a few seconds."
    )

    try:
        probe = await run_ffprobe(url)
    except RuntimeError as exc:
        await status_msg.edit_text(f"❌ **Stream Analysis Failed**\n\n`{exc}`")
        return

    video_tracks = probe["video_tracks"]
    audio_tracks = probe["audio_tracks"]

    if not video_tracks and not audio_tracks:
        await status_msg.edit_text(
            "❌ No media tracks found in this stream. "
            "Please verify the URL is a valid M3U8 / HLS stream."
        )
        return

    task_id = new_task_id()

    pending_selections[user_id] = {
        "task_id": task_id,
        "url": url,
        "tracks": probe,
        "sel_vid": 0,
        "sel_aud": 0,
        "msg_id": status_msg.id,
        "tier": tier,
    }

    duration_limit = config.TIER_LIMITS[tier]["max_duration"]
    vid_summary = ", ".join(vt["label"] for vt in video_tracks[:4]) or "None"
    aud_summary = ", ".join(at["label"] for at in audio_tracks[:4]) or "None"

    text = (
        f"📡 **Stream Analysis Complete**\n\n"
        f"🎥 Video tracks: `{len(video_tracks)}` — {vid_summary}\n"
        f"🔊 Audio tracks: `{len(audio_tracks)}` — {aud_summary}\n"
        f"⏱ Your limit: `{format_duration(duration_limit)}`\n\n"
        "Select your desired **video** and **audio** tracks below, "
        "then press **✅ Start Recording**."
    )
    keyboard = build_track_keyboard(task_id, probe, sel_vid=0, sel_aud=0)
    await status_msg.edit_text(text, reply_markup=keyboard)


async def _send_admin_panel(target: Any, edit: bool = False) -> None:
    from worker import task_queue, active_cancel_events

    total_users = await db.count_all_users()
    active_tasks = await db.count_all_active_tasks()
    queue_size = task_queue.qsize()
    running = len(active_cancel_events)
    uptime = format_duration(int(time.time() - _start_time))

    text = (
        "⚙️ **Admin Control Panel**\n\n"
        f"👥 Total Users: `{total_users}`\n"
        f"📋 Active Tasks: `{active_tasks}`\n"
        f"🔄 Running: `{running}`\n"
        f"📥 Queue: `{queue_size}`\n"
        f"⏱ Uptime: `{uptime}`\n"
        f"⚙️ Workers: `{config.NUM_WORKERS}`"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 All Tasks", callback_data="ap_tasks"),
            InlineKeyboardButton("👥 User Stats", callback_data="ap_users"),
        ],
        [
            InlineKeyboardButton("📥 Export Premium", callback_data="ap_premium"),
            InlineKeyboardButton("🔄 Refresh", callback_data="ap_refresh"),
        ],
    ])

    if edit:
        await target.edit_message_text(text, reply_markup=keyboard)
    else:
        await target.reply_text(text, reply_markup=keyboard)


# ─────────────────────────────────────────────────────────────────────────────
# ── CALLBACK QUERY HANDLERS ──────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@pyro_app.on_callback_query()
async def on_callback(client: Client, cq: CallbackQuery) -> None:
    data = cq.data or ""
    user_id = cq.from_user.id

    # ── Ignore header-only buttons ─────────────────────────────────────────
    if data == "noop":
        await cq.answer()
        return

    # ── Video track selection ──────────────────────────────────────────────
    if data.startswith("sv_"):
        _, tid_short, idx_str = data.split("_", 2)
        await _handle_select_track(cq, user_id, tid_short, "video", int(idx_str))
        return

    # ── Audio track selection ──────────────────────────────────────────────
    if data.startswith("sa_"):
        _, tid_short, idx_str = data.split("_", 2)
        await _handle_select_track(cq, user_id, tid_short, "audio", int(idx_str))
        return

    # ── Start recording ────────────────────────────────────────────────────
    if data.startswith("sr_"):
        tid_short = data[3:]
        await _handle_start_recording(cq, user_id, tid_short)
        return

    # ── Cancel pending selection ───────────────────────────────────────────
    if data.startswith("xp_"):
        pending_selections.pop(user_id, None)
        await cq.answer("Selection cancelled.")
        await cq.message.edit_text("❌ Selection cancelled.")
        return

    # ── Cancel running task ────────────────────────────────────────────────
    if data.startswith("ct_"):
        tid_short = data[3:]
        await _handle_cancel_task(cq, user_id, tid_short)
        return

    # ── Force cancel (admin) ───────────────────────────────────────────────
    if data.startswith("fc_"):
        if not is_admin(user_id):
            await cq.answer("⛔ Not authorised.", show_alert=True)
            return
        tid_short = data[3:]
        await _handle_force_cancel(cq, tid_short)
        return

    # ── Admin tasks pagination ─────────────────────────────────────────────
    if data.startswith("at_"):
        if not is_admin(user_id):
            await cq.answer("⛔ Not authorised.", show_alert=True)
            return
        page = int(data[3:])
        await cq.answer()
        await _send_admin_tasks_page(page, cq, edit=True)
        return

    # ── Admin panel actions ────────────────────────────────────────────────
    if data.startswith("ap_"):
        if not is_admin(user_id):
            await cq.answer("⛔ Not authorised.", show_alert=True)
            return
        action = data[3:]
        await _handle_admin_panel_action(cq, action)
        return

    # ── Search pagination ──────────────────────────────────────────────────
    if data.startswith("sp_"):
        parts = data.split("_")
        if len(parts) == 3:
            _, target_uid_str, direction = parts
            target_uid = int(target_uid_str)
            if user_id != target_uid and not is_admin(user_id):
                await cq.answer("⛔ Not your search.", show_alert=True)
                return
            session = search_sessions.get(target_uid)
            if not session:
                await cq.answer("Session expired.", show_alert=True)
                return
            if direction == "n":
                session["page"] += 1
            else:
                session["page"] = max(0, session["page"] - 1)
            await cq.answer()
            await _send_search_page(target_uid, cq, edit=True)
        return

    # ── Record channel from search ─────────────────────────────────────────
    if data.startswith("rch_"):
        cid = int(data[4:])
        url = channel_cache.get(cid)
        if not url:
            await cq.answer("Channel URL expired. Try /search again.", show_alert=True)
            return
        await cq.answer()
        # Trigger /rec flow
        cq.message.text = f"/rec {url}"
        cq.message.from_user = cq.from_user
        await cmd_rec(client, cq.message)
        return

    await cq.answer()


# ─────────────────────────────────────────────────────────────────────────────
# Callback sub-handlers
# ─────────────────────────────────────────────────────────────────────────────

async def _handle_select_track(
    cq: CallbackQuery,
    user_id: int,
    tid_short: str,
    track_type: str,
    idx: int,
) -> None:
    sel = pending_selections.get(user_id)
    if not sel or not sel["task_id"].startswith(tid_short):
        await cq.answer("Selection expired. Please run /rec again.", show_alert=True)
        return

    tracks = sel["tracks"]
    if track_type == "video":
        max_idx = len(tracks.get("video_tracks", []))
        if idx >= max_idx:
            await cq.answer("Invalid track.", show_alert=True)
            return
        sel["sel_vid"] = idx
        label = tracks["video_tracks"][idx]["label"]
        await cq.answer(f"✅ Video: {label}")
    else:
        max_idx = len(tracks.get("audio_tracks", []))
        if idx >= max_idx:
            await cq.answer("Invalid track.", show_alert=True)
            return
        sel["sel_aud"] = idx
        label = tracks["audio_tracks"][idx]["label"]
        await cq.answer(f"✅ Audio: {label}")

    keyboard = build_track_keyboard(
        sel["task_id"], sel["tracks"], sel["sel_vid"], sel["sel_aud"]
    )
    try:
        await cq.message.edit_reply_markup(keyboard)
    except (MessageNotModified, RPCError):
        pass


async def _handle_start_recording(
    cq: CallbackQuery,
    user_id: int,
    tid_short: str,
) -> None:
    from worker import enqueue_task

    sel = pending_selections.get(user_id)
    if not sel or not sel["task_id"].startswith(tid_short):
        await cq.answer("Selection expired. Please run /rec again.", show_alert=True)
        return

    tier = sel["tier"]
    limit_error = await check_user_limits(user_id, tier)
    if limit_error:
        await cq.answer(limit_error, show_alert=True)
        return

    task_id = sel["task_id"]
    url = sel["url"]
    tracks = sel["tracks"]
    sel_vid = sel["sel_vid"]
    sel_aud = sel["sel_aud"]

    video_tracks = tracks.get("video_tracks", [])
    audio_tracks = tracks.get("audio_tracks", [])

    video_stream_idx = video_tracks[sel_vid]["index"] if video_tracks else 0
    audio_stream_idx = audio_tracks[sel_aud]["index"] if audio_tracks else 1

    vid_label = video_tracks[sel_vid]["label"] if video_tracks else "N/A"
    aud_label = audio_tracks[sel_aud]["label"] if audio_tracks else "N/A"

    duration = config.TIER_LIMITS[tier]["max_duration"]

    # Create task in MongoDB
    task_doc = {
        "task_id": task_id,
        "user_id": user_id,
        "chat_id": cq.message.chat.id,
        "url": url,
        "video_stream_idx": video_stream_idx,
        "audio_stream_idx": audio_stream_idx,
        "video_label": vid_label,
        "audio_label": aud_label,
        "duration": duration,
        "tier": tier,
        "status": "queued",
        "progress": 0.0,
        "created_at": datetime.now(timezone.utc),
        "progress_msg_id": cq.message.id,
    }
    await db.create_task(task_doc)

    # Update the selection message to show queued status
    await cq.answer("✅ Recording queued!")
    pending_selections.pop(user_id, None)

    text = (
        f"⏳ **Recording Queued**\n\n"
        f"📂 Task ID: `{task_id[:8]}`\n"
        f"🎥 Video: `{vid_label}`\n"
        f"🔊 Audio: `{aud_label}`\n"
        f"⏱ Max Duration: `{format_duration(duration)}`\n\n"
        "_Your recording will start shortly. "
        "Use /mytasks to monitor progress._"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("❌ Cancel", callback_data=f"ct_{task_id[:12]}")
    ]])

    try:
        await cq.message.edit_text(text, reply_markup=keyboard)
    except Exception:
        pass

    # Add to worker queue (task_doc already has progress_msg_id set)
    await enqueue_task(task_doc)


async def _handle_cancel_task(
    cq: CallbackQuery,
    user_id: int,
    tid_short: str,
) -> None:
    from worker import cancel_task

    task_id = resolve_tid(tid_short)
    task = await db.get_task(task_id)

    if not task:
        await cq.answer("Task not found.", show_alert=True)
        return

    # Allow owner or admin to cancel anyone's task; users can only cancel their own
    if task.get("user_id") != user_id and not is_admin(user_id):
        await cq.answer("⛔ Not your task.", show_alert=True)
        return

    ok = await cancel_task(task_id)
    if ok:
        await cq.answer("🛑 Cancellation signal sent.")
    else:
        # Not running yet — cancel from queue
        await db.update_task(task_id, {
            "status": "cancelled",
            "ended_at": datetime.now(timezone.utc),
        })
        await cq.answer("✅ Queued task cancelled.")
        try:
            await cq.message.edit_text(f"❌ Task `{task_id[:8]}` cancelled.")
        except Exception:
            pass


async def _handle_force_cancel(cq: CallbackQuery, tid_short: str) -> None:
    from worker import cancel_task

    task_id = resolve_tid(tid_short)
    ok = await cancel_task(task_id)
    if not ok:
        await db.update_task(task_id, {
            "status": "cancelled",
            "ended_at": datetime.now(timezone.utc),
        })
    await cq.answer(f"Force cancelled: {task_id[:8]}", show_alert=True)
    # Refresh admin tasks page
    await _send_admin_tasks_page(0, cq, edit=True)


async def _handle_admin_panel_action(cq: CallbackQuery, action: str) -> None:
    if action == "refresh":
        await cq.answer("Refreshed!")
        await _send_admin_panel(cq, edit=True)

    elif action == "tasks":
        await cq.answer()
        await _send_admin_tasks_page(0, cq, edit=True)

    elif action == "users":
        total = await db.count_all_users()
        premium = len(await db.get_premium_users())
        await cq.answer(
            f"👥 Total: {total}\n💎 Premium: {premium}",
            show_alert=True,
        )

    elif action == "premium":
        users = await db.get_premium_users()
        if not users:
            await cq.answer("No premium users.", show_alert=True)
            return
        data = json.dumps(users, indent=2, default=str)
        await cq.answer()
        await cq.message.reply_document(
            io.BytesIO(data.encode()),
            file_name="premium_users.json",
            caption=f"💎 {len(users)} Premium users",
        )
