"""M3U / M3U8 playlist → JSON converter."""

import json
import logging
import os
import re
import tempfile
from typing import Optional
from urllib.parse import urlparse

log = logging.getLogger(__name__)


def slugify(text: str) -> str:
    """Convert *text* into a URL/key-safe slug.

    Removes common country-code prefixes (e.g. ``IN: ``), lowercases the
    string, strips non-word characters, and replaces whitespace/hyphens
    with underscores.
    """
    # Remove two-letter country prefix like "IN: ", "US: ", etc.
    text = re.sub(r'^[A-Za-z]{2}:\s*', '', text)
    text = text.lower()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s-]+', '_', text)
    return text.strip('_')


def _parse_exthttp(line: str) -> dict[str, str]:
    """Parse ``#EXTHTTP:{...}`` JSON into a plain dict of headers."""
    raw = line.split(":", 1)[1].strip() if ":" in line else ""
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            result: dict[str, str] = {}
            for k, v in obj.items():
                # Normalise header names: cookie → Cookie
                result[k.strip().title()] = str(v).strip()
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    return {}


def convert_m3u_to_json(
    m3u_filepath: str,
    json_filepath: str,
) -> Optional[str]:
    """Parse an M3U/M3U8 file and write the channels as a JSON list.

    Each channel becomes a dict with ``name``, ``url``, and ``group`` keys.
    Channels that include ``#KODIPROP``, ``#EXTVLCOPT``, or ``#EXTHTTP``
    directives will also have ``headers`` and/or ``drm`` keys:

    .. code-block:: json

       {
         "name": "Sony Yay Hindi",
         "url": "https://…/index.mpd?…",
         "group": "Kids",
         "headers": {"User-Agent": "…", "Cookie": "…"},
         "drm": {"type": "clearkey", "key": "kid:key"}
       }

    Returns the *json_filepath* on success, or ``None`` on failure.
    """
    channels: list[dict] = []

    try:
        with open(m3u_filepath, 'r', encoding='utf-8') as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        log.error("M3U file not found: %s", m3u_filepath)
        return None
    except Exception as exc:
        log.error("Failed to read M3U file %s: %s", m3u_filepath, exc)
        return None

    if not lines:
        log.warning("M3U file is empty: %s", m3u_filepath)
        return None

    current_info: dict = {}
    current_headers: dict[str, str] = {}
    current_drm: dict[str, str] = {}

    for line in lines:
        line = line.strip()

        if line.startswith('#EXTINF:'):
            group_match = re.search(r'group-title="([^"]*)"', line)
            group_title = group_match.group(1) if group_match else ""

            name_match = re.search(r',([^,]+)$', line)
            channel_name = (
                name_match.group(1).strip() if name_match else "Unknown Channel"
            )

            current_info = {
                'group_title': group_title,
                'name': channel_name,
            }
            # Reset per-channel accumulated directives
            current_headers = {}
            current_drm = {}

        elif line.startswith('#KODIPROP:'):
            prop_val = line.split(":", 1)[1].strip() if ":" in line else ""
            if "license_type=" in prop_val:
                current_drm["type"] = prop_val.rsplit("=", 1)[-1].strip()
            elif "license_key=" in prop_val:
                current_drm["key"] = prop_val.rsplit("=", 1)[-1].strip()

        elif line.startswith('#EXTVLCOPT:'):
            opt = line.split(":", 1)[1].strip() if ":" in line else ""
            if opt.startswith("http-user-agent="):
                current_headers["User-Agent"] = opt.split("=", 1)[1].strip()
            elif opt.startswith("http-referrer="):
                current_headers["Referer"] = opt.split("=", 1)[1].strip()
            elif opt.startswith("http-origin="):
                current_headers["Origin"] = opt.split("=", 1)[1].strip()

        elif line.startswith('#EXTHTTP:'):
            current_headers.update(_parse_exthttp(line))

        elif (line.startswith('http://') or line.startswith('https://')) and 'name' in current_info:
            entry: dict = {
                "name": current_info['name'],
                "url": line,
                "group": current_info.get('group_title', ''),
            }
            if current_headers:
                entry["headers"] = dict(current_headers)
            if current_drm:
                entry["drm"] = dict(current_drm)
            channels.append(entry)
            current_info = {}
            current_headers = {}
            current_drm = {}

    if not channels:
        log.warning("No channels found in M3U file: %s", m3u_filepath)
        return None

    try:
        os.makedirs(os.path.dirname(json_filepath) or '.', exist_ok=True)
        with open(json_filepath, 'w', encoding='utf-8') as fh:
            json.dump(channels, fh, indent=4, ensure_ascii=False)
    except Exception as exc:
        log.error("Failed to write JSON file %s: %s", json_filepath, exc)
        return None

    log.info(
        "Converted %d channel(s) from %s → %s",
        len(channels), m3u_filepath, json_filepath,
    )
    return json_filepath


async def download_m3u_url(
    url: str,
    json_filepath: str,
) -> Optional[str]:
    """Download an M3U/M3U8 playlist from *url* and convert it to JSON.

    Returns the *json_filepath* on success, or ``None`` on failure.
    """
    import asyncio

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        log.error("Unsupported URL scheme: %s", parsed.scheme)
        return None

    # Download using curl (available on almost all systems)
    with tempfile.NamedTemporaryFile(
        suffix=".m3u", delete=False, mode="wb"
    ) as tmp:
        tmp_path = tmp.name

    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-fsSL", "--max-time", "30", "-o", tmp_path, url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            log.error("Failed to download M3U from %s: %s", url, err)
            return None

        return convert_m3u_to_json(tmp_path, json_filepath)
    except Exception as exc:
        log.error("Error downloading M3U from %s: %s", url, exc)
        return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
