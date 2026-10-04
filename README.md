# DBot — Gallery to Telegram Zip Bot

Send a gallery/post link in Telegram → bot downloads everything → sends back
as one 2GB zip in chat.

Supports:
- `e-hentai.org / exhentai.org` galleries (`/g/...`)
- `pawchive.pw` posts (`/.../user/.../post/...`)

No single huge zip. 1000+ images = `part1.zip, part2.zip, ...` automatically.

---

## 1. How it works

```
You send link
   → bot downloads to temp folder (eh_downloader.py)
   → packs everything into one zip (max 2GB)
   → sends part1.zip, part2.zip...
   → deletes temp files
```

- Files keep site order: `001 - name.webp`, `002 - name.jpg` ...
- Resume-safe filenames, retry on network hiccup.
- Fixed 2GB per zip. Gallery 2GB-er moddhe hole single zip, besi hole part1, part2...
- **Auto-delete:** send sesh hole bot `workdir` (images + zips) `shutil.rmtree` diye
  permanently delete kore. Alada delete kora lage na, VPS full hobe na.
  `botapi` container-er cache `cleaner` service hourly (6h+ purono file) clean kore.

> Normal Telegram bots are limited to **50MB per file** via `api.telegram.org`.
> This repo runs a **local Bot API server** next to the bot, so **2GB**
> files work. Human accounts have 2GB, bots need this local server for the same.

---

## 2. What you need

| Thing | Where to get | Used for |
|---|---|---|
| `BOT_TOKEN` | Telegram → [@BotFather](https://t.me/BotFather) → `/newbot` | Bot login |
| `TELEGRAM_API_ID` | https://my.telegram.org → API Development Tools | Local API server login |
| `TELEGRAM_API_HASH` | Same page as above | Local API server login |
| VPS + Dokploy | Your server | 24/7 hosting |

No domain needed. Bot uses polling (outgoing connection only).

---

## 3. Get API_ID / API_HASH (5 min, one time)

1. Go to https://my.telegram.org and log in with your phone number.
2. Click **API Development Tools**.
3. Fill the form:
   - **App title:** `DBot` (anything)
   - **Short name:** `dbotapp` (5–32 chars, letters+numbers only — `DBot` alone is too short)
   - **URL:** leave empty
   - **Platform:** `Other (specify in description)`
   - **Description:** `Self-hosted bot api server`
4. Click **Create application**.
5. Copy `api_id` (numbers) and `api_hash` (letters+numbers). Keep them secret.

---

## 4. Get BOT_TOKEN (2 min)

1. Open [@BotFather](https://t.me/BotFather) in Telegram.
2. Send `/newbot`, follow the questions (name + username).
3. Copy the token that looks like `123456:ABC-...`. Keep it secret.
4. Optional: send `/setprivacy` → disable? Not needed. Just start chatting with your new bot once (`/start`).

---

## 5. Deploy on Dokploy (recommended)

You already have the code at `https://github.com/realmjunaid/dbot`.

### Step 1 — Push code (already done if you see files on GitHub)

Required files in repo root:

```
bot.py
requirements.txt
Dockerfile
docker-compose.yml   <- local API, 2GB
```

### Step 2 — Create service in Dokploy

1. Dokploy → your Project → **Create Service → Application**.
2. **Provider:** GitHub → select repo `realmjunaid/dbot`, branch `main`.
3. **Build Type:** `Docker Compose`.
4. **Compose File:** `docker-compose.yml`.
5. No port / domain needed. If Dokploy asks for a domain, leave it empty or ignore — bot needs no incoming web traffic.

### Step 3 — Add environment variables

Dokploy → Service → **Environment** tab → add these 3:

```env
BOT_TOKEN=123456:ABC-your-bot-token
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=abcdef1234567890abcdef1234567890
```

Optional:

```env
ALLOWED_IDS=
```

> Leave `ALLOWED_IDS` empty = anyone can use the bot.
> Set it to your Telegram numeric ID (e.g. `ALLOWED_IDS=123456789`) = only you.

Click **Deploy**.

### Step 4 — Check logs

First start takes 1–2 minutes (`botapi` logs into Telegram).

Look for:

```
botapi  | ... authorised ...
bot     | Bot running... (MAX_ZIP_MB=100)
```

Then open Telegram → your bot → send a gallery link.

---

## 6. Use the bot

No command needed. Just paste a link directly:

```
https://e-hentai.org/g/xxxx/yyyy/
https://pawchive.pw/patreon/user/xxx/post/xxx
```

Bot direct zip pathabe (kono button na):

```
Scanning...
░░░░░░░░░░ 0% (finding images)
61 images, ~145 MB
Downloading...
██████░░░░ 45% (28/61)
Zipping...
██████░░░░ 60% (37/61)
Uploading 1 zip file...
gallery — part 1/1 (145 MB)
Done! 61 files, 1 zip.
```

Shuru-tei gallery scan kore file count + estimated size dekhay, tarpor download shuru hoy.
2GB-er moddhe hole single zip, besi hole part1, part2...

## 6b. Mega file link

```
https://mega.nz/file/AbC123#keyXYZ
```

* `.zip/.rar/.7z` hole **direct** pathabe (re-zip na)
* Single image/video/doc hole **direct document** hisebe jabe
* 2GB besi hole refuse korbe
* Free IP quota sesh hole `Mega free quota sesh` dekhabe — pore try koro ba `MEGA_EMAIL`/`MEGA_PASSWORD` env dao (quota bare)
* Folder link (`/folder/...`) supported — sob file namiye **zip** kore pathabe (live test pass, 17 file verified)

## 6d. Direct file link (universal)

```
https://files.catbox.moe/abc123.mp4
https://cdn.discordapp.com/.../pic.png
https://example.com/book.pdf
```

* Gallery/Mega/Terabox na — **jekono direct file link** namiye dibe
* 1 ta file hole direct document, ek message-e onek link thakle zip
* Webpage link hole refuse korbe (`Direct file paini`)
* 2GB+ file skip, JS/Captcha wall host-e hobe na

## 6e. Video link (YouTube / Facebook / Instagram / TikTok)

```
https://youtu.be/xxxx
https://vt.tiktok.com/xxxx
https://fb.watch/xxxx
https://instagram.com/reel/xxxx
```

* yt-dlp diye **mp4 (max 1080p)** namiye document hisebe pathabe
* **YouTube video** hole quality button asbe (1080p/720p/480p...) — choose korle oi quality-te download
* **Shorts + FB/IG/TikTok** auto-highest (button nai)
* **FB/IG photo post + carousel** — image gulo original-e **direct document** (zip na)
* 1.9GB+ file refuse, 60 min+ video refuse, live stream refuse
* **FB photo post** — m.story album theke SUDHU oi post-er photo (onno post/sticker asbe na), s2048 full-size, sob direct document
* 8+ photo-r post-e static HTML-e na thakle baki gulo miss hote pare — tokhon repo root-e `cookies.txt` (browser export, Netscape format) rekhe redeploy dao, login session-e full render Ashe. `cookies.txt` git-e jabe na (.gitignore)
* **IG photo/carousel** — custom scraper (yt-dlp photo-te fail): s1080 originals, sob direct document. Video/reel hole yt-dlp fallback
* Login wall (private/FB/IG) asle `cookies.txt` repo root-e rekhe redeploy dao (browser theke export)

## 6c. Terabox share link

```
https://1024terabox.com/s/1AbC2dEf
```

* Share-er sob file namiye **zip** kore pathabe (gallery flow — progress bar soho)
* Verification captcha asle clean message dibe (VPS IP-block hole pore try koro)
* Expired/deleted link-e clean message
* Experimental — real link diye test kore confirm koro

---

## 7. Run locally (test on PC)

```bash
# 1. clone
git clone https://github.com/realmjunaid/dbot.git
cd dbot

# 2. env file
copy .env.example .env
# edit .env -> BOT_TOKEN only (zip fixed 2GB)

# 3. install + run (simple mode, no local API)
pip install -r requirements.txt
python bot.py
```

For full 2GB local mode on PC you need Docker:

```bash
docker compose up --build
```

---

## 8. Zip size

Fixed **2000MB (2GB) per zip** (`bot.py`-te hardcode). 2GB-er moddhe hole
single zip, besi hole part1, part2... Change korte chaile `bot.py`-te
`MAX_ZIP_MB = 2000` line edit kore redeploy dao.

---

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `BOT_TOKEN .env-e bosao` | Env var missing in Dokploy. Add `BOT_TOKEN`. |
| Bot replies `❌ Download fail` | Link wrong, post deleted, or Cloudflare block. Try link in browser first. |
| `botapi` keeps restarting | Wrong `TELEGRAM_API_ID/HASH`. Recopy from my.telegram.org. |
| `❌ Error: Timed out` on 90MB+ zip (kintu file eshe jay) | Old build-er choto upload timeout. New version-e max (write 2h) — redeploy dao. |
| Zip not received, `413 Request Entity Too Large` | You are on official API, not local. Check `TELEGRAM_API_BASE_URL=http://botapi:8081/bot` is set and `botapi` is running. |
| `Short name` error on my.telegram.org | Must be 5–32 alphanumeric. Use `dbotapp`, not `DBot`. |
| Dokploy asks for domain/port | Skip it. Polling bot needs no inbound port. `expose: 8081` is internal only (bot → botapi). |
| Only I want to use the bot | Set `ALLOWED_IDS` to your Telegram ID. Get it from [@userinfobot](https://t.me/userinfobot). |

---

## 10. Files

| File | Purpose |
|---|---|
| `bot.py` | All-in-one: scraper + Telegram handlers, download→zip→send, size-based split |
| `Dockerfile` | Bot container (`python:3.12-slim`) |
| `docker-compose.yml` | Bot + local Bot API (2GB max) |
| `requirements.txt` | `cloudscraper, beautifulsoup4, python-telegram-bot, python-dotenv` |

---

## 11. Security notes

- Never commit `.env` or tokens. `.gitignore` already excludes `.env`.
- Rotate `@BotFather` token if it ever leaks (`/revoke`).
- `TELEGRAM_API_ID/HASH` belong to your Telegram account — don't share screenshots of them.
