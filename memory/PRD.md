# M3U8 / M3U Recorder Telegram Bot — PRD

## Original Problem Statement
Build a production-ready Telegram bot (@MIANZUKIBOT) that records live M3U8/HLS streams,
auto-splits files to stay under Telegram's 2 GB upload limit, and provides a polished
interactive UI for track selection and real-time progress monitoring.

## Architecture

### Tech Stack
- **Language**: Python 3.11
- **Telegram**: Pyrogram 2.0.106 (async MTProto) + TgCrypto
- **Database**: Motor 3.3.1 (async MongoDB driver)
- **Processing**: asyncio, FFmpeg 5.1, FFprobe
- **Metadata**: Hachoir, Pillow
- **Server**: FastAPI + Uvicorn (supervisor-managed)

### File Structure
```
/app/backend/
├── server.py          FastAPI + bot lifespan entry point
├── config.py          Environment variable loader
├── database.py        MongoDB motor CRUD (users + tasks)
├── ffmpeg_utils.py    FFprobe analysis, track keyboard, FFmpeg recorder, thumbnail
├── worker.py          Asyncio task queue + worker pool + file upload
├── bot.py             Pyrogram client + all command/callback handlers
├── sessions/          Pyrogram session files
└── .env               Secrets (API_ID, API_HASH, BOT_TOKEN, OWNER_ID, MONGO_URL…)

/app/channels/
└── sample.json        Pre-loaded channel list (BBC News, NASA TV, DW, Al Jazeera, France24)
```

### Database Collections
- **users**: user_id, tier (default/verified/premium), created_at, verified_until, total_recordings
- **tasks**: task_id, user_id, chat_id, url, video/audio stream indices, duration, status, progress, ffmpeg_log, segments, etc.

## User Tiers
| Tier     | Max Duration | Max Parallel Tasks |
|----------|--------------|--------------------|
| Default  | 30 min       | 1                  |
| Verified | 2 hours      | 2                  |
| Premium  | 12 hours     | 3                  |

## Commands Implemented

### User Commands
- `/start` — Welcome message with instructions
- `/rec <url>` — FFprobe analysis + interactive track selection UI → queues recording
- `/cancel <task_id>` — Cancel running or queued task (SIGTERM to FFmpeg)
- `/mytasks` — Active/queued tasks list with inline cancel buttons
- `/status` — Bot uptime, active tasks, worker stats
- `/verify <token>` — Placeholder (shortlink skipped per user request)
- `/search <query>` — Search JSON channel lists with paginated inline buttons
- `/channel <name>` — Direct record from channel list

### Admin Commands (OWNER_ID: 943270135)
- `/auth <uid>` — Grant Premium tier
- `/deauth <uid>` — Remove Premium tier
- `/tasks` — Paginated all-active-tasks view with force-cancel buttons
- `/add_m3u8` — (Reply to .json) Save channel list
- `/remove_m3u8 <file>` — Delete channel list
- `/pull <m3u8|log|premium|admin>` — Export data as file
- `/flog <file|msg> <task_id>` — FFmpeg log for a task
- `/admin_panel` — Inline control panel with stats

## Key Design Decisions
1. **Worker Queue**: `asyncio.Queue` with NUM_WORKERS=3 concurrent FFmpeg tasks
2. **Progress**: FFmpeg `-progress pipe:1` → structured key=value output → edit Telegram message every 10s
3. **Auto-splitting**: FFmpeg segment muxer with 45-min segments (safe for most bitrates)
4. **Cancellation**: `asyncio.Event` + `SIGTERM` to FFmpeg process group
5. **State**: In-memory dicts for pending selections + active processes; MongoDB for persistence
6. **Upload**: Sequential per-segment with thumbnail extraction, FloodWait retry

## Environment Variables
```
MONGO_URL=mongodb://localhost:27017
DB_NAME=m3u8bot
API_ID=28983177
API_HASH=<hash>
BOT_TOKEN=<token>
OWNER_ID=943270135
NUM_WORKERS=3
OUTPUT_DIR=/tmp/recordings
CHANNELS_DIR=/app/channels
PROGRESS_UPDATE_INTERVAL=10
MAX_SEGMENT_SECS=2700
```

## What's Been Implemented (2026-03-09)
- Complete 6-file bot codebase per specification
- All user and admin commands
- FFprobe stream analysis with interactive track selection keyboard
- Live progress bar with ETA, speed, size, bitrate, cancel button
- Graceful cancellation (SIGTERM + partial upload)
- MongoDB user tiers with auto-expiry for verified tier
- Channel list management (JSON files in /app/channels/)
- Admin panel with stats, user management, data export
- 44/44 backend tests passing

## Prioritized Backlog

### P0 — Must Have (Done)
- [x] Core recording flow (rec → probe → select → record → upload)
- [x] All user and admin commands
- [x] Worker queue with concurrent processing

### P1 — Should Have (Next)
- [ ] Shortlink token verification service integration (mdisk/droplink)
- [ ] Rate limiting per user (prevent spam)
- [ ] Notification when bot is restarted (lost sessions)
- [ ] Persistent retry for failed uploads

### P2 — Nice to Have (Backlog)
- [ ] Web dashboard for admin monitoring
- [ ] M3U playlist import (auto-load channels from .m3u file)
- [ ] Scheduled recording (record at specific time)
- [ ] Quality presets (re-encode to specific bitrate)
- [ ] Multi-language support

## Next Tasks List
1. Test end-to-end Telegram flow by messaging @MIANZUKIBOT with /start
2. Integrate shortlink verification if needed
3. Add rate limiting for /rec command
4. Add more channel lists to /app/channels/
