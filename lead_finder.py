import os
import re
import time
import json
import logging
import requests
import gspread

from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from google.oauth2.service_account import Credentials


# ============================================================
# CONFIG
# ============================================================

GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

RESULTS_PER_QUERY = 10
MAX_QUERIES_PER_RUN = 90
MAX_WORKERS = 12

REQUEST_TIMEOUT = 15
GOOGLE_TIMEOUT = 30

# Do not hammer Google
GOOGLE_DELAY_SECONDS = 0.5

# Environment variables / GitHub Secrets
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY", "").strip()
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "").strip()

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_JSON", ""
).strip()

SHEET_NAME = os.environ.get("GOOGLE_SHEET_NAME", "Leads").strip()


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("shopify-lead-finder")


# ============================================================
# QUERY POOL
# ============================================================

NICHES = [
    "bags",
    "shoes",
    "fashion",
    "clothing",
    "jewelry",
    "watches",
    "beauty",
    "skincare",
    "cosmetics",
    "supplements",
    "fitness",
    "gym",
    "sports",
    "pet products",
    "home decor",
    "furniture",
    "kitchen",
    "baby products",
    "organic products",
    "coffee",
    "tea",
    "food",
    "accessories",
    "electronics",
    "gadgets",
    "candles",
    "gifts",
    "handmade products",
    "streetwear",
    "activewear",
    "swimwear",
    "mens fashion",
    "womens fashion",
]

TEMPLATES = [
    "new arrivals",
    "best sellers",
    "shop now",
    "collections",
    "sale",
    "featured collection",
    "featured products",
    "product collection",
    "our products",
    "online store",
    "buy online",
    "free shipping",
    "limited edition",
    "summer collection",
    "winter collection",
    "new collection",
    "gift collection",
    "shop collection",
    "product details",
    "add to cart",
]

MODIFIERS = [
    "Shopify",
    "shop",
    "store",
    "official",
    "online",
    "brand",
]


def build_query_pool():
    queries = []

    for niche in NICHES:
        for template in TEMPLATES:
            for modifier in MODIFIERS:
                queries.append(
                    f'"{niche}" "{template}" {modifier}'
                )

    # Remove duplicates
    queries = list(dict.fromkeys(queries))

    logger.info("Query pool created: %s unique queries", len(queries))

    return queries


# ============================================================
# GOOGLE API
# ============================================================

def validate_google_config():
    """
    We do not print secrets.
    We only confirm whether GitHub injected them.
    """

    logger.info(
        "GOOGLE_API_KEY present: %s",
        bool(GOOGLE_API_KEY)
    )

    logger.info(
        "GOOGLE_CSE_ID present: %s",
        bool(GOOGLE_CSE_ID)
    )

    if not GOOGLE_API_KEY:
        logger.error(
            "GOOGLE_API_KEY is missing from GitHub Secrets."
        )
        return False

    if not GOOGLE_CSE_ID:
        logger.error(
            "GOOGLE_CSE_ID is missing from GitHub Secrets."
        )
        return False

    return True


def search_google(query, start=1):
    """
    Google Custom Search request.

    Important:
    We print the REAL Google error reason when Google returns
    403/400/401/etc.
    """

    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        return []

    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "num": RESULTS_PER_QUERY,
        "start": start,
    }

    try:
        response = requests.get(
            GOOGLE_SEARCH_URL,
            params=params,
            timeout=GOOGLE_TIMEOUT
        )

    except requests.RequestException as e:
        logger.error(
            "Google request failed: %s",
            str(e)
        )
        return []

    # --------------------------------------------------------
    # SUCCESS
    # --------------------------------------------------------

    if response.status_code == 200:

        try:
            data = response.json()
        except Exception:
            logger.error("Google returned invalid JSON.")
            return []

        results = data.get("items", [])

        output = []

        for item in results:
            link = item.get("link")

            if link:
                output.append(link)

        return output

    # --------------------------------------------------------
    # ERROR
    # --------------------------------------------------------

    logger.error(
        "Google HTTP %s for query: %s",
        response.status_code,
        query
    )

    try:
        error_data = response.json()

        logger.error(
            "Google error response:\n%s",
            json.dumps(
                error_data,
                indent=2,
                ensure_ascii=False
            )
        )

        error = error_data.get("error", {})

        logger.error(
            "Google error message: %s",
            error.get("message", "Unknown")
        )

        errors = error.get("errors", [])

        if errors:
            for err in errors:
                logger.error(
                    "Google reason=%s | domain=%s | message=%s",
                    err.get("reason"),
                    err.get("domain"),
                    err.get("message")
                )

    except Exception:
        logger.error(
            "Raw Google response:\n%s",
            response.text[:4000]
        )

    # Do NOT pretend every 403 means quota.
    if response.status_code == 403:
        logger.error(
            "IMPORTANT: Google returned 403. "
            "Read the error 'reason' above. "
            "It may be API restriction, disabled API, invalid "
            "permission, billing/quota, or another configuration issue."
        )

    return []


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


def normalize_url(url):

    try:
        parsed = urlparse(url)

        if not parsed.scheme:
            return "https://" + url

        return f"{parsed.scheme}://{parsed.netloc}"

    except Exception:
        return url


def is_bad_domain(domain):

    bad_domains = [
        "facebook.com",
        "instagram.com",
        "youtube.com",
        "tiktok.com",
        "pinterest.com",
        "twitter.com",
        "x.com",
        "linkedin.com",
        "amazon.com",
        "ebay.com",
        "etsy.com",
        "walmart.com",
        "reddit.com",
        "google.com",
        "bing.com",
        "shopify.com",
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

        response = requests.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True
        )

        if response.status_code >= 400:
            return "", response.url

        return response.text, response.url

    except Exception:
        return "", url


# ============================================================
# SHOPIFY DETECTION
# ============================================================

def detect_shopify(html, headers=None):

    if not html:
        return False

    text = html.lower()

    indicators = [
        "cdn.shopify.com",
        "shopifycdn.com",
        "shopify.theme",
        "shopify.shop",
        "myshopify.com",
        "x-shopify-stage",
        "shopify-section",
        "shopify-payment-button",
    ]

    for indicator in indicators:

        if indicator in text:
            return True

    if headers:

        for key, value in headers.items():

            combined = f"{key} {value}".lower()

            if "shopify" in combined:
                return True

    return False


# ============================================================
# THEME DETECTION
# ============================================================

def detect_theme(html):

    if not html:
        return "Unknown"

    text = html.lower()

    themes = [
        "dawn",
        "refresh",
        "sense",
        "craft",
        "ride",
        "taste",
        "origin",
        "publisher",
        "spotlight",
        "studio",
        "crave",
        "colorblock",
        "be yours",
        "impulse",
        "prestige",
        "warehouse",
        "motion",
        "debut",
    ]

    for theme in themes:

        if theme in text:
            return theme.title()

    schema_match = re.search(
        r'"theme_name"\s*:\s*"([^"]+)"',
        html,
        re.I
    )

    if schema_match:
        return schema_match.group(1)

    return "Unknown"


# ============================================================
# PRODUCT COUNT
# ============================================================

def get_product_count(base_url):

    url = base_url.rstrip("/") + "/products.json?limit=250"

    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    try:

        response = requests.get(
            url,
            headers=headers,
            timeout=REQUEST_TIMEOUT
        )

        if response.status_code != 200:
            return 0

        data = response.json()

        products = data.get("products", [])

        return len(products)

    except Exception:
        return 0


# ============================================================
# EMAIL EXTRACTION
# ============================================================

EMAIL_REGEX = re.compile(
    r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
    re.I
)


def extract_emails(text):

    if not text:
        return []

    emails = EMAIL_REGEX.findall(text)

    cleaned = []

    for email in emails:

        email = email.lower().strip()

        bad_extensions = [
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".gif",
        ]

        if any(email.endswith(x) for x in bad_extensions):
            continue

        if email not in cleaned:
            cleaned.append(email)

    return cleaned


def find_email(base_url, homepage_html):

    all_text = homepage_html

    pages = [
        "/contact",
        "/pages/contact",
        "/about",
        "/pages/about",
        "/privacy-policy",
        "/policies/privacy-policy",
    ]

    for path in pages:

        try:

            url = base_url.rstrip("/") + path

            html, _ = fetch_page(url)

            if html:
                all_text += "\n" + html

        except Exception:
            pass

    emails = extract_emails(all_text)

    if emails:
        return emails[0]

    return ""


# ============================================================
# FEATURE DETECTION
# ============================================================

def has_any(text, keywords):

    text = text.lower()

    return any(keyword.lower() in text for keyword in keywords)


def detect_features(html):

    text = html.lower()

    features = {
        "FAQ": has_any(
            text,
            [
                "faq",
                "frequently asked questions",
                "questions",
            ]
        ),

        "Testimonials": has_any(
            text,
            [
                "testimonial",
                "testimonials",
                "what our customers say",
                "customer stories",
            ]
        ),

        "Reviews": has_any(
            text,
            [
                "customer reviews",
                "reviews",
                "star-rating",
                "rating",
                "review-widget",
                "judge.me",
                "loox",
                "yotpo",
            ]
        ),

        "Sticky Add To Cart": has_any(
            text,
            [
                "sticky add to cart",
                "sticky-add-to-cart",
                "sticky_atc",
                "sticky-atc",
            ]
        ),

        "Size Guide": has_any(
            text,
            [
                "size guide",
                "size chart",
                "sizing guide",
            ]
        ),

        "Newsletter": has_any(
            text,
            [
                "newsletter",
                "subscribe to our",
                "email signup",
                "sign up for",
            ]
        ),

        "WhatsApp": has_any(
            text,
            [
                "whatsapp",
                "wa.me",
                "whatsapp.com",
            ]
        ),

        "Countdown": has_any(
            text,
            [
                "countdown",
                "count-down",
                "limited time",
                "ends in",
            ]
        ),

        "Instagram": has_any(
            text,
            [
                "instagram.com",
                "instagram-feed",
                "instagram gallery",
            ]
        ),
    }

    return features


# ============================================================
# REASON ENGINE
# ============================================================

def build_reasons(
    theme,
    product_count,
    features,
    email
):

    reasons = []
    recommendations = []

    # Theme
    if theme == "Unknown":

        reasons.append(
            "Theme could not be identified"
        )

    elif theme.lower() in [
        "dawn",
        "refresh",
        "sense",
        "craft",
        "ride",
        "taste",
        "origin",
        "publisher",
        "spotlight",
        "studio",
    ]:

        reasons.append(
            f"Uses a common Shopify theme: {theme}"
        )

    # Products
    if product_count == 0:

        reasons.append(
            "Product catalog could not be detected"
        )

    elif product_count < 10:

        reasons.append(
            f"Small product catalog ({product_count} products)"
        )

    elif product_count >= 50:

        reasons.append(
            f"Larger product catalog ({product_count} products)"
        )

    # Missing features
    if not features["FAQ"]:

        reasons.append(
            "No obvious FAQ section detected"
        )

        recommendations.append("FAQ")

    if not features["Testimonials"]:

        reasons.append(
            "No obvious testimonials section detected"
        )

        recommendations.append("Testimonials")

    if not features["Reviews"]:

        reasons.append(
            "No obvious review system detected"
        )

        recommendations.append("Reviews")

    if not features["Sticky Add To Cart"]:

        reasons.append(
            "No obvious sticky Add to Cart detected"
        )

        recommendations.append("Sticky Add to Cart")

    if not features["Size Guide"]:

        reasons.append(
            "No obvious size guide detected"
        )

        recommendations.append("Size Guide")

    if not features["Newsletter"]:

        reasons.append(
            "No obvious newsletter signup detected"
        )

        recommendations.append("Newsletter")

    if not features["WhatsApp"]:

        reasons.append(
            "No obvious WhatsApp contact button detected"
        )

        recommendations.append("Sticky WhatsApp")

    if not features["Countdown"]:

        recommendations.append("Countdown Timer")

    if not features["Instagram"]:

        recommendations.append("Instagram Gallery")

    # Email
    if email:

        reasons.append(
            "Business contact email found"
        )

    return reasons, recommendations


# ============================================================
# SCORE
# ============================================================

def calculate_score(
    product_count,
    features,
    email,
    theme
):

    score = 0

    # Shopify theme
    if theme != "Unknown":
        score += 10

    # Products
    if product_count >= 50:
        score += 20

    elif product_count >= 20:
        score += 15

    elif product_count >= 10:
        score += 10

    elif product_count > 0:
        score += 5

    # Missing conversion features
    if not features["FAQ"]:
        score += 10

    if not features["Testimonials"]:
        score += 10

    if not features["Reviews"]:
        score += 10

    if not features["Sticky Add To Cart"]:
        score += 10

    if not features["Size Guide"]:
        score += 5

    if not features["Newsletter"]:
        score += 5

    if not features["WhatsApp"]:
        score += 5

    # Contactability
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

def analyze_store(url, query):

    base_url = normalize_url(url)

    try:

        html, final_url = fetch_page(base_url)

        if not html:
            return None

        # Shopify check
        if not detect_shopify(html):
            return None

        domain = normalize_domain(final_url)

        if not domain:
            return None

        if is_bad_domain(domain):
            return None

        theme = detect_theme(html)

        product_count = get_product_count(base_url)

        email = find_email(
            base_url,
            html
        )

        features = detect_features(html)

        reasons, recommendations = build_reasons(
            theme,
            product_count,
            features,
            email
        )

        score = calculate_score(
            product_count,
            features,
            email,
            theme
        )

        quality = score_quality(score)

        return {
            "Store URL": base_url,
            "Domain": domain,
            "Theme": theme,
            "Email": email,
            "Products": product_count,
            "Lead Score": score,
            "Quality": quality,

            "FAQ": "YES" if features["FAQ"] else "NO",
            "Testimonials": (
                "YES"
                if features["Testimonials"]
                else "NO"
            ),
            "Reviews": (
                "YES"
                if features["Reviews"]
                else "NO"
            ),
            "Sticky Add To Cart": (
                "YES"
                if features["Sticky Add To Cart"]
                else "NO"
            ),
            "Size Guide": (
                "YES"
                if features["Size Guide"]
                else "NO"
            ),
            "Newsletter": (
                "YES"
                if features["Newsletter"]
                else "NO"
            ),
            "WhatsApp": (
                "YES"
                if features["WhatsApp"]
                else "NO"
            ),

            "Reason": " | ".join(reasons),

            "Recommended Sections": ", ".join(
                recommendations
            ),

            "Search Query": query,
        }

    except Exception as e:

        logger.debug(
            "Store analysis failed %s: %s",
            url,
            str(e)
        )

        return None


# ============================================================
# GOOGLE SHEETS
# ============================================================

HEADERS = [
    "Store URL",
    "Domain",
    "Theme",
    "Email",
    "Products",
    "Lead Score",
    "Quality",
    "FAQ",
    "Testimonials",
    "Reviews",
    "Sticky Add To Cart",
    "Size Guide",
    "Newsletter",
    "WhatsApp",
    "Reason",
    "Recommended Sections",
    "Search Query",
]


def connect_sheet():

    if not GOOGLE_SERVICE_ACCOUNT_JSON:

        logger.error(
            "GOOGLE_SERVICE_ACCOUNT_JSON is missing."
        )

        return None

    if not GOOGLE_SHEET_ID:

        logger.error(
            "GOOGLE_SHEET_ID is missing."
        )

        return None

    try:

        service_account_info = json.loads(
            GOOGLE_SERVICE_ACCOUNT_JSON
        )

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        credentials = Credentials.from_service_account_info(
            service_account_info,
            scopes=scopes
        )

        client = gspread.authorize(credentials)

        spreadsheet = client.open_by_key(
            GOOGLE_SHEET_ID
        )

        try:
            worksheet = spreadsheet.worksheet(
                SHEET_NAME
            )
        except Exception:
            worksheet = spreadsheet.add_worksheet(
                title=SHEET_NAME,
                rows=1000,
                cols=len(HEADERS)
            )

        return worksheet

    except Exception as e:

        logger.error(
            "Google Sheet connection failed: %s",
            str(e)
        )

        return None


def setup_headers(worksheet):

    try:

        current = worksheet.row_values(1)

        if current != HEADERS:

            worksheet.update(
                values=[HEADERS],
                range_name="A1"
            )

            logger.info(
                "Sheet headers updated."
            )

    except Exception as e:

        logger.error(
            "Could not update headers: %s",
            str(e)
        )


def get_existing_domains(worksheet):

    existing = set()

    try:

        values = worksheet.get_all_values()

        if len(values) <= 1:
            return existing

        header = values[0]

        try:
            domain_index = header.index(
                "Domain"
            )
        except ValueError:
            domain_index = 1

        for row in values[1:]:

            if len(row) > domain_index:

                domain = row[domain_index].strip().lower()

                if domain:
                    existing.add(domain)

    except Exception as e:

        logger.error(
            "Could not read existing leads: %s",
            str(e)
        )

    return existing


def row_from_lead(lead):

    return [
        lead.get("Store URL", ""),
        lead.get("Domain", ""),
        lead.get("Theme", ""),
        lead.get("Email", ""),
        lead.get("Products", ""),
        lead.get("Lead Score", ""),
        lead.get("Quality", ""),
        lead.get("FAQ", ""),
        lead.get("Testimonials", ""),
        lead.get("Reviews", ""),
        lead.get("Sticky Add To Cart", ""),
        lead.get("Size Guide", ""),
        lead.get("Newsletter", ""),
        lead.get("WhatsApp", ""),
        lead.get("Reason", ""),
        lead.get("Recommended Sections", ""),
        lead.get("Search Query", ""),
    ]


def save_leads(worksheet, leads):

    if not leads:
        return

    rows = [
        row_from_lead(lead)
        for lead in leads
    ]

    try:

        worksheet.append_rows(
            rows,
            value_input_option="USER_ENTERED"
        )

        logger.info(
            "Saved %s new leads to Google Sheet.",
            len(rows)
        )

    except Exception as e:

        logger.error(
            "Could not save leads: %s",
            str(e)
        )


# ============================================================
# MAIN
# ============================================================

def main():

    logger.info("=" * 70)
    logger.info("SHOPIFY LEAD INTELLIGENCE ENGINE")
    logger.info("=" * 70)

    # --------------------------------------------------------
    # Validate Google config
    # --------------------------------------------------------

    if not validate_google_config():

        logger.error(
            "Google configuration is incomplete."
        )

        return

    # --------------------------------------------------------
    # Connect Sheet
    # --------------------------------------------------------

    worksheet = connect_sheet()

    if worksheet:

        setup_headers(worksheet)

        existing_domains = get_existing_domains(
            worksheet
        )

        logger.info(
            "Existing leads: %s",
            len(existing_domains)
        )

    else:

        logger.warning(
            "Google Sheet unavailable. "
            "The script will still perform discovery."
        )

        existing_domains = set()

    # --------------------------------------------------------
    # Build queries
    # --------------------------------------------------------

    query_pool = build_query_pool()

    queries = query_pool[:MAX_QUERIES_PER_RUN]

    logger.info(
        "Queries this run: %s",
        len(queries)
    )

    # --------------------------------------------------------
    # Discovery
    # --------------------------------------------------------

    discovered = {}

    for index, query in enumerate(
        queries,
        start=1
    ):

        logger.info(
            "[%s/%s] Searching: %s",
            index,
            len(queries),
            query
        )

        urls = search_google(query)

        for url in urls:

            domain = normalize_domain(url)

            if not domain:
                continue

            if is_bad_domain(domain):
                continue

            if domain in existing_domains:
                continue

            if domain not in discovered:

                discovered[domain] = {
                    "url": url,
                    "query": query
                }

        time.sleep(
            GOOGLE_DELAY_SECONDS
        )

    logger.info(
        "Unique domains discovered: %s",
        len(discovered)
    )

    # --------------------------------------------------------
    # Analyze
    # --------------------------------------------------------

    if not discovered:

        logger.warning(
            "No domains discovered."
        )

        logger.warning(
            "If Google showed HTTP 403 above, "
            "DO NOT change the lead analyzer. "
            "Fix Google API configuration first."
        )

        logger.info(
            "RUN COMPLETE"
        )

        return

    candidates = list(
        discovered.values()
    )

    logger.info(
        "New candidates for analysis: %s",
        len(candidates)
    )

    leads = []

    with ThreadPoolExecutor(
        max_workers=MAX_WORKERS
    ) as executor:

        future_map = {}

        for candidate in candidates:

            future = executor.submit(
                analyze_store,
                candidate["url"],
                candidate["query"]
            )

            future_map[future] = candidate

        completed = 0

        for future in as_completed(
            future_map
        ):

            completed += 1

            try:

                lead = future.result()

                if lead:

                    leads.append(lead)

                    logger.info(
                        "[%s/%s] Shopify lead: %s | %s | Score %s",
                        completed,
                        len(candidates),
                        lead["Domain"],
                        lead["Quality"],
                        lead["Lead Score"]
                    )

            except Exception as e:

                logger.error(
                    "Analysis error: %s",
                    str(e)
                )

    # --------------------------------------------------------
    # Sort
    # --------------------------------------------------------

    leads.sort(
        key=lambda x: x.get(
            "Lead Score",
            0
        ),
        reverse=True
    )

    # --------------------------------------------------------
    # Save
    # --------------------------------------------------------

    if worksheet:

        save_leads(
            worksheet,
            leads
        )

    # --------------------------------------------------------
    # Stats
    # --------------------------------------------------------

    hot = sum(
        1
        for lead in leads
        if lead.get("Quality") == "HOT"
    )

    warm = sum(
        1
        for lead in leads
        if lead.get("Quality") == "WARM"
    )

    logger.info("=" * 70)
    logger.info("RUN COMPLETE")
    logger.info("Discovered: %s", len(discovered))
    logger.info("Analyzed: %s", len(candidates))
    logger.info("Shopify leads saved: %s", len(leads))
    logger.info("HOT leads: %s", hot)
    logger.info("WARM leads: %s", warm)
    logger.info("=" * 70)


if __name__ == "__main__":
    main()
