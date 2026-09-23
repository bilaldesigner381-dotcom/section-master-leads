"""
SHOPIFY LEAD INTELLIGENCE ENGINE — FREE / HIGH-VOLUME VERSION
================================================================
Discovery is powered by Common Crawl (https://commoncrawl.org), a public,
free, unlimited dataset of internet crawls. No API key, no daily quota.
We query it for URLs under *.myshopify.com, which are guaranteed-real
Shopify stores. A single Common Crawl index page can return 10,000+ URLs,
so a handful of pages easily gives you thousands of unique candidate
domains per run — something Google CSE's 100/day free quota can never do.

Google Custom Search (if you have keys) is kept as an OPTIONAL bonus
source layered on top — not required.

Output:
  - Always writes to a local CSV (works with zero configuration)
  - Also writes to Google Sheets IF you set the Sheets env vars

Run:  python shopify_lead_finder.py
"""

import os
import re
import csv
import time
import json
import logging
import requests

from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

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


# ============================================================
# CONFIG
# ============================================================

# --- Common Crawl discovery settings ---
CC_COLLINFO_URL = "https://index.commoncrawl.org/collinfo.json"
CC_NUM_INDEXES_TO_USE = int(os.environ.get("CC_NUM_INDEXES", "2"))      # how many recent crawl snapshots to query
CC_MAX_PAGES_PER_INDEX = int(os.environ.get("CC_MAX_PAGES", "15"))      # each page ≈ thousands of URLs
CC_URL_PATTERN = os.environ.get("CC_URL_PATTERN", "myshopify.com")     # domain to match (no wildcard needed)
CC_PAGE_WORKERS = int(os.environ.get("CC_PAGE_WORKERS", "6"))

# --- How many NEW candidate domains to actually analyze this run ---
MAX_DOMAINS_PER_RUN = int(os.environ.get("MAX_DOMAINS_PER_RUN", "3000"))

# --- Analysis concurrency ---
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "25"))
REQUEST_TIMEOUT = 12

# --- Optional Google CSE (bonus source, not required) ---
GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "").strip()
USE_GOOGLE_CSE = bool(GOOGLE_API_KEY and GOOGLE_CSE_ID)
RESULTS_PER_QUERY = 10
GOOGLE_DELAY_SECONDS = 0.5

# --- Shopify App Store review scraping (bonus source, store-NAME guesses) ---
# Review pages only expose the store's display name + country, not a URL,
# so we scrape names, turn each into 2-4 candidate domains, and let the
# normal analyze_store() step verify/discard them (no extra code needed).
DEFAULT_APP_HANDLES = (
    "judgeme,loox,product-reviews,ali-reviews,vitals,"
    "klaviyo-email-marketing,privy,smile-io,recart,"
    "seal-subscriptions,rebuy-personalization,pushowl-web-push"
)
APP_STORE_HANDLES = [
    h.strip() for h in os.environ.get("APP_STORE_HANDLES", DEFAULT_APP_HANDLES).split(",")
    if h.strip()
]
APP_STORE_MAX_PAGES = int(os.environ.get("APP_STORE_MAX_PAGES", "10"))   # pages per app
APP_STORE_DELAY_SECONDS = float(os.environ.get("APP_STORE_DELAY", "0.4"))

# --- How often (every N analyzed) to flush to CSV + Sheet during a run ---
CHECKPOINT_INTERVAL = int(os.environ.get("CHECKPOINT_INTERVAL", "50"))

# --- Optional Google Sheets output ---
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Leads").strip()

# --- Local persistent files (always used, zero config needed) ---
DATA_DIR = os.environ.get("LEAD_DATA_DIR", "./lead_data")
SEEN_DOMAINS_FILE = os.path.join(DATA_DIR, "seen_domains.json")
LEADS_CSV_FILE = os.path.join(DATA_DIR, "leads.csv")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
logger = logging.getLogger("shopify-lead-finder")


# ============================================================
# LOCAL PERSISTENCE (dedupe across daily runs, works with no setup)
# ============================================================

def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


def load_seen_domains():
    ensure_data_dir()
    if not os.path.exists(SEEN_DOMAINS_FILE):
        return set()
    try:
        with open(SEEN_DOMAINS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen_domains(domains):
    ensure_data_dir()
    try:
        with open(SEEN_DOMAINS_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(domains), f)
    except Exception as e:
        logger.error("Could not save seen_domains.json: %s", str(e))


def append_leads_csv(leads):
    if not leads:
        return
    ensure_data_dir()
    file_exists = os.path.exists(LEADS_CSV_FILE)
    try:
        with open(LEADS_CSV_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=HEADERS)
            if not file_exists:
                writer.writeheader()
            for lead in leads:
                writer.writerow({h: lead.get(h, "") for h in HEADERS})
        logger.info("Appended %s leads to %s", len(leads), LEADS_CSV_FILE)
    except Exception as e:
        logger.error("Could not write CSV: %s", str(e))


# ============================================================
# COMMON CRAWL DISCOVERY (the free, high-volume source)
# ============================================================

def get_recent_cc_indexes(n):
    try:
        resp = requests.get(CC_COLLINFO_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        ids = [item["id"] for item in data if "id" in item]
        return ids[:n]
    except Exception as e:
        logger.error("Could not fetch Common Crawl index list: %s", str(e))
        return []


def query_cc_index_page(index_id, url_pattern, page):
    """
    Uses matchType=domain, which is the documented/reliable way to get a
    domain + all its subdomains from the CC Index Server. (The "*.domain"
    wildcard syntax gets url-encoded by requests into %2A and silently
    matches nothing — that was the bug causing 0 results.)
    """
    base = f"https://index.commoncrawl.org/{index_id}-index"

    # Strip any leading "*." the user might still pass via env var, since
    # matchType=domain already implies "this domain + all subdomains".
    clean_pattern = url_pattern[2:] if url_pattern.startswith("*.") else url_pattern

    params = {
        "url": clean_pattern,
        "matchType": "domain",
        "output": "json",
        "page": page,
    }
    headers = {"User-Agent": "lead-finder/1.0 (research use)"}

    try:
        resp = requests.get(base, params=params, headers=headers, timeout=30)
    except requests.RequestException as e:
        logger.error("CC index request failed (index=%s page=%s): %s", index_id, page, str(e))
        return set(), False

    if resp.status_code == 404:
        # Page beyond the last available page — normal end-of-results signal.
        return set(), False

    if resp.status_code != 200:
        # Log the FIRST failure per index in detail so it's debuggable,
        # instead of silently returning nothing like before.
        logger.error(
            "CC index HTTP %s (index=%s page=%s): %s",
            resp.status_code, index_id, page, resp.text[:300]
        )
        return set(), False

    text = resp.text.strip()
    if not text:
        return set(), False

    domains = set()
    for line in text.split("\n"):
        if not line:
            continue
        try:
            record = json.loads(line)
        except Exception:
            continue
        url = record.get("url", "")
        domain = normalize_domain(url)
        if domain:
            domains.add(domain)

    return domains, True


def discover_via_common_crawl():
    logger.info("Discovering candidates via Common Crawl (free, no quota)...")

    indexes = get_recent_cc_indexes(CC_NUM_INDEXES_TO_USE)

    if not indexes:
        logger.warning("No Common Crawl indexes available. Skipping CC discovery.")
        return set()

    logger.info("Using Common Crawl indexes: %s", indexes)

    # Quick single-page diagnostic BEFORE launching full pagination, so a
    # broken query fails fast with a clear reason instead of silently
    # burning through every page.
    test_domains, test_ok = query_cc_index_page(indexes[0], CC_URL_PATTERN, 0)
    if not test_ok:
        logger.error(
            "Common Crawl diagnostic query returned no usable data. "
            "Check the error above (rate limit / API change / pattern issue)."
        )
    else:
        logger.info(
            "Diagnostic check OK: index %s page 0 returned %s domains.",
            indexes[0], len(test_domains)
        )

    all_domains = set(test_domains)

    for index_id in indexes:
        logger.info("Querying index %s for pattern %s ...", index_id, CC_URL_PATTERN)

        jobs = []
        with ThreadPoolExecutor(max_workers=CC_PAGE_WORKERS) as executor:
            futures = {
                executor.submit(query_cc_index_page, index_id, CC_URL_PATTERN, page): page
                for page in range(CC_MAX_PAGES_PER_INDEX)
            }

            empty_streak = 0

            for future in as_completed(futures):
                domains, ok = future.result()

                if not ok or not domains:
                    empty_streak += 1
                    continue

                all_domains |= domains

        logger.info(
            "Index %s done. Running total unique domains: %s",
            index_id, len(all_domains)
        )

    logger.info("Common Crawl discovery complete: %s unique domains", len(all_domains))
    return all_domains


# ============================================================
# OPTIONAL GOOGLE CSE (bonus, only runs if keys are set)
# ============================================================

NICHES = ["bags", "shoes", "fashion", "jewelry", "beauty", "supplements",
          "fitness", "pet products", "home decor", "coffee", "gifts"]
TEMPLATES = ["new arrivals", "best sellers", "shop now", "collections", "sale"]
MODIFIERS = ["Shopify", "shop", "store", "official"]


def build_query_pool():
    queries = []
    for niche in NICHES:
        for template in TEMPLATES:
            for modifier in MODIFIERS:
                queries.append(f'"{niche}" "{template}" {modifier}')
    return list(dict.fromkeys(queries))


def search_google(query):
    if not USE_GOOGLE_CSE:
        return []

    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "num": RESULTS_PER_QUERY,
    }

    try:
        response = requests.get(GOOGLE_SEARCH_URL, params=params, timeout=30)
    except requests.RequestException:
        return []

    if response.status_code != 200:
        return []

    try:
        data = response.json()
    except Exception:
        return []

    return [item.get("link") for item in data.get("items", []) if item.get("link")]


def discover_via_google():
    if not USE_GOOGLE_CSE:
        logger.info("Google CSE keys not set — skipping (optional bonus source).")
        return set()

    logger.info("Google CSE keys found — running bonus discovery (up to 100 queries/day).")

    queries = build_query_pool()[:90]
    domains = set()

    for i, query in enumerate(queries, start=1):
        urls = search_google(query)
        for url in urls:
            d = normalize_domain(url)
            if d:
                domains.add(d)
        time.sleep(GOOGLE_DELAY_SECONDS)

    logger.info("Google CSE discovery complete: %s domains", len(domains))
    return domains


# ============================================================
# SHOPIFY APP STORE REVIEW SCRAPING (bonus, name-guess based)
# ============================================================

def scrape_app_reviews_page(handle, page):
    """
    Returns a set of store display names found on one review page.
    Structure confirmed against apps.shopify.com's review block:
    div[data-merchant-review] > (meta block, author/location block)
    """
    url = f"https://apps.shopify.com/{handle}/reviews"
    params = {"sort_by": "newest", "page": page}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131 Safari/537.36"
        ),
        "Accept-Language": "en-US;q=0.9",
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=20)
    except requests.RequestException:
        return set()

    if resp.status_code != 200:
        return set()

    soup = BeautifulSoup(resp.text, "html.parser")
    review_blocks = soup.find_all("div", attrs={"data-merchant-review": True})

    names = set()
    for block in review_blocks:
        children = block.find_all("div", recursive=False)
        if len(children) < 2:
            continue

        info_block = children[1]
        author_div = info_block.find("div", class_="tw-text-fg-primary")
        if not author_div:
            continue

        name = author_div.get_text(strip=True)
        if name:
            names.add(name)

    return names


def guess_domains_from_name(name):
    """
    Turns a store display name into a handful of plausible domain guesses.
    These are UNVERIFIED — analyze_store() will fetch each one and only
    keep it if it's a live, real Shopify store. Wrong guesses are dropped
    automatically at zero extra cost.
    """
    base = re.sub(r"[^a-z0-9\s-]", "", name.lower()).strip()
    if not base:
        return set()

    nospace = re.sub(r"\s+", "", base)
    dashed = re.sub(r"\s+", "-", base).strip("-")

    guesses = set()
    if nospace:
        guesses.add(f"{nospace}.myshopify.com")
        guesses.add(f"{nospace}.com")
    if dashed and dashed != nospace:
        guesses.add(f"{dashed}.myshopify.com")
        guesses.add(f"{dashed}.com")

    return guesses


def discover_via_app_store_reviews():
    if not BS4_AVAILABLE:
        logger.warning(
            "beautifulsoup4 not installed — skipping App Store review "
            "discovery. Run: pip install beautifulsoup4"
        )
        return set()

    logger.info(
        "Scraping Shopify App Store reviews for store names (%s apps)...",
        len(APP_STORE_HANDLES)
    )

    all_names = set()

    for handle in APP_STORE_HANDLES:
        logger.info("App: %s", handle)
        found_any_on_app = False

        for page in range(1, APP_STORE_MAX_PAGES + 1):
            names = scrape_app_reviews_page(handle, page)

            if not names:
                break  # ran out of review pages for this app

            all_names |= names
            found_any_on_app = True
            time.sleep(APP_STORE_DELAY_SECONDS)

        if not found_any_on_app:
            logger.warning("No reviews found for app handle: %s (typo or app removed?)", handle)

    logger.info("App Store reviews: %s unique store names collected", len(all_names))

    candidate_domains = set()
    for name in all_names:
        candidate_domains |= guess_domains_from_name(name)

    logger.info(
        "App Store reviews: %s candidate domains generated (unverified guesses, "
        "will be filtered during analysis)",
        len(candidate_domains)
    )

    return candidate_domains


# ============================================================
# URL HELPERS
# ============================================================

def normalize_domain(url):
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def normalize_url(domain_or_url):
    try:
        if domain_or_url.startswith("http://") or domain_or_url.startswith("https://"):
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
    ]
    for bad in bad_domains:
        if domain == bad or domain.endswith("." + bad):
            return True
    return False


# ============================================================
# WEBSITE FETCH
# ============================================================

def fetch_page(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131 Safari/537.36"
        )
    }
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        if response.status_code >= 400:
            return "", response.url
        return response.text, response.url
    except Exception:
        return "", url


# ============================================================
# SHOPIFY / THEME DETECTION
# ============================================================

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


def detect_theme(html):
    if not html:
        return "Unknown"
    text = html.lower()
    themes = [
        "dawn", "refresh", "sense", "craft", "ride", "taste", "origin",
        "publisher", "spotlight", "studio", "crave", "colorblock",
        "be yours", "impulse", "prestige", "warehouse", "motion", "debut",
    ]
    for theme in themes:
        if theme in text:
            return theme.title()
    match = re.search(r'"theme_name"\s*:\s*"([^"]+)"', html, re.I)
    if match:
        return match.group(1)
    return "Unknown"


def get_product_count(base_url):
    url = base_url.rstrip("/") + "/products.json?limit=250"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return 0
        data = response.json()
        return len(data.get("products", []))
    except Exception:
        return 0


# ============================================================
# EMAIL EXTRACTION
# ============================================================

EMAIL_REGEX = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)


def extract_emails(text):
    if not text:
        return []
    emails = EMAIL_REGEX.findall(text)
    cleaned = []
    bad_ext = (".png", ".jpg", ".jpeg", ".webp", ".gif")
    for email in emails:
        email = email.lower().strip()
        if email.endswith(bad_ext):
            continue
        if email not in cleaned:
            cleaned.append(email)
    return cleaned


MAILTO_REGEX = re.compile(r'mailto:([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})', re.I)


def extract_mailto_emails(html):
    """
    mailto: links are near-certain to be a real contact email (vs random
    strings from analytics/CDN scripts that trip up a plain regex scan),
    so we check these first and trust them most.
    """
    if not html:
        return []

    matches = MAILTO_REGEX.findall(html)
    cleaned = []
    for email in matches:
        email = email.lower().strip()
        if email not in cleaned:
            cleaned.append(email)
    return cleaned


def fetch_extra_pages(base_url, homepage_html):
    """
    Fetches contact/about/policy/support pages ONCE and returns the combined
    text. This combined text is then reused for BOTH email extraction and
    feature detection (FAQ/Testimonials/etc.) — previously feature detection
    only looked at the homepage, which missed sections that live on their
    own page (a very common Shopify pattern).
    """
    pages = [
        "/contact",
        "/pages/contact",
        "/pages/contact-us",
        "/pages/get-in-touch",
        "/about",
        "/pages/about",
        "/pages/faq",
        "/pages/faqs",
        "/pages/support",
        "/support",
        "/help",
        "/policies/contact-information",
        "/policies/privacy-policy",
        "/policies/refund-policy",
        "/policies/terms-of-service",
    ]

    combined_text = homepage_html or ""

    for path in pages:
        try:
            html, _ = fetch_page(base_url.rstrip("/") + path)
            if html:
                combined_text += "\n" + html
        except Exception:
            pass

    return combined_text


def guess_email_patterns(domain):
    """
    When no real email is found on the site, offer common pattern-based
    guesses as a starting point. These are UNVERIFIED — clearly separated
    into their own column so they're never confused with a confirmed email.
    Verify before using for outreach (e.g. with an email-verification tool)
    to avoid bounces.
    """
    if not domain:
        return ""
    prefixes = ["info", "contact", "support", "hello", "sales"]
    return ", ".join(f"{p}@{domain}" for p in prefixes[:3])


def find_email(combined_text):
    # mailto: links are near-certain to be a real contact email — check first.
    mailto_hits = extract_mailto_emails(combined_text)
    if mailto_hits:
        return mailto_hits[0]

    # Fall back to a plain-text scan across everything fetched.
    emails = extract_emails(combined_text)
    return emails[0] if emails else ""


# ============================================================
# FEATURE DETECTION
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


# ============================================================
# REASON ENGINE + SCORE
# ============================================================

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
# ANALYZE STORE
# ============================================================

def analyze_store(domain, source):
    base_url = normalize_url(domain)

    try:
        html, final_url = fetch_page(base_url)
        if not html:
            return None

        if not detect_shopify(html):
            return None

        final_domain = normalize_domain(final_url)
        if not final_domain or is_bad_domain(final_domain):
            return None

        theme = detect_theme(html)
        product_count = get_product_count(base_url)

        # Fetch contact/about/policy/support pages ONCE and reuse the
        # combined text for both email extraction and feature detection —
        # this is the "properly visit and analyze the site" pass, instead
        # of judging FAQ/Testimonials/etc. off the homepage alone.
        combined_text = fetch_extra_pages(base_url, html)

        email = find_email(combined_text)
        email_guessed = guess_email_patterns(final_domain) if not email else ""

        features = detect_features(combined_text)
        reasons, recommendations = build_reasons(theme, product_count, features, email)
        score = calculate_score(product_count, features, email, theme)
        quality = score_quality(score)

        return {
            "Store URL": base_url,
            "Domain": final_domain,
            "Theme": theme,
            "Email": email,
            "Email (Guessed)": email_guessed,
            "Products": product_count,
            "Lead Score": score,
            "Quality": quality,
            "FAQ": "YES" if features["FAQ"] else "NO",
            "Testimonials": "YES" if features["Testimonials"] else "NO",
            "Reviews": "YES" if features["Reviews"] else "NO",
            "Sticky Add To Cart": "YES" if features["Sticky Add To Cart"] else "NO",
            "Size Guide": "YES" if features["Size Guide"] else "NO",
            "Newsletter": "YES" if features["Newsletter"] else "NO",
            "WhatsApp": "YES" if features["WhatsApp"] else "NO",
            "Reason": " | ".join(reasons),
            "Recommended Sections": ", ".join(recommendations),
            "Search Query": source,
        }

    except Exception as e:
        logger.debug("Store analysis failed %s: %s", domain, str(e))
        return None


# ============================================================
# GOOGLE SHEETS (OPTIONAL)
# ============================================================

HEADERS = [
    "Store URL", "Domain", "Theme", "Email", "Email (Guessed)", "Products", "Lead Score",
    "Quality", "FAQ", "Testimonials", "Reviews", "Sticky Add To Cart",
    "Size Guide", "Newsletter", "WhatsApp", "Reason",
    "Recommended Sections", "Search Query",
]


def connect_sheet():
    if not GSPREAD_AVAILABLE:
        logger.warning(
            "Google Sheet output skipped: gspread/google-auth not installed. "
            "Run: pip install gspread google-auth"
        )
        return None

    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.warning(
            "Google Sheet output skipped: GOOGLE_SERVICE_ACCOUNT_JSON env var "
            "is not set. Leads will still be saved to the local CSV."
        )
        return None

    if not GOOGLE_SHEET_ID:
        logger.warning(
            "Google Sheet output skipped: GOOGLE_SHEET_ID env var is not set. "
            "Leads will still be saved to the local CSV."
        )
        return None

    try:
        service_account_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        credentials = Credentials.from_service_account_info(service_account_info, scopes=scopes)
        client = gspread.authorize(credentials)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        try:
            worksheet = spreadsheet.worksheet(SHEET_NAME)
        except Exception:
            worksheet = spreadsheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADERS))
        logger.info("Connected to Google Sheet successfully.")
        return worksheet
    except json.JSONDecodeError:
        logger.error(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. Paste the full "
            "contents of the service account key file, not a file path."
        )
        return None
    except Exception as e:
        logger.error(
            "Google Sheet connection failed: %s | Common causes: (1) the "
            "sheet wasn't shared with the service account's client_email as "
            "Editor, (2) wrong GOOGLE_SHEET_ID, (3) Sheets/Drive API not "
            "enabled on that Google Cloud project.",
            str(e)
        )
        return None


def setup_headers(worksheet):
    try:
        current = worksheet.row_values(1)
        if current != HEADERS:
            worksheet.update(values=[HEADERS], range_name="A1")
    except Exception as e:
        logger.error("Could not update headers: %s", str(e))


def row_from_lead(lead):
    return [lead.get(h, "") for h in HEADERS]


def save_leads_to_sheet(worksheet, leads):
    if not leads:
        return
    rows = [row_from_lead(lead) for lead in leads]
    try:
        worksheet.append_rows(rows, value_input_option="USER_ENTERED")
        logger.info("Saved %s leads to Google Sheet.", len(rows))
    except Exception as e:
        logger.error("Could not save leads to sheet: %s", str(e))


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info("=" * 70)
    logger.info("SHOPIFY LEAD INTELLIGENCE ENGINE — FREE / HIGH VOLUME")
    logger.info("=" * 70)

    seen_domains = load_seen_domains()
    logger.info("Previously seen domains (will skip): %s", len(seen_domains))

    # --------------------------------------------------------
    # DISCOVERY
    # --------------------------------------------------------
    cc_domains = discover_via_common_crawl()
    google_domains = discover_via_google()
    app_store_domains = discover_via_app_store_reviews()

    all_discovered = cc_domains | google_domains | app_store_domains
    logger.info(
        "Discovery breakdown -> Common Crawl: %s | Google CSE: %s | "
        "App Store name-guesses: %s | Combined unique: %s",
        len(cc_domains), len(google_domains), len(app_store_domains), len(all_discovered)
    )

    new_domains = [d for d in all_discovered if d not in seen_domains and not is_bad_domain(d)]
    logger.info("New unseen candidate domains: %s", len(new_domains))

    if not new_domains:
        logger.warning("No new domains discovered this run. Try again later or widen CC_URL_PATTERN.")
        logger.info("RUN COMPLETE")
        return

    candidates = new_domains[:MAX_DOMAINS_PER_RUN]
    logger.info("Analyzing %s candidates this run (cap: %s)", len(candidates), MAX_DOMAINS_PER_RUN)

    # --------------------------------------------------------
    # CONNECT TO SHEET ONCE, UP FRONT — not just at the very end.
    # This was the bug causing "sheet not created": the old code only
    # tried to save to Sheets after the full analysis loop finished, using
    # only whatever leads were left over after the last 200-checkpoint.
    # If the count landed exactly on a multiple of 200, Sheets never got
    # called at all. Now we connect once and write to BOTH CSV and Sheet
    # at every checkpoint.
    # --------------------------------------------------------
    worksheet = connect_sheet()
    if worksheet:
        setup_headers(worksheet)
    else:
        logger.info(
            "Continuing with CSV-only output this run — leads will be in %s",
            LEADS_CSV_FILE
        )

    # --------------------------------------------------------
    # ANALYSIS
    # --------------------------------------------------------
    leads = []
    processed_domains = set()
    total_leads_found = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(analyze_store, domain, "common_crawl_or_google"): domain
            for domain in candidates
        }

        completed = 0
        for future in as_completed(futures):
            domain = futures[future]
            completed += 1
            processed_domains.add(domain)

            try:
                lead = future.result()
                if lead:
                    leads.append(lead)
                    total_leads_found += 1
                    logger.info(
                        "[%s/%s] LEAD: %s | %s | Score %s",
                        completed, len(candidates),
                        lead["Domain"], lead["Quality"], lead["Lead Score"]
                    )
            except Exception as e:
                logger.error("Analysis error for %s: %s", domain, str(e))

            if completed % CHECKPOINT_INTERVAL == 0:
                seen_domains |= processed_domains
                save_seen_domains(seen_domains)

                if leads:
                    append_leads_csv(leads)
                    if worksheet:
                        save_leads_to_sheet(worksheet, leads)
                    leads = []

                logger.info("Checkpoint saved at %s processed.", completed)

    # --------------------------------------------------------
    # FINAL SAVE (whatever is left since the last checkpoint)
    # --------------------------------------------------------
    seen_domains |= processed_domains
    save_seen_domains(seen_domains)

    if leads:
        leads.sort(key=lambda x: x.get("Lead Score", 0), reverse=True)
        append_leads_csv(leads)
        if worksheet:
            save_leads_to_sheet(worksheet, leads)

    logger.info("=" * 70)
    logger.info("RUN COMPLETE")
    logger.info("Candidates analyzed: %s", len(candidates))
    logger.info("Total leads found this run: %s", total_leads_found)
    logger.info("All leads this run saved to: %s", LEADS_CSV_FILE)
    if worksheet:
        logger.info("Leads also saved to Google Sheet: %s", SHEET_NAME)
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
