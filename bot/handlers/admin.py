"""Admin‑only command handlers."""

import logging
import os
import subprocess

from pyrogram import Client, filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from bot.config import CHANNEL_LIST_DIR, OWNER_ID
from bot.db.database import (
    add_admin,
    all_active_tasks,
    count_active_tasks,
    is_admin,
    remove_admin,
    set_tier,
)
from bot.utils.channels import list_json_files, remove_json_file
from bot.utils.m3u_converter import convert_m3u_to_json
from bot.utils.worker import cancel_task

log = logging.getLogger(__name__)


def _is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


async def _check_admin(message: Message) -> bool:
    uid = message.from_user.id
    if _is_owner(uid):
        return True
    if await is_admin(uid):
        return True
    await message.reply("🚫 This command is restricted to admins.")
    return False


def register(app: Client) -> None:
    """Register admin handlers on *app*."""

    # ── /auth <user_id> ──────────────────────────────────────────────────

    @app.on_message(filters.command("auth") & filters.private)
    async def cmd_auth(client: Client, message: Message) -> None:
        if not await _check_admin(message):
            return
        parts = message.text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            await message.reply("⚠️ Usage: `/auth <user_id>`")
            return
        target = int(parts[1].strip())
        await set_tier(target, "premium")
        await message.reply(f"✅ User `{target}` upgraded to **Premium**.")

    # ── /deauth <user_id> ────────────────────────────────────────────────

    @app.on_message(filters.command("deauth") & filters.private)
    async def cmd_deauth(client: Client, message: Message) -> None:
        if not await _check_admin(message):
            return
        parts = message.text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip().isdigit():
            await message.reply("⚠️ Usage: `/deauth <user_id>`")
            return
        target = int(parts[1].strip())
        await set_tier(target, "default")
        await message.reply(f"✅ User `{target}` downgraded to **Default**.")

    # ── /tasks (paginated) ───────────────────────────────────────────────

    @app.on_message(filters.command("tasks") & filters.private)
    async def cmd_tasks(client: Client, message: Message) -> None:
        if not await _check_admin(message):
            return
        await _send_tasks_page(client, message.chat.id, page=0)

    @app.on_callback_query(filters.regex(r"^adm_pg:"))
    async def cb_tasks_page(client: Client, cq: CallbackQuery) -> None:
        if not _is_owner(cq.from_user.id) and not await is_admin(cq.from_user.id):
            await cq.answer("Admins only.", show_alert=True)
            return
        page = int(cq.data.split(":")[1])
        await _edit_tasks_page(client, cq.message, page)
        await cq.answer()

    @app.on_callback_query(filters.regex(r"^adm_cancel:"))
    async def cb_admin_cancel(client: Client, cq: CallbackQuery) -> None:
        if not _is_owner(cq.from_user.id) and not await is_admin(cq.from_user.id):
            await cq.answer("Admins only.", show_alert=True)
            return
        task_id = cq.data.split(":", 1)[1]
        if cancel_task(task_id):
            await cq.answer(f"Task {task_id} cancelled.")
            await _edit_tasks_page(client, cq.message, page=0)
        else:
            await cq.answer("Task not found.", show_alert=True)

    # ── /add_m3u8 (reply to .json file) ─────────────────────────────────

    @app.on_message(filters.command("add_m3u8") & filters.private)
    async def cmd_add_m3u8(client: Client, message: Message) -> None:
        if not await _check_admin(message):
            return
        reply = message.reply_to_message
        if not reply or not reply.document:
            await message.reply(
                "⚠️ Reply to a `.json` file with `/add_m3u8` to add a channel list."
            )
            return
        fname = reply.document.file_name or "channels.json"
        if not fname.endswith(".json"):
            await message.reply("⚠️ The file must be a `.json` file.")
            return
        os.makedirs(CHANNEL_LIST_DIR, exist_ok=True)
        dest = os.path.join(CHANNEL_LIST_DIR, fname)
        await reply.download(dest)
        await message.reply(f"✅ Saved channel list as `{fname}`.")

    # ── /convert_m3u (reply to .m3u/.m3u8 file) ────────────────────────

    @app.on_message(filters.command("convert_m3u") & filters.private)
    async def cmd_convert_m3u(client: Client, message: Message) -> None:
        if not await _check_admin(message):
            return
        reply = message.reply_to_message
        if not reply or not reply.document:
            await message.reply(
                "⚠️ Reply to an `.m3u` or `.m3u8` file with "
                "`/convert_m3u` to convert it to a JSON channel list."
            )
            return
        fname = reply.document.file_name or "playlist.m3u"
        if not fname.endswith((".m3u", ".m3u8")):
            await message.reply("⚠️ The file must be an `.m3u` or `.m3u8` file.")
            return

        status = await message.reply("🔄 Converting M3U to JSON…")

        # Download the M3U file to a temporary location
        os.makedirs(CHANNEL_LIST_DIR, exist_ok=True)
        m3u_path = os.path.join(CHANNEL_LIST_DIR, fname)
        await reply.download(m3u_path)

        # Derive JSON filename from the M3U filename
        json_fname = os.path.splitext(fname)[0] + ".json"
        json_path = os.path.join(CHANNEL_LIST_DIR, json_fname)

        result = convert_m3u_to_json(m3u_path, json_path)

        # Clean up the downloaded M3U file
        try:
            os.remove(m3u_path)
        except OSError:
            pass

        if result is None:
            await status.edit(
                "❌ Conversion failed. Make sure the file is a valid M3U playlist."
            )
            return

        await status.edit(
            f"✅ Converted and saved as `{json_fname}`.\n"
            f"The channel list is now available for `/search`."
        )

    # ── /remove_m3u8 <filename> ──────────────────────────────────────────

    @app.on_message(filters.command("remove_m3u8") & filters.private)
    async def cmd_remove_m3u8(client: Client, message: Message) -> None:
        if not await _check_admin(message):
            return
        parts = message.text.split(None, 1)
        if len(parts) < 2:
            files = list_json_files()
            if files:
                await message.reply(
                    "📂 Available lists:\n" + "\n".join(f"• `{f}`" for f in files)
                )
            else:
                await message.reply("📭 No channel lists loaded.")
            return
        fname = parts[1].strip()
        if remove_json_file(fname):
            await message.reply(f"✅ Removed `{fname}`.")
        else:
            await message.reply(f"⚠️ File `{fname}` not found.")

    # ── /pull (git pull) ─────────────────────────────────────────────────

    @app.on_message(filters.command("pull") & filters.private)
    async def cmd_pull(client: Client, message: Message) -> None:
        if not _is_owner(message.from_user.id):
            await message.reply("🚫 Owner only.")
            return
        try:
            result = subprocess.run(
                ["git", "pull"],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            )
            output = (result.stdout + "\n" + result.stderr).strip() or "No output."
            await message.reply(f"```\n{output}\n```")
        except Exception as exc:
            await message.reply(f"❌ `{exc}`")


# ── Helpers ──────────────────────────────────────────────────────────────

PAGE_SIZE = 5


async def _send_tasks_page(client: Client, chat_id: int, page: int) -> None:
    text, markup = await _tasks_page_content(page)
    await client.send_message(chat_id, text, reply_markup=markup)


async def _edit_tasks_page(client: Client, message, page: int) -> None:
    text, markup = await _tasks_page_content(page)
    await message.edit_text(text, reply_markup=markup)


async def _tasks_page_content(page: int):
    total = await count_active_tasks()
    tasks = await all_active_tasks(skip=page * PAGE_SIZE, limit=PAGE_SIZE)

    if not tasks:
        return "📭 No active tasks.", None

    lines = [f"📋 **All Active Tasks** (page {page + 1})\n"]
    buttons: list[list[InlineKeyboardButton]] = []
    for t in tasks:
        tid = t["task_id"]
        lines.append(
            f"• `{tid}` — user `{t['user_id']}` — {t['status']}"
        )
        buttons.append([
            InlineKeyboardButton(
                f"❌ Cancel {tid}", callback_data=f"adm_cancel:{tid}"
            )
        ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(
            InlineKeyboardButton("⬅️ Prev", callback_data=f"adm_pg:{page - 1}")
        )
    if (page + 1) * PAGE_SIZE < total:
        nav.append(
            InlineKeyboardButton("➡️ Next", callback_data=f"adm_pg:{page + 1}")
        )
    if nav:
        buttons.append(nav)

    return "\n".join(lines), InlineKeyboardMarkup(buttons) if buttons else None
