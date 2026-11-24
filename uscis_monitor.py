#!/usr/bin/env python3
"""USCIS case status monitor.

Checks a USCIS receipt number on egov.uscis.gov and sends email / SMS
notifications when the status changes. Configuration comes from environment
variables so no personal data has to live in the repository:

  CASE_NUMBERS     Comma-separated receipt numbers (e.g. IOE0123456789)
  STATE_FILE       Where the last-seen status is stored (default: state.json)

  Email (any SMTP server; Gmail shown as default):
  SMTP_HOST        default smtp.gmail.com
  SMTP_PORT        default 465 (SSL). Use 587 for STARTTLS.
  SMTP_USER        login, e.g. you@gmail.com
  SMTP_PASSWORD    password / Gmail App Password
  EMAIL_TO         comma-separated recipients. Carrier SMS gateways
                   (e.g. 5551234567@tmomail.net) work here for free texts.

  Text via Twilio (optional):
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM, SMS_TO

  NOTIFY_ON_FIRST_RUN  "1" to send a message on the very first check (default 1)
  ALWAYS_NOTIFY        "1" to send a message on every check, changed or not
"""

from __future__ import annotations

import base64
import html
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.message import EmailMessage

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
API_AUTH = "https://egov.uscis.gov/csol-api/ui-auth"
API_STATUS = "https://egov.uscis.gov/csol-api/case-statuses/{}"
LEGACY_FORM = "https://egov.uscis.gov/casestatus/mycasestatus.do"
PUBLIC_LINK = "https://egov.uscis.gov/casestatus/landing.do"

RECEIPT_RE = re.compile(r"^[A-Z]{3}\d{10}$")


class CheckError(Exception):
    pass


def _request(url, data=None, headers=None, timeout=30):
    hdrs = {"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=data, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def check_via_api(receipt: str) -> dict:
    """The JSON API used by the current USCIS Case Status Online page."""
    _, body = _request(API_AUTH)
    token = json.loads(body).get("JwtResponse", {}).get("accessToken")
    if not token:
        raise CheckError("no access token from ui-auth")
    _, body = _request(API_STATUS.format(receipt), headers={"Authorization": f"Bearer {token}"})
    data = json.loads(body)
    info = data.get("CaseStatusResponse") or {}
    detail = info.get("detailsEng") or {}
    title = detail.get("actionCodeText") or ""
    text = _strip_tags(detail.get("actionCodeDesc") or "")
    if not title:
        msg = info.get("errorMessage") or data.get("message") or body[:300]
        raise CheckError(f"API returned no status: {msg}")
    return {
        "title": title.strip(),
        "details": text,
        "form": (detail.get("formNum") or "").strip(),
        "form_title": (detail.get("formTitle") or "").strip(),
    }


def check_via_legacy_form(receipt: str) -> dict:
    """Older HTML form endpoint, kept as a fallback."""
    payload = urllib.parse.urlencode(
        {"changeLocale": "", "appReceiptNum": receipt, "initCaseSearch": "CHECK STATUS"}
    ).encode()
    _, page = _request(
        LEGACY_FORM, data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    m = re.search(r'<div class="rows text-center">\s*<h1>(.*?)</h1>\s*<p>(.*?)</p>', page, re.S)
    if not m:
        err = re.search(r'id="formErrorMessages".*?<li>(.*?)</li>', page, re.S)
        raise CheckError(_strip_tags(err.group(1)) if err else "could not parse status page")
    return {"title": _strip_tags(m.group(1)), "details": _strip_tags(m.group(2)), "form": "", "form_title": ""}


def check_status(receipt: str, retries: int = 3) -> dict:
    errors = []
    for attempt in range(retries):
        for fn in (check_via_api, check_via_legacy_form):
            try:
                return fn(receipt)
            except (CheckError, urllib.error.URLError, ValueError, TimeoutError) as e:
                errors.append(f"{fn.__name__}: {e}")
        time.sleep(5 * (attempt + 1))
    raise CheckError("; ".join(errors[-4:]))


# ---------------------------------------------------------------- notifications

def send_email(subject: str, body: str) -> bool:
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"), os.getenv("EMAIL_TO")
    if not (user and pw and to):
        return False
    host = os.getenv("SMTP_HOST") or "smtp.gmail.com"
    port = int(os.getenv("SMTP_PORT") or "465")
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, user
    msg["To"] = ", ".join(a.strip() for a in to.split(",") if a.strip())
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pw)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(user, pw)
            s.send_message(msg)
    return True


def send_twilio_sms(text: str) -> bool:
    sid, tok = os.getenv("TWILIO_ACCOUNT_SID"), os.getenv("TWILIO_AUTH_TOKEN")
    frm, to = os.getenv("TWILIO_FROM"), os.getenv("SMS_TO")
    if not (sid and tok and frm and to):
        return False
    auth = base64.b64encode(f"{sid}:{tok}".encode()).decode()
    for number in (n.strip() for n in to.split(",") if n.strip()):
        data = urllib.parse.urlencode({"From": frm, "To": number, "Body": text[:1500]}).encode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json",
            data=data, headers={"Authorization": f"Basic {auth}"},
        )
        urllib.request.urlopen(req, timeout=30).read()
    return True


def notify(subject: str, body: str, short: str) -> None:
    sent = []
    for name, fn in (("email", lambda: send_email(subject, body)), ("sms", lambda: send_twilio_sms(short))):
        try:
            if fn():
                sent.append(name)
        except Exception as e:  # keep going if one channel fails
            print(f"[warn] {name} notification failed: {e}", file=sys.stderr)
    print(f"Notified via: {', '.join(sent) or 'nothing configured'}")


# ---------------------------------------------------------------- main

def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def mask(receipt: str) -> str:
    return receipt[:3] + "*" * 6 + receipt[-4:]


def main() -> int:
    receipts = [r.strip().upper() for r in os.getenv("CASE_NUMBERS", "").split(",") if r.strip()]
    if not receipts:
        print("Set CASE_NUMBERS (e.g. CASE_NUMBERS=IOE0123456789)", file=sys.stderr)
        return 2
    state_path = os.getenv("STATE_FILE", "state.json")
    first_notify = os.getenv("NOTIFY_ON_FIRST_RUN", "1") == "1"
    always = os.getenv("ALWAYS_NOTIFY", "0") == "1"
    state = load_state(state_path)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    failures = 0

    for r in receipts:
        if not RECEIPT_RE.match(r):
            print(f"[warn] {mask(r)} doesn't look like a receipt number (3 letters + 10 digits)")
        try:
            cur = check_status(r)
        except CheckError as e:
            failures += 1
            print(f"[error] {mask(r)}: {e}", file=sys.stderr)
            continue

        prev = state.get(r)
        changed = prev is not None and (prev.get("title"), prev.get("details")) != (cur["title"], cur["details"])
        is_first = prev is None
        print(f"{mask(r)}: {cur['title']} ({'CHANGED' if changed else 'first check' if is_first else 'no change'})")

        if changed or always or (is_first and first_notify):
            tag = "CHANGED" if changed else "Current status"
            subject = f"USCIS {tag}: {cur['title']} ({r})"
            lines = [f"Receipt: {r}", f"Checked: {now}", ""]
            if cur.get("form"):
                lines.append(f"Form: {cur['form']} {cur.get('form_title', '')}".strip())
            if changed:
                lines.append(f"Previous status: {prev.get('title')}")
            lines += [f"Status: {cur['title']}", "", cur["details"], "", f"Check it yourself: {PUBLIC_LINK}"]
            notify(subject, "\n".join(lines), f"USCIS {r}: {cur['title']}")

        cur["checked_at"] = now
        cur["since"] = prev.get("since", now) if prev and not changed else now
        state[r] = cur

    save_state(state_path, state)
    return 1 if failures == len(receipts) else 0


if __name__ == "__main__":
    sys.exit(main())
