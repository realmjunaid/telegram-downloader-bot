# DBot — Telegram Download Bot

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](Dockerfile)
[![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](docker-compose.yml)

Send a link in Telegram → the bot downloads the **original files** and sends them
back in chat. Free and open source, self-hosted with Docker and a local Telegram
Bot API server (**2GB file support**).

## Features

| Source | What you get |
|---|---|
| Mega file links (`mega.nz/file/...`) | Sent directly as documents |
| Mega folder links (`mega.nz/folder/...`) | Downloaded (decrypted + verified), packed into zip(s) |
| Terabox share links | All files downloaded, packed into zip(s) |
| Facebook posts | Photos/videos as original documents (full albums supported) |
| Instagram posts, carousels & reels | Original-quality documents |
| YouTube videos | Quality picker buttons (up to 1080p) |
| YouTube Shorts, TikTok | Best quality automatically (up to 1080p) |
| Any direct file link | Downloaded and sent (up to 20 links per message) |

- **Original quality, always** — bytes are never recompressed or converted
  (zips use `ZIP_STORED`, Mega downloads are MAC-verified).
- **Live progress bars** for downloading, zipping and uploading.
- **Smart splitting** — sets over 2GB are split into `part1.zip`, `part2.zip`, …
- **Auto-cleanup** — temp files are deleted after every job; a `cleaner` service
  also purges the Bot API cache hourly. Your VPS disk stays clean.
- **Bot commands** — `/start`, `/status` (uptime, active jobs, free disk),
  `/ping`, `/help`.
- **Private mode** — optional `ALLOWED_IDS` restricts the bot to you.

> Why a local Bot API server? Official `api.telegram.org` caps bot files at
> **50MB**. This repo runs Telegram's open-source Bot API next to the bot, so
> files up to **2GB** work — same as human accounts.

## Requirements

| Thing | Where to get it | Used for |
|---|---|---|
| `BOT_TOKEN` | [@BotFather](https://t.me/BotFather) → `/newbot` | Bot login |
| `TELEGRAM_API_ID` | [my.telegram.org](https://my.telegram.org) → API Development Tools | Local API server |
| `TELEGRAM_API_HASH` | Same page as above | Local API server |
| VPS + Dokploy | Your server | 24/7 hosting |

No domain is needed — the bot uses polling (outgoing connections only).

## Setup

### 1. Get `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` (5 min, one time)

1. Log in at [my.telegram.org](https://my.telegram.org) with your phone number.
2. Open **API Development Tools** → **Create application**.
3. Fill in:
   - **App title:** anything (e.g. `DBot`)
   - **Short name:** 5–32 alphanumeric chars (e.g. `dbotapp`)
   - **URL:** leave empty
   - **Platform:** `Other (specify in description)`
   - **Description:** `Self-hosted bot api server`
4. Copy `api_id` and `api_hash` and keep them secret.

### 2. Get `BOT_TOKEN` (2 min)

1. Open [@BotFather](https://t.me/BotFather), send `/newbot` and follow the steps.
2. Copy the token (`123456:ABC-...`) and keep it secret.
3. Open a chat with your new bot once.

### 3. Deploy on Dokploy (recommended)

1. **Create Service → Application**, provider **GitHub**,
   repo `realmjunaid/telegram-downloader-bot`, branch `main`.
2. **Build Type:** `Docker Compose`, **Compose file:** `docker-compose.yml`.
3. Skip domain/port — a polling bot needs no inbound traffic.
4. **Environment** tab — add these 3 variables:

```env
BOT_TOKEN=123456:ABC-your-bot-token
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
```

Optional variables:

```env
ALLOWED_IDS=123456789        # only this Telegram user ID may use the bot (empty = everyone)
MEGA_EMAIL=you@example.com   # higher Mega quota (optional)
MEGA_PASSWORD=your-password  # higher Mega quota (optional)
```

> Get your numeric ID from [@userinfobot](https://t.me/userinfobot).

5. **Deploy.** First start takes 1–2 minutes while `botapi` logs in. Check **Logs** for:

```
botapi  | ... authorised ...
bot     | Bot running... (MAX_ZIP_MB=2000)
```

Then send your bot a link.

> After deploying, force a rebuild when updating (a 2-second build means Docker
> reused the old image — delete the `bot` container/image and redeploy).

## Usage

Just paste a link — no command needed.

- **Mega file** → sent directly. **Mega folder** → downloaded and zipped.
- **Terabox share** → all files downloaded and zipped.
- **Facebook / Instagram photos** → original documents (no zip).
- **YouTube video** → quality buttons (`1080p`, `720p`, …). Shorts, TikTok and
  reels download at best quality automatically.
- **Direct links** → one file arrives as a document; several links in one
  message arrive zipped (max 20 links).

Limits: 2GB max per file/zip, 60-minute max video length, no live streams,
no login-walled content unless you provide `cookies.txt` (export from your
browser in Netscape format, place next to `docker-compose.yml` — it is
git-ignored, never commit it).

## Run locally

```bash
git clone https://github.com/realmjunaid/telegram-downloader-bot.git
cd telegram-downloader-bot
cp .env.example .env   # then edit .env
pip install -r requirements.txt
python bot.py
```

For full 2GB support locally:

```bash
docker compose up --build
```

## Project structure

| File | Purpose |
|---|---|
| `bot.py` | Everything: scrapers, downloaders, Telegram handlers |
| `Dockerfile` | Bot container (`python:3.12-slim` + ffmpeg for video merging) |
| `docker-compose.yml` | `bot` + local Bot API (`botapi`) + cache `cleaner` |
| `requirements.txt` | `cloudscraper`, `python-telegram-bot`, `python-dotenv`, `mega.py`, `yt-dlp` |

The zip size cap lives in `bot.py` as `MAX_ZIP_MB = 2000` (Telegram's local-API
limit — do not raise it).

## Troubleshooting

| Problem | Fix |
|---|---|
| `BOT_TOKEN ...` on start | `BOT_TOKEN` env var is missing — add it and redeploy. |
| `botapi` keeps restarting | Wrong `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` — recopy from my.telegram.org. |
| `413 Request Entity Too Large` | Not using the local API server — check `botapi` is running. |
| Download fails on login-walled posts | Add `cookies.txt` (browser export) and redeploy. |
| Mega says quota exceeded | Free IP quota is spent — wait, retry later, or set `MEGA_EMAIL` / `MEGA_PASSWORD`. |
| Only you should use the bot | Set `ALLOWED_IDS` to your Telegram user ID. |

## Contributing

Issues and pull requests are welcome. Please keep downloads personal and legal —
only fetch content you own or are allowed to archive, and respect each site's
terms of service.

## License

[MIT](LICENSE) © 2026 realmjunaid
