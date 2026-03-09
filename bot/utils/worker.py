"""Asyncio‑based worker queue for recording tasks."""

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import PROGRESS_INTERVAL
from bot.db.database import save_task, delete_task, get_task, user_active_tasks, delete_user_tasks
from bot.utils.ffmpeg import RecordingProcess, format_progress

if TYPE_CHECKING:
    from pyrogram import Client

log = logging.getLogger(__name__)

# Global state
task_queue: asyncio.Queue = asyncio.Queue()
active_recordings: dict[str, RecordingProcess] = {}
_cancelled_tasks: set[str] = set()
_workers: list[asyncio.Task] = []
BOT_START_TIME: float = 0.0


async def enqueue(task: dict) -> None:
    """Put a task dict onto the global queue."""
    await save_task(task)
    await task_queue.put(task)


async def _update_progress(
    client: "Client",
    chat_id: int,
    msg_id: int,
    rec: RecordingProcess,
    duration: int,
    task_id: str,
) -> None:
    """Edit the status message periodically."""
    while rec.proc and rec.proc.returncode is None:
        await asyncio.sleep(PROGRESS_INTERVAL)
        if rec.cancelled:
            break
        text = format_progress(rec, duration)
        text += f"\n\n🆔 Task: `{task_id}`"
        try:
            await client.edit_message_text(
                chat_id,
                msg_id,
                text,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(
                        "❌ Cancel", callback_data=f"cancel:{task_id}"
                    )]
                ]),
            )
        except Exception:
            pass


async def _worker(client: "Client") -> None:
    """Worker coroutine – pulls tasks from the queue and records them."""
    while True:
        task = await task_queue.get()
        task_id: str = task["task_id"]

        # Skip tasks cancelled while queued
        if task_id in _cancelled_tasks:
            _cancelled_tasks.discard(task_id)
            task_queue.task_done()
            continue

        chat_id: int = task["chat_id"]
        url: str = task["url"]
        duration: int = task["duration"]
        video_map: int = task.get("video_map", 0)
        audio_map = task.get("audio_map", 1)
        if isinstance(audio_map, int):
            audio_map = [audio_map]
        custom_filename: str | None = task.get("custom_filename")

        # Update status in DB
        task["status"] = "recording"
        await save_task(task)

        rec = RecordingProcess()
        active_recordings[task_id] = rec

        status_msg = await client.send_message(
            chat_id, f"🔴 **Recording started**\n🆔 Task: `{task_id}`"
        )

        try:
            await rec.start(url, task_id, duration, video_map, audio_map,
                            filename_prefix=custom_filename)

            progress_reader = asyncio.create_task(rec.read_progress())
            progress_updater = asyncio.create_task(
                _update_progress(
                    client, chat_id, status_msg.id, rec, duration, task_id
                )
            )

            await rec.wait()
            progress_reader.cancel()
            progress_updater.cancel()

            files = rec.output_files()
            if files:
                await client.edit_message_text(
                    chat_id,
                    status_msg.id,
                    f"✅ **Recording complete** – uploading {len(files)} segment(s)…",
                )
                for idx, fpath in enumerate(files, 1):
                    caption = (
                        f"📹 Segment {idx}/{len(files)}\n"
                        f"🆔 Task: `{task_id}`"
                    )
                    try:
                        await client.send_document(
                            chat_id,
                            fpath,
                            caption=caption,
                            force_document=True,
                        )
                    except Exception as exc:
                        log.error("Upload failed for %s: %s", fpath, exc)
                        await client.send_message(
                            chat_id,
                            f"⚠️ Upload failed for segment {idx}: `{exc}`",
                        )
            else:
                await client.edit_message_text(
                    chat_id,
                    status_msg.id,
                    "⚠️ **No output files generated.** The stream may be unavailable.",
                )

        except Exception as exc:
            log.exception("Recording error for task %s", task_id)
            try:
                await client.edit_message_text(
                    chat_id, status_msg.id, f"❌ **Error:** `{exc}`"
                )
            except Exception:
                pass
        finally:
            rec.cleanup()
            active_recordings.pop(task_id, None)
            await delete_task(task_id)
            task_queue.task_done()


def start_workers(client: "Client", num_workers: int) -> None:
    """Spawn *num_workers* background worker tasks."""
    global BOT_START_TIME
    BOT_START_TIME = time.time()
    for i in range(num_workers):
        t = asyncio.create_task(_worker(client))
        _workers.append(t)
    log.info("Started %d recording workers", num_workers)


async def cancel_task(task_id: str) -> bool:
    """Cancel a running or queued task. Returns True if found."""
    # Active recording – kill the ffmpeg process
    rec = active_recordings.get(task_id)
    if rec:
        rec.cancel()
        return True
    # Queued or stale in DB – mark for skip and remove from DB
    task = await get_task(task_id)
    if task:
        _cancelled_tasks.add(task_id)
        await delete_task(task_id)
        return True
    return False


async def cancel_user_tasks(user_id: int) -> int:
    """Cancel every active task belonging to *user_id*. Returns count."""
    count = 0
    # Cancel active recordings for this user
    for tid, rec in list(active_recordings.items()):
        task = await get_task(tid)
        if task and task.get("user_id") == user_id:
            rec.cancel()
            count += 1
    # Cancel remaining queued / stale tasks in DB
    tasks = await user_active_tasks(user_id)
    for t in tasks:
        tid = t["task_id"]
        if tid not in active_recordings:
            _cancelled_tasks.add(tid)
            count += 1
    # Bulk-delete from DB
    count_db = await delete_user_tasks(user_id)
    # Return the larger of the two counts (avoid double-counting)
    return max(count, count_db)
