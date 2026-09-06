import requests
from collections import defaultdict
import re
import time
import random
import os
import gspread
from google.oauth2.credentials import Credentials

# Google Custom Search API (free tier: 100 queries/day). DDGS scraping
# hata diya kyunke GitHub Actions IPs search engines ke paas already
# "bot" flagged hain — scraping consistently block/degrade ho rahi thi.
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
GOOGLE_CX = os.environ.get("GOOGLE_CX")
GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

# Free tier daily limit 100 hai — is run mein kitni queries use karni
# hain woh yahan control hota hai. Workflow schedule ke hisab se adjust
# karein (hourly = ~4/run safe, har 4 ghante = ~15/run safe).
MAX_QUERIES_PER_RUN = 15

# ---------- SETTINGS ----------

niches = [
    "jewelry", "candles", "fitness", "skincare", "pet supplies",
    "home decor", "clothing boutique", "shoes", "handbags", "sunglasses",
    "coffee", "tea", "supplements", "baby products", "toys",
    "phone accessories", "art prints", "furniture", "kitchenware", "bags",
    "watches", "beauty products", "outdoor gear", "cycling gear", "yoga",
    "gaming accessories", "electronics", "stationery", "plants", "swimwear",
]

# Shopify ke current + legacy FREE default themes. Agar koi store inme se
# kisi ek ka schema use kar raha hai, matlab unhone koi paid/custom theme
# lagane mein invest nahi kiya — yeh humari asal target hai.
default_theme_names = [
    "dawn", "craft", "sense", "refresh", "taste", "studio", "ride",
    "colorblock", "origin", "debut",
]

# Size filter: bohat chhoti (test/empty) store aur bohat bari (already
# established, apna dev resource rakhne wali) store dono exclude karni
# hain. Sweet spot: chhoti-medium active stores jo abhi bhi free theme
# pe hain — yeh sabse zyada convert hone wala segment hai.
MIN_PRODUCTS = 3
MAX_PRODUCTS = 60

SEARCH_TEMPLATES = [
    '"powered by shopify" {niche} store',
    '{niche} shop "add to cart" shopify',
]

BLOCKED_DOMAINS = [
    "myshopify.com", "shopify.com", "pinterest.com", "facebook.com",
    "instagram.com", "youtube.com", "medium.com", "reddit.com",
    "wikipedia.org", "amazon.com", "etsy.com", "gempages.net",
    "omnisend.com", "bsscommerce.com", "webinopoly.com", "ebay.com",
    "walmart.com", "target.com", "aliexpress.com",
]

SHEET_NAME = "Section Master Leads"

# Default 'python-requests/x.x' User-Agent bohat sari Cloudflare/Shopify
# protected sites turant bot samajh kar block kar deti hain (403 ya
# connection reset). Ek real browser jaisa User-Agent bhejna is masle
# ko kaafi had tak kam kar deta hai.
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}


# ---------- STEP 1: STORE URLs DHOONDNA (Google Custom Search API) ----------

class QuotaExhausted(Exception):
    """Jab is run ka query budget khatam ho jaye — poore run ko gracefully rok dete hain."""
    pass


def google_search(query, num=10):
    """Google Custom Search API se results laata hai. Free tier ki
    100/din limit hai isliye caller (find_store_urls) query count track
    karta hai aur budget khatam hone par QuotaExhausted raise karta hai."""
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        print("⚠️  GOOGLE_API_KEY ya GOOGLE_CX set nahi hai — GitHub Secrets check karein.")
        return []

    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CX,
        "q": query,
        "num": min(num, 10),  # Google API max 10 per request deta hai
    }
    try:
        response = requests.get(GOOGLE_SEARCH_URL, params=params, timeout=15)
    except requests.exceptions.RequestException as e:
        print(f"   ⚠️  Google API request fail ({e}), skip kar rahe hain")
        return []

    if response.status_code == 429:
        raise QuotaExhausted("Google API daily quota khatam ho gayi")
    if response.status_code == 403:
        # Kabhi kabhi quota khatam hone par bhi 403 aata hai (billing/quota error)
        data = response.json()
        reason = data.get("error", {}).get("errors", [{}])[0].get("reason", "")
        if "quota" in reason.lower() or "rateLimitExceeded" in reason:
            raise QuotaExhausted("Google API quota khatam ho gayi (403)")
        print(f"   ⚠️  Google API 403 error: {reason}")
        return []
    if response.status_code != 200:
        print(f"   ⚠️  Google API error: status {response.status_code}")
        return []

    data = response.json()
    items = data.get("items", [])
    return [{"href": item.get("link", ""), "title": item.get("title", "")} for item in items]


def find_store_urls(niche, queries_used):
    """queries_used ek mutable list [int] hai jisse yeh function run ke
    total query count ko update karta hai (caller ke sath shared state)."""
    all_urls = []
    templates = SEARCH_TEMPLATES.copy()
    random.shuffle(templates)

    for template in templates:
        if queries_used[0] >= MAX_QUERIES_PER_RUN:
            break

        query = template.format(niche=niche)
        print(f"🔍 Searching: '{query}'")
        queries_used[0] += 1
        results = google_search(query, num=10)

        for result in results:
            url = result.get("href", "")
            if not url:
                continue
            if any(blocked in url.lower() for blocked in BLOCKED_DOMAINS):
                continue
            if "/blogs/" in url.lower() or "/blog/" in url.lower():
                continue
            all_urls.append(url)

        time.sleep(1)

    return list(dict.fromkeys(all_urls))


# ---------- STEP 2: THEME CHECK KARNA ----------

def check_store(url):
    try:
        response = requests.get(url, timeout=15, headers=DEFAULT_HEADERS)
    except requests.exceptions.RequestException as e:
        return None, "network_error"

    if response.status_code != 200:
        return None, f"status_{response.status_code}"

    html_content = response.text
    schema_match = re.search(r'"schema_name"\s*:\s*"([^"]+)"', html_content)
    name_match = re.search(r'"name"\s*:\s*"([^"]+)"', html_content)
    if not schema_match:
        return None, "no_schema_found"  # page load hui lekin Shopify theme schema nahi mila (false-positive search match)

    schema_name = schema_match.group(1)
    theme_name = name_match.group(1) if name_match else "Unknown"
    is_default = any(default in schema_name.lower() for default in default_theme_names)

    return {
        "url": url,
        "theme_name": theme_name,
        "schema_name": schema_name,
        "is_lead": is_default,
        "homepage_html": html_content,
    }, None


# ---------- STEP 2.5: FEATURE-GAP DETECTION (asal Section Master fit check) ----------

# Section Master jo features deta hai unke common HTML/CSS signatures.
# Agar store ki homepage mein yeh keywords bilkul nahi milte, matlab
# unke paas yeh feature hi nahi hai — strong signal ke unhe app chahiye.
TARGET_FEATURE_KEYWORDS = [
    "faq", "testimonial", "countdown", "sticky-cart", "sticky-add-to-cart",
    "logo-carousel", "hero-slider", "newsletter", "promo-banner",
]

# Agar store pehle se in competing section-builder/page-builder apps
# mein se koi use kar raha hai, to unka masla already solve ho chuka
# hai — is lead ko skip karna behtar hai.
COMPETITOR_SIGNATURES = [
    "pagefly", "gempages", "shogun", "zipify", "ecomposer",
    "vitals-cdn", "pagebuilder-shopify", "shogunpagebuilder",
]


# Har keyword ka insaan-parhne-laiq naam — email personalization ke liye
FEATURE_DISPLAY_NAMES = {
    "faq": "an FAQ section",
    "testimonial": "customer testimonials",
    "countdown": "a countdown/promo timer",
    "sticky-cart": "a sticky add-to-cart bar",
    "sticky-add-to-cart": "a sticky add-to-cart bar",
    "logo-carousel": "a trust-badge/logo carousel",
    "hero-slider": "a hero image slider",
    "newsletter": "a newsletter signup form",
    "promo-banner": "a promotional banner",
}


def analyze_feature_gaps(html_content):
    if not html_content:
        return {"missing_count": 0, "has_competitor": False, "missing_features": []}

    html_lower = html_content.lower()
    has_competitor = any(sig in html_lower for sig in COMPETITOR_SIGNATURES)

    missing_display_names = []
    seen = set()
    for kw in TARGET_FEATURE_KEYWORDS:
        if kw not in html_lower:
            display_name = FEATURE_DISPLAY_NAMES.get(kw, kw)
            if display_name not in seen:
                missing_display_names.append(display_name)
                seen.add(display_name)

    missing_count = len(missing_display_names)

    return {
        "missing_count": missing_count,
        "has_competitor": has_competitor,
        "missing_features": missing_display_names,
    }


# ---------- STEP 3: PRODUCT COUNT CHECK KARNA (Size Filter) ----------

def get_product_count(store_url):
    base_match = re.match(r'(https?://[^/]+)', store_url)
    if not base_match:
        return None

    base_url = base_match.group(1)
    try:
        response = requests.get(f"{base_url}/products.json?limit=250", timeout=15, headers=DEFAULT_HEADERS)
        if response.status_code == 200:
            data = response.json()
            count = len(data.get("products", []))
            return count
    except Exception:
        pass

    return None


# ---------- STEP 4: EMAIL DHOONDNA ----------

IGNORE_KEYWORDS = ["example.com", "sentry", "wixpress", "godaddy", "yourdomain",
                    "domain.com", "email.com", "test.com", ".png", ".jpg", ".jpeg",
                    ".gif", ".heic", ".webp", ".svg", ".bmp", "@2x", "@3x"]


def extract_emails_from_html(html_content):
    email_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    found = re.findall(email_pattern, html_content)
    clean_emails = []
    for email in found:
        email_lower = email.lower()
        username_part = email.split("@")[0]
        looks_like_image_dimension = bool(re.search(r'\d+x\d+', username_part.lower()))
        if not any(bad in email_lower for bad in IGNORE_KEYWORDS) and not looks_like_image_dimension:
            clean_emails.append(email)
    return list(dict.fromkeys(clean_emails))


def extract_facebook_link(html_content):
    fb_pattern = r'https?://(?:www\.)?facebook\.com/[a-zA-Z0-9._-]+'
    found = re.findall(fb_pattern, html_content)
    ignore_fb = ["sharer", "plugins", "dialog", "tr?", "share.php", "l.php"]
    for link in found:
        if not any(bad in link.lower() for bad in ignore_fb):
            return link
    return None


def find_email(store_url):
    emails_found = []
    homepage_html = ""

    try:
        response = requests.get(store_url, timeout=15, headers=DEFAULT_HEADERS)
        if response.status_code == 200:
            homepage_html = response.text
            emails_found.extend(extract_emails_from_html(homepage_html))
    except requests.exceptions.RequestException:
        pass

    if not emails_found:
        base_match = re.match(r'(https?://[^/]+)', store_url)
        if base_match:
            base_url = base_match.group(1)
            contact_urls_to_try = [
                base_url + "/pages/contact",
                base_url + "/pages/contact-us",
                base_url + "/pages/about-us",
                base_url + "/policies/privacy-policy",
                base_url + "/pages/privacy-policy",
            ]
            for contact_url in contact_urls_to_try:
                try:
                    response = requests.get(contact_url, timeout=15, headers=DEFAULT_HEADERS)
                    if response.status_code == 200:
                        emails_found.extend(extract_emails_from_html(response.text))
                        if emails_found:
                            break
                except requests.exceptions.RequestException:
                    continue

    if emails_found:
        return emails_found[0]

    if homepage_html:
        fb_link = extract_facebook_link(homepage_html)
        if fb_link:
            return f"Facebook: {fb_link}"

    return "Nahi mila"


# ---------- STEP 5: LEAD SCORE CALCULATE KARNA ----------

def calculate_lead_score(product_count, email_found, missing_feature_count):
    """Ab feature-gap (kitne Section Master jaisi cheezein missing hain)
    sabse bara signal hai — jitni zyada missing utni behtar lead."""
    score = 20  # base: confirmed default theme

    gap_ratio = missing_feature_count / len(TARGET_FEATURE_KEYWORDS)
    score += round(gap_ratio * 35)  # up to 35 pts agar sab features missing hon

    if product_count >= 15:
        score += 20
    elif product_count >= 3:
        score += 10
    else:
        score += 3

    if email_found and email_found != "Nahi mila" and not email_found.startswith("Facebook"):
        score += 25

    return min(score, 100)


def get_quality_label(score):
    if score >= 70:
        return "🔥 Hot Lead"
    elif score >= 40:
        return "⭐ Warm Lead"
    else:
        return "🌱 Cold Lead"


# ---------- STEP 6: GOOGLE SHEETS ----------

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]


def get_or_create_sheet():
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    gc = gspread.authorize(creds)
    try:
        sheet = gc.open(SHEET_NAME).sheet1
    except gspread.SpreadsheetNotFound:
        spreadsheet = gc.create(SHEET_NAME)
        sheet = spreadsheet.sheet1
        sheet.append_row(["Store URL", "Niche", "Theme", "Email", "Products", "Missing Features", "Lead Score", "Quality", "Reason"])
    return sheet


def get_existing_urls(sheet):
    try:
        existing_rows = sheet.get_all_values()[1:]
        return set(row[0] for row in existing_rows if row)
    except Exception:
        return set()


def save_lead_to_sheet(sheet, lead):
    sheet.append_row([
        lead["url"],
        lead["niche"],
        lead["theme_name"],
        lead["email"],
        lead["product_count"],
        ", ".join(lead.get("missing_features", [])),
        lead["score"],
        lead["quality"],
        lead["reason"],
    ])


# ---------- MAIN ----------

TARGET_LEADS_PER_RUN = 12


def run_once():
    print("📊 Google Sheet se connect ho rahe hain...")
    sheet = get_or_create_sheet()
    existing_urls = get_existing_urls(sheet)
    print(f"✅ Connected! Sheet mein pehle se {len(existing_urls)} leads hain.\n")

    new_leads_count = 0
    hot_leads_count = 0
    skipped_not_default = 0
    skipped_size = 0
    skipped_competitor = 0
    skipped_fetch_failed = 0
    fetch_fail_reasons = defaultdict(int)
    tried_urls = set()  # is run mein already check ki gayi URLs (pass ho ya fail) — dobara try na ho
    queries_used = [0]  # mutable — find_store_urls isse update karta hai

    shuffled_niches = niches.copy()
    random.shuffle(shuffled_niches)

    quota_hit = False

    for niche in shuffled_niches:
        if new_leads_count >= TARGET_LEADS_PER_RUN:
            break
        if queries_used[0] >= MAX_QUERIES_PER_RUN:
            print(f"\n⚠️  Is run ka query budget ({MAX_QUERIES_PER_RUN}) khatam ho gaya, ruk rahe hain.")
            break

        try:
            urls = find_store_urls(niche, queries_used)
        except QuotaExhausted as e:
            print(f"\n🛑 {e} — is run ko yahin rok rahe hain (kal/agle window mein quota reset hoga).")
            quota_hit = True
            break

        print(f"   {len(urls)} store URLs mile is niche mein ({niche}). [Queries used: {queries_used[0]}/{MAX_QUERIES_PER_RUN}]\n")

        for url in urls:
            if new_leads_count >= TARGET_LEADS_PER_RUN:
                break
            if url in existing_urls or url in tried_urls:
                continue
            tried_urls.add(url)

            result, fail_reason = check_store(url)
            time.sleep(1)

            if result is None:
                skipped_fetch_failed += 1
                fetch_fail_reasons[fail_reason] += 1
                continue

            if not result["is_lead"]:
                skipped_not_default += 1
                continue

            product_count = get_product_count(url)
            time.sleep(1)
            if product_count is None or product_count < MIN_PRODUCTS or product_count > MAX_PRODUCTS:
                skipped_size += 1
                continue

            gap_info = analyze_feature_gaps(result.get("homepage_html"))
            if gap_info["has_competitor"]:
                skipped_competitor += 1
                continue

            missing_count = gap_info["missing_count"]
            result["missing_features"] = gap_info["missing_features"]

            result["niche"] = niche
            result["reason"] = (
                f"Default '{result['schema_name']}' theme, {product_count} products, "
                f"missing {missing_count}/{len(TARGET_FEATURE_KEYWORDS)} target features "
                f"(FAQ/testimonials/sticky-cart/etc.) — strong Section Master fit"
            )

            email = find_email(url)
            time.sleep(1)

            result["email"] = email
            result["product_count"] = product_count

            score = calculate_lead_score(product_count, email, missing_count)
            result["score"] = score
            result["quality"] = get_quality_label(score)

            save_lead_to_sheet(sheet, result)
            existing_urls.add(url)
            new_leads_count += 1
            if score >= 70:
                hot_leads_count += 1

            print(f"   [{new_leads_count}/{TARGET_LEADS_PER_RUN}] {result['quality']} (Score: {score}) — {url} | Products: {product_count} | Email: {email}")

    print(f"\n===== DONE =====")
    print(f"Total {new_leads_count} nayi leads add hui, jisme se {hot_leads_count} 🔥 Hot Leads hain!")
    print(f"Queries used this run: {queries_used[0]}/{MAX_QUERIES_PER_RUN}")
    print(f"Skipped: {skipped_fetch_failed} (fetch/network fail — breakdown: {dict(fetch_fail_reasons)}), {skipped_not_default} (custom theme), {skipped_size} (size filter se bahar), {skipped_competitor} (already competitor app use kar rahe)")

    if new_leads_count < TARGET_LEADS_PER_RUN and not quota_hit:
        print(f"\n⚠️  Target {TARGET_LEADS_PER_RUN} tak nahi pahunch saka (sirf {new_leads_count} mile). Isका matlab:")
        print("    - Is niche list mein itni fresh (pehle se sheet mein na hoin) qualifying stores nahi bachin, ya")
        print("    - Filters bohat tight hain is volume ke liye — niches list barhayein ya MIN/MAX_PRODUCTS thoda relax karein.\n")
    elif quota_hit:
        print(f"\n⚠️  Google API ka daily quota is run mein khatam ho gaya. Agla run zyada leads laa sakta hai jab quota reset ho.\n")


if __name__ == "__main__":
    run_once()
