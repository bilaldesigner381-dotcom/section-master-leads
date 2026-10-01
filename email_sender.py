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
MIN_QUALITY = os.environ.get("MIN_QUALITY", "LOW").upper()
SEND_MODE_RAW = os.environ.get("SEND_MODE", "")
DRY_RUN = SEND_MODE_RAW.strip().lower() not in ("live", "1", "true", "yes", "on")
SEND_GAP = (int(os.environ.get("SEND_GAP_MIN", "10")), int(os.environ.get("SEND_GAP_MAX", "40")))
IMAP_LOOKBACK_DAYS = int(os.environ.get("IMAP_LOOKBACK_DAYS", "30"))
DAILY_MAX_PER_SENDER = int(os.environ.get("DAILY_MAX_PER_SENDER", "48"))   # 24 ghante me har account ki hard limit
BOUNCE_PAUSE_RATIO = float(os.environ.get("BOUNCE_PAUSE_RATIO", "0.06"))     # bounce rate is se zyada => sending band
BOUNCE_MIN_SAMPLE = int(os.environ.get("BOUNCE_MIN_SAMPLE", "40"))           # itni mails ke baad hi rate par faisla
BOUNCE_MIN_COUNT = int(os.environ.get("BOUNCE_MIN_COUNT", "3"))              # aur kam az kam itne bounce

QUALITY_RANK = {"LOW": 0, "GOOD": 1, "WARM": 2, "HOT": 3}
HARD_MAX_PER_SENDER_PER_RUN = 5      # Control tab se bhi isse zyada nahi
HARD_MAX_DAILY_PER_SENDER = 60

# ---- multi-account (v4): providers, ramp-up, per-account health ----
PROVIDERS = {
    "gmail":   {"smtp_host": "smtp.gmail.com",     "smtp_port": 465, "imap_host": "imap.gmail.com"},
    "zoho":    {"smtp_host": "smtp.zoho.com",      "smtp_port": 465, "imap_host": "imap.zoho.com"},
    "outlook": {"smtp_host": "smtp.office365.com", "smtp_port": 587, "imap_host": "outlook.office365.com"},
}
SENDER_HEADERS = ["Email", "Provider", "Name", "Status", "Start Date", "Daily Limit Override", "Age (days)",
                  "Daily Limit Today", "Sent 24h", "Sent 7d", "Replies (all)", "Bounced 7d", "Bounce Rate 7d",
                  "Reply Rate", "Notes"]
DEFAULT_RAMP = "5,10,15,20,25,30"            # har hafte ki daily limit (naye account ke liye), aakhri value aage chalti hai
ACCOUNT_BOUNCE_RATIO = float(os.environ.get("ACCOUNT_BOUNCE_RATIO", "0.06"))
ACCOUNT_BOUNCE_MIN_SENT = int(os.environ.get("ACCOUNT_BOUNCE_MIN_SENT", "20"))
ACCOUNT_BOUNCE_MIN_COUNT = int(os.environ.get("ACCOUNT_BOUNCE_MIN_COUNT", "3"))
GLOBAL_MAX_PER_RUN = int(os.environ.get("GLOBAL_MAX_PER_RUN", "500"))
ESTABLISHED_AGE_DAYS = 28                    # purane (GMAIL_n) accounts ko "warmed" maana jata hai

# Purani (non Online Store 2.0) themes — inme app sections nahi chalte, isliye skip
LEGACY_THEMES = {
    "debut", "brooklyn", "minimal", "supply", "venture", "simple", "narrative",
    "boundless", "express", "pop", "jumpstart", "fashionopolism", "vintage",
}

OUT_HEADERS = [
    "Email", "Store Name", "Domain", "Quality", "Lead Score",   # A-E
    "Status", "Stage", "Sender", "Message-ID", "Subject",       # F-J
    "Last Sent", "Next Due", "Notes",                           # K-M
    # ---- tracking columns (v3) ----
    "Sent", "Emails Sent", "First Sent",                        # N-P
    "Replied", "Reply Count", "Replied At", "Reply Type", "Reply Snippet",   # Q-U
    "Bounced", "Clicks", "First Click", "Last Click", "Track ID",            # V-Z
]
ROW_COLS = OUT_HEADERS[5:]          # F..Z  (jo script update karti hai)
LAST_COL = "Z"
TRACK_SALT = os.environ.get("TRACK_SALT", "sm")


def to_int(v):
    try:
        return int(float(v))
    except Exception:
        return 0


def make_row(d):
    return [d.get(h, "") for h in OUT_HEADERS]


def row_vals(r):
    return [r.get(h, "") for h in ROW_COLS]


def sig(r):
    return tuple(str(v) for v in row_vals(r))


def make_tid(email):
    return hashlib.sha1((email + TRACK_SALT).encode()).hexdigest()[:10]


def derive(r):
    """Sheet ke friendly columns (Sent / Emails Sent / Bounced / Replied ...) Stage+Status se nikalta hai."""
    n = to_int(r.get("Stage"))
    r["Emails Sent"] = n
    if n > 0:
        r["Sent"] = f"Sent {min(n, 3)}/3"
    elif r.get("Status") == "skipped":
        r["Sent"] = "Skipped"
    elif r.get("Status") == "unsub":
        r["Sent"] = "Unsubscribed"
    else:
        r["Sent"] = "Not sent"
    r["Bounced"] = "Yes" if r.get("Status") == "bounced" else "No"
    r["Replied"] = "Yes" if (r.get("Replied") == "Yes" or r.get("Status") == "replied") else "No"
    r["Reply Count"] = to_int(r.get("Reply Count"))
    if r["Replied"] == "Yes" and r["Reply Count"] == 0:
        r["Reply Count"] = 1
    r["Clicks"] = to_int(r.get("Clicks"))

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


REAL_COM_TLDS = {"community", "computer", "commbank", "comcast", "compare", "comsec"}


def repair_email(e):
    """Glued/ganda addresses theek karna: '%20info@x.com', 'u003ehello@x.com', 'info@x.comif' -> 'info@x.com'."""
    e = (e or "").strip().lower()
    e = re.sub(r"^(%20|u003e|u003c)+", "", e)
    if e.count("@") == 1:
        local, dom = e.split("@")
        parts = dom.split(".")
        tld = parts[-1]
        if len(tld) > 3 and tld not in GOOD_TLDS and tld not in REAL_COM_TLDS and tld.startswith("com"):
            parts[-1] = "com"
        e = local + "@" + ".".join(parts)
    return e


def candidate_emails(lead):
    c = [lead.get("Email", "")] + [x.strip() for x in (lead.get("Other Emails", "") or "").split(",")]
    return [x.lower() for x in c if x and "@" in x]


def pick_email(lead, known, unsub, first=None):
    """Lead ki sab emails (primary + Other Emails, repaired) me se pehli jo theek ho aur pehle se list me na ho."""
    cands = []
    for e in ([first.lower()] if first else []) + candidate_emails(lead):
        for v in (e, repair_email(e)):
            if v not in cands:
                cands.append(v)
    for e in cands:
        if e in known or email_problem(e) or is_unsub(e, unsub):
            continue
        return e
    return None


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


def ensure_headers(ws, headers):
    cur = ws.row_values(1)
    if cur[:len(headers)] != headers:
        if ws.col_count < len(headers):
            ws.add_cols(len(headers) - ws.col_count)
        ws.update(values=[headers], range_name="A1")
        log.info("Tab '%s' ke naye tracking columns add ho gaye.", ws.title)


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
            ws = sh.add_worksheet(title=title, rows=1000, cols=max(len(headers), 10))
            ws.update(values=[headers], range_name="A1")
            ws.freeze(rows=1)
            return ws

    out = tab(OUTREACH_TAB, OUT_HEADERS)
    ensure_headers(out, OUT_HEADERS)
    try:
        clicks = sh.worksheet("Clicks")
    except gspread.exceptions.WorksheetNotFound:
        clicks = None
    return {
        "gc": gc, "leads": sh.worksheet(LEADS_TAB), "out": out,
        "unsub": tab(UNSUB_TAB, ["Email or Domain"]), "control": tab("Control", ["Setting", "Value"]),
        "replies": tab("Replies", ["Store Name", "Email", "Domain", "Quality", "Replied At", "Reply Snippet",
                                   "Emails Sent", "Sender", "Status"]),
        "dashboard": tab("Dashboard", ["Metric", "Value"]),
        "senders": tab("Senders", SENDER_HEADERS),
        "clicks": clicks,
    }


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
    for attempt in range(4):
        try:
            out_ws.update(values=[row_vals(r)], range_name=f"F{r['_row']}:{LAST_COL}{r['_row']}",
                          value_input_option="RAW")
            return True
        except Exception as e:
            log.warning("Row update retry %s: %s", attempt + 1, e)
            time.sleep(5 * (attempt + 1))
    log.error("Row %s update fail hui (%s).", r["_row"], r["Email"])
    return False


def batch_update_rows(out_ws, rows):
    """Bahut si rows ek hi API call me (Sheets ki '60 writes/min' quota se bachne ke liye)."""
    payload = [{"range": f"F{r['_row']}:{LAST_COL}{r['_row']}", "values": [row_vals(r)]} for r in rows]
    for i in range(0, len(payload), 200):
        chunk = payload[i:i + 200]
        for attempt in range(5):
            try:
                out_ws.batch_update(chunk, value_input_option="RAW")
                break
            except Exception as e:
                log.warning("Batch update retry %s: %s", attempt + 1, e)
                time.sleep(10 * (attempt + 1))
        else:
            log.error("Batch update fail (%s rows) — agli run me dobara try hoga.", len(chunk))


def block_write_all(out_ws, rows):
    """Bohot saari rows badli hon (jaise pehli baar naye columns bharna) to F:Z poora block chunks me likho."""
    rows = sorted(rows, key=lambda r: r["_row"])
    for i in range(0, len(rows), 3000):
        chunk = rows[i:i + 3000]
        start, end = chunk[0]["_row"], chunk[-1]["_row"]
        for attempt in range(5):
            try:
                out_ws.update(values=[row_vals(r) for r in chunk], range_name=f"F{start}:{LAST_COL}{end}",
                              value_input_option="RAW")
                break
            except Exception as e:
                log.warning("Block write retry %s: %s", attempt + 1, e)
                time.sleep(10 * (attempt + 1))
        else:
            log.error("Block write fail (rows %s-%s).", start, end)


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
        rows.append(make_row({
            "Email": email, "Store Name": store, "Domain": domain, "Lead Score": "0", "Status": status,
            "Stage": 1, "Sender": sender, "Subject": f"A quick idea for {store}'s homepage",
            "Next Due": due if status == "active" else "", "Notes": "migrated from old sheet",
        }))
    append_in_chunks(out_ws, rows)
    log.info("Purani sheet se %s already-emailed stores import hue (ab sirf follow-ups jayenge).", len(rows))


# ============================================================
# LEADS -> OUTREACH SYNC
# ============================================================
def sync_leads(leads_rows, out_ws, out_rows, unsub, min_quality="LOW", include_legacy=False):
    known = {r["Email"].lower() for r in out_rows}
    alt_domains = {r["Domain"].lower() for r in out_rows if r.get("Notes", "").startswith("alt email")}
    new_rows = []
    for lead in leads_rows:
        primary = lead.get("Email", "").lower()
        if not primary or "@" not in primary or primary in known or lead.get("Domain", "").lower() in alt_domains:
            continue
        quality = lead.get("Quality", "LOW").upper()
        chosen = pick_email(lead, known, unsub)
        email = chosen or primary
        known.add(email)
        status, note = "new", ""
        if chosen and chosen != primary:
            note = f"alt email used (primary was: {primary})"
        if not chosen:
            bad = email_problem(primary)
            if bad:
                status, note = "skipped", f"bad email: {bad}"
            elif is_unsub(primary, unsub):
                status, note = "unsub", "in Unsubscribed tab"
        if status == "new" and QUALITY_RANK.get(quality, 0) < QUALITY_RANK.get(min_quality, 0):
            status, note = "skipped", f"quality {quality} < {min_quality}"
        elif status == "new" and not include_legacy and lead.get("Theme", "").strip().lower() in LEGACY_THEMES:
            status, note = "skipped", "legacy theme (no OS2.0 sections)"
        new_rows.append(make_row({
            "Email": email, "Store Name": lead.get("Store Name", ""), "Domain": lead.get("Domain", ""),
            "Quality": quality, "Lead Score": lead.get("Lead Score", "0"), "Status": status, "Stage": 0,
            "Notes": note,
        }))
    append_in_chunks(out_ws, new_rows)
    if new_rows:
        log.info("Outreach tab me %s naye leads add hue.", len(new_rows))


def reconsider_skipped(out_rows, lead_by_email, lead_by_domain, unsub, min_quality, include_legacy):
    """Pehle skip hui rows (jinhe kabhi mail nahi gayi) dobara dekhta hai: setting dheeli hui to wapas queue me;
    bad/role email ho to Other Emails ya repaired address se."""
    known = {r["Email"].lower() for r in out_rows}
    back = fixed = 0
    for r in out_rows:
        if r["Status"] != "skipped" or to_int(r["Stage"]) > 0:
            continue
        note = r.get("Notes", "")
        if note.startswith("quality "):
            if QUALITY_RANK.get(r["Quality"].upper(), 0) >= QUALITY_RANK.get(min_quality, 0):
                r["Status"], r["Notes"] = "new", ""
                back += 1
        elif note.startswith("legacy theme"):
            if include_legacy:
                r["Status"], r["Notes"] = "new", ""
                back += 1
        elif note.startswith("bad email"):
            old = r["Email"].lower()
            lead = lead_by_email.get(old) or lead_by_domain.get((r.get("Domain") or "").lower()) or {"Email": old}
            new = pick_email(lead, known, unsub, first=old)
            if new and new != old:
                r["Email"], r["Status"], r["Notes"] = new, "new", f"alt email used (was: {old})"
                r["_email_dirty"] = True
                known.add(new)
                fixed += 1
    if back or fixed:
        log.info("Pehle skip hui rows wapas queue me: %s (setting ki wajah se) | %s (email theek/alternate mil gayi).", back, fixed)


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


def build_message(lead, stage, base_subject=None, link=None):
    """stage 0 = pehli email, 1/2 = follow-ups. Return (subject, body)."""
    store = (lead.get("Store Name") or "").strip() or "your store"
    domain = lead.get("Domain", "")
    theme = (lead.get("Theme") or "").strip()
    picks = picks_for(lead)
    names = [p[0] for p in picks]
    missing = [p[2] for p in picks if p[2]][:2]
    link = link or APP_URL
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
            f"You can check it out here:\n{link}\n\n"
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
                f"Happy to send a quick demo for {store} if useful: {link}\n\n"
                f"Best regards,\n{FROM_NAME}\n{APP_NAME} Team"
            )
        else:
            body = (
                f"Hi {store} team,\n\n"
                f"Last note from me — I don't want to clutter your inbox. If polishing {store}'s homepage "
                f"is on your list, {APP_NAME} is here whenever you need it: {link}\n\n"
                f"If it's not a fit, no worries at all.\n\n"
                f"Best regards,\n{FROM_NAME}\n{APP_NAME} Team"
            )

    footer = '\n\n---\nIf you\'d prefer not to receive emails like this, just reply with "unsubscribe" and I won\'t reach out again.'
    if PHYSICAL_ADDRESS:
        footer += f"\n{PHYSICAL_ADDRESS}"
    return subject, body + footer


# ============================================================
# SMTP / IMAP
# ============================================================
def load_senders():
    """Accounts: SENDERS_JSON secret (kitne bhi) + purane GMAIL_n_ADDRESS/GMAIL_n_APPPASS (1..20)."""
    senders, seen = [], set()
    raw = os.environ.get("SENDERS_JSON", "").strip()
    if raw:
        try:
            items = json.loads(raw)
        except Exception as e:
            log.error("SENDERS_JSON valid JSON nahi (%s) — sirf GMAIL_n accounts chalenge.", e)
            items = []
        for it in items:
            email = (it.get("email") or "").strip().lower()
            pw = (it.get("password") or it.get("app_password") or "").strip()
            if not email or not pw or email in seen:
                continue
            prov = (it.get("provider") or "gmail").strip().lower()
            cfg = dict(PROVIDERS.get(prov, PROVIDERS["gmail"]))
            for k in ("smtp_host", "smtp_port", "imap_host"):
                if it.get(k):
                    cfg[k] = it[k]
            senders.append({"email": email, "password": pw, "name": it.get("name") or FROM_NAME, "provider": prov,
                            "established": bool(it.get("warmed")), "start_date": (it.get("start_date") or "").strip(),
                            **cfg})
            seen.add(email)
    for i in range(1, 21):
        addr = os.environ.get(f"GMAIL_{i}_ADDRESS", "").strip().lower()
        pw = os.environ.get(f"GMAIL_{i}_APPPASS", "").strip()
        if addr and pw and addr not in seen:
            senders.append({"email": addr, "password": pw, "name": FROM_NAME, "provider": "gmail",
                            "established": True, "start_date": "", **PROVIDERS["gmail"]})
            seen.add(addr)
    return senders


def smtp_send(sender, to_email, subject, body, in_reply_to=None):
    msg = EmailMessage()
    msg["From"] = formataddr((sender["name"], sender["email"]))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    mid = make_msgid(domain=sender["email"].split("@")[1])
    msg["Message-ID"] = mid
    msg["List-Unsubscribe"] = f"<mailto:{sender['email']}?subject=unsubscribe>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    ctx = ssl.create_default_context()
    port = int(sender["smtp_port"])
    if port == 465:
        server = smtplib.SMTP_SSL(sender["smtp_host"], port, context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(sender["smtp_host"], port, timeout=30)
        server.starttls(context=ctx)
    try:
        server.login(sender["email"], sender["password"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass
    return mid


def text_of(msg):
    try:
        for p in (msg.walk() if msg.is_multipart() else [msg]):
            if p.get_content_type() == "text/plain":
                payload = p.get_payload(decode=True)
                if payload:
                    return payload.decode(p.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        pass
    return ""


AUTO_SUBJ_RE = re.compile(
    r"(auto[- ]?reply|automatic reply|autoreply|out of office|out-of-office|away from (the )?office|vacation|"
    r"request received|we(?:'ve| have) received|thank you for (contacting|reaching)|do not reply|\[ticket|ticket #)", re.I)


def is_auto_message(h):
    if (h.get("Auto-Submitted") or "").strip().lower() not in ("", "no"):
        return True
    if (h.get("Precedence") or "").strip().lower() in ("auto_reply", "bulk", "junk"):
        return True
    if h.get("X-Autoreply") or h.get("X-Autorespond") or h.get("X-Autoresponder") or h.get("X-Autoresponse"):
        return True
    return bool(AUTO_SUBJ_RE.search(h.get("Subject") or ""))


def clean_snippet(body, n=180):
    """Reply ka apna likha hua hissa (quoted purani email nikal kar)."""
    lines = []
    for ln in (body or "").splitlines():
        t = ln.strip()
        if t.startswith(">"):
            continue
        if re.match(r"^(on .+wrote:|-{2,}\s*original message|from:\s|sent from my)", t, re.I):
            break
        lines.append(t)
    return re.sub(r"\s+", " ", " ".join(lines)).strip()[:n]


def msg_date_iso(hdr_date):
    try:
        from email.utils import parsedate_to_datetime
        return iso(parsedate_to_datetime(hdr_date).astimezone(timezone.utc))
    except Exception:
        return ""


def check_inbox(sender, tracked):
    """tracked: {email: row}. Returns list of events:
       {'email','kind': 'reply'|'unsub'|'auto'|'bounced','date','snippet'}"""
    events = []
    domain_map = {}
    for e in tracked:
        d = e.split("@")[-1]
        if not is_free(d):
            domain_map.setdefault(d, e)
    try:
        M = imaplib.IMAP4_SSL(sender["imap_host"], timeout=30)
        M.login(sender["email"], sender["password"])
        M.select("INBOX", readonly=True)
        since = (utcnow() - timedelta(days=IMAP_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
        _, data = M.search(None, "SINCE", since)
        ids = data[0].split()

        fields = "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE AUTO-SUBMITTED PRECEDENCE X-AUTOREPLY X-AUTORESPOND X-AUTORESPONDER X-AUTORESPONSE)])"
        candidates = []  # (imap_id, matched_email or None(bounce), headers)
        for i in range(0, len(ids), 300):
            _, resp = M.fetch(b",".join(ids[i:i + 300]), fields)
            for item in resp:
                if not isinstance(item, tuple):
                    continue
                mid = item[0].split()[0]
                h = message_from_bytes(item[1])
                hd = {k: h.get(k) for k in ("From", "Subject", "Date", "Auto-Submitted", "Precedence",
                                            "X-Autoreply", "X-Autorespond", "X-Autoresponder", "X-Autoresponse")}
                from_addr = parseaddr(hd["From"] or "")[1].lower()
                if from_addr == sender["email"]:
                    continue
                if re.match(r"(mailer-daemon|postmaster)@", from_addr):
                    candidates.append((mid, None, hd))
                elif from_addr in tracked:
                    candidates.append((mid, from_addr, hd))
                elif from_addr.split("@")[-1] in domain_map:
                    candidates.append((mid, domain_map[from_addr.split("@")[-1]], hd))

        for mid, matched, hd in candidates[:400]:
            date = msg_date_iso(hd.get("Date") or "")
            if matched is not None and is_auto_message(hd):
                events.append({"email": matched, "kind": "auto", "date": date, "snippet": ""})
                continue
            _, fetched = M.fetch(mid, "(BODY.PEEK[]<0.9000>)")
            raw = next((x[1] for x in fetched if isinstance(x, tuple)), None)
            if not raw:
                continue
            body = text_of(message_from_bytes(raw))
            if matched is None:  # bounce
                blob = body + " " + raw.decode("utf-8", errors="ignore")
                for e in set(x.lower() for x in EMAIL_RE.findall(blob)):
                    if e in tracked:
                        events.append({"email": e, "kind": "bounced", "date": date, "snippet": ""})
            else:
                kind = "unsub" if STOP_RE.search(body.strip()[:300]) else "reply"
                events.append({"email": matched, "kind": kind, "date": date, "snippet": clean_snippet(body)})
        M.logout()
    except Exception as e:
        log.warning("IMAP check fail (%s): %s", sender["email"], e)
    return events


def sent_folder_has(sender, to_email, since_dt):
    """Sent folder me dekhta hai ke is address ko mail gayi thi ya nahi. True/False, ya None (check na ho saka)."""
    try:
        M = imaplib.IMAP4_SSL(sender.get("imap_host", "imap.gmail.com"), timeout=30)
        M.login(sender["email"], sender["password"])
        for folder in ('"[Gmail]/Sent Mail"', "Sent", '"Sent Items"'):
            typ, _ = M.select(folder, readonly=True)
            if typ == "OK":
                break
        _, data = M.search(None, "SINCE", since_dt.strftime("%d-%b-%Y"), "TO", f'"{to_email}"')
        found = bool(data and data[0].split())
        M.logout()
        return found
    except Exception as e:
        log.warning("Sent folder check fail (%s): %s", sender["email"], e)
        return None


def recover_stuck(out_rows, senders, now):
    """Run bheech me cancel/crash ho jaye to 'sending' me atki rows ko Sent folder se check karke theek karta hai,
    taake koi lead chupke se rah na jaye (aur double mail bhi na jaye)."""
    by_email = {s["email"]: s for s in senders}
    fixed = 0
    for r in out_rows:
        if r["Status"] != "sending":
            continue
        t = parse_iso(r["Last Sent"])
        if t and now - t < timedelta(minutes=30):
            continue
        s = by_email.get(r["Sender"].lower())
        if not s or fixed >= 20:
            continue
        found = sent_folder_has(s, r["Email"], (t or now) - timedelta(days=1))
        if found is None:
            continue
        stage = to_int(r["Stage"])
        if found:
            r["Stage"] = stage + 1
            if stage == 0:
                r["Status"], r["First Sent"] = "active", r["Last Sent"]
                r["Next Due"] = iso(now + timedelta(days=FU1_DAYS))
            elif stage == 1:
                r["Status"], r["Next Due"] = "active", iso(now + timedelta(days=FU2_DAYS))
            else:
                r["Status"], r["Next Due"] = "done", ""
            r["Notes"] = "recovered: mail Sent folder me mili"
        else:
            r["Status"] = "new" if stage == 0 else "active"
            if stage == 0:
                r["Last Sent"] = ""
            r["Notes"] = "recovered: mail nahi gayi thi, dobara try hogi"
        fixed += 1
        log.info("%s: atki 'sending' row theek ki -> %s", r["Email"], r["Status"])


def apply_events(tracked, events, now):
    """Inbox events ko rows me likho: reply count, replied at, snippet, type, status. Returns set of rows touched."""
    by = {}
    for ev in events:
        by.setdefault(ev["email"], []).append(ev)
    touched = {}
    for email, evs in by.items():
        r = tracked.get(email)
        if not r:
            continue
        real = [e for e in evs if e["kind"] == "reply"]
        unsubs = [e for e in evs if e["kind"] == "unsub"]
        autos = [e for e in evs if e["kind"] == "auto"]
        bounces = [e for e in evs if e["kind"] == "bounced"]
        if bounces and not real and not unsubs and r["Status"] != "bounced":
            r["Status"], r["Notes"] = "bounced", f"bounce detected {iso(now)}"
            log.info("%s -> bounced", email)
        if real:
            first = min(real, key=lambda e: e["date"] or "9")
            r["Reply Count"] = max(to_int(r.get("Reply Count")), len(real))
            r["Replied"], r["Reply Type"] = "Yes", "reply"
            if not r.get("Replied At"):
                r["Replied At"] = first["date"]
            if not r.get("Reply Snippet"):
                r["Reply Snippet"] = first["snippet"]
            if r["Status"] not in ("unsub", "bounced", "replied"):
                r["Status"], r["Notes"] = "replied", f"reply detected {iso(now)}"
                log.info("%s -> replied", email)
        if unsubs:
            if r["Status"] != "unsub":
                log.info("%s -> unsub", email)
            r["Status"], r["Notes"], r["Next Due"] = "unsub", f"unsubscribe request {iso(now)}", ""
            r["Reply Type"] = "unsubscribe"
            if not r.get("Reply Snippet"):
                r["Reply Snippet"] = unsubs[0]["snippet"]
        if autos and not real and not unsubs and not r.get("Reply Type"):
            r["Reply Type"] = "auto-reply"
        touched[r["_row"]] = r
    return touched


BOT_UA_RE = re.compile(
    r"(bot|spider|crawl|scan|proofpoint|mimecast|barracuda|safelinks|microsoft office|ms-office|outlook|"
    r"googleimageproxy|curl|python|wget|headless|preview|slack|whatsapp|facebookexternalhit|twitterbot|"
    r"linkedinbot|go-http|java/|okhttp|libwww|urlscan)", re.I)


def apply_clicks(clicks_ws, out_rows):
    """'Clicks' tab (Apps Script se bharta hai) -> Outreach me Clicks / First Click / Last Click.
    Bots aur email-security scanners (send ke 15 sec ke andar click, ya bot user-agent) ignore hote hain."""
    if clicks_ws is None:
        return 0
    by_tid = {r["Track ID"]: r for r in out_rows if r.get("Track ID")}
    if not by_tid:
        return 0
    times = {}
    for row in clicks_ws.get_all_values()[1:]:
        if len(row) < 2:
            continue
        ts, tid, ua = row[0].strip(), row[1].strip(), (row[2] if len(row) > 2 else "")
        r = by_tid.get(tid)
        if not r or BOT_UA_RE.search(ua):
            continue
        try:
            t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except Exception:
            t = None
        ls = parse_iso(r.get("Last Sent", ""))
        if t and ls and 0 <= (t - ls).total_seconds() < 15:
            continue
        times.setdefault(tid, []).append(t)
    n = 0
    for tid, ts_list in times.items():
        r = by_tid[tid]
        r["Clicks"] = len(ts_list)
        valid = [t for t in ts_list if t]
        if valid:
            r["First Click"], r["Last Click"] = iso(min(valid)), iso(max(valid))
        n += 1
    return n


# ============================================================
# DASHBOARD + REPLIES TAB
# ============================================================
def write_dashboard(ws, senders, sent24, now, waiting=0, per_day=0):
    O = OUTREACH_TAB
    rows = [
        ["SECTION MASTER — OUTREACH DASHBOARD", ""],
        ["Last updated (UTC)", iso(now)],
        ["", ""],
        ["Stores in list", f"=COUNTA({O}!A2:A)"],                                    # 4
        ["Waiting (not emailed yet)", f'=COUNTIF({O}!F2:F,"new")'],                  # 5
        ["Stores emailed (at least 1 email)", f'=COUNTIF({O}!O2:O,">0")'],           # 6
        ["Total emails sent", f"=SUM({O}!O2:O)"],                                    # 7
        ["Emails sent in last 24h", sent24],                                         # 8
        ["Sent 1/3", f'=COUNTIF({O}!N2:N,"Sent 1/3")'],                              # 9
        ["Sent 2/3", f'=COUNTIF({O}!N2:N,"Sent 2/3")'],                              # 10
        ["Sent 3/3 (sequence complete)", f'=COUNTIF({O}!N2:N,"Sent 3/3")'],          # 11
        ["Replies (real people)", f'=COUNTIF({O}!Q2:Q,"Yes")'],                      # 12
        ["Reply rate", "=IFERROR(B12/B6,0)"],                                        # 13
        ["Auto-replies", f'=COUNTIF({O}!T2:T,"auto-reply")'],                        # 14
        ["Unsubscribes", f'=COUNTIF({O}!T2:T,"unsubscribe")'],                       # 15
        ["Bounced", f'=COUNTIF({O}!V2:V,"Yes")'],                                    # 16
        ["Bounce rate", "=IFERROR(B16/B6,0)"],                                       # 17
        ["Stores that clicked the link", f'=COUNTIF({O}!W2:W,">0")'],                # 18
        ["Click rate", "=IFERROR(B18/B6,0)"],                                        # 19
        ["Total clicks", f"=SUM({O}!W2:W)"],                                         # 20
        ["Max sending speed (emails/day, all accounts)", per_day],                   # 21
        ["Estimated days to email all waiting leads", round(waiting / per_day) if per_day else "-"],  # 22
        ["Skipped: bad/junk email (nothing deliverable)", f'=COUNTIFS({O}!F2:F,"skipped",{O}!M2:M,"bad email*")'],
        ["Skipped: quality filter", f'=COUNTIFS({O}!F2:F,"skipped",{O}!M2:M,"quality*")'],
        ["Skipped: old theme (app unsupported)", f'=COUNTIFS({O}!F2:F,"skipped",{O}!M2:M,"legacy theme*")'],
        ["", ""],
        ["BY SENDER", "Stores emailed", "Replies", "Reply rate", "Bounced"],         # 22
    ]
    pct_cells = ["B13", "B17", "B19"]
    for s in senders:
        n = len(rows) + 1
        rows.append([s["email"],
                     f'=COUNTIFS({O}!H2:H,A{n},{O}!O2:O,">0")',
                     f'=COUNTIFS({O}!H2:H,A{n},{O}!Q2:Q,"Yes")',
                     f"=IFERROR(C{n}/B{n},0)",
                     f'=COUNTIFS({O}!H2:H,A{n},{O}!V2:V,"Yes")'])
        pct_cells.append(f"D{n}")
    rows.append(["", ""])
    rows.append(["BY LEAD QUALITY", "Stores emailed", "Replies", "Reply rate", "Bounced"])
    for label, crit in (("HOT", "HOT"), ("WARM", "WARM"), ("GOOD", "GOOD"), ("(old campaign / no quality)", "")):
        n = len(rows) + 1
        qcrit = f'"{crit}"'
        rows.append([label,
                     f'=COUNTIFS({O}!D2:D,{qcrit},{O}!O2:O,">0")',
                     f'=COUNTIFS({O}!D2:D,{qcrit},{O}!Q2:Q,"Yes")',
                     f"=IFERROR(C{n}/B{n},0)",
                     f'=COUNTIFS({O}!D2:D,{qcrit},{O}!V2:V,"Yes")'])
        pct_cells.append(f"D{n}")
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    try:
        ws.clear()
        ws.update(values=rows, range_name="A1", value_input_option="USER_ENTERED")
        ws.format(pct_cells, {"numberFormat": {"type": "PERCENT", "pattern": "0.0%"}})
        ws.format("A1", {"textFormat": {"bold": True, "fontSize": 13}})
    except Exception as e:
        log.warning("Dashboard update fail: %s", e)


def write_replies_tab(ws, rows):
    hdr = ["Store Name", "Email", "Domain", "Quality", "Replied At", "Reply Snippet", "Emails Sent", "Sender", "Status"]
    data = [[r["Store Name"], r["Email"], r["Domain"], r["Quality"], r["Replied At"], r["Reply Snippet"],
             r["Emails Sent"], r["Sender"], r["Status"]] for r in rows if r.get("Replied") == "Yes"]
    data.sort(key=lambda x: x[4], reverse=True)
    try:
        ws.clear()
        ws.update(values=[hdr] + data, range_name="A1", value_input_option="RAW")
        ws.freeze(rows=1)
    except Exception as e:
        log.warning("Replies tab update fail: %s", e)


# ============================================================
# ONE RUN
# ============================================================
LIVE_VALUES = ("live", "1", "true", "yes", "on")


def parse_ramp(txt):
    vals = [int(x) for x in re.findall(r"\d+", txt or "")]
    return vals or [int(x) for x in DEFAULT_RAMP.split(",")]


def parse_hours(txt):
    """'all' ya '13-23' (UTC ghante, inclusive; '22-4' bhi chalta hai) -> list of hours."""
    t = (txt or "all").strip().lower()
    m = re.match(r"^(\d{1,2})\s*-\s*(\d{1,2})$", t)
    if not m:
        return list(range(24))
    a, b = int(m.group(1)) % 24, int(m.group(2)) % 24
    hours, h = [a], a
    while h != b:
        h = (h + 1) % 24
        hours.append(h)
    return hours


def slots_for_hour(limit, hours, hour):
    """Daily limit ko send-window ke ghanton me barabar baantna (jaise 30/din = 1,2,1,2 ... har ghante)."""
    if hour not in hours:
        return 0
    k, w = hours.index(hour), len(hours)
    return (limit * (k + 1)) // w - (limit * k) // w


def daily_limit_for(info, ramp, cap, hard_cap, now):
    try:
        start = datetime.strptime(info["start"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        age = max(0, (now - start).days)
    except Exception:
        age = 0
    base = ramp[min(age // 7, len(ramp) - 1)]
    override = to_int(info.get("override"))
    if override > 0:
        base = override
    return max(0, min(base, cap, hard_cap))


def sync_senders_tab(ws, senders, now):
    """Senders tab me har account ki ek row (naye accounts khud add). User Status/Start Date/Override badal sakta hai."""
    rows = {r["Email"].lower(): r for r in sheet_rows(ws) if r.get("Email")}
    new_rows = []
    for s in senders:
        if s["email"] in rows:
            continue
        if s.get("start_date"):
            start = s["start_date"]
        elif s.get("established"):
            start = (now - timedelta(days=ESTABLISHED_AGE_DAYS)).strftime("%Y-%m-%d")
        else:
            start = now.strftime("%Y-%m-%d")
        new_rows.append([s["email"], s["provider"], s["name"], "active", start, "", "", "", "", "", "", "", "", "",
                         "new account - ramp-up shuru" if not s.get("established") else "established account"])
    if new_rows:
        ws.append_rows(new_rows, value_input_option="RAW")
        log.info("Senders tab me %s naye accounts add hue.", len(new_rows))
        rows = {r["Email"].lower(): r for r in sheet_rows(ws) if r.get("Email")}
    info = {}
    for s in senders:
        r = rows.get(s["email"])
        if r:
            info[s["email"]] = {"status": (r.get("Status") or "active").strip().lower(), "start": r.get("Start Date", ""),
                                "override": r.get("Daily Limit Override", ""), "notes": r.get("Notes", ""),
                                "_row": r["_row"]}
    return info


def sender_stats(out_rows, now):
    st = {}
    d1, d7 = now - timedelta(hours=24), now - timedelta(days=7)
    hour_start = now.replace(minute=0, second=0, microsecond=0)
    for r in out_rows:
        sd = r["Sender"].lower()
        if not sd:
            continue
        x = st.setdefault(sd, {"s24": 0, "s7": 0, "b7": 0, "replies": 0, "emailed": 0, "hour": 0})
        if to_int(r["Stage"]) > 0:
            x["emailed"] += 1
        if r.get("Replied") == "Yes":
            x["replies"] += 1
        t = parse_iso(r["Last Sent"])
        if t:
            if t > d1:
                x["s24"] += 1
            if t >= hour_start:
                x["hour"] += 1
            if t > d7:
                x["s7"] += 1
                if r["Status"] == "bounced":
                    x["b7"] += 1
    return st


def apply_account_health(senders, sinfo, stats, now):
    """Kisi account ka bounce rate zyada ho to sirf wahi 48 ghante ke liye pause; pause khatam to auto resume."""
    for s in senders:
        i, x = sinfo[s["email"]], stats.get(s["email"], {})
        if i["status"] == "paused":
            m = re.search(r"paused until (\S+)", i["notes"])
            until = parse_iso(m.group(1)) if m else None
            if until and until <= now:
                i["status"], i["notes"] = "active", "auto-resumed after pause"
                log.info("%s: pause khatam, wapas active.", s["email"])
        elif i["status"] == "active":
            s7, b7 = x.get("s7", 0), x.get("b7", 0)
            if s7 >= ACCOUNT_BOUNCE_MIN_SENT and b7 >= ACCOUNT_BOUNCE_MIN_COUNT and b7 / s7 > ACCOUNT_BOUNCE_RATIO:
                i["status"] = "paused"
                i["notes"] = f"paused until {iso(now + timedelta(hours=48))} (bounce {100 * b7 / s7:.0f}% of {s7})"
                log.error("%s: bounce rate %s/%s — account 48 ghante ke liye pause.", s["email"], b7, s7)


def write_senders_tab(ws, senders, sinfo, stats, limits, now):
    payload = []
    for s in senders:
        i, x = sinfo[s["email"]], stats.get(s["email"], {})
        try:
            age = (now.date() - datetime.strptime(i["start"], "%Y-%m-%d").date()).days
        except Exception:
            age = ""
        s7, b7, em = x.get("s7", 0), x.get("b7", 0), x.get("emailed", 0)
        payload.append({"range": f"A{i['_row']}:O{i['_row']}", "values": [[
            s["email"], s["provider"], s["name"], i["status"], i["start"], i["override"], age,
            limits.get(s["email"], 0), x.get("s24", 0), s7, x.get("replies", 0), b7,
            f"{100 * b7 / s7:.1f}%" if s7 else "", f"{100 * x.get('replies', 0) / em:.1f}%" if em else "", i["notes"]]]})
    try:
        for k in range(0, len(payload), 200):
            ws.batch_update(payload[k:k + 200], value_input_option="RAW")
    except Exception as e:
        log.warning("Senders tab update fail: %s", e)


def order_pool(pool, priority, score):
    """Naye leads kis tarteeb se bheje jayen. score = best leads pehle | random | newest | oldest."""
    if priority == "random":
        pool = list(pool)
        random.shuffle(pool)
        return pool
    if priority == "newest":
        return sorted(pool, key=lambda r: -r["_row"])
    if priority == "oldest":
        return sorted(pool, key=lambda r: r["_row"])
    return sorted(pool, key=lambda r: (-QUALITY_RANK.get(r["Quality"].upper(), 0), -score(r)))


def read_settings(control_ws):
    """Control tab: 'Setting | Value' rows -> dict. Missing settings khud add ho jati hain (default value ke sath)."""
    settings, has_note = {}, False
    for row in control_ws.get_all_values()[1:]:
        if row and row[0].strip():
            settings[row[0].strip().upper()] = (row[1] if len(row) > 1 else "").strip()
            has_note = has_note or row[0].strip().lower() == "(note)"
    defaults = [
        ("SEND_MODE", "dry" if DRY_RUN else "live"),
        ("TRACK_URL", ""),
        ("PER_SENDER_PER_RUN", str(PER_SENDER_PER_RUN)),
        ("DAILY_MAX_PER_SENDER", str(DAILY_MAX_PER_SENDER)),
        ("PRIORITY", "score"),
        ("RAMP_SCHEDULE", DEFAULT_RAMP),
        ("SEND_HOURS_UTC", "all"),
        ("MIN_QUALITY", MIN_QUALITY),
        ("INCLUDE_LEGACY_THEMES", "no"),
    ]
    added = False
    for key, val in defaults:
        if key not in settings:
            settings[key] = val
            control_ws.append_row([key, val], value_input_option="RAW")
            added = True
    if added and not has_note:
        control_ws.append_row([
            "(note)",
            "SEND_MODE: live/dry/stop | TRACK_URL: click tracking link (khali=band) | PER_SENDER_PER_RUN: har account har run "
            "(max 5) | DAILY_MAX_PER_SENDER: har account ki upar ki hadd (max 60) | PRIORITY: score / random / newest / oldest | "
            "RAMP_SCHEDULE: naye account ki har hafte ki daily limit | SEND_HOURS_UTC: 'all' ya '13-23' | "
            "MIN_QUALITY: LOW (sab) / GOOD / WARM / HOT | INCLUDE_LEGACY_THEMES: yes/no"],
            value_input_option="RAW")
    return settings


def run_once():
    global DRY_RUN, PER_SENDER_PER_RUN, DAILY_MAX_PER_SENDER
    senders = load_senders()
    if not senders:
        sys.exit("Koi GMAIL_n_ADDRESS / GMAIL_n_APPPASS env var nahi mila.")
    log.info("%s Gmail accounts load hue | mode: %s | SEND_MODE env = %r", len(senders), "DRY RUN" if DRY_RUN else "LIVE", SEND_MODE_RAW)

    T = connect()
    gc, leads_ws, out_ws, unsub_ws, control_ws = T["gc"], T["leads"], T["out"], T["unsub"], T["control"]

    # SHEET KA "Control" TAB = asli switch
    sheet_val, track_url, settings = None, "", {}
    try:
        settings = read_settings(control_ws)
        sheet_val = settings.get("SEND_MODE", "").lower()
        track_url = settings.get("TRACK_URL", "").strip() or os.environ.get("TRACK_URL", "").strip()
        PER_SENDER_PER_RUN = min(max(to_int(settings.get("PER_SENDER_PER_RUN")) or PER_SENDER_PER_RUN, 1),
                                 HARD_MAX_PER_SENDER_PER_RUN)
        DAILY_MAX_PER_SENDER = min(max(to_int(settings.get("DAILY_MAX_PER_SENDER")) or DAILY_MAX_PER_SENDER, 1),
                                   HARD_MAX_DAILY_PER_SENDER)
    except Exception as e:
        log.warning("Control tab padh nahi saka (%s) — GitHub env wala mode chalega.", e)
    if sheet_val:
        DRY_RUN = sheet_val not in LIVE_VALUES
    if track_url and not track_url.startswith("http"):
        log.warning("TRACK_URL 'http' se shuru nahi hota — click tracking band.")
        track_url = ""
    per_sender = min(5, max(1, to_int(settings.get("PER_SENDER_PER_RUN")) or PER_SENDER_PER_RUN))
    daily_max = min(60, max(1, to_int(settings.get("DAILY_MAX_PER_SENDER")) or DAILY_MAX_PER_SENDER))
    priority = (settings.get("PRIORITY") or "score").strip().lower()
    min_quality = (settings.get("MIN_QUALITY") or MIN_QUALITY).strip().upper()
    if min_quality not in QUALITY_RANK:
        min_quality = "LOW"
    include_legacy = (settings.get("INCLUDE_LEGACY_THEMES") or "no").strip().lower() in LIVE_VALUES
    log.info("FINAL MODE: %s | GitHub env SEND_MODE=%r | Sheet 'Control' tab SEND_MODE=%r | click tracking: %s",
             "DRY RUN (koi email nahi jayegi)" if DRY_RUN else "LIVE (asli emails)", SEND_MODE_RAW, sheet_val,
             "ON" if track_url else "OFF")
    log.info("Settings: har account har run %s mails | 24h me max %s | tarteeb: %s | min quality: %s | purani themes: %s",
             per_sender, daily_max, priority, min_quality, "shamil" if include_legacy else "nahi")
    log.info("SPEED: har account %s mail/run, max %s/din | %s accounts => max %s mails/din",
             PER_SENDER_PER_RUN, DAILY_MAX_PER_SENDER, len(senders),
             len(senders) * min(PER_SENDER_PER_RUN * 24, DAILY_MAX_PER_SENDER))
    unsub = {r["Email or Domain"].lower() for r in sheet_rows(unsub_ws) if r.get("Email or Domain")}

    out_rows = sheet_rows(out_ws)
    migrate_old_sheet(gc, out_ws, out_rows, senders, unsub)
    out_rows = sheet_rows(out_ws)

    leads_rows = sheet_rows(leads_ws)
    sync_leads(leads_rows, out_ws, out_rows, unsub, min_quality, include_legacy)
    out_rows = sheet_rows(out_ws)
    lead_by_email = {l["Email"].lower(): l for l in leads_rows if l.get("Email")}
    lead_by_domain = {l["Domain"].lower(): l for l in leads_rows if l.get("Domain")}
    now = utcnow()
    log.info("Leads tab: %s rows | Outreach tab: %s rows", len(leads_rows), len(out_rows))

    original = {r["_row"]: sig(r) for r in out_rows}   # is run ke shuru ki haalat (kya badla, ye dekhne ke liye)

    # ---- 0) skip hui rows dobara dekho + atki 'sending' rows theek karo ----
    reconsider_skipped(out_rows, lead_by_email, lead_by_domain, unsub, min_quality, include_legacy)
    recover_stuck(out_rows, senders, now)

    # ---- 1) inbox: replies / auto-replies / bounces / unsubscribes ----
    tracked = {r["Email"].lower(): r for r in out_rows if to_int(r["Stage"]) > 0}
    legacy_replied = {e for e, r in tracked.items() if r["Status"] == "replied" and not r.get("Reply Type")}
    events = []
    for s in senders:
        events += check_inbox(s, tracked)
    apply_events(tracked, events, now)
    # purani (v2) 'replied' marking me auto-replies bhi thin: agar ab scan me sirf auto-reply mile to wapas active
    seen_kinds = {}
    for ev in events:
        seen_kinds.setdefault(ev["email"], set()).add(ev["kind"])
    for e in legacy_replied:
        kinds = seen_kinds.get(e)
        r = tracked[e]
        if kinds and kinds <= {"auto"} and r["Status"] == "replied":
            r["Status"], r["Replied"], r["Reply Count"], r["Reply Type"] = "active", "No", 0, "auto-reply"
            r["Notes"] = "auto-reply (not a real reply)"

    # ---- 1b) junk / placeholder addresses jo abhi 'new' ya 'active' hain -> skipped ----
    bad_count = 0
    for r in out_rows:
        if r["Status"] in ("new", "active"):
            why = email_problem(r["Email"])
            if why:
                r["Status"], r["Notes"], r["Next Due"] = "skipped", f"bad email: {why}", ""
                bad_count += 1
    if bad_count:
        log.info("%s junk/galat email addresses skip mark hue.", bad_count)

    # ---- 2) manual Unsubscribed tab ----
    for r in out_rows:
        if r["Status"] in ("new", "active") and is_unsub(r["Email"].lower(), unsub):
            r["Status"], r["Notes"] = "unsub", "in Unsubscribed tab"

    # ---- 3) link clicks ----
    try:
        clicked = apply_clicks(T["clicks"], out_rows)
        if clicked:
            log.info("Click tracking: %s stores ne link click kiya.", clicked)
    except Exception as e:
        log.warning("Clicks tab process nahi hua: %s", e)

    # ---- 4) friendly columns + sheet me sirf jo badla wo likho ----
    for r in out_rows:
        derive(r)
    changed = [r for r in out_rows if sig(r) != original[r["_row"]]]
    if len(changed) > 600:
        log.info("%s rows update ho rahi hain (naye tracking columns) — block me likh raha hun...", len(changed))
        block_write_all(out_ws, out_rows)
    elif changed:
        batch_update_rows(out_ws, changed)
    if changed:
        log.info("Sheet me %s rows update hui.", len(changed))
    dirty = [r for r in out_rows if r.get("_email_dirty")]
    for i in range(0, len(dirty), 300):
        try:
            out_ws.batch_update([{"range": f"A{r['_row']}", "values": [[r["Email"]]]} for r in dirty[i:i + 300]],
                                value_input_option="RAW")
        except Exception as e:
            log.warning("Email column update fail: %s", e)
    original = {r["_row"]: sig(r) for r in out_rows}

    # ---- 5) CIRCUIT BREAKER ----
    since72 = now - timedelta(hours=72)
    sent72 = [r for r in out_rows if (parse_iso(r["Last Sent"]) or since72) > since72]
    bounced72 = [r for r in sent72 if r["Status"] == "bounced"]
    paused = (len(sent72) >= BOUNCE_MIN_SAMPLE and len(bounced72) >= BOUNCE_MIN_COUNT
              and len(bounced72) / len(sent72) > BOUNCE_PAUSE_RATIO)
    if paused:
        log.error("SENDING PAUSED: pichle 72h me %s me se %s bounce (%.0f%%) — limit %.0f%%. "
                  "Pehle bounce ki wajah dekhein, phir Outreach tab me bounced rows check karein.",
                  len(sent72), len(bounced72), 100 * len(bounced72) / len(sent72), 100 * BOUNCE_PAUSE_RATIO)

    # ---- 6) quota + sending ----
    total_sent = 0
    if not paused:
        window_start = now - timedelta(minutes=QUOTA_WINDOW_MINUTES)
        day_start = now - timedelta(hours=24)
        recent, daily = {}, {}
        for r in out_rows:
            t, sd = parse_iso(r["Last Sent"]), r["Sender"].lower()
            if sd and t:
                if t > window_start:
                    recent[sd] = recent.get(sd, 0) + 1
                if t > day_start:
                    daily[sd] = daily.get(sd, 0) + 1

        def score(r):
            try:
                return int(r["Lead Score"] or 0)
            except ValueError:
                return 0

        new_pool = order_pool([r for r in out_rows if r["Status"] == "new"], priority, score)
        log.info("Queue: %s naye leads intezar me (tarteeb: %s)", len(new_pool), priority)
        max_per_run = per_sender * len(senders)   # poori run ki HARD limit
        known_senders = {x["email"] for x in senders}

        for s in senders:
            if total_sent >= max_per_run:
                break
            quota = min(per_sender - recent.get(s["email"], 0),
                        daily_max - daily.get(s["email"], 0))
            if quota <= 0:
                log.info("%s: quota pura, skip.", s["email"])
                continue

            due = sorted(
                [r for r in out_rows if r["Status"] == "active"
                 and (r["Sender"].lower() == s["email"] or r["Sender"].lower() not in known_senders)
                 and r["Stage"] in ("1", "2", 1, 2) and (parse_iso(r["Next Due"]) or now) <= now],
                key=lambda r: r["Next Due"],
            )
            batch = due[:quota]
            while len(batch) < quota and new_pool:
                batch.append(new_pool.pop(0))

            for n, r in enumerate(batch):
                if total_sent >= max_per_run:
                    break
                email = r["Email"].lower()
                stage = to_int(r["Stage"])
                lead = (lead_by_email.get(email) or lead_by_domain.get((r["Domain"] or "").lower())
                        or {"Store Name": r["Store Name"], "Domain": r["Domain"]})
                tid = r.get("Track ID") or make_tid(email)
                link = None
                if track_url:
                    base = track_url if "?" in track_url else track_url.rstrip("/") + "/"
                    link = base + ("&" if "?" in base else "?") + "t=" + tid
                subject, body = build_message(lead, stage, base_subject=r["Subject"] or None, link=link)

                if DRY_RUN:
                    log.info("[DRY RUN] %s -> %s (email %s/3)\nSubject: %s\n%s\n%s",
                             s["email"], email, stage + 1, subject, body, "-" * 60)
                    continue

                # PEHLE sheet me "sending" reserve karo — save na ho to email bheji hi nahi jayegi
                prev = {k: r.get(k, "") for k in ("Status", "Last Sent", "Sender", "Track ID")}
                r["Status"], r["Last Sent"], r["Sender"], r["Track ID"] = "sending", iso(utcnow()), s["email"], tid
                if not update_row(out_ws, r):
                    r.update(prev)
                    log.error("Sheet me reserve save nahi hua — sending ROK di (duplicate se bachne ke liye).")
                    total_sent = -1
                    break

                def rollback():
                    r.update(prev)
                    update_row(out_ws, r)

                try:
                    mid = smtp_send(s, email, subject, body,
                                    in_reply_to=r["Message-ID"] if (stage > 0 and r["Message-ID"]) else None)
                except smtplib.SMTPAuthenticationError as e:
                    log.error("%s: login fail (%s) — is account ko skip kar raha hun.", s["email"], e)
                    rollback()
                    break
                except (smtplib.SMTPRecipientsRefused, smtplib.SMTPDataError) as e:
                    r["Status"], r["Notes"] = "bounced", f"SMTP refused: {str(e)[:120]}"
                    derive(r)
                    update_row(out_ws, r)
                    continue
                except Exception as e:
                    log.error("Send fail %s -> %s: %s", s["email"], email, e)
                    rollback()
                    continue

                r["Stage"] = stage + 1
                if stage == 0:
                    r["Message-ID"], r["Subject"], r["Status"] = mid, subject, "active"
                    r["First Sent"] = iso(utcnow())
                    r["Next Due"] = iso(utcnow() + timedelta(days=FU1_DAYS))
                elif stage == 1:
                    r["Status"] = "active"
                    r["Next Due"] = iso(utcnow() + timedelta(days=FU2_DAYS))
                else:
                    r["Next Due"], r["Status"] = "", "done"
                derive(r)
                update_row(out_ws, r)
                total_sent += 1
                log.info("SENT %s -> %s (email %s/3) [%s/%s is run me]", s["email"], email, stage + 1, total_sent, max_per_run)
                if n < len(batch) - 1:
                    time.sleep(random.randint(*SEND_GAP))
            if total_sent < 0:
                total_sent = 0
                break

    # ---- 7) Dashboard + Replies tab ----
    sent24 = 0
    for r in out_rows:
        t = parse_iso(r["Last Sent"])
        if t and t > now - timedelta(hours=24):
            sent24 += 1
    waiting = sum(1 for r in out_rows if r["Status"] == "new")
    per_day = len(senders) * min(PER_SENDER_PER_RUN * 24, DAILY_MAX_PER_SENDER)
    write_dashboard(T["dashboard"], senders, sent24, now, waiting, per_day)
    write_replies_tab(T["replies"], out_rows)

    counts = {}
    for r in out_rows:
        counts[r["Status"]] = counts.get(r["Status"], 0) + 1
    replies = sum(1 for r in out_rows if r.get("Replied") == "Yes")
    log.info("===== DONE ===== is run me bheji: %s | replies: %s | status summary: %s", total_sent, replies, counts)


if __name__ == "__main__":
    run_once()
