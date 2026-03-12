"""N3U8DL-RE download helper for DRM/MPD streams."""

import asyncio
import logging
import os
import shutil

from bot.config import DOWNLOAD_DIR, N3U8DL_PATH

log = logging.getLogger(__name__)


def is_available() -> bool:
    """Return True if the N3U8DL-RE binary is found on PATH."""
    return shutil.which(N3U8DL_PATH) is not None


def build_n3u8dl_cmd(
    url: str,
    save_dir: str,
    save_name: str,
    duration: int | None = None,
    headers: dict[str, str] | None = None,
    drm: dict[str, str] | None = None,
) -> list[str]:
    """Build the N3U8DL-RE command list.

    Parameters
    ----------
    url:
        Stream URL (HLS, DASH/MPD, etc.).
    save_dir:
        Directory to write output files into.
    save_name:
        Base name for the output file (without extension).
    duration:
        Optional recording duration limit in seconds.
    headers:
        Optional HTTP headers (User-Agent, Cookie, Referer, …).
    drm:
        Optional DRM dict with ``type`` and ``key`` (e.g. ClearKey
        ``kid:key``).
    """
    cmd = [
        N3U8DL_PATH,
        url,
        "--save-dir", save_dir,
        "--save-name", save_name,
        "-M", "format=mp4",
        "--auto-select",
        "--no-log",
    ]

    if headers:
        for k, v in headers.items():
            cmd.extend(["-H", f"{k}: {v}"])

    if drm:
        key_val = drm.get("key", "")
        if key_val:
            cmd.extend(["--key", key_val])

    if duration and duration > 0:
        # N3U8DL-RE uses --live-record-limit HH:MM:SS for live recording cap
        h, rem = divmod(duration, 3600)
        m, s = divmod(rem, 60)
        cmd.extend(["--live-record-limit", f"{h:02d}:{m:02d}:{s:02d}"])

    return cmd


class N3U8DLProcess:
    """Wrapper around an N3U8DL-RE subprocess."""

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.cancelled = False
        self.output_dir: str = ""
        self.save_name: str = ""

    async def start(
        self,
        url: str,
        task_id: str,
        duration: int | None = None,
        headers: dict[str, str] | None = None,
        drm: dict[str, str] | None = None,
        filename_prefix: str | None = None,
    ) -> None:
        import re as _re

        self.output_dir = os.path.join(DOWNLOAD_DIR, task_id)
        os.makedirs(self.output_dir, exist_ok=True)

        if filename_prefix:
            safe = _re.sub(r'[^\w\s\-]', '', filename_prefix).strip()
            self.save_name = safe.replace(" ", "_") if safe else "output"
        else:
            self.save_name = "output"

        cmd = build_n3u8dl_cmd(
            url,
            save_dir=self.output_dir,
            save_name=self.save_name,
            duration=duration,
            headers=headers,
            drm=drm,
        )
        log.info("N3U8DL-RE cmd: %s", " ".join(cmd))

        # N3U8DL-RE is a .NET application that requires libicu.
        # Set DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1 to avoid
        # "Couldn't find a valid ICU package" crash on systems
        # where libicu is not installed.
        env = os.environ.copy()
        env["DOTNET_SYSTEM_GLOBALIZATION_INVARIANT"] = "1"

        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

    async def wait(self) -> int:
        """Wait for the process to finish, read and log stderr."""
        if self.proc is None:
            return -1
        stdout, stderr = await self.proc.communicate()
        rc = self.proc.returncode or -1
        if stderr:
            text = stderr.decode(errors="replace").strip()
            if text:
                log.warning("N3U8DL-RE stderr (rc=%d):\n%s", rc, text)
        if rc != 0:
            log.warning("N3U8DL-RE exited with code %d", rc)
        return rc

    def cancel(self) -> None:
        """Send SIGTERM to the process."""
        self.cancelled = True
        if self.proc and self.proc.returncode is None:
            try:
                pid = self.proc.pid
                if pid is not None:
                    os.kill(pid, __import__("signal").SIGTERM)
            except OSError:
                pass

    def output_files(self) -> list[str]:
        """Return sorted list of media files in the output directory."""
        if not os.path.isdir(self.output_dir):
            return []
        files = sorted(
            os.path.join(self.output_dir, f)
            for f in os.listdir(self.output_dir)
            if f.endswith((".mp4", ".mkv", ".ts"))
        )
        return files

    def cleanup(self) -> None:
        """Remove all output files and directory."""
        for f in self.output_files():
            try:
                os.remove(f)
            except OSError:
                pass
        # Remove any leftover non-mp4 artefacts
        if os.path.isdir(self.output_dir):
            for f in os.listdir(self.output_dir):
                try:
                    os.remove(os.path.join(self.output_dir, f))
                except OSError:
                    pass
        try:
            os.rmdir(self.output_dir)
        except OSError:
            pass
