"""
SHOPIFY LEAD FINDER v2 — APP STORE REVIEWS ONLY, EMAIL MANDATORY
=================================================================
Discovery : sirf Shopify App Store review pages (store names)
Analysis  : har name ke liye domain guesses -> store visit -> verify
Output    : Google Sheet ("Leads" tab) + local CSV backup

RULE: sirf wohi store "Leads" me jata hai jiski REAL email site par mili.
      Jin stores ki email nahi mili wo "NoEmail" tab me jate hain (baad me
      manually / Hunter / Apollo se email nikalne ke liye) — leads me nahi.

Kya fix hua (purane version ke muqable me)
------------------------------------------
1. Google Sheet: service account ki Drive quota nahi hoti (403 error), is liye
   ab aap khud sheet banate hain, service account ko Editor share karte hain,
   aur GOOGLE_SHEET_ID dete hain. Script "Leads" + "NoEmail" tabs khud bana leti hai.
2. Duplicates: ek store ke 8 domain-guesses ab ek hi task me sequentially try
   hote hain aur pehli hit par ruk jate hain. Final domain par bhi dedupe hota hai.
3. Junk stores: password-protected / khali stores, bina products wale stores, aur
   woh stores jinka page review wale store name se match nahi karta (galat guess)
   ab reject ho jate hain.
4. Email: mailto, Cloudflare-protected, [at]/[dot], JSON-LD, contact/about/policy
   pages, homepage ke contact links. Junk emails (theme/app/CDN wali) filter hoti
   hain, store ke apne domain wali email ko priority milti hai, aur (dnspython ho
   to) email ke domain ka MX record check hota hai taake bounce na ho.
5. Theme detection: Shopify.theme JSON se (pehle 'withdrawn' me bhi 'dawn' pakad leta tha).

Install:
  pip install requests beautifulsoup4 gspread google-auth dnspython

Run:  python shopify_lead_finder_v2.py
"""

import os
import re
import sys
import csv
import json
import time
import html as html_lib
import logging
import requests

from collections import Counter
from functools import lru_cache
from urllib.parse import urlparse, urljoin, unquote
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

try:
    import dns.resolver
    DNS_AVAILABLE = True
except ImportError:
    DNS_AVAILABLE = False


# ============================================================
# CONFIG
# ============================================================

def _env_bool(name, default):
    return os.environ.get(name, "1" if default else "0").strip().lower() in ("1", "true", "yes", "on")


# --- Discovery: Shopify App Store reviews ---
DEFAULT_APP_HANDLES = (
    "judgeme,loox,product-reviews,ali-reviews,vitals,"
    "klaviyo-email-marketing,privy,smile-io,recart,"
    "seal-subscriptions,rebuy-personalization,pushowl-web-push"
)
APP_STORE_HANDLES = [
    h.strip() for h in os.environ.get("APP_STORE_HANDLES", DEFAULT_APP_HANDLES).split(",")
    if h.strip()
]
APP_STORE_MAX_PAGES = int(os.environ.get("APP_STORE_MAX_PAGES", "15"))
APP_STORE_DELAY_SECONDS = float(os.environ.get("APP_STORE_DELAY", "0.4"))

# --- Analysis ---
MAX_NAMES_PER_RUN = int(os.environ.get("MAX_NAMES_PER_RUN", "3000"))
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "25"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "10"))
CHECKPOINT_INTERVAL = int(os.environ.get("CHECKPOINT_INTERVAL", "50"))

# --- Quality filters ---
MIN_PRODUCTS = int(os.environ.get("MIN_PRODUCTS", "1"))               # 0 = bina products wale stores bhi
STRICT_NAME_MATCH = _env_bool("STRICT_NAME_MATCH", True)              # galat domain guesses reject
VERIFY_MX = _env_bool("VERIFY_MX", True)                              # email domain ka MX check (dnspython chahiye)
EXCLUDE_DOMAINS = [
    d.strip().lower() for d in os.environ.get("EXCLUDE_DOMAINS", "").split(",") if d.strip()
]

# --- Google Sheets ---
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()       # ID ya poora sheet URL
SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Leads").strip()
NO_EMAIL_SHEET_NAME = os.environ.get("GOOGLE_NO_EMAIL_SHEET_NAME", "NoEmail").strip()
REQUIRE_SHEET = _env_bool("REQUIRE_SHEET", True)                      # sheet fail ho to run rok do

# --- Local files ---
DATA_DIR = os.environ.get("LEAD_DATA_DIR", "./lead_data")
SEEN_FILE = os.path.join(DATA_DIR, "seen_names.json")
LEADS_CSV_FILE = os.path.join(DATA_DIR, "leads.csv")
NO_EMAIL_CSV_FILE = os.path.join(DATA_DIR, "no_email.csv")

HEADERS = [
    "Store Name", "Store URL", "Domain", "Email", "Other Emails", "Theme",
    "Products", "Lead Score", "Quality", "FAQ", "Testimonials", "Reviews",
    "Sticky Add To Cart", "Size Guide", "Newsletter", "WhatsApp",
    "Reason", "Recommended Sections", "Source",
]
NO_EMAIL_HEADERS = ["Store Name", "Store URL", "Domain", "Products", "Source"]


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
# DISCOVERY: SHOPIFY APP STORE REVIEWS
# ============================================================

TRAILING_GENERIC_WORDS = {"store", "shop", "official", "co", "llc", "inc", "ltd"}
GENERIC_NAME_WORDS = {"the", "and", "store", "shop", "official", "co", "llc", "inc", "ltd"}


def scrape_app_reviews_page(handle, page):
    """Ek review page se store display names (list)."""
    url = f"https://apps.shopify.com/{handle}/reviews"
    params = {"sort_by": "newest", "page": page}
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/131 Safari/537.36"
        ),
        "Accept-Language": "en-US;q=0.9",
    }

    resp = None
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=20)
        except requests.RequestException:
            time.sleep(2)
            continue
        if resp.status_code == 429:
            time.sleep(5 * (attempt + 1))
            continue
        break
    else:
        return []

    if resp is None or resp.status_code != 200:
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
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
    return names


def discover_store_names():
    """Returns {store_name: app_handle}."""
    if not BS4_AVAILABLE:
        logger.error("beautifulsoup4 install nahi hai. Run: pip install beautifulsoup4")
        return {}

    logger.info("Scraping App Store reviews (%s apps)...", len(APP_STORE_HANDLES))
    names = {}

    for handle in APP_STORE_HANDLES:
        logger.info("App: %s", handle)
        found_any = False
        for page in range(1, APP_STORE_MAX_PAGES + 1):
            page_names = scrape_app_reviews_page(handle, page)
            if not page_names:
                break
            found_any = True
            for n in page_names:
                names.setdefault(n, handle)
            time.sleep(APP_STORE_DELAY_SECONDS)
        if not found_any:
            logger.warning("No reviews found for app handle: %s (typo, removed, ya blocked?)", handle)

    logger.info("Unique store names collected: %s", len(names))
    return names


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


# ============================================================
# WEBSITE FETCH + VERIFICATION
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
    try:
        response = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=REQUEST_TIMEOUT)
        if response.status_code != 200:
            return 0
        return len(response.json().get("products", []))
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


@lru_cache(maxsize=50000)
def domain_has_mx(domain):
    """Bounce se bachne ke liye: domain email receive kar sakta hai? (dnspython ho tabhi)."""
    if not (VERIFY_MX and DNS_AVAILABLE):
        return True
    try:
        dns.resolver.resolve(domain, "MX", lifetime=4)
        return True
    except (dns.resolver.NXDOMAIN, dns.resolver.NoNameservers):
        return False
    except dns.resolver.NoAnswer:
        try:
            dns.resolver.resolve(domain, "A", lifetime=4)
            return True
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            return False
        except Exception:
            return True
    except Exception:
        return True   # timeout waghera — transient error par email drop nahi karte


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


def fetch_extra_pages(base_url, homepage_html):
    """Contact/about/policy pages ek baar fetch — email + feature detection dono isi se."""
    pages = [
        "/pages/contact", "/pages/contact-us", "/contact", "/pages/get-in-touch",
        "/pages/about", "/about", "/pages/about-us", "/pages/faq", "/pages/support",
        "/policies/contact-information", "/policies/refund-policy",
        "/policies/shipping-policy", "/policies/privacy-policy",
        "/policies/terms-of-service",
    ]
    pages += discover_contact_links(homepage_html, base_url)
    pages = list(dict.fromkeys(pages))[:18]

    combined = homepage_html or ""
    for path in pages:
        try:
            page_html, _ = fetch_page(base_url.rstrip("/") + path)
            if page_html:
                combined += "\n" + page_html
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

def analyze_store(domain, store_name, source_app):
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

        base_url = normalize_url(final_url)

        product_count = get_product_count(base_url)
        if product_count < MIN_PRODUCTS:
            return "no_products", None

        combined_text = fetch_extra_pages(base_url, html)

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


def process_store_name(name, handle, guesses):
    """Ek store name ke saare domain guesses sequentially try karta hai aur
    pehli asli hit (ok ya no_email) par ruk jata hai — duplicates + faltu requests khatam.
    Returns (status, data, reasons_counter)."""
    reasons = Counter()
    for domain in guesses:
        status, data = analyze_store(domain, name, handle)
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
    """Returns (leads_ws, no_email_ws) ya (None, None)."""
    if not GSPREAD_AVAILABLE:
        logger.error("Run: pip install gspread google-auth")
        return None, None
    if not GOOGLE_SERVICE_ACCOUNT_JSON:
        logger.error("GOOGLE_SERVICE_ACCOUNT_JSON env var set nahi hai.")
        return None, None
    if not GOOGLE_SHEET_ID:
        logger.error(
            "GOOGLE_SHEET_ID set nahi hai. Service account khud sheet create nahi kar sakta "
            "(403 'Drive storage quota exceeded'). Fix: (1) apne Google Drive me ek khali sheet "
            "banayein, (2) usay service account ki email par Editor share karein, (3) sheet ka "
            "URL ya ID GOOGLE_SHEET_ID me daal dein."
        )
        return None, None

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

        leads_ws = get_or_create_tab(spreadsheet, SHEET_NAME, HEADERS)
        no_email_ws = get_or_create_tab(spreadsheet, NO_EMAIL_SHEET_NAME, NO_EMAIL_HEADERS)
        return leads_ws, no_email_ws

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
    return None, None


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
    logger.error("Sheet me save nahi ho saka (%s) — in rows ki copy local CSV me hai.", label)


# ============================================================
# MAIN
# ============================================================

def main():
    logger.info("=" * 70)
    logger.info("SHOPIFY LEAD FINDER v2 — App Store reviews only | email mandatory")
    logger.info("=" * 70)

    ensure_data_dir()
    ensure_csv_schema_matches(LEADS_CSV_FILE, HEADERS)
    ensure_csv_schema_matches(NO_EMAIL_CSV_FILE, NO_EMAIL_HEADERS)

    # Sheet pehle connect hoti hai — masla ho to scraping se pehle pata chal jaye.
    leads_ws, no_email_ws = connect_sheets()
    if leads_ws is None:
        if REQUIRE_SHEET:
            logger.error("Sheet connect nahi hui, isliye run ruk gaya (REQUIRE_SHEET=0 set karein to CSV-only chalega).")
            sys.exit(1)
        logger.warning("Sheet ke baghair CSV-only mode me chal raha hun: %s", LEADS_CSV_FILE)

    # ---------------- Already-processed data ----------------
    seen = load_seen()                       # entries: "name:<normalized store name>"
    found_final_domains = set()

    if leads_ws is not None:
        lead_domains = load_tab_column(leads_ws, HEADERS, "Domain")
        lead_names = load_tab_column(leads_ws, HEADERS, "Store Name")
        ne_domains = load_tab_column(no_email_ws, NO_EMAIL_HEADERS, "Domain")
        ne_names = load_tab_column(no_email_ws, NO_EMAIL_HEADERS, "Store Name")
        found_final_domains |= {d.lower() for d in lead_domains + ne_domains}
        seen |= {f"name:{norm(n)}" for n in lead_names + ne_names}
        logger.info("Sheet me pehle se: %s leads, %s no-email stores", len(lead_domains), len(ne_domains))

    logger.info("Already processed store names (will skip): %s", len(seen))

    # ---------------- DISCOVERY ----------------
    names = discover_store_names()

    todo = []
    for name, handle in names.items():
        key = f"name:{norm(name)}"
        if key in seen:
            continue
        guesses = [d for d in guess_domains_from_name(name) if not is_bad_domain(d)]
        if guesses:
            todo.append((name, handle, guesses))

    logger.info("New store names to analyze: %s", len(todo))
    if not todo:
        logger.warning("Koi naya store name nahi mila. Baad me try karein ya APP_STORE_HANDLES badhayein.")
        logger.info("RUN COMPLETE")
        return

    todo = todo[:MAX_NAMES_PER_RUN]
    logger.info("Analyzing %s store names this run (cap: %s)", len(todo), MAX_NAMES_PER_RUN)

    # ---------------- ANALYSIS ----------------
    leads, no_email_rows = [], []
    stats = Counter()
    reason_stats = Counter()
    completed = 0

    def flush():
        nonlocal leads, no_email_rows
        if leads:
            append_csv(LEADS_CSV_FILE, HEADERS, leads)
            save_rows_to_sheet(leads_ws, HEADERS, leads, SHEET_NAME)
            leads = []
        if no_email_rows:
            append_csv(NO_EMAIL_CSV_FILE, NO_EMAIL_HEADERS, no_email_rows)
            save_rows_to_sheet(no_email_ws, NO_EMAIL_HEADERS, no_email_rows, NO_EMAIL_SHEET_NAME)
            no_email_rows = []
        save_seen(seen)

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
    try:
        futures = {
            executor.submit(process_store_name, name, handle, guesses): name
            for name, handle, guesses in todo
        }

        for future in as_completed(futures):
            name = futures[future]
            completed += 1

            try:
                status, data, reasons = future.result()
            except Exception as e:
                logger.error("Analysis error for %s: %s", name, str(e))
                status, data, reasons = "not_found", None, Counter({"error": 1})

            reason_stats.update(reasons)

            # Transient errors wale names ko 'seen' nahi banate taake agli run me dobara try hon.
            if not reasons.get("error"):
                seen.add(f"name:{norm(name)}")

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
        executor.shutdown(wait=False)
        leads.sort(key=lambda x: x.get("Lead Score", 0), reverse=True)
        flush()

    # ---------------- SUMMARY ----------------
    logger.info("=" * 70)
    logger.info("RUN COMPLETE")
    logger.info("Store names analyzed: %s", completed)
    logger.info("LEADS saved (real email found): %s", stats["leads"])
    logger.info("Shopify stores but NO email (-> '%s' tab): %s", NO_EMAIL_SHEET_NAME, stats["no_email"])
    logger.info("Names jinka koi valid store nahi mila: %s", stats["not_found"])
    logger.info("Duplicates skipped: %s", stats["duplicate"])
    if reason_stats:
        logger.info(
            "Guesses reject hone ki wajah -> not_shopify: %s | password: %s | "
            "name_mismatch: %s | no_products: %s | error: %s",
            reason_stats["not_shopify"], reason_stats["password"],
            reason_stats["name_mismatch"], reason_stats["no_products"], reason_stats["error"]
        )
    logger.info("CSV backup: %s", LEADS_CSV_FILE)
    if leads_ws is not None:
        logger.info("Google Sheet tab: %s", SHEET_NAME)
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
