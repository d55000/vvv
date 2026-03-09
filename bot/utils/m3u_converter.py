"""M3U / M3U8 playlist → JSON converter."""

import json
import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)


def slugify(text: str) -> str:
    """Convert *text* into a slug suitable for a dictionary key.

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


def convert_m3u_to_json(
    m3u_filepath: str,
    json_filepath: str,
) -> Optional[str]:
    """Parse an M3U/M3U8 file and write the channels as a JSON list.

    Each channel becomes a dict with ``name``, ``url``, and ``group`` keys.
    The output is a JSON **list** so it is directly compatible with the
    existing :func:`bot.utils.channels._load_all_channels` loader.

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

        elif line.startswith('http') and 'name' in current_info:
            channels.append({
                "name": current_info['name'],
                "url": line,
                "group": current_info.get('group_title', ''),
            })
            current_info = {}

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
