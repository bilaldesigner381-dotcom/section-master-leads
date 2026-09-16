import os
import requests
import re
import time
import random
import gspread
from google.oauth2.credentials import Credentials

# ---------- SETTINGS ----------

# Google Cloud Console -> "Custom Search API" enable karo -> API key banao
GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
# https://programmablesearchengine.google.com -> naya search engine banao
# -> "Search the entire web" ON karo -> Search Engine ID (cx) copy karo
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID")

GOOGLE_SEARCH_URL = "https://www.googleapis.com/customsearch/v1"

# Page 6 = results 51-60. Free tier 100 queries/day deta hai,
# aur har query mein sirf 1 API call lagti hai (num=10, start=51).
START_POSITION = 51
RESULTS_PER_QUERY = 10

MAX_QUERIES_PER_RUN = 90  # 100 ki limit se thoda neeche, safety margin ke liye

SHEET_NAME = "Section Master Leads"
QUERY_STATE_TAB = "QueryState"

# ---------- QUERY POOL (Advanced auto-expanding query generator) ----------
# AI model call kiye bina, yeh niches x templates x modifiers combine karke
# 1800+ unique search queries banata hai. Har run mein agle N naye queries
# (jo pehle try nahi hue) use hote hain, aur position (pointer) Sheet mein
# save hota hai — taake system khud "yaad" rakhe kahan tak pahoncha tha.

niches = [
    "jewelry", "candles", "fitness", "skincare", "pet supplies",
    "home decor", "clothing boutique", "shoes", "handbags", "sunglasses",
    "coffee", "tea", "supplements", "baby products", "toys",
    "phone accessories", "art prints", "furniture", "kitchenware", "bags",
    "watches", "beauty products", "outdoor gear", "cycling gear", "yoga",
    "gaming accessories", "electronics", "stationery", "plants", "swimwear",
]

BASE_TEMPLATES = [
    '"powered by shopify" {niche} store',
    '{niche} online store shopify -site:myshopify.com',
    '{niche} shop "add to cart" shopify',
    'buy {niche} online shopify store',
    '{niche} boutique shopify -blog',
]

EXTRA_MODIFIERS = [
    "", "best", "top rated", "new arrivals", "on sale", "discount",
    "free shipping", "premium", "handmade", "eco friendly", "affordable", "trending",
]

default_theme_names = ["dawn", "debut", "craft", "sense", "refresh", "taste", "studio", "ride"]

BLOCKED_DOMAINS = [
    "myshopify.com", "shopify.com", "pinterest.com", "facebook.com",
    "instagram.com", "youtube.com", "medium.com", "reddit.com",
    "wikipedia.org", "amazon.com", "etsy.com", "gempages.net",
    "omnisend.com", "bsscommerce.com", "webinopoly.com", "ebay.com",
    "walmart.com", "target.com", "aliexpress.com",
]


def build_query_pool():
    """Niches x templates x modifiers combine karke bara pool banata hai.
    Fixed seed ke saath shuffle karta hai taake har baar same (deterministic)
    order mile — isse pointer system reliably kaam karta hai."""
    pool = []
    for niche in niches:
        for template in BASE_TEMPLATES:
            base_query = template.format(niche=niche)
            for modifier in EXTRA_MODIFIERS:
                query = f"{base_query} {modifier}".strip()
                pool.append(query)

    rng = random.Random(42)  # fixed seed -> deterministic order, but "random" lagta hai
    rng.shuffle(pool)
    return pool


QUERY_POOL = build_query_pool()
print(f"🧠 Query pool ban gaya: {len(QUERY_POOL)} unique queries available.")


# ---------- QUERY POINTER (Sheet mein persist hota hai) ----------

def get_or_create_state_tab(spreadsheet):
    try:
        return spreadsheet.worksheet(QUERY_STATE_TAB)
    except gspread.WorksheetNotFound:
        tab = spreadsheet.add_worksheet(title=QUERY_STATE_TAB, rows=2, cols=2)
        tab.update("A1", [["next_index", "0"]])
        return tab


def get_next_index(state_tab):
    try:
        value = state_tab.acell("B1").value
        return int(value) if value else 0
    except Exception:
        return 0


def save_next_index(state_tab, index):
    state_tab.update("B1", [[str(index)]])


def get_next_queries(state_tab, count):
    """Pool se agle 'count' naye queries deta hai, pointer aage badhata hai.
    Pool khatam hone par wapas 0 se cycle kar leta hai."""
    start_index = get_next_index(state_tab)
    pool_size = len(QUERY_POOL)

    selected = []
    for i in range(count):
        idx = (start_index + i) % pool_size
        selected.append(QUERY_POOL[idx])

    new_index = (start_index + count) % pool_size
    save_next_index(state_tab, new_index)

    return selected


# ---------- STEP 1: GOOGLE PAR SEARCH KARNA (Custom Search API) ----------

def search_google(query):
    """Position 51-60 (page 6) ka block mangwata hai — 1 hi API call."""
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        print("⚠️  GOOGLE_API_KEY ya GOOGLE_CSE_ID set nahi hai.")
        return []

    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CSE_ID,
        "q": query,
        "start": START_POSITION,
        "num": RESULTS_PER_QUERY,
    }

    try:
        response = requests.get(GOOGLE_SEARCH_URL, params=params, timeout=15)
        if response.status_code == 429:
            print("   ⚠️  Daily quota khatam ho gaya (429), rok rahe hain.")
            return None  # signal: quota khatam, poora run rok do
        response.raise_for_status()
        items = response.json().get("items", [])
        return [item.get("link", "") for item in items if item.get("link")]
    except Exception as e:
        print(f"   ⚠️  Google search fail ho gayi: {e}")
        return []


# ---------- STEP 2: THEME CHECK KARNA ----------

def check_store(url):
    try:
        response = requests.get(url, timeout=10)
    except requests.exceptions.RequestException:
        return None
    if response.status_code != 200:
        return None

    html_content = response.text
    schema_match = re.search(r'"schema_name"\s*:\s*"([^"]+)"', html_content)
    name_match = re.search(r'"name"\s*:\s*"([^"]+)"', html_content)
    if not schema_match:
        return None

    schema_name = schema_match.group(1)
    theme_name = name_match.group(1) if name_match else "Unknown"
    is_default = any(default in schema_name.lower() for default in default_theme_names)

    return {
        "url": url,
        "theme_name": theme_name,
        "schema_name": schema_name,
        "is_lead": is_default,
    }


# ---------- STEP 3: PRODUCT COUNT CHECK KARNA ----------

def get_product_count(store_url):
    base_match = re.match(r'(https?://[^/]+)', store_url)
    if not base_match:
        return None

    base_url = base_match.group(1)
    try:
        response = requests.get(f"{base_url}/products.json?limit=250", timeout=10)
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
        response = requests.get(store_url, timeout=10)
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
                    response = requests.get(contact_url, timeout=10)
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

def calculate_lead_score(is_default_theme, product_count, email_found):
    score = 0

    if is_default_theme:
        score += 40
    else:
        score += 10

    if product_count is not None:
        if product_count >= 20:
            score += 35
        elif product_count >= 5:
            score += 20
        else:
            score += 5

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
        spreadsheet = gc.open(SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        spreadsheet = gc.create(SHEET_NAME)

    sheet = spreadsheet.sheet1
    if not sheet.get_all_values():
        sheet.append_row(["Store URL", "Niche", "Theme", "Email", "Products", "Lead Score", "Quality", "Reason", "Search Query"])

    return spreadsheet, sheet


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
        lead["product_count"] if lead["product_count"] is not None else "N/A",
        lead["score"],
        lead["quality"],
        lead["reason"],
        lead.get("query", "N/A"),
    ])


# ---------- MAIN ----------

def run_once():
    print("📊 Google Sheet se connect ho rahe hain...")
    spreadsheet, sheet = get_or_create_sheet()
    state_tab = get_or_create_state_tab(spreadsheet)
    existing_urls = get_existing_urls(sheet)
    print(f"✅ Connected! Sheet mein pehle se {len(existing_urls)} leads hain.\n")

    queries = get_next_queries(state_tab, MAX_QUERIES_PER_RUN)
    print(f"📝 Is run ke liye {len(queries)} naye queries select hue (pool se).\n")

    new_leads_count = 0
    hot_leads_count = 0

    for query in queries:
        print(f"🔍 Searching (page 6): '{query}'")
        result_urls = search_google(query)

        if result_urls is None:
            print("🛑 Google quota khatam ho gaya, run yahin rok rahe hain.")
            break

        filtered_urls = []
        for url in result_urls:
            if any(blocked in url.lower() for blocked in BLOCKED_DOMAINS):
                continue
            if "/blogs/" in url.lower() or "/blog/" in url.lower():
                continue
            filtered_urls.append(url)

        for url in filtered_urls:
            if url in existing_urls:
                continue

            result = check_store(url)
            time.sleep(1)

            if result is None:
                continue

            is_default = result["is_lead"]
            result["niche"] = "unknown"
            result["query"] = query
            if is_default:
                result["reason"] = f"Default '{result['schema_name']}' theme use kar rahe hain — customization ki zaroorat hai"
            else:
                result["reason"] = f"Custom '{result['schema_name']}' theme, lekin phir bhi potential lead"

            email = find_email(url)
            result["email"] = email
            time.sleep(1)

            product_count = get_product_count(url)
            result["product_count"] = product_count
            time.sleep(1)

            score = calculate_lead_score(is_default, product_count, email)
            result["score"] = score
            result["quality"] = get_quality_label(score)

            save_lead_to_sheet(sheet, result)
            existing_urls.add(url)
            new_leads_count += 1
            if score >= 70:
                hot_leads_count += 1

            print(f"   {result['quality']} (Score: {score}) — {url} | Products: {product_count} | Email: {email}")

        time.sleep(1)

    print(f"\n===== DONE =====")
    print(f"Total {new_leads_count} nayi leads add hui, jisme se {hot_leads_count} 🔥 Hot Leads hain!\n")


if __name__ == "__main__":
    run_once()
