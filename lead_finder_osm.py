import re
import time
import random
import requests
import gspread
from google.oauth2.credentials import Credentials

# =====================================================================
# WHY NO GEOCODING ANYMORE
# =====================================================================
# Pehle wala version Nominatim (city -> lat/lon) use kar raha tha, lekin
# Nominatim GitHub Actions jaise cloud/datacenter IPs ko 403 Forbidden
# de kar block kar deta hai — yeh hamesha fail hoga chahe kuch bhi karein.
#
# Fix: geocoding step hi hata do. Overpass API seedha COUNTRY CODE se
# query kar sakta hai (area["ISO3166-1"="US"]) — city dhoondne ki
# zaroorat hi nahi. Isse ek hi query se poore mulk ka data mil jata hai,
# aur koi IP-block wala masla nahi.
# =====================================================================

SHEET_NAME = "WebDev Leads"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "WebDevLeadFinder/1.0"

# Niche -> OSM tag mapping
NICHE_TAGS = {
    "restaurant": ("amenity", "restaurant"),
    "cafe": ("amenity", "cafe"),
    "bakery": ("shop", "bakery"),
    "bar": ("amenity", "bar"),
    "hairdresser": ("shop", "hairdresser"),
    "beauty salon": ("shop", "beauty"),
    "gym": ("leisure", "fitness_centre"),
    "yoga studio": ("leisure", "yoga"),
    "dentist": ("amenity", "dentist"),
    "doctor clinic": ("amenity", "doctors"),
    "veterinary": ("amenity", "veterinary"),
    "law firm": ("office", "lawyer"),
    "accountant": ("office", "accountant"),
    "real estate agent": ("office", "estate_agent"),
    "car repair": ("shop", "car_repair"),
    "florist": ("shop", "florist"),
    "pet grooming": ("shop", "pet_grooming"),
    "photography studio": ("shop", "photo"),
    "driving school": ("amenity", "driving_school"),
}

# ISO 3166-1 alpha-2 country codes -> display name.
# Yeh list jitni barhaayenge utni zyada coverage milegi (koi limit nahi).
COUNTRIES = {
    "US": "USA", "GB": "UK", "CA": "Canada", "AU": "Australia",
    "AE": "UAE", "PK": "Pakistan", "SA": "Saudi Arabia",
    "DE": "Germany", "NL": "Netherlands", "IN": "India",
    "ZA": "South Africa", "IE": "Ireland", "NZ": "New Zealand",
}

COMBOS_PER_RUN = 6         # ek run mein kitne (niche, country) combos try karne hain
                           # country-wide query bhaari hoti hai isliye yeh number chhota rakha hai

IGNORE_EMAIL_KEYWORDS = [
    "example.com", "sentry", "wixpress", "godaddy", "yourdomain",
    "domain.com", "email.com", "test.com", "noreply", "no-reply",
]


# =====================================================================
# STEP 1: OVERPASS QUERY BANANA AUR CHALANA (poore country ke liye)
# =====================================================================

def build_overpass_query(country_code, tag_key, tag_value):
    return f"""
    [out:json][timeout:90];
    area["ISO3166-1"="{country_code}"][admin_level=2]->.searchArea;
    (
      node["{tag_key}"="{tag_value}"][~"^(email|contact:email)$"~"."]["!website"]["!contact:website"](area.searchArea);
      way["{tag_key}"="{tag_value}"][~"^(email|contact:email)$"~"."]["!website"]["!contact:website"](area.searchArea);
    );
    out tags;
    """


def run_overpass_query(query, retries=2):
    for attempt in range(retries + 1):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query},
                                  headers={"User-Agent": USER_AGENT}, timeout=100)
            if resp.status_code == 200:
                return resp.json().get("elements", [])
            elif resp.status_code in (429, 504):
                print(f"   ⚠️  Overpass busy (status {resp.status_code}), thodi der ruk kar retry karte hain...")
                time.sleep(15 * (attempt + 1))
            else:
                print(f"   ⚠️  Overpass error: status {resp.status_code}")
                return []
        except requests.exceptions.RequestException as e:
            print(f"   ⚠️  Overpass request fail: {e}")
            time.sleep(5)
    return []


# =====================================================================
# STEP 2: RESULT SE LEAD BANANA
# =====================================================================

def clean_email(raw_email):
    email = raw_email.strip().lower()
    email = email.replace("mailto:", "").split(";")[0].split(",")[0].strip()
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
        return None
    if any(bad in email for bad in IGNORE_EMAIL_KEYWORDS):
        return None
    return email


def element_to_lead(element, niche, country_name):
    tags = element.get("tags", {})
    name = tags.get("name", "").strip()
    if not name:
        return None

    raw_email = tags.get("email") or tags.get("contact:email")
    email = clean_email(raw_email) if raw_email else None
    if not email:
        return None

    phone = tags.get("phone") or tags.get("contact:phone") or ""
    address = tags.get("addr:street", "")
    city = tags.get("addr:city", "").strip()

    osm_type = element.get("type", "node")
    osm_id = element.get("id")
    osm_url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"

    return {
        "source": "OpenStreetMap",
        "profile_url": osm_url,
        "business_name": name,
        "niche": niche,
        "city": city,
        "country": country_name,
        "email": email,
        "phone": phone,
        "address": address,
    }


def calculate_score(lead):
    score = 40  # base: confirmed no website + confirmed email (query guarantees both)
    if lead["phone"]:
        score += 30
    if lead["address"]:
        score += 15
    score += 15
    return min(score, 100)


def get_quality_label(score):
    if score >= 70:
        return "🔥 Hot Lead"
    elif score >= 40:
        return "⭐ Warm Lead"
    return "🌱 Cold Lead"


# =====================================================================
# STEP 3: GOOGLE SHEETS (same sheet jo lead_finder_webdev.py use karta hai)
# =====================================================================

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


def get_existing_business_keys(sheet):
    try:
        rows = sheet.get_all_values()[1:]
        return set((row[2].strip().lower(), row[6].strip().lower()) for row in rows if len(row) > 6 and row[2])
    except Exception:
        return set()


def save_lead_to_sheet(sheet, lead, score, quality):
    sheet.append_row([
        lead["source"],
        lead["profile_url"],
        lead["business_name"],
        lead["niche"],
        lead["city"],
        "",
        lead["country"],
        lead["email"],
        "No",
        "Yes" if lead["phone"] else "No",
        "No",
        score,
        quality,
    ])


# =====================================================================
# MAIN
# =====================================================================

def run_once():
    print("📊 Google Sheet se connect ho rahe hain...")
    sheet = get_or_create_sheet()
    existing_keys = get_existing_business_keys(sheet)
    print(f"✅ Connected! Sheet mein pehle se {len(existing_keys)} businesses hain.\n")

    combos = [(niche, code, name) for niche in NICHE_TAGS for code, name in COUNTRIES.items()]
    random.shuffle(combos)
    combos = combos[:COMBOS_PER_RUN]

    new_leads_count = 0
    hot_leads_count = 0

    for niche, country_code, country_name in combos:
        tag_key, tag_value = NICHE_TAGS[niche]

        query = build_overpass_query(country_code, tag_key, tag_value)
        print(f"🔍 Overpass query: {niche} in {country_name}")
        elements = run_overpass_query(query)
        print(f"   {len(elements)} raw results mile.\n")
        time.sleep(3)  # Overpass fair-use: queries ke darmiyan gap

        for element in elements:
            lead = element_to_lead(element, niche, country_name)
            if not lead:
                continue

            key = (lead["business_name"].strip().lower(), lead["country"].strip().lower())
            if key in existing_keys:
                continue

            score = calculate_score(lead)
            quality = get_quality_label(score)

            save_lead_to_sheet(sheet, lead, score, quality)
            existing_keys.add(key)
            new_leads_count += 1
            if score >= 70:
                hot_leads_count += 1

            print(f"   {quality} (Score: {score}) — {lead['business_name']} | Email: {lead['email']}")

    print(f"\n===== DONE =====")
    print(f"Total {new_leads_count} nayi leads add hui, jisme se {hot_leads_count} 🔥 Hot Leads hain!\n")


if __name__ == "__main__":
    run_once()
