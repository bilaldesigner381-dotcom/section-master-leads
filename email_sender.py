"""
SECTION MASTER — EMAIL SENDER v2 (aapke purane setup ke mutabiq)
================================================================
* Gmail accounts: wahi GMAIL_1_ADDRESS/GMAIL_1_APPPASS ... (1..20 tak) env vars
* Google login: token.json (TOKEN_JSON secret) ya GOOGLE_SERVICE_ACCOUNT_JSON — jo mile
* Leads: NAYI lead-finder sheet (GOOGLE_SHEET_ID) ka "Leads" tab (sirf padhta hai)
* State: usi sheet ka "Outreach" tab (Status, Stage, Sender, Next Due ...)
* Har account har run 2 emails (PER_SENDER_PER_RUN) — follow-ups pehle, phir naye leads (HOT > WARM > GOOD)
* Har store: 1 email + 2 follow-ups (3 din baad, phir 4 din baad), same sender, same thread
* PURANI sheet ("Section Master Leads") me jinhe "Yes (...)" mark hai, unhe dobara pehli email
  NAHI jati — wo Outreach me stage 1 par import hote hain aur unhe sirf follow-ups milte hain
* Reply / bounce / "unsubscribe" IMAP se detect -> us store ko dobara email nahi

SEND_MODE=dry (default) : sirf log me dikhata hai, email nahi bhejta
SEND_MODE=live          : asli emails
"""

import os
import re
import ssl
import sys
import json
import time
import random
import hashlib
import smtplib
import imaplib
import logging
from datetime import datetime, timedelta, timezone
from email import message_from_bytes
from email.message import EmailMessage
from email.utils import make_msgid, formatdate, parseaddr, formataddr

import gspread

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("outreach")

# ============================================================
# CONFIG
# ============================================================
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
OLD_SHEET_NAME = os.environ.get("OLD_SHEET_NAME", "Section Master Leads").strip()

LEADS_TAB = os.environ.get("GOOGLE_SHEET_NAME", "Leads")
OUTREACH_TAB = "Outreach"
UNSUB_TAB = "Unsubscribed"

FROM_NAME = os.environ.get("FROM_NAME", "Bilal Ahmed")
APP_NAME = "Section Master"
APP_URL = os.environ.get("APP_URL", "https://apps.shopify.com/section-master")
PHYSICAL_ADDRESS = os.environ.get("PHYSICAL_ADDRESS", "").strip()

PER_SENDER_PER_RUN = int(os.environ.get("PER_SENDER_PER_RUN", "2"))
QUOTA_WINDOW_MINUTES = int(os.environ.get("QUOTA_WINDOW_MINUTES", "45"))
FU1_DAYS = float(os.environ.get("FOLLOWUP1_DAYS", "3"))
FU2_DAYS = float(os.environ.get("FOLLOWUP2_DAYS", "4"))
MIGRATED_FU_DELAY_DAYS = float(os.environ.get("MIGRATED_FU_DELAY_DAYS", "2"))
MIN_QUALITY = os.environ.get("MIN_QUALITY", "GOOD").upper()
SEND_MODE_RAW = os.environ.get("SEND_MODE", "")
DRY_RUN = SEND_MODE_RAW.strip().lower() not in ("live", "1", "true", "yes", "on")
SEND_GAP = (int(os.environ.get("SEND_GAP_MIN", "10")), int(os.environ.get("SEND_GAP_MAX", "40")))
IMAP_LOOKBACK_DAYS = int(os.environ.get("IMAP_LOOKBACK_DAYS", "30"))
DAILY_MAX_PER_SENDER = int(os.environ.get("DAILY_MAX_PER_SENDER", "30"))   # 24 ghante me har account ki hard limit
BOUNCE_PAUSE_RATIO = float(os.environ.get("BOUNCE_PAUSE_RATIO", "0.08"))     # bounce rate is se zyada => sending band
BOUNCE_MIN_SAMPLE = int(os.environ.get("BOUNCE_MIN_SAMPLE", "10"))

QUALITY_RANK = {"LOW": 0, "GOOD": 1, "WARM": 2, "HOT": 3}

# Purani (non Online Store 2.0) themes — inme app sections nahi chalte, isliye skip
LEGACY_THEMES = {
    "debut", "brooklyn", "minimal", "supply", "venture", "simple", "narrative",
    "boundless", "express", "pop", "jumpstart", "fashionopolism", "vintage",
}

OUT_HEADERS = [
    "Email", "Store Name", "Domain", "Quality", "Lead Score",   # A-E
    "Status", "Stage", "Sender", "Message-ID", "Subject",       # F-J
    "Last Sent", "Next Due", "Notes",                           # K-M
]

FREE_PROVIDERS = {"gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com",
                  "live.com", "icloud.com", "aol.com", "proton.me", "protonmail.com"}
FREE_PROVIDERS |= {
    "qq.com", "163.com", "126.com", "sina.com", "sohu.com", "yeah.net", "foxmail.com", "gmx.com", "gmx.de",
    "gmx.net", "web.de", "mail.com", "mail.ru", "yandex.com", "yandex.ru", "rediffmail.com", "ymail.com",
    "rocketmail.com", "btinternet.com", "comcast.net", "verizon.net", "att.net", "sbcglobal.net",
    "bellsouth.net", "cox.net", "shaw.ca", "rogers.com", "bigpond.com", "free.fr", "orange.fr",
    "wanadoo.fr", "t-online.de", "libero.it", "virgilio.it", "naver.com", "daum.net",
}


def is_free(dom):
    return dom in FREE_PROVIDERS or bool(re.match(r"^(yahoo|hotmail|outlook|live|gmx|msn)\.", dom))


# --- email quality filter (junk / placeholder / file-name / glued-text addresses) ---
GOOD_TLDS = {
    "com", "net", "org", "info", "biz", "edu", "gov", "shop", "store", "online", "xyz", "app", "dev", "site",
    "club", "live", "tech", "design", "art", "studio", "boutique", "fashion", "beauty", "jewelry", "jewellery",
    "gifts", "gift", "life", "world", "today", "news", "email", "cafe", "coffee", "tea", "wine", "beer", "fit",
    "fitness", "yoga", "health", "care", "shopping", "market", "clothing", "wear", "style", "co", "pet", "pets",
    "home", "house", "garden", "florist", "flowers", "photo", "photography", "agency", "company", "group",
    "services", "solutions", "media", "digital", "cloud", "website", "space", "store", "one", "bio", "eco",
}
BAD_TLDS_2 = {"js", "py", "md", "ts"}
PLACEHOLDER_LOCALS = {
    "recipient", "destinataire", "nom_utilisateur", "brugernavn", "benutzername", "usuario", "yourname",
    "name", "email", "user", "username", "example", "test", "noreply", "no-reply", "donotreply",
    "do-not-reply", "mailer-daemon", "postmaster", "customer", "yourmail", "youremail",
}
PLACEHOLDER_DOMAINS = {
    "example", "exemple", "eksempel", "beispiel", "ejemplo", "yourstore", "yourdomain", "yoursite", "domain",
    "email", "mysite", "mydomain", "test", "sentry", "wixpress", "shopify", "myshopify", "yourcompany",
}
ROLE_LOCALS = {"privacy", "dpo", "legal", "abuse", "security", "compliance", "gdpr", "careers", "jobs", "hr"}
STRICT_EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._+-]{0,63}@(?:[a-z0-9-]+\.)+[a-z]{2,24}$")


def email_problem(email):
    """Khali string = theek hai, warna wajah (junk/placeholder/glued text/role mailbox)."""
    e = (email or "").strip().lower()
    if not STRICT_EMAIL_RE.match(e):
        return "invalid format"
    local, dom = e.split("@")
    if re.match(r"^u00[0-9a-f]{2}", local):
        return "encoded junk"
    tld = dom.rsplit(".", 1)[-1]
    if tld in BAD_TLDS_2 or (len(tld) > 2 and tld not in GOOD_TLDS):
        return f"suspicious TLD .{tld}"
    if local in PLACEHOLDER_LOCALS:
        return "placeholder name"
    labels = dom.split(".")
    if labels[0] in PLACEHOLDER_DOMAINS or (len(labels) > 1 and labels[-2] in PLACEHOLDER_DOMAINS):
        return "placeholder domain"
    if local in ROLE_LOCALS:
        return "role mailbox"
    return ""


EMAIL_RE = re.compile(r"[a-z0-9._%+-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", re.I)
STOP_RE = re.compile(r"\b(stop|unsubscribe|remove me|remove us|not interested|don'?t email|do not email|opt out)\b", re.I)


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ============================================================
# GOOGLE SHEETS
# ============================================================
def get_client():
    if SA_JSON:
        from google.oauth2.service_account import Credentials as SACreds
        return gspread.authorize(SACreds.from_service_account_info(json.loads(SA_JSON), scopes=SCOPES))
    from google.oauth2.credentials import Credentials as UserCreds
    return gspread.authorize(UserCreds.from_authorized_user_file("token.json", SCOPES))


def connect():
    if not SHEET_ID:
        sys.exit("GOOGLE_SHEET_ID secret set karein (nayi lead-finder sheet ka ID/URL).")
    gc = get_client()
    sh = gc.open_by_key(re.sub(r".*/d/([\w-]+).*", r"\1", SHEET_ID))
    log.info("CONNECTED SHEET: '%s' -> %s", sh.title, sh.url)

    def tab(title, headers):
        try:
            return sh.worksheet(title)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=title, rows=1000, cols=len(headers))
            ws.update(values=[headers], range_name="A1")
            ws.freeze(rows=1)
            return ws

    return (gc, sh.worksheet(LEADS_TAB), tab(OUTREACH_TAB, OUT_HEADERS), tab(UNSUB_TAB, ["Email or Domain"]),
            tab("Control", ["Setting", "Value"]))


def sheet_rows(ws):
    values = ws.get_all_values()
    if not values:
        return []
    head = values[0]
    out = []
    for i, row in enumerate(values[1:], start=2):
        row = row + [""] * (len(head) - len(row))
        d = {h: row[j].strip() for j, h in enumerate(head) if h}
        d["_row"] = i
        out.append(d)
    return out


def append_in_chunks(ws, rows):
    for i in range(0, len(rows), 1000):
        ws.append_rows(rows[i:i + 1000], value_input_option="RAW")


def update_row(out_ws, r):
    """True agar sheet me save ho gaya, warna False."""
    vals = [r["Status"], r["Stage"], r["Sender"], r["Message-ID"], r["Subject"],
            r["Last Sent"], r["Next Due"], r["Notes"]]
    for attempt in range(4):
        try:
            out_ws.update(values=[vals], range_name=f"F{r['_row']}:M{r['_row']}", value_input_option="RAW")
            return True
        except Exception as e:
            log.warning("Row update retry %s: %s", attempt + 1, e)
            time.sleep(5 * (attempt + 1))
    log.error("Row %s update fail hui (%s).", r["_row"], r["Email"])
    return False


def batch_update_rows(out_ws, rows):
    """Bahut si rows ek hi API call me (Sheets ki '60 writes/min' quota se bachne ke liye)."""
    payload = [{"range": f"F{r['_row']}:M{r['_row']}",
                "values": [[r["Status"], r["Stage"], r["Sender"], r["Message-ID"], r["Subject"],
                            r["Last Sent"], r["Next Due"], r["Notes"]]]} for r in rows]
    for i in range(0, len(payload), 300):
        chunk = payload[i:i + 300]
        for attempt in range(5):
            try:
                out_ws.batch_update(chunk, value_input_option="RAW")
                break
            except Exception as e:
                log.warning("Batch update retry %s: %s", attempt + 1, e)
                time.sleep(10 * (attempt + 1))
        else:
            log.error("Batch update fail (%s rows) — sending ab bhi safe hai, agli run me dobara try hoga.", len(chunk))


def is_unsub(email, unsub):
    dom = email.split("@")[-1]
    return email in unsub or (dom in unsub and not is_free(dom))


# ============================================================
# PURANI SHEET SE MIGRATION (already emailed -> follow-ups only)
# ============================================================
def store_name_from_url(url):
    name = re.sub(r"https?://(www\.)?", "", url).split("/")[0].split(".")[0]
    return name.replace("-", " ").title()


def migrate_old_sheet(gc, out_ws, out_rows, senders, unsub):
    if not OLD_SHEET_NAME or OLD_SHEET_NAME.lower() == "none":
        return
    if any(r["Notes"].startswith("migrated") for r in out_rows):
        return  # pehle ho chuka
    try:
        old = gc.open(OLD_SHEET_NAME).sheet1.get_all_values()
    except Exception as e:
        log.warning("Purani sheet '%s' khul nahi saki (%s) — migration skip.", OLD_SHEET_NAME, e)
        return
    if not old:
        return
    head = old[0]
    try:
        i_email, i_sent, i_url = head.index("Email"), head.index("Emailed"), head.index("Store URL")
    except ValueError:
        log.warning("Purani sheet me Email/Emailed/Store URL columns nahi mile — migration skip.")
        return

    known = {r["Email"].lower() for r in out_rows}
    sender_emails = [s["email"] for s in senders]
    due = iso(utcnow() + timedelta(days=MIGRATED_FU_DELAY_DAYS))
    rows = []
    for row in old[1:]:
        row = row + [""] * (len(head) - len(row))
        email, sent, url = row[i_email].strip().lower(), row[i_sent].strip(), row[i_url].strip()
        if not sent.lower().startswith("yes") or "@" not in email or email in known:
            continue
        known.add(email)
        m = re.search(r"\(([^)]+)\)", sent)
        sender = (m.group(1).lower() if m else "")
        if sender not in sender_emails:
            sender = sender_emails[0]
        store = store_name_from_url(url)
        domain = re.sub(r"https?://(www\.)?", "", url).split("/")[0].lower()
        status = "unsub" if is_unsub(email, unsub) else "active"
        rows.append([email, store, domain, "", "0", status, 1, sender, "",
                     f"A quick idea for {store}'s homepage", "", due if status == "active" else "",
                     "migrated from old sheet"])
    append_in_chunks(out_ws, rows)
    log.info("Purani sheet se %s already-emailed stores import hue (ab sirf follow-ups jayenge).", len(rows))


# ============================================================
# LEADS -> OUTREACH SYNC
# ============================================================
def sync_leads(leads_rows, out_ws, out_rows, unsub):
    known = {r["Email"].lower() for r in out_rows}
    new_rows = []
    for lead in leads_rows:
        email = lead.get("Email", "").lower()
        if not email or "@" not in email or email in known:
            continue
        known.add(email)
        quality = lead.get("Quality", "LOW").upper()
        status, note = "new", ""
        bad = email_problem(email)
        if bad:
            status, note = "skipped", f"bad email: {bad}"
        elif QUALITY_RANK.get(quality, 0) < QUALITY_RANK.get(MIN_QUALITY, 1):
            status, note = "skipped", f"quality {quality} < {MIN_QUALITY}"
        elif lead.get("Theme", "").strip().lower() in LEGACY_THEMES:
            status, note = "skipped", "legacy theme (no OS2.0 sections)"
        elif is_unsub(email, unsub):
            status, note = "unsub", "in Unsubscribed tab"
        new_rows.append([email, lead.get("Store Name", ""), lead.get("Domain", ""), quality,
                         lead.get("Lead Score", "0"), status, 0, "", "", "", "", "", note])
    append_in_chunks(out_ws, new_rows)
    if new_rows:
        log.info("Outreach tab me %s naye leads add hue.", len(new_rows))


# ============================================================
# PERSONALIZATION
# ============================================================
KNOWN_THEMES = {
    "dawn", "refresh", "craft", "sense", "taste", "studio", "ride", "origin", "crave", "publisher", "colorblock",
    "spotlight", "horizon", "trade", "impulse", "prestige", "empire", "turbo", "broadcast", "fabric", "vantage",
    "ritual", "symmetry", "warehouse", "focal", "expanse", "kagami", "motion", "pipeline", "enterprise", "canopy",
    "streamline", "palo alto", "mr parker", "be yours", "fetch", "local", "retina", "reformation", "combine",
    "blockshop", "stiletto", "wonder", "shine", "sitar", "minimog", "gain", "aurora", "ella",
}


def picks_for(lead):
    """(section, benefit, observation) — Leads sheet ke columns se store ke liye relevant sections."""
    g = lambda k: (lead.get(k, "") or "").strip().upper()
    recs = (lead.get("Recommended Sections", "") or "").lower()
    try:
        products = int(lead.get("Products", "0") or 0)
    except ValueError:
        products = 0
    picks = []
    if g("Testimonials") == "NO":
        picks.append(("Testimonials", "build trust with customer quotes right on the homepage", "a testimonials section"))
    if g("FAQ") == "NO":
        picks.append(("FAQ Accordion", "answer shipping and returns questions before they become support tickets", "an FAQ section"))
    if g("Newsletter") == "NO":
        picks.append(("Newsletter Signup", "capture emails from visitors who aren't ready to buy yet", "a newsletter signup"))
    if "countdown" in recs:
        picks.append(("Announcement Countdown", "add urgency to sales and launches", "a countdown/announcement bar"))
    if "instagram" in recs:
        picks.append(("Instagram Feed", "show real social proof and fresh content", "an Instagram feed"))
    if products >= 10:
        picks.append(("Featured Collection", "push your best sellers above the fold", ""))
    for f in [
        ("Hero Banner Slider", "rotate your best offers in the hero area", ""),
        ("Promo Banner", "highlight a current offer without touching code", ""),
        ("Featured Product Premium", "give a hero product a proper showcase", ""),
    ]:
        if len(picks) >= 3:
            break
        picks.append(f)
    return picks[:3]


def join_or(items):
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " or " + items[-1]


def join_and(items):
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def pick_variant(seed, options):
    return options[int(hashlib.md5(seed.encode()).hexdigest(), 16) % len(options)]


def build_message(lead, stage, base_subject=None):
    """stage 0 = pehli email, 1/2 = follow-ups. Return (subject, body)."""
    store = (lead.get("Store Name") or "").strip() or "your store"
    domain = lead.get("Domain", "")
    theme = (lead.get("Theme") or "").strip()
    picks = picks_for(lead)
    names = [p[0] for p in picks]
    missing = [p[2] for p in picks if p[2]][:2]
    first = picks[0]
    extras = [x for x in ("Hero Banner Slider", "Logo Carousel", "Contact Form", "Promo Banner") if x not in names][:3]

    subject0 = pick_variant(domain or store, [
        f"A quick idea for {store}'s homepage",
        f"{store}: homepage sections without a developer",
        f"Idea for {store}",
    ])

    if missing:
        observation = (f"I came across {store} ({domain}) on Shopify and couldn't spot "
                       f"{join_or(missing)} on the pages I checked.")
    else:
        observation = (f"I came across {store} ({domain}) on Shopify, and the catalog looks great — "
                       f"the homepage could be doing even more for it.")

    theme_line = ""
    if theme.lower() in KNOWN_THEMES:   # sirf asli/mashhoor themes ka naam, "Copy of Copy of backup..." jaisa nahi
        theme_line = f" It works right inside your {theme} theme's editor."

    if stage == 0:
        subject = subject0
        body = (
            f"Hi {store} team,\n\n"
            f"{observation}\n\n"
            f"We built {APP_NAME}, a free Shopify app that lets you add sections without touching any code — "
            f"{join_and(names)}, plus {join_and(extras)} and more.{theme_line}\n\n"
            f"You can check it out here:\n{APP_URL}\n\n"
            f"If you'd like a quick free demo of how {names[0]} could look on {store}, just reply to this email.\n\n"
            f"Best regards,\n{FROM_NAME}\n{APP_NAME} Team"
        )
    else:
        base = base_subject or subject0
        subject = "Re: " + re.sub(r"^(re:\s*)+", "", base, flags=re.I)
        if stage == 1:
            body = (
                f"Hi {store} team,\n\n"
                f"Just floating this back up in case it got buried. One example: {first[0]} lets you "
                f"{first[1]} — set up in a couple of minutes from the theme editor, and the app is free.\n\n"
                f"Happy to send a quick demo for {store} if
