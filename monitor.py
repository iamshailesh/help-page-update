"""
Log360 help-doc change monitor.

Crawls every page under the configured help-doc prefixes, extracts the main
content, compares it with the snapshot from the previous run, and emails a
summary with diffs of what changed, what's new, and what was removed.
"""

import difflib
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from collections import deque
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ---------- Configuration (override via environment variables) ----------
START_URLS = [u.strip() for u in os.getenv(
    "START_URLS",
    "https://www.manageengine.com/log-management/help/,"
    "https://www.manageengine.com/cloud-log-management/help/",
).split(",") if u.strip()]
MAX_PAGES = int(os.getenv("MAX_PAGES", "1500"))
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "1.0"))  # seconds between requests
MAX_DIFF_LINES = int(os.getenv("MAX_DIFF_LINES", "60"))   # per page, in the email
SEND_IF_NO_CHANGES = os.getenv("SEND_IF_NO_CHANGES", "true").lower() == "true"
# Pages whose "Last updated" date is on or after this date get listed.
# Format: YYYY-MM-DD, e.g. 2026-10-01. Leave empty to turn the date filter off.
SINCE_DATE = os.getenv("SINCE_DATE", "").strip()

DATA_DIR = Path(os.getenv("DATA_DIR", "snapshots"))
STATE_FILE = DATA_DIR / "state.json"

HEADERS = {"User-Agent": "Log360-HelpDoc-Monitor/1.0 (internal docs change tracker)"}

# Lines like "Last updated on September 12, 2025" are captured separately
# so a date bump alone doesn't count as a content change.
LAST_UPDATED_RE = re.compile(r"last\s+updated\s*(on)?\s*:?\s*(.*)", re.I)

DATE_FORMATS = ["%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%d %B %Y", "%d %b %Y",
                "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y"]
DATE_IN_TEXT_RE = re.compile(
    r"([A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{1,2} [A-Za-z]{3,9}\.? \d{4}|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{4})")


def parse_date(text: str):
    """Turn 'September 12, 2025' (and similar) into a date, or None."""
    m = DATE_IN_TEXT_RE.search(text or "")
    if not m:
        return None
    raw = m.group(1).replace(".", "")
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


session = requests.Session()
session.headers.update(HEADERS)


# ---------- Crawling ----------
def normalize(url: str) -> str:
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    path = parsed.path or "/"
    return f"{parsed.scheme}://{parsed.netloc}{path}"


def in_scope(url: str) -> bool:
    if not any(url.startswith(prefix) for prefix in START_URLS):
        return False
    path = urlparse(url).path.lower()
    # Skip assets; keep folders and .html pages
    return path.endswith("/") or path.endswith(".html") or "." not in path.rsplit("/", 1)[-1]


def fetch(url: str):
    try:
        resp = session.get(url, timeout=30)
        return resp.status_code, resp.text if resp.ok else ""
    except requests.RequestException as exc:
        print(f"  ! fetch failed {url}: {exc}")
        return None, ""


def extract(page_html: str):
    """Return (clean_text, last_updated, links) for a page."""
    soup = BeautifulSoup(page_html, "html.parser")

    links = [a["href"] for a in soup.find_all("a", href=True)]

    for tag in soup(["script", "style", "noscript", "nav", "header", "footer",
                     "form", "iframe", "svg"]):
        tag.decompose()

    main = (soup.select_one("main") or soup.select_one("article")
            or soup.select_one("#content") or soup.select_one(".content")
            or soup.body or soup)

    lines, last_updated, want_date = [], "", False
    for raw in main.get_text("\n").splitlines():
        line = " ".join(raw.split())
        if not line:
            continue
        # Date sitting on the line after a bare "Last updated on:" label
        if want_date:
            want_date = False
            if DATE_IN_TEXT_RE.search(line):
                last_updated = line
                continue
        m = LAST_UPDATED_RE.search(line)
        if m:
            if DATE_IN_TEXT_RE.search(m.group(2)):
                last_updated = m.group(2).strip()
            else:
                want_date = True  # label and date are in separate HTML elements
            continue
        lines.append(line)

    return "\n".join(lines), last_updated, links


def crawl():
    seen, pages = set(), {}
    queue = deque(normalize(u) for u in START_URLS)
    # Optional: extra known URLs (one per line) so pages no menu links to still get checked
    seed_file = Path("seed_urls.txt")
    if seed_file.exists():
        queue.extend(normalize(u.strip()) for u in seed_file.read_text().splitlines()
                     if u.strip() and not u.startswith("#"))

    while queue and len(pages) < MAX_PAGES:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)

        status, body = fetch(url)
        time.sleep(REQUEST_DELAY)
        if status != 200 or not body:
            continue

        text, last_updated, links = extract(body)
        pages[url] = {"text": text, "last_updated": last_updated}
        print(f"  ✓ {url}")

        for href in links:
            nxt = normalize(urljoin(url, href))
            if in_scope(nxt) and nxt not in seen:
                queue.append(nxt)

    if len(pages) >= MAX_PAGES:
        print(f"  ! hit MAX_PAGES={MAX_PAGES}; raise it if the docs have grown")
    return pages


# ---------- Snapshots ----------
def slug(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16] + ".txt"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state, pages):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for url, info in pages.items():
        (DATA_DIR / slug(url)).write_text(info["text"], encoding="utf-8")
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


# ---------- Comparison ----------
def compare(old_state, pages):
    changed, new, removed = [], [], []
    new_state = {}

    for url, info in pages.items():
        h = digest(info["text"])
        new_state[url] = {"hash": h, "last_updated": info["last_updated"]}
        prev = old_state.get(url)
        if prev is None:
            new.append(url)
        elif prev["hash"] != h:
            old_file = DATA_DIR / slug(url)
            old_text = old_file.read_text(encoding="utf-8") if old_file.exists() else ""
            diff = list(difflib.unified_diff(
                old_text.splitlines(), info["text"].splitlines(),
                lineterm="", n=1))[2:]  # drop ---/+++ headers
            changed.append({
                "url": url,
                "diff": diff,
                "old_date": prev.get("last_updated", ""),
                "new_date": info["last_updated"],
            })

    # Pages from last run that weren't reached this time: confirm before calling them removed
    for url, prev in old_state.items():
        if url in pages:
            continue
        status, _ = fetch(url)
        time.sleep(REQUEST_DELAY)
        if status in (404, 410):
            removed.append(url)
        else:
            new_state[url] = prev  # still exists (just unlinked or a temporary error)

    return changed, new, removed, new_state


# ---------- Date filter ----------
def filter_by_date(pages):
    """Return (recent, undated): pages updated on/after SINCE_DATE, and pages with no readable date."""
    if not SINCE_DATE:
        return None, []
    since = datetime.strptime(SINCE_DATE, "%Y-%m-%d").date()
    recent, undated = [], []
    for url, info in pages.items():
        d = parse_date(info["last_updated"])
        if d is None:
            undated.append(url)
        elif d >= since:
            recent.append((d, url))
    recent.sort(reverse=True)  # newest first
    return recent, undated


# ---------- Email ----------
def build_email(changed, new, removed, total, baseline, recent=None, undated=()):
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    esc = html.escape

    if recent is not None:
        subject = (f"[Log360 Help Docs] {len(recent)} pages updated since "
                   f"{SINCE_DATE} ({today})")
    elif baseline:
        subject = f"[Log360 Help Docs] Baseline created: {total} pages tracked"
    elif changed or new or removed:
        subject = (f"[Log360 Help Docs] {len(changed)} changed, "
                   f"{len(new)} new, {len(removed)} removed ({today})")
    else:
        subject = f"[Log360 Help Docs] No changes this week ({today})"

    parts = [f"<h2>Log360 help-doc weekly check: {today}</h2>",
             f"<p>{total} pages checked.</p>"]

    if recent is not None:
        since_txt = datetime.strptime(SINCE_DATE, "%Y-%m-%d").strftime("%d %b %Y")
        parts.append(f"<h3>Pages updated on or after {since_txt} ({len(recent)})</h3>")
        if recent:
            parts.append('<table cellpadding="4" style="border-collapse:collapse">')
            for d, u in recent:
                parts.append(f'<tr><td style="white-space:nowrap">{d.strftime("%d %b %Y")}</td>'
                             f'<td><a href="{esc(u)}">{esc(u)}</a></td></tr>')
            parts.append("</table>")
        else:
            parts.append("<p>No pages updated since that date.</p>")
        if undated:
            parts.append(f"<p><i>{len(undated)} pages had no readable 'Last updated' date "
                         "and were skipped by this filter.</i></p>")
        parts.append("<hr><h3>Content changes since last week's run</h3>")

    if baseline:
        parts.append("<p>This was the first run, so it saved a snapshot of every page. "
                     "From next week you'll get the changes.</p>")
    else:
        if changed:
            parts.append(f"<h3>Changed ({len(changed)})</h3>")
            for c in changed:
                date_note = ""
                if c["new_date"] and c["new_date"] != c["old_date"]:
                    date_note = f" <i>(last updated: {esc(c['old_date'] or '?')} → {esc(c['new_date'])})</i>"
                parts.append(f'<p><a href="{esc(c["url"])}">{esc(c["url"])}</a>{date_note}</p>')
                rows = []
                for line in c["diff"][:MAX_DIFF_LINES]:
                    if line.startswith("+"):
                        rows.append(f'<div style="background:#e6ffec">{esc(line)}</div>')
                    elif line.startswith("-"):
                        rows.append(f'<div style="background:#ffebe9">{esc(line)}</div>')
                    elif line.startswith("@@"):
                        rows.append(f'<div style="color:#888">…</div>')
                    else:
                        rows.append(f"<div>{esc(line)}</div>")
                if len(c["diff"]) > MAX_DIFF_LINES:
                    rows.append(f"<div><i>…{len(c['diff']) - MAX_DIFF_LINES} more diff lines</i></div>")
                parts.append('<div style="font-family:monospace;font-size:12px;'
                             'border:1px solid #ddd;padding:6px;margin-bottom:14px">'
                             + "".join(rows) + "</div>")
        if new:
            parts.append(f"<h3>New pages ({len(new)})</h3><ul>")
            parts += [f'<li><a href="{esc(u)}">{esc(u)}</a></li>' for u in new]
            parts.append("</ul>")
        if removed:
            parts.append(f"<h3>Removed pages ({len(removed)})</h3><ul>")
            parts += [f"<li>{esc(u)}</li>" for u in removed]
            parts.append("</ul>")
        if not (changed or new or removed):
            parts.append("<p>No content changes since last week.</p>")

    return subject, "\n".join(parts)


def send_email(subject, body_html):
    host = os.environ["SMTP_HOST"]
    port = int(os.getenv("SMTP_PORT", "465"))
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASS"]
    to_addrs = [a.strip() for a in os.environ["MAIL_TO"].split(",") if a.strip()]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = os.getenv("MAIL_FROM", user)
    msg["To"] = ", ".join(to_addrs)
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    if port == 465:
        with smtplib.SMTP_SSL(host, port) as s:
            s.login(user, password)
            s.sendmail(msg["From"], to_addrs, msg.as_string())
    else:
        with smtplib.SMTP(host, port) as s:
            s.starttls()
            s.login(user, password)
            s.sendmail(msg["From"], to_addrs, msg.as_string())


# ---------- Zoho Connect ----------
MAX_LINES_PER_POST = int(os.getenv("CONNECT_MAX_LINES", "40"))


def page_label(url: str) -> str:
    """'…/cloud-log-management/help/ueba.html' -> 'Cloud · ueba.html'"""
    edition = "Cloud" if "/cloud-log-management/" in url else "On-prem"
    name = url.split("/help/", 1)[-1] or "Help home"
    return f"{edition} · {name}"


def connect_link(url: str) -> str:
    return f"[{page_label(url)}]({url})"


def build_connect_posts(changed, new, removed, total, baseline, recent=None, undated=()):
    """Return a list of (title, message) posts. Long lists are split across posts."""
    today = datetime.now(timezone.utc).strftime("%d %b %Y")
    lines = []

    if recent is not None:
        since_txt = datetime.strptime(SINCE_DATE, "%Y-%m-%d").strftime("%d %b %Y")
        title = f"Log360 help docs: {len(recent)} pages updated since {since_txt}"
        if recent:
            lines += [f"{d.strftime('%d %b %Y')} – {connect_link(u)}" for d, u in recent]
        else:
            lines.append("No pages updated since that date.")
        if undated:
            lines.append(f"_{len(undated)} pages had no readable 'Last updated' date._")
    elif baseline:
        title = f"Log360 help docs: baseline created ({total} pages)"
    else:
        title = f"Log360 help docs: weekly check ({today})"

    if not baseline and (changed or new or removed):
        lines.append("")
        lines.append("*Content changes since last week*")
        lines += [f"✏️ {connect_link(c['url'])}" for c in changed]
        lines += [f"🆕 {connect_link(u)}" for u in new]
        lines += [f"🗑️ {page_label(u)}" for u in removed]
    elif baseline:
        lines.append(f"First run: saved a snapshot of {total} pages. Changes will show from next week.")

    lines.append("")
    lines.append(f"_{total} pages checked on {today}._")

    posts = []
    chunks = [lines[i:i + MAX_LINES_PER_POST] for i in range(0, len(lines), MAX_LINES_PER_POST)]
    for i, chunk in enumerate(chunks, 1):
        t = title if len(chunks) == 1 else f"{title} ({i}/{len(chunks)})"
        posts.append((t, "<br>".join(chunk)))
    return posts


def post_to_connect(posts):
    url = os.environ["CONNECT_WEBHOOK_URL"]
    for title, message in posts:
        payload = json.dumps({"title": title, "message": message}, ensure_ascii=False)
        resp = requests.post(url, data={"payload": payload}, timeout=30)  # form-urlencoded, as Connect requires
        if not resp.ok or '"failure"' in resp.text:
            raise RuntimeError(f"Zoho Connect post failed ({resp.status_code}): {resp.text[:300]}")
        print(f"Posted to Zoho Connect: {title}")
        time.sleep(1)


# ---------- Main ----------
def main():
    print("Crawling help docs…")
    pages = crawl()
    if not pages:
        print("No pages fetched; site may be blocking requests. Aborting without touching snapshots.")
        sys.exit(1)

    old_state = load_state()
    baseline = not old_state
    changed, new, removed, new_state = compare(old_state, pages)
    save_state(new_state, pages)

    print(f"Pages: {len(pages)} | changed {len(changed)} | new {len(new)} | removed {len(removed)}")

    recent, undated = filter_by_date(pages)
    if recent is not None:
        print(f"Updated on/after {SINCE_DATE}: {len(recent)} | no date found: {len(undated)}")

    if not (recent or baseline or changed or new or removed or SEND_IF_NO_CHANGES):
        return
    dry_run = os.getenv("DRY_RUN", "false").lower() == "true"

    # Zoho Connect (used when CONNECT_WEBHOOK_URL is set)
    if os.getenv("CONNECT_WEBHOOK_URL") or dry_run:
        posts = build_connect_posts(changed, new, removed, len(pages), baseline, recent, undated)
        if dry_run:
            Path("connect_preview.txt").write_text(
                "\n\n".join(f"{t}\n{m}" for t, m in posts), encoding="utf-8")
            print(f"DRY_RUN: would post {len(posts)} message(s) to Connect (connect_preview.txt)")
        else:
            post_to_connect(posts)

    # Email (optional: used only when SMTP_HOST is set)
    if os.getenv("SMTP_HOST") or dry_run:
        subject, body = build_email(changed, new, removed, len(pages), baseline, recent, undated)
        if dry_run:
            Path("email_preview.html").write_text(body, encoding="utf-8")
            print(f"DRY_RUN: would send '{subject}' (preview in email_preview.html)")
        else:
            send_email(subject, body)
            print(f"Email sent: {subject}")


if __name__ == "__main__":
    main()
