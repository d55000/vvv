"""Asyncio‑based worker queue for recording tasks."""

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import PROGRESS_INTERVAL
from bot.db.database import save_task, delete_task, get_task, user_active_tasks, delete_user_tasks
from bot.utils.ffmpeg import RecordingProcess, format_progress, get_video_duration, generate_thumbnail

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


def _needs_n3u8dl(task: dict) -> bool:
    """Return True if the task should use N3U8DL-RE instead of FFmpeg."""
    if task.get("drm"):
        return True
    url = task.get("url", "")
    # DASH/MPD streams require N3U8DL-RE for proper handling
    if ".mpd" in url.split("?")[0].lower():
        return True
    return False


async def _upload_segment(
    client: "Client",
    chat_id: int,
    fpath: str,
    caption: str,
    upload_mode: str,
) -> None:
    """Upload a single segment file as video or document."""
    if upload_mode == "video":
        duration = await get_video_duration(fpath)
        thumb = await generate_thumbnail(fpath)
        try:
            await client.send_video(
                chat_id,
                fpath,
                caption=caption,
                duration=duration,
                thumb=thumb,
                supports_streaming=True,
            )
        finally:
            # Clean up thumbnail
            if thumb and os.path.isfile(thumb):
                try:
                    os.remove(thumb)
                except OSError:
                    pass
    else:
        await client.send_document(
            chat_id,
            fpath,
            caption=caption,
            force_document=True,
        )


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
        upload_mode: str = task.get("upload_mode", "file")
        headers: dict | None = task.get("headers")
        drm: dict | None = task.get("drm")

        use_n3u8dl = _needs_n3u8dl(task)

        # Update status in DB
        task["status"] = "recording"
        await save_task(task)

        status_msg = await client.send_message(
            chat_id, f"🔴 **Recording started**\n🆔 Task: `{task_id}`"
        )

        if use_n3u8dl:
            await _run_n3u8dl_task(
                client, task, chat_id, task_id, url, duration,
                custom_filename, upload_mode, headers, drm, status_msg,
            )
        else:
            await _run_ffmpeg_task(
                client, task, chat_id, task_id, url, duration,
                video_map, audio_map, custom_filename, upload_mode,
                status_msg,
            )


async def _run_ffmpeg_task(
    client: "Client",
    task: dict,
    chat_id: int,
    task_id: str,
    url: str,
    duration: int,
    video_map: int,
    audio_map: list[int],
    custom_filename: str | None,
    upload_mode: str,
    status_msg,
) -> None:
    """Execute a recording using FFmpeg."""
    rec = RecordingProcess()
    active_recordings[task_id] = rec

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
                    await _upload_segment(
                        client, chat_id, fpath, caption, upload_mode
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


async def _run_n3u8dl_task(
    client: "Client",
    task: dict,
    chat_id: int,
    task_id: str,
    url: str,
    duration: int,
    custom_filename: str | None,
    upload_mode: str,
    headers: dict | None,
    drm: dict | None,
    status_msg,
) -> None:
    """Execute a download using N3U8DL-RE (for DRM/MPD streams)."""
    from bot.utils.n3u8dl import N3U8DLProcess, is_available

    if not is_available():
        await client.edit_message_text(
            chat_id, status_msg.id,
            "❌ **N3U8DL-RE is not installed.** "
            "This stream requires N3U8DL-RE for DRM/MPD support.\n"
            "Ask the bot admin to install it.",
        )
        await delete_task(task_id)
        task_queue.task_done()
        return

    proc = N3U8DLProcess()
    # Store in active_recordings so cancel works
    # We use a duck-type wrapper that has .cancel() and .cancelled
    active_recordings[task_id] = proc  # type: ignore[assignment]

    try:
        await proc.start(
            url, task_id,
            duration=duration,
            headers=headers,
            drm=drm,
            filename_prefix=custom_filename,
        )

        await client.edit_message_text(
            chat_id, status_msg.id,
            f"🔴 **Downloading (N3U8DL-RE)**\n🆔 Task: `{task_id}`\n"
            "⏳ This may take a while…",
        )

        await proc.wait()

        files = proc.output_files()
        if files:
            await client.edit_message_text(
                chat_id, status_msg.id,
                f"✅ **Download complete** – uploading {len(files)} file(s)…",
            )
            for idx, fpath in enumerate(files, 1):
                caption = (
                    f"📹 File {idx}/{len(files)}\n"
                    f"🆔 Task: `{task_id}`"
                )
                try:
                    await _upload_segment(
                        client, chat_id, fpath, caption, upload_mode
                    )
                except Exception as exc:
                    log.error("Upload failed for %s: %s", fpath, exc)
                    await client.send_message(
                        chat_id,
                        f"⚠️ Upload failed for file {idx}: `{exc}`",
                    )
        else:
            await client.edit_message_text(
                chat_id, status_msg.id,
                "⚠️ **No output files generated.** "
                "The stream may be unavailable or DRM decryption failed.",
            )

    except Exception as exc:
        log.exception("N3U8DL-RE error for task %s", task_id)
        try:
            await client.edit_message_text(
                chat_id, status_msg.id, f"❌ **Error:** `{exc}`"
            )
        except Exception:
            pass
    finally:
        proc.cleanup()
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
    # Bulk-delete all remaining DB entries for this user
    await delete_user_tasks(user_id)
    return count
