"""User‑facing command handlers."""

import asyncio
import json
import logging
import os
import re
import shlex
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
    count_active_tasks,
    get_tier,
    get_user,
    get_verify_hours,
    is_admin,
    is_auth_group,
    is_user_verified,
    set_tier,
    set_user_verified,
    tier_limits,
    user_active_tasks,
)
from bot.utils.channels import get_channel_by_name, search_channels
from bot.utils.ffmpeg import parse_tracks, probe_streams
from bot.utils.m3u_converter import download_m3u_url
from bot.utils.worker import (
    BOT_START_TIME,
    active_recordings,
    cancel_task,
    cancel_user_tasks,
    enqueue,
    task_queue,
)

log = logging.getLogger(__name__)

# Temporary cache: chat_id → probe result & selection state
_probe_cache: dict[int, dict] = {}

# Cache for M3U playlist channels: user_id → list of channel dicts
_m3u_cache: dict[int, list[dict]] = {}

# Maximum channels to display as inline buttons (Telegram limit)
_MAX_M3U_BUTTONS = 30


def _is_m3u_playlist_url(url: str) -> bool:
    """Return True if *url* looks like an M3U playlist link (channel list).

    Matches URLs whose path ends in ``.m3u`` or whose query string
    contains ``type=m3u`` / ``output=m3u``.  ``.m3u8`` is intentionally
    **excluded** – those are HLS stream manifests, not IPTV playlists,
    and should go straight to FFmpeg / N3U8DL-RE.
    """
    path_lower = url.split("?")[0].lower()
    # Explicit .m3u extension (not .m3u8 – those are usually HLS manifests)
    if path_lower.endswith(".m3u"):
        return True
    # Query-string hints common in IPTV providers
    url_lower = url.lower()
    if "type=m3u" in url_lower or "output=m3u" in url_lower:
        return True
    return False


async def _check_access(message: Message) -> bool:
    """Return True if the user may use bot commands in this chat.

    Access is granted when any of the following is true:
    * The chat is a private chat.
    * The user is the owner or an admin.
    * The chat (group) is an authorised group.

    For non‑admin users in private chats that are **not** in an auth group,
    a valid time‑based verification is required.
    """
    uid = message.from_user.id

    # Owner / admin – always allowed
    if uid == OWNER_ID or await is_admin(uid):
        return True

    # Group chat – only allowed if the group is authorised
    if message.chat.type in ("group", "supergroup"):
        if await is_auth_group(message.chat.id):
            return True
        # Not an auth group – silently ignore
        return False

    # Private chat – require valid verification
    if await is_user_verified(uid):
        return True

    hours = await get_verify_hours()
    await message.reply(
        f"🔒 **Verification required.**\n\n"
        f"Use `/verify [token]` to verify your account.\n"
        f"Verification is valid for **{hours} hour(s)**."
    )
    return False

# ── /rec argument parser ─────────────────────────────────────────────────

_DURATION_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")
_LANG_RE = re.compile(r"^\.L(\d+)$", re.IGNORECASE)


def parse_rec_args(text: str) -> dict | None:
    """Parse ``/rec`` arguments.

    Accepted format::

        /rec "<URL or Channel>" [HH:MM:SS] ["filename"] [.L#]

    Returns a dict with keys *source*, *duration_sec*, *filename*,
    *lang_index* or ``None`` when the source is missing.
    """
    parts = text.split(None, 1)
    if len(parts) < 2:
        return None

    try:
        tokens = shlex.split(parts[1])
    except ValueError:
        # Unbalanced quotes – treat entire remainder as the source
        tokens = [parts[1].strip()]

    if not tokens:
        return None

    result: dict = {
        "source": tokens[0],
        "duration_sec": None,
        "filename": None,
        "lang_index": None,
    }

    for token in tokens[1:]:
        m = _LANG_RE.match(token)
        if m:
            result["lang_index"] = int(m.group(1))
        elif _DURATION_RE.match(token):
            h, mn, s = token.split(":")
            result["duration_sec"] = int(h) * 3600 + int(mn) * 60 + int(s)
        elif result["filename"] is None:
            result["filename"] = token

    return result


def register(app: Client) -> None:
    """Register all user‑facing handlers on *app*."""

    # ── /start ────────────────────────────────────────────────────────────

    @app.on_message(filters.command("start"))
    async def cmd_start(client: Client, message: Message) -> None:
        await message.reply(
            "👋 **Welcome to the M3U8 Recorder Bot!**\n\n"
            "📹 I can record M3U8/M3U live streams and send them as "
            "auto‑split MP4 files.\n\n"
            "**Commands:**\n"
            '• `/rec [url]` – Record a stream\n'
            '• `/rec [m3u_url]` – Browse channels from M3U playlist\n'
            '• `/rec "URL or Channel" HH:MM:SS "name" .L#` – Custom recording\n'
            "• `/cancel [task_id]` – Cancel a recording\n"
            "• `/cancelall` – Cancel all your recordings\n"
            "• `/mytasks` – List your active recordings\n"
            "• `/status` – Bot statistics\n"
            "• `/verify [token]` – Upgrade to Verified tier\n"
            "• `/search [query]` – Search preloaded channel lists\n"
            "• `/channel [name]` – Record a channel by name\n",
            disable_web_page_preview=True,
        )

    # ── /rec <url> ────────────────────────────────────────────────────────

    @app.on_message(filters.command("rec"))
    async def cmd_rec(client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        args = parse_rec_args(message.text)
        if not args:
            await message.reply(
                "⚠️ **Usage:**\n"
                '`/rec [url]`\n'
                '`/rec "URL or Channel" HH:MM:SS "filename" .L#`\n\n'
                "**Examples:**\n"
                '`/rec https://example.com/stream.m3u8`\n'
                '`/rec "Disney Channel (4K)" 00:00:10 "My Cartoon" .L1`\n'
                '`/rec "https://example.com/stream.m3u8" 00:05:00 "My Stream"`'
            )
            return

        source = args["source"]

        # Resolve channel name → URL when source is not an HTTP link
        if source.startswith(("http://", "https://")):
            # Check if this is an M3U playlist URL (channel list)
            if _is_m3u_playlist_url(source):
                await _handle_m3u_url(client, message, source)
                return
            url = source
            ch_headers = None
            ch_drm = None
        else:
            ch = get_channel_by_name(source)
            if not ch:
                await message.reply(
                    f"⚠️ Channel **{source}** not found in any loaded list."
                )
                return
            url = ch.get("url", "")
            if not url:
                await message.reply(
                    f"⚠️ Channel **{source}** has no URL."
                )
                return
            ch_headers = ch.get("headers")
            ch_drm = ch.get("drm")

        # Check tier limits
        limits = await tier_limits(message.from_user.id)
        active = await user_active_tasks(message.from_user.id)
        if len(active) >= limits["max_tasks"]:
            await message.reply(
                f"⚠️ You already have **{len(active)}** active task(s). "
                f"Your tier allows **{limits['max_tasks']}**."
            )
            return

        # Cap custom duration at tier max (0 or negative treated as invalid)
        custom_duration = args["duration_sec"]
        if custom_duration is not None:
            if custom_duration <= 0:
                await message.reply(
                    "⚠️ Duration must be greater than 0.\n"
                    "Use `HH:MM:SS` format, e.g. `00:05:00` for 5 minutes."
                )
                return
            custom_duration = min(custom_duration, limits["max_duration"])

        status = await message.reply("🔍 **Analysing stream…** Please wait.")

        # Check if this stream likely needs N3U8DL-RE (DRM or MPD)
        is_drm_or_mpd = bool(ch_drm) or url.split("?")[0].lower().endswith(".mpd")

        try:
            probe_data = await probe_streams(url, headers=ch_headers)
        except Exception as exc:
            if is_drm_or_mpd:
                # DRM/MPD streams often can't be probed by ffprobe –
                # skip track selection and let N3U8DL-RE handle it.
                probe_data = None
            else:
                await status.edit(f"❌ **Probe failed:** {exc}")
                return

        if probe_data is not None:
            tracks = parse_tracks(probe_data)
        else:
            tracks = {"video": [], "audio": []}

        if not tracks["video"] and not tracks["audio"] and not is_drm_or_mpd:
            await status.edit("⚠️ No video/audio tracks found in the stream.")
            return

        # Pre-select audio track based on .L# flag
        selected_audio: set[int] = set()
        if tracks["audio"]:
            if args["lang_index"] is not None:
                li = args["lang_index"] - 1  # .L is 1-based
                if 0 <= li < len(tracks["audio"]):
                    selected_audio.add(tracks["audio"][li]["index"])
            if not selected_audio:
                selected_audio.add(tracks["audio"][0]["index"])

        # Store state for this chat
        _probe_cache[message.from_user.id] = {
            "url": url,
            "tracks": tracks,
            "selected_video": tracks["video"][0]["index"] if tracks["video"] else None,
            "selected_audio": selected_audio,
            "custom_duration": custom_duration,
            "custom_filename": args["filename"],
            "upload_mode": "file",
            "engine": "n3u8dl" if is_drm_or_mpd else "auto",
            "headers": ch_headers,
            "drm": ch_drm,
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
        sel: set = state["selected_audio"]
        if idx in sel:
            if len(sel) > 1:
                sel.discard(idx)
                await cq.answer(f"Audio track #{idx} deselected")
            else:
                await cq.answer("At least one audio track must be selected.", show_alert=True)
                return
        else:
            sel.add(idx)
            await cq.answer(f"Audio track #{idx} selected")
        await cq.message.edit_text(
            _build_track_selection_text(state["tracks"]),
            reply_markup=_build_track_keyboard(uid, state["tracks"]),
        )

    @app.on_callback_query(filters.regex(r"^upl:"))
    async def cb_upload_mode(client: Client, cq: CallbackQuery) -> None:
        mode = cq.data.split(":")[1]
        uid = cq.from_user.id
        state = _probe_cache.get(uid)
        if not state:
            await cq.answer("Session expired. Use /rec again.", show_alert=True)
            return
        state["upload_mode"] = mode
        await cq.message.edit_text(
            _build_track_selection_text(state["tracks"]),
            reply_markup=_build_track_keyboard(uid, state["tracks"]),
        )
        label = "📹 Video" if mode == "video" else "📄 File"
        await cq.answer(f"Upload as {label}")

    @app.on_callback_query(filters.regex(r"^eng:"))
    async def cb_engine(client: Client, cq: CallbackQuery) -> None:
        engine = cq.data.split(":")[1]
        uid = cq.from_user.id
        state = _probe_cache.get(uid)
        if not state:
            await cq.answer("Session expired. Use /rec again.", show_alert=True)
            return
        state["engine"] = engine
        await cq.message.edit_text(
            _build_track_selection_text(state["tracks"]),
            reply_markup=_build_track_keyboard(uid, state["tracks"]),
        )
        labels = {"auto": "⚙️ Auto", "ffmpeg": "🎬 FFmpeg", "n3u8dl": "📦 N3U8DL-RE"}
        await cq.answer(f"Engine: {labels.get(engine, engine)}")

    @app.on_callback_query(filters.regex(r"^start_rec$"))
    async def cb_start_rec(client: Client, cq: CallbackQuery) -> None:
        uid = cq.from_user.id
        state = _probe_cache.pop(uid, None)
        if not state:
            await cq.answer("Session expired. Use /rec again.", show_alert=True)
            return

        limits = await tier_limits(uid)
        task_id = uuid.uuid4().hex[:10]

        # Use custom duration if provided, otherwise tier max
        duration = state.get("custom_duration") or limits["max_duration"]

        audio_list = sorted(state["selected_audio"])
        upload_mode = state.get("upload_mode", "file")
        engine = state.get("engine", "auto")

        task = {
            "task_id": task_id,
            "user_id": uid,
            "chat_id": cq.message.chat.id,
            "url": state["url"],
            "duration": duration,
            "video_map": state["selected_video"],
            "audio_map": audio_list,
            "status": "queued",
            "custom_filename": state.get("custom_filename"),
            "upload_mode": upload_mode,
            "engine": engine,
        }
        if state.get("headers"):
            task["headers"] = state["headers"]
        if state.get("drm"):
            task["drm"] = state["drm"]

        await enqueue(task)

        dur_min, dur_sec = divmod(duration, 60)
        dur_h, dur_min = divmod(dur_min, 60)
        dur_str = f"{dur_h}h {dur_min}m {dur_sec}s" if dur_h else f"{dur_min}m {dur_sec}s"

        upl_label = "📹 Video" if upload_mode == "video" else "📄 File"
        eng_labels = {"auto": "⚙️ Auto", "ffmpeg": "🎬 FFmpeg", "n3u8dl": "📦 N3U8DL-RE"}
        eng_label = eng_labels.get(engine, engine)
        audio_str = ", ".join(f"#{a}" for a in audio_list)
        lines = [
            f"✅ **Task queued!**",
            f"🆔 Task ID: `{task_id}`",
            f"⏱ Duration: {dur_str}",
            f"📺 Video: track #{state['selected_video']}",
            f"🔊 Audio: track(s) {audio_str}",
            f"📤 Upload: {upl_label}",
            f"🛠 Engine: {eng_label}",
        ]
        if state.get("custom_filename"):
            lines.append(f"📝 Filename: {state['custom_filename']}")

        await cq.message.edit_text("\n".join(lines))
        await cq.answer("Recording queued!")

    # ── /cancel <task_id> ─────────────────────────────────────────────────

    @app.on_message(filters.command("cancel"))
    async def cmd_cancel(client: Client, message: Message) -> None:
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            await message.reply("⚠️ Usage: `/cancel [task_id]`")
            return
        task_id = parts[1].strip()
        if await cancel_task(task_id):
            await message.reply(f"🛑 Task `{task_id}` is being cancelled.")
        else:
            await message.reply(f"⚠️ No active task with ID `{task_id}` found.")

    # ── Cancel via inline button ──────────────────────────────────────────

    @app.on_callback_query(filters.regex(r"^cancel:"))
    async def cb_cancel(client: Client, cq: CallbackQuery) -> None:
        task_id = cq.data.split(":", 1)[1]
        if await cancel_task(task_id):
            await cq.answer("Task cancelled!")
            await cq.message.edit_text(f"🛑 Task `{task_id}` cancelled.")
        else:
            await cq.answer("Task not found or already finished.", show_alert=True)

    # ── /cancelall ────────────────────────────────────────────────────────

    @app.on_message(filters.command("cancelall"))
    async def cmd_cancelall(client: Client, message: Message) -> None:
        count = await cancel_user_tasks(message.from_user.id)
        if count:
            await message.reply(f"🛑 Cancelled **{count}** task(s).")
        else:
            await message.reply("📭 You have no active tasks to cancel.")

    # ── /mytasks ──────────────────────────────────────────────────────────

    @app.on_message(filters.command("mytasks"))
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

    @app.on_message(filters.command("status"))
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

    @app.on_message(filters.command("verify"))
    async def cmd_verify(client: Client, message: Message) -> None:
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            await message.reply("⚠️ Usage: `/verify [token]`")
            return
        token = parts[1].strip()
        if await consume_token(token):
            await set_tier(message.from_user.id, "verified")
            await set_user_verified(message.from_user.id)
            hours = await get_verify_hours()
            await message.reply(
                f"🎉 **Verification successful!** You're now Verified.\n"
                f"⏳ Valid for **{hours} hour(s)**."
            )
        else:
            await message.reply("❌ Invalid or already‑used token.")

    # ── /search <query> ──────────────────────────────────────────────────

    @app.on_message(filters.command("search"))
    async def cmd_search(client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            await message.reply("⚠️ Usage: `/search [query]`")
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
        await cq.answer("Analysing stream…")
        await _start_probe_flow(
            client, cq.from_user.id, cq.message.chat.id, url,
            headers=ch.get("headers"), drm=ch.get("drm"),
        )

    # ── /channel <name> ──────────────────────────────────────────────────

    @app.on_message(filters.command("channel"))
    async def cmd_channel(client: Client, message: Message) -> None:
        if not await _check_access(message):
            return
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            await message.reply("⚠️ Usage: `/channel [name]`")
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
        await _start_probe_flow(
            client, message.from_user.id, message.chat.id, url,
            headers=ch.get("headers"), drm=ch.get("drm"),
        )

    # ── M3U playlist URL channel selection callback ──────────────────────

    @app.on_callback_query(filters.regex(r"^m3u_ch:"))
    async def cb_m3u_channel(client: Client, cq: CallbackQuery) -> None:
        """Handle selection of a channel from a parsed M3U playlist."""
        uid = cq.from_user.id
        try:
            idx = int(cq.data.split(":", 1)[1])
        except (ValueError, IndexError):
            await cq.answer("Invalid selection.", show_alert=True)
            return
        channels = _m3u_cache.get(uid)
        if not channels or idx < 0 or idx >= len(channels):
            await cq.answer("Session expired. Use /rec again.", show_alert=True)
            return
        ch = channels[idx]
        _m3u_cache.pop(uid, None)
        url = ch.get("url", "")
        if not url:
            await cq.answer("No URL for this channel.", show_alert=True)
            return
        await cq.answer("Analysing stream…")
        await _start_probe_flow(
            client, uid, cq.message.chat.id, url,
            headers=ch.get("headers"), drm=ch.get("drm"),
        )


# ── Helpers ───────────────────────────────────────────────────────────────


async def _handle_m3u_url(
    client: Client, message: Message, m3u_url: str,
) -> None:
    """Download an M3U playlist from *m3u_url*, parse channels, and show
    a selection keyboard so the user can pick which channel to record."""
    import tempfile

    status = await message.reply("🔄 **Downloading M3U playlist…** Please wait.")

    # Download and parse the M3U into a temporary JSON
    fd, tmp_json = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    try:
        result = await download_m3u_url(m3u_url, tmp_json)
        if result is None:
            await status.edit(
                "❌ Failed to download or parse the M3U playlist.\n"
                "Make sure the URL points to a valid M3U file."
            )
            return
        with open(tmp_json, "r", encoding="utf-8") as fh:
            channels = json.load(fh)
    except Exception as exc:
        log.error("M3U URL parse error: %s", exc)
        await status.edit(f"❌ **Error parsing playlist:** {exc}")
        return
    finally:
        try:
            os.remove(tmp_json)
        except OSError:
            pass

    if not channels:
        await status.edit("⚠️ No channels found in the M3U playlist.")
        return

    uid = message.from_user.id

    # If single channel, go straight to probe
    if len(channels) == 1:
        ch = channels[0]
        await status.edit(
            f"📺 Found **{ch.get('name', 'Unknown')}** → starting analysis…"
        )
        await _start_probe_flow(
            client, uid, message.chat.id, ch.get("url", ""),
            headers=ch.get("headers"), drm=ch.get("drm"),
        )
        return

    # Multiple channels – cache them and show selection buttons
    _m3u_cache[uid] = channels

    display = channels[:_MAX_M3U_BUTTONS]
    buttons = []
    for i, ch in enumerate(display):
        name = ch.get("name", "Unknown")
        drm_tag = " 🔒" if ch.get("drm") else ""
        buttons.append([
            InlineKeyboardButton(
                f"📺 {name}{drm_tag}",
                callback_data=f"m3u_ch:{i}",
            )
        ])

    extra = ""
    if len(channels) > _MAX_M3U_BUTTONS:
        extra = (
            f"\n\n_(Showing first {_MAX_M3U_BUTTONS} "
            f"of {len(channels)} channels)_"
        )

    await status.edit(
        f"📋 **Found {len(channels)} channel(s) in playlist:**{extra}\n"
        "Select a channel to record:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def _start_probe_flow(
    client: Client,
    user_id: int,
    chat_id: int,
    url: str,
    custom_duration: int | None = None,
    custom_filename: str | None = None,
    lang_index: int | None = None,
    headers: dict[str, str] | None = None,
    drm: dict[str, str] | None = None,
) -> None:
    """Run ffprobe on *url*, parse tracks, and send the selection keyboard."""
    limits = await tier_limits(user_id)
    active = await user_active_tasks(user_id)
    if len(active) >= limits["max_tasks"]:
        await client.send_message(
            chat_id,
            f"⚠️ You already have **{len(active)}** active task(s). "
            f"Your tier allows **{limits['max_tasks']}**.",
        )
        return

    status = await client.send_message(chat_id, "🔍 **Analysing stream…** Please wait.")

    # Check if this stream likely needs N3U8DL-RE (DRM or MPD)
    is_drm_or_mpd = bool(drm) or url.split("?")[0].lower().endswith(".mpd")

    try:
        probe_data = await probe_streams(url, headers=headers)
    except Exception as exc:
        if is_drm_or_mpd:
            probe_data = None
        else:
            await status.edit(f"❌ **Probe failed:** {exc}")
            return

    if probe_data is not None:
        tracks = parse_tracks(probe_data)
    else:
        tracks = {"video": [], "audio": []}

    if not tracks["video"] and not tracks["audio"] and not is_drm_or_mpd:
        await status.edit("⚠️ No video/audio tracks found in the stream.")
        return

    selected_audio: set[int] = set()
    if tracks["audio"]:
        if lang_index is not None:
            li = lang_index - 1
            if 0 <= li < len(tracks["audio"]):
                selected_audio.add(tracks["audio"][li]["index"])
        if not selected_audio:
            selected_audio.add(tracks["audio"][0]["index"])

    _probe_cache[user_id] = {
        "url": url,
        "tracks": tracks,
        "selected_video": tracks["video"][0]["index"] if tracks["video"] else None,
        "selected_audio": selected_audio,
        "custom_duration": custom_duration,
        "custom_filename": custom_filename,
        "upload_mode": "file",
        "engine": "n3u8dl" if is_drm_or_mpd else "auto",
        "headers": headers,
        "drm": drm,
    }

    await status.edit(
        _build_track_selection_text(tracks),
        reply_markup=_build_track_keyboard(user_id, tracks),
    )


def _build_track_selection_text(tracks: dict) -> str:
    lines = ["🎛 **Track Selection**\n"]
    if not tracks["video"] and not tracks["audio"]:
        lines.append(
            "🔒 **DRM / MPD stream** – track selection is handled "
            "automatically by N3U8DL-RE."
        )
    else:
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
    sel_a: set = state.get("selected_audio", set())
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

    # Audio buttons (multi‑select)
    arow: list[InlineKeyboardButton] = []
    for a in tracks["audio"]:
        label = a["language"].upper()
        if a["index"] in sel_a:
            label = f"✅ {label}"
        arow.append(
            InlineKeyboardButton(label, callback_data=f"sel_a:{a['index']}")
        )
    if arow:
        rows.append(arow)

    # Upload mode buttons
    cur_mode = state.get("upload_mode", "file")
    file_label = "✅ 📄 File" if cur_mode == "file" else "📄 File"
    video_label = "✅ 📹 Video" if cur_mode == "video" else "📹 Video"
    rows.append([
        InlineKeyboardButton(file_label, callback_data="upl:file"),
        InlineKeyboardButton(video_label, callback_data="upl:video"),
    ])

    # Engine selection buttons
    cur_eng = state.get("engine", "auto")
    auto_label = "✅ ⚙️ Auto" if cur_eng == "auto" else "⚙️ Auto"
    ffmpeg_label = "✅ 🎬 FFmpeg" if cur_eng == "ffmpeg" else "🎬 FFmpeg"
    n3u8dl_label = "✅ 📦 N3U8DL" if cur_eng == "n3u8dl" else "📦 N3U8DL"
    rows.append([
        InlineKeyboardButton(auto_label, callback_data="eng:auto"),
        InlineKeyboardButton(ffmpeg_label, callback_data="eng:ffmpeg"),
        InlineKeyboardButton(n3u8dl_label, callback_data="eng:n3u8dl"),
    ])

    # Start button
    rows.append([InlineKeyboardButton("✅ Start Recording", callback_data="start_rec")])
    return InlineKeyboardMarkup(rows)
