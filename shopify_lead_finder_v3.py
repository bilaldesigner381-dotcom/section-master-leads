"""
SHOPIFY LEAD FINDER v3.2 — DYNAMIC APP DISCOVERY, HOURLY RUNS, EMAIL MANDATORY
=============================================================================
Ab koi hardcoded app list nahi hai.

Har run:
  1. Shopify ke apne sitemap (apps.shopify.com/sitemap -> sitemap_apps_en.xml) se
     SAARI apps ki list uthata hai (16,000+ apps) — nayi apps sitemap me khud aa jati hain.
  2. Google Sheet ke "Apps" tab me dekhta hai kaun si apps pehle scrape ho chuki hain,
     aur agli NAYI apps chunta hai (jin ka review scrape nahi hua).
     Sab nayi apps ho chuki hon to purani apps ke naye reviews (page 1..N) dobara dekhta hai.
  3. In apps ke review pages se store names nikalta hai, jab tak NAMES_TARGET_PER_RUN
     naye names na mil jayein.
  4. Har name ke domain guesses -> store verify -> REAL email nikalna.
  5. Email mili  -> "Leads" tab.   Email nahi mili -> "NoEmail" tab.

State (kaun si apps / names ho chuke) Google Sheet me rehti hai (tabs: Apps, Checked),
is liye har ghante naye server / GitHub runner par bhi run wahin se aage barhta hai.

Run one-shot (cron / GitHub Actions ke liye):   python shopify_lead_finder_v3.py
Run har ghante khud (VPS / Render / PC par):     RUN_FOREVER=1 python shopify_lead_finder_v3.py

Install:
  pip install requests beautifulsoup4 gspread google-auth dnspython

-------------------------------------------------------------------------------
CHANGELOG v3.2 — FIX: run "bilkul stuck" ho jati thi, koi lead nahi, phir GitHub
khud 1 ghante baad forceful cancel kar deta tha (koi log nahi)
-------------------------------------------------------------------------------
Root cause (v3.1 wale DNS bug ki "jud/twin", ek aur jagah): `domain_has_mx()`
ke liye to hum ne pehle hi (v3.1 me) fix kar diya tha ke DNS lookup kabhi
poore pipeline ko block na kare. LEKIN `bounded_get()` — jo `fetch_page`,
`get_product_count`, review-page fetch, sab kuch use karta hai — abhi bhi
seedha `requests.get(url, timeout=(connect, read))` call karta tha.

Masla ye hai ke `requests`/`urllib3` ka `timeout=` parameter sirf socket
CONNECT aur READ ko bound karta hai — lekin us se PEHLE jo `socket.getaddrinfo()`
(DNS resolution) hoti hai, wo is timeout se bilkul independent hai. Agar kisi
guessed domain (jaise `storename.com` — jinme se zyadatar wajood hi nahi
rakhte ya ajeeb DNS setup wale parked/dead registrars par hote hain) ki DNS
lookup kahin phas jaye, to us worker thread ko koi bhi `timeout=` value wapas
nahi la sakti — chahe wo 10 second ho ya 20.

25 parallel workers jab sath sath bohot saare (mostly-invalid) guessed
domains resolve karne ki koshish karte hain, to dheere dheere zyada workers
isi trap me phasty jate hain. Jab saare phas jayein: koi naya store process
nahi hota, koi naya lead print nahi hota, heartbeat sirf shuru ke 1-2 baar
chalti hai (jab tak koi worker free hai) phir wo bhi ruk jati hai — aur akhir
me hamara apna 50-minute watchdog ya GitHub Actions ka apna 1-hour forceful
cancel (bina kisi diagnostic log ke) chal jata hai.

FIX: `bounded_get()` ke andar ka asal `requests.get(...)` call ab ek dedicated
thread-pool (`_http_executor`) me submit hota hai aur `future.result(timeout=
hard_timeout + buffer)` se HARD wall-clock cap ke sath bandha hai — bilkul
`domain_has_mx()` wala hi pattern. Agar underlying request (DNS/connect/read,
kuch bhi) kahin phas jaye, calling worker turant `FutureTimeoutError` le kar
wapas aa jata hai aur agla store try karta hai — pipeline kabhi block nahi
hota. (Note: Python threads force-kill nahi ho sakti, isliye stuck thread khud
background me zinda reh sakti hai — is liye `_http_executor` ka pool
`MAX_WORKERS` se kaafi bara (4x) rakha gaya hai, taake stuck threads jama hone
par bhi naye kaam ke liye jagah bani rahe.)

Baaqi POORI script (v3.1) waisi ki waisi hai — sirf `bounded_get()` function
aur ek naya `_http_executor` + `_do_bounded_get_request()` helper add hua hai.
-------------------------------------------------------------------------------
CHANGELOG v3.1 — FIX: script "stuck ho jati thi" bug
-------------------------------------------------------------------------------
Root cause #1 (asal wajah — deadlock): `domain_has_mx()` `@lru_cache` use kar
raha tha. Python ka `lru_cache` ek SINGLE GLOBAL LOCK ke sath kaam karta hai —
jab do threads EK HI WAQT alag-alag (cache-miss) arguments ke sath call karte
hain, dusra thread pehle wale ke poora hone tak BLOCK ho jata hai, chahe
domain bilkul different ho. Agar kisi ek domain ki DNS lookup kahin phans
jaye (dnspython ka `lifetime=` kuch network conditions — jaise silently
dropped UDP packets — me guarantee nahi karta), to us EK stuck call ke peeche
saare 25 workers is lock par queue ho kar poora pipeline freeze kar dete
hain. Ye "kuch dair chalne ke baad achanak silence" wale symptom ki 100%
wajah thi.
  FIX: `domain_has_mx` ab ek chhoti dedicated thread-pool me chalti hai aur
  `future.result(timeout=DNS_HARD_TIMEOUT_SECONDS)` se HARD wall-clock cap
  ke sath bandhi hai — dnspython internally kuch bhi kare, humari taraf se
  guarantee hai ke ye kabhi bhi DNS_HARD_TIMEOUT_SECONDS se zyada nahi rukegi.
  Cache ab manual dict + lock hai jahan sirf dict read/write lock hota hai,
  khud DNS call nahi — is se different domains ke concurrent lookups ab
  ek-dusre ko block nahi karte.

Root cause #2 (defense-in-depth — trickle attack): sirf `fetch_text()` me
comment tha ke "timeout tuple sirf har read chunk ke darmiyan gap bound karta
hai, total download time nahi — agar server data trickle kare (thora thora,
har chunk apne window ke andar) to total time unbounded reh sakta hai." Ye
fix sirf `fetch_text` me tha; `fetch_page`, `get_product_count`, aur review
page fetch me nahi — jahan 25 parallel workers store websites hit karte hain.
  FIX: naya `bounded_get()` helper — `stream=True` ke sath chunk-by-chunk
  padhta hai aur HAR chunk ke baad total-elapsed-time check karta hai; agar
  hard cap cross ho jaye to turant abort. Ab `fetch_text`, `fetch_page`,
  `get_product_count`, aur review-page fetch sab isi helper se guzarte hain.

Root cause #3 (belt-and-suspenders): `socket.setdefaulttimeout(...)` add kiya
— ye un calls (jaise Google Sheets / gspread) ko bhi ek fallback socket
timeout deta hai jinka apna explicit per-call timeout set nahi hota.

Operational advice: agar GitHub Actions me chalate ho, workflow ke
`timeout-minutes` ko is script ke HARD_TIMEOUT_MINUTES se thoda ZYADA rakho
(default = RUN_TIME_BUDGET_MINUTES + 5), taake script ka apna watchdog
GitHub ke forceful "cancelled" se PEHLE fire ho — GitHub ka cancel kisi
diagnostic log ke bina hota hai, script ka apna watchdog leads save karke
cleanly exit karta hai.
-------------------------------------------------------------------------------
"""

import os
import re
import sys
import csv
import json
import time
import socket
import html as html_lib
import logging
import threading
import requests

from collections import Counter, namedtuple
from urllib.parse import urlparse, urljoin, unquote
from concurrent.futures import (
    ThreadPoolExecutor, as_completed, TimeoutError as FutureTimeoutError
)

try:
    import gspread
    from google.oauth2.service_account import Credentials
    GSPREAD_AVAILABLE = True
except ImportError:
    GSPREAD_AVAILABLE = False

try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

try:
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:
    DNS_AVAILABLE = False


# ============================================================
# GLOBAL SAFETY NET
# ============================================================
# Fallback socket-level timeout for ANY connection that doesn't explicitly
# set its own (e.g. gspread/Google API calls). Calls that DO set an explicit
# timeout (our own requests.get(..., timeout=...)) are unaffected by this —
# this only kicks in when nothing else would.
socket.setdefaulttimeout(20)


# ============================================================
# CONFIG
# ============================================================

def _env_bool(name, default):
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# --- Dynamic app discovery ---
SITEMAP_INDEX_URL = os.environ.get("APPS_SITEMAP_INDEX", "https://apps.shopify.com/sitemap").strip()
APPS_SITEMAP_LANG = os.environ.get("APPS_SITEMAP_LANG", "en").strip()
CATEGORY_FALLBACK_PAGES = int(os.environ.get("CATEGORY_FALLBACK_PAGES", "4"))   # sitemap fail ho to
MAX_SITEMAP_FILES = int(os.environ.get("MAX_SITEMAP_FILES", "6"))               # ek run me kitni sitemap files fetch karein
DISCOVERY_WORKERS = int(os.environ.get("DISCOVERY_WORKERS", "4"))
DISCOVERY_BUDGET_MINUTES = float(os.environ.get("DISCOVERY_BUDGET_MINUTES", "6"))  # app-list dhoondne ka max waqt

# --- Hard safety timeout: script kabhi bhi hang ho (network trickle, DNS, ya koi
#     anjaan bug) to process ZABARDASTI khatam ho jata hai taake GitHub ka apna
#     forceful "cancelled" (jisme koi clean log nahi milta) kabhi na aaye.
#     0 = khud-ba-khud RUN_TIME_BUDGET_MINUTES + 5 minute.
HARD_TIMEOUT_MINUTES = float(os.environ.get("HARD_TIMEOUT_MINUTES", "0"))

# --- Network hard caps (trickle-attack safe: total wall-clock time, na ke
#     sirf do reads ke darmiyan gap) ---
NETWORK_HARD_TIMEOUT_SECONDS = float(os.environ.get("NETWORK_HARD_TIMEOUT_SECONDS", "20"))
DNS_HARD_TIMEOUT_SECONDS = float(os.environ.get("DNS_HARD_TIMEOUT_SECONDS", "8"))
DNS_WORKERS = int(os.environ.get("DNS_WORKERS", "8"))

# --- How much to scrape per run ---
NAMES_TARGET_PER_RUN = int(os.environ.get("NAMES_TARGET_PER_RUN", "2500"))      # naye store names ka target
MAX_APPS_PER_RUN = int(os.environ.get("MAX_APPS_PER_RUN", "400"))
PAGES_PER_NEW_APP = int(os.environ.get("PAGES_PER_NEW_APP", "30"))              # nayi app: itne review pages
REFRESH_PAGES = int(os.environ.get("REFRESH_PAGES", "5"))                       # purani app: sirf naye reviews
APP_SCRAPE_WORKERS = int(os.environ.get("APP_SCRAPE_WORKERS", "4"))
APP_BATCH_SIZE = int(os.environ.get("APP_BATCH_SIZE", "8"))
APP_STORE_DELAY_SECONDS = float(os.environ.get("APP_STORE_DELAY", "0.4"))

# --- Time budget (hourly runs overlap na karein) ---
SCRAPE_BUDGET_MINUTES = float(os.environ.get("SCRAPE_BUDGET_MINUTES", "12"))
RUN_TIME_BUDGET_MINUTES = float(os.environ.get("RUN_TIME_BUDGET_MINUTES", "50"))

# --- Loop mode (khud har ghante) ---
RUN_FOREVER = _env_bool("RUN_FOREVER", False)
RUN_EVERY_MINUTES = float(os.environ.get("RUN_EVERY_MINUTES", "60"))

# --- Analysis ---
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "25"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "10"))
CHECKPOINT_INTERVAL = int(os.environ.get("CHECKPOINT_INTERVAL", "100"))
STORE_TIME_BUDGET_SECONDS = float(os.environ.get("STORE_TIME_BUDGET_SECONDS", "25"))
HEARTBEAT_SECONDS = float(os.environ.get("HEARTBEAT_SECONDS", "30"))

# --- Quality filters ---
MIN_PRODUCTS = int(os.environ.get("MIN_PRODUCTS", "1"))
STRICT_NAME_MATCH = _env_bool("STRICT_NAME_MATCH", True)
VERIFY_MX = _env_bool("VERIFY_MX", True)
EXCLUDE_DOMAINS = [
    d.strip().lower() for d in os.environ.get("EXCLUDE_DOMAINS", "").split(",") if d.strip()
]

# --- Google Sheets ---
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Leads").strip()
NO_EMAIL_SHEET_NAME = os.environ.get("GOOGLE_NO_EMAIL_SHEET_NAME", "NoEmail").strip()
APPS_SHEET_NAME = os.environ.get("GOOGLE_APPS_SHEET_NAME", "Apps").strip()
CHECKED_SHEET_NAME = os.environ.get("GOOGLE_CHECKED_SHEET_NAME", "Checked").strip()
REQUIRE_SHEET = _env_bool("REQUIRE_SHEET", True)

# --- Local files ---
DATA_DIR = os.environ.get("LEAD_DATA_DIR", "./lead_data")
SEEN_FILE = os.path.join(DATA_DIR, "seen_names.json")
APPS_STATE_FILE = os.path.join(DATA_DIR, "apps_state.json")
LEADS_CSV_FILE = os.path.join(DATA_DIR, "leads.csv")
NO_EMAIL_CSV_FILE = os.path.join(DATA_DIR, "no_email.csv")

HEADERS = [
    "Store Name", "Store URL", "Domain", "Email", "Other Emails", "Theme",
    "Products", "Lead Score", "Quality", "FAQ", "Testimonials", "Reviews",
    "Sticky Add To Cart", "Size Guide", "Newsletter", "WhatsApp",
    "Reason", "Recommended Sections", "Source",
]
NO_EMAIL_HEADERS = ["Store Name", "Store URL", "Domain", "Products", "Source"]
APPS_HEADERS = ["Handle", "Status", "Pages Scraped", "Names Found", "Last Scraped"]
CHECKED_HEADERS = ["Key", "Store Name", "Checked"]

SheetTabs = namedtuple("SheetTabs", "leads no_email apps checked")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("shopify-lead-finder")


# ============================================================
# SMALL HELPERS
# ============================================================

def norm(text):
    """Sirf lowercase letters+digits (matching ke liye)."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def name_key(name):
    return f"name:{norm(name)}"


def now_str():
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def normalize_domain(url):
    try:
        domain = urlparse(url).netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def normalize_url(domain_or_url):
    try:
        if domain_or_url.startswith(("http://", "https://")):
            parsed = urlparse(domain_or_url)
            return f"{parsed.scheme}://{parsed.netloc}"
        return f"https://{domain_or_url}"
    except Exception:
        return domain_or_url


def is_bad_domain(domain):
    bad_domains = [
        "facebook.com", "instagram.com", "youtube.com", "tiktok.com",
        "pinterest.com", "twitter.com", "x.com", "linkedin.com",
        "amazon.com", "ebay.com", "etsy.com", "walmart.com", "reddit.com",
        "google.com", "bing.com", "shopify.com", "apps.shopify.com",
        "community.shopify.com", "help.shopify.com", "themes.shopify.com",
    ] + EXCLUDE_DOMAINS
    return any(domain == bad or domain.endswith("." + bad) for bad in bad_domains)


# ============================================================
# BOUNDED NETWORK FETCH
# ============================================================
# requests' `timeout=(connect, read)` only bounds the gap BETWEEN two reads —
# a server that trickles data (small chunks, each arriving just inside the
# read-timeout window) can keep a connection open indefinitely, and that
# unbounded call can eventually starve every worker thread. bounded_get()
# closes that hole: it streams the response and checks TOTAL elapsed wall
# time after every chunk, aborting hard if the cap is crossed — regardless
# of how the server paces its bytes.
#
# v3.2: on top of that, `requests`' `timeout=` NEVER bounds DNS resolution
# (socket.getaddrinfo happens before any socket/timeout exists). A hung DNS
# lookup on a guessed/junk domain can block a worker forever no matter what
# hard_timeout says. So the actual request now runs inside a dedicated
# executor and is bounded from the OUTSIDE with future.result(timeout=...) —
# the exact same pattern already used for domain_has_mx() below. See the
# v3.2 changelog at the top of this file for the full story.

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 Chrome/131 Safari/537.36"
)

# Dedicated pool for the actual HTTP work. Sized larger than MAX_WORKERS on
# purpose: since a stuck DNS lookup leaves its thread occupied forever
# (Python threads can't be force-killed), a pool sized exactly to
# MAX_WORKERS would eventually fill up with stuck threads and start
# queueing new requests behind them — recreating the exact freeze we're
# fixing. A 4x pool gives plenty of headroom for stuck threads to
# accumulate over a run while fresh requests still get a free thread.
HTTP_EXECUTOR_WORKERS = int(os.environ.get("HTTP_EXECUTOR_WORKERS", str(MAX_WORKERS * 4)))
_http_executor = ThreadPoolExecutor(max_workers=HTTP_EXECUTOR_WORKERS, thread_name_prefix="http-fetch")


def _do_bounded_get_request(url, params, req_headers, hard_timeout, max_bytes):
    """Actual (potentially slow / DNS-hanging) network work — runs inside
    _http_executor. Returns (status_code, text, final_url) or raises."""
    resp = None
    try:
        start = time.time()
        resp = requests.get(
            url, params=params, headers=req_headers, stream=True,
            timeout=(min(hard_timeout, 10), hard_timeout),
            allow_redirects=True,
        )
        chunks, total = [], 0
        for chunk in resp.iter_content(chunk_size=8192):
            if time.time() - start > hard_timeout:
                raise TimeoutError(f"bounded_get: {hard_timeout}s cap crossed (trickle?) for {url}")
            if chunk:
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    break
        body = b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")
        return resp.status_code, body, resp.url
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass


def bounded_get(url, params=None, headers=None, hard_timeout=None, retries=1,
                 retry_sleep=1.5, max_bytes=8_000_000):
    """GET jo total wall-clock time ko HARD cap karta hai (trickle-safe AND
    DNS-hang-safe — see v3.2 changelog above).
    Returns a lightweight object with .status_code, .text, .url — ya None
    agar fetch fail/timeout ho jaye."""
    hard_timeout = hard_timeout if hard_timeout is not None else NETWORK_HARD_TIMEOUT_SECONDS
    req_headers = {"User-Agent": DEFAULT_UA, "Accept-Language": "en-US;q=0.9"}
    if headers:
        req_headers.update(headers)

    class _Result:
        def __init__(self, status_code, text, url):
            self.status_code = status_code
            self.text = text
            self.url = url

    for attempt in range(max(1, retries)):
        try:
            future = _http_executor.submit(
                _do_bounded_get_request, url, params, req_headers, hard_timeout, max_bytes
            )
            # +5s buffer so our own outer cap always fires strictly after the
            # inner per-chunk cap would have (never tighter than it).
            status_code, text, final_url = future.result(timeout=hard_timeout + 5)
            return _Result(status_code, text, final_url)
        except FutureTimeoutError:
            # Request (most likely DNS resolution) is still stuck somewhere
            # inside _http_executor. We do NOT wait for it — we give up on
            # this attempt immediately so the calling worker is freed up.
            # The stuck background thread will linger (can't be killed) but
            # no longer blocks the pipeline.
            logger.warning(
                "bounded_get: HARD timeout (%.0fs) — DNS/connect kahin phas gaya lagta hai: %s",
                hard_timeout, url
            )
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(retry_sleep)
    return None


def fetch_text(url, timeout=30, retries=3):
    """Sitemap/index pages ke liye — bounded_get par based, trickle-safe."""
    for attempt in range(retries):
        result = bounded_get(url, hard_timeout=timeout, retries=1)
        if result is None:
            time.sleep(1.5)
            continue
        if result.status_code == 200:
            return result.text
        if result.status_code == 429:
            time.sleep(3 * (attempt + 1))
            continue
        return ""
    return ""


# ============================================================
# LOCAL PERSISTENCE
# ============================================================

def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_seen():
    ensure_data_dir()
    if not os.path.exists(SEEN_FILE):
        return set()
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    ensure_data_dir()
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f)
    except Exception as e:
        logger.error("Could not save %s: %s", SEEN_FILE, str(e))


def load_local_apps_state():
    if not os.path.exists(APPS_STATE_FILE):
        return {}
    try:
        with open(APPS_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_local_apps_state(state):
    ensure_data_dir()
    try:
        with open(APPS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f)
    except Exception as e:
        logger.error("Could not save %s: %s", APPS_STATE_FILE, str(e))


def ensure_csv_schema_matches(path, headers):
    """Purani CSV ke columns alag hon to usay rename kar deta hai (data safe)."""
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", newline="", encoding="utf-8") as f:
            existing = next(csv.reader(f), None)
    except Exception as e:
        logger.error("Could not read CSV header %s: %s", path, str(e))
        return

    if existing is not None and existing != headers:
        base, ext = os.path.splitext(path)
        backup = f"{base}_old_schema_{time.strftime('%Y%m%d-%H%M%S')}{ext}"
        try:
            os.rename(path, backup)
            logger.warning("Purani CSV ke columns alag the — data safe hai: %s", backup)
        except Exception as e:
            logger.error("Could not rotate %s: %s", path, str(e))


def append_csv(path, headers, records):
    if not records:
        return
    ensure_data_dir()
    file_exists = os.path.exists(path)
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=headers)
            if not file_exists:
                writer.writeheader()
            for r in records:
                writer.writerow({h: r.get(h, "") for h in headers})
        logger.info("Appended %s rows to %s", len(records), path)
    except Exception as e:
        logger.error("Could not write %s: %s", path, str(e))


# ============================================================
# DYNAMIC APP DISCOVERY (koi hardcoded app nahi)
# ============================================================

RESERVED_HANDLES = {
    "categories", "collections", "partners", "stories", "search", "sitemap",
    "login", "partner", "tutorials", "blog", "guides", "built-in-features",
    "extensions", "category-features", "robots.txt",
}


def handle_from_url(url):
    path = urlparse(url.strip()).path.strip("/")
    if not path or "/" in path or path in RESERVED_HANDLES:
        return ""
    return path


def discover_apps_via_categories(deadline):
    """Fallback: sitemap na chale to category pages se app handles nikalta hai.
    Deadline se bandha hua hai — pehle ye function unbounded tha aur (agar sitemap
    fail ho aur Shopify GitHub Actions ke IPs ko slow/block kare) ghanton tak
    sequentially fetch karta reh sakta tha. Ab parallel + deadline-checked hai."""
    logger.warning("Sitemap se apps nahi mili — category pages se try kar raha hun (fallback, bounded).")
    home = fetch_text("https://apps.shopify.com/", timeout=20)
    slugs = list(dict.fromkeys(re.findall(r"/categories/([a-z0-9-]+)", home)))[:40]
    apps = {}

    def fetch_slug_page(slug, page):
        if time.time() > deadline:
            return slug, page, []
        page_html = fetch_text(f"https://apps.shopify.com/categories/{slug}/all?page={page}", timeout=20)
        found = re.findall(
            r'href="(?:https://apps\.shopify\.com)?/([a-z0-9][a-z0-9_-]*)\?[^"]*surface_type=category',
            page_html,
        )
        return slug, page, found

    with ThreadPoolExecutor(max_workers=DISCOVERY_WORKERS) as executor:
        for page in range(1, CATEGORY_FALLBACK_PAGES + 1):
            if time.time() > deadline or not slugs:
                break
            futures = [executor.submit(fetch_slug_page, slug, page) for slug in slugs]
            still_active = []
            for future in as_completed(futures):
                slug, _, found = future.result()
                if found:
                    still_active.append(slug)
                    for h in found:
                        if h not in RESERVED_HANDLES:
                            apps.setdefault(h, "")
            slugs = still_active   # jo slug is page par khali aaya, agli page skip

    logger.info("Category fallback se apps mile: %s", len(apps))
    return apps


def discover_app_handles():
    """Returns [(handle, lastmod)] — recently updated (active) apps pehle.
    Poori discovery DISCOVERY_BUDGET_MINUTES tak bandhi hai — kabhi bhi is se
    zyada waqt scraping shuru hue bina nahi guzarta."""
    deadline = time.time() + DISCOVERY_BUDGET_MINUTES * 60

    index_xml = fetch_text(SITEMAP_INDEX_URL, timeout=20)
    sitemap_urls = re.findall(
        r"<loc>\s*([^<\s]*sitemap_apps_%s\.xml[^<\s]*)\s*</loc>" % re.escape(APPS_SITEMAP_LANG),
        index_xml,
    )
    if not sitemap_urls:
        sitemap_urls = [f"https://apps.shopify.com/sitemap_apps_{APPS_SITEMAP_LANG}.xml"]

    if len(sitemap_urls) > MAX_SITEMAP_FILES:
        logger.warning(
            "%s sitemap files mile, sirf pehli %s is run me (MAX_SITEMAP_FILES) — baaqi agli run me.",
            len(sitemap_urls), MAX_SITEMAP_FILES
        )
        sitemap_urls = sitemap_urls[:MAX_SITEMAP_FILES]

    logger.info("Sitemap files fetch honi hain: %s", len(sitemap_urls))
    apps = {}

    def fetch_one(sm_url):
        if time.time() > deadline:
            return sm_url, ""
        return sm_url, fetch_text(sm_url, timeout=25, retries=2)

    with ThreadPoolExecutor(max_workers=min(DISCOVERY_WORKERS, max(1, len(sitemap_urls)))) as executor:
        futures = [executor.submit(fetch_one, u) for u in sitemap_urls]
        done = 0
        for future in as_completed(futures):
            sm_url, xml = future.result()
            done += 1
            count_before = len(apps)
            for m in re.finditer(
                r"<url>\s*<loc>\s*([^<\s]+)\s*</loc>(?:\s*<lastmod>\s*([^<\s]*)\s*</lastmod>)?", xml
            ):
                handle = handle_from_url(m.group(1))
                if handle:
                    apps.setdefault(handle, m.group(2) or "")
            logger.info(
                "Sitemap %s/%s fetch hui (%s): +%s apps (total %s)",
                done, len(sitemap_urls), sm_url, len(apps) - count_before, len(apps)
            )

    if not apps and time.time() < deadline:
        apps = discover_apps_via_categories(deadline)

    if not apps:
        logger.error(
            "Koi bhi app discover nahi ho saki is run me (sitemap + category fallback dono khali/blocked). "
            "Ho sakta hai apps.shopify.com is IP range ko throttle/block kar raha ho — agli run me dobara try hoga."
        )

    ordered = sorted(apps.items(), key=lambda kv: kv[1], reverse=True)
    logger.info(
        "Apps discovered from Shopify App Store: %s (discovery %.1f min lagi)",
        len(ordered), (time.time() - (deadline - DISCOVERY_BUDGET_MINUTES * 60)) / 60
    )
    return ordered


# ============================================================
# APP REVIEW SCRAPING
# ============================================================

TRAILING_GENERIC_WORDS = {"store", "shop", "official", "co", "llc", "inc", "ltd"}
GENERIC_NAME_WORDS = {"the", "and", "store", "shop", "official", "co", "llc", "inc", "ltd"}


def scrape_app_reviews_page(handle, page):
    """Returns (names_list, state) — state: 'ok' | 'missing' (404) | 'error'."""
    url = f"https://apps.shopify.com/{handle}/reviews"
    params = {"sort_by": "newest", "page": page}

    result = None
    for attempt in range(3):
        result = bounded_get(url, params=params, hard_timeout=20, retries=1)
        if result is None:
            time.sleep(2)
            continue
        if result.status_code == 429:
            time.sleep(5 * (attempt + 1))
            result = None
            continue
        break

    if result is None:
        return [], "error"
    if result.status_code == 404:
        return [], "missing"
    if result.status_code != 200:
        return [], "error"

    soup = BeautifulSoup(result.text, "html.parser")
    review_blocks = soup.find_all("div", attrs={"data-merchant-review": True})

    names = []
    for block in review_blocks:
        children = block.find_all("div", recursive=False)
        if len(children) < 2:
            continue
        author_div = children[1].find("div", class_="tw-text-fg-primary")
        if not author_div:
            continue
        name = author_div.get_text(strip=True)
        if name:
            names.append(name)

    if review_blocks and not names:
        logger.warning(
            "App %s page %s: review blocks mile lekin names parse nahi hue — "
            "shayad Shopify ne page ka HTML/CSS class badal diya hai.", handle, page
        )
    return names, "ok"


def scrape_app(handle, max_pages, deadline):
    """Ek app ke review pages (newest pehle). Returns (names, pages_scraped, status)
    status: 'done' | 'empty' | 'dead' | 'error'."""
    all_names, pages, prev = [], 0, None
    for page in range(1, max_pages + 1):
        if time.time() > deadline:
            break
        page_names, state = scrape_app_reviews_page(handle, page)
        if state == "missing":
            return all_names, pages, ("dead" if page == 1 else "done")
        if state == "error":
            return all_names, pages, "error"
        if not page_names or page_names == prev:   # khatam ya same page repeat
            break
        prev = page_names
        pages += 1
        all_names += page_names
        time.sleep(APP_STORE_DELAY_SECONDS)
    return all_names, pages, ("done" if all_names else "empty")


def guess_domains_from_name(name):
    """Store name -> possible domains (ordered, unverified)."""
    cleaned = re.sub(r"[^a-z0-9\s-]", "", name.lower()).strip()
    words = [w for w in cleaned.split() if w.strip("-")]
    if not words:
        return []

    variants = [words]
    trimmed = list(words)
    while len(trimmed) > 1 and trimmed[-1] in TRAILING_GENERIC_WORDS:
        trimmed = trimmed[:-1]
    if trimmed != words:
        variants.append(trimmed)

    guesses = []
    for v in variants:
        for slug in ("".join(v), "-".join(v)):
            slug = slug.strip("-")
            if len(slug) < 3:
                continue
            for d in (f"{slug}.myshopify.com", f"{slug}.com"):
                if d not in guesses:
                    guesses.append(d)
    return guesses


def name_keys(name):
    """(full, core, significant_tokens) — page matching ke liye."""
    words = [w.strip("-") for w in re.sub(r"[^a-z0-9\s-]", "", name.lower()).split()]
    words = [w for w in words if w]
    core_words = [w for w in words if w not in GENERIC_NAME_WORDS]
    full = "".join(words)
    core = "".join(core_words)
    tokens = [w for w in core_words if len(w) >= 3]
    return full, core, tokens


def plan_apps(sitemap_apps, apps_state):
    """Is run me kaun si apps scrape hongi: [(handle, max_pages, kind)].
    Order: retry (unprocessed names) -> bilkul nayi apps -> error wali -> purani apps ka refresh."""
    retry, fresh, errored = [], [], []
    for handle, _ in sitemap_apps:
        st = apps_state.get(handle, {}).get("status")
        if st == "retry":
            retry.append(handle)
        elif st is None:
            fresh.append(handle)
        elif st == "error":
            errored.append(handle)

    refresh = sorted(
        [h for h, s in apps_state.items() if s.get("status") == "done"],
        key=lambda h: apps_state[h].get("last", ""),
    )

    plan = [(h, PAGES_PER_NEW_APP, "new") for h in retry + fresh + errored]
    plan += [(h, REFRESH_PAGES, "refresh") for h in refresh]
    return plan, {"retry": len(retry), "new": len(fresh), "error": len(errored), "refresh": len(refresh)}


def collect_new_names(plan, seen, deadline):
    """Apps scrape karta hai jab tak NAMES_TARGET_PER_RUN naye names na mil jayein.
    Returns (new_names {name: app_handle}, app_updates {handle: state_dict})."""
    new_names, updates = {}, {}
    apps_done, bad_batches = 0, 0

    with ThreadPoolExecutor(max_workers=APP_SCRAPE_WORKERS) as executor:
        idx = 0
        while idx < len(plan):
            if (len(new_names) >= NAMES_TARGET_PER_RUN or apps_done >= MAX_APPS_PER_RUN
                    or time.time() > deadline):
                break

            batch = plan[idx: idx + APP_BATCH_SIZE]
            idx += APP_BATCH_SIZE
            futures = {executor.submit(scrape_app, h, pages, deadline): (h, kind) for h, pages, kind in batch}
            errors_in_batch = 0

            for future in as_completed(futures):
                handle, kind = futures[future]
                apps_done += 1
                try:
                    names, pages, status = future.result()
                except Exception as e:
                    logger.error("App scrape error %s: %s", handle, str(e))
                    names, pages, status = [], 0, "error"

                if status == "error":
                    errors_in_batch += 1
                    if kind == "new":
                        updates[handle] = {"status": "error", "pages": pages, "names": len(names), "last": now_str()}
                elif kind == "refresh" and status != "done":
                    pass   # purani app ka refresh khali aaya — state waise hi rehne do
                else:
                    updates[handle] = {"status": status, "pages": pages, "names": len(names), "last": now_str()}

                for n in names:
                    if (norm(n) and name_key(n) not in seen and n not in new_names
                            and guess_domains_from_name(n)):
                        new_names[n] = handle

            bad_batches = bad_batches + 1 if errors_in_batch == len(batch) else 0
            if bad_batches >= 3:
                logger.warning(
                    "Lagataar 3 batches fail (rate-limit / block?) — is run me scraping rok raha hun, "
                    "agli run me dobara try hoga."
                )
                break

    logger.info("Apps scraped this run: %s | new store names: %s", apps_done, len(new_names))
    return new_names, updates


# ============================================================
# WEBSITE FETCH + VERIFICATION
# ============================================================

def fetch_page(url):
    result = bounded_get(url, hard_timeout=min(NETWORK_HARD_TIMEOUT_SECONDS, max(REQUEST_TIMEOUT, 10)), retries=1)
    if result is None:
        return "", url
    if result.status_code >= 400:
        return "", result.url
    return result.text, result.url


def detect_shopify(html):
    if not html:
        return False
    text = html.lower()
    indicators = [
        "cdn.shopify.com", "shopifycdn.com", "shopify.theme",
        "shopify.shop", "myshopify.com", "shopify-section",
        "shopify-payment-button",
    ]
    return any(ind in text for ind in indicators)


def is_password_page(html, final_url):
    """Password-protected / 'opening soon' store — asli live store nahi."""
    if urlparse(final_url).path.rstrip("/") == "/password":
        return True
    return bool(re.search(r'<form[^>]+action=["\']/password["\']', html or "", re.I))


def page_identity_text(html):
    """Page title + og tags (name-match ke liye). Domain/handle jaan-boojh kar shamil nahi —
    wo guess se bane hote hain, isliye unka match hona koi saboot nahi."""
    parts = []
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        parts.append(m.group(1))
    for tag in re.finditer(
        r'<meta[^>]+(?:property|name)=["\'](?:og:site_name|og:title|application-name)["\'][^>]*>',
        html, re.I
    ):
        c = re.search(r'content=["\']([^"\']*)["\']', tag.group(0), re.I)
        if c:
            parts.append(c.group(1))
    return html_lib.unescape(" ".join(parts))


def name_matches_page(store_name, final_domain, html):
    """Review wale store name ka page title / og:site_name se match hona (ya custom
    domain ka name se milna). Isse 'mystore.myshopify.com' jaisi galat guesses reject hoti hain.
    Domain ko akela saboot nahi mana jata kyunke domain naam se hi guess hua tha."""
    full, core, tokens = name_keys(store_name)
    if not full and not core:
        return True

    identity = page_identity_text(html)
    hay = norm(identity)
    id_tokens = set(re.findall(r"[a-z0-9]+", identity.lower()))

    def hit(key):
        if not key:
            return False
        if len(key) >= 4:
            return key in hay
        return key in id_tokens          # chhote naam: poora word match hona chahiye

    if hit(core) or hit(full):
        return True
    if tokens and all(t in hay for t in tokens):
        return True

    # Custom domain (myshopify nahi) jo brand name se milta ho — title generic ho tab bhi.
    if not final_domain.endswith(".myshopify.com"):
        label = domain_label(final_domain)
        for key in (core, full):
            if key and len(key) >= 4 and (key in label or (len(label) >= 4 and label in key)):
                return True
    return False


def detect_theme(html):
    if not html:
        return "Unknown"
    block = re.search(r'Shopify\.theme\s*=\s*(\{[^}]*\})', html)
    if block:
        for key in ("schema_name", "name"):
            m = re.search(r'"%s"\s*:\s*"([^"]+)"' % key, block.group(1))
            if m:
                return m.group(1)
    m = re.search(r'"theme_name"\s*:\s*"([^"]+)"', html, re.I)
    if m:
        return m.group(1)
    return "Unknown"


def get_product_count(base_url):
    url = base_url.rstrip("/") + "/products.json?limit=250"
    result = bounded_get(url, hard_timeout=min(NETWORK_HARD_TIMEOUT_SECONDS, max(REQUEST_TIMEOUT, 10)), retries=1)
    if result is None or result.status_code != 200:
        return 0
    try:
        return len(json.loads(result.text).get("products", []))
    except Exception:
        return 0


# ============================================================
# EMAIL EXTRACTION (mandatory for every lead)
# ============================================================

EMAIL_REGEX = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24}", re.I)
MAILTO_REGEX = re.compile(r'mailto:([^"\'?\s<>&]+)', re.I)
CF_EMAIL_REGEX = re.compile(r'data-cfemail="([0-9a-f]+)"|email-protection#([0-9a-f]+)', re.I)
OBFUSCATED_AT = re.compile(r"\s*[\[\(\{]\s*at\s*[\]\)\}]\s*", re.I)
OBFUSCATED_DOT = re.compile(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", re.I)

JUNK_EMAIL_DOMAINS = (
    "example.com", "domain.com", "yourdomain.com", "yourstore.com", "email.com",
    "mysite.com", "mydomain.com", "test.com", "sentry.io", "wixpress.com",
    "shopify.com", "myshopify.com", "shopifyapps.com", "shopifycloud.com",
    "klaviyo.com", "cloudflare.com", "schema.org", "w3.org", "apple.com",
    "google.com", "googleapis.com", "gstatic.com", "facebook.com", "twitter.com",
    "instagram.com", "paypal.com", "godaddy.com", "judge.me", "loox.io",
    "privy.com", "smile.io", "recart.com", "gorgias.com", "zendesk.com",
    "freshdesk.com", "intercom.io", "mailchimp.com", "hubspot.com", "wix.com",
    "squarespace.com", "wordpress.com", "jsdelivr.net", "cloudfront.net",
)
JUNK_LOCALPARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon",
    "postmaster", "john.doe", "jane.doe", "your.name", "yourname", "name",
    "email", "user", "username", "someone", "example", "test",
}
BAD_TLDS = {
    "png", "jpg", "jpeg", "gif", "webp", "svg", "css", "js", "woff", "woff2",
    "ttf", "eot", "ico", "mp4", "json", "map", "php", "html",
}
FREE_PROVIDERS = {
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "outlook.com",
    "hotmail.com", "hotmail.co.uk", "live.com", "msn.com", "icloud.com",
    "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com",
    "zoho.com",
}
PREFERRED_LOCALPARTS = (
    "info", "contact", "hello", "support", "care", "sales", "help", "team",
    "customercare", "customerservice", "orders", "shop", "admin",
)


def clean_email(raw):
    e = html_lib.unescape(raw).strip().strip(".,;:'\"<>()[]").lower()
    return re.sub(r"^(u003e|u003c)", "", e)


def is_valid_email(e):
    if e.count("@") != 1 or len(e) > 80 or ".." in e:
        return False
    local, domain = e.split("@")
    if not local or not domain or local in JUNK_LOCALPARTS:
        return False
    if domain.rsplit(".", 1)[-1] in BAD_TLDS:
        return False
    return not any(domain == bad or domain.endswith("." + bad) for bad in JUNK_EMAIL_DOMAINS)


def decode_cf_email(hex_str):
    try:
        key = int(hex_str[:2], 16)
        return "".join(chr(int(hex_str[i:i + 2], 16) ^ key) for i in range(2, len(hex_str), 2))
    except Exception:
        return ""


def collect_emails(raw_text):
    """Returns {email: trusted_bool} (trusted = mailto / Cloudflare-decoded)."""
    found = {}
    if not raw_text:
        return found

    def add(candidate, trusted):
        e = clean_email(candidate)
        if e and is_valid_email(e):
            found[e] = found.get(e, False) or trusted

    for match in MAILTO_REGEX.findall(raw_text):
        for part in unquote(match).split(","):
            add(part, True)

    for m in CF_EMAIL_REGEX.finditer(raw_text):
        add(decode_cf_email(m.group(1) or m.group(2)), True)

    text = html_lib.unescape(raw_text)
    text = OBFUSCATED_AT.sub("@", text)
    text = OBFUSCATED_DOT.sub(".", text)
    for match in EMAIL_REGEX.findall(text):
        add(match, False)

    return found


def domain_label(d):
    parts = d.split(".")
    if d.endswith(".myshopify.com"):
        return norm(parts[0])
    if len(parts) >= 3 and len(parts[-1]) == 2 and parts[-2] in {"co", "com", "org", "net", "gov", "ac", "edu"}:
        return norm(parts[-3])
    if len(parts) >= 2:
        return norm(parts[-2])
    return norm(parts[0])


def is_related(email_domain, store_domain, keys):
    if (email_domain == store_domain or email_domain.endswith("." + store_domain)
            or store_domain.endswith("." + email_domain)):
        return True
    a, b = domain_label(email_domain), domain_label(store_domain)
    if not a:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) >= 4 and (a in b or b in a):
        return True
    for k in keys:
        if len(k) >= 4 and (k in a or (len(a) >= 4 and a in k)):
            return True
    return False


def rank_emails(found, store_domain, store_name):
    """Best email pehle. Store se unrelated (aur free-provider bhi nahi, aur mailto
    bhi nahi) emails — jaise theme/agency/plugin wali — drop ho jati hain."""
    full, core, _ = name_keys(store_name)
    keys = [k for k in (core, full) if k]

    scored = []
    for order, (email, trusted) in enumerate(found.items()):
        local, dom = email.split("@")
        related = is_related(dom, store_domain, keys)
        free = dom in FREE_PROVIDERS
        if not related and not free and not trusted:
            continue
        score = 0
        if related:
            score += 3
        if trusted:
            score += 2
        if local in PREFERRED_LOCALPARTS:
            score += 1
        scored.append((-score, order, email))

    scored.sort()
    return [e for _, _, e in scored]


# ------------------------------------------------------------
# DNS / MX verification — FIXED (see CHANGELOG v3.1 above)
# ------------------------------------------------------------
# Ye pehle @lru_cache use karti thi, jiska ek SINGLE GLOBAL LOCK hota hai —
# concurrent cache-misses (chahe alag-alag domains ke liye) ek-dusre ko
# BLOCK karte the. Agar ek domain ki DNS lookup kahin phas jaye, saare
# workers isi lock ke peeche queue ho kar poori script ko freeze kar dete
# the. Ab: (1) cache sirf ek dict+lock hai jo sirf read/write lock karta hai,
# DNS call ko nahi, aur (2) har lookup ek dedicated thread-pool me chalti hai
# jis par HARD wall-clock timeout (future.result(timeout=...)) lagaya gaya
# hai — dnspython internally kuch bhi kare, ye kabhi DNS_HARD_TIMEOUT_SECONDS
# se zyada nahi rukegi.

_mx_cache = {}
_mx_cache_lock = threading.Lock()
_dns_executor = ThreadPoolExecutor(max_workers=DNS_WORKERS, thread_name_prefix="dns-mx") if DNS_AVAILABLE else None


def _resolve_has_mx(domain):
    """Actual (potentially slow) DNS work — runs inside _dns_executor."""
    resolver = dns.resolver.Resolver()
    resolver.timeout = 3       # per-nameserver attempt
    resolver.lifetime = 4      # total across all nameservers
    try:
        resolver.resolve(domain, "MX")
        return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
        return False
    except dns.resolver.NoAnswer:
        try:
            resolver.resolve(domain, "A")
            return True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            return False
        except Exception:
            return True
    except Exception:
        return True   # timeout waghera — transient error par email drop nahi karte


def domain_has_mx(domain):
    """Bounce se bachne ke liye: domain email receive kar sakta hai?
    Hard wall-clock capped — kabhi bhi poore pipeline ko block nahi karti."""
    if not (VERIFY_MX and DNS_AVAILABLE):
        return True

    with _mx_cache_lock:
        cached = _mx_cache.get(domain)
    if cached is not None:
        return cached

    try:
        future = _dns_executor.submit(_resolve_has_mx, domain)
        result = future.result(timeout=DNS_HARD_TIMEOUT_SECONDS)
    except FutureTimeoutError:
        # Lookup abhi bhi background me chal rahi hogi (thread pool me) — hum
        # aage badh jaate hain aur transient treat karte hain (drop nahi karte).
        result = True
    except Exception:
        result = True

    with _mx_cache_lock:
        _mx_cache[domain] = result
    return result


def discover_contact_links(html, base_url):
    links = []
    base_netloc = urlparse(base_url).netloc
    for href in re.findall(r'href=["\']([^"\'#]+)["\']', html or "", re.I):
        if not any(k in href.lower() for k in ("contact", "about", "support", "help")):
            continue
        parsed = urlparse(urljoin(base_url, href))
        path = parsed.path
        if parsed.netloc != base_netloc or not path or path == "/":
            continue
        if any(x in path for x in ("/products/", "/collections/", "/cdn/")):
            continue
        if path.endswith((".jpg", ".png", ".css", ".js", ".svg", ".webp", ".pdf")):
            continue
        links.append(path)
    return list(dict.fromkeys(links))[:4]


def fetch_extra_pages(base_url, homepage_html, deadline=None):
    """Contact/about/policy pages fetch — email + feature detection dono isi se.
    Speedup + safety: (1) trusted email (mailto/Cloudflare) milte hi ruk jata hai,
    baqi pages fetch nahi hoti — zyadatar stores 1-2 pages ke baad hi mil jati hain.
    (2) deadline diya ho to har page se pehle check hota hai, taake ek slow store
    baqi 25 workers ke saath poora run atka na sake. (3) har individual page fetch
    khud bhi trickle-safe hai (bounded_get)."""
    pages = [
        "/pages/contact", "/pages/contact-us", "/contact", "/pages/get-in-touch",
        "/pages/about", "/about", "/pages/about-us", "/pages/faq", "/pages/support",
        "/policies/contact-information", "/policies/refund-policy",
        "/policies/shipping-policy", "/policies/privacy-policy",
        "/policies/terms-of-service",
    ]
    pages += discover_contact_links(homepage_html, base_url)
    pages = list(dict.fromkeys(pages))[:10]

    combined = homepage_html or ""
    if any(v for v in collect_emails(combined).values()):
        return combined   # homepage par hi trusted email mil gayi

    for path in pages:
        if deadline is not None and time.time() > deadline:
            break
        try:
            page_html, _ = fetch_page(base_url.rstrip("/") + path)
            if page_html:
                combined += "\n" + page_html
                if any(v for v in collect_emails(page_html).values()):
                    break   # trusted email mil gayi — baqi pages fetch karne ki zaroorat nahi
        except Exception:
            pass
    return combined


# ============================================================
# FEATURE DETECTION + SCORING
# ============================================================

def has_any(text, keywords):
    text = text.lower()
    return any(k.lower() in text for k in keywords)


def detect_features(html):
    text = html.lower()
    return {
        "FAQ": has_any(text, ["faq", "frequently asked questions"]),
        "Testimonials": has_any(text, ["testimonial", "what our customers say"]),
        "Reviews": has_any(text, ["customer reviews", "reviews", "judge.me", "loox", "yotpo"]),
        "Sticky Add To Cart": has_any(text, ["sticky add to cart", "sticky-atc"]),
        "Size Guide": has_any(text, ["size guide", "size chart"]),
        "Newsletter": has_any(text, ["newsletter", "subscribe to our"]),
        "WhatsApp": has_any(text, ["whatsapp", "wa.me"]),
        "Countdown": has_any(text, ["countdown", "limited time"]),
        "Instagram": has_any(text, ["instagram.com", "instagram-feed"]),
    }


def build_reasons(theme, product_count, features, email):
    reasons, recommendations = [], []

    if theme == "Unknown":
        reasons.append("Theme could not be identified")
    else:
        reasons.append(f"Uses theme: {theme}")

    if product_count == 0:
        reasons.append("Product catalog could not be detected")
    elif product_count < 10:
        reasons.append(f"Small product catalog ({product_count} products)")
    elif product_count >= 50:
        reasons.append(f"Larger product catalog ({product_count} products)")

    checks = [
        ("FAQ", "No obvious FAQ section detected", "FAQ"),
        ("Testimonials", "No obvious testimonials section detected", "Testimonials"),
        ("Reviews", "No obvious review system detected", "Reviews"),
        ("Sticky Add To Cart", "No obvious sticky Add to Cart detected", "Sticky Add to Cart"),
        ("Size Guide", "No obvious size guide detected", "Size Guide"),
        ("Newsletter", "No obvious newsletter signup detected", "Newsletter"),
        ("WhatsApp", "No obvious WhatsApp contact button detected", "Sticky WhatsApp"),
    ]
    for key, reason_text, rec in checks:
        if not features[key]:
            reasons.append(reason_text)
            recommendations.append(rec)

    if not features["Countdown"]:
        recommendations.append("Countdown Timer")
    if not features["Instagram"]:
        recommendations.append("Instagram Gallery")

    if email:
        reasons.append("Business contact email found")

    return reasons, recommendations


def calculate_score(product_count, features, email, theme):
    score = 0
    if theme != "Unknown":
        score += 10
    if product_count >= 50:
        score += 20
    elif product_count >= 20:
        score += 15
    elif product_count >= 10:
        score += 10
    elif product_count > 0:
        score += 5

    for key, pts in [("FAQ", 10), ("Testimonials", 10), ("Reviews", 10),
                     ("Sticky Add To Cart", 10), ("Size Guide", 5),
                     ("Newsletter", 5), ("WhatsApp", 5)]:
        if not features[key]:
            score += pts

    if email:
        score += 15

    return min(score, 100)


def score_quality(score):
    if score >= 75:
        return "HOT"
    if score >= 55:
        return "WARM"
    if score >= 35:
        return "GOOD"
    return "LOW"


# ============================================================
# ANALYZE ONE DOMAIN  ->  (status, data)
#   ok / no_email / not_shopify / password / name_mismatch / no_products / error
# ============================================================

def analyze_store(domain, store_name, source_app, deadline=None):
    try:
        html, final_url = fetch_page(normalize_url(domain))
        if not html or not detect_shopify(html):
            return "not_shopify", None

        final_domain = normalize_domain(final_url)
        if not final_domain or is_bad_domain(final_domain):
            return "not_shopify", None

        if is_password_page(html, final_url):
            return "password", None

        if STRICT_NAME_MATCH and not name_matches_page(store_name, final_domain, html):
            return "name_mismatch", None

        if deadline is not None and time.time() > deadline:
            return "timeout", None

        base_url = normalize_url(final_url)

        product_count = get_product_count(base_url)
        if product_count < MIN_PRODUCTS:
            return "no_products", None

        if deadline is not None and time.time() > deadline:
            return "timeout", None

        combined_text = fetch_extra_pages(base_url, html, deadline)

        ranked = rank_emails(collect_emails(combined_text), final_domain, store_name)
        email = next((e for e in ranked if domain_has_mx(e.split("@")[1])), "")

        source = f"App Store reviews: {source_app}"

        if not email:
            return "no_email", {
                "Store Name": store_name,
                "Store URL": base_url,
                "Domain": final_domain,
                "Products": product_count,
                "Source": source,
            }

        other_emails = [e for e in ranked if e != email][:3]
        theme = detect_theme(html)
        features = detect_features(combined_text)
        reasons, recommendations = build_reasons(theme, product_count, features, email)
        score = calculate_score(product_count, features, email, theme)

        return "ok", {
            "Store Name": store_name,
            "Store URL": base_url,
            "Domain": final_domain,
            "Email": email,
            "Other Emails": ", ".join(other_emails),
            "Theme": theme,
            "Products": product_count,
            "Lead Score": score,
            "Quality": score_quality(score),
            "FAQ": "YES" if features["FAQ"] else "NO",
            "Testimonials": "YES" if features["Testimonials"] else "NO",
            "Reviews": "YES" if features["Reviews"] else "NO",
            "Sticky Add To Cart": "YES" if features["Sticky Add To Cart"] else "NO",
            "Size Guide": "YES" if features["Size Guide"] else "NO",
            "Newsletter": "YES" if features["Newsletter"] else "NO",
            "WhatsApp": "YES" if features["WhatsApp"] else "NO",
            "Reason": " | ".join(reasons),
            "Recommended Sections": ", ".join(recommendations),
            "Source": source,
        }

    except Exception as e:
        logger.debug("Store analysis failed %s: %s", domain, str(e))
        return "error", None


def process_store_name(name, handle, guesses, deadline):
    """Ek store name ke saare domain guesses sequentially try karta hai aur pehli asli hit
    (ok ya no_email) par ruk jata hai. Har guess ko STORE_TIME_BUDGET_SECONDS se zyada waqt
    nahi milta — pehle ye bandish nahi thi, is liye ek slow/heavy store worker ko kai minute
    tak roke rakh sakta tha, jab tak baaqi saare 25 workers bhi similar slow stores par
    na ja atken aur log kaafi der ke liye chup ho jaye. Global time budget khatam ho to
    'timeout' (name seen nahi banta, agli run me dobara try hota hai).
    Returns (status, data, reasons_counter)."""
    reasons = Counter()
    for domain in guesses:
        if time.time() > deadline:
            return "timeout", None, reasons
        guess_deadline = min(deadline, time.time() + STORE_TIME_BUDGET_SECONDS)
        status, data = analyze_store(domain, name, handle, guess_deadline)
        if status in ("ok", "no_email"):
            return status, data, reasons
        reasons[status] += 1
    return "not_found", None, reasons


# ============================================================
# GOOGLE SHEETS  (user-created sheet + service account Editor access)
# ============================================================

def parse_sheet_id(value):
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", value)
    return m.group(1) if m else value.strip()


def get_or_create_tab(spreadsheet, title, headers):
    """Tab (worksheet) tayyar karta hai. Purane columns wali tab rename ho jati hai (data safe)."""
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        sheets = spreadsheet.worksheets()
        if len(sheets) == 1 and not sheets[0].row_values(1) and title == SHEET_NAME:
            ws = sheets[0]                       # nayi sheet ka khali default tab reuse
            ws.update_title(title)
        else:
            ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))

    header = ws.row_values(1)
    if header == headers:
        return ws

    if header:
        old_title = f"{title}_old_{time.strftime('%Y%m%d_%H%M%S')}"
        ws.update_title(old_title)
        logger.warning("Tab '%s' ke columns purane the — '%s' naam se save, nayi tab ban rahi hai.", title, old_title)
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers))

    ws.update(values=[headers], range_name="A1")
    try:
        ws.freeze(rows=1)
    except Exception:
        pass
    return ws


def connect_sheets():
    """Returns SheetTabs(leads, no_email, apps, checked) ya None."""
    if not GSPREAD_AVAILABLE:
        logger.error("Run: pip install gspread google-auth")
        return None
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.error("GOOGLE_SERVICE_ACCOUNT_JSON env var set nahi hai.")
        return None
    if not GOOGLE_SHEET_ID:
        logger.error(
            "GOOGLE_SHEET_ID set nahi hai. Service account khud sheet create nahi kar sakta "
            "(403 'Drive storage quota exceeded'). Fix: (1) apne Google Drive me ek khali sheet "
            "banayein, (2) usay service account ki email par Editor share karein, (3) sheet ka "
            "URL ya ID GOOGLE_SHEET_ID me daal dein."
        )
        return None

    client_email = "?"
    try:
        info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        client_email = info.get("client_email", "?")
        credentials = Credentials.from_service_account_info(
            info,
            scopes=[
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ],
        )
        client = gspread.authorize(credentials)
        logger.info("Service account: %s", client_email)

        spreadsheet = client.open_by_key(parse_sheet_id(GOOGLE_SHEET_ID))
        logger.info("Connected to Google Sheet: %s", spreadsheet.url)

        return SheetTabs(
            leads=get_or_create_tab(spreadsheet, SHEET_NAME, HEADERS),
            no_email=get_or_create_tab(spreadsheet, NO_EMAIL_SHEET_NAME, NO_EMAIL_HEADERS),
            apps=get_or_create_tab(spreadsheet, APPS_SHEET_NAME, APPS_HEADERS),
            checked=get_or_create_tab(spreadsheet, CHECKED_SHEET_NAME, CHECKED_HEADERS),
        )

    except json.JSONDecodeError:
        logger.error("GOOGLE_SERVICE_ACCOUNT_JSON valid JSON nahi — key file ka poora content paste karein, path nahi.")
    except gspread.exceptions.SpreadsheetNotFound:
        logger.error(
            "Sheet nahi mili ya share nahi hui. Sheet ko %s par Editor share karein "
            "aur GOOGLE_SHEET_ID check karein.", client_email
        )
    except Exception as e:
        logger.error(
            "Google Sheet connection failed: %s | Check: Sheets API + Drive API enable hain? "
            "Sheet %s ko Editor share hui?", str(e), client_email
        )
    return None


def load_tab_column(ws, headers, column_name):
    try:
        idx = headers.index(column_name) + 1
        return [v.strip() for v in ws.col_values(idx)[1:] if v.strip()]
    except Exception as e:
        logger.warning("Could not read '%s' from sheet: %s", column_name, str(e))
        return []


def save_rows_to_sheet(ws, headers, records, label):
    if not ws or not records:
        return
    rows = [[r.get(h, "") for h in headers] for r in records]
    for attempt in range(4):
        try:
            ws.append_rows(rows, value_input_option="RAW")   # RAW: '=...' wale naam formula nahi banenge
            logger.info("Saved %s rows to Google Sheet tab '%s'.", len(rows), label)
            return
        except Exception as e:
            logger.error("Sheet save attempt %s failed (%s): %s", attempt + 1, label, str(e))
            time.sleep(5 * (attempt + 1))
    logger.error("Sheet me save nahi ho saka (%s) — leads ki copy local CSV me hai.", label)


def load_apps_state(apps_ws):
    """Apps tab -> {handle: {status, pages, names, last, row}}"""
    state = {}
    try:
        rows = apps_ws.get_all_values()
    except Exception as e:
        logger.error("Could not read Apps tab: %s", str(e))
        return state
    for row_no, row in enumerate(rows[1:], start=2):
        row = list(row) + [""] * (len(APPS_HEADERS) - len(row))
        handle = row[0].strip()
        if handle:
            state[handle] = {"status": row[1].strip(), "pages": row[2], "names": row[3],
                             "last": row[4].strip(), "row": row_no}
    return state


def save_apps_state(apps_ws, state, updates):
    """Purani rows ko batch-update, nayi apps ko append."""
    if not updates:
        return
    new_rows, batch = [], []
    for handle, u in updates.items():
        values = [u["status"], u["pages"], u["names"], u["last"]]
        cur = state.get(handle)
        if cur and cur.get("row"):
            batch.append({"range": f"B{cur['row']}:E{cur['row']}", "values": [values]})
        else:
            new_rows.append([handle] + values)

    try:
        for i in range(0, len(new_rows), 1000):
            apps_ws.append_rows(new_rows[i:i + 1000], value_input_option="RAW")
        for i in range(0, len(batch), 400):
            apps_ws.batch_update(batch[i:i + 400], value_input_option="RAW")
        logger.info("Apps tab updated: %s new, %s updated.", len(new_rows), len(batch))
    except Exception as e:
        logger.error("Could not update Apps tab: %s", str(e))


# ============================================================
# ONE RUN
# ============================================================

def _watchdog_force_exit(minutes):
    """Aakhri safety net: agar koi cheez (unknown bug, network trickle, DNS hang)
    is process ko itni der rok le, to process khud khatam ho jata hai — GitHub
    ke forceful 'cancelled' (jisme koi diagnostic log nahi milta) ka intezar
    nahi karta. Data loss minimal hota hai kyunke leads har CHECKPOINT_INTERVAL
    par pehle hi sheet me save ho chuki hoti hain."""
    time.sleep(minutes * 60)
    logger.error(
        "HARD TIMEOUT (%.0f min) — process kahin atka hua tha, isliye zabardasti "
        "exit kar raha hun. Ab tak jo leads mil chuki thin wo already save hain. "
        "Agli run automatically aage se shuru hogi.",
        minutes
    )
    logging.shutdown()
    os._exit(1)


def run_once():
    """Returns 0 on success, 1 on setup failure."""
    t0 = time.time()
    scrape_deadline = t0 + SCRAPE_BUDGET_MINUTES * 60
    run_deadline = t0 + RUN_TIME_BUDGET_MINUTES * 60
    hard_minutes = HARD_TIMEOUT_MINUTES if HARD_TIMEOUT_MINUTES > 0 else (RUN_TIME_BUDGET_MINUTES + 5)

    watchdog = threading.Thread(target=_watchdog_force_exit, args=(hard_minutes,), daemon=True)
    watchdog.start()

    logger.info("=" * 70)
    logger.info(
        "SHOPIFY LEAD FINDER v3.2 — dynamic apps | email mandatory | budget %s min | hard timeout %s min",
        RUN_TIME_BUDGET_MINUTES, hard_minutes
    )
    logger.info(
        "NOTE: agar GitHub Actions me chala rahe ho, workflow 'timeout-minutes' ko %s se zyada rakho, "
        "warna GitHub ka forceful cancel humare apne watchdog se pehle chal sakta hai.",
        hard_minutes
    )
    logger.info("=" * 70)

    ensure_data_dir()
    ensure_csv_schema_matches(LEADS_CSV_FILE, HEADERS)
    ensure_csv_schema_matches(NO_EMAIL_CSV_FILE, NO_EMAIL_HEADERS)

    tabs = connect_sheets()
    if tabs is None:
        if REQUIRE_SHEET:
            logger.error("Sheet connect nahi hui, isliye run ruk gaya (REQUIRE_SHEET=0 set karein to CSV-only chalega).")
            return 1
        logger.warning("Sheet ke baghair CSV-only mode me chal raha hun: %s", LEADS_CSV_FILE)

    # ---------------- Already-processed data ----------------
    seen = load_seen()                       # entries: "name:<normalized store name>"
    found_final_domains = set()

    if tabs is not None:
        apps_state = load_apps_state(tabs.apps)
        checked_keys = load_tab_column(tabs.checked, CHECKED_HEADERS, "Key")
        lead_names = load_tab_column(tabs.leads, HEADERS, "Store Name")
        ne_names = load_tab_column(tabs.no_email, NO_EMAIL_HEADERS, "Store Name")
        lead_domains = load_tab_column(tabs.leads, HEADERS, "Domain")
        ne_domains = load_tab_column(tabs.no_email, NO_EMAIL_HEADERS, "Domain")
        seen |= {f"name:{k}" for k in checked_keys}
        seen |= {name_key(n) for n in lead_names + ne_names}
        found_final_domains |= {d.lower() for d in lead_domains + ne_domains}
        logger.info(
            "Sheet me pehle se: %s leads | %s no-email | %s checked names | %s apps tracked",
            len(lead_domains), len(ne_domains), len(checked_keys), len(apps_state)
        )
    else:
        apps_state = {h: dict(s) for h, s in load_local_apps_state().items()}

    logger.info("Already processed store names (will skip): %s", len(seen))

    # ---------------- DYNAMIC APP DISCOVERY + REVIEW SCRAPING ----------------
    if not BS4_AVAILABLE:
        logger.error("beautifulsoup4 install nahi hai. Run: pip install beautifulsoup4")
        return 1

    sitemap_apps = discover_app_handles()
    if not sitemap_apps:
        logger.error("Shopify App Store se koi app nahi mili (network / block?). Agli run me dobara try hoga.")
        return 1

    plan, plan_counts = plan_apps(sitemap_apps, apps_state)
    logger.info(
        "App queue -> retry: %s | never scraped: %s | previously errored: %s | refresh (purani apps): %s",
        plan_counts["retry"], plan_counts["new"], plan_counts["error"], plan_counts["refresh"]
    )

    new_names, app_updates = collect_new_names(plan, seen, scrape_deadline)

    # Apps ki state PEHLE save (scraping ho chuki hai) — retry wali baad me overwrite hongi.
    todo = []
    for name, handle in new_names.items():
        guesses = [d for d in guess_domains_from_name(name) if not is_bad_domain(d)]
        if guesses:
            todo.append((name, handle, guesses))

    logger.info("Store names to analyze this run: %s", len(todo))

    # ---------------- ANALYSIS ----------------
    leads, no_email_rows, checked_rows = [], [], []
    stats = Counter()
    reason_stats = Counter()
    completed_names = set()
    completed = 0

    def flush():
        nonlocal leads, no_email_rows, checked_rows
        if leads:
            append_csv(LEADS_CSV_FILE, HEADERS, leads)
            if tabs is not None:
                save_rows_to_sheet(tabs.leads, HEADERS, leads, SHEET_NAME)
            leads = []
        if no_email_rows:
            append_csv(NO_EMAIL_CSV_FILE, NO_EMAIL_HEADERS, no_email_rows)
            if tabs is not None:
                save_rows_to_sheet(tabs.no_email, NO_EMAIL_HEADERS, no_email_rows, NO_EMAIL_SHEET_NAME)
            no_email_rows = []
        if checked_rows:
            if tabs is not None:
                save_rows_to_sheet(tabs.checked, CHECKED_HEADERS, checked_rows, CHECKED_SHEET_NAME)
            checked_rows = []
        save_seen(seen)

    if todo:
        progress = {"done": 0, "total": len(todo)}
        stop_heartbeat = threading.Event()

        def _heartbeat():
            # Sirf visibility ke liye — koi logic yahan nahi. Ab se log kabhi
            # HEARTBEAT_SECONDS se zyada der chup nahi rahega, chahe koi store
            # slow ho ya sab workers busy hon — pehle iski koi khabar nahi milti thi.
            while not stop_heartbeat.wait(HEARTBEAT_SECONDS):
                logger.info(
                    "... jaari hai: %s/%s store names complete (kaam chal raha hai, atka nahi) | "
                    "http-pool: %s threads active",
                    progress["done"], progress["total"], threading.active_count()
                )

        threading.Thread(target=_heartbeat, daemon=True).start()

        executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        try:
            futures = {
                executor.submit(process_store_name, name, handle, guesses, run_deadline): name
                for name, handle, guesses in todo
            }

            for future in as_completed(futures):
                name = futures[future]

                try:
                    status, data, reasons = future.result()
                except Exception as e:
                    logger.error("Analysis error for %s: %s", name, str(e))
                    status, data, reasons = "timeout", None, Counter()

                if status == "timeout":
                    continue                      # seen nahi banega -> agli run me dobara

                completed += 1
                progress["done"] = completed
                completed_names.add(name)
                reason_stats.update(reasons)

                # Transient errors wale names ko 'seen' nahi banate taake agli run me dobara try hon.
                if not reasons.get("error"):
                    seen.add(name_key(name))
                    checked_rows.append({"Key": norm(name), "Store Name": name, "Checked": now_str()})

                if status == "ok":
                    domain = data["Domain"].lower()
                    if domain in found_final_domains:
                        stats["duplicate"] += 1
                    else:
                        found_final_domains.add(domain)
                        leads.append(data)
                        stats["leads"] += 1
                        logger.info(
                            "[%s/%s] LEAD: %s | %s | %s | Score %s",
                            completed, len(todo), data["Domain"], data["Email"],
                            data["Quality"], data["Lead Score"]
                        )
                elif status == "no_email":
                    domain = data["Domain"].lower()
                    if domain in found_final_domains:
                        stats["duplicate"] += 1
                    else:
                        found_final_domains.add(domain)
                        no_email_rows.append(data)
                        stats["no_email"] += 1
                else:
                    stats["not_found"] += 1

                if completed % CHECKPOINT_INTERVAL == 0:
                    flush()
                    logger.info("Checkpoint saved at %s/%s.", completed, len(todo))

        except KeyboardInterrupt:
            logger.warning("Interrupted — ab tak ka data save kar raha hun...")
            executor.shutdown(wait=False, cancel_futures=True)
        finally:
            stop_heartbeat.set()
            executor.shutdown(wait=False)
            leads.sort(key=lambda x: x.get("Lead Score", 0), reverse=True)
            flush()

    # ---------------- APP STATE SAVE ----------------
    # Jin apps ke kuch names time budget ki wajah se process nahi hue, unhein 'retry' mark karte hain
    # taake agli run me pehle wahi scrape hon (names lose na hon).
    unprocessed = [(n, h) for n, h, _ in todo if n not in completed_names]
    for _, handle in unprocessed:
        if handle in app_updates:
            app_updates[handle]["status"] = "retry"
        else:
            app_updates[handle] = {"status": "retry", "pages": 0, "names": 0, "last": now_str()}

    if tabs is not None:
        save_apps_state(tabs.apps, apps_state, app_updates)
    for handle, u in app_updates.items():
        apps_state[handle] = {**apps_state.get(handle, {}), **u}
    save_local_apps_state({h: {k: v for k, v in s.items() if k != "row"} for h, s in apps_state.items()})

    # ---------------- SUMMARY ----------------
    minutes = (time.time() - t0) / 60
    logger.info("=" * 70)
    logger.info("RUN COMPLETE in %.1f min", minutes)
    logger.info("Apps scraped: %s | new store names found: %s | analyzed: %s", len(app_updates), len(todo), completed)
    logger.info("LEADS saved (real email found): %s", stats["leads"])
    logger.info("Shopify stores but NO email (-> '%s' tab): %s", NO_EMAIL_SHEET_NAME, stats["no_email"])
    logger.info("Names jinka koi valid store nahi mila: %s | Duplicates skipped: %s", stats["not_found"], stats["duplicate"])
    if unprocessed:
        logger.info("Time budget khatam: %s names agli run me (apps 'retry' mark hui).", len(unprocessed))
    if reason_stats:
        logger.info(
            "Guesses reject hone ki wajah -> not_shopify: %s | password: %s | "
            "name_mismatch: %s | no_products: %s | error: %s",
            reason_stats["not_shopify"], reason_stats["password"],
            reason_stats["name_mismatch"], reason_stats["no_products"], reason_stats["error"]
        )
    logger.info("Apps abhi tak kabhi scrape nahi hui (baaqi): %s", max(plan_counts["new"] - sum(
        1 for h, _, k in plan if k == "new" and h in app_updates), 0))
    logger.info("CSV backup: %s", LEADS_CSV_FILE)
    logger.info("=" * 70)
    return 0


# ============================================================
# ENTRY POINT
# ============================================================

def main():
    if not RUN_FOREVER:
        sys.exit(run_once())

    logger.info("Loop mode: har %s minute me ek run.", RUN_EVERY_MINUTES)
    while True:
        started = time.time()
        try:
            run_once()
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            return
        except Exception as e:
            logger.exception("Run crashed: %s", str(e))
        sleep_for = max(60, RUN_EVERY_MINUTES * 60 - (time.time() - started))
        logger.info("Agli run %.0f minute baad.", sleep_for / 60)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            logger.info("Stopped by user.")
            return


if __name__ == "__main__":
    main()
