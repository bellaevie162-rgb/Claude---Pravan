#!/usr/bin/env python3
"""DACA watch: a daily email digest of new DACA developments.

Sources (all free and public, no login):
  * Federal Register API   - official rules, notices and proposed rules
  * CourtListener API      - new court opinions that mention DACA
  * Google News RSS        - news coverage (Congress, courts, USCIS, politics)

Items about a possible path to citizenship (Dream Act, green cards, etc.)
are flagged and listed first. Only items not seen before are emailed.

Environment variables:
  SMTP_USER, SMTP_PASSWORD, EMAIL_TO   (same as the USCIS bot)
  SMTP_HOST (default smtp.gmail.com), SMTP_PORT (default 465)
  STATE_FILE        default daca_state.json
  LOOKBACK_DAYS     how far back to look (default 3; first run uses 14)
  COURTLISTENER_TOKEN  optional, raises CourtListener rate limits
  DRY_RUN=1         print the digest instead of emailing it
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
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from html import unescape

UA = "daca-watch/1.0 (personal news digest)"

# What counts as "path to citizenship" news.
CITIZENSHIP_RE = re.compile(
    r"citizenship|green card|permanent resid|lawful permanent|dream act|"
    r"american dream and promise|dignity act|pathway|path to legal|legaliz|"
    r"adjust(ment of)? status|registry date",
    re.I,
)
COURT_RE = re.compile(r"court|judge|ruling|ruled|lawsuit|appeal|texas v\.|injunction|5th circuit|fifth circuit", re.I)
CONGRESS_RE = re.compile(r"congress|senate|house|bill|legislat|lawmaker|senator|representative", re.I)
AGENCY_RE = re.compile(r"uscis|dhs|homeland security|renewal|work permit|federal register|rule\b|regulation", re.I)

# A news headline must match this to be included (filters off-topic stories).
RELEVANT_RE = re.compile(
    r"\bDACA\b|dreamer|dream act|deferred action|childhood arrival|amnesty|"
    r"legal status|citizenship|legaliz|undocumented (students|youth|immigrants brought)",
    re.I,
)

NEWS_QUERIES = [
    '"DACA"',
    '"Deferred Action for Childhood Arrivals"',
    '"Dream Act" OR "Dreamers" citizenship',
]


def fetch(url: str, headers: dict | None = None, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", unescape(text)).strip()


# ---------------------------------------------------------------- sources

def federal_register(since: date) -> list[dict]:
    items = {}
    for term in ('"Deferred Action for Childhood Arrivals"', "DACA"):
        params = [
            ("conditions[term]", term),
            ("conditions[publication_date][gte]", since.isoformat()),
            ("order", "newest"),
            ("per_page", "20"),
        ] + [("fields[]", f) for f in ("title", "html_url", "publication_date", "type", "abstract", "document_number")]
        data = json.loads(fetch("https://www.federalregister.gov/api/v1/documents.json?" + urllib.parse.urlencode(params)))
        for d in data.get("results") or []:
            items[d["document_number"]] = {
                "id": "fr:" + d["document_number"],
                "source": "Federal Register",
                "kind": "rules",
                "title": f'{d.get("type", "Document")}: {d["title"]}',
                "url": d["html_url"],
                "date": d.get("publication_date", ""),
                "summary": clean(d.get("abstract") or "")[:400],
            }
    return list(items.values())


def courtlistener(since: date) -> list[dict]:
    headers = {}
    if os.getenv("COURTLISTENER_TOKEN"):
        headers["Authorization"] = "Token " + os.environ["COURTLISTENER_TOKEN"]
    params = {
        "q": '"Deferred Action for Childhood Arrivals" OR "DACA"',
        "type": "o",
        "order_by": "dateFiled desc",
        "filed_after": since.strftime("%m/%d/%Y"),
    }
    data = json.loads(fetch("https://www.courtlistener.com/api/rest/v4/search/?" + urllib.parse.urlencode(params), headers))
    out = []
    for r in (data.get("results") or [])[:15]:
        out.append({
            "id": f'cl:{r.get("cluster_id") or r.get("absolute_url")}',
            "source": f'CourtListener · {r.get("court", "")}'.strip(" ·"),
            "kind": "courts",
            "title": r.get("caseName") or "Court opinion",
            "url": "https://www.courtlistener.com" + (r.get("absolute_url") or ""),
            "date": (r.get("dateFiled") or "")[:10],
            "summary": clean(" ".join(o.get("snippet", "") for o in r.get("opinions", [])[:1]))[:400],
        })
    return out


def google_news(days: int) -> list[dict]:
    out = {}
    for q in NEWS_QUERIES:
        url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
            {"q": f"{q} when:{days}d", "hl": "en-US", "gl": "US", "ceid": "US:en"}
        )
        root = ET.fromstring(fetch(url))
        for it in root.iter("item"):
            title = clean(it.findtext("title"))
            if not RELEVANT_RE.search(title):
                continue
            # Same story often appears under several queries; dedupe on headline.
            key = re.sub(r"[^a-z0-9]", "", title.lower().rsplit(" - ", 1)[0])[:80]
            if not key or key in out:
                continue
            try:
                d = parsedate_to_datetime(it.findtext("pubDate")).date().isoformat()
            except Exception:
                d = ""
            out[key] = {
                "id": "gn:" + key,
                "source": clean(it.findtext("source")) or "News",
                "kind": "news",
                "title": title,
                "url": it.findtext("link") or "",
                "date": d,
                "summary": "",
            }
    return list(out.values())


# ---------------------------------------------------------------- digest

def categorize(item: dict) -> str:
    text = f'{item["title"]} {item["summary"]}'
    if CITIZENSHIP_RE.search(text):
        return "citizenship"
    if item["kind"] in ("rules", "courts"):
        return item["kind"]
    if COURT_RE.search(text):
        return "courts"
    if CONGRESS_RE.search(text):
        return "congress"
    if AGENCY_RE.search(text):
        return "rules"
    return "news"


SECTIONS = [
    ("citizenship", "⭐ PATH TO CITIZENSHIP / GREEN CARD"),
    ("congress", "🏛  CONGRESS"),
    ("courts", "⚖  COURTS"),
    ("rules", "📜 USCIS / DHS / FEDERAL RULES"),
    ("news", "📰 OTHER NEWS"),
]


def build_digest(items: list[dict]) -> tuple[str, str]:
    groups: dict[str, list[dict]] = {k: [] for k, _ in SECTIONS}
    for it in items:
        groups[categorize(it)].append(it)
    n_cit = len(groups["citizenship"])
    subject = f"DACA watch: {len(items)} new update{'s' if len(items) != 1 else ''}"
    if n_cit:
        subject += f" ({n_cit} about a path to citizenship)"
    lines = [f"New DACA developments as of {date.today():%B %d, %Y}.", ""]
    for key, label in SECTIONS:
        group = sorted(groups[key], key=lambda x: x["date"], reverse=True)
        if not group:
            continue
        lines += [label, "-" * len(label)]
        for it in group:
            lines.append(f'• {it["title"]}')
            lines.append(f'  {it["source"]} · {it["date"]}')
            if it["summary"]:
                lines.append(f'  {it["summary"]}')
            lines.append(f'  {it["url"]}')
            lines.append("")
    lines += [
        "—",
        "Headlines are sorted by keywords, not read for meaning. Open the link before",
        "relying on anything here, and talk to an immigration attorney or a DOJ-accredited",
        "representative before acting on it.",
    ]
    return subject, "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    user, pw, to = os.getenv("SMTP_USER"), os.getenv("SMTP_PASSWORD"), os.getenv("EMAIL_TO")
    if not (user and pw and to):
        raise SystemExit("Missing SMTP_USER / SMTP_PASSWORD / EMAIL_TO")
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


def main() -> int:
    state_path = os.getenv("STATE_FILE", "daca_state.json")
    try:
        with open(state_path) as f:
            state = json.load(f)
    except (FileNotFoundError, ValueError):
        state = {}
    seen: dict[str, str] = state.get("seen", {})
    first_run = not seen
    days = 14 if first_run else int(os.getenv("LOOKBACK_DAYS") or "3")
    since = date.today() - timedelta(days=days)

    items, ok = [], 0
    for name, fn in (("Federal Register", lambda: federal_register(since)),
                     ("CourtListener", lambda: courtlistener(since)),
                     ("Google News", lambda: google_news(days))):
        try:
            got = fn()
            ok += 1
            print(f"{name}: {len(got)} items")
            items += got
        except Exception as e:
            print(f"[warn] {name} failed: {e}", file=sys.stderr)
    if ok == 0:
        print("All sources failed.", file=sys.stderr)
        return 1

    new = [it for it in items if it["id"] not in seen]
    counts = {}
    for it in new:
        counts[categorize(it)] = counts.get(categorize(it), 0) + 1
    print(f"{len(new)} new item(s): {counts}")
    if new:
        subject, body = build_digest(new)
        if first_run:
            subject += " — first run, past 2 weeks"
        if os.getenv("DRY_RUN") == "1":
            print(subject + "\n\n" + body)
            print(f"\n[summary] {len(new)} new item(s): {counts}")
        else:
            send_email(subject, body)
            print("Email sent.")

    today = date.today().isoformat()
    for it in new:
        seen[it["id"]] = today
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    state["seen"] = {k: v for k, v in seen.items() if v >= cutoff}
    state["last_run"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with open(state_path, "w") as f:
        json.dump(state, f, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
