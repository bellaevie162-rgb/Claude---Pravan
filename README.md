# USCIS Case Status Monitor

> **Status: paused.** USCIS blocks requests from GitHub's servers (HTTP 403), so the automatic schedule has been removed. The script can still be run from a home computer. See "Run it locally instead" below.

A small bot that checks your USCIS case status every 30 minutes and **emails and/or texts you when the status changes**. It runs free on GitHub Actions, so nothing has to stay running on your computer.

Your receipt number and passwords are stored as **GitHub Secrets**, not in the code. The last-seen status lives in the Actions cache, not in the repo.

## Setup (about 10 minutes)

### 1. Get a Gmail App Password (for sending email)
1. Turn on 2-Step Verification for your Google account.
2. Go to <https://myaccount.google.com/apppasswords>, create one named "USCIS bot", and copy the 16-character password.

### 2. Add repository secrets
Go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Secret | Value |
|---|---|
| `CASE_NUMBERS` | Your receipt number, e.g. `IOE0123456789` (separate several with commas) |
| `SMTP_USER` | Your Gmail address |
| `SMTP_PASSWORD` | The App Password from step 1 |
| `EMAIL_TO` | Where to send alerts (comma-separated). Add a text gateway address to get free texts, see below |

**Free texts:** add your phone's email-to-SMS address to `EMAIL_TO`:
- T-Mobile: `5551234567@tmomail.net`
- Verizon: `5551234567@vtext.com`
- AT&T: `5551234567@txt.att.net`

*(Carriers sometimes delay or filter these. For reliable texts, use Twilio below.)*

**Optional, Twilio texts:** `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_FROM` (your Twilio number, `+1...`), `SMS_TO` (your number, `+1...`).

**Optional, a mail server other than Gmail:** `SMTP_HOST`, `SMTP_PORT` (465 for SSL, 587 for STARTTLS).

### 3. Turn it on
1. Open the **Actions** tab and enable workflows if GitHub asks.
2. On a phone, tap the ◧ icon next to "All workflows" to see the list. Choose **USCIS case status monitor → Run workflow** to test it right away. You should get a "Current status" email.
3. After that it runs by itself every 30 minutes and only messages you when something changes.

## Run it locally instead
```bash
export CASE_NUMBERS=IOE0123456789 SMTP_USER=you@gmail.com SMTP_PASSWORD='app password' EMAIL_TO=you@gmail.com
python3 uscis_monitor.py
```
Use cron (`*/30 * * * * cd /path && python3 uscis_monitor.py`) to repeat it. It uses only the Python standard library.

## Notes
- GitHub can delay scheduled runs by a few minutes, and it pauses schedules in repos that have had no activity for 60 days. If that happens, re-enable the workflow in the Actions tab.
- USCIS sometimes blocks requests from cloud servers. If the Actions log shows errors on every run, run the script from your own computer with cron.
- USCIS also offers free official email/text updates if you create a USCIS online account at <https://my.uscis.gov>. It's worth turning on as a backup.


---

# DACA Watch

A daily email digest of new DACA developments. **Path to citizenship** news (Dream Act, green cards, legalization bills) is flagged and listed first.

**Sources** are all free and public, with no login:
- Federal Register: official USCIS/DHS rules and notices
- CourtListener: new court opinions mentioning DACA
- Google News: coverage of Congress, the courts, USCIS and politics

**Setup:** it uses the same email secrets as the USCIS bot (`SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`, plus `SMTP_HOST`/`SMTP_PORT` if you're not on Gmail). It runs every morning and only emails you when something new appears. The first email covers the past 2 weeks.

**Test it:** Actions → DACA watch → Run workflow. Tick "dry run" to see the digest in the log without sending an email.

Headlines are sorted by keywords, not read for meaning, so open the link before relying on anything. Talk to an immigration attorney or a DOJ-accredited representative before acting on any news.
