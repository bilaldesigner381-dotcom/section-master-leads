import re
import time
import random
import requests
import gspread
from google.oauth2.credentials import Credentials

# =====================================================================
# WHY THIS FILE EXISTS
# =====================================================================
# DDGS (DuckDuckGo scraping) rate-limits/blocks after a few hundred
# queries — it can never realistically get you to "millions of leads".
#
# OpenStreetMap's Overpass API is a real, free, structured database of
# tens of millions of businesses worldwide. Businesses are tagged with
# things like name, phone, email, and website directly by mappers/owners.
# We can query it directly for: "has an email tag" + "has NO website tag"
# — exactly the leads you want, with zero scraping and zero rate-limit
# drama (Overpass has fair-use limits, but they're generous and this
# script respects them with delays/retries).
# =====================================================================

SHEET_NAME = "WebDev Leads"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "WebDevLeadFinder/1.0 (contact: your-email@example.com)"  # Nominatim policy ke liye required

# Niche -> OSM tag mapping. Har niche ek (key, value) OSM tag hai.
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

# Seed cities — expand this list freely, koi hard limit nahi hai (yeh
# sirf starting sample hai, jitni cities add karenge utni zyada leads)
CITIES = [
    ("Houston", "USA"), ("Dallas", "USA"), ("Miami", "USA"), ("Orlando", "USA"),
    ("Atlanta", "USA"), ("Phoenix", "USA"), ("Chicago", "USA"), ("Los Angeles", "USA"),
    ("Austin", "USA"), ("Denver", "USA"), ("Seattle", "USA"), ("Boston", "USA"),
    ("London", "UK"), ("Manchester", "UK"), ("Birmingham", "UK"),
    ("Toronto", "Canada"), ("Vancouver", "Canada"),
    ("Sydney", "Australia"), ("Melbourne", "Australia"),
    ("Dubai", "UAE"), ("Abu Dhabi", "UAE"),
    ("Karachi", "Pakistan"), ("Lahore", "Pakistan"), ("Islamabad", "Pakistan"),
    ("Riyadh", "Saudi Arabia"), ("Jeddah", "Saudi Arabia"),
    ("Berlin", "Germany"), ("Amsterdam", "Netherlands"),
]

SEARCH_RADIUS_METERS = 15000   # ~15km radius around city center
COMBOS_PER_RUN = 15            # kitne (niche, city) combos ek run mein try karne hain

IGNORE_EMAIL_KEYWORDS = [
    "example.com", "sentry", "wixpress", "godaddy", "yourdomain",
    "domain.com", "email.com", "test.com", "noreply", "no-reply",
]


# =====================================================================
# STEP 1: CITY KO LAT/LON MEIN CONVERT KARNA (Nominatim geocoding)
# =====================================================================

_geocode_cache = {}


def geocode_city(city, country):
    cache_key = (city, country)
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    params = {"q": f"{city}, {country}", "format": "json", "limit": 1}
    headers = {"User-Agent": USER_AGENT}
    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        results = resp.json()
        if results:
            lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
            _geocode_cache[cache_key] = (lat, lon)
            time.sleep(1)  # Nominatim usage policy: max 1 request/sec
            return lat, lon
    except Exception as e:
        print(f"   ⚠️  Geocoding fail ho gayi {city}, {country}: {e}")

    time.sleep(1)
    return None


# =====================================================================
# STEP 2: OVERPASS QUERY BANANA AUR CHALANA
# =====================================================================

def build_overpass_query(lat, lon, radius, tag_key, tag_value):
    return f"""
    [out:json][timeout:60];
    (
      node["{tag_key}"="{tag_value}"]["email"]["!website"](around:{radius},{lat},{lon});
      way["{tag_key}"="{tag_value}"]["email"]["!website"](around:{radius},{lat},{lon});
    );
    out tags center;
    """


def run_overpass_query(query, retries=2):
    for attempt in range(retries + 1):
        try:
            resp = requests.post(OVERPASS_URL, data={"data": query},
                                  headers={"User-Agent": USER_AGENT}, timeout=70)
            if resp.status_code == 200:
                return resp.json().get("elements", [])
            elif resp.status_code in (429, 504):
                print(f"   ⚠️  Overpass busy (status {resp.status_code}), thodi der ruk kar retry karte hain...")
                time.sleep(10 * (attempt + 1))
            else:
                print(f"   ⚠️  Overpass error: status {resp.status_code}")
                return []
        except requests.exceptions.RequestException as e:
            print(f"   ⚠️  Overpass request fail: {e}")
            time.sleep(5)
    return []


# =====================================================================
# STEP 3: RESULT SE LEAD BANANA
# =====================================================================

def clean_email(raw_email):
    email = raw_email.strip().lower()
    # OSM mein kabhi kabhi "mailto:" prefix ya multiple emails ";" se separate hote hain
    email = email.replace("mailto:", "").split(";")[0].split(",")[0].strip()
    if not re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
        return None
    if any(bad in email for bad in IGNORE_EMAIL_KEYWORDS):
        return None
    return email


def element_to_lead(element, niche, city, country):
    tags = element.get("tags", {})
    name = tags.get("name", "").strip()
    if not name:
        return None

    raw_email = tags.get("email") or tags.get("contact:email")
    email = clean_email(raw_email) if raw_email else None
    if not email:
        return None  # email ke bina lead nahi chahiye

    phone = tags.get("phone") or tags.get("contact:phone") or ""
    address = tags.get("addr:street", "")

    osm_type = element.get("type", "node")
    osm_id = element.get("id")
    osm_url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"

    return {
        "source": "OpenStreetMap",
        "profile_url": osm_url,
        "business_name": name,
        "niche": niche,
        "city": city,
        "country": country,
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
# STEP 4: GOOGLE SHEETS (same sheet jo lead_finder_webdev.py use karta hai)
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
        return set((row[2].strip().lower(), row[4].strip().lower()) for row in rows if len(row) > 4 and row[2])
    except Exception:
        return set()


def save_lead_to_sheet(sheet, lead, score, quality):
    sheet.append_row([
        lead["source"],
        lead["profile_url"],
        lead["business_name"],
        lead["niche"],
        lead["city"],
        "",  # Region column — OSM path city-level hi hai, region blank chhod diya
        lead["country"],
        lead["email"],
        "No",  # yeh query hi guarantee karti hai ke website nahi hai
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

    combos = [(niche, city, country) for niche in NICHE_TAGS for city, country in CITIES]
    random.shuffle(combos)
    combos = combos[:COMBOS_PER_RUN]

    new_leads_count = 0
    hot_leads_count = 0

    for niche, city, country in combos:
        tag_key, tag_value = NICHE_TAGS[niche]

        coords = geocode_city(city, country)
        if not coords:
            continue
        lat, lon = coords

        query = build_overpass_query(lat, lon, SEARCH_RADIUS_METERS, tag_key, tag_value)
        print(f"🔍 Overpass query: {niche} near {city}, {country}")
        elements = run_overpass_query(query)
        print(f"   {len(elements)} raw results mile.\n")
        time.sleep(2)  # Overpass fair-use: queries ke darmiyan thoda gap

        for element in elements:
            lead = element_to_lead(element, niche, city, country)
            if not lead:
                continue

            key = (lead["business_name"].strip().lower(), city.strip().lower())
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
