import re
import time
import random
import requests
import gspread
from google.oauth2.credentials import Credentials
from ddgs import DDGS

# =====================================================================
# SETTINGS
# =====================================================================

# Local business niches jinko usually website ki zaroorat hoti hai
NICHES = [
    "restaurant", "cafe", "bakery", "catering", "food truck",
    "hair salon", "barbershop", "spa", "nail salon",
    "gym", "yoga studio", "personal trainer",
    "plumber", "electrician", "hvac contractor", "roofer", "landscaping",
    "cleaning service", "auto repair shop", "car wash",
    "real estate agent", "dentist", "chiropractor", "law firm",
    "accountant", "photographer", "event planner", "florist",
    "pet grooming", "daycare", "tutoring center", "driving school",
]

# Seed city/region list — NOTE: yeh "duniya ke har city" ka chhota sa
# starting sample hai. Isko aap khud CSV/list se expand kar sakte hain
# (jitni cities add karenge utna zyada coverage milega, bas run time
# aur DDGS rate-limit ka khayal rakhein).
LOCATIONS = [
    ("Houston", "TX", "USA"), ("Dallas", "TX", "USA"), ("Miami", "FL", "USA"),
    ("Orlando", "FL", "USA"), ("Atlanta", "GA", "USA"), ("Phoenix", "AZ", "USA"),
    ("Chicago", "IL", "USA"), ("Los Angeles", "CA", "USA"), ("Austin", "TX", "USA"),
    ("Denver", "CO", "USA"), ("Seattle", "WA", "USA"), ("Boston", "MA", "USA"),
    ("London", "", "UK"), ("Manchester", "", "UK"), ("Birmingham", "", "UK"),
    ("Toronto", "ON", "Canada"), ("Vancouver", "BC", "Canada"),
    ("Sydney", "NSW", "Australia"), ("Melbourne", "VIC", "Australia"),
    ("Dubai", "", "UAE"), ("Abu Dhabi", "", "UAE"),
    ("Karachi", "", "Pakistan"), ("Lahore", "", "Pakistan"), ("Islamabad", "", "Pakistan"),
    ("Riyadh", "", "Saudi Arabia"), ("Jeddah", "", "Saudi Arabia"),
    ("Berlin", "", "Germany"), ("Amsterdam", "", "Netherlands"),
]

PLATFORMS = ["facebook.com", "instagram.com", "linkedin.com"]

# Directories/socials jinko "independent website" nahi maana jayega
NON_WEBSITE_DOMAINS = [
    "facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com",
    "yelp.com", "tripadvisor.com", "yellowpages.com", "mapquest.com",
    "google.com", "maps.google.com", "opentable.com", "foursquare.com",
    "thefork.com", "zomato.com", "justdial.com", "pinterest.com",
    "youtube.com", "tiktok.com", "wikipedia.org", "indeed.com", "glassdoor.com",
]

IGNORE_EMAIL_KEYWORDS = [
    "example.com", "sentry", "wixpress", "godaddy", "yourdomain",
    "domain.com", "email.com", "test.com", ".png", ".jpg", ".jpeg",
    ".gif", ".webp", ".svg", "noreply", "no-reply", "@2x", "@3x",
]

SHEET_NAME = "WebDev Leads"

MAX_RESULTS_PER_QUERY = 8
MAX_CANDIDATES_TO_VERIFY_PER_QUERY = 5   # rate-limit / run-time control
QUERIES_PER_RUN = 12                     # ek run mein kitne (niche+location+platform) combos try karne hain


# =====================================================================
# STEP 1: SOCIAL PROFILE URLS DHOONDNA (DDGS ke zariye, direct scraping nahi)
# =====================================================================

def ddgs_search(query, max_results=MAX_RESULTS_PER_QUERY):
    try:
        return DDGS().text(query, max_results=max_results)
    except Exception as e:
        print(f"   ⚠️  Search fail ho gayi ({e}), skip kar rahe hain")
        return []


def find_social_leads(niche, city, region, country, platform):
    location_str = " ".join(p for p in [city, region, country] if p)
    query = f'site:{platform} "{niche}" "{city}" "{country}"'
    print(f"🔍 Searching: '{query}'")
    results = ddgs_search(query)

    leads = []
    for r in results:
        url = r.get("href", "")
        title = r.get("title", "")
        body = r.get("body", "")
        if not url:
            continue
        leads.append({
            "platform": platform,
            "profile_url": url,
            "title": title,
            "body": body,
            "niche": niche,
            "city": city,
            "region": region,
            "country": country,
        })
    return leads


def clean_business_name(title, platform):
    name = title
    for suffix in [" | Facebook", " - Facebook", " (@", " | LinkedIn", " - LinkedIn", " • Instagram photos and videos"]:
        idx = name.find(suffix)
        if idx != -1:
            name = name[:idx]
    return name.strip()


# =====================================================================
# STEP 2: CHECK KARNA KE INDEPENDENT WEBSITE HAI YA NAHI
# =====================================================================

def has_own_website(business_name, city, country):
    """Business naam + location dobara search karke check karta hai ke
    koi independent (non-social/non-directory) website mila ya nahi."""
    if not business_name:
        return False

    query = f'"{business_name}" {city} {country}'
    results = ddgs_search(query, max_results=6)

    for r in results:
        url = r.get("href", "").lower()
        if not url:
            continue
        if any(domain in url for domain in NON_WEBSITE_DOMAINS):
            continue
        # koi aur (independent) domain mil gaya -> pehle se website hai
        return True

    return False


# =====================================================================
# STEP 3: EMAIL DHOONDNA (search snippets + secondary contact-search)
# =====================================================================

EMAIL_PATTERN = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'


def extract_emails_from_text(text):
    if not text:
        return []
    found = re.findall(EMAIL_PATTERN, text)
    clean = []
    for email in found:
        email_lower = email.lower()
        if not any(bad in email_lower for bad in IGNORE_EMAIL_KEYWORDS):
            clean.append(email)
    return list(dict.fromkeys(clean))


def find_email(lead):
    # 1) pehle jo search snippet (body/title) mila usi mein email dhoondo
    emails = extract_emails_from_text(lead.get("body", "") + " " + lead.get("title", ""))
    if emails:
        return emails[0]

    # 2) best-effort: profile page khud khol kar dekho (bohat baar login-wall
    #    ki wajah se fail hoga — isliye chhota timeout aur silent fail)
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; LeadResearchBot/1.0)"}
        resp = requests.get(lead["profile_url"], headers=headers, timeout=6)
        if resp.status_code == 200:
            emails = extract_emails_from_text(resp.text)
            if emails:
                return emails[0]
    except requests.exceptions.RequestException:
        pass

    # 3) secondary DDGS search: business naam + city + "email" / "contact"
    business_name = clean_business_name(lead.get("title", ""), lead["platform"])
    if business_name:
        query = f'"{business_name}" {lead["city"]} email contact'
        results = ddgs_search(query, max_results=5)
        for r in results:
            emails = extract_emails_from_text(r.get("body", "") + " " + r.get("title", ""))
            if emails:
                return emails[0]

    return "Nahi mila"


# =====================================================================
# STEP 4: LEAD SCORE
# =====================================================================

def calculate_lead_score(has_website, email_found):
    score = 0
    if not has_website:
        score += 50  # asal signal: website hi nahi hai
    else:
        score += 5

    if email_found and email_found != "Nahi mila":
        score += 35

    score += 15  # base: social presence active hai, matlab active business hai
    return min(score, 100)


def get_quality_label(score):
    if score >= 70:
        return "🔥 Hot Lead"
    elif score >= 40:
        return "⭐ Warm Lead"
    else:
        return "🌱 Cold Lead"


# =====================================================================
# STEP 5: GOOGLE SHEETS
# =====================================================================

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]


def get_or_create_sheet():
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    gc = gspread.authorize(creds)
    try:
        sheet = gc.open(SHEET_NAME).sheet1
    except gspread.SpreadsheetNotFound:
        spreadsheet = gc.create(SHEET_NAME)
        sheet = spreadsheet.sheet1
        sheet.append_row([
            "Platform", "Profile URL", "Business Name", "Niche",
            "City", "Region", "Country", "Email",
            "Has Own Website", "Lead Score", "Quality",
        ])
    return sheet


def get_existing_urls(sheet):
    try:
        rows = sheet.get_all_values()[1:]
        return set(row[1] for row in rows if len(row) > 1)
    except Exception:
        return set()


def save_lead_to_sheet(sheet, lead):
    sheet.append_row([
        lead["platform"],
        lead["profile_url"],
        lead["business_name"],
        lead["niche"],
        lead["city"],
        lead["region"],
        lead["country"],
        lead["email"],
        "No" if not lead["has_website"] else "Yes",
        lead["score"],
        lead["quality"],
    ])


# =====================================================================
# MAIN
# =====================================================================

def run_once():
    print("📊 Google Sheet se connect ho rahe hain...")
    sheet = get_or_create_sheet()
    existing_urls = get_existing_urls(sheet)
    print(f"✅ Connected! Sheet mein pehle se {len(existing_urls)} leads hain.\n")

    # Har run mein alag combos try karne ke liye shuffle
    combos = []
    for niche in NICHES:
        for city, region, country in LOCATIONS:
            for platform in PLATFORMS:
                combos.append((niche, city, region, country, platform))
    random.shuffle(combos)
    combos = combos[:QUERIES_PER_RUN]

    new_leads_count = 0
    hot_leads_count = 0

    for niche, city, region, country, platform in combos:
        social_leads = find_social_leads(niche, city, region, country, platform)
        time.sleep(2)

        checked = 0
        for lead in social_leads:
            if checked >= MAX_CANDIDATES_TO_VERIFY_PER_QUERY:
                break
            if lead["profile_url"] in existing_urls:
                continue

            business_name = clean_business_name(lead["title"], platform)
            checked += 1

            website_exists = has_own_website(business_name, city, country)
            time.sleep(1)

            if website_exists:
                # pehle se website hai -> hamari lead nahi
                continue

            email = find_email(lead)
            time.sleep(1)

            score = calculate_lead_score(website_exists, email)
            quality = get_quality_label(score)

            lead.update({
                "business_name": business_name,
                "email": email,
                "has_website": website_exists,
                "score": score,
                "quality": quality,
            })

            save_lead_to_sheet(sheet, lead)
            existing_urls.add(lead["profile_url"])
            new_leads_count += 1
            if score >= 70:
                hot_leads_count += 1

            print(f"   {quality} (Score: {score}) — {business_name} ({platform}) | Email: {email}")

    print(f"\n===== DONE =====")
    print(f"Total {new_leads_count} nayi leads add hui, jisme se {hot_leads_count} 🔥 Hot Leads hain!\n")


if __name__ == "__main__":
    run_once()
