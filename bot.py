import asyncio
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

import eh_downloader as dl

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# MB per zip. Default 48 = normal Bot API safe (50MB limit).
# Local Bot API server chalale 100 / 200 / 2000 porjonto dite parba.
MAX_ZIP_MB = float(os.getenv("MAX_ZIP_MB", "48"))
ALLOWED_IDS = os.getenv("ALLOWED_IDS", "").strip()  # optional: "123,456" — khali thakle sobai use korte parbe
# Local Bot API server use korle (docker-compose): http://botapi:8081
API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL", "").strip()
API_BASE_FILE_URL = os.getenv("TELEGRAM_API_BASE_FILE_URL", "").strip()

URL_RE = re.compile(r'https?://[^\s]+')


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_IDS:
        return True
    try:
        return str(user_id) in [x.strip() for x in ALLOWED_IDS.split(",")]
    except Exception:
        return True


def make_zip_parts(src_dir: str, out_dir: str, base_name: str, max_bytes: int):
    """src_dir-er files size onujayi multiple zip-e vag kore. Returns [zip_paths]."""
    files = sorted([p for p in Path(src_dir).rglob("*") if p.is_file() and not p.name.endswith(".tmp")])
    if not files:
        return []

    safe_base = re.sub(r'[\\/*?:"<>|]', "", base_name).strip()[:80] or "gallery"
    zips = []
    part = 1
    cur_zip_path = None
    cur_zip = None
    cur_size = 0

    def open_new_part():
        nonlocal part, cur_zip_path, cur_zip, cur_size
        if cur_zip:
            cur_zip.close()
        cur_zip_path = os.path.join(out_dir, f"{safe_base}_part{part}.zip")
        cur_zip = zipfile.ZipFile(cur_zip_path, "w", zipfile.ZIP_STORED)
        cur_size = 0
        part += 1

    open_new_part()
    for f in files:
        fsize = f.stat().st_size
        # single file-i limit er besi holeo alada zip-e dhukao (Telegram emniteo katbe)
        if cur_size > 0 and cur_size + fsize > max_bytes:
            zips.append(cur_zip_path)
            open_new_part()
        arc = os.path.relpath(f, src_dir)
        cur_zip.write(f, arc)
        cur_size += fsize

    if cur_zip:
        cur_zip.close()
        zips.append(cur_zip_path)

    return zips


def blocking_download(url: str, workdir: str):
    """Thread-e chalano blocking download. Returns (target_dir, total_bytes)."""
    target = dl.download_any(url, save_path=workdir)
    if not target or not os.path.isdir(target):
        return None, 0
    total = 0
    for root, _, filenames in os.walk(target):
        for fn in filenames:
            if fn.endswith(".tmp"):
                continue
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return target, total


async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Link pathao — ami download kore zip kore dibo.\n"
        f"1 zip = {MAX_ZIP_MB:.0f}MB max, boro gallery hole part1, part2... ashbe.\n"
        "Usage: /dl <gallery/post url>"
    )


async def dl_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /dl <url>")
        return
    await handle_one_url(update, ctx.args[0].strip())


async def url_listener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not update.message or not update.message.text:
        return
    m = URL_RE.search(update.message.text)
    if not m:
        return
    url = m.group(0)
    if "e-hentai.org" not in url and "exhentai.org" not in url and "pawchive.pw" not in url:
        return
    await handle_one_url(update, url)


async def handle_one_url(update: Update, url: str):
    status = await update.message.reply_text("🔍 Downloading... (eta boro gallery hole somoy lagbe)")
    workdir = tempfile.mkdtemp(prefix="tgdl_")
    try:
        target, total = await asyncio.to_thread(blocking_download, url, workdir)

        if not target:
            await status.edit_text("❌ Download fail. Link / Cloudflare check koro.")
            return

        n_files = sum(1 for _ in Path(target).rglob("*") if _.is_file())
        if n_files == 0:
            await status.edit_text("❌ Kono file paini.")
            return

        await status.edit_text(
            f"📦 {n_files} files ({total/1048576:.1f}MB) — {MAX_ZIP_MB:.0f}MB chunk e zip hocche..."
        )

        zipdir = os.path.join(workdir, "_zips")
        os.makedirs(zipdir, exist_ok=True)
        base = os.path.basename(target.rstrip(os.sep))
        max_bytes = int(MAX_ZIP_MB * 1024 * 1024)

        zips = await asyncio.to_thread(make_zip_parts, target, zipdir, base, max_bytes)
        if not zips:
            await status.edit_text("❌ Zip banano jayni.")
            return

        await status.edit_text(f"📤 {len(zips)} ta zip pathacchi...")
        for i, zp in enumerate(zips, start=1):
            size_mb = os.path.getsize(zp) / 1048576
            await update.message.reply_document(
                document=open(zp, "rb"),
                filename=os.path.basename(zp),
                caption=f"📦 {base} — part {i}/{len(zips)} ({size_mb:.1f}MB)",
            )

        await status.edit_text(f"🎉 Done! {n_files} files, {len(zips)} zip.")
    except Exception as e:
        try:
            await status.edit_text(f"❌ Error: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN .env-e bosao. (.env.example dekho)")
    builder = ApplicationBuilder().token(BOT_TOKEN)
    if API_BASE_URL:
        builder = builder.base_url(API_BASE_URL)
    if API_BASE_FILE_URL:
        builder = builder.base_file_url(API_BASE_FILE_URL)
    app = builder.build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("dl", dl_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_listener))
    print(f"Bot running... (MAX_ZIP_MB={MAX_ZIP_MB})")
    app.run_polling()


if __name__ == "__main__":
    main()
