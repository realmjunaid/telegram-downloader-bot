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


def is_html_page(resp, data: bytes) -> bool:
    """Cloudflare block / error page (HTML) media bole vul kore save na hoy."""
    try:
        ctype = (resp.headers.get('Content-Type', '') if resp is not None else '').lower()
    except Exception:
        ctype = ''
    if 'text/html' in ctype or 'text/plain' in ctype:
        return True
    head = data.lstrip()[:200].lower()
    return head.startswith(b'<') and (b'<html' in head or b'<!doctype' in head)


def fix_ext_by_magic(data: bytes, ext: str) -> str:
    """Bytes untouched rekhe sudhu extension content-er sathe milay.
    Ext vul hole kichu client file khulte pare na (tap-e kichu hoyna)."""
    if data[:3] == b'\xff\xd8\xff':
        real = 'jpg'
    elif data[:8] == b'\x89PNG\r\n\x1a\n':
        real = 'png'
    elif data[:6] in (b'GIF87a', b'GIF89a'):
        real = 'gif'
    elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        real = 'webp'
    elif data[:4] == b'\x1a\x45\xdf\xa3':
        real = 'webm'
    elif len(data) > 12 and data[4:8] == b'ftyp':
        real = 'mp4'
    else:
        return ext  # unknown (zip/rar/psd/...) — jemon ache temon
    if real == ext or (real == 'jpg' and ext in ('jpg', 'jpeg')):
        return ext
    return real

def parse_gallery_meta(soup):
    """e-hentai gallery page theke file count + total size ('61 images (145.4 MB)') ber kore."""
    text = soup.get_text(" ", strip=True)
    count = size = None
    m = re.search(r'(\d[\d,]*)\s*images?', text)
    if m:
        try:
            count = int(m.group(1).replace(',', ''))
        except ValueError:
            count = None
    m2 = re.search(r'\(?\s*([\d.]+)\s*(KB|MB|GB|TB)\s*\)?', text, re.I)
    if m2:
        try:
            mult = {'kb': 1024, 'mb': 1024 ** 2, 'gb': 1024 ** 3, 'tb': 1024 ** 4}
            size = int(float(m2.group(1)) * mult[m2.group(2).lower()])
        except (ValueError, KeyError):
            size = None
    return count, size


def scan_eh_gallery(gallery_url):
    """Sudhu gallery page scan kore title, page_urls, count + estimated size.
    Kono image download hoy na."""
    title = "EH_Gallery"
    page_urls = []
    count = size = None
    current_url = gallery_url

    while current_url:
        res = safe_get(current_url, timeout=30, retries=PAGE_RETRIES, quiet=True)
        if res is None:
            print(" Failed to access page after retry")
            break
        soup = BeautifulSoup(res.text, 'html.parser')

        if title == "EH_Gallery":
            title_tag = soup.find('h1', id='gn') or soup.find('h1', id='gj')
            if title_tag and title_tag.text.strip():
                title = sanitize_folder_name(title_tag.text)

        if count is None or size is None:
            c, s = parse_gallery_meta(soup)
            count = count if count is not None else c
            size = size if size is not None else s

        for a in soup.find_all('a', href=re.compile(r'/s/[a-f0-9]+/\d+-\d+')):
            if a['href'] not in page_urls:
                page_urls.append(a['href'])

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
        time.sleep(random.uniform(0.2, 0.5))

    if count is None:
        count = len(page_urls)
    return {'title': title, 'page_urls': page_urls, 'count': count, 'est_bytes': size}


def download_eh_gallery(gallery_url, save_path=".", progress_cb=None, prescan=None):
    print(f" Scanning gallery: {gallery_url}")

    if prescan and prescan.get('page_urls'):
        title = prescan['title']
        page_urls = prescan['page_urls']
    else:
        scan = scan_eh_gallery(gallery_url)
        title = scan['title']
        page_urls = scan['page_urls']

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

            # Cloudflare error page (HTML) image nam-e save hole Telegram-e
            # blank/okl file jeto — tai HTML content reject.
            if is_html_page(img_res, img_data):
                return f"[{i}/{total_images}]  Skipped (HTML, not media)."

            ext = fix_ext_by_magic(img_data, decide_ext(media_url, img_res))

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
                if progress_cb is not None:
                    try:
                        progress_cb(done, total_images)
                    except Exception:
                        pass
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


def scan_pawchive_post(post_url):
    """Pawchive API theke sudhu post info (title, file count, estimated size) — download chara."""
    service, user, post_id = parse_pawchive_url(post_url)
    if not post_id:
        print(" Pawchive post URL bujha jayni.")
        return None

    api_url = f"https://pawchive.pw/api/v1/{service}/user/{user}/post/{post_id}"
    print(f" Pawchive API: {api_url}")
    res = safe_get(api_url, timeout=30, retries=PAGE_RETRIES, quiet=True,
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
    if isinstance(author, dict):
        author = author.get('name') or user
    folder = sanitize_folder_name(
        f"{author} - {title} [{service} {post_id}] (Patreon)" if service == 'patreon'
        else f"{author} - {title} [{service} {post_id}]"
    )

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

    # API-te size na thakle estimate unknown — None pathay, bot "unknown" dekhay
    total = 0
    has_size = False
    for a in ([main] if main.get('path') else []) + (post.get('attachments') or []):
        sz = a.get('size') or a.get('filesize') or a.get('bytes')
        try:
            if sz:
                total += int(sz)
                has_size = True
        except (TypeError, ValueError):
            pass

    return {
        'title': title,
        'folder': folder,
        'count': len(items),
        'est_bytes': total if has_size else None,
        'has_full': post.get('has_full'),
    }


def download_pawchive_post(post_url, save_path=".", progress_cb=None, prescan=None):
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
        if is_html_page(r, data):
            return f"[{i}/{len(items)}]  Skipped (HTML, not media): {name}"
        # ext content-er sathe na mille client khulte pare na — bytes same, sudhu ext thik
        if '.' in safe_name:
            stem, dot, e = safe_name.rpartition('.')
            fixed = fix_ext_by_magic(data, e.lower())
            if fixed != e.lower():
                safe_name = f"{stem}.{fixed}"
                out = os.path.join(target_dir, f"{i:03d} - {safe_name}")
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
                if progress_cb is not None:
                    try:
                        progress_cb(done, len(items))
                    except Exception:
                        pass
                if done % 10 == 0:
                    _thread_log(f"--- Progress: {done}/{len(items)} ({done/max(time.time()-t0,1):.1f} file/s) ---")
    finally:
        prevent_sleep_stop()

    print(f"\n Pawchive Download Complete! Saved in: {os.path.abspath(target_dir)}")
    return os.path.abspath(target_dir)


def scan_any(url):
    """URL dekhe site chine sothik scanner-e pathay. Returns scan dict or None."""
    if 'pawchive.pw' in url:
        return scan_pawchive_post(url)
    return scan_eh_gallery(url)


def download_any(url, save_path=".", progress_cb=None, prescan=None):
    """URL dekhe site chine sothik downloader-e pathay. Returns target_dir or None."""
    if 'pawchive.pw' in url:
        return download_pawchive_post(url, save_path=save_path, progress_cb=progress_cb, prescan=prescan)
    else:
        return download_eh_gallery(url, save_path=save_path, progress_cb=progress_cb, prescan=prescan)


TERA_DOMAINS = (
    'terabox.com', '1024terabox.com', 'teraboxapp.com', 'mirrobox.com',
    'nephobox.com', 'freemibox.com', '1024tera.com', 'teraboxlink.com',
)
TERA_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
           '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')


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
    import requests
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

    def list_dir(path, depth):
        params = dict(base_params, page='1', num='1000', order='time', desc='1', dir=path)
        r = sess.get('https://www.terabox.com/share/list', params=params,
                     headers={'Referer': share_page_url}, timeout=30)
        data = r.json()
        if data.get('errno') not in (0, None):
            raise ValueError(f"ERRNO_{data.get('errno')}")
        return data.get('list') or []

    while queue:
        dpath, depth = queue.pop(0)
        if depth > 3 or dpath in seen:
            continue
        seen.add(dpath)
        try:
            items = list_dir(dpath, depth)
        except ValueError:
            raise
        except Exception:
            continue
        for it in items:
            if it.get('isdir'):
                queue.append((it.get('path', ''), depth + 1))
            else:
                it['_dir'] = dpath
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
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.error import RetryAfter, TimedOut
from telegram.ext import (
    ApplicationBuilder,
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
    for n, f in enumerate(files, start=1):
        fsize = f.stat().st_size
        # single file-i limit er besi holeo alada zip-e dhukao (Telegram emniteo katbe)
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


def blocking_download(url: str, workdir: str, progress_cb=None, prescan=None):
    """Thread-e chalano blocking download. Returns (target_dir, total_bytes)."""
    target = download_any(url, save_path=workdir, progress_cb=progress_cb, prescan=prescan)
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


async def url_listener(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.effective_user.id):
        return
    if not update.message or not update.message.text:
        return
    m = URL_RE.search(update.message.text)
    if not m:
        return
    url = m.group(0)
    if "mega.nz" in url or "mega.io" in url:
        await send_mega(update.message, url)
        return
    if is_terabox_url(url):
        await send_terabox(update.message, url)
        return
    if "e-hentai.org" not in url and "exhentai.org" not in url and "pawchive.pw" not in url:
        return
    await send_as_zip(update.message, url)


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


async def send_as_zip(message, url: str):
    """Link dile direct zip pathay — kono button na."""
    loop = asyncio.get_running_loop()
    status = await message.reply_text(
        "Scanning...\n░░░░░░░░░░ 0% (finding images)"
    )
    workdir = tempfile.mkdtemp(prefix="tgdl_")
    try:
        # 1. Scan only — download shuru korar age count + size dekhay
        info = await asyncio.to_thread(scan_any, url)
        if not info or not info.get('count'):
            await status.edit_text("No images found. Link / Cloudflare check koro.")
            return

        n_files = info['count']
        est = info.get('est_bytes')
        est_txt = f"~{human_size(est)}" if est else "size unknown"
        await status.edit_text(
            f"{n_files} images, {est_txt}\nPreparing download..."
        )

        # 2. Download with progress bar
        target, total = await asyncio.to_thread(
            blocking_download, url, workdir, make_progress_cb(status, loop, "Downloading..."), info
        )

        if not target:
            await status.edit_text("Download fail. Link / Cloudflare check koro.")
            return

        await zip_and_send(status, message.chat, target, total)
    except Exception as e:
        try:
            await status.edit_text(f"Error: {str(e)[:300]}")
        except Exception:
            pass
    finally:
        # sob temp file (downloaded images + zips) permanently delete,
        # nahole VPS storage full hoye jabe
        shutil.rmtree(workdir, ignore_errors=True)


async def zip_and_send(status, chat, target_dir: str, total: int):
    """Downloaded folder -> zip (+progress) -> upload. Gallery + Terabox 2 jon-e use kore."""
    loop = asyncio.get_running_loop()
    n_files = sum(1 for _ in Path(target_dir).rglob("*") if _.is_file())
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


def blocking_mega_download(url: str, workdir: str):
    """Mega file link theke 1 ta file namay. Returns path, naile exception.
    Folder link ekhono supported na."""
    from mega import Mega
    if '/folder/' in url or '#F!' in url:
        raise ValueError("FOLDER_LINK")
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
    """Mega file link: zip hole direct zip, single file hole direct document."""
    status = await message.reply_text("Downloading from Mega...")
    workdir = tempfile.mkdtemp(prefix="tgdl_")
    try:
        try:
            target = await asyncio.to_thread(blocking_mega_download, url, workdir)
        except ValueError as e:
            if str(e) == "FOLDER_LINK":
                await status.edit_text("Mega folder link ekhono supported na — file link dao.")
                return
            raise
        if not target:
            await status.edit_text("Download fail. Link check koro.")
            return

        size_mb = os.path.getsize(target) / 1048576
        if size_mb > 2000:
            await status.edit_text(f"File too big ({size_mb:.0f}MB) — Telegram max 2GB.")
            return

        name = os.path.basename(target)
        kind = 'zip' if name.lower().endswith(('.zip', '.rar', '.7z')) else 'file'
        await status.edit_text(f"Uploading {name} ({size_mb:.1f}MB)...")
        chat = message.chat
        k = await send_one_file(chat, target, name)
        print(f"Mega send kind: {k} ({name})", flush=True)
        await status.edit_text(f"Done! {name} ({size_mb:.1f}MB).")
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
    """Terabox share link -> files namay -> zip -> send (gallery flow reuse)."""
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
    if API_BASE_URL:
        builder = builder.base_url(API_BASE_URL)
    if API_BASE_FILE_URL:
        builder = builder.base_file_url(API_BASE_FILE_URL)
    app = builder.build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, url_listener))
    print(f"Bot running... (MAX_ZIP_MB={MAX_ZIP_MB})")
    app.run_polling()


if __name__ == "__main__":
    main()
