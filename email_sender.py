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

# 5 accounts environment variables se load honge (GitHub Secrets ya local env vars)
GMAIL_ACCOUNTS = []
for i in range(1, 7):
    address = os.environ.get(f"GMAIL_{i}_ADDRESS")
    app_password = os.environ.get(f"GMAIL_{i}_APPPASS")
    if address and app_password:
        GMAIL_ACCOUNTS.append({"address": address, "password": app_password})

EMAIL_SUBJECT = "A free way to boost {store_name}'s homepage"

EMAIL_TEMPLATE = """Hi {store_name} team,

I came across your store while researching {niche} brands on Shopify.

I noticed your store could benefit from a few extra trust-building sections on your homepage and product pages. We built Section Master, a free Shopify app that lets you add professional, drag-and-drop sections without touching any code — including:

- FAQ sections (answer common customer questions right on the page)
- Customer testimonials (build trust and boost conversions)
- A sticky WhatsApp button (let customers reach you instantly)

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


def send_email(account, to_email, store_name, niche):
    subject = EMAIL_SUBJECT.format(store_name=store_name)
    body = EMAIL_TEMPLATE.format(store_name=store_name, niche=niche)

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

    emailed_col_index = header.index("Emailed") + 1  # 1-based column number
    email_col_index = header.index("Email")
    niche_col_index = header.index("Niche")
    url_col_index = header.index("Store URL")

    # Har row ko header ki length tak pad karo, taake short rows pe index error na aaye
    target_len = len(header)
    for row in all_rows[1:]:
        while len(row) < target_len:
            row.append("")

    # Sab already-emailed EMAIL ADDRESSES ka set bana lo (URL ke bajaye email track karna,
    # taake duplicate stores jinka email same ho unhe dobara email na jaye)
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
        max_attempts_per_account = EMAILS_PER_ACCOUNT_PER_RUN + 2  # thodi si extra room fail hone par, lekin bounded

        for row_num, row in enumerate(all_rows[1:], start=2):  # sheet row 2 se shuru (row 1 = header)
            if sent_for_this_account >= EMAILS_PER_ACCOUNT_PER_RUN:
                break
            if attempts_for_this_account >= max_attempts_per_account:
                print(f"   ⚠️  {account['address']} ke liye zyada fails ho gaye, is account ko skip kar rahe hain")
                break

            email = row[email_col_index] if len(row) > email_col_index else ""

            if not email or email == "Nahi mila" or email.startswith("Facebook"):
                continue
            if re.match(r'^x+@x+\.x+$', email.lower()):  # xxx@xxx.xxx jaisi fake/placeholder emails
                continue
            if email.lower() in already_emailed_addresses:  # ye email pehle hi kisi row se ja chuki hai
                continue

            store_url = row[url_col_index]
            niche = row[niche_col_index] if len(row) > niche_col_index else "e-commerce"
            store_name = get_store_name(store_url)

            attempts_for_this_account += 1

            try:
                send_email(account, email, store_name, niche)
                sheet.update_cell(row_num, emailed_col_index, f"Yes ({account['address']})")
                row[emailed_col_index - 1] = f"Yes ({account['address']})"  # local copy bhi update, taake dobara attempt na ho
                already_emailed_addresses.add(email.lower())  # is email ko globally track karo
                print(f"   ✅ Email sent to {email} from {account['address']}")
                sent_for_this_account += 1
                sent_count += 1
                time.sleep(5)
            except smtplib.SMTPAuthenticationError as e:
                print(f"   ❌ {account['address']} ka login fail ho gaya (credentials galat hain), is account ko skip kar rahe hain")
                break  # is account ke credentials hi galat hain, aage try karne ka faida nahi
            except Exception as e:
                print(f"   ❌ Failed to send to {email}: {e}")
                sheet.update_cell(row_num, emailed_col_index, f"Failed: {e}")
                row[emailed_col_index - 1] = f"Failed: {e}"

    print(f"\n===== DONE ===== Total {sent_count} emails is run mein send hui.")


if __name__ == "__main__":
    run_once()
