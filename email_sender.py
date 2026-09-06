import os
import re
import time
import smtplib
from email.mime.text import MIMEText
import gspread
from google.oauth2.credentials import Credentials

SHEET_NAME = "Section Master Leads"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

EMAILS_PER_ACCOUNT_PER_RUN = 2

GMAIL_ACCOUNTS = []
for i in range(1, 7):
    address = os.environ.get(f"GMAIL_{i}_ADDRESS")
    app_password = os.environ.get(f"GMAIL_{i}_APPPASS")
    if address and app_password:
        GMAIL_ACCOUNTS.append({"address": address, "password": app_password})

EMAIL_SUBJECT = "A quick idea for {store_name}'s homepage"

# {feature_intro} aur {feature_bullets} ab har lead ke liye alag hongi —
# jo unke store mein waqai missing hai wahi mention hoga, generic list nahi.
EMAIL_TEMPLATE = """Hi {store_name} team,

I came across your store while researching {niche} brands on Shopify, and noticed you're on a default theme without {feature_intro}.

We built Section Master, a free Shopify app that lets you add exactly these kinds of sections without touching any code:

{feature_bullets}

All of these are completely free to use. You can check it out here:
https://apps.shopify.com/section-master

If you'd like a quick free demo of how it could look on your store, just reply to this email.

Best regards,
Bilal Ahmed
Section Master Team

---
If you'd prefer not to receive emails like this, just reply with "unsubscribe" and I won't reach out again.
"""


def get_sheet():
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    gc = gspread.authorize(creds)
    return gc.open(SHEET_NAME).sheet1


def get_store_name(url):
    name = re.sub(r'https?://(www\.)?', '', url)
    name = name.split('/')[0].split('.')[0]
    return name.replace('-', ' ').title()


def build_personalization(missing_features_str):
    """Sheet ke 'Missing Features' column (comma-separated) se
    personalized intro line aur bullet list banata hai."""
    features = [f.strip() for f in missing_features_str.split(",") if f.strip()]

    if not features:
        # Purani rows ya data na hone ki surat mein reasonable fallback
        features = ["an FAQ section", "customer testimonials", "a sticky add-to-cart bar"]

    features = features[:4]  # email zyada lamba na ho, top 4 tak rakho

    if len(features) == 1:
        feature_intro = features[0]
    elif len(features) == 2:
        feature_intro = f"{features[0]} or {features[1]}"
    else:
        feature_intro = ", ".join(features[:-1]) + f", or {features[-1]}"

    feature_bullets = "\n".join(f"- {feat[0].upper() + feat[1:]}" for feat in features)

    return feature_intro, feature_bullets


def send_email(account, to_email, store_name, niche, feature_intro, feature_bullets):
    subject = EMAIL_SUBJECT.format(store_name=store_name)
    body = EMAIL_TEMPLATE.format(
        store_name=store_name, niche=niche,
        feature_intro=feature_intro, feature_bullets=feature_bullets,
    )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = account["address"]
    msg["To"] = to_email

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(account["address"], account["password"])
        server.sendmail(account["address"], [to_email], msg.as_string())


def run_once():
    if not GMAIL_ACCOUNTS:
        print("⚠️  Koi Gmail account configure nahi hua (environment variables missing). Rokte hain.")
        return

    print(f"📧 {len(GMAIL_ACCOUNTS)} Gmail accounts load hue.")

    sheet = get_sheet()
    all_rows = sheet.get_all_values()
    header = all_rows[0]

    if "Emailed" not in header:
        sheet.update_cell(1, len(header) + 1, "Emailed")
        header.append("Emailed")

    emailed_col_index = header.index("Emailed") + 1
    email_col_index = header.index("Email")
    niche_col_index = header.index("Niche")
    url_col_index = header.index("Store URL")
    # "Missing Features" column purani sheets mein nahi hogi — gracefully handle karo
    missing_features_col_index = header.index("Missing Features") if "Missing Features" in header else None

    target_len = len(header)
    for row in all_rows[1:]:
        while len(row) < target_len:
            row.append("")

    already_emailed_addresses = set()
    for row in all_rows[1:]:
        row_email = row[email_col_index] if len(row) > email_col_index else ""
        row_status = row[emailed_col_index - 1] if len(row) >= emailed_col_index else ""
        if row_email and row_status.startswith("Yes"):
            already_emailed_addresses.add(row_email.lower())

    sent_count = 0

    for account in GMAIL_ACCOUNTS:
        sent_for_this_account = 0
        attempts_for_this_account = 0
        max_attempts_per_account = EMAILS_PER_ACCOUNT_PER_RUN + 2

        for row_num, row in enumerate(all_rows[1:], start=2):
            if sent_for_this_account >= EMAILS_PER_ACCOUNT_PER_RUN:
                break
            if attempts_for_this_account >= max_attempts_per_account:
                print(f"   ⚠️  {account['address']} ke liye zyada fails ho gaye, is account ko skip kar rahe hain")
                break

            email = row[email_col_index] if len(row) > email_col_index else ""

            if not email or email == "Nahi mila" or email.startswith("Facebook"):
                continue
            if re.match(r'^x+@x+\.x+$', email.lower()):
                continue
            if email.lower() in already_emailed_addresses:
                continue

            store_url = row[url_col_index]
            niche = row[niche_col_index] if len(row) > niche_col_index else "e-commerce"
            store_name = get_store_name(store_url)

            missing_features_str = ""
            if missing_features_col_index is not None and len(row) > missing_features_col_index:
                missing_features_str = row[missing_features_col_index]

            feature_intro, feature_bullets = build_personalization(missing_features_str)

            attempts_for_this_account += 1

            try:
                send_email(account, email, store_name, niche, feature_intro, feature_bullets)
                sheet.update_cell(row_num, emailed_col_index, f"Yes ({account['address']})")
                row[emailed_col_index - 1] = f"Yes ({account['address']})"
                already_emailed_addresses.add(email.lower())
                print(f"   ✅ Email sent to {email} from {account['address']} (personalized: {feature_intro})")
                sent_for_this_account += 1
                sent_count += 1
                time.sleep(5)
            except smtplib.SMTPAuthenticationError:
                print(f"   ❌ {account['address']} ka login fail ho gaya (credentials galat hain), is account ko skip kar rahe hain")
                break
            except Exception as e:
                print(f"   ❌ Failed to send to {email}: {e}")
                sheet.update_cell(row_num, emailed_col_index, f"Failed: {e}")
                row[emailed_col_index - 1] = f"Failed: {e}"

    print(f"\n===== DONE ===== Total {sent_count} emails is run mein send hui.")


if __name__ == "__main__":
    run_once()
