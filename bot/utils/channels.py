"""Channel list (JSON) search helpers."""

import json
import logging
import os
from typing import Optional

from bot.config import CHANNEL_LIST_DIR

log = logging.getLogger(__name__)


def _load_all_channels() -> list[dict]:
    """Load and merge all JSON files from the channel_lists directory."""
    channels: list[dict] = []
    if not os.path.isdir(CHANNEL_LIST_DIR):
        return channels
    for fname in os.listdir(CHANNEL_LIST_DIR):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(CHANNEL_LIST_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                if isinstance(data, list):
                    channels.extend(data)
                elif isinstance(data, dict):
                    channels.append(data)
        except Exception as exc:
            log.warning("Failed to load %s: %s", fpath, exc)
    return channels


def search_channels(query: str, limit: int = 10) -> list[dict]:
    """Return channels whose name matches *query* (case-insensitive)."""
    q = query.lower()
    results = []
    for ch in _load_all_channels():
        name = ch.get("name", "")
        if q in name.lower():
            results.append(ch)
            if len(results) >= limit:
                break
    return results


def get_channel_by_name(name: str) -> Optional[dict]:
    q = name.lower()
    for ch in _load_all_channels():
        if ch.get("name", "").lower() == q:
            return ch
    return None


def list_json_files() -> list[str]:
    if not os.path.isdir(CHANNEL_LIST_DIR):
        return []
    return [f for f in os.listdir(CHANNEL_LIST_DIR) if f.endswith(".json")]


def remove_json_file(filename: str) -> bool:
    fpath = os.path.join(CHANNEL_LIST_DIR, filename)
    if os.path.isfile(fpath):
        os.remove(fpath)
        return True
    return False
