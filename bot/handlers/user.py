"""User‑facing command handlers."""

import asyncio
import logging
import time
import uuid

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import OWNER_ID
from bot.db.database import (
    consume_token,
    get_tier,
    get_user,
    set_tier,
    tier_limits,
    user_active_tasks,
)
from bot.utils.channels import get_channel_by_name, search_channels
from bot.utils.ffmpeg import parse_tracks, probe_streams
from bot.utils.worker import (
    BOT_START_TIME,
    active_recordings,
    cancel_task,
    count_active_tasks,
    enqueue,
    task_queue,
)

log = logging.getLogger(__name__)

# Temporary cache: chat_id → probe result & selection state
_probe_cache: dict[int, dict] = {}


def register(app: Client) -> None:
    """Register all user‑facing handlers on *app*."""

    # ── /start ────────────────────────────────────────────────────────────

    @app.on_message(filters.command("start") & filters.private)
    async def cmd_start(client: Client, message: Message) -> None:
        await message.reply(
            "👋 **Welcome to the M3U8 Recorder Bot!**\n\n"
            "📹 I can record M3U8/M3U live streams and send them to you as "
            "auto‑split MP4 files.\n\n"
            "**Commands:**\n"
            "• `/rec <m3u8_url>` – Analyse & record a stream\n"
            "• `/cancel <task_id>` – Cancel a recording\n"
            "• `/mytasks` – List your active recordings\n"
            "• `/status` – Bot statistics\n"
            "• `/verify <token>` – Upgrade to Verified tier\n"
            "• `/search <query>` – Search preloaded channel lists\n"
            "• `/channel <name>` – Record a channel by name\n",
            disable_web_page_preview=True,
        )

    # ── /rec <url> ────────────────────────────────────────────────────────

    @app.on_message(filters.command("rec") & filters.private)
    async def cmd_rec(client: Client, message: Message) -> None:
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            await message.reply("⚠️ Usage: `/rec <m3u8_url>`")
            return

        url = parts[1].strip()

        # Check tier limits
        limits = await tier_limits(message.from_user.id)
        active = await user_active_tasks(message.from_user.id)
        if len(active) >= limits["max_tasks"]:
            await message.reply(
                f"⚠️ You already have **{len(active)}** active task(s). "
                f"Your tier allows **{limits['max_tasks']}**."
            )
            return

        status = await message.reply("🔍 **Analysing stream…** Please wait.")

        try:
            probe_data = await probe_streams(url)
        except Exception as exc:
            await status.edit(f"❌ **Probe failed:** `{exc}`")
            return

        tracks = parse_tracks(probe_data)
        if not tracks["video"] and not tracks["audio"]:
            await status.edit("⚠️ No video/audio tracks found in the stream.")
            return

        # Store state for this chat
        _probe_cache[message.from_user.id] = {
            "url": url,
            "tracks": tracks,
            "selected_video": tracks["video"][0]["index"] if tracks["video"] else None,
            "selected_audio": tracks["audio"][0]["index"] if tracks["audio"] else None,
        }

        await status.edit(
            _build_track_selection_text(tracks),
            reply_markup=_build_track_keyboard(message.from_user.id, tracks),
        )

    # ── Track selection callbacks ─────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^sel_v:"))
    async def cb_select_video(client: Client, cq: CallbackQuery) -> None:
        idx = int(cq.data.split(":")[1])
        uid = cq.from_user.id
        state = _probe_cache.get(uid)
        if not state:
            await cq.answer("Session expired. Use /rec again.", show_alert=True)
            return
        state["selected_video"] = idx
        await cq.message.edit_text(
            _build_track_selection_text(state["tracks"]),
            reply_markup=_build_track_keyboard(uid, state["tracks"]),
        )
        await cq.answer(f"Video track #{idx} selected")

    @app.on_callback_query(filters.regex(r"^sel_a:"))
    async def cb_select_audio(client: Client, cq: CallbackQuery) -> None:
        idx = int(cq.data.split(":")[1])
        uid = cq.from_user.id
        state = _probe_cache.get(uid)
        if not state:
            await cq.answer("Session expired. Use /rec again.", show_alert=True)
            return
        state["selected_audio"] = idx
        await cq.message.edit_text(
            _build_track_selection_text(state["tracks"]),
            reply_markup=_build_track_keyboard(uid, state["tracks"]),
        )
        await cq.answer(f"Audio track #{idx} selected")

    @app.on_callback_query(filters.regex(r"^start_rec$"))
    async def cb_start_rec(client: Client, cq: CallbackQuery) -> None:
        uid = cq.from_user.id
        state = _probe_cache.pop(uid, None)
        if not state:
            await cq.answer("Session expired. Use /rec again.", show_alert=True)
            return

        limits = await tier_limits(uid)
        task_id = uuid.uuid4().hex[:10]

        task = {
            "task_id": task_id,
            "user_id": uid,
            "chat_id": cq.message.chat.id,
            "url": state["url"],
            "duration": limits["max_duration"],
            "video_map": state["selected_video"],
            "audio_map": state["selected_audio"],
            "status": "queued",
        }

        await enqueue(task)
        await cq.message.edit_text(
            f"✅ **Task queued!**\n"
            f"🆔 Task ID: `{task_id}`\n"
            f"⏱ Max duration: {limits['max_duration'] // 60} min\n"
            f"📺 Video: track #{state['selected_video']}\n"
            f"🔊 Audio: track #{state['selected_audio']}\n"
        )
        await cq.answer("Recording queued!")

    # ── /cancel <task_id> ─────────────────────────────────────────────────

    @app.on_message(filters.command("cancel") & filters.private)
    async def cmd_cancel(client: Client, message: Message) -> None:
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            await message.reply("⚠️ Usage: `/cancel <task_id>`")
            return
        task_id = parts[1].strip()
        if cancel_task(task_id):
            await message.reply(f"🛑 Task `{task_id}` is being cancelled.")
        else:
            await message.reply(f"⚠️ No active task with ID `{task_id}` found.")

    # ── Cancel via inline button ──────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^cancel:"))
    async def cb_cancel(client: Client, cq: CallbackQuery) -> None:
        task_id = cq.data.split(":", 1)[1]
        if cancel_task(task_id):
            await cq.answer("Task cancelled!")
            await cq.message.edit_text(f"🛑 Task `{task_id}` cancelled.")
        else:
            await cq.answer("Task not found or already finished.", show_alert=True)

    # ── /mytasks ──────────────────────────────────────────────────────────

    @app.on_message(filters.command("mytasks") & filters.private)
    async def cmd_mytasks(client: Client, message: Message) -> None:
        tasks = await user_active_tasks(message.from_user.id)
        if not tasks:
            await message.reply("📭 You have no active tasks.")
            return
        lines = ["📋 **Your Active Tasks:**\n"]
        for t in tasks:
            lines.append(
                f"• `{t['task_id']}` – {t['status']}  "
                f"[{t.get('url', '')[:40]}…]"
            )
        await message.reply("\n".join(lines))

    # ── /status ───────────────────────────────────────────────────────────

    @app.on_message(filters.command("status") & filters.private)
    async def cmd_status(client: Client, message: Message) -> None:
        uptime = int(time.time() - BOT_START_TIME) if BOT_START_TIME else 0
        h, rem = divmod(uptime, 3600)
        m, s = divmod(rem, 60)
        active = len(active_recordings)
        queued = task_queue.qsize()
        total_db = await count_active_tasks()
        tier = await get_tier(message.from_user.id)

        await message.reply(
            f"📊 **Bot Status**\n\n"
            f"⏱ Uptime: `{h}h {m}m {s}s`\n"
            f"🔴 Active recordings: **{active}**\n"
            f"📥 Queued: **{queued}**\n"
            f"📦 Total DB tasks: **{total_db}**\n"
            f"👤 Your tier: **{tier}**"
        )

    # ── /verify <token> ──────────────────────────────────────────────────

    @app.on_message(filters.command("verify") & filters.private)
    async def cmd_verify(client: Client, message: Message) -> None:
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            await message.reply("⚠️ Usage: `/verify <token>`")
            return
        token = parts[1].strip()
        if await consume_token(token):
            await set_tier(message.from_user.id, "verified")
            await message.reply("🎉 **Verification successful!** You're now Verified.")
        else:
            await message.reply("❌ Invalid or already‑used token.")

    # ── /search <query> ──────────────────────────────────────────────────

    @app.on_message(filters.command("search") & filters.private)
    async def cmd_search(client: Client, message: Message) -> None:
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            await message.reply("⚠️ Usage: `/search <query>`")
            return
        results = search_channels(parts[1].strip())
        if not results:
            await message.reply("🔍 No channels found.")
            return
        buttons = []
        for ch in results:
            name = ch.get("name", "Unknown")
            url = ch.get("url", "")
            buttons.append([
                InlineKeyboardButton(
                    f"📺 {name}", callback_data=f"chrec:{name[:40]}"
                )
            ])
        await message.reply(
            f"🔍 **Found {len(results)} channel(s):**",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @app.on_callback_query(filters.regex(r"^chrec:"))
    async def cb_channel_rec(client: Client, cq: CallbackQuery) -> None:
        name = cq.data.split(":", 1)[1]
        ch = get_channel_by_name(name)
        if not ch:
            await cq.answer("Channel not found.", show_alert=True)
            return
        url = ch.get("url", "")
        if not url:
            await cq.answer("No URL for this channel.", show_alert=True)
            return
        # Trigger the /rec flow programmatically
        await cq.message.reply(f"/rec {url}")
        await cq.answer()

    # ── /channel <name> ──────────────────────────────────────────────────

    @app.on_message(filters.command("channel") & filters.private)
    async def cmd_channel(client: Client, message: Message) -> None:
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            await message.reply("⚠️ Usage: `/channel <name>`")
            return
        ch = get_channel_by_name(parts[1].strip())
        if not ch:
            await message.reply("⚠️ Channel not found in any loaded list.")
            return
        url = ch.get("url", "")
        if not url:
            await message.reply("⚠️ This channel entry has no URL.")
            return
        # Trigger recording with probe
        await message.reply(f"Found **{ch.get('name', '')}** → starting analysis…")
        # Re-use cmd_rec logic by constructing a synthetic message text
        message.text = f"/rec {url}"
        await cmd_rec(client, message)


# ── Helpers ───────────────────────────────────────────────────────────────


def _build_track_selection_text(tracks: dict) -> str:
    lines = ["🎛 **Track Selection**\n"]
    if tracks["video"]:
        lines.append("**📺 Video Tracks:**")
        for v in tracks["video"]:
            lines.append(f"  • #{v['index']} – {v['resolution']} ({v['codec']})")
    if tracks["audio"]:
        lines.append("\n**🔊 Audio Tracks:**")
        for a in tracks["audio"]:
            lines.append(f"  • #{a['index']} – {a['language']} ({a['codec']})")
    lines.append(
        "\nSelect your preferred tracks below, then tap **✅ Start Recording**."
    )
    return "\n".join(lines)


def _build_track_keyboard(
    uid: int, tracks: dict
) -> InlineKeyboardMarkup:
    state = _probe_cache.get(uid, {})
    sel_v = state.get("selected_video")
    sel_a = state.get("selected_audio")
    rows: list[list[InlineKeyboardButton]] = []

    # Video buttons
    vrow: list[InlineKeyboardButton] = []
    for v in tracks["video"]:
        label = v["resolution"]
        if v["index"] == sel_v:
            label = f"✅ {label}"
        vrow.append(
            InlineKeyboardButton(label, callback_data=f"sel_v:{v['index']}")
        )
    if vrow:
        rows.append(vrow)

    # Audio buttons
    arow: list[InlineKeyboardButton] = []
    for a in tracks["audio"]:
        label = a["language"].upper()
        if a["index"] == sel_a:
            label = f"✅ {label}"
        arow.append(
            InlineKeyboardButton(label, callback_data=f"sel_a:{a['index']}")
        )
    if arow:
        rows.append(arow)

    # Start button
    rows.append([InlineKeyboardButton("✅ Start Recording", callback_data="start_rec")])
    return InlineKeyboardMarkup(rows)
