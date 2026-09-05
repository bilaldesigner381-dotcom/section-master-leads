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

PLATFORMS = ["facebook.com", "instagram.com", "linkedin.com/company"]

# Personal profiles, posts, groups, and other non-business-page URL patterns —
# in inko lead ke taur par nahi lena kyunke yeh actual business page nahi hain
JUNK_URL_PATTERNS = [
    "facebook.com/people/", "facebook.com/profile.php", "facebook.com/groups/",
    "facebook.com/pages/category", "facebook.com/watch", "facebook.com/marketplace",
    "instagram.com/p/", "instagram.com/reel/", "instagram.com/stories/", "instagram.com/explore/",
    "linkedin.com/in/", "linkedin.com/posts/", "linkedin.com/jobs/",
]

# Yeh keywords indicate karte hain ke business band ho chuka hai — aisi
# leads bilkul faida mand nahi, isliye skip
CLOSED_KEYWORDS = [
    "permanently closed", "temporarily closed", "out of business",
    "no longer in business", "this business has closed",
]

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

# Sirf inhi free/generic email providers ko "quality lead" mana jayega.
# Business jo apna khud ka domain-email (info@apnicompany.com) use kar raha
# ho, uska matlab hai woh pehle se kisi domain/hosting ka istemal kar raha
# hai — jo hamare "no website" signal ko kamzor karta hai. Isliye sirf
# generic-provider email wali leads hi rakhi jayengi.
FREE_EMAIL_PROVIDERS = {
    "gmail.com", "googlemail.com",
    "hotmail.com", "hotmail.co.uk", "hotmail.fr",
    "yahoo.com", "yahoo.co.uk", "yahoo.com.au",
    "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com",
    "aol.com", "ymail.com", "rocketmail.com",
    "protonmail.com", "proton.me",
}


def is_free_provider_email(email):
    if "@" not in email:
        return False
    domain = email.split("@")[-1].lower().strip()
    return domain in FREE_EMAIL_PROVIDERS

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

        url_lower = url.lower()
        if any(pattern in url_lower for pattern in JUNK_URL_PATTERNS):
            continue  # personal profile, post, group, ya job listing — business page nahi

        combined_text = f"{title} {body}".lower()
        if any(kw in combined_text for kw in CLOSED_KEYWORDS):
            continue  # business band ho chuka hai

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


def extract_emails_from_text(text, free_provider_only=True):
    if not text:
        return []
    found = re.findall(EMAIL_PATTERN, text)
    clean = []
    for email in found:
        email_lower = email.lower()
        if any(bad in email_lower for bad in IGNORE_EMAIL_KEYWORDS):
            continue
        if free_provider_only and not is_free_provider_email(email_lower):
            continue
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
# STEP 3.5: EXTRA QUALITY SIGNALS (phone number, ratings/reviews)
# =====================================================================

PHONE_PATTERN = r'(\+?\d[\d\s().-]{7,}\d)'
RATING_PATTERN = r'(\d(?:\.\d)?)\s*(?:stars?|★|/\s*5)'
REVIEW_COUNT_PATTERN = r'(\d+)\s*(?:reviews?|ratings?)'


def has_phone_number(text):
    if not text:
        return False
    return bool(re.search(PHONE_PATTERN, text))


def has_ratings_or_reviews(text):
    if not text:
        return False
    return bool(re.search(RATING_PATTERN, text, re.IGNORECASE) or re.search(REVIEW_COUNT_PATTERN, text, re.IGNORECASE))


# =====================================================================
# STEP 4: LEAD SCORE
# =====================================================================

def calculate_lead_score(has_website, email_found, has_phone, has_reviews):
    """Zyada signals = zyada confident lead ke active/real business hone ka."""
    score = 0

    if not has_website:
        score += 35  # asal signal: apni website nahi hai
    else:
        score += 5

    if email_found and email_found != "Nahi mila":
        score += 25  # free-provider email mila = aasani se contact ho sakta hai

    if has_phone:
        score += 20  # phone number listed = active/reachable business

    if has_reviews:
        score += 15  # ratings/reviews mile = active customer base

    score += 5  # base: social page active hai
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
            "Has Own Website", "Has Phone", "Has Reviews", "Lead Score", "Quality",
        ])
    return sheet


def get_existing_urls(sheet):
    try:
        rows = sheet.get_all_values()[1:]
        return set(row[1] for row in rows if len(row) > 1)
    except Exception:
        return set()


def get_existing_business_keys(sheet):
    """(business_name, city) combos jo already sheet mein hain — taake
    same business FB + Instagram + LinkedIn teeno se dobara add na ho."""
    try:
        rows = sheet.get_all_values()[1:]
        keys = set()
        for row in rows:
            if len(row) > 4:
                name = row[2].strip().lower()
                city = row[4].strip().lower()
                if name:
                    keys.add((name, city))
        return keys
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
        "Yes" if lead.get("has_phone") else "No",
        "Yes" if lead.get("has_reviews") else "No",
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
    existing_business_keys = get_existing_business_keys(sheet)
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

            business_key = (business_name.strip().lower(), city.strip().lower())
            if not business_name or business_key in existing_business_keys:
                continue  # yeh business kisi aur platform se pehle hi add ho chuki hai

            website_exists = has_own_website(business_name, city, country)
            time.sleep(1)

            if website_exists:
                # pehle se website hai -> hamari lead nahi
                continue

            email = find_email(lead)
            time.sleep(1)

            if email == "Nahi mila":
                # Email nahi mila (ya domain-email nikla jo free-provider
                # nahi tha) -> yeh lead skip, sheet mein add nahi hogi
                continue

            combined_text = lead.get("body", "") + " " + lead.get("title", "")
            phone_found = has_phone_number(combined_text)
            reviews_found = has_ratings_or_reviews(combined_text)

            score = calculate_lead_score(website_exists, email, phone_found, reviews_found)
            quality = get_quality_label(score)

            lead.update({
                "business_name": business_name,
                "email": email,
                "has_website": website_exists,
                "has_phone": phone_found,
                "has_reviews": reviews_found,
                "score": score,
                "quality": quality,
            })

            save_lead_to_sheet(sheet, lead)
            existing_urls.add(lead["profile_url"])
            existing_business_keys.add(business_key)
            new_leads_count += 1
            if score >= 70:
                hot_leads_count += 1

            print(f"   {quality} (Score: {score}) — {business_name} ({platform}) | Email: {email}")

    print(f"\n===== DONE =====")
    print(f"Total {new_leads_count} nayi leads add hui, jisme se {hot_leads_count} 🔥 Hot Leads hain!\n")


if __name__ == "__main__":
    run_once()
