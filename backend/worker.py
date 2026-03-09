"""
worker.py — Async task queue and recording worker pool.

The queue accepts task_data dicts.  NUM_WORKERS coroutines consume the queue
concurrently.  Each worker:
  1. Marks the task as 'recording' in MongoDB.
  2. Calls ffmpeg_utils.run_ffmpeg() with a live on_progress callback.
  3. Uploads each output segment to Telegram.
  4. Cleans up temp files and marks the task 'done' / 'cancelled' / 'failed'.
"""

import asyncio
import glob
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pyrogram import Client
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config
import database as db
from ffmpeg_utils import (
    extract_thumbnail,
    format_duration,
    get_video_duration,
    make_progress_bar,
    parse_time_to_secs,
    run_ffmpeg,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Shared state (module-level)
# ─────────────────────────────────────────────────────────────────────────────

# The Pyrogram client — set by start_workers()
_bot_client: Optional[Client] = None

# asyncio queue — task_data dicts are pushed here
task_queue: asyncio.Queue = asyncio.Queue()

# Map task_id → cancel asyncio.Event (so handlers can cancel running tasks)
active_cancel_events: Dict[str, asyncio.Event] = {}

# Running worker asyncio Tasks
_worker_tasks: List[asyncio.Task] = []


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def start_workers(client: Client, num_workers: Optional[int] = None) -> None:
    """Spawn worker coroutines and keep a reference to each Task."""
    global _bot_client, _worker_tasks
    _bot_client = client
    n = num_workers or config.NUM_WORKERS
    _worker_tasks = [asyncio.create_task(_worker_loop(i)) for i in range(n)]
    logger.info("Started %d recording workers", n)


async def stop_workers() -> None:
    """Cancel all worker tasks and wait for them to finish."""
    for t in _worker_tasks:
        t.cancel()
    await asyncio.gather(*_worker_tasks, return_exceptions=True)
    _worker_tasks.clear()
    logger.info("All workers stopped.")


async def enqueue_task(task_data: Dict[str, Any]) -> None:
    """Add a task to the processing queue."""
    await task_queue.put(task_data)


async def cancel_task(task_id: str) -> bool:
    """Signal a running recording to stop.  Returns True if found."""
    event = active_cancel_events.get(task_id)
    if event:
        event.set()
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Worker loop
# ─────────────────────────────────────────────────────────────────────────────

async def _worker_loop(worker_id: int) -> None:
    logger.info("Worker %d ready", worker_id)
    while True:
        task_data: Dict[str, Any] = await task_queue.get()
        tid = task_data.get("task_id", "?")
        logger.info("Worker %d picked up task %s", worker_id, tid)
        try:
            await _process_task(task_data)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.error("Worker %d — task %s failed: %s", worker_id, tid, exc, exc_info=True)
            await _task_failed(task_data, str(exc))
        finally:
            task_queue.task_done()


# ─────────────────────────────────────────────────────────────────────────────
# Core task processor
# ─────────────────────────────────────────────────────────────────────────────

async def _process_task(task_data: Dict[str, Any]) -> None:
    task_id = task_data["task_id"]
    user_id = task_data["user_id"]
    chat_id = task_data["chat_id"]
    url = task_data["url"]
    video_idx = task_data.get("video_stream_idx", 0)
    audio_idx = task_data.get("audio_stream_idx", 1)
    duration = task_data.get("duration", 1800)
    progress_msg_id = task_data.get("progress_msg_id")

    # Cancel event — set externally to abort recording
    cancel_event = asyncio.Event()
    active_cancel_events[task_id] = cancel_event

    # Mark recording start
    await db.update_task(task_id, {
        "status": "recording",
        "started_at": datetime.now(timezone.utc),
    })

    # Prepare output directory
    output_dir = os.path.join(config.OUTPUT_DIR, task_id)
    os.makedirs(output_dir, exist_ok=True)
    output_base = os.path.join(output_dir, "segment")

    # ── Progress callback ──────────────────────────────────────────────────
    _last_edit: List[float] = [0.0]

    async def on_progress(prog: Dict[str, str]) -> None:
        now = asyncio.get_event_loop().time()
        if now - _last_edit[0] < config.PROGRESS_UPDATE_INTERVAL:
            return
        _last_edit[0] = now

        elapsed_secs = parse_time_to_secs(prog.get("out_time", "0"))
        speed_raw = prog.get("speed", "?x").strip()
        size_bytes = int(prog.get("total_size", 0) or 0)
        bitrate = prog.get("bitrate", "?kbits/s")
        frame = prog.get("frame", "0")

        percent = min((elapsed_secs / duration * 100) if duration > 0 else 0, 100)
        size_mb = size_bytes / (1024 * 1024)
        size_str = (
            f"{size_mb:.1f} MB" if size_mb < 1024 else f"{size_mb / 1024:.2f} GB"
        )
        elapsed_str = format_duration(int(elapsed_secs))
        total_str = format_duration(duration)

        try:
            speed_val = float(speed_raw.rstrip("x"))
            eta_secs = (duration - elapsed_secs) / speed_val if speed_val > 0 else 0
        except ValueError:
            eta_secs = 0
        eta_str = format_duration(int(eta_secs))

        text = (
            "🔴 **Recording in Progress**\n\n"
            f"{make_progress_bar(percent)}\n\n"
            f"⏳ `{elapsed_str}` / `{total_str}`\n"
            f"⏱ ETA: `{eta_str}`\n"
            f"💾 Size: `{size_str}`\n"
            f"🚀 Speed: `{speed_raw}`\n"
            f"📡 Bitrate: `{bitrate}`\n"
            f"🎞 Frame: `{frame}`\n"
            f"📂 Task: `{task_id[:8]}`"
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel Recording", callback_data=f"ct_{task_id[:12]}")
        ]])

        await _safe_edit(chat_id, progress_msg_id, text, keyboard)
        await db.update_task(task_id, {
            "progress": round(percent, 1),
            "size_mb": round(size_mb, 2),
            "elapsed_secs": int(elapsed_secs),
        })

    # ── Run FFmpeg ─────────────────────────────────────────────────────────
    try:
        return_code, ffmpeg_log = await run_ffmpeg(
            task_id=task_id,
            url=url,
            output_base=output_base,
            video_stream_idx=video_idx,
            audio_stream_idx=audio_idx,
            duration=duration,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
    finally:
        active_cancel_events.pop(task_id, None)

    # Save FFmpeg log (last 4000 chars)
    await db.update_task(task_id, {"ffmpeg_log": ffmpeg_log[-4000:] if ffmpeg_log else ""})

    # ── Collect output files ───────────────────────────────────────────────
    output_files = sorted(glob.glob(os.path.join(output_dir, "segment_*.mp4")))

    # Handle cancellation
    was_cancelled = cancel_event.is_set()
    if was_cancelled and not output_files:
        await db.update_task(task_id, {
            "status": "cancelled",
            "ended_at": datetime.now(timezone.utc),
        })
        await _safe_edit(
            chat_id, progress_msg_id,
            f"⚠️ Recording `{task_id[:8]}` was cancelled. No segments recorded.",
        )
        shutil.rmtree(output_dir, ignore_errors=True)
        return

    if not output_files:
        error_tail = ffmpeg_log[-500:] if ffmpeg_log else "No output"
        await _task_failed(task_data, f"FFmpeg produced no output.\n\nLog tail:\n```\n{error_tail}\n```")
        shutil.rmtree(output_dir, ignore_errors=True)
        return

    # ── Upload segments ────────────────────────────────────────────────────
    await db.update_task(task_id, {"status": "uploading"})
    total_segs = len(output_files)
    await _safe_edit(
        chat_id, progress_msg_id,
        f"📤 **Uploading {total_segs} segment(s)…**\nTask: `{task_id[:8]}`",
    )

    uploaded_count = 0
    for i, file_path in enumerate(output_files, 1):
        try:
            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            await _safe_edit(
                chat_id, progress_msg_id,
                f"📤 **Uploading segment {i}/{total_segs}**\n"
                f"💾 `{file_size_mb:.1f} MB`\nTask: `{task_id[:8]}`",
            )
            thumb = await extract_thumbnail(file_path)
            vid_dur = await get_video_duration(file_path)

            status_tag = "⚠️ Partial — cancelled" if (was_cancelled and i == total_segs) else ""
            caption = (
                f"📹 **Segment {i}/{total_segs}**\n"
                f"📂 Task: `{task_id[:8]}`\n"
                f"💾 Size: `{file_size_mb:.1f} MB`"
                + (f"\n{status_tag}" if status_tag else "")
            )

            await _send_video_with_retry(
                chat_id=chat_id,
                file_path=file_path,
                thumb=thumb,
                duration=vid_dur,
                caption=caption,
            )
            uploaded_count += 1

            # Clean up thumbnail
            if thumb and os.path.exists(thumb):
                os.remove(thumb)

        except Exception as exc:
            logger.error("Upload error for segment %s: %s", file_path, exc)

    # ── Finalise ───────────────────────────────────────────────────────────
    shutil.rmtree(output_dir, ignore_errors=True)

    final_status = "cancelled" if was_cancelled else "done"
    await db.update_task(task_id, {
        "status": final_status,
        "ended_at": datetime.now(timezone.utc),
        "segments": total_segs,
        "uploaded_segments": uploaded_count,
    })

    # Increment user's recording count
    await db.increment_user_recordings(user_id)

    status_icon = "✅" if final_status == "done" else "⚠️"
    await _safe_edit(
        chat_id, progress_msg_id,
        f"{status_icon} **Recording Complete!**\n\n"
        f"📂 Task: `{task_id[:8]}`\n"
        f"📦 Segments: `{total_segs}`\n"
        f"⬆️ Uploaded: `{uploaded_count}`\n"
        f"{'_(Recording was cancelled mid-way)_' if was_cancelled else ''}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ─────────────────────────────────────────────────────────────────────────────

async def _task_failed(task_data: Dict[str, Any], error: str) -> None:
    task_id = task_data["task_id"]
    chat_id = task_data["chat_id"]
    progress_msg_id = task_data.get("progress_msg_id")

    await db.update_task(task_id, {
        "status": "failed",
        "error": error[:500],
        "ended_at": datetime.now(timezone.utc),
    })
    await _safe_edit(
        chat_id, progress_msg_id,
        f"❌ **Task Failed**\n📂 `{task_id[:8]}`\n\n{error[:300]}",
    )


async def _safe_edit(
    chat_id: int,
    message_id: Optional[int],
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Edit a message, silently ignoring errors."""
    if not message_id or not _bot_client:
        return
    try:
        await _bot_client.edit_message_text(
            chat_id, message_id, text, reply_markup=reply_markup
        )
    except FloodWait as fw:
        await asyncio.sleep(fw.value)
    except RPCError:
        pass
    except Exception as exc:
        logger.debug("_safe_edit error: %s", exc)


async def _send_video_with_retry(
    chat_id: int,
    file_path: str,
    thumb: Optional[str],
    duration: int,
    caption: str,
    retries: int = 3,
) -> None:
    """Send a video to Telegram with exponential-backoff retry."""
    for attempt in range(1, retries + 1):
        try:
            await _bot_client.send_video(
                chat_id,
                file_path,
                thumb=thumb,
                duration=duration,
                caption=caption,
                supports_streaming=True,
            )
            return
        except FloodWait as fw:
            await asyncio.sleep(fw.value)
        except Exception as exc:
            if attempt == retries:
                raise
            wait = 5 * attempt
            logger.warning("Upload attempt %d failed (%s). Retrying in %ds…", attempt, exc, wait)
            await asyncio.sleep(wait)
