import os
import time
import smtplib
from email.mime.text import MIMEText
import gspread
from google.oauth2.credentials import Credentials

SHEET_NAME = "WebDev Leads"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

EMAILS_PER_ACCOUNT_PER_RUN = 2

# Same 6 Gmail accounts (same GitHub Secrets) reuse ho rahe hain
GMAIL_ACCOUNTS = []
for i in range(1, 7):
    address = os.environ.get(f"GMAIL_{i}_ADDRESS")
    app_password = os.environ.get(f"GMAIL_{i}_APPPASS")
    if address and app_password:
        GMAIL_ACCOUNTS.append({"address": address, "password": app_password})

EMAIL_SUBJECT = "Quick idea for {business_name}'s online presence"

EMAIL_TEMPLATE = """Hi {business_name} team,

I came across your {niche} business in {city} while researching local businesses,
and noticed you don't have a dedicated website yet (just your social page).

A simple, professional website could help you:
- Show up when people search for "{niche} near {city}" on Google
- Let customers see your menu/services, hours, and location in one place
- Build more trust than a social page alone

I build affordable, fast-loading websites for local businesses like yours. If you'd
like, I can put together a free mockup of what your site could look like — no
obligation at all.

Just reply to this email if you're interested, or let me know if you'd rather not
be contacted again.

Best regards,
Bilal Ahmed

---
If you'd prefer not to receive emails like this, just reply with "unsubscribe" and
I won't reach out again.
"""


def get_sheet():
    creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    gc = gspread.authorize(creds)
    return gc.open(SHEET_NAME).sheet1


def send_email(account, to_email, business_name, niche, city):
    subject = EMAIL_SUBJECT.format(business_name=business_name)
    body = EMAIL_TEMPLATE.format(business_name=business_name, niche=niche, city=city)

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
    business_name_col_index = header.index("Business Name")
    niche_col_index = header.index("Niche")
    city_col_index = header.index("City")

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

            if not email or email == "Nahi mila":
                continue
            if email.lower() in already_emailed_addresses:
                continue

            business_name = row[business_name_col_index] if len(row) > business_name_col_index else "there"
            niche = row[niche_col_index] if len(row) > niche_col_index else "local"
            city = row[city_col_index] if len(row) > city_col_index else "your area"

            attempts_for_this_account += 1

            try:
                send_email(account, email, business_name, niche, city)
                sheet.update_cell(row_num, emailed_col_index, f"Yes ({account['address']})")
                row[emailed_col_index - 1] = f"Yes ({account['address']})"
                already_emailed_addresses.add(email.lower())
                print(f"   ✅ Email sent to {email} from {account['address']}")
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
