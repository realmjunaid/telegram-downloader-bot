import os
import re
import time
import glob
import random
import threading
from urllib.parse import urljoin, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
import cloudscraper
from bs4 import BeautifulSoup

# ---- Speed settings ----
MAX_WORKERS = 5  # 3 = safe, 5 = fast, 8+ = ban risk
PAGE_RETRIES = 4
IMG_RETRIES = 4

# resume-e sob media type check hobe (age mp4/webm chilo na)
MEDIA_EXTS = ("jpg", "jpeg", "png", "webp", "gif", "avif", "mp4", "webm", "m4v", "mov")

_thread_local = threading.local()
_print_lock = threading.Lock()

try:
    # Windows console cp1252/codepage-e emoji print-e crash korto (UnicodeEncodeError)
    import sys as _sys
    if hasattr(_sys.stdout, 'reconfigure'):
        _sys.stdout.reconfigure(errors='replace')
    if hasattr(_sys.stderr, 'reconfigure'):
        _sys.stderr.reconfigure(errors='replace')
except Exception:
    pass


def _thread_log(msg):
    with _print_lock:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            print(str(msg).encode('ascii', 'replace').decode('ascii'), flush=True)


def get_scraper():
    # proti thread-e alada session: fast + thread-safe
    if not hasattr(_thread_local, "scraper"):
        _thread_local.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
        )
    return _thread_local.scraper


# Cloudflare Bypass & Browser Spoofing (main thread-er jonno)
scraper = cloudscraper.create_scraper(
    browser={
        'browser': 'chrome',
        'platform': 'windows',
        'desktop': True
    }
)

# Custom HTTP Headers
headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

# NOTE: Ei script pure HTTP (cloudscraper) diye download kore.
# Ekhane kono browser window / screenshot / GUI automation nai.
# Tai terminal minimize ba screen off er sathe scraping-er direct somporko nai.
# Asol problem: screen off hole Windows WiFi/power-saving er karone
# network-e choto hiccup hoy, ar ager code-e kono retry chilo na —
# 1 bar fail korlei image miss hoye jeto. Tai mone hoto
# "minimize korle fail, open korle kaj kore".
# Fix: retry + resume + sleep-prevent, jate background-e cholleo fail na hoy.


def prevent_sleep_start():
    """Windows ke sleep-e jete badha dey download chola obosthay."""
    try:
        import ctypes
        # ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x00000001)
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    except Exception:
        pass


def prevent_sleep_stop():
    """Ager power state-e firiye dey."""
    try:
        import ctypes
        # ES_CONTINUOUS only = normal behaviour restore
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    except Exception:
        pass


def safe_get(url, timeout=30, retries=4, stream=False, extra_headers=None, quiet=False):
    """Transient network error hole fast retry kore. Thread-safe."""
    last_err = None
    h = dict(headers)
    if extra_headers:
        h.update(extra_headers)
    sess = get_scraper()
    for attempt in range(1, retries + 1):
        try:
            res = sess.get(url, headers=h, timeout=timeout, stream=stream)
            if res.status_code == 200:
                return res
            last_err = f"HTTP {res.status_code}"
        except Exception as e:
            last_err = str(e)[:120]
        if not quiet:
            _thread_log(f"   Retry {attempt}/{retries} ({last_err})")
        # fast backoff: 1s, 2s, 3s... age chilo 2,4,8,16s (etai slow korto)
        if attempt < retries:
            time.sleep(attempt * 0.8 + random.uniform(0, 0.5))
    if not quiet:
        _thread_log(f"   Sob retry fail: {last_err}")
    return None

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


def extract_original_filename(soup, media_url, resp):
    """Website-e dekha hubuhu original filename ber kore.
    Priority: page text (NAME :: WxH) > URL tail > Content-Disposition.
    """
    # 1. /s/ page-er "#i2 div" e thake: "NAME.webp :: 1080 x 608 :: 5.27 MiB"
    for div in soup.select('#i2 div, #i4 div'):
        txt = div.get_text(strip=True)
        if '::' in txt:
            first = txt.split('::')[0].strip()
            if '.' in first and len(first) < 200:
                return first
    # 2. hath media URL-er seshe asol filename thake: .../xres=org/NAME.webp
    tail = unquote(media_url.rsplit('/', 1)[-1].split('?')[0].split(';')[0])
    if '.' in tail and len(tail) < 200 and not tail.startswith('keystamp'):
        m = re.search(r'([^;=]+\.(webp|gif|png|jpe?g|avif|mp4|webm|m4v|mov))$', tail, re.I)
        if m:
            return m.group(1)
    # 3. Content-Disposition
    cd = resp.headers.get('Content-Disposition', '') if resp is not None else ''
    if cd:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\s]+)"?', cd, re.I)
        if m:
            return unquote(m.group(1).strip().strip('"'))
    return None


def already_downloaded(target_dir, i):
    """001.* ba '001 - *.ext' — jekono format-e thaklei resume (website sorting same thakbe)."""
    prefix_num = f"{i:03d}"
    # purono format: 001.jpg
    for e in MEDIA_EXTS:
        p = os.path.join(target_dir, f"{prefix_num}.{e}")
        if os.path.exists(p) and os.path.getsize(p) > 10240:
            return True
    # notun format: 001 - original.webp
    for p in glob.glob(os.path.join(target_dir, f"{prefix_num} - .*")):
        try:
            if os.path.getsize(p) > 10240:
                return True
        except OSError:
            pass
    return False


def extract_media_url(soup, page_url):
    """Page theke asol media URL ber kore.

    Priority:
    1. fullimg.php (original file - animated webp/gif/mp4 ekhanei thake, animation preserve kore)
    2. <video>/<source> tag (kokhono mp4/webm direct dey)
    3. mp4/webm link
    4. <img id="img"> (animated webp/gif hole etai animated file)
    Age sudhu 1+4 check hoto, video tag ignore hoto bole
    video-type poster sudhu static frame hoye namto.
    """
    # 1. Download original (RAW - animation/video preserve kore)
    original_a = soup.find('a', href=re.compile(r'fullimg\.php'))
    if original_a and original_a.get('href'):
        return urljoin(page_url, original_a['href']), 'original'

    # 2. video tag
    video = soup.find('video')
    if video is not None:
        if video.get('src'):
            return urljoin(page_url, video['src']), 'video'
        source = video.find('source')
        if source is not None and source.get('src'):
            return urljoin(page_url, source['src']), 'video'

    source = soup.find('source', src=True)
    if source is not None:
        return urljoin(page_url, source['src']), 'video'

    # 3. direct video link
    for a in soup.find_all('a', href=True):
        if re.search(r'\.(mp4|webm|m4v|mov)(\?|;|$)', a['href'], re.I):
            return urljoin(page_url, a['href']), 'video'

    # 4. standard viewer image (animated webp/gif hole etai animated file)
    img_tag = soup.find('img', id='img')
    if img_tag is not None and img_tag.get('src'):
        return urljoin(page_url, img_tag['src']), 'image'

    # 5. fallback: #i3 box-er ভেতর যেকোনো media
    box = soup.find('div', id='i3')
    if box is not None:
        v = box.find('video') or box.find('source')
        if v is not None and v.get('src'):
            return urljoin(page_url, v['src']), 'video'
        im = box.find('img')
        if im is not None and im.get('src'):
            return urljoin(page_url, im['src']), 'image'

    return None, None


def decide_ext(media_url, resp):
    """Content-Type + Content-Disposition + URL diye sothik extension."""
    # 1. Content-Disposition: filename="....webp" / "...mp4"
    cd = resp.headers.get('Content-Disposition', '')
    if cd:
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\s]+)"?', cd, re.I)
        if m:
            fn = m.group(1).strip().strip('"')
            if '.' in fn:
                e = fn.rsplit('.', 1)[-1].lower().split('?')[0].split(';')[0]
                if 2 <= len(e) <= 4 and e.isalnum():
                    return {'jpeg': 'jpg'}.get(e, e)

    # 2. Content-Type (sobcheye reliable - url-te ext na thakleo kaj kore)
    ctype = resp.headers.get('Content-Type', '').lower().split(';')[0].strip()
    ctype_map = {
        'image/webp': 'webp',
        'image/gif': 'gif',
        'image/png': 'png',
        'image/jpeg': 'jpg',
        'image/jpg': 'jpg',
        'image/avif': 'avif',
        'video/mp4': 'mp4',
        'video/webm': 'webm',
        'video/quicktime': 'mov',
        'video/x-m4v': 'm4v',
        'application/octet-stream': None,  # niche URL theke ber koro
    }
    if ctype in ctype_map and ctype_map[ctype]:
        return ctype_map[ctype]

    # 3. URL-er seshe asol filename (hath URL-e .../xres=org/NAME.webp thake)
    tail = media_url.rsplit('/', 1)[-1].lower()
    m2 = re.search(r'\.(webp|gif|png|jpe?g|avif|mp4|webm|m4v|mov)(?:[?;]|$)', tail)
    if m2:
        e = m2.group(1)
        return 'jpg' if e == 'jpeg' else e

    # 4. fullimg.php?id=...&...&ext=webp type query
    m3 = re.search(r'ext=(webp|gif|png|jpe?g|mp4|webm)', media_url.lower())
    if m3:
        e = m3.group(1)
        return 'jpg' if e == 'jpeg' else e

    return 'mp4' if 'video' in ctype else 'jpg'

def download_eh_gallery(gallery_url, save_path="."):
    print(f" Scanning gallery: {gallery_url}")
    
    title = "EH_Gallery"
    page_urls = []
    current_url = gallery_url

    # 1. Scan all gallery pages
    while current_url:
        res = safe_get(current_url, timeout=30, retries=PAGE_RETRIES)
        if res is None:
            print(f" Failed to access page after retry: {current_url}")
            break

        soup = BeautifulSoup(res.text, 'html.parser')

        # Extract gallery title
        if title == "EH_Gallery":
            title_tag = soup.find('h1', id='gn') or soup.find('h1', id='gj')
            if title_tag and title_tag.text.strip():
                title = sanitize_folder_name(title_tag.text)

        # Find all thumbnail image links (/s/)
        anchors = soup.find_all('a', href=re.compile(r'/s/[a-f0-9]+/\d+-\d+'))
        for a in anchors:
            href = a['href']
            if href not in page_urls:
                page_urls.append(href)

        # Pagination check
        next_page = None
        ptt_table = soup.find('table', class_='ptt')
        if ptt_table:
            all_tds = ptt_table.find_all('td')
            if all_tds:
                last_a = all_tds[-1].find('a')
                if last_a and 'href' in last_a.attrs:
                    next_url = last_a['href']
                    if next_url != current_url:
                        next_page = next_url

        current_url = next_page
        # gallery list page scan fast - boro delay dorkar nai
        time.sleep(random.uniform(0.2, 0.5))

    if not page_urls:
        print(" No images found. The layout changed or Cloudflare blocked the request.")
        return None

    # 2. Create output directory
    target_dir = os.path.join(save_path, title)
    os.makedirs(target_dir, exist_ok=True)
    total_images = len(page_urls)

    print(f" Target Folder: {os.path.abspath(target_dir)}")
    print(f" Found {total_images} images. Downloading RAW with {MAX_WORKERS} parallel workers...\n")

    def download_one(i, page_url):
        # Resume support (purono 001.ext + notun '001 - name.ext' duitai)
        if already_downloaded(target_dir, i):
            return f"[{i}/{total_images}]  Skipped (exists)."
        try:
            r = safe_get(page_url, timeout=30, retries=PAGE_RETRIES, quiet=True)
            if r is None:
                return f"[{i}/{total_images}]  Page fail: {page_url}"
            s = BeautifulSoup(r.text, 'html.parser')

            media_url, kind = extract_media_url(s, page_url)

            if not media_url:
                return f"[{i}/{total_images}]  Image/video link not found."

            img_res = safe_get(
                media_url, timeout=60, retries=IMG_RETRIES, quiet=True,
                extra_headers={'Referer': page_url,
                               'Accept': '*/*'},
            )
            if img_res is None:
                return f"[{i}/{total_images}]  Media fail after retry."

            img_data = img_res.content
            if len(img_data) < 10240:
                return f"[{i}/{total_images}]  Too small ({len(img_data)}b)."

            ext = decide_ext(media_url, img_res)

            # Original title + sorting: '001 - NAME.webp'
            # number prefix thakay local folder-e website-er hubuhu order thake,
            # ar pichone original title thakay ki file bujha jay.
            orig = extract_original_filename(s, media_url, img_res)
            if orig:
                stem = orig.rpartition('.')[0] if '.' in orig else orig
                stem = sanitize_file_name(stem)
                filename = os.path.join(target_dir, f"{i:03d} - {stem}.{ext}")
            else:
                filename = os.path.join(target_dir, f"{i:03d}.{ext}")
            tmpfile = filename + ".tmp"
            with open(tmpfile, 'wb') as f:
                f.write(img_data)
            os.replace(tmpfile, filename)
            label = ' video' if ext in ('mp4', 'webm', 'm4v', 'mov') else (' animated' if ext in ('webp', 'gif') else ' image')
            return f"[{i}/{total_images}]  {label} {os.path.basename(filename)} ({len(img_data)//1024} KB)"
        except Exception as e:
            return f"[{i}/{total_images}]  {str(e)[:100]}"

    prevent_sleep_start()
    try:
        done = 0
        t0 = time.time()
        # ThreadPool: 1 ta 1 ta kore na namiye 5 ta eksathe namabe -> ~4-5x fast
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futures = {ex.submit(download_one, i, u): i for i, u in enumerate(page_urls, start=1)}
            for fut in as_completed(futures):
                _thread_log(fut.result())
                done += 1
                if done % 10 == 0:
                    speed = done / max(time.time() - t0, 1)
                    _thread_log(f"--- Progress: {done}/{total_images} ({speed:.1f} img/s) ---")
    finally:
        prevent_sleep_stop()

    print(f"\n RAW Download Complete! Saved in: {os.path.abspath(target_dir)}")
    return os.path.abspath(target_dir)


def parse_pawchive_url(url):
    """https://pawchive.pw/{service}/user/{user}/post/{post} -> (service, user, post)"""
    m = re.search(r'pawchive\.pw/([^/]+)/user/([^/]+)/post/([^/?#]+)', url)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None, None, None


def download_pawchive_post(post_url, save_path="."):
    """Pawchive post-er sob attachment original filename soho download kore.

    API: /api/v1/{service}/user/{user}/post/{post} theke
    asol filename (1M.png, ...) + hash path pay.
    has_full=True hole /data/ (original), na hole /thumbnail/data/ (preview)
    theke namay — site-ei full file na thakle original deya somvob na.
    """
    service, user, post_id = parse_pawchive_url(post_url)
    if not post_id:
        print(" Pawchive post URL bujha jayni. Example: https://pawchive.pw/patreon/user/72639416/post/139203021")
        return None

    api_url = f"https://pawchive.pw/api/v1/{service}/user/{user}/post/{post_id}"
    print(f" Pawchive API: {api_url}")
    res = safe_get(api_url, timeout=30, retries=PAGE_RETRIES,
                   extra_headers={'Accept': 'application/json'})
    if res is None:
        print(" API theke post info pelam na.")
        return None
    try:
        post = res.json()
    except Exception:
        print(" API response JSON na.")
        return None

    title = (post.get('title') or f"pawchive_{post_id}").strip()
    author = post.get('author') or post.get('user') or user
    # author API-te object hote pare
    if isinstance(author, dict):
        author = author.get('name') or user
    folder = sanitize_folder_name(f"{author} - {title} [{service} {post_id}] (Patreon)" if service == 'patreon' else f"{author} - {title} [{service} {post_id}]")
    target_dir = os.path.join(save_path, folder)
    os.makedirs(target_dir, exist_ok=True)

    # file + attachments, path diye dedupe
    items, seen = [], set()
    main = post.get('file') or {}
    for a in ([main] if main.get('path') else []) + (post.get('attachments') or []):
        name, path = (a.get('name') or '').strip(), a.get('path') or ''
        if path and path not in seen and name:
            seen.add(path)
            items.append((name, path))

    if not items:
        print(" Ei post-e kono file/attachment nai.")
        return None

    has_full = post.get('has_full')
    base = 'https://img.pawchive.pw/data' if has_full else 'https://img.pawchive.pw/thumbnail/data'
    if has_full:
        print(f" Full-res archived ({len(items)} files).")
    else:
        print(" Ei post ekhono archive hoyni (has_full=false) — site-e original nai,")
        print("   tai preview quality nambe. Pore import hole abar chalale full-res pabe.")

    print(f" Target Folder: {os.path.abspath(target_dir)}")

    def download_one(i, name, path):
        safe_name = sanitize_file_name(name)
        # preview mode-e archive file-er (rar/zip/psd) kono thumbnail thake na
        if not has_full and safe_name.lower().endswith(('.rar', '.zip', '.7z', '.psd', '.psb', '.clip', '.sai', '.bin')):
            return f"[{i}/{len(items)}]  Skipped (archive, full-res import hole pabe): {safe_name}"
        out = os.path.join(target_dir, f"{i:03d} - {safe_name}")
        if os.path.exists(out) and os.path.getsize(out) > 10240:
            return f"[{i}/{len(items)}]  Skipped (exists)."
        # purono numeric-only format thakleo skip
        if already_downloaded(target_dir, i):
            return f"[{i}/{len(items)}]  Skipped (exists)."
        url = base + path if path.startswith('/') else base + '/' + path
        r = safe_get(url, timeout=60, retries=IMG_RETRIES, quiet=True,
                     extra_headers={'Referer': post_url, 'Accept': '*/*'})
        if r is None and has_full:
            # kokhono flag thakleo file missing thake — preview fallback
            url = 'https://img.pawchive.pw/thumbnail/data' + path
            r = safe_get(url, timeout=60, retries=IMG_RETRIES, quiet=True,
                         extra_headers={'Referer': post_url, 'Accept': '*/*'})
        if r is None:
            return f"[{i}/{len(items)}]  Fail: {name}"
        data = r.content
        if len(data) < 10240:
            return f"[{i}/{len(items)}]  Too small ({len(data)}b): {name}"
        tmp = out + ".tmp"
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, out)
        tag = ' (preview)' if (not has_full or '/thumbnail/' in url) else ''
        return f"[{i}/{len(items)}]  {os.path.basename(out)}{tag} ({len(data)//1024} KB)"

    prevent_sleep_start()
    try:
        done, t0 = 0, time.time()
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(download_one, i, n, p): i for i, (n, p) in enumerate(items, start=1)}
            for fut in as_completed(futs):
                _thread_log(fut.result())
                done += 1
                if done % 10 == 0:
                    _thread_log(f"--- Progress: {done}/{len(items)} ({done/max(time.time()-t0,1):.1f} file/s) ---")
    finally:
        prevent_sleep_stop()

    print(f"\n Pawchive Download Complete! Saved in: {os.path.abspath(target_dir)}")
    return os.path.abspath(target_dir)


def download_any(url, save_path="."):
    """URL dekhe site chine sothik downloader-e pathay. Returns target_dir or None."""
    if 'pawchive.pw' in url:
        return download_pawchive_post(url, save_path=save_path)
    else:
        return download_eh_gallery(url, save_path=save_path)

# ================= BOT =================
import asyncio
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# Fixed 100MB per zip (local Bot API server, 2GB porjonto support kore).
MAX_ZIP_MB = 100
ALLOWED_IDS = os.getenv("ALLOWED_IDS", "").strip()  # optional: "123,456" — khali thakle sobai use korte parbe
# Local Bot API server use korle (docker-compose): http://botapi:8081
API_BASE_URL = os.getenv("TELEGRAM_API_BASE_URL", "").strip()
API_BASE_FILE_URL = os.getenv("TELEGRAM_API_BASE_FILE_URL", "").strip()

URL_RE = re.compile(r'https?://[^\s]+')

# link dile sathe sathe download na kore choice button dekhai.
# key: short id -> {"url": ..., "user_id": ...}
PENDING = {}


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
    target = download_any(url, save_path=workdir)
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
    await ask_mode(update.message, url, update.effective_user.id)


async def ask_mode(message, url: str, user_id: int):
    """Link pele download na kore Files/Zip button dekhay."""
    key = uuid.uuid4().hex[:8]
    PENDING[key] = {"url": url, "user_id": user_id}
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Files", callback_data=f"files:{key}"),
            InlineKeyboardButton("Zip", callback_data=f"zip:{key}"),
        ]
    ])
    await message.reply_text(
        "Kivabe dibo?",
        reply_markup=kb,
    )


async def on_mode_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        mode, key = q.data.split(":", 1)
    except ValueError:
        return
    item = PENDING.pop(key, None)
    if not item:
        await q.edit_message_text("Expired — link ta abar pathao.")
        return
    if q.from_user.id != item["user_id"] and not is_allowed(q.from_user.id):
        await q.edit_message_text("Unauthorized.")
        return
    if mode == "files":
        await send_files_direct(q, item["url"])
    else:
        await send_as_zip(q, item["url"])


def _downloaded_files(target: str):
    return sorted(
        [p for p in Path(target).rglob("*") if p.is_file() and not p.name.endswith(".tmp")]
    )


async def send_files_direct(q, url: str):
    """Original quality-te protita image file akare pathay (no zip)."""
    status = await q.edit_message_text("Downloading... (eta boro gallery hole somoy lagbe)")
    # CallbackQuery-er edit kora message-ke status hisebe use kori
    workdir = tempfile.mkdtemp(prefix="tgdl_")
    try:
        target, total = await asyncio.to_thread(blocking_download, url, workdir)
        if not target:
            await status.edit_text("Download fail. Link / Cloudflare check koro.")
            return
        files = _downloaded_files(target)
        if not files:
            await status.edit_text("Kono file paini.")
            return
        base = os.path.basename(target.rstrip(os.sep))
        await status.edit_text(f"{len(files)} ta file pathacchi (original quality)...")
        chat = q.message.chat
        for i, fp in enumerate(files, start=1):
            with open(fp, "rb") as fh:
                await chat.send_document(
                    document=fh,
                    filename=fp.name,
                    caption=f"{base} — {i}/{len(files)}",
                    read_timeout=3600,
                    write_timeout=7200,
                    connect_timeout=120,
                    pool_timeout=120,
                )
        await status.edit_text(f"Done! {len(files)} files sent.")
    except Exception as e:
        try:
            await status.edit_text(f"Error: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        # images + temp sob permanently delete, nahole VPS full hobe
        shutil.rmtree(workdir, ignore_errors=True)


async def send_as_zip(q, url: str):
    status = await q.edit_message_text("Downloading... (eta boro gallery hole somoy lagbe)")
    workdir = tempfile.mkdtemp(prefix="tgdl_")
    try:
        target, total = await asyncio.to_thread(blocking_download, url, workdir)

        if not target:
            await status.edit_text("Download fail. Link / Cloudflare check koro.")
            return

        n_files = sum(1 for _ in Path(target).rglob("*") if _.is_file())
        if n_files == 0:
            await status.edit_text("Kono file paini.")
            return

        await status.edit_text(
            f"{n_files} files ({total/1048576:.1f}MB) — {MAX_ZIP_MB:.0f}MB chunk e zip hocche..."
        )

        zipdir = os.path.join(workdir, "_zips")
        os.makedirs(zipdir, exist_ok=True)
        base = os.path.basename(target.rstrip(os.sep))
        max_bytes = int(MAX_ZIP_MB * 1024 * 1024)

        zips = await asyncio.to_thread(make_zip_parts, target, zipdir, base, max_bytes)
        if not zips:
            await status.edit_text("Zip banano jayni.")
            return

        await status.edit_text(f"{len(zips)} ta zip pathacchi...")
        chat = q.message.chat
        for i, zp in enumerate(zips, start=1):
            size_mb = os.path.getsize(zp) / 1048576
            with open(zp, "rb") as fh:
                await chat.send_document(
                    document=fh,
                    filename=os.path.basename(zp),
                    caption=f"{base} — part {i}/{len(zips)} ({size_mb:.1f}MB)",
                    read_timeout=3600,
                    write_timeout=7200,
                    connect_timeout=120,
                    pool_timeout=120,
                )

        await status.edit_text(f"Done! {n_files} files, {len(zips)} zip.")
    except Exception as e:
        try:
            await status.edit_text(f"Error: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        # sob temp file (downloaded images + zips) permanently delete,
        # nahole VPS storage full hoye jabe
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN .env-e bosao. (.env.example dekho)")
    builder = ApplicationBuilder().token(BOT_TOKEN)
    # 2GB porjonto file-e maximum timeout — slow VPS upload-eo "Timed out" hobe na.
    builder = builder.connect_timeout(120).read_timeout(3600).write_timeout(7200).pool_timeout(120)
    if API_BASE_URL:
        builder = builder.base_url(API_BASE_URL)
    if API_BASE_FILE_URL:
        builder = builder.base_file_url(API_BASE_FILE_URL)
    app = builder.build()
    app.add_handler(CallbackQueryHandler(on_mode_choice, pattern=r"^(files|zip):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_listener))
    print(f"Bot running... (MAX_ZIP_MB={MAX_ZIP_MB})")
    app.run_polling()


if __name__ == "__main__":
    main()
