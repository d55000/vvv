"""Tests for bot utility modules (no external services required)."""

import asyncio
import json
import os
import tempfile

import pytest

from bot.utils.ffmpeg import (
    RecordingProcess,
    _build_record_cmd,
    format_progress,
    parse_tracks,
)
from bot.utils.channels import (
    get_channel_by_name,
    list_json_files,
    remove_json_file,
    search_channels,
)
from bot.db.database import TIER_CONFIG
from bot.utils.m3u_converter import convert_m3u_to_json, slugify
from bot.handlers.user import parse_rec_args


# ── FFprobe parse_tracks ──────────────────────────────────────────────────


SAMPLE_PROBE = {
    "streams": [
        {
            "index": 0,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
        },
        {
            "index": 1,
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1280,
            "height": 720,
        },
        {
            "index": 2,
            "codec_type": "audio",
            "codec_name": "aac",
            "tags": {"language": "eng"},
        },
        {
            "index": 3,
            "codec_type": "audio",
            "codec_name": "aac",
            "tags": {"language": "spa"},
        },
    ]
}


def test_parse_tracks():
    tracks = parse_tracks(SAMPLE_PROBE)
    assert len(tracks["video"]) == 2
    assert len(tracks["audio"]) == 2
    assert tracks["video"][0]["resolution"] == "1920x1080"
    assert tracks["video"][1]["resolution"] == "1280x720"
    assert tracks["audio"][0]["language"] == "eng"
    assert tracks["audio"][1]["language"] == "spa"


def test_parse_tracks_empty():
    tracks = parse_tracks({"streams": []})
    assert tracks["video"] == []
    assert tracks["audio"] == []


def test_parse_tracks_missing_tags():
    data = {
        "streams": [
            {"index": 0, "codec_type": "audio", "codec_name": "aac"},
        ]
    }
    tracks = parse_tracks(data)
    assert len(tracks["audio"]) == 1
    assert tracks["audio"][0]["language"] == "trk0"


# ── Build record command ──────────────────────────────────────────────────


def test_build_record_cmd():
    cmd = _build_record_cmd(
        "http://example.com/stream.m3u8",
        "/tmp/out_%03d.mp4",
        duration=600,
        video_map=0,
        audio_map=2,
    )
    assert cmd[0] == "ffmpeg"
    assert "-map" in cmd
    assert "0:0" in cmd
    assert "0:2" in cmd
    assert "-t" in cmd
    idx = cmd.index("-t")
    assert cmd[idx + 1] == "600"


# ── Format progress ──────────────────────────────────────────────────────


def test_format_progress():
    rec = RecordingProcess()
    rec.elapsed_us = 150_000_000  # 150 seconds
    rec.speed = "1.5x"
    rec.total_size = 50 * 1024 * 1024  # 50 MB
    text = format_progress(rec, 300)
    assert "50.0%" in text or "50.0" in text
    assert "50.0 MB" in text
    assert "1.5x" in text


def test_format_progress_zero_duration():
    rec = RecordingProcess()
    rec.elapsed_us = 0
    rec.speed = "0x"
    rec.total_size = 0
    text = format_progress(rec, 0)
    assert "0.0%" in text


# ── Channel search helpers ────────────────────────────────────────────────


def test_search_channels(tmp_path, monkeypatch):
    # Create a temp channel list
    channels = [
        {"name": "BBC News", "url": "http://bbc.m3u8"},
        {"name": "CNN Live", "url": "http://cnn.m3u8"},
        {"name": "Fox News", "url": "http://fox.m3u8"},
    ]
    ch_file = tmp_path / "test_channels.json"
    ch_file.write_text(json.dumps(channels))
    monkeypatch.setattr("bot.utils.channels.CHANNEL_LIST_DIR", str(tmp_path))

    results = search_channels("news")
    assert len(results) == 2  # BBC News, Fox News
    names = [r["name"] for r in results]
    assert "BBC News" in names
    assert "Fox News" in names


def test_get_channel_by_name(tmp_path, monkeypatch):
    channels = [{"name": "TestChannel", "url": "http://test.m3u8"}]
    ch_file = tmp_path / "ch.json"
    ch_file.write_text(json.dumps(channels))
    monkeypatch.setattr("bot.utils.channels.CHANNEL_LIST_DIR", str(tmp_path))

    ch = get_channel_by_name("TestChannel")
    assert ch is not None
    assert ch["url"] == "http://test.m3u8"

    assert get_channel_by_name("Missing") is None


def test_list_json_files(tmp_path, monkeypatch):
    (tmp_path / "a.json").write_text("[]")
    (tmp_path / "b.json").write_text("[]")
    (tmp_path / "c.txt").write_text("nope")
    monkeypatch.setattr("bot.utils.channels.CHANNEL_LIST_DIR", str(tmp_path))

    files = list_json_files()
    assert sorted(files) == ["a.json", "b.json"]


def test_remove_json_file(tmp_path, monkeypatch):
    fpath = tmp_path / "removeme.json"
    fpath.write_text("[]")
    monkeypatch.setattr("bot.utils.channels.CHANNEL_LIST_DIR", str(tmp_path))

    assert remove_json_file("removeme.json") is True
    assert not fpath.exists()
    assert remove_json_file("nope.json") is False


# ── Tier config ───────────────────────────────────────────────────────────


def test_tier_config():
    assert "default" in TIER_CONFIG
    assert "verified" in TIER_CONFIG
    assert "premium" in TIER_CONFIG
    for tier in TIER_CONFIG.values():
        assert "max_duration" in tier
        assert "max_tasks" in tier
    assert TIER_CONFIG["default"]["max_tasks"] < TIER_CONFIG["premium"]["max_tasks"]
    assert TIER_CONFIG["default"]["max_tasks"] == 2


# ── M3U converter ─────────────────────────────────────────────────────────

SAMPLE_M3U = """\
#EXTM3U
#EXTINF:-1 group-title="News",IN: BBC News
http://bbc.example.com/live.m3u8
#EXTINF:-1 group-title="News",IN: CNN International
http://cnn.example.com/live.m3u8
#EXTINF:-1 group-title="Sports",ESPN HD
http://espn.example.com/live.m3u8
#EXTINF:-1,No Group Channel
http://nogroup.example.com/live.m3u8
"""


def test_slugify():
    assert slugify("IN: BBC News") == "bbc_news"
    assert slugify("ESPN HD") == "espn_hd"
    assert slugify("Hello World!") == "hello_world"
    assert slugify("  leading-trailing  ") == "leading_trailing"
    assert slugify("US: Fox News") == "fox_news"


def test_convert_m3u_to_json(tmp_path):
    m3u_file = tmp_path / "test.m3u"
    m3u_file.write_text(SAMPLE_M3U)
    json_file = tmp_path / "test.json"

    result = convert_m3u_to_json(str(m3u_file), str(json_file))
    assert result == str(json_file)
    assert json_file.exists()

    data = json.loads(json_file.read_text())
    assert isinstance(data, list)
    assert len(data) == 4

    # Check first channel
    assert data[0]["name"] == "IN: BBC News"
    assert data[0]["url"] == "http://bbc.example.com/live.m3u8"
    assert data[0]["group"] == "News"

    # Check channel without group
    assert data[3]["name"] == "No Group Channel"
    assert data[3]["group"] == ""


def test_convert_m3u_to_json_empty_file(tmp_path):
    m3u_file = tmp_path / "empty.m3u"
    m3u_file.write_text("")
    json_file = tmp_path / "empty.json"

    result = convert_m3u_to_json(str(m3u_file), str(json_file))
    assert result is None


def test_convert_m3u_to_json_missing_file(tmp_path):
    json_file = tmp_path / "out.json"
    result = convert_m3u_to_json("/nonexistent/file.m3u", str(json_file))
    assert result is None


def test_convert_m3u_to_json_no_channels(tmp_path):
    m3u_file = tmp_path / "header_only.m3u"
    m3u_file.write_text("#EXTM3U\n# Just comments\n")
    json_file = tmp_path / "header_only.json"

    result = convert_m3u_to_json(str(m3u_file), str(json_file))
    assert result is None


def test_convert_m3u_compatible_with_channel_search(tmp_path, monkeypatch):
    """Verify converted JSON works with the existing channel search."""
    m3u_file = tmp_path / "channels.m3u"
    m3u_file.write_text(SAMPLE_M3U)
    json_file = tmp_path / "channels.json"
    convert_m3u_to_json(str(m3u_file), str(json_file))

    monkeypatch.setattr("bot.utils.channels.CHANNEL_LIST_DIR", str(tmp_path))
    results = search_channels("BBC")
    assert len(results) == 1
    assert results[0]["name"] == "IN: BBC News"
    assert results[0]["url"] == "http://bbc.example.com/live.m3u8"


# ── parse_rec_args ────────────────────────────────────────────────────────


def test_parse_rec_args_url_only():
    result = parse_rec_args("/rec https://example.com/stream.m3u8")
    assert result is not None
    assert result["source"] == "https://example.com/stream.m3u8"
    assert result["duration_sec"] is None
    assert result["filename"] is None
    assert result["lang_index"] is None


def test_parse_rec_args_full():
    result = parse_rec_args('/rec "Disney Channel (4K)" 00:00:10 "My Cartoon" .L1')
    assert result is not None
    assert result["source"] == "Disney Channel (4K)"
    assert result["duration_sec"] == 10
    assert result["filename"] == "My Cartoon"
    assert result["lang_index"] == 1


def test_parse_rec_args_url_with_duration_and_filename():
    result = parse_rec_args('/rec "https://example.com/stream.m3u8" 00:05:00 "My Stream"')
    assert result is not None
    assert result["source"] == "https://example.com/stream.m3u8"
    assert result["duration_sec"] == 300
    assert result["filename"] == "My Stream"
    assert result["lang_index"] is None


def test_parse_rec_args_duration_only():
    result = parse_rec_args("/rec https://example.com/live.m3u8 01:30:00")
    assert result is not None
    assert result["source"] == "https://example.com/live.m3u8"
    assert result["duration_sec"] == 5400
    assert result["filename"] is None
    assert result["lang_index"] is None


def test_parse_rec_args_lang_only():
    result = parse_rec_args("/rec https://example.com/live.m3u8 .L2")
    assert result is not None
    assert result["source"] == "https://example.com/live.m3u8"
    assert result["lang_index"] == 2
    assert result["duration_sec"] is None


def test_parse_rec_args_no_args():
    assert parse_rec_args("/rec") is None


def test_parse_rec_args_empty_string():
    assert parse_rec_args("/rec   ") is None


def test_parse_rec_args_channel_name_unquoted():
    result = parse_rec_args("/rec BBC")
    assert result is not None
    assert result["source"] == "BBC"


def test_parse_rec_args_lang_case_insensitive():
    result = parse_rec_args("/rec https://example.com/live.m3u8 .l3")
    assert result is not None
    assert result["lang_index"] == 3


# ── Multi‑audio _build_record_cmd ─────────────────────────────────────────


def test_build_record_cmd_multi_audio():
    cmd = _build_record_cmd(
        "http://example.com/stream.m3u8",
        "/tmp/out_%03d.mp4",
        duration=600,
        video_map=0,
        audio_map=[2, 3],
    )
    assert cmd[0] == "ffmpeg"
    # Should have three -map entries: one video + two audio
    map_indices = [i for i, x in enumerate(cmd) if x == "-map"]
    assert len(map_indices) == 3
    assert cmd[map_indices[0] + 1] == "0:0"
    assert cmd[map_indices[1] + 1] == "0:2"
    assert cmd[map_indices[2] + 1] == "0:3"


def test_build_record_cmd_single_audio_int():
    """Backward-compatible: single int audio_map still works."""
    cmd = _build_record_cmd(
        "http://example.com/stream.m3u8",
        "/tmp/out_%03d.mp4",
        duration=60,
        video_map=0,
        audio_map=1,
    )
    map_indices = [i for i, x in enumerate(cmd) if x == "-map"]
    assert len(map_indices) == 2
    assert cmd[map_indices[1] + 1] == "0:1"


# ── Admin task limit config ──────────────────────────────────────────────


def test_admin_max_tasks_config():
    from bot.config import ADMIN_MAX_TASKS
    assert ADMIN_MAX_TASKS >= 10
