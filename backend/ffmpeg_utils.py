"""
ffmpeg_utils.py — Async wrappers around FFprobe and FFmpeg.

Key public functions:
  run_ffprobe(url)             → dict with 'video_tracks' and 'audio_tracks'
  build_track_keyboard(...)    → pyrogram InlineKeyboardMarkup
  run_ffmpeg(...)              → (return_code, ffmpeg_stderr_log)
  extract_thumbnail(path)      → thumbnail path or None
  get_video_duration(path)     → seconds (int)
"""

import asyncio
import json
import logging
import os
import re
import signal
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

import config

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_progress_bar(percent: float, width: int = 20) -> str:
    """Return a Unicode block progress bar with percentage label."""
    filled = int(width * percent / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"`[{bar}]` **{percent:.1f}%**"


def format_duration(secs: int) -> str:
    """Convert seconds to HH:MM:SS string."""
    secs = max(0, int(secs))
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_time_to_secs(time_str: str) -> float:
    """Parse HH:MM:SS.mmm into float seconds."""
    try:
        time_str = time_str.split(".")[0]  # strip microseconds
        parts = time_str.split(":")
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        return float(time_str)
    except Exception:
        return 0.0


def _ffmpeg_headers() -> str:
    """Return formatted HTTP headers string for FFmpeg -headers flag."""
    return (
        f"User-Agent: {config.FFMPEG_USER_AGENT}\r\n"
        f"Referer: {config.FFMPEG_REFERER}\r\n"
    )


# ─────────────────────────────────────────────────────────────────────────────
# FFprobe — stream analysis
# ─────────────────────────────────────────────────────────────────────────────

async def run_ffprobe(url: str) -> Dict[str, Any]:
    """
    Run ffprobe on *url* and return a dict:
      {
        'video_tracks': [{'index': int, 'resolution': str, 'codec': str, 'fps': str}, ...],
        'audio_tracks': [{'index': int, 'lang': str, 'codec': str, 'channels': int}, ...],
        'raw': <full ffprobe JSON dict>
      }
    Raises RuntimeError if ffprobe fails or the stream is unreachable.
    """
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        "-user_agent", config.FFMPEG_USER_AGENT,
        "-headers", f"Referer: {config.FFMPEG_REFERER}\r\n",
        url,
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("FFprobe timed out (30 s). The stream may be unreachable.")

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        raise RuntimeError(f"FFprobe failed:\n{err[:800]}")

    try:
        probe = json.loads(stdout.decode())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FFprobe returned invalid JSON: {exc}")

    streams = probe.get("streams", [])
    video_tracks: List[Dict[str, Any]] = []
    audio_tracks: List[Dict[str, Any]] = []

    for stream in streams:
        idx = stream.get("index", 0)
        codec_type = stream.get("codec_type", "")

        if codec_type == "video":
            w = stream.get("width", 0)
            h = stream.get("height", 0)
            codec = stream.get("codec_name", "?")
            # frame rate: "30/1" or "30000/1001"
            fps_str = stream.get("r_frame_rate", "?")
            try:
                num, den = fps_str.split("/")
                fps = round(int(num) / int(den), 2)
                fps_display = f"{fps:.0f}fps"
            except Exception:
                fps_display = fps_str

            resolution = f"{h}p" if h else "?"
            video_tracks.append({
                "index": idx,
                "resolution": resolution,
                "width": w,
                "height": h,
                "codec": codec.upper(),
                "fps": fps_display,
                "label": f"{resolution} {codec.upper()} {fps_display}",
            })

        elif codec_type == "audio":
            lang = (
                stream.get("tags", {}).get("language")
                or stream.get("tags", {}).get("LANGUAGE")
                or "und"
            )
            codec = stream.get("codec_name", "?")
            channels = stream.get("channels", 0)
            channel_label = {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(channels, f"{channels}ch")
            audio_tracks.append({
                "index": idx,
                "lang": lang.upper(),
                "codec": codec.upper(),
                "channels": channels,
                "label": f"{lang.upper()} {codec.upper()} {channel_label}",
            })

    # Sort video by resolution descending
    video_tracks.sort(key=lambda t: t.get("height", 0), reverse=True)

    return {"video_tracks": video_tracks, "audio_tracks": audio_tracks, "raw": probe}


# ─────────────────────────────────────────────────────────────────────────────
# Inline keyboard builder for track selection
# ─────────────────────────────────────────────────────────────────────────────

def build_track_keyboard(
    task_id: str,
    tracks: Dict[str, Any],
    sel_vid: int = 0,
    sel_aud: int = 0,
) -> InlineKeyboardMarkup:
    """
    Build an InlineKeyboardMarkup for video + audio track selection.
    Callback data format (always ≤ 64 bytes):
      sv_{task_id}_{idx}  — select video track
      sa_{task_id}_{idx}  — select audio track
      sr_{task_id}        — start recording
      xp_{task_id}        — cancel / discard pending selection
    """
    tid = task_id[:12]  # Use first 12 chars of UUID
    rows: List[List[InlineKeyboardButton]] = []

    video_tracks = tracks.get("video_tracks", [])
    audio_tracks = tracks.get("audio_tracks", [])

    # ── Video track row(s) ─────────────────────────────────────────────────
    if video_tracks:
        rows.append([InlineKeyboardButton("🎥 Video Track:", callback_data="noop")])
        vid_row: List[InlineKeyboardButton] = []
        for i, vt in enumerate(video_tracks):
            check = "✅ " if i == sel_vid else ""
            btn_text = f"{check}{vt['resolution']}"
            vid_row.append(InlineKeyboardButton(btn_text, callback_data=f"sv_{tid}_{i}"))
            # Max 4 per row
            if len(vid_row) == 4:
                rows.append(vid_row)
                vid_row = []
        if vid_row:
            rows.append(vid_row)
    else:
        rows.append([InlineKeyboardButton("🎥 No video tracks found", callback_data="noop")])

    # ── Audio track row(s) ─────────────────────────────────────────────────
    if audio_tracks:
        rows.append([InlineKeyboardButton("🔊 Audio Track:", callback_data="noop")])
        aud_row: List[InlineKeyboardButton] = []
        for i, at in enumerate(audio_tracks):
            check = "✅ " if i == sel_aud else ""
            btn_text = f"{check}{at['lang']}"
            aud_row.append(InlineKeyboardButton(btn_text, callback_data=f"sa_{tid}_{i}"))
            if len(aud_row) == 4:
                rows.append(aud_row)
                aud_row = []
        if aud_row:
            rows.append(aud_row)
    else:
        rows.append([InlineKeyboardButton("🔊 No audio tracks found", callback_data="noop")])

    # ── Action buttons ─────────────────────────────────────────────────────
    rows.append([
        InlineKeyboardButton("✅ Start Recording", callback_data=f"sr_{tid}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"xp_{tid}"),
    ])

    return InlineKeyboardMarkup(rows)


# ─────────────────────────────────────────────────────────────────────────────
# FFmpeg — live recording with real-time progress
# ─────────────────────────────────────────────────────────────────────────────

async def run_ffmpeg(
    task_id: str,
    url: str,
    output_base: str,
    video_stream_idx: int,
    audio_stream_idx: int,
    duration: int,
    on_progress: Callable[[Dict[str, Any]], Coroutine],
    cancel_event: asyncio.Event,
) -> Tuple[int, str]:
    """
    Run FFmpeg to record *url* into segmented MP4 files.

    Output files: {output_base}_000.mp4, {output_base}_001.mp4, …

    - Reconnects automatically on stream drops.
    - Sends progress to stdout via `-progress pipe:1`.
    - Monitors *cancel_event*; if set, sends SIGTERM to the FFmpeg process.
    - Returns (return_code, stderr_log_string).
    """
    segment_pattern = f"{output_base}_%03d.mp4"
    segment_list_file = f"{output_base}_segments.txt"

    cmd = [
        "ffmpeg",
        # Stream reconnection
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        # HTTP headers
        "-user_agent", config.FFMPEG_USER_AGENT,
        "-headers", f"Referer: {config.FFMPEG_REFERER}\r\n",
        # Input
        "-i", url,
        # Track mapping
        "-map", f"0:{video_stream_idx}",
        "-map", f"0:{audio_stream_idx}",
        # Codec: copy streams without re-encoding
        "-c:v", "copy",
        "-c:a", "copy",
        # Stop after duration seconds
        "-t", str(duration),
        # Segmented MP4 output
        "-f", "segment",
        "-segment_time", str(config.MAX_SEGMENT_SECS),
        "-segment_format", "mp4",
        "-segment_list", segment_list_file,
        "-reset_timestamps", "1",
        # Send structured progress to stdout
        "-progress", "pipe:1",
        # Overwrite existing segments
        "-y",
        segment_pattern,
    ]

    logger.info("FFmpeg command: %s", " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # New process group so we can SIGTERM the whole group
        preexec_fn=os.setsid,
    )

    stderr_lines: List[str] = []

    async def _read_stderr():
        """Collect stderr lines for error reporting."""
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").rstrip()
            stderr_lines.append(line)
            if len(stderr_lines) > 200:  # Keep last 200 lines
                stderr_lines.pop(0)

    async def _read_progress():
        """Parse structured progress from stdout and call *on_progress*."""
        data: Dict[str, str] = {}
        async for raw in proc.stdout:
            # Check for cancellation
            if cancel_event.is_set():
                _kill_process(proc)
                return

            line = raw.decode(errors="replace").strip()
            if "=" in line:
                k, v = line.split("=", 1)
                data[k.strip()] = v.strip()
                # FFmpeg emits a full progress block ending with "progress=continue|end"
                if k.strip() == "progress":
                    try:
                        await on_progress(dict(data))
                    except Exception as exc:
                        logger.warning("on_progress callback error: %s", exc)
                    data = {}

    # Poll for cancellation while ffmpeg runs
    async def _cancel_watcher():
        while not cancel_event.is_set():
            await asyncio.sleep(1)
        _kill_process(proc)

    cancel_watcher = asyncio.create_task(_cancel_watcher())

    try:
        await asyncio.gather(_read_stderr(), _read_progress())
        await proc.wait()
    finally:
        cancel_watcher.cancel()
        try:
            await cancel_watcher
        except asyncio.CancelledError:
            pass

    return_code = proc.returncode or 0
    stderr_log = "\n".join(stderr_lines)
    logger.info("FFmpeg finished for task %s with code %s", task_id, return_code)
    return return_code, stderr_log


def _kill_process(proc: asyncio.subprocess.Process) -> None:
    """Send SIGTERM to the FFmpeg process group."""
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        logger.info("Sent SIGTERM to FFmpeg process group %s", pgid)
    except (ProcessLookupError, OSError):
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Post-processing helpers
# ─────────────────────────────────────────────────────────────────────────────

async def extract_thumbnail(video_path: str) -> Optional[str]:
    """
    Extract a JPEG thumbnail from the first frame of *video_path*.
    Returns the thumbnail path or None on failure.
    """
    thumb_path = video_path.replace(".mp4", "_thumb.jpg")
    cmd = [
        "ffmpeg",
        "-i", video_path,
        "-ss", "00:00:03",
        "-vframes", "1",
        "-vf", "scale=320:-1",
        "-q:v", "3",
        "-y",
        thumb_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=15)
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
    except Exception as exc:
        logger.warning("Thumbnail extraction failed for %s: %s", video_path, exc)
    return None


async def get_video_duration(video_path: str) -> int:
    """Return the duration of *video_path* in seconds (0 on failure)."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        video_path,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        info = json.loads(stdout.decode())
        duration = float(info.get("format", {}).get("duration", 0))
        return int(duration)
    except Exception as exc:
        logger.warning("Duration probe failed for %s: %s", video_path, exc)
        return 0
