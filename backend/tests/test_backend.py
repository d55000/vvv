"""
Backend tests for M3U8 Recorder Bot.
Tests: API endpoints, database functions, ffmpeg_utils helpers, config, worker queue.
"""

import asyncio
import os
import sys
import pytest
import requests

# Add backend to path
sys.path.insert(0, "/app/backend")

BASE_URL = "http://localhost:8001"


# ── API Endpoint Tests ────────────────────────────────────────────────────────

class TestAPIEndpoints:
    """Health and stats API endpoint tests"""

    def test_health_returns_200(self):
        r = requests.get(f"{BASE_URL}/api/health")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        print("PASS: /api/health returns 200")

    def test_health_response_structure(self):
        r = requests.get(f"{BASE_URL}/api/health")
        data = r.json()
        assert "status" in data, "Missing 'status' field"
        assert data["status"] == "ok", f"Expected 'ok', got {data['status']}"
        assert "active_tasks" in data, "Missing 'active_tasks' field"
        assert isinstance(data["active_tasks"], int), "active_tasks must be int"
        print(f"PASS: /api/health structure correct: {data}")

    def test_health_active_tasks_is_zero(self):
        r = requests.get(f"{BASE_URL}/api/health")
        data = r.json()
        assert data["active_tasks"] == 0, f"Expected 0, got {data['active_tasks']}"
        print("PASS: /api/health active_tasks == 0")

    def test_stats_returns_200(self):
        r = requests.get(f"{BASE_URL}/api/stats")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        print("PASS: /api/stats returns 200")

    def test_stats_response_structure(self):
        r = requests.get(f"{BASE_URL}/api/stats")
        data = r.json()
        assert "total_users" in data, "Missing 'total_users'"
        assert "active_tasks" in data, "Missing 'active_tasks'"
        assert "queue_depth" in data, "Missing 'queue_depth'"
        assert isinstance(data["total_users"], int)
        assert isinstance(data["active_tasks"], int)
        assert isinstance(data["queue_depth"], int)
        print(f"PASS: /api/stats structure correct: {data}")


# ── Config Tests ──────────────────────────────────────────────────────────────

class TestConfig:
    """Config env var loading tests"""

    def test_api_id_loaded(self):
        from dotenv import load_dotenv
        load_dotenv("/app/backend/.env")
        import config
        assert config.API_ID == 28983177, f"Expected 28983177, got {config.API_ID}"
        print(f"PASS: API_ID = {config.API_ID}")

    def test_owner_id_loaded(self):
        import config
        assert config.OWNER_ID == 943270135, f"Expected 943270135, got {config.OWNER_ID}"
        print(f"PASS: OWNER_ID = {config.OWNER_ID}")

    def test_bot_token_loaded(self):
        import config
        assert config.BOT_TOKEN, "BOT_TOKEN is empty"
        assert config.BOT_TOKEN.startswith("7032738206"), f"Unexpected BOT_TOKEN: {config.BOT_TOKEN}"
        print("PASS: BOT_TOKEN loaded correctly")

    def test_mongo_url_loaded(self):
        import config
        assert config.MONGO_URL == "mongodb://localhost:27017"
        print(f"PASS: MONGO_URL = {config.MONGO_URL}")

    def test_db_name_loaded(self):
        import config
        assert config.DB_NAME == "m3u8bot"
        print(f"PASS: DB_NAME = {config.DB_NAME}")

    def test_tier_limits_structure(self):
        import config
        tiers = config.TIER_LIMITS
        for tier_name in ["default", "verified", "premium"]:
            assert tier_name in tiers, f"Missing tier: {tier_name}"
            assert "max_duration" in tiers[tier_name]
            assert "max_tasks" in tiers[tier_name]
        print("PASS: TIER_LIMITS has all required tiers")

    def test_admin_ids_includes_owner(self):
        import config
        assert config.OWNER_ID in config.ADMIN_IDS
        print(f"PASS: OWNER_ID in ADMIN_IDS: {config.ADMIN_IDS}")


# ── ffmpeg_utils helper tests ─────────────────────────────────────────────────

class TestFFmpegHelpers:
    """Unit tests for pure functions in ffmpeg_utils.py"""

    def test_make_progress_bar_0_percent(self):
        from ffmpeg_utils import make_progress_bar
        bar = make_progress_bar(0)
        assert "0.0%" in bar
        assert "░" in bar
        print(f"PASS: make_progress_bar(0) = {bar}")

    def test_make_progress_bar_100_percent(self):
        from ffmpeg_utils import make_progress_bar
        bar = make_progress_bar(100)
        assert "100.0%" in bar
        assert "█" in bar
        print(f"PASS: make_progress_bar(100) = {bar}")

    def test_make_progress_bar_50_percent(self):
        from ffmpeg_utils import make_progress_bar
        bar = make_progress_bar(50, width=20)
        assert "50.0%" in bar
        # Should have 10 filled and 10 empty
        assert bar.count("█") == 10
        assert bar.count("░") == 10
        print(f"PASS: make_progress_bar(50) balanced")

    def test_format_duration_zero(self):
        from ffmpeg_utils import format_duration
        assert format_duration(0) == "00:00:00"
        print("PASS: format_duration(0) = 00:00:00")

    def test_format_duration_one_hour(self):
        from ffmpeg_utils import format_duration
        assert format_duration(3600) == "01:00:00"
        print("PASS: format_duration(3600) = 01:00:00")

    def test_format_duration_90_minutes(self):
        from ffmpeg_utils import format_duration
        assert format_duration(5400) == "01:30:00"
        print("PASS: format_duration(5400) = 01:30:00")

    def test_format_duration_mixed(self):
        from ffmpeg_utils import format_duration
        assert format_duration(3661) == "01:01:01"
        print("PASS: format_duration(3661) = 01:01:01")

    def test_parse_time_to_secs_basic(self):
        from ffmpeg_utils import parse_time_to_secs
        assert parse_time_to_secs("00:01:30") == 90.0
        print("PASS: parse_time_to_secs('00:01:30') == 90")

    def test_parse_time_to_secs_with_millis(self):
        from ffmpeg_utils import parse_time_to_secs
        result = parse_time_to_secs("01:00:00.500")
        assert result == 3600.0
        print(f"PASS: parse_time_to_secs with millis = {result}")

    def test_parse_time_to_secs_invalid(self):
        from ffmpeg_utils import parse_time_to_secs
        result = parse_time_to_secs("invalid")
        assert result == 0.0
        print("PASS: parse_time_to_secs invalid returns 0.0")

    def test_parse_time_to_secs_one_hour(self):
        from ffmpeg_utils import parse_time_to_secs
        assert parse_time_to_secs("01:00:00") == 3600.0
        print("PASS: parse_time_to_secs('01:00:00') == 3600")


# ── build_track_keyboard tests ────────────────────────────────────────────────

class TestBuildTrackKeyboard:
    """Tests for build_track_keyboard InlineKeyboardMarkup builder"""

    def test_returns_inline_keyboard_markup(self):
        from ffmpeg_utils import build_track_keyboard
        from pyrogram.types import InlineKeyboardMarkup
        tracks = {
            "video_tracks": [{"index": 0, "resolution": "720p", "label": "720p H264 30fps"}],
            "audio_tracks": [{"index": 1, "lang": "ENG", "label": "ENG AAC Stereo"}],
        }
        kb = build_track_keyboard("test-task-id-1234", tracks)
        assert isinstance(kb, InlineKeyboardMarkup)
        print("PASS: build_track_keyboard returns InlineKeyboardMarkup")

    def test_keyboard_has_action_buttons(self):
        from ffmpeg_utils import build_track_keyboard
        tracks = {
            "video_tracks": [{"index": 0, "resolution": "1080p", "label": "1080p H264 30fps"}],
            "audio_tracks": [],
        }
        kb = build_track_keyboard("abc123xyz", tracks)
        # Flatten all buttons
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        callback_data_list = [b.callback_data for b in all_buttons]
        # Should have start recording and cancel buttons
        assert any("sr_" in cd for cd in callback_data_list), "Missing start recording button"
        assert any("xp_" in cd for cd in callback_data_list), "Missing cancel button"
        print(f"PASS: Action buttons present: {callback_data_list}")

    def test_keyboard_video_callback_data_format(self):
        from ffmpeg_utils import build_track_keyboard
        tracks = {
            "video_tracks": [
                {"index": 0, "resolution": "720p", "label": "720p"},
                {"index": 1, "resolution": "480p", "label": "480p"},
            ],
            "audio_tracks": [],
        }
        kb = build_track_keyboard("taskid12345678", tracks)
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        sv_buttons = [b for b in all_buttons if b.callback_data and b.callback_data.startswith("sv_")]
        assert len(sv_buttons) == 2, f"Expected 2 sv_ buttons, got {len(sv_buttons)}"
        print(f"PASS: Video select buttons: {[b.callback_data for b in sv_buttons]}")

    def test_keyboard_selected_video_has_checkmark(self):
        from ffmpeg_utils import build_track_keyboard
        tracks = {
            "video_tracks": [{"index": 0, "resolution": "1080p", "label": "1080p"}],
            "audio_tracks": [],
        }
        kb = build_track_keyboard("taskid", tracks, sel_vid=0)
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        sv_buttons = [b for b in all_buttons if b.callback_data and b.callback_data.startswith("sv_")]
        assert sv_buttons[0].text.startswith("✅"), f"Selected button should have checkmark: {sv_buttons[0].text}"
        print("PASS: Selected video track has checkmark")

    def test_keyboard_empty_tracks(self):
        from ffmpeg_utils import build_track_keyboard
        from pyrogram.types import InlineKeyboardMarkup
        kb = build_track_keyboard("tid123", {"video_tracks": [], "audio_tracks": []})
        assert isinstance(kb, InlineKeyboardMarkup)
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        texts = [b.text for b in all_buttons]
        assert any("No video" in t for t in texts)
        assert any("No audio" in t for t in texts)
        print("PASS: Empty tracks shows 'No video/audio' messages")


# ── Database tests ────────────────────────────────────────────────────────────

class TestDatabase:
    """Tests for database.py async functions"""

    @pytest.fixture(autouse=True)
    def event_loop_setup(self):
        """Run each test in an async event loop."""
        pass

    def test_connect_and_get_or_create_user(self):
        async def run():
            import database
            await database.connect_db()
            user = await database.get_or_create_user(
                user_id=999999999,
                username="test_user",
                full_name="Test User"
            )
            assert user["user_id"] == 999999999
            assert user["tier"] == "default"
            assert "created_at" in user
            print(f"PASS: get_or_create_user: {user}")
            # Cleanup
            await database.db.users.delete_one({"user_id": 999999999})
            await database.close_db()
        asyncio.run(run())

    def test_get_or_create_user_idempotent(self):
        async def run():
            import database
            await database.connect_db()
            uid = 888888888
            user1 = await database.get_or_create_user(uid, "u1", "User One")
            user2 = await database.get_or_create_user(uid, "u1", "User One")
            assert user1["user_id"] == user2["user_id"]
            assert user1["tier"] == user2["tier"]
            print("PASS: get_or_create_user is idempotent")
            # Cleanup
            await database.db.users.delete_one({"user_id": uid})
            await database.close_db()
        asyncio.run(run())

    def test_set_and_get_user_tier(self):
        async def run():
            import database
            await database.connect_db()
            uid = 777777777
            await database.get_or_create_user(uid)
            await database.set_user_tier(uid, "premium")
            tier = await database.get_user_tier(uid)
            assert tier == "premium", f"Expected 'premium', got '{tier}'"
            print(f"PASS: set_user_tier → get_user_tier = {tier}")
            # Cleanup
            await database.db.users.delete_one({"user_id": uid})
            await database.close_db()
        asyncio.run(run())

    def test_get_user_tier_default_for_nonexistent(self):
        async def run():
            import database
            await database.connect_db()
            tier = await database.get_user_tier(111111111111)
            assert tier == "default", f"Expected 'default', got '{tier}'"
            print("PASS: get_user_tier returns 'default' for unknown user")
            await database.close_db()
        asyncio.run(run())

    def test_count_all_users(self):
        async def run():
            import database
            await database.connect_db()
            count = await database.count_all_users()
            assert isinstance(count, int)
            assert count >= 0
            print(f"PASS: count_all_users = {count}")
            await database.close_db()
        asyncio.run(run())

    def test_count_all_active_tasks(self):
        async def run():
            import database
            await database.connect_db()
            count = await database.count_all_active_tasks()
            assert isinstance(count, int)
            assert count >= 0
            print(f"PASS: count_all_active_tasks = {count}")
            await database.close_db()
        asyncio.run(run())


# ── Worker / Queue tests ──────────────────────────────────────────────────────

class TestWorker:
    """Tests for worker.py task queue"""

    def test_task_queue_is_asyncio_queue(self):
        import worker
        assert isinstance(worker.task_queue, asyncio.Queue)
        print("PASS: task_queue is asyncio.Queue")

    def test_enqueue_task_adds_to_queue(self):
        async def run():
            import worker
            initial_size = worker.task_queue.qsize()
            task_data = {
                "task_id": "test-task-001",
                "user_id": 12345,
                "chat_id": 12345,
                "url": "https://example.com/test.m3u8",
                "duration": 60,
            }
            await worker.enqueue_task(task_data)
            new_size = worker.task_queue.qsize()
            assert new_size == initial_size + 1, f"Queue size should increase by 1"
            # Drain the test item
            worker.task_queue.get_nowait()
            worker.task_queue.task_done()
            print(f"PASS: enqueue_task added item to queue (size {initial_size} → {new_size})")
        asyncio.run(run())

    def test_active_cancel_events_is_dict(self):
        import worker
        assert isinstance(worker.active_cancel_events, dict)
        print("PASS: active_cancel_events is a dict")

    def test_cancel_task_returns_false_for_unknown(self):
        async def run():
            import worker
            result = await worker.cancel_task("nonexistent-task-id")
            assert result is False
            print("PASS: cancel_task returns False for unknown task_id")
        asyncio.run(run())


# ── Syntax / Import Tests ─────────────────────────────────────────────────────

class TestSyntaxAndImports:
    """Verify all Python files compile without errors"""

    def test_server_py_imports(self):
        # server.py imports bot which starts pyrogram; just check syntax via compile
        import py_compile
        py_compile.compile("/app/backend/server.py", doraise=True)
        print("PASS: server.py compiles without syntax errors")

    def test_config_py_imports(self):
        import config
        print("PASS: config.py imports successfully")

    def test_database_py_imports(self):
        import database
        print("PASS: database.py imports successfully")

    def test_ffmpeg_utils_py_imports(self):
        import ffmpeg_utils
        print("PASS: ffmpeg_utils.py imports successfully")

    def test_worker_py_imports(self):
        import worker
        print("PASS: worker.py imports successfully")


# ── FFprobe integration test ──────────────────────────────────────────────────

class TestFFprobe:
    """Test run_ffprobe with a real public HLS stream"""

    def test_run_ffprobe_public_hls(self):
        async def run():
            from ffmpeg_utils import run_ffprobe
            url = "https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8"
            result = await run_ffprobe(url)
            assert "video_tracks" in result
            assert "audio_tracks" in result
            assert "raw" in result
            assert isinstance(result["video_tracks"], list)
            assert isinstance(result["audio_tracks"], list)
            print(f"PASS: run_ffprobe returned {len(result['video_tracks'])} video, {len(result['audio_tracks'])} audio tracks")
            if result["video_tracks"]:
                vt = result["video_tracks"][0]
                assert "index" in vt
                assert "resolution" in vt
                assert "codec" in vt
                print(f"  First video track: {vt}")
        asyncio.run(run())
