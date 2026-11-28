#!/usr/bin/env python3
"""Bill watch: email alerts when a bill in Congress changes.

Uses the official Congress.gov API (free key from https://api.congress.gov/sign-up/;
falls back to the shared DEMO_KEY, which has low rate limits) and reports:
  * new actions (introduced, referred to committee, hearings, votes, passed, signed...)
  * cosponsors added or withdrawn
  * new or changed bill text versions (the bill being amended/rewritten)
  * new amendments, new official summaries, title changes
Optionally adds recent news headlines that mention the bill.

Environment variables:
  BILLS            comma-separated, format congress/type/number, e.g. "119/hr/4393"
  BILL_NEWS_QUERY  optional Google News query, e.g. '"Dignity Act"'
  CONGRESS_API_KEY optional (DEMO_KEY used if missing)
  SMTP_USER, SMTP_PASSWORD, EMAIL_TO, SMTP_HOST, SMTP_PORT  (same as other bots)
  STATE_FILE       default bill_state.json
  DRY_RUN=1        print instead of emailing
"""

from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html import unescape

API = "https://api.congress.gov/v3"
UA = "bill-watch/1.0 (personal alert)"
TYPE_NAMES = {"hr": "H.R.", "s": "S.", "hjres": "H.J.Res.", "sjres": "S.J.Res.", "hres": "H.Res.", "sres": "S.Res."}
TYPE_URL = {"hr": "house-bill", "s": "senate-bill", "hjres": "house-joint-resolution",
            "sjres": "senate-joint-resolution", "hres": "house-resolution", "sres": "senate-resolution"}


def ordinal(n: int) -> str:
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def api_get(path: str, **params) -> dict:
    params = {"format": "json", "limit": 250, "api_key": os.getenv("CONGRESS_API_KEY") or "DEMO_KEY", **params}
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def snapshot(congress: str, btype: str, number: str) -> dict:
    base = f"/bill/{congress}/{btype}/{number}"
    bill = api_get(base)["bill"]
    actions = api_get(base + "/actions").get("actions", [])
    cosponsors = api_get(base + "/cosponsors").get("cosponsors", [])
    texts = api_get(base + "/text").get("textVersions", [])
    amendments = api_get(base + "/amendments").get("amendments", []) if bill.get("amendments") else []
    summaries = api_get(base + "/summaries").get("summaries", []) if bill.get("summaries") else []
    sponsor = (bill.get("sponsors") or [{}])[0]
    return {
        "label": f"{TYPE_NAMES.get(btype, btype.upper())} {number} ({ordinal(int(congress))} Congress)",
        "url": f"https://www.congress.gov/bill/{ordinal(int(congress))}-congress/{TYPE_URL.get(btype, btype)}/{number}",
        "title": bill.get("title", ""),
        "sponsor": sponsor.get("fullName", ""),
        "introduced": bill.get("introducedDate", ""),
        "latest_action": bill.get("latestAction") or {},
        "actions": [f'{a.get("actionDate", "")} | {a.get("text", "").strip()}' for a in actions],
        "cosponsors": {c.get("bioguideId", c.get("fullName")): c.get("fullName", "") for c in cosponsors
                       if not c.get("sponsorshipWithdrawnDate")},
        "texts": [f'{t.get("date") or ""} | {t.get("type", "")}' for t in texts],
        "amendments": [f'{a.get("type", "")} {a.get("number", "")}: {(a.get("description") or a.get("purpose") or "").strip()}'
                       for a in amendments],
        "summaries": [f'{s.get("actionDate", "")} | {s.get("actionDesc", "")}' for s in summaries],
    }


def party_counts(cos: dict) -> str:
    # fullName looks like "Rep. Smith, Jane [R-FL-26]"
    parties = [m.group(1) for n in cos.values() if (m := re.search(r"\[([A-Z])-", n))]
    if not parties:
        return f"{len(cos)}"
    bits = [f"{parties.count(p)} {p}" for p in sorted(set(parties))]
    return f"{len(cos)} ({', '.join(bits)})"


def diff(old: dict, new: dict) -> list[str]:
    out = []
    if old.get("title") != new["title"]:
        out.append(f'TITLE CHANGED\n  from: {old.get("title")}\n  to:   {new["title"]}')
    added = [a for a in new["actions"] if a not in set(old.get("actions", []))]
    if added:
        out.append("NEW ACTIONS\n" + "\n".join(f"  • {a}" for a in added))
    oc, nc = old.get("cosponsors", {}), new["cosponsors"]
    joined = [nc[k] for k in nc if k not in oc]
    left = [oc[k] for k in oc if k not in nc]
    if joined or left:
        lines = [f"COSPONSORS: now {party_counts(nc)}"]
        lines += [f"  + {n}" for n in joined] + [f"  - {n} (withdrew)" for n in left]
        out.append("\n".join(lines))
    for key, label in (("texts", "NEW BILL TEXT VERSION"), ("amendments", "NEW AMENDMENTS"),
                       ("summaries", "NEW OFFICIAL SUMMARY")):
        added = [x for x in new[key] if x not in set(old.get(key, []))]
        if added:
            out.append(label + "\n" + "\n".join(f"  • {x}" for x in added))
    return out


def status_block(s: dict) -> str:
    la = s["latest_action"]
    return (f'{s["label"]}: {s["title"]}\n'
            f'Sponsor: {s["sponsor"]} · Introduced {s["introduced"]}\n'
            f'Cosponsors: {party_counts(s["cosponsors"])}\n'
            f'Latest action: {la.get("actionDate", "")} {la.get("text", "")}\n'
            f'Text versions: {len(s["texts"])} · Amendments: {len(s["amendments"])}\n'
            f'{s["url"]}')


def news(query: str, seen: set) -> list[str]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:3d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    root = ET.fromstring(urllib.request.urlopen(req, timeout=30).read())
    out = []
    for it in root.iter("item"):
        title = re.sub(r"\s+", " ", unescape(it.findtext("title") or "")).strip()
        key = re.sub(r"[^a-z0-9]", "", title.lower())[:80]
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            d = parsedate_to_datetime(it.findtext("pubDate")).date().isoformat()
        except Exception:
            d = ""
        out.append(f"  • {title} ({d})\n    {it.findtext('link')}")
    return out


def send_email(subject: str, body: str) -> None:
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"), os.getenv("EMAIL_TO")
    if not (user and pw and to):
        raise SystemExit("Missing SMTP_USER / SMTP_PASSWORD / EMAIL_TO")
    host, port = os.getenv("SMTP_HOST") or "smtp.gmail.com", int(os.getenv("SMTP_PORT") or "465")
    msg = EmailMessage()
    msg["Subject"], msg["From"] = subject, user
    msg["To"] = ", ".join(a.strip() for a in to.split(",") if a.strip())
    msg.set_content(body)
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(user, pw); s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx); s.login(user, pw); s.send_message(msg)


def main() -> int:
    bills = [b.strip().lower() for b in (os.getenv("BILLS") or "").split(",") if b.strip()]
    if not bills:
        print("Set BILLS, e.g. 119/hr/4393", file=sys.stderr)
        return 2
    path = os.getenv("STATE_FILE", "bill_state.json")
    try:
        state = json.load(open(path))
    except (FileNotFoundError, ValueError):
        state = {}
    sections, subjects, errors = [], [], 0

    for b in bills:
        try:
            congress, btype, number = b.split("/")
            cur = snapshot(congress, btype, number)
        except Exception as e:
            errors += 1
            print(f"[error] {b}: {e}", file=sys.stderr)
            continue
        prev = state.get(b)
        print(f"{cur['label']}: {cur['title']} | latest: {cur['latest_action']}")
        if prev is None:
            sections.append("NOW WATCHING\n" + status_block(cur))
            subjects.append(f"now watching {cur['label'].split(' (')[0]}")
        else:
            changes = diff(prev, cur)
            if changes:
                sections.append("\n\n".join(changes) + "\n\nCURRENT STATUS\n" + status_block(cur))
                subjects.append(f"{cur['label'].split(' (')[0]} changed")
        state[b] = cur

    query = os.getenv("BILL_NEWS_QUERY")
    if query:
        seen = set(state.get("news_seen", []))
        try:
            items = news(query, seen)
            if items and sections:  # only attach news to a bill-change email, or...
                sections.append("RECENT NEWS\n" + "\n".join(items))
            elif items:              # ...send a news-only email, kept clearly separate
                sections.append(f"NEWS MENTIONING {query} (no change to the bill itself)\n" + "\n".join(items))
                subjects.append("news")
        except Exception as e:
            print(f"[warn] news failed: {e}", file=sys.stderr)
        state["news_seen"] = list(seen)[-500:]

    if errors == len(bills):
        return 1
    if sections:
        subject = "Bill watch: " + ", ".join(subjects)
        body = (f"Update as of {date.today():%B %d, %Y}\n\n" + ("\n\n" + "=" * 40 + "\n\n").join(sections)
                + "\n\n—\nSource: Congress.gov (official). Bill changes are exact; news is headline-matched.")
        if os.getenv("DRY_RUN") == "1":
            print(subject + "\n\n" + body)
        else:
            send_email(subject, body)
            print("Email sent.")
    else:
        print("No changes.")
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    json.dump(state, open(path, "w"), indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
