import os
import re
import time
import random
import threading
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor
import cloudscraper
import requests

_thread_local = threading.local()

try:
    # Windows console cp1252/codepage-e emoji print-e crash korto (UnicodeEncodeError)
    import sys as _sys
    if hasattr(_sys.stdout, 'reconfigure'):
        _sys.stdout.reconfigure(errors='replace')
    if hasattr(_sys.stderr, 'reconfigure'):
        _sys.stderr.reconfigure(errors='replace')
except Exception:
    pass


def new_scraper():
    """Fresh cloudscraper session (+cookies.txt thakle login cookies soho)."""
    sc = cloudscraper.create_scraper(
        browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
    )
    try:
        import http.cookiejar as _cj
        if os.path.exists('cookies.txt'):
            jar = _cj.MozillaCookieJar('cookies.txt')
            jar.load(ignore_discard=True, ignore_expires=True)
            n = 0
            for c in jar:
                # domain thik rekhe session-e daw (onno site-e leak hobe na)
                sc.cookies.set_cookie(c)
                n += 1
            if n:
                print(f" cookies.txt loaded ({n} cookies)", flush=True)
    except Exception as e:
        print(f" cookies.txt load fail: {str(e)[:100]}", flush=True)
    return sc


def get_scraper():
    # proti thread-e alada session: fast + thread-safe
    if not hasattr(_thread_local, "scraper"):
        _thread_local.scraper = new_scraper()
    return _thread_local.scraper


def sanitize_folder_name(name):
    """Removes invalid characters from folder names."""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def sanitize_file_name(name, max_len=120):
    """Original title theke safe filename. Sorting-er jonno number prefix alada thakbe."""
    name = unquote(name).strip().replace('+', ' ')
    name = re.sub(r'[\\/*?:"<>|]', "", name).strip()
    name = re.sub(r'\s+', ' ', name)
    if len(name) > max_len:
        stem, dot, ext = name.rpartition('.')
        if dot and len(ext) <= 4:
            name = stem[:max_len - len(ext) - 1] + dot + ext
        else:
            name = name[:max_len]
    return name or "image"


TERA_DOMAINS = (
    'terabox.com', '1024terabox.com', 'teraboxapp.com', 'mirrobox.com',
    'nephobox.com', 'freemibox.com', '1024tera.com', 'teraboxlink.com',
)
TERA_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

VIDEO_DOMAINS = (
    'youtube.com', 'youtu.be',
    'tiktok.com', 'vt.tiktok.com', 'vm.tiktok.com',
)
FB_DOMAINS = ('facebook.com', 'fb.watch', 'fb.com')
# 60 min-er besi video refuse (2GB cross + ghonta lege jay)
VIDEO_MAX_MINUTES = 60


def is_video_url(url):
    low = url.lower()
    return any(d in low for d in VIDEO_DOMAINS)


def is_terabox_url(url):
    return any(d in url for d in TERA_DOMAINS)


def parse_terabox_surl(url):
    """Share link theke surl ber kore. /s/XXX, ?surl=XXX 2 format-e."""
    m = re.search(r'surl=([A-Za-z0-9_-]+)', url)
    if m:
        return m.group(1)
    m = re.search(r'/s/([A-Za-z0-9_-]+)', url)
    if m:
        return m.group(1)
    return None


def extract_terabox_auth(page):
    """Share page HTML theke sign/timestamp/shareid/uk ber kore. Multiple pattern try."""
    out = {}
    pats = {
        'sign': [r'"sign"\s*:\s*"([a-f0-9]{20,})"', r"'sign'\s*:\s*'([a-f0-9]{20,})'",
                 r'sign%22%3A%22([a-f0-9]{20,})', r'sign=([a-f0-9]{20,})'],
        'timestamp': [r'"timestamp"\s*:\s*(\d{9,})', r"'timestamp'\s*:\s*(\d{9,})",
                      r'timestamp%22%3A(\d{9,})'],
        'shareid': [r'"share_id"\s*:\s*(\d+)', r'"shareid"\s*:\s*(\d+)', r"'share_id'\s*:\s*(\d+)",
                    r'shareid%22%3A(\d+)'],
        'uk': [r'"share_uk"\s*:\s*"(\d+)"', r'"share_uk"\s*:\s*(\d+)', r'"uk"\s*:\s*(\d+)',
               r"'share_uk'\s*:\s*(\d+)", r'share_uk%22%3A%22(\d+)'],
    }
    for key, plist in pats.items():
        for p in plist:
            m = re.search(p, page)
            if m:
                out[key] = m.group(1)
                break
    return out


def blocking_terabox_download(url, workdir, byte_cb=None):
    """Terabox share link -> sob file namay. Returns target_dir.
    Raises ValueError(VERIFY/LOGIN/EXPIRED/EMPTY) clean message-er jonno."""
    surl = parse_terabox_surl(url)
    if not surl:
        raise ValueError("BAD_LINK")

    sess = requests.Session()
    sess.headers.update({'User-Agent': TERA_UA})

    # 1. share page (nijer domain age, fallback www.terabox.com)
    try:
        host = re.search(r'https?://([^/]+)', url).group(1)
    except Exception:
        host = 'www.terabox.com'
    page, auth, share_page_url = None, {}, ''
    for h in dict.fromkeys([host, 'www.terabox.com', '1024terabox.com']):
        try:
            share_page_url = f"https://{h}/sharing/link?surl={surl}"
            r = sess.get(share_page_url, timeout=30)
            if r.status_code == 200 and len(r.text) > 5000:
                page = r.text
                auth = extract_terabox_auth(page)
                if auth.get('sign'):
                    break
        except Exception:
            continue
    if not page:
        raise ValueError("PAGE_FAIL")
    low = page.lower()
    if not auth.get('sign'):
        if 'verify' in low or 'captcha' in low or 'vcode' in low:
            raise ValueError("VERIFY")
        raise ValueError("EXPIRED")

    base_params = {
        'app_id': '250528', 'web': '1', 'channel': 'dubox', 'clienttype': '0',
        'sign': auth['sign'], 'timestamp': auth.get('timestamp', ''),
        'shareid': auth.get('shareid', ''), 'uk': auth.get('uk', ''),
    }

    # 2. file list (dir recursive, depth 3)
    all_files, total_size, queue, seen = [], [0], [('', 0)], set()

    def list_dir(path):
        """Sob page ghure file list. Terabox page-e max ~100 dey."""
        out, page = [], 1
        while True:
            params = dict(base_params, page=str(page), num='100', order='time', desc='1', dir=path)
            r = sess.get('https://www.terabox.com/share/list', params=params,
                         headers={'Referer': share_page_url}, timeout=30)
            data = r.json()
            if data.get('errno') not in (0, None):
                raise ValueError(f"ERRNO_{data.get('errno')}")
            batch = data.get('list') or []
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    while queue:
        dpath, depth = queue.pop(0)
        if depth > 3 or dpath in seen:
            continue
        seen.add(dpath)
        try:
            items = list_dir(dpath)
        except ValueError:
            raise
        except Exception:
            continue
        for it in items:
            if it.get('isdir'):
                queue.append((it.get('path', ''), depth + 1))
            else:
                all_files.append(it)
                try:
                    total_size[0] += int(it.get('size') or 0)
                except (TypeError, ValueError):
                    pass

    if not all_files:
        raise ValueError("EMPTY")

    # 3. download protita file (dlink direct, na thakle download API)
    target_dir = os.path.join(workdir, sanitize_folder_name(f"terabox_{surl[:12]}"))
    os.makedirs(target_dir, exist_ok=True)
    done_bytes = [0]

    for it in all_files:
        name = sanitize_file_name(it.get('server_filename') or 'file')
        dlink = it.get('dlink') or ''
        if not dlink:
            try:
                p = dict(base_params, fidlist=f"[{it.get('fs_id')}]", type='nolimit')
                r = sess.get('https://www.terabox.com/api/download', params=p,
                             headers={'Referer': share_page_url}, timeout=30)
                d = r.json()
                if isinstance(d, dict):
                    dl = d.get('dlink') or d.get('list') or []
                    if isinstance(dl, list) and dl:
                        dlink = dl[0].get('dlink', '') if isinstance(dl[0], dict) else ''
                    elif isinstance(dl, str):
                        dlink = dl
            except Exception:
                dlink = ''
        if not dlink:
            continue
        out = os.path.join(target_dir, name)
        k = 1
        stem, dot, ext = out.rpartition('.')
        while os.path.exists(out):
            out = f"{stem}({k}){dot}{ext}" if dot else f"{out}({k})"
            k += 1
        r = sess.get(dlink, headers={'Referer': share_page_url, 'User-Agent': TERA_UA},
                     stream=True, timeout=60)
        r.raise_for_status()
        with open(out, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 256):
                if not chunk:
                    continue
                f.write(chunk)
                done_bytes[0] += len(chunk)
                if byte_cb is not None:
                    try:
                        byte_cb(done_bytes[0], total_size[0])
                    except Exception:
                        pass
        if os.path.getsize(out) < 1024:
            os.remove(out)

    files = [p for p in Path(target_dir).rglob("*") if p.is_file()]
    if not files:
        raise ValueError("EMPTY")
    return target_dir

# ================= BOT =================
import asyncio
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# Fixed 2000MB (2GB) per zip — local Bot API server-er max limit.
# Er besi Telegram nebe na, tai 2000-er upore dio na.
MAX_ZIP_MB = 2000
ALLOWED_IDS = os.getenv("ALLOWED_IDS", "").strip()  # optional: "123,456" — khali thakle sobai use korte parbe
# Local Bot API server use korle (docker-compose): http://botapi:8081
API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL", "").strip()
API_BASE_FILE_URL = os.getenv("TELEGRAM_API_BASE_FILE_URL", "").strip()
# Mega Pro login thakle quota bare (optional — khali thakle anonymous).
MEGA_EMAIL = os.getenv("MEGA_EMAIL", "").strip()
MEGA_PASSWORD = os.getenv("MEGA_PASSWORD", "").strip()

URL_RE = re.compile(r'https?://[^\s]+')

START_TIME = time.time()
ACTIVE_JOBS = 0

# YT quality choice: key -> {"url": ..., "user_id": ..., "ts": ...}
PENDING_Q = {}
# yt-dlp info cache: url -> (ts, info). Video+quality 2 bar info chay — 10 min reuse.
INFO_CACHE = {}
INFO_TTL = 600


def is_youtube_video(url):
    """Regular YT video (buttons). Shorts auto-highest."""
    low = url.lower()
    return ('youtube.com' in low or 'youtu.be' in low) and '/shorts/' not in low


def is_allowed(user_id: int) -> bool:
    if not ALLOWED_IDS:
        return True
    try:
        return str(user_id) in [x.strip() for x in ALLOWED_IDS.split(",")]
    except Exception:
        return True


def make_zip_parts(src_dir: str, out_dir: str, base_name: str, max_bytes: int, progress_cb=None):
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
    total = len(files)
    skipped_big = 0
    for n, f in enumerate(files, start=1):
        fsize = f.stat().st_size
        if fsize > max_bytes:
            # single file-i limit-er besi — Telegram-e jabe na, skip
            print(f" Skip oversize ({fsize//1048576}MB): {f.name}", flush=True)
            skipped_big += 1
            if progress_cb is not None:
                try:
                    progress_cb(n, total)
                except Exception:
                    pass
            continue
        # ager part full hole notun part khulo
        if cur_size > 0 and cur_size + fsize > max_bytes:
            zips.append(cur_zip_path)
            open_new_part()
        arc = os.path.relpath(f, src_dir)
        cur_zip.write(f, arc)
        cur_size += fsize
        if progress_cb is not None:
            try:
                progress_cb(n, total)
            except Exception:
                pass

    if cur_zip:
        cur_zip.close()
        zips.append(cur_zip_path)

    return zips


async def send_one_file(chat, path, caption: str):
    """Ekta file flood-safe way-te pathay. RetryAfter/TimedOut hole wait kore retry.
    Returns: 'doc' / 'photo' / 'other' / 'failed' — Telegram ki hisebe nilo."""
    kind = 'failed'
    for attempt in range(4):
        try:
            with open(path, "rb") as fh:
                msg = await chat.send_document(
                    document=fh,
                    filename=os.path.basename(path),
                    caption=caption,
                    read_timeout=3600,
                    write_timeout=7200,
                    connect_timeout=120,
                    pool_timeout=120,
                )
            if getattr(msg, 'document', None) is not None:
                kind = 'doc'
            elif getattr(msg, 'photo', None):
                kind = 'photo'
            else:
                kind = 'other'
            break
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TimedOut:
            if attempt == 3:
                raise
            await asyncio.sleep(3)
    # porpor 100+ file gele Telegram flood dey, tai choto gap
    await asyncio.sleep(0.7)
    return kind


async def _safe_edit(status, text: str):
    try:
        await status.edit_text(text)
    except Exception:
        pass


def make_progress_cb(status, loop, label="Downloading..."):
    """Worker thread theke status message-e progress bar update kore (throttled)."""
    state = {'t': 0.0}

    def cb(done: int, total: int):
        now = time.monotonic()
        if now - state['t'] < 4 and done < total:
            return
        state['t'] = now
        pct = int(done * 100 / max(total, 1))
        bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
        asyncio.run_coroutine_threadsafe(
            _safe_edit(status, f"{label}\n{bar} {pct}% ({done}/{total})"),
            loop,
        )

    return cb


def make_byte_progress_cb(status, loop, label, total_bytes):
    """Stream download-er jonno MB-based progress bar (Terabox/Mega)."""
    state = {'t': 0.0}

    def cb(done_bytes: int, total: int = 0):
        now = time.monotonic()
        tot = total or total_bytes or 0
        if now - state['t'] < 4 and (not tot or done_bytes < tot):
            return
        state['t'] = now
        if tot:
            pct = int(done_bytes * 100 / tot)
            bar = '█' * (pct // 10) + '░' * (10 - pct // 10)
            txt = f"{label}\n{bar} {pct}% ({human_size(done_bytes)}/{human_size(tot)})"
        else:
            txt = f"{label}\n{human_size(done_bytes)} downloaded..."
        asyncio.run_coroutine_threadsafe(_safe_edit(status, txt), loop)

    return cb


async def start_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Link pathao — ami download kore Telegram-e diye dibo.\n\n"
        "Supported:\n"
        "- Mega file/folder link\n"
        "- Terabox share link\n"
        "- Facebook post (photo/video)\n"
        "- Instagram post/reel\n"
        "- YouTube / TikTok video\n"
        "- Jekono direct file link\n\n"
        "Commands: /status /ping /help"
    )


async def ping_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t0 = time.monotonic()
    m = await update.message.reply_text("Pong...")
    ms = int((time.monotonic() - t0) * 1000)
    await m.edit_text(f"Pong! {ms}ms")


async def status_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    up = int(time.time() - START_TIME)
    h, rem = divmod(up, 3600)
    mnt, sec = divmod(rem, 60)
    try:
        free = shutil.disk_usage(tempfile.gettempdir()).free
        disk = human_size(free)
    except Exception:
        disk = "?"
    await update.message.reply_text(
        f"Status: online\n"
        f"Uptime: {h}h {mnt}m {sec}s\n"
        f"Active jobs: {ACTIVE_JOBS}\n"
        f"Disk free: {disk}"
    )


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Mega file -> direct. Mega folder -> zip.\n"
        "Terabox share -> files -> zip.\n"
        "FB / IG photo -> direct document. FB video -> document.\n"
        "YouTube video -> quality button. Shorts/TikTok -> auto 1080p.\n"
        "Onno link -> direct file (1 ta hole document, onekgula hole zip, max 20 link).\n\n"
        "Limits: 2GB max file/zip, 60 min max video, no live stream.\n"
        "Sob temp file send-er por auto-delete hoy."
    )


async def url_listener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not update.message or not update.message.text:
        return
    if not URL_RE.search(update.message.text):
        return
    global ACTIVE_JOBS
    ACTIVE_JOBS += 1
    try:
        await route_url(update)
    finally:
        ACTIVE_JOBS -= 1


async def route_url(update: Update):
    urls = URL_RE.findall(update.message.text)
    if not urls:
        return
    urls = list(dict.fromkeys(urls))[:20]  # dedupe, max 20
    url = urls[0]
    if "mega.nz" in url or "mega.io" in url:
        await send_mega(update.message, url)
        return
    if is_terabox_url(url):
        await send_terabox(update.message, url)
        return
    if is_fb_url(url):
        await send_facebook(update.message, url)
        return
    if is_instagram_url(url):
        await send_instagram(update.message, url)
        return
    if is_video_url(url):
        await send_video(update.message, url)
        return
    await send_direct(update.message, urls)


def human_size(num):
    """Bytes -> '145.4 MB' format. None/unknown hole '?' dey."""
    if not num or num <= 0:
        return "?"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i, f = 0, float(num)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    txt = f"{f:.1f}".rstrip('0').rstrip('.')
    return f"{txt} {units[i]}"


async def zip_and_send(status, chat, target_dir: str, total: int):
    """Downloaded folder -> zip (+progress) -> upload."""
    loop = asyncio.get_running_loop()
    n_files = sum(1 for _ in Path(target_dir).rglob("*")
                  if _.is_file() and not _.name.endswith(".tmp"))
    if n_files == 0:
        await status.edit_text("Kono file paini.")
        return

    await status.edit_text(
        f"{n_files} files ({total/1048576:.1f}MB) — zip hocche..."
    )

    zipdir = os.path.join(os.path.dirname(target_dir.rstrip(os.sep)), "_zips")
    os.makedirs(zipdir, exist_ok=True)
    base = os.path.basename(target_dir.rstrip(os.sep))
    max_bytes = int(MAX_ZIP_MB * 1024 * 1024)

    await status.edit_text("Zipping...")
    zips = await asyncio.to_thread(
        make_zip_parts, target_dir, zipdir, base, max_bytes,
        make_progress_cb(status, loop, "Zipping..."),
    )
    if not zips:
        await status.edit_text("Zip banano jayni.")
        return

    n_zips = len(zips)
    await status.edit_text(f"Uploading {n_zips} zip {'file' if n_zips == 1 else 'files'}...")
    kinds = []
    for i, zp in enumerate(zips, start=1):
        size_mb = os.path.getsize(zp) / 1048576
        kinds.append(await send_one_file(chat, zp, f"{base} — part {i}/{len(zips)} ({size_mb:.1f}MB)"))

    summary = f"doc:{kinds.count('doc')} photo:{kinds.count('photo')} other:{kinds.count('other')} failed:{kinds.count('failed')}"
    print(f"Send kinds: {summary}", flush=True)
    await status.edit_text(f"Done! {n_files} files, {len(zips)} zip ({summary}).")


def mega_folder_api(node, payload):
    """Mega share-context (n=node) API call. int return = errno."""
    seq = random.randint(100000, 999999)
    ep = f'https://g.api.mega.co.nz/cs?id={seq}&n={node}'
    r = requests.post(ep, json=payload, timeout=30)
    d = r.json()
    d = d[0] if isinstance(d, list) else d
    if isinstance(d, int):
        raise ValueError(f'MEGA_ERRNO_{d}')
    return d


def mega_fold_key(k8):
    return (k8[0] ^ k8[4], k8[1] ^ k8[5], k8[2] ^ k8[6], k8[3] ^ k8[7])


def blocking_mega_folder_download(url, workdir, byte_cb=None):
    """Mega folder link -> sob file namay (decrypt soho). Returns target_dir."""
    from Crypto.Cipher import AES
    from Crypto.Util import Counter
    from mega.crypto import (
        a32_to_str, base64_url_decode, decrypt_attr, decrypt_key,
        get_chunks, str_to_a32,
    )
    m = re.search(r'folder/([A-Za-z0-9_-]+)#([A-Za-z0-9_-]+)', url)
    if not m:
        raise ValueError("BAD_LINK")
    node, key_b64 = m.group(1), m.group(2)
    try:
        urlkey = str_to_a32(base64_url_decode(key_b64))
    except Exception:
        raise ValueError("BAD_LINK")

    try:
        data = mega_folder_api(node, [{'a': 'f', 'c': 1, 'ca': 1, 'r': 1}])
    except ValueError as e:
        code = str(e)
        if 'MEGA_ERRNO_-9' in code:
            raise ValueError("EXPIRED")
        if 'MEGA_ERRNO_-16' in code or 'MEGA_ERRNO_-18' in code:
            raise ValueError("QUOTA")
        if 'MEGA_ERRNO_-11' in code:
            raise ValueError("EACCESS")
        raise
    nodes = data.get('f', [])

    files = []
    for f in nodes:
        if f.get('t') != 0 or ':' not in (f.get('k') or ''):
            continue
        try:
            fk8 = decrypt_key(str_to_a32(base64_url_decode(f['k'].rsplit(':', 1)[1])), urlkey)
            enc_at = f.get('a') or f.get('at')
            at = decrypt_attr(base64_url_decode(enc_at), mega_fold_key(fk8)) if enc_at else None
            if not at or not at.get('n'):
                continue
            files.append((f['h'], at['n'], int(f.get('s') or 0), fk8))
        except Exception:
            continue
    if not files:
        raise ValueError("EMPTY")

    target_dir = os.path.join(workdir, sanitize_folder_name(f"mega_{node[:8]}"))
    os.makedirs(target_dir, exist_ok=True)
    total_all = sum(s for _, _, s, _ in files)
    done_all = [0]
    done_lock = threading.Lock()
    sess = requests.Session()

    def mac_update(encryptor, mac_encryptor, chunk):
        """Mega-exact MAC, kintu C-level bulk op — per-block Python loop-er bodle."""
        tail_len = len(chunk) % 16
        if tail_len:
            bulk, tail = chunk[:-tail_len], chunk[-tail_len:] + b'\0' * (16 - tail_len)
        else:
            bulk, tail = chunk[:-16], chunk[-16:]
        if bulk:
            encryptor.encrypt(bulk)
        return mac_encryptor.encrypt(encryptor.encrypt(tail))

    def fetch_one(item):
        handle, name, size, fk8 = item
        safe = sanitize_file_name(name)
        out = os.path.join(target_dir, safe)
        if os.path.exists(out):
            stem, dot, ext = safe.rpartition('.')
            k = 1
            while os.path.exists(out):
                out = os.path.join(target_dir, f"{stem}({k}){dot}{ext}" if dot else f"{safe}({k})")
                k += 1
        try:
            try:
                g = mega_folder_api(node, [{'a': 'g', 'g': 1, 'n': handle}])
            except ValueError as e:
                if 'QUOTA' in str(e) or '-16' in str(e) or '-18' in str(e):
                    raise ValueError("QUOTA")
                return
            tmp_url = g.get('g', '') if isinstance(g, dict) else ''
            if not tmp_url:
                return
            k = mega_fold_key(fk8)
            iv = fk8[4:6]
            meta_mac = fk8[6:8]
            k_str = a32_to_str(k)
            counter = Counter.new(128, initial_value=((iv[0] << 32) + iv[1]) << 64)
            aes = AES.new(k_str, AES.MODE_CTR, counter=counter)
            mac_str = '\0' * 16
            mac_encryptor = AES.new(k_str, AES.MODE_CBC, mac_str.encode("utf8"))
            iv_str = a32_to_str([iv[0], iv[1], iv[0], iv[1]])
            r = sess.get(tmp_url, stream=True, timeout=60)
            r.raise_for_status()
            raw = r.raw
            try:
                with open(out, 'wb') as f:
                    for _, chunk_size in get_chunks(size):
                        need, parts = chunk_size, []
                        while need > 0:
                            piece = raw.read(need)
                            if not piece:
                                break
                            parts.append(piece)
                            need -= len(piece)
                        chunk = aes.decrypt(b''.join(parts))
                        if not chunk:
                            break
                        f.write(chunk)
                        with done_lock:
                            done_all[0] += len(chunk)
                            cur = done_all[0]
                        if byte_cb is not None:
                            try:
                                byte_cb(cur, total_all)
                            except Exception:
                                pass
                        encryptor = AES.new(k_str, AES.MODE_CBC, iv_str)
                        mac_str = mac_update(encryptor, mac_encryptor, chunk)
            finally:
                try:
                    r.close()
                except Exception:
                    pass
            file_mac = str_to_a32(mac_str)
            if (file_mac[0] ^ file_mac[1], file_mac[2] ^ file_mac[3]) != meta_mac:
                try:
                    os.remove(out)  # corrupt file — skip, baki gulo cholbe
                except OSError:
                    pass
                return
        except ValueError:
            raise
        except Exception:
            try:
                if os.path.exists(out):
                    os.remove(out)
            except OSError:
                pass

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(fetch_one, files))

    got = [p for p in Path(target_dir).rglob("*") if p.is_file()]
    if not got:
        raise ValueError("EMPTY")
    return target_dir


def blocking_mega_download(url: str, workdir: str, byte_cb=None):
    """Mega file link theke 1 ta file, folder link theke sob file namay.
    Returns file path ba folder path, naile exception."""
    if '/folder/' in url or '#F!' in url or re.search(r'folder/([A-Za-z0-9_-]+)#', url):
        return blocking_mega_folder_download(url, workdir, byte_cb)
    from mega import Mega
    mega = Mega()
    m = mega.login(MEGA_EMAIL, MEGA_PASSWORD) if MEGA_EMAIL else mega.login()
    got = m.download_url(url, dest_path=workdir)
    path = got if isinstance(got, str) else None
    if not path or not os.path.isfile(path):
        # fallback: workdir-e notun file khujo
        cands = [os.path.join(workdir, f) for f in os.listdir(workdir)]
        cands = [p for p in cands if os.path.isfile(p)]
        if not cands:
            raise RuntimeError("DOWNLOAD_EMPTY")
        path = max(cands, key=os.path.getmtime)
    return path


async def send_mega(message, url: str):
    """Mega link: single file hole direct document, folder/multiple hole zip."""
    loop = asyncio.get_running_loop()
    status = await message.reply_text("Downloading from Mega...")
    workdir = tempfile.mkdtemp(prefix="tgdl_")
    try:
        target = await asyncio.to_thread(
            blocking_mega_download, url, workdir,
            make_byte_progress_cb(status, loop, "Downloading from Mega...", 0),
        )
        if not target:
            await status.edit_text("Download fail. Link check koro.")
            return

        if os.path.isdir(target):
            files = [p for p in Path(target).rglob("*") if p.is_file()]
            if len(files) == 1:
                target = files[0]
            else:
                total = 0
                for p in files:
                    try:
                        total += os.path.getsize(p)
                    except OSError:
                        pass
                await zip_and_send(status, message.chat, target, total)
                return

        size_mb = os.path.getsize(target) / 1048576
        if size_mb > 2000:
            await status.edit_text(f"File too big ({size_mb:.0f}MB) — Telegram max 2GB.")
            return

        name = os.path.basename(target)
        await status.edit_text(f"Uploading {name} ({size_mb:.1f}MB)...")
        chat = message.chat
        k = await send_one_file(chat, target, name)
        print(f"Mega send kind: {k} ({name})", flush=True)
        await status.edit_text(f"Done! {name} ({size_mb:.1f}MB).")
    except ValueError as e:
        code = str(e)
        msg = {
            "EXPIRED": "Link expired/deleted. Notun link dao.",
            "EMPTY": "Ei folder-e kono file paini.",
            "QUOTA": "Mega free quota sesh (IP limit). Pore try koro ba MEGA_EMAIL/PASSWORD env dao.",
            "EACCESS": "Access denied. Link check koro.",
            "BAD_LINK": "Link bujha jayni. Mega file/folder link dao.",
        }.get(code)
        if msg is None and code.startswith("MEGA_ERRNO_"):
            msg = "Mega error. Pore try koro."
        await status.edit_text(msg or f"Error: {code[:200]}")
    except Exception as e:
        err = str(e)
        if 'quota' in err.lower() or 'overquota' in err.lower() or 'EOVERQUOTA' in err:
            await status.edit_text("Mega free quota sesh (IP limit). Pore try koro ba MEGA_EMAIL/PASSWORD env dao.")
        else:
            try:
                await status.edit_text(f"Error: {err[:300]}")
            except Exception:
                pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


TERA_ERRORS = {
    'BAD_LINK': "Link bujha jayni. Terabox share link dao.",
    'PAGE_FAIL': "Terabox page khule nai. Link / network check koro.",
    'VERIFY': "Terabox verification chacche (VPS IP block). Pore try koro ba cookie lagbe.",
    'EXPIRED': "Link expired/deleted. Notun share link dao.",
    'EMPTY': "Ei share-e kono file paini.",
}


async def send_terabox(message, url: str):
    """Terabox share link -> files namay -> zip -> send."""
    loop = asyncio.get_running_loop()
    status = await message.reply_text("Connecting to Terabox...")
    workdir = tempfile.mkdtemp(prefix="tgdl_")
    try:
        target = await asyncio.to_thread(
            blocking_terabox_download, url, workdir,
            make_byte_progress_cb(status, loop, "Downloading from Terabox...", 0),
        )
        if not target:
            await status.edit_text("Download fail. Link check koro.")
            return
        total = 0
        for root, _, fns in os.walk(target):
            for fn in fns:
                try:
                    total += os.path.getsize(os.path.join(root, fn))
                except OSError:
                    pass
        await zip_and_send(status, message.chat, target, total)
    except ValueError as e:
        msg = TERA_ERRORS.get(str(e), None)
        if msg is None and str(e).startswith('ERRNO_'):
            msg = "Terabox login/verification chacche. Pore try koro."
        await status.edit_text(msg or f"Error: {str(e)[:200]}")
    except Exception as e:
        try:
            await status.edit_text(f"Error: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def blocking_ytdlp_download(url, workdir, byte_cb=None, height=1080):
    """yt-dlp diye video/image namay (mp4, height cap, 1900MB cap). Returns [file paths].
    Raises ValueError(LIVE/TOO_LONG/TOO_BIG/LOGIN/NONE)."""
    import yt_dlp

    def hook(d):
        if d.get('status') == 'downloading' and byte_cb is not None:
            try:
                byte_cb(d.get('downloaded_bytes') or 0,
                        d.get('total_bytes') or d.get('total_bytes_estimate') or 0)
            except Exception:
                pass

    opts = {
        'format': (f'bv*[height<={height}][ext=mp4]+ba[ext=m4a]/bv*[height<={height}]+ba/'
                   f'b[height<={height}][ext=mp4]/b[height<={height}]/best'),
        'merge_output_format': 'mp4',
        'max_filesize': 1900 * 1024 * 1024,
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'retries': 3,
        'concurrent_fragment_downloads': 4,
        'outtmpl': os.path.join(workdir, '%(title).80s [%(id)s]-%(autonumber)02d.%(ext)s'),
        'progress_hooks': [hook],
    }
    if os.path.exists('cookies.txt'):
        opts['cookiefile'] = 'cookies.txt'

    with yt_dlp.YoutubeDL(opts) as ydl:
        hit = INFO_CACHE.get(url)
        if hit and time.time() - hit[0] < INFO_TTL:
            info = hit[1]
        else:
            try:
                info = ydl.extract_info(url, download=False)
            except Exception as e:
                err = str(e).lower()
                if 'sign in' in err or 'login' in err or 'cookies' in err or 'private' in err:
                    raise ValueError("LOGIN")
                raise
        if not info:
            raise ValueError("NONE")
        if info.get('is_live'):
            raise ValueError("LIVE")
        dur = info.get('duration') or 0
        if dur and dur > VIDEO_MAX_MINUTES * 60:
            raise ValueError("TOO_LONG")
        try:
            ydl.download([url])
        except Exception as e:
            err = str(e).lower()
            if 'larger than' in err or 'max-filesize' in err or 'file size' in err:
                raise ValueError("TOO_BIG")
            if 'sign in' in err or 'login' in err or 'cookies' in err or 'private' in err:
                raise ValueError("LOGIN")
            raise

    cands = [os.path.join(workdir, f) for f in os.listdir(workdir)]
    cands = sorted([p for p in cands if os.path.isfile(p)])
    if not cands:
        raise ValueError("NONE")
    return cands


def probe_audio(path):
    """ffprobe diye audio stream ache kina. '' / ' (no audio in source)' / ' (with audio)'."""
    import subprocess
    if not shutil.which('ffprobe'):
        return ""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'stream=codec_type',
             '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=30,
        )
        types = r.stdout.lower()
        if 'audio' in types:
            return " (with audio)"
        return " (no audio in source)"
    except Exception:
        return ""


VIDEO_ERRORS = {
    "LIVE": "Live stream download hoy na.",
    "TOO_BIG": "Video 1.9GB besi — Telegram-e jabe na.",
    "LOGIN": "Login wall (private/cookie lage). cookies.txt dao.",
    "NONE": "Video paini. Link check koro.",
}


def fetch_video_info(url):
    """yt-dlp info only (no download). Returns (info, error_code). 10 min cache."""
    import yt_dlp
    now = time.time()
    hit = INFO_CACHE.get(url)
    if hit and now - hit[0] < INFO_TTL:
        return hit[1], ""
    opts = {
        'quiet': True, 'no_warnings': True, 'noplaylist': True,
        'retries': 3,
    }
    if os.path.exists('cookies.txt'):
        opts['cookiefile'] = 'cookies.txt'
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        err = str(e).lower()
        if 'sign in' in err or 'login' in err or 'cookies' in err or 'private' in err:
            return None, "LOGIN"
        return None, "NONE"
    if not info:
        return None, "NONE"
    if info.get('is_live'):
        return None, "LIVE"
    dur = info.get('duration') or 0
    if dur and dur > VIDEO_MAX_MINUTES * 60:
        return None, "TOO_LONG"
    INFO_CACHE[url] = (now, info)
    # cache boro na hoy — 50 tar besi rakhbo na
    while len(INFO_CACHE) > 50:
        INFO_CACHE.pop(next(iter(INFO_CACHE)))
    return info, ""


def available_heights(info, cap=1080):
    """Info theke mp4 heights (cap porjonto), boro age. Max 5 ta."""
    seen = set()
    for f in info.get('formats') or []:
        h = f.get('height')
        if not h or h > cap:
            continue
        if f.get('vcodec') in (None, 'none'):
            continue
        if (f.get('ext') or '') != 'mp4' and f.get('protocol', '') not in ('https', 'http'):
            continue
        seen.add(int(h))
    return sorted(seen, reverse=True)[:5]


async def send_video(message, url: str):
    """Social video: YT regular hole quality button, Shorts+onnanno auto-highest."""
    loop = asyncio.get_running_loop()
    status = await message.reply_text("Fetching video info...")
    try:
        info, err = await asyncio.to_thread(fetch_video_info, url)
    except Exception as e:
        await status.edit_text(f"Error: {str(e)[:200]}")
        return
    if err:
        if err == "TOO_LONG":
            await status.edit_text(f"Video {VIDEO_MAX_MINUTES} min-er besi — refuse.")
        else:
            await status.edit_text(VIDEO_ERRORS.get(err, "Video paini."))
        return

    if is_youtube_video(url):
        heights = await asyncio.to_thread(available_heights, info)
        if len(heights) > 1:
            key = uuid.uuid4().hex[:8]
            sender = message.from_user.id if message.from_user else message.chat.id
            PENDING_Q[key] = {"url": url, "user_id": sender, "ts": time.time(),
                              "title": info.get('title') or 'video'}
            # expire purono entry (30 min+)
            for k in [k for k, v in PENDING_Q.items() if time.time() - v.get("ts", 0) > 1800]:
                PENDING_Q.pop(k, None)
            kb = InlineKeyboardMarkup([[
                InlineKeyboardButton(f"{h}p", callback_data=f"q:{key}:{h}")
                for h in heights
            ]])
            await status.edit_text(
                f"{(info.get('title') or 'Video')[:80]}\nChoose quality:",
                reply_markup=kb,
            )
            return
        # 1 tai quality thakle direct
        h = heights[0] if heights else 1080
        await download_and_send_video(status, message.chat, url, h, loop)
        return

    await download_and_send_video(status, message.chat, url, 1080, loop)


async def download_and_send_video(status, chat, url: str, height: int, loop):
    """Chosen/auto quality-te download + send + cleanup. 1 file=direct, onek=zip."""
    workdir = tempfile.mkdtemp(prefix="tgdl_")
    try:
        await status.edit_text(f"Downloading video ({height}p)...")
        try:
            targets = await asyncio.to_thread(
                blocking_ytdlp_download, url, workdir,
                make_byte_progress_cb(status, loop, "Downloading video...", 0),
                height,
            )
        except ValueError as e:
            code = str(e)
            await status.edit_text(VIDEO_ERRORS.get(code, f"Error: {code[:200]}"))
            return
        if len(targets) == 1:
            target = targets[0]
            size_mb = os.path.getsize(target) / 1048576
            name = os.path.basename(target)
            audio_note = ""
            if name.lower().endswith(('.mp4', '.webm', '.mov', '.m4v', '.mkv')):
                audio_note = await asyncio.to_thread(probe_audio, target)
            await status.edit_text(f"Uploading {name} ({size_mb:.1f}MB)...")
            k = await send_one_file(chat, target, name)
            print(f"Video send kind: {k} ({name})", flush=True)
            await status.edit_text(f"Done! {name} ({size_mb:.1f}MB){audio_note}.")
            return
        # social carousel/multiple: sob direct document (zip na)
        kinds, sent = [], 0
        await status.edit_text(f"{len(targets)} ta file pathacchi (original)...")
        for i, fp in enumerate(sorted(targets), start=1):
            try:
                kinds.append(await send_one_file(chat, fp, os.path.basename(fp)))
                sent += 1
            except Exception as e:
                kinds.append('failed')
                print(f"Social send fail {fp}: {str(e)[:120]}", flush=True)
            if i % 20 == 0:
                try:
                    await status.edit_text(f"{i}/{len(targets)} sent...")
                except Exception:
                    pass
        summary = f"doc:{kinds.count('doc')} photo:{kinds.count('photo')} failed:{kinds.count('failed')}"
        print(f"Social send kinds: {summary}", flush=True)
        await status.edit_text(f"Done! {sent}/{len(targets)} files sent ({summary}).")
    except Exception as e:
        try:
            await status.edit_text(f"Error: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


async def on_quality_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        _, key, h = q.data.split(":", 2)
        height = int(h)
    except ValueError:
        return
    if height <= 0 or height > 1080:
        return  # forge kora callback ignore
    item = PENDING_Q.pop(key, None)
    if not item:
        await q.edit_message_text("Expired — link ta abar pathao.")
        return
    if time.time() - item.get("ts", 0) > 1800:
        await q.edit_message_text("Expired — link ta abar pathao.")
        return
    if q.from_user.id != item["user_id"] and not is_allowed(q.from_user.id):
        await q.edit_message_text("Unauthorized.")
        return
    status = await q.edit_message_text("Starting download...")
    global ACTIVE_JOBS
    ACTIVE_JOBS += 1
    try:
        await download_and_send_video(status, q.message.chat, item["url"], height,
                                      asyncio.get_running_loop())
    finally:
        ACTIVE_JOBS -= 1


def is_fb_url(url):
    low = url.lower()
    return any(d in low for d in FB_DOMAINS)


def fb_big_variant(url):
    """ctp thumb param -> s2048 full (signature same thake, CDN boro dey)."""
    if 'ctp=' in url:
        return re.sub(r'ctp=[^&]*', 'ctp=s2048x2048', url)
    return url


def fb_attachment_nodes(page):
    """all_subattachments blocks: [(mediaset_token, declared, items)].
    items = [(fbid, viewer_uri)], viewer_image = signed full-size.
    Caller post-ID match kore thik block ney."""
    out = []
    for m in re.finditer(r'"all_subattachments"\s*:\s*\{\s*"count"\s*:\s*(\d+)\s*,\s*"nodes"\s*:\s*\[', page):
        try:
            total = int(m.group(1))
        except ValueError:
            continue
        # bracket balance kore nodes array sesh khujo
        depth, i, start = 0, m.end() - 1, m.end() - 1
        end = -1
        while i < len(page):
            c = page[i]
            if c == '[':
                depth += 1
            elif c == ']':
                depth -= 1
                if depth == 0:
                    end = i
                    break
            i += 1
        if end < 0:
            continue
        seg = page[start:end + 1]
        items = []
        for nm in re.finditer(
                r'"viewer_image"\s*:\s*\{[^}]*?"uri"\s*:\s*"([^"]+)"[^}]*?\}\s*,\s*"id"\s*:\s*"(\d+)"\s*,\s*"__isMedia"\s*:\s*"Photo"',
                seg):
            u = nm.group(1).replace('\\/', '/').replace('\\u0026', '&').replace('&amp;', '&')
            items.append((nm.group(2), u))
        tok = re.search(r'"mediaset_token"\s*:\s*"([^"]+)"', page[max(0, m.start() - 2000):m.start()])
        out.append((tok.group(1) if tok else "", total, items))
    return out


def fb_scoped_pairs(page):
    """WWW page theke SUDHU oi post-er photo (story_attachment-er kache + og cover).
    Onno post (115KB dure) + sticker bad jay. Returns [(big_url, orig_url)]."""
    full_by_id = {}
    for u in re.findall(r'https://scontent[^"\\\s]+?\.(?:jpg|png|webp)[^"\\\s]*', page):
        u = u.replace('\\/', '/').replace('&amp;', '&')
        m = re.search(r'(\d+_\d+_\d+_n)', u)
        if m and m.group(1) not in full_by_id:
            full_by_id[m.group(1)] = u
    marks = [m.start() for m in re.finditer('story_attachment', page)]
    scoped = []
    for m in re.finditer(r't39\.99422-6/(\d+_\d+_\d+_n)', page):
        pid = m.group(1)
        if pid in scoped:
            continue
        if marks and min(abs(m.start() - x) for x in marks) > 60000:
            continue
        scoped.append(pid)
    mo = re.search(r'property="og:image"[^>]*content="([^"]+)', page)
    if mo:
        mi = re.search(r'(\d+_\d+_\d+_n)', mo.group(1))
        if mi and mi.group(1) not in scoped:
            scoped.insert(0, mi.group(1))
    pairs = []
    for pid in scoped:
        u = full_by_id.get(pid)
        if not u:
            continue
        big = fb_big_variant(u)
        pairs.append((big, u) if big != u else (u, None))
    return pairs


def fb_unescape_url(url):
    """FB JSON-er escaped CDN url ke normal URL banay."""
    return (url.replace('\\/', '/')
            .replace('\\u0026', '&')
            .replace('\\u0025', '%')
            .replace('\\u003d', '=')
            .replace('&amp;', '&'))


def fb_parse_photo_page(html):
    """Photo viewer page theke full image url + porer photo id.
    Returns (image_url, next_id). next_id video holeo chain chole, save hoy na."""
    # script-er vitore JSON escaped thakte pare
    flat = html.replace('\\/', '/').replace('\\"', '"')
    url = ""
    for blob in (html, flat):
        um = re.search(r',"image":\{"uri":"([^"]+)"', blob)
        if not um:
            um = re.search(r'"viewer_image"\s*:\s*\{[^}]*?"uri"\s*:\s*"([^"]+)"', blob)
        if um:
            url = fb_unescape_url(um.group(1))
            break
    nxt = ""
    for blob in (html, flat):
        nm = re.search(r'"nextMediaAfterNodeId":\{"__typename":"Photo","id":"(\d+)"', blob)
        if not nm:
            nm = re.search(
                r'"nextMediaAfterNodeId":\{"__typename":"Video","id":"(\d+)"', blob)
        if nm:
            nxt = nm.group(1)
            break
    return url, nxt


def fb_walk_pcb(set_id, seed_ids, sess=None):
    """Post collage (pcb) er protita photo.
    HTML-e Facebook prothom ~5 ta embed kore, count-o 5 bole dite pare.
    Chain (nextMediaAfterNodeId) loop-e fire gele set sesh — declared count-e thambe na."""
    if not set_id or not str(set_id).startswith("pcb."):
        return []
    seeds = [s for s in seed_ids if s and str(s).isdigit()]
    if not seeds:
        return []
    if sess is None:
        sess = new_scraper()
    hdr = {
        'Accept': 'text/html,application/xhtml+xml',
        'User-Agent': TERA_UA,
        'Referer': 'https://www.facebook.com/',
    }
    cap = 100
    out, seen, have = [], set(), set()
    photo_id = seeds[0]
    misses = 0
    while photo_id and len(out) < cap and misses < 6:
        if photo_id in seen:
            break
        seen.add(photo_id)
        html = ""
        for purl in (
            f"https://www.facebook.com/photo/?fbid={photo_id}&set={set_id}",
            f"https://www.facebook.com/photo.php?fbid={photo_id}&set={set_id}",
        ):
            try:
                r = sess.get(purl, headers=hdr, timeout=30)
            except Exception as e:
                print(f" FB photo {photo_id} fail: {str(e)[:80]}", flush=True)
                continue
            if r.status_code == 200 and len(r.text) > 2000 and "nextMedia" in r.text:
                html = r.text
                break
        if not html:
            misses += 1
            print(f" FB photo {photo_id} page nai", flush=True)
            photo_id = next((s for s in seeds if s not in seen), "")
            continue
        misses = 0
        url, nxt = fb_parse_photo_page(html)
        # emoji / sticker (t39.1997) photo na
        if url and "scontent" in url and "/t39.1997" not in url:
            m = re.search(r"(\d+_\d+_\d+_n)", url)
            key = m.group(1) if m else photo_id
            if key not in have:
                have.add(key)
                out.append((key, url))
        if not nxt or nxt in seen:
            break
        photo_id = nxt
        time.sleep(0.25)
    print(f" FB set walk {set_id}: {len(out)} photos", flush=True)
    return out


def fb_candidate_videos(page):
    """playable_url (hd age) list."""
    vids = []
    for pat in (r'"playable_url_quality_hd"\s*:\s*"([^"]+)"',
                r'"playable_url"\s*:\s*"([^"]+)"',
                r'"browser_native_hd_url"\s*:\s*"([^"]+)"'):
        for v in re.findall(pat, page):
            vids.append(v.replace('\\/', '/').replace('&amp;', '&'))
    seen, uniq = set(), []
    for v in vids:
        if v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq[:5]


def blocking_facebook_download(url, workdir, byte_cb=None):
    """FB share/post link -> video thakle video, naile photo set. Returns target_dir.
    Raises ValueError(PAGE_FAIL/LOGIN/NO_PHOTO)."""
    sess = get_scraper()
    # render session-sticky — proti attempt-e fresh session-e alada render aste pare
    union, declared, page, postid = {}, 0, "", ""
    set_id, seed_ids, page_sess = "", [], None
    targets = [url]
    m0 = re.search(r'/(\d+)/posts/(\d+)', url)
    if m0:
        targets.append(f"https://m.facebook.com/{m0.group(1)}/posts/{m0.group(2)}/")
    for attempt in range(6):
        u = targets[attempt % len(targets)]
        try:
            cur = new_scraper()
            r = cur.get(u, timeout=30)
        except Exception:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code != 200 or len(r.text) < 5000:
            time.sleep(2 * (attempt + 1))
            continue
        if len(r.text) > len(page):
            page = r.text
            page_sess = cur
        if not postid:
            pm = re.search(r'/posts/(\d+)', r.url)
            if pm:
                postid = pm.group(1)
        try:
            blocks = fb_attachment_nodes(r.text)
        except Exception:
            blocks = []
        for tok, dec, items in blocks:
            if postid and postid not in tok and tok:
                continue  # onno post-er block skip (token chara block nirdosh)
            if dec > declared:
                declared = dec
            if not set_id and tok.startswith("pcb."):
                set_id = tok
            for fbid, uu in items:
                if str(fbid).isdigit() and fbid not in seed_ids:
                    seed_ids.append(fbid)
                m = re.search(r'(\d+_\d+_\d+_n)', uu)
                union.setdefault(m.group(1) if m else fbid, (uu, None))
        try:
            for big_u, orig_u in fb_scoped_pairs(r.text):
                m = re.search(r'(\d+_\d+_\d+_n)', orig_u or big_u)
                key = m.group(1) if m else big_u
                if key not in union:
                    union[key] = (big_u, orig_u)
        except Exception:
            pass
        print(f" FB nodes fetch {attempt + 1}: {len(union)}/{declared or '?'}", flush=True)
        # baki photo HTML-e nai. count 5 holeo set aro boro hote pare — walk-e pawa jabe.
        if set_id and seed_ids:
            break
        time.sleep(2)
    if not set_id and postid:
        set_id = f"pcb.{postid}"
    # declared count-e trust kora jay na: FB prothom 5 ta embed kore count-o 5 dite pare.
    # chain nijer loop-e thambe, tai post-er baki photo o ashe.
    if set_id.startswith("pcb.") and seed_ids:
        walked = fb_walk_pcb(set_id, seed_ids, page_sess)
        if len(walked) > len(union):
            union = {key: (url, None) for key, url in walked}
            if len(union) > declared:
                declared = len(union)
            print(f" FB set filled: {len(union)}/{declared}", flush=True)
    if not page:
        raise ValueError("PAGE_FAIL")
    low = page.lower()
    if ('login' in low and 'password' in low and 'scontent' not in low
            and 'playable_url' not in low):
        raise ValueError("LOGIN")

    target_dir = os.path.join(workdir, "facebook")
    os.makedirs(target_dir, exist_ok=True)
    dl_headers = {'Referer': 'https://www.facebook.com/', 'Accept': '*/*'}
    done_all, total_known = [0], [0]

    def fetch(u, prefix, i):
        try:
            d = sess.get(u, headers=dl_headers, timeout=60, stream=True)
            if d.status_code != 200:
                return None
            ctype = d.headers.get('Content-Type', '').lower().split(';')[0].strip()
            if 'text/html' in ctype:
                return None
            ext = '.mp4' if prefix == 'video' else '.jpg'
            if 'png' in ctype:
                ext = '.png'
            elif 'webp' in ctype:
                ext = '.webp'
            elif 'mp4' in ctype or 'video' in ctype:
                ext = '.mp4'
            out = os.path.join(target_dir, f"{i:03d}{ext}")
            wrote = 0
            with open(out, 'wb') as f:
                for chunk in d.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    if wrote + len(chunk) > 1900 * 1024 * 1024:
                        break
                    f.write(chunk)
                    wrote += len(chunk)
                    done_all[0] += len(chunk)
                    if byte_cb is not None:
                        try:
                            byte_cb(done_all[0], total_known[0])
                        except Exception:
                            pass
            if wrote < 20000:
                try:
                    os.remove(out)
                except OSError:
                    pass
                return None
            return out
        except Exception:
            return None

    # video post (photo set nai) hole video. multi-photo post-e onno video dhukbe na.
    if len(union) <= 1:
        for i, vu in enumerate(fb_candidate_videos(page), start=1):
            if fetch(vu, 'video', i):
                return target_dir, 1

    # 2. photo set: EXACT nodes union (viewer full-size) -> fallback scoped
    import hashlib
    seen_hash = set()
    pairs = [(b, o) for b, o in union.values()]
    print(f" FB nodes total: {len(pairs)}/{declared or '?'} photos", flush=True)
    if not pairs:
        pairs = fb_scoped_pairs(page)
        print(f" FB scoped fallback: {len(pairs)} photos", flush=True)
    if not pairs:
        # fallback: cover (og:image) only — puro page scrape NA (onno post dhukto)
        m = re.search(r'property="og:image"[^>]*content="([^"]+)', page)
        if m:
            u = m.group(1).replace('&amp;', '&')
            big = fb_big_variant(u)
            pairs.append((big, u) if big != u else (u, None))
    n = 0
    for big_u, orig_u in pairs:
        n += 1
        out = fetch(big_u, 'img', n)
        if not out and orig_u:
            out = fetch(orig_u, 'img', n)
        if not out:
            continue
        try:
            with open(out, 'rb') as f:
                h = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            continue
        if h in seen_hash:
            try:
                os.remove(out)
            except OSError:
                pass
            continue
        seen_hash.add(h)

    files = [p for p in Path(target_dir).rglob("*") if p.is_file()]
    if not files:
        raise ValueError("NO_PHOTO")
    print(f" FB final: {len(files)}/{declared or '?'} files", flush=True)
    return target_dir, declared


async def send_facebook(message, url: str):
    """FB link: video/photo sob direct document (zip na)."""
    loop = asyncio.get_running_loop()
    status = await message.reply_text("Fetching Facebook post...")
    workdir = tempfile.mkdtemp(prefix="tgdl_")
    try:
        try:
            target, declared = await asyncio.to_thread(
                blocking_facebook_download, url, workdir,
                make_byte_progress_cb(status, loop, "Downloading...", 0),
            )
        except ValueError as e:
            code = str(e)
            await status.edit_text({
                "PAGE_FAIL": "Facebook page khule nai. Link check koro.",
                "LOGIN": "Facebook login wall. Public post-er link dao.",
                "NO_PHOTO": "Ei post-e photo/video paini.",
            }.get(code, f"Error: {code[:200]}"))
            return
        files = sorted([p for p in Path(target).rglob("*") if p.is_file()])
        if not files:
            await status.edit_text("Kono file paini.")
            return
        short_note = ""
        if declared and len(files) < declared:
            short_note = f" ({len(files)}/{declared} found — cookies.txt for full)"
        await status.edit_text(f"{len(files)} ta file pathacchi (original)...")
        chat = message.chat
        kinds, sent = [], 0
        for i, fp in enumerate(files, start=1):
            try:
                kinds.append(await send_one_file(chat, fp, fp.name))
                sent += 1
            except Exception as e:
                kinds.append('failed')
                print(f"FB send fail {fp.name}: {str(e)[:120]}", flush=True)
            if i % 20 == 0:
                try:
                    await status.edit_text(f"{i}/{len(files)} sent...")
                except Exception:
                    pass
        summary = f"doc:{kinds.count('doc')} photo:{kinds.count('photo')} failed:{kinds.count('failed')}"
        print(f"FB send kinds: {summary}", flush=True)
        await status.edit_text(f"Done! {sent}/{len(files)} files sent ({summary}){short_note}.")
    except Exception as e:
        try:
            await status.edit_text(f"Error: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def is_instagram_url(url):
    return 'instagram.com' in url.lower()


def parse_ig_shortcode(url):
    m = re.search(r'instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)', url)
    return m.group(1) if m else None


def ig_best_block_url(block):
    """Candidates block theke sobcheye boro rendition (s1080 > p720 > base)."""
    urls = [u.replace('\\/', '/').replace('\\u0026', '&')
            for u in re.findall(r'"url"\s*:\s*"([^"]+)"', block)]
    if not urls:
        return None
    for pat in ('s1080x1080', 'p1080x1080', 'p720x720', 's750x750', 's640x640'):
        for u in urls:
            if pat in u:
                return u
    return urls[0]


def blocking_instagram_download(url, workdir, byte_cb=None):
    """IG post: photo/carousel -> s1080 originals. Returns (target_dir, is_video).
    Video post hole video file. Raises ValueError(PAGE_FAIL/LOGIN/NO_PHOTO)."""
    code = parse_ig_shortcode(url)
    if not code:
        raise ValueError("BAD_LINK")
    sess = get_scraper()
    try:
        r = sess.get(f'https://www.instagram.com/p/{code}/', timeout=40)
    except Exception:
        raise ValueError("PAGE_FAIL")
    if r.status_code != 200 or len(r.text) < 50000:
        raise ValueError("PAGE_FAIL")
    page = r.text

    target_dir = os.path.join(workdir, sanitize_folder_name(f"ig_{code}"))
    os.makedirs(target_dir, exist_ok=True)
    dl_headers = {'Referer': 'https://www.instagram.com/', 'Accept': '*/*'}
    done_all, total_known = [0], [0]

    def fetch(u, i, ext_hint='.jpg'):
        try:
            d = sess.get(u, headers=dl_headers, timeout=60, stream=True)
            if d.status_code != 200:
                return None
            ctype = d.headers.get('Content-Type', '').lower().split(';')[0].strip()
            if 'text/html' in ctype:
                return None
            ext = ext_hint
            if 'png' in ctype:
                ext = '.png'
            elif 'webp' in ctype:
                ext = '.webp'
            elif 'mp4' in ctype or 'video' in ctype:
                ext = '.mp4'
            out = os.path.join(target_dir, f"{i:03d}{ext}")
            wrote = 0
            with open(out, 'wb') as f:
                for chunk in d.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    if wrote + len(chunk) > 1900 * 1024 * 1024:
                        break
                    f.write(chunk)
                    wrote += len(chunk)
                    done_all[0] += len(chunk)
                    if byte_cb is not None:
                        try:
                            byte_cb(done_all[0], total_known[0])
                        except Exception:
                            pass
            if wrote < 10240:
                try:
                    os.remove(out)
                except OSError:
                    pass
                return None
            return out
        except Exception:
            return None

    # 1. photo/carousel blocks (profile-pic bad dite cover dup URL-dedupe)
    seen, wins = set(), []
    for b in re.findall(r'"candidates"\s*:\s*\[(.*?)\]', page):
        pick = ig_best_block_url(b)
        if pick and pick not in seen:
            seen.add(pick)
            wins.append(pick)
    if wins:
        import hashlib
        seen_hash = set()
        for i, u in enumerate(wins, start=1):
            out = fetch(u, i)
            if not out:
                continue
            try:
                with open(out, 'rb') as f:
                    h = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                continue
            if h in seen_hash:
                try:
                    os.remove(out)
                except OSError:
                    pass
                continue
            seen_hash.add(h)
        files = [p for p in Path(target_dir).rglob("*") if p.is_file()]
        if files:
            return target_dir, False
        # blocks chilo kintu download fail — video hote pare, niche try

    # 2. video post
    vids = sorted(set(
        u.replace('\\/', '/').replace('\\u0026', '&')
        for u in re.findall(r'"video_url"\s*:\s*"([^"]+)"', page)
    ))
    for i, vu in enumerate(vids[:3], start=1):
        if fetch(vu, i, '.mp4'):
            return target_dir, True

    files = [p for p in Path(target_dir).rglob("*") if p.is_file()]
    if not files:
        raise ValueError("NO_PHOTO")
    return target_dir, False


async def send_instagram(message, url: str):
    """IG post: photo/carousel direct document, video direct, na hole yt-dlp fallback."""
    loop = asyncio.get_running_loop()
    status = await message.reply_text("Fetching Instagram post...")
    workdir = tempfile.mkdtemp(prefix="tgdl_")
    try:
        try:
            target, is_video = await asyncio.to_thread(
                blocking_instagram_download, url, workdir,
                make_byte_progress_cb(status, loop, "Downloading...", 0),
            )
        except ValueError as e:
            code = str(e)
            if code == "BAD_LINK":
                await status.edit_text("Link bujha jayni. IG post/reel link dao.")
            elif code == "NO_PHOTO":
                # reels/login-wall: yt-dlp fallback
                await send_video(message, url)
            else:
                await status.edit_text("Instagram page khule nai. Link check koro.")
            return
        files = sorted([p for p in Path(target).rglob("*") if p.is_file()])
        if not files:
            await status.edit_text("Kono file paini.")
            return
        await status.edit_text(f"{len(files)} ta file pathacchi (original)...")
        chat = message.chat
        kinds, sent = [], 0
        for i, fp in enumerate(files, start=1):
            try:
                kinds.append(await send_one_file(chat, fp, fp.name))
                sent += 1
            except Exception as e:
                kinds.append('failed')
                print(f"IG send fail {fp.name}: {str(e)[:120]}", flush=True)
            if i % 20 == 0:
                try:
                    await status.edit_text(f"{i}/{len(files)} sent...")
                except Exception:
                    pass
        summary = f"doc:{kinds.count('doc')} photo:{kinds.count('photo')} failed:{kinds.count('failed')}"
        print(f"IG send kinds: {summary}", flush=True)
        await status.edit_text(f"Done! {sent}/{len(files)} files sent ({summary}).")
    except Exception as e:
        try:
            await status.edit_text(f"Error: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def direct_filename(url, resp):
    """Content-Disposition, naile URL tail theke filename."""
    cd = resp.headers.get('Content-Disposition', '') if resp is not None else ''
    if cd:
        m = re.search(r"filename\*\s*=\s*UTF-8''([^;\s]+)", cd, re.I)
        if not m:
            m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I)
        if m:
            fn = sanitize_file_name(unquote(m.group(1).strip()))
            if fn and fn != "image":
                return fn
    tail = unquote(url.rsplit('/', 1)[-1].split('?')[0].split('#')[0]).strip()
    tail = sanitize_file_name(tail)
    return tail if tail and tail != "image" and '.' in tail else ""


def blocking_direct_download(urls, workdir, byte_cb=None):
    """Direct file URL list namay. Webpage/oversize skip. Returns target_dir.
    Raises ValueError(NO_FILE) jodi kichui namano na jay."""
    target_dir = os.path.join(workdir, "direct")
    os.makedirs(target_dir, exist_ok=True)
    sess = requests.Session()
    sess.headers.update({'User-Agent': TERA_UA})
    done_all, lock, total_known = [0], threading.Lock(), [0]
    saved = []

    def fetch_one(url):
        try:
            r = sess.get(url, stream=True, timeout=30, allow_redirects=True)
            ctype = r.headers.get('Content-Type', '').lower().split(';')[0].strip()
            if 'text/html' in ctype or 'text/plain' in ctype and 'attachment' not in r.headers.get('Content-Disposition', '').lower():
                return
            try:
                length = int(r.headers.get('Content-Length') or 0)
            except (TypeError, ValueError):
                length = 0
            if length > 2000 * 1024 * 1024:
                return  # 2GB+ single file skip (Telegram limit)
            name = direct_filename(url, r)
            if not name:
                # first chunk dekhe html kina check
                it = r.iter_content(chunk_size=8192)
                try:
                    first = next(it)
                except StopIteration:
                    return
                if first.lstrip()[:15].lower().startswith((b'<html', b'<!doctype')):
                    return
                name = sanitize_file_name(f"file_{abs(hash(url)) % 100000}.bin")
                chunks = [first]
            else:
                chunks = []
            out = os.path.join(target_dir, name)
            if os.path.exists(out):
                stem, dot, ext = name.rpartition('.')
                k = 1
                while os.path.exists(out):
                    out = os.path.join(target_dir, f"{stem}({k}){dot}{ext}" if dot else f"{name}({k})")
                    k += 1
            if length:
                with lock:
                    total_known[0] += length
            wrote = 0
            with open(out, 'wb') as f:
                for c in chunks:
                    f.write(c)
                    wrote += len(c)
                for chunk in r.iter_content(chunk_size=1024 * 256):
                    if not chunk:
                        continue
                    if wrote + len(chunk) > 2000 * 1024 * 1024:
                        break
                    f.write(chunk)
                    wrote += len(chunk)
                    with lock:
                        done_all[0] += len(chunk)
                        cur = done_all[0]
                    if byte_cb is not None:
                        try:
                            byte_cb(cur, total_known[0])
                        except Exception:
                            pass
            if wrote < 1024:
                try:
                    os.remove(out)
                except OSError:
                    pass
                return
            saved.append(out)
        except Exception:
            return
        finally:
            try:
                r.close()
            except Exception:
                pass

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(fetch_one, urls))

    if not saved:
        raise ValueError("NO_FILE")
    return target_dir


async def send_direct(message, urls):
    """Jekono direct file link: 1 ta hole document, onekgula hole zip."""
    loop = asyncio.get_running_loop()
    status = await message.reply_text("Checking link...")
    workdir = tempfile.mkdtemp(prefix="tgdl_")
    try:
        target = await asyncio.to_thread(
            blocking_direct_download, urls, workdir,
            make_byte_progress_cb(status, loop, "Downloading...", 0),
        )
        files = sorted([p for p in Path(target).rglob("*") if p.is_file()])
        if len(files) == 1:
            fp = files[0]
            size_mb = os.path.getsize(fp) / 1048576
            if size_mb > 2000:
                await status.edit_text(f"File too big ({size_mb:.0f}MB) — Telegram max 2GB.")
                return
            await status.edit_text(f"Uploading {fp.name} ({size_mb:.1f}MB)...")
            k = await send_one_file(message.chat, fp, fp.name)
            print(f"Direct send kind: {k} ({fp.name})", flush=True)
            await status.edit_text(f"Done! {fp.name} ({size_mb:.1f}MB).")
            return
        total = 0
        for p in files:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        await zip_and_send(status, message.chat, target, total)
    except ValueError as e:
        if str(e) == "NO_FILE":
            await status.edit_text("Direct file paini. Webpage link support kore na.")
        else:
            await status.edit_text(f"Error: {str(e)[:200]}")
    except Exception as e:
        try:
            await status.edit_text(f"Error: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


BOT_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("status", "Bot status"),
    BotCommand("ping", "Check latency"),
    BotCommand("help", "How to use"),
]


async def post_init(app):
    """Start-er somoy command menu Telegram-e register kore."""
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        print(f"set_my_commands fail: {e}")


def clean_stale_workdirs():
    """Ag-er crash-e volume-e kono tgdl_* pore thakle start-ei delete."""
    try:
        tmp = tempfile.gettempdir()
        for name in os.listdir(tmp):
            if name.startswith("tgdl_"):
                shutil.rmtree(os.path.join(tmp, name), ignore_errors=True)
    except OSError:
        pass


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN .env-e bosao. (.env.example dekho)")
    clean_stale_workdirs()
    builder = ApplicationBuilder().token(BOT_TOKEN)
    # 2GB porjonto file-e maximum timeout — slow VPS upload-eo "Timed out" hobe na.
    builder = builder.connect_timeout(120).read_timeout(3600).write_timeout(7200).pool_timeout(120)
    builder = builder.post_init(post_init)
    if API_BASE_URL:
        builder = builder.base_url(API_BASE_URL)
    if API_BASE_FILE_URL:
        builder = builder.base_file_url(API_BASE_FILE_URL)
    app = builder.build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("ping", ping_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(on_quality_choice, pattern=r"^q:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_listener))
    print(f"Bot running... (MAX_ZIP_MB={MAX_ZIP_MB})")
    app.run_polling()


if __name__ == "__main__":
    main()
