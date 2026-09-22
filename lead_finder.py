import os
import re
import time
import random
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin

import requests
import gspread
from google.oauth2.credentials import Credentials


# ============================================================
# SHOPIFY LEAD INTELLIGENCE ENGINE V2
# ============================================================

APP_NAME = "Shopify Lead Intelligence Engine V2"

# ---------------- SETTINGS ----------------

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID")

GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

# Number of Google queries in one GitHub run
MAX_QUERIES_PER_RUN = int(
    os.environ.get("MAX_QUERIES_PER_RUN", "90")
)

# Search results per query
RESULTS_PER_QUERY = 10

# Parallel website analyzers
MAX_WORKERS = int(
    os.environ.get("MAX_WORKERS", "12")
)

# Maximum discovered URLs kept in memory
MAX_DISCOVERED_URLS = int(
    os.environ.get("MAX_DISCOVERED_URLS", "5000")
)

SHEET_NAME = os.environ.get(
    "SHEET_NAME",
    "Shopify Lead Intelligence"
)

QUERY_STATE_TAB = "QueryState"

REQUEST_TIMEOUT = 12

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/139.0 Safari/537.36"
)

SESSION_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
}


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(APP_NAME)


# ============================================================
# TARGET NICHES
# ============================================================

NICHES = [
    "jewelry",
    "candles",
    "fitness",
    "skincare",
    "pet supplies",
    "home decor",
    "clothing boutique",
    "shoes",
    "handbags",
    "sunglasses",
    "coffee",
    "tea",
    "supplements",
    "baby products",
    "toys",
    "phone accessories",
    "art prints",
    "furniture",
    "kitchenware",
    "bags",
    "watches",
    "beauty products",
    "outdoor gear",
    "cycling gear",
    "yoga",
    "gaming accessories",
    "electronics",
    "stationery",
    "plants",
    "swimwear",
    "streetwear",
    "men clothing",
    "women clothing",
    "skincare brand",
    "hair care",
    "organic products",
    "home accessories",
]


# ============================================================
# DISCOVERY QUERY ENGINE
# ============================================================

QUERY_TEMPLATES = [

    # Shopify footprints
    '"powered by shopify" {niche}',
    '"myshopify.com" "{niche}"',
    '"shopify" "{niche}" "add to cart"',
    '"shopify" "{niche}" "buy now"',
    '"shopify" "{niche}" "checkout"',
    '"shopify" "{niche}" "free shipping"',

    # Ecommerce language
    '"{niche}" "add to cart" online store',
    '"{niche}" "shop now" ecommerce',
    '"{niche}" "new arrivals" store',
    '"{niche}" "collections" store',
    '"{niche}" "sale" "add to cart"',

    # Conversion sections
    '"{niche}" shop "frequently asked questions"',
    '"{niche}" shop "customer reviews"',
    '"{niche}" shop "testimonials"',
    '"{niche}" shop "newsletter"',
    '"{niche}" shop "free shipping"',

    # Theme / storefront footprints
    '"{niche}" "Dawn" Shopify',
    '"{niche}" "Craft" Shopify',
    '"{niche}" "Refresh" Shopify',
    '"{niche}" "Sense" Shopify',

    # Product pages
    '"{niche}" "size guide" Shopify',
    '"{niche}" "product details" Shopify',
    '"{niche}" "quantity" "add to cart" Shopify',
]


MODIFIERS = [
    "",
    "brand",
    "store",
    "shop",
    "online",
    "official",
    "USA",
    "UK",
    "Canada",
    "Australia",
]


BLOCKED_DOMAINS = [
    "myshopify.com",
    "shopify.com",
    "pinterest.com",
    "facebook.com",
    "instagram.com",
    "youtube.com",
    "tiktok.com",
    "linkedin.com",
    "medium.com",
    "reddit.com",
    "wikipedia.org",
    "amazon.com",
    "etsy.com",
    "ebay.com",
    "walmart.com",
    "target.com",
    "aliexpress.com",
    "gempages.net",
    "omnisend.com",
]


def build_query_pool():
    pool = []

    for niche in NICHES:
        for template in QUERY_TEMPLATES:

            base = template.format(niche=niche)

            for modifier in MODIFIERS:

                query = f"{base} {modifier}".strip()

                # Avoid obviously duplicated queries
                query = re.sub(r"\s+", " ", query)

                pool.append(query)

    # deterministic shuffle
    rng = random.Random(2026)
    rng.shuffle(pool)

    return list(dict.fromkeys(pool))


QUERY_POOL = build_query_pool()

logger.info(
    "Query pool created: %s unique queries",
    len(QUERY_POOL)
)


# ============================================================
# GOOGLE SHEETS STATE
# ============================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_google_client():

    creds = Credentials.from_authorized_user_file(
        "token.json",
        SCOPES
    )

    return gspread.authorize(creds)


def get_or_create_sheet():

    gc = get_google_client()

    try:
        spreadsheet = gc.open(SHEET_NAME)

    except gspread.SpreadsheetNotFound:
        spreadsheet = gc.create(SHEET_NAME)

    sheet = spreadsheet.sheet1

    headers = [
        "Store URL",
        "Domain",
        "Niche",
        "Shopify",
        "Theme",
        "Products",
        "Email",
        "Instagram",
        "Facebook",
        "TikTok",
        "FAQ",
        "Testimonials",
        "Reviews",
        "Sticky ATC",
        "Size Guide",
        "Newsletter",
        "WhatsApp",
        "Lead Score",
        "Quality",
        "Reasons",
        "Recommended Sections",
        "Search Query",
        "Found At",
    ]

    if not sheet.get_all_values():
        sheet.append_row(headers)

    return spreadsheet, sheet


def get_existing_urls(sheet):

    try:

        values = sheet.get_all_values()

        if len(values) <= 1:
            return set()

        return {
            row[0].strip().lower()
            for row in values[1:]
            if row and row[0]
        }

    except Exception as e:

        logger.warning(
            "Could not read existing URLs: %s",
            e
        )

        return set()


def get_or_create_state_tab(spreadsheet):

    try:
        return spreadsheet.worksheet(QUERY_STATE_TAB)

    except gspread.WorksheetNotFound:

        tab = spreadsheet.add_worksheet(
            title=QUERY_STATE_TAB,
            rows=2,
            cols=2
        )

        tab.update(
            "A1",
            [["next_index", "0"]]
        )

        return tab


def get_next_index(state_tab):

    try:

        value = state_tab.acell("B1").value

        return int(value) if value else 0

    except Exception:

        return 0


def save_next_index(state_tab, index):

    state_tab.update(
        "B1",
        [[str(index)]]
    )


def get_next_queries(state_tab, count):

    start = get_next_index(state_tab)

    selected = []

    for i in range(count):

        index = (start + i) % len(QUERY_POOL)

        selected.append(
            QUERY_POOL[index]
        )

    new_index = (
        start + count
    ) % len(QUERY_POOL)

    save_next_index(
        state_tab,
        new_index
    )

    return selected


# ============================================================
# URL NORMALIZATION
# ============================================================

def normalize_url(url):

    if not url:
        return None

    url = url.strip()

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    if not parsed.netloc:
        return None

    clean = (
        parsed.scheme
        + "://"
        + parsed.netloc
    )

    return clean.rstrip("/")


def get_domain(url):

    try:
        return urlparse(url).netloc.lower().replace(
            "www.",
            ""
        )

    except Exception:
        return ""


def is_blocked(url):

    domain = get_domain(url)

    return any(
        blocked in domain
        for blocked in BLOCKED_DOMAINS
    )


# ============================================================
# GOOGLE DISCOVERY
# ============================================================

def search_google(query):

    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:

        logger.error(
            "GOOGLE_API_KEY / GOOGLE_CSE_ID missing"
        )

        return None

    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "num": RESULTS_PER_QUERY,
    }

    try:

        response = requests.get(
            GOOGLE_SEARCH_URL,
            params=params,
            headers=SESSION_HEADERS,
            timeout=20
        )

        if response.status_code == 429:

            logger.warning(
                "Google quota reached."
            )

            return None

        if response.status_code != 200:

            logger.warning(
                "Google HTTP %s",
                response.status_code
            )

            return []

        data = response.json()

        return [
            item.get("link")
            for item in data.get("items", [])
            if item.get("link")
        ]

    except Exception as e:

        logger.warning(
            "Google search failed: %s",
            e
        )

        return []


def discover_urls(queries):

    discovered = {}

    for number, query in enumerate(
        queries,
        start=1
    ):

        logger.info(
            "[%s/%s] Searching: %s",
            number,
            len(queries),
            query
        )

        urls = search_google(query)

        if urls is None:
            break

        for raw_url in urls:

            url = normalize_url(raw_url)

            if not url:
                continue

            if is_blocked(url):
                continue

            domain = get_domain(url)

            if domain not in discovered:

                discovered[domain] = {
                    "url": url,
                    "query": query,
                }

            if len(discovered) >= MAX_DISCOVERED_URLS:
                return list(discovered.values())

    return list(discovered.values())


# ============================================================
# HTML HELPERS
# ============================================================

def clean_html(html):

    text = re.sub(
        r"<script\b[^>]*>.*?</script>",
        " ",
        html,
        flags=re.I | re.S
    )

    text = re.sub(
        r"<style\b[^>]*>.*?</style>",
        " ",
        text,
        flags=re.I | re.S
    )

    text = re.sub(
        r"<[^>]+>",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.lower()


def has_any(text, keywords):

    return any(
        keyword.lower() in text
        for keyword in keywords
    )


# ============================================================
# FEATURE DETECTION
# ============================================================

def detect_features(html):

    text = clean_html(html)

    features = {}

    features["faq"] = has_any(
        text,
        [
            "frequently asked questions",
            "faq",
            "common questions",
            "questions & answers",
        ]
    )

    features["testimonials"] = has_any(
        text,
        [
            "testimonials",
            "what our customers say",
            "customer stories",
            "customer love",
            "love from our customers",
        ]
    )

    features["reviews"] = has_any(
        text,
        [
            "customer reviews",
            "reviews",
            "write a review",
            "verified review",
            "star rating",
        ]
    )

    features["sticky_atc"] = has_any(
        text,
        [
            "sticky add to cart",
            "sticky-add-to-cart",
            "sticky cart",
            "add to cart",
        ]
    )

    features["size_guide"] = has_any(
        text,
        [
            "size guide",
            "size chart",
            "sizing guide",
            "size & fit",
        ]
    )

    features["newsletter"] = has_any(
        text,
        [
            "subscribe to our newsletter",
            "newsletter",
            "sign up for updates",
            "join our mailing list",
        ]
    )

    features["whatsapp"] = has_any(
        text,
        [
            "wa.me/",
            "whatsapp",
            "api.whatsapp.com",
        ]
    )

    return features


# ============================================================
# SHOPIFY DETECTION
# ============================================================

def detect_shopify(html, headers):

    signatures = [
        "cdn.shopify.com",
        "shopify",
        "shopify.theme",
        "shopifyanalytics",
        "shopify-section",
        "shopify-payment-button",
    ]

    lower_html = html.lower()

    html_detected = any(
        signature in lower_html
        for signature in signatures
    )

    header_detected = any(
        "shopify" in str(value).lower()
        for value in headers.values()
    )

    return html_detected or header_detected


# ============================================================
# THEME DETECTION
# ============================================================

def detect_theme(html):

    schema_match = re.search(
        r'"schema_name"\s*:\s*"([^"]+)"',
        html,
        re.I
    )

    name_match = re.search(
        r'"name"\s*:\s*"([^"]+)"',
        html,
        re.I
    )

    schema_name = (
        schema_match.group(1)
        if schema_match
        else "Unknown"
    )

    theme_name = (
        name_match.group(1)
        if name_match
        else "Unknown"
    )

    known_themes = [
        "dawn",
        "debut",
        "craft",
        "sense",
        "refresh",
        "taste",
        "studio",
        "ride",
        "origin",
        "spotlight",
    ]

    combined = (
        schema_name + " " + theme_name
    ).lower()

    default_theme = any(
        theme in combined
        for theme in known_themes
    )

    return (
        theme_name,
        schema_name,
        default_theme
    )


# ============================================================
# EMAIL / SOCIAL EXTRACTION
# ============================================================

EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+-]+"
    r"@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
)


def extract_emails(html):

    emails = EMAIL_PATTERN.findall(html)

    bad = [
        "example.com",
        "sentry",
        "wixpress",
        "godaddy",
        "yourdomain",
        "domain.com",
        "email.com",
        "test.com",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
    ]

    clean = []

    for email in emails:

        lower = email.lower()

        if any(
            bad_word in lower
            for bad_word in bad
        ):
            continue

        if email not in clean:
            clean.append(email)

    return clean


def extract_social(html):

    socials = {
        "instagram": None,
        "facebook": None,
        "tiktok": None,
    }

    patterns = {
        "instagram":
            r"https?://(?:www\.)?instagram\.com/[A-Za-z0-9_.-]+",

        "facebook":
            r"https?://(?:www\.)?facebook\.com/[A-Za-z0-9_.-]+",

        "tiktok":
            r"https?://(?:www\.)?tiktok\.com/@?[A-Za-z0-9_.-]+",
    }

    for name, pattern in patterns.items():

        match = re.search(
            pattern,
            html,
            re.I
        )

        if match:
            socials[name] = match.group(0)

    return socials


# ============================================================
# PRODUCT COUNT
# ============================================================

def get_product_count(base_url):

    try:

        response = requests.get(
            base_url + "/products.json?limit=250",
            headers=SESSION_HEADERS,
            timeout=10
        )

        if response.status_code != 200:
            return None

        data = response.json()

        products = data.get(
            "products",
            []
        )

        return len(products)

    except Exception:
        return None


# ============================================================
# DEEP PAGE DISCOVERY
# ============================================================

def find_contact_pages(base_url):

    paths = [
        "/pages/contact",
        "/pages/contact-us",
        "/pages/about",
        "/pages/about-us",
    ]

    pages = []

    for path in paths:

        pages.append(
            urljoin(
                base_url + "/",
                path.lstrip("/")
            )
        )

    return pages


def find_email(base_url, homepage_html):

    emails = extract_emails(
        homepage_html
    )

    if emails:
        return emails[0]

    for page in find_contact_pages(base_url):

        try:

            response = requests.get(
                page,
                headers=SESSION_HEADERS,
                timeout=8
            )

            if response.status_code == 200:

                emails = extract_emails(
                    response.text
                )

                if emails:
                    return emails[0]

        except Exception:
            continue

    return None


# ============================================================
# REASON ENGINE
# ============================================================

def build_reasons(
    default_theme,
    theme_name,
    schema_name,
    product_count,
    email,
    features,
):

    reasons = []
    recommended = []

    # Theme
    if default_theme:

        reasons.append(
            f"Uses a common/default Shopify theme ({schema_name})"
        )

    # FAQ
    if not features["faq"]:

        reasons.append(
            "No obvious FAQ section detected"
        )

        recommended.append(
            "FAQ"
        )

    # Testimonials
    if not features["testimonials"]:

        reasons.append(
            "No obvious testimonials section detected"
        )

        recommended.append(
            "Testimonials"
        )

    # Reviews
    if not features["reviews"]:

        reasons.append(
            "No obvious customer-review system detected"
        )

    # Sticky ATC
    if not features["sticky_atc"]:

        reasons.append(
            "No obvious sticky add-to-cart feature detected"
        )

        recommended.append(
            "Sticky Add to Cart"
        )

    # Size guide
    if not features["size_guide"]:

        reasons.append(
            "No obvious size guide detected"
        )

        recommended.append(
            "Product Info"
        )

    # Newsletter
    if not features["newsletter"]:

        reasons.append(
            "No obvious newsletter signup detected"
        )

        recommended.append(
            "Newsletter"
        )

    # WhatsApp
    if not features["whatsapp"]:

        reasons.append(
            "No obvious WhatsApp contact detected"
        )

    # Products
    if product_count is not None:

        if product_count >= 50:

            reasons.append(
                f"Large catalog detected ({product_count}+ products)"
            )

        elif product_count >= 20:

            reasons.append(
                f"Established catalog detected ({product_count}+ products)"
            )

    # Contact
    if email:

        reasons.append(
            "Business contact email discovered"
        )

    return reasons, list(
        dict.fromkeys(recommended)
    )


# ============================================================
# SCORE ENGINE
# ============================================================

def calculate_score(
    default_theme,
    product_count,
    email,
    features,
    reasons
):

    score = 0

    # Shopify baseline
    score += 15

    # Theme
    if default_theme:
        score += 20
    else:
        score += 5

    # Products
    if product_count is not None:

        if product_count >= 100:
            score += 20

        elif product_count >= 50:
            score += 15

        elif product_count >= 20:
            score += 10

        elif product_count >= 5:
            score += 5

    # Contact
    if email:
        score += 15

    # Missing conversion features
    missing_count = 0

    for key in [
        "faq",
        "testimonials",
        "reviews",
        "sticky_atc",
        "size_guide",
        "newsletter",
    ]:

        if not features.get(key):
            missing_count += 1

    score += min(
        missing_count * 5,
        30
    )

    return min(score, 100)


def quality_label(score):

    if score >= 75:
        return "🔥 Hot Lead"

    if score >= 50:
        return "⭐ Warm Lead"

    return "🌱 Cold Lead"


# ============================================================
# SINGLE WEBSITE ANALYZER
# ============================================================

def analyze_store(item):

    url = item["url"]
    query = item["query"]

    result = {
        "url": url,
        "domain": get_domain(url),
        "query": query,
    }

    try:

        response = requests.get(
            url,
            headers=SESSION_HEADERS,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

        if response.status_code != 200:
            return None

        html = response.text

        # Shopify check
        if not detect_shopify(
            html,
            response.headers
        ):
            return None

        # Theme
        (
            theme_name,
            schema_name,
            default_theme
        ) = detect_theme(html)

        # Features
        features = detect_features(
            html
        )

        # Email
        email = find_email(
            url,
            html
        )

        # Social
        socials = extract_social(
            html
        )

        # Products
        product_count = get_product_count(
            url
        )

        # Reasons
        (
            reasons,
            recommended
        ) = build_reasons(
            default_theme,
            theme_name,
            schema_name,
            product_count,
            email,
            features
        )

        score = calculate_score(
            default_theme,
            product_count,
            email,
            features,
            reasons
        )

        result.update({

            "shopify": "YES",

            "theme": (
                schema_name
                if schema_name != "Unknown"
                else theme_name
            ),

            "products": product_count,

            "email": email or "Nahi mila",

            "instagram":
                socials["instagram"] or "",

            "facebook":
                socials["facebook"] or "",

            "tiktok":
                socials["tiktok"] or "",

            "faq":
                "YES" if features["faq"]
                else "NO",

            "testimonials":
                "YES"
                if features["testimonials"]
                else "NO",

            "reviews":
                "YES"
                if features["reviews"]
                else "NO",

            "sticky_atc":
                "YES"
                if features["sticky_atc"]
                else "NO",

            "size_guide":
                "YES"
                if features["size_guide"]
                else "NO",

            "newsletter":
                "YES"
                if features["newsletter"]
                else "NO",

            "whatsapp":
                "YES"
                if features["whatsapp"]
                else "NO",

            "score": score,

            "quality":
                quality_label(score),

            "reasons":
                " | ".join(reasons),

            "recommended":
                ", ".join(recommended)
                if recommended
                else "General Shopify optimization",

        })

        return result

    except Exception as e:

        logger.debug(
            "Analysis failed for %s: %s",
            url,
            e
        )

        return None


# ============================================================
# SHEET WRITER
# ============================================================

def save_lead(sheet, lead):

    sheet.append_row([
        lead.get("url", ""),
        lead.get("domain", ""),
        "Unknown",
        lead.get("shopify", ""),
        lead.get("theme", ""),
        lead.get("products")
            if lead.get("products") is not None
            else "N/A",
        lead.get("email", ""),
        lead.get("instagram", ""),
        lead.get("facebook", ""),
        lead.get("tiktok", ""),
        lead.get("faq", ""),
        lead.get("testimonials", ""),
        lead.get("reviews", ""),
        lead.get("sticky_atc", ""),
        lead.get("size_guide", ""),
        lead.get("newsletter", ""),
        lead.get("whatsapp", ""),
        lead.get("score", 0),
        lead.get("quality", ""),
        lead.get("reasons", ""),
        lead.get("recommended", ""),
        lead.get("query", ""),
        time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    ])


# ============================================================
# MAIN ENGINE
# ============================================================

def run():

    logger.info(
        "=============================================="
    )

    logger.info(
        "Starting %s",
        APP_NAME
    )

    logger.info(
        "=============================================="
    )

    # Google Sheets
    spreadsheet, sheet = (
        get_or_create_sheet()
    )

    state_tab = (
        get_or_create_state_tab(
            spreadsheet
        )
    )

    existing_urls = (
        get_existing_urls(sheet)
    )

    logger.info(
        "Existing leads: %s",
        len(existing_urls)
    )

    # Queries
    queries = get_next_queries(
        state_tab,
        MAX_QUERIES_PER_RUN
    )

    logger.info(
        "Queries this run: %s",
        len(queries)
    )

    # Discovery
    discovered = discover_urls(
        queries
    )

    logger.info(
        "Unique domains discovered: %s",
        len(discovered)
    )

    # Remove existing
    candidates = [
        item
        for item in discovered
        if get_domain(item["url"])
        not in {
            get_domain(existing)
            for existing in existing_urls
        }
    ]

    logger.info(
        "New candidates for analysis: %s",
        len(candidates)
    )

    # Parallel analysis
    leads = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                analyze_store,
                item
            ): item
            for item in candidates
        }

        completed = 0

        for future in as_completed(
            futures
        ):

            completed += 1

            try:

                lead = future.result()

                if lead:

                    leads.append(lead)

                    logger.info(
                        "[%s/%s] %s | %s",
                        completed,
                        len(candidates),
                        lead["quality"],
                        lead["url"]
                    )

                else:

                    logger.info(
                        "[%s/%s] Not Shopify / rejected",
                        completed,
                        len(candidates)
                    )

            except Exception as e:

                logger.warning(
                    "Worker error: %s",
                    e
                )

    # Sort best leads first
    leads.sort(
        key=lambda x: x.get(
            "score",
            0
        ),
        reverse=True
    )

    # Save
    saved = 0
    hot = 0

    for lead in leads:

        domain = lead["domain"]

        if domain in {
            get_domain(x)
            for x in existing_urls
        }:
            continue

        try:

            save_lead(
                sheet,
                lead
            )

            existing_urls.add(
                lead["url"]
            )

            saved += 1

            if lead["score"] >= 75:
                hot += 1

        except Exception as e:

            logger.warning(
                "Sheet save failed for %s: %s",
                lead["url"],
                e
            )

    logger.info(
        "=============================================="
    )

    logger.info(
        "RUN COMPLETE"
    )

    logger.info(
        "Discovered: %s",
        len(discovered)
    )

    logger.info(
        "Analyzed: %s",
        len(candidates)
    )

    logger.info(
        "Shopify leads saved: %s",
        saved
    )

    logger.info(
        "Hot leads: %s",
        hot
    )

    logger.info(
        "=============================================="
    )


if __name__ == "__main__":
    run()
