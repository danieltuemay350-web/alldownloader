# Telegram Media Downloader Bot

Production-oriented Telegram bot built with Python, `aiogram`, `yt-dlp`, `FFmpeg`, and optional MTProto uploads through `Telethon`.

## Features

- Accepts direct URLs or `/video <url>` and `/audio <url>`
- Supports YouTube, TikTok, Instagram, and Facebook
- Downloads best available video with `yt-dlp`
- Extracts MP3 audio at 320 kbps with FFmpeg
- Async worker queue with configurable concurrency
- Per-user rate limiting
- Rejects unsupported URLs and media that exceeds Telegram delivery or processing limits
- Delivery fallback chain:
  - `<= 50 MB`: Bot API
  - `> 50 MB and <= 2 GB`: MTProto via Telethon
  - `> 2 GB`: rejected as too large to send in Telegram
- Temporary file cleanup after delivery or cancellation

## Requirements

- Python 3.11+
- FFmpeg installed and available on `PATH`
- Telegram bot token from BotFather
- Optional for large uploads:
  - `TELEGRAM_API_ID`
  - `TELEGRAM_API_HASH`

## Setup

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Set your real values in `.env`, then run:

```powershell
python main.py
```

Or run it in Docker:

```powershell
docker build -t telegram-media-bot .
docker run --env-file .env telegram-media-bot
```

The bot also starts a lightweight HTTP health server on `SERVICE_HOST:SERVICE_PORT` so service platforms such as Choreo can keep the container healthy while the Telegram bot continues polling in the background.

Health endpoints:

- `/`
- `/healthz`
- `/readyz`

## Deploy on Render

This project includes `render.yaml` for a `Background Worker` deployment on Render.

Recommended setup:

- Use a `Background Worker`, not a web service
- Keep the instance count at `1`
- Fill in these secret environment variables in Render:
  - `BOT_TOKEN`
  - `TELEGRAM_API_ID`
  - `TELEGRAM_API_HASH`
  - `ADMIN_CHAT_ID` (optional)

The included Dockerfile installs FFmpeg inside the container, so no extra build steps are required.

To receive error alerts in Telegram, set:

```env
ADMIN_CHAT_ID=123456789
```

This should be the numeric chat ID where the bot is allowed to message you.

Speed tuning environment variables:

```env
YTDLP_HTTP_CHUNK_SIZE_BYTES=10485760
YTDLP_FRAGMENT_CONCURRENCY=6
YTDLP_RETRIES=5
YTDLP_EXTRACTOR_RETRIES=5
YTDLP_SLEEP_INTERVAL_REQUESTS=0.75
YTDLP_RETRY_SLEEP_SECONDS=2.0
MTPROTO_PART_SIZE_KB=512
```

For faster MTProto uploads, keep `cryptg` installed from `requirements.txt`. Telethon's official docs note that it can provide a considerable speed-up for heavy upload/download workloads.

For TikTok, the bot now retries temporary connection resets automatically. If TikTok continues refusing requests from your network, you can optionally provide `TIKTOK_API_HOSTNAME`, `TIKTOK_APP_INFO`, `TIKTOK_DEVICE_ID`, or a proxy in `.env`.

For YouTube, some videos may trigger a sign-in challenge from YouTube. In that case, configure `YTDLP_COOKIE_FILE` with an authorized cookies file in Netscape format. On Choreo, this is best provided through a file mount.

## Large File Delivery

- `<= 50 MB`: delivered via Bot API
- `> 50 MB and <= 2 GB`: delivered via MTProto when API credentials are configured
- `> 2 GB`: rejected before or after download verification as too large to send in Telegram

## Commands

- `/start`
- `/help`
- `/video <url>`
- `/audio <url>`

Plain URLs open the preview card so the user can choose the exact video or audio version.

The preview menu also includes faster options when available, such as original audio and smaller video versions.
