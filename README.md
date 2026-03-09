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
- **M3U to JSON Converter** – Convert `.m3u`/`.m3u8` playlists to JSON channel lists directly via bot command.

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
| `/convert_m3u` | Reply to an `.m3u`/`.m3u8` file to convert & save as JSON channel list |
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
│       ├── channels.py      # Channel list search
│       └── m3u_converter.py # M3U/M3U8 → JSON converter
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

## Example Usage

### Recording a stream

```
/rec https://example.com/live/stream.m3u8
```

The bot will analyse the stream and show an interactive track selection keyboard.
Pick your preferred video resolution and audio language, then tap **✅ Start Recording**.

### Searching & recording from channel lists

```
/search BBC
```

Returns matching channels with inline buttons to start recording directly.

```
/channel BBC News
```

Looks up the exact channel name and begins stream analysis.

### Converting an M3U playlist to a channel list (Admin)

1. Upload an `.m3u` or `.m3u8` file to the bot chat.
2. Reply to the uploaded file with:

```
/convert_m3u
```

The bot converts the playlist to JSON and saves it under `channel_lists/`.
Channels are then searchable via `/search`.

### Adding a pre-built JSON channel list (Admin)

1. Upload a `.json` channel list file.
2. Reply with:

```
/add_m3u8
```

### Managing channel lists (Admin)

```
/remove_m3u8
```

Lists all loaded channel list files.

```
/remove_m3u8 india_channels.json
```

Deletes the specified file.

### Managing users (Admin)

```
/auth 123456789
```

Grants Premium tier to the user.

```
/deauth 123456789
```

Revokes Premium tier (downgrade to Default).

### Checking bot status

```
/status
```

Shows uptime, active recordings, queue size, and your tier.

```
/mytasks
```

Lists your active recordings with task IDs for cancellation.

```
/cancel abc1234def
```

Cancels the recording with the given task ID.
