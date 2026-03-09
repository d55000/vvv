# M3U8 / M3U Recorder Telegram Bot

An asynchronous Telegram bot that records M3U8/M3U live streams, auto-splits them to bypass Telegram's 2 GB upload limit, and features an interactive track selection UI.

## Features

- **Async Worker Queue** – Concurrent FFmpeg recordings via `asyncio.Queue`, limited by `NUM_WORKERS`.
- **Auto-Splitting** – FFmpeg segmenting keeps each `.mp4` file under 1.95 GB.
- **Interactive Track Selection** – Inline keyboard to pick video resolution & audio language before recording.
- **Live Progress Bar** – Real-time status with progress bar, ETA, size, speed, and inline cancel button.
- **Graceful Cancellation** – `SIGTERM` to FFmpeg, partial upload, queue cleanup.
- **User Tiers (MongoDB)** – Default / Verified / Premium with configurable limits.
- **Channel Lists** – Load JSON files, search & record channels by name.

## Tech Stack

| Component | Library |
|-----------|---------|
| Telegram  | `pyrogram` + `tgcrypto` |
| Database  | `motor` (async MongoDB) |
| Processing| `ffmpeg` / `ffprobe` (system) |
| Metadata  | `hachoir`, `Pillow` |
| Config    | `python-dotenv` |

## Setup

### 1. Prerequisites

- Python 3.9+
- FFmpeg & FFprobe installed and on `PATH`
- MongoDB instance

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure environment

```bash
cp .env.example .env
# Edit .env with your API_ID, API_HASH, BOT_TOKEN, MONGO_URI, OWNER_ID
```

### 4. Run

```bash
python main.py
```

## Commands

### User Commands

| Command | Description |
|---------|-------------|
| `/start` | Welcome message |
| `/rec <url>` | Analyse stream & select tracks |
| `/cancel <task_id>` | Cancel a recording |
| `/mytasks` | List your active recordings |
| `/status` | Bot uptime & stats |
| `/verify <token>` | Upgrade to Verified tier |
| `/search <query>` | Search channel lists |
| `/channel <name>` | Record a channel by name |

### Admin Commands

| Command | Description |
|---------|-------------|
| `/auth <user_id>` | Grant Premium tier |
| `/deauth <user_id>` | Revoke Premium tier |
| `/tasks` | Paginated list of all active tasks |
| `/add_m3u8` | Reply to a `.json` file to add a channel list |
| `/remove_m3u8 <file>` | Delete a channel list |
| `/pull` | Git pull (owner only) |

## User Tiers

| Tier | Max Duration | Parallel Tasks |
|------|-------------|----------------|
| Default | 30 min | 1 |
| Verified | 2 hours | 2 |
| Premium | 12 hours | 3 |

## Project Structure

```
├── main.py                  # Entry point
├── bot/
│   ├── config.py            # Environment & constants
│   ├── db/
│   │   └── database.py      # MongoDB helpers
│   ├── handlers/
│   │   ├── user.py          # User command handlers
│   │   └── admin.py         # Admin command handlers
│   └── utils/
│       ├── ffmpeg.py        # FFmpeg/FFprobe wrappers
│       ├── worker.py        # Async worker queue
│       └── channels.py      # Channel list search
├── channel_lists/           # JSON channel list files
├── tests/
│   └── test_utils.py        # Unit tests
├── requirements.txt
├── .env.example
└── README.md
```

## Testing

```bash
pip install pytest
pytest tests/ -v
```
