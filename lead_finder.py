import requests
import re
import time
import random
import gspread
from google.oauth2.credentials import Credentials
from ddgs import DDGS

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
    '{niche} online store shopify -site:myshopify.com',
    '{niche} shop "add to cart" shopify',
    'buy {niche} online shopify store',
    '{niche} boutique shopify -blog',
]

BLOCKED_DOMAINS = [
    "myshopify.com", "shopify.com", "pinterest.com", "facebook.com",
    "instagram.com", "youtube.com", "medium.com", "reddit.com",
    "wikipedia.org", "amazon.com", "etsy.com", "gempages.net",
    "omnisend.com", "bsscommerce.com", "webinopoly.com", "ebay.com",
    "walmart.com", "target.com", "aliexpress.com",
]

SHEET_NAME = "Section Master Leads"


# ---------- STEP 1: STORE URLs DHOONDNA ----------

def find_store_urls(niche, max_results=10):
    all_urls = []
    templates = SEARCH_TEMPLATES.copy()
    random.shuffle(templates)

    for template in templates:
        query = template.format(niche=niche)
        print(f"🔍 Searching: '{query}'")
        try:
            results = DDGS().text(query, max_results=max_results)
        except Exception as e:
            print(f"   ⚠️  Search fail ho gayi ({e}), skip kar rahe hain")
            continue

        for result in results:
            url = result.get("href", "")
            if not url:
                continue
            if any(blocked in url.lower() for blocked in BLOCKED_DOMAINS):
                continue
            if "/blogs/" in url.lower() or "/blog/" in url.lower():
                continue
            all_urls.append(url)

        time.sleep(2)

    return list(dict.fromkeys(all_urls))


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


# ---------- STEP 3: PRODUCT COUNT CHECK KARNA (Size Filter) ----------

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

def calculate_lead_score(product_count, email_found):
    """Ab sirf default-theme stores hi yahan tak pahunchti hain, isliye
    theme wala factor hata diya — ab size aur contactability pe focus hai."""
    score = 40  # base: confirmed default theme

    if product_count >= 15:
        score += 35  # active, real inventory wali store
    elif product_count >= 3:
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
        sheet = gc.open(SHEET_NAME).sheet1
    except gspread.SpreadsheetNotFound:
        spreadsheet = gc.create(SHEET_NAME)
        sheet = spreadsheet.sheet1
        sheet.append_row(["Store URL", "Niche", "Theme", "Email", "Products", "Lead Score", "Quality", "Reason"])
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
        lead["score"],
        lead["quality"],
        lead["reason"],
    ])


# ---------- MAIN ----------

def run_once():
    print("📊 Google Sheet se connect ho rahe hain...")
    sheet = get_or_create_sheet()
    existing_urls = get_existing_urls(sheet)
    print(f"✅ Connected! Sheet mein pehle se {len(existing_urls)} leads hain.\n")

    new_leads_count = 0
    hot_leads_count = 0
    skipped_not_default = 0
    skipped_size = 0

    shuffled_niches = niches.copy()
    random.shuffle(shuffled_niches)

    for niche in shuffled_niches:
        urls = find_store_urls(niche, max_results=10)
        print(f"   {len(urls)} store URLs mile is niche mein.\n")
        time.sleep(3)

        for url in urls:
            if url in existing_urls:
                continue

            result = check_store(url)
            time.sleep(1)

            if result is None:
                continue

            # FILTER 1: sirf default/free theme wali stores chahiye
            if not result["is_lead"]:
                skipped_not_default += 1
                continue

            # FILTER 2: size ka sweet spot — bohat chhoti ya bohat bari nahi
            product_count = get_product_count(url)
            time.sleep(1)
            if product_count is None or product_count < MIN_PRODUCTS or product_count > MAX_PRODUCTS:
                skipped_size += 1
                continue

            result["niche"] = niche
            result["reason"] = f"Default '{result['schema_name']}' theme, {product_count} products — customization ki zaroorat hai"

            email = find_email(url)
            time.sleep(1)

            result["email"] = email
            result["product_count"] = product_count

            score = calculate_lead_score(product_count, email)
            result["score"] = score
            result["quality"] = get_quality_label(score)

            save_lead_to_sheet(sheet, result)
            existing_urls.add(url)
            new_leads_count += 1
            if score >= 70:
                hot_leads_count += 1

            print(f"   {result['quality']} (Score: {score}) — {url} | Products: {product_count} | Email: {email}")

    print(f"\n===== DONE =====")
    print(f"Total {new_leads_count} nayi leads add hui, jisme se {hot_leads_count} 🔥 Hot Leads hain!")
    print(f"Skipped: {skipped_not_default} (custom theme), {skipped_size} (size filter se bahar)\n")


if __name__ == "__main__":
    run_once()
