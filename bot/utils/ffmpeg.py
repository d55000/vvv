"""FFmpeg & FFprobe async wrappers."""

import asyncio
import json
import logging
import os
import re
import signal
from typing import Optional

from bot.config import DOWNLOAD_DIR, MAX_FILE_SIZE

log = logging.getLogger(__name__)


# ── FFprobe ───────────────────────────────────────────────────────────────


async def probe_streams(url: str) -> dict:
    """Run ffprobe on *url* and return parsed JSON with stream info."""
    cmd = [
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        url,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffprobe failed (rc={proc.returncode}): {stderr.decode(errors='replace')}"
        )
    return json.loads(stdout.decode())


def parse_tracks(probe_data: dict) -> dict:
    """Extract video / audio tracks from ffprobe JSON.

    Returns::

        {
            "video": [{"index": 0, "codec": "h264", "resolution": "1920x1080"}, ...],
            "audio": [{"index": 1, "codec": "aac", "language": "eng"}, ...],
        }
    """
    video, audio = [], []
    for s in probe_data.get("streams", []):
        if s.get("codec_type") == "video":
            w = s.get("width", "?")
            h = s.get("height", "?")
            video.append({
                "index": s["index"],
                "codec": s.get("codec_name", "?"),
                "resolution": f"{w}x{h}",
            })
        elif s.get("codec_type") == "audio":
            tags = s.get("tags", {})
            lang = tags.get("language", tags.get("title", f"trk{s['index']}"))
            audio.append({
                "index": s["index"],
                "codec": s.get("codec_name", "?"),
                "language": lang,
            })
    return {"video": video, "audio": audio}


# ── FFmpeg recording ──────────────────────────────────────────────────────


def _build_record_cmd(
    url: str,
    output_template: str,
    duration: int,
    video_map: int = 0,
    audio_map: int = 1,
    segment_size_bytes: int = int(MAX_FILE_SIZE),
) -> list[str]:
    """Build the FFmpeg command list for segmented recording."""
    return [
        "ffmpeg",
        "-y",
        "-i", url,
        "-map", f"0:{video_map}",
        "-map", f"0:{audio_map}",
        "-c", "copy",
        "-t", str(duration),
        "-f", "segment",
        "-segment_time", "3600",
        "-segment_format", "mp4",
        "-fs", str(segment_size_bytes),
        "-reset_timestamps", "1",
        "-progress", "pipe:1",
        output_template,
    ]


class RecordingProcess:
    """Wrapper around an FFmpeg subprocess with progress parsing."""

    def __init__(self) -> None:
        self.proc: Optional[asyncio.subprocess.Process] = None
        self.cancelled = False
        self.output_dir: str = ""
        self.output_template: str = ""
        # Live stats
        self.elapsed_us: int = 0
        self.speed: str = "0x"
        self.total_size: int = 0

    async def start(
        self,
        url: str,
        task_id: str,
        duration: int,
        video_map: int = 0,
        audio_map: int = 1,
        filename_prefix: str | None = None,
    ) -> None:
        self.output_dir = os.path.join(DOWNLOAD_DIR, task_id)
        os.makedirs(self.output_dir, exist_ok=True)

        # Sanitise the user-supplied prefix; fall back to "part"
        if filename_prefix:
            safe = re.sub(r'[^\w\s\-]', '', filename_prefix).strip()
            prefix = safe.replace(" ", "_") if safe else "part"
        else:
            prefix = "part"
        self.output_template = os.path.join(self.output_dir, f"{prefix}_%03d.mp4")

        cmd = _build_record_cmd(
            url, self.output_template, duration, video_map, audio_map
        )
        log.info("FFmpeg cmd: %s", " ".join(cmd))

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    async def read_progress(self) -> None:
        """Read ``-progress pipe:1`` output and update live stats."""
        if self.proc is None or self.proc.stdout is None:
            return
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                break
            decoded = line.decode(errors="replace").strip()
            if decoded.startswith("out_time_us="):
                try:
                    self.elapsed_us = int(decoded.split("=", 1)[1])
                except ValueError:
                    pass
            elif decoded.startswith("speed="):
                self.speed = decoded.split("=", 1)[1].strip()
            elif decoded.startswith("total_size="):
                try:
                    self.total_size = int(decoded.split("=", 1)[1])
                except ValueError:
                    pass

    async def wait(self) -> int:
        if self.proc is None:
            return -1
        return await self.proc.wait()

    def cancel(self) -> None:
        """Send SIGTERM to FFmpeg."""
        self.cancelled = True
        if self.proc and self.proc.returncode is None:
            try:
                pid = self.proc.pid
                if pid is not None:
                    os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    def output_files(self) -> list[str]:
        """Return sorted list of generated segment files."""
        if not os.path.isdir(self.output_dir):
            return []
        files = sorted(
            os.path.join(self.output_dir, f)
            for f in os.listdir(self.output_dir)
            if f.endswith(".mp4")
        )
        return files

    def cleanup(self) -> None:
        """Remove all output files."""
        for f in self.output_files():
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(self.output_dir)
        except OSError:
            pass


# ── Progress formatting ───────────────────────────────────────────────────

_SPEED_RE = re.compile(r"([\d.]+)")


def format_progress(rec: RecordingProcess, duration_sec: int) -> str:
    """Build a human‑friendly progress string."""
    elapsed = rec.elapsed_us / 1_000_000
    pct = min(elapsed / duration_sec * 100, 100) if duration_sec else 0
    bar_len = 20
    filled = int(bar_len * pct / 100)
    bar = "█" * filled + "░" * (bar_len - filled)

    size_mb = rec.total_size / (1024 ** 2)

    # ETA
    match = _SPEED_RE.search(rec.speed)
    speed_val = float(match.group(1)) if match else 0
    remaining = duration_sec - elapsed
    if speed_val > 0:
        eta_sec = remaining / speed_val
        eta_m, eta_s = divmod(int(eta_sec), 60)
        eta_str = f"{eta_m}m {eta_s}s"
    else:
        eta_str = "calculating…"

    return (
        f"📊 `[{bar}]` {pct:.1f}%\n"
        f"⏳ ETA: **{eta_str}**\n"
        f"💾 Size: **{size_mb:.1f} MB**\n"
        f"🚀 Speed: **{rec.speed}**"
    )
