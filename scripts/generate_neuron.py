#!/usr/bin/env python3
"""Build neuron.json from the latest issue of The Neuron Daily newsletter.

Strategy:
  1) Fetch the RSS feed (https://www.theneurondaily.com/rss) for the last issue link.
  2) Fetch that issue's HTML.
  3) Extract: emoji + headline + bullets (with embedded links + emojis) + main story.
  4) Promote yesterday's data (from existing neuron.json) into a "previous" block.
  5) Write neuron.json.

Output schema (matches what myAI's renderTodayInAI / openTodayDetail expect):
  {
    "date": "2026-05-06",
    "headline": "...",
    "subtitle": "...",
    "emoji": "🤖",
    "intro": "...",
    "issue_url": "https://...",
    "bullets": [
      {"emoji": "🚀", "text": "...", "summary": "...", "url": "...", "source": "..."}, ...
    ],
    "main_story": {
      "title": "...", "body": "...", "why": "...", "take": "..."
    },
    "previous": { "date": "...", "bullets": [...] }
  }
"""
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparse

NEURON_RSS = "https://www.theneurondaily.com/rss"
HEADERS = {"User-Agent": "myai-bot/1.0 (+https://github.com/frankknebeljanssen-create/myAInews)"}

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_FILE  = REPO_ROOT / "neuron.json"

EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F1E6-\U0001F1FF"
    "]"
)


def latest_issue_url():
    print(f"Fetching feed: {NEURON_RSS}")
    feed = feedparser.parse(NEURON_RSS, agent=HEADERS["User-Agent"])
    if not feed.entries:
        raise RuntimeError("Neuron RSS empty")
    entry = feed.entries[0]
    pub = entry.get("published") or entry.get("updated") or ""
    iso = ""
    try:
        iso = dtparse.parse(pub).strftime("%Y-%m-%d") if pub else datetime.now(timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return entry.link, iso, entry.get("title", "")


def extract_emoji(text: str) -> str:
    if not text:
        return ""
    m = EMOJI_RE.search(text)
    return m.group(0) if m else ""


def parse_issue(url: str):
    print(f"Fetching issue: {url}")
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    article = soup.find("article") or soup

    # ── Headline ──
    h1 = article.find(["h1", "h2"])
    headline = h1.get_text(strip=True) if h1 else ""
    emoji = extract_emoji(headline)
    if emoji and headline.startswith(emoji):
        headline = headline[len(emoji):].strip()

    # ── Intro / subtitle: first paragraph after headline ──
    intro = ""
    if h1:
        nxt = h1.find_next("p")
        if nxt:
            intro = nxt.get_text(" ", strip=True)[:400]

    # ── Bullets: every <li> under the first <ul> with at least 3 items, OR the section
    # tagged "Here's what happened in AI today" / "In today's edition". ──
    bullets = []
    ul = None
    for candidate in article.find_all("ul"):
        if len(candidate.find_all("li")) >= 3:
            ul = candidate
            break
    if ul is None:
        # fallback: collect bullet-shaped paragraphs
        ul = article
    for li in ul.find_all("li"):
        txt = li.get_text(" ", strip=True)
        if not txt:
            continue
        b_emoji = extract_emoji(txt)
        text = txt
        if b_emoji and text.startswith(b_emoji):
            text = text[len(b_emoji):].strip()
        a = li.find("a")
        b_url = a["href"] if a and a.get("href") else ""
        bullets.append({
            "emoji": b_emoji or "•",
            "text": text[:240],
            "summary": text,
            "url": b_url,
            "source": "",
        })
        if len(bullets) >= 12:
            break

    # ── Main story: first long paragraph block ──
    main_title = ""
    main_body  = ""
    main_why   = ""
    main_take  = ""
    for h in article.find_all(["h2", "h3"]):
        t = h.get_text(strip=True)
        if any(k in t.lower() for k in ("why this matters", "warum")):
            sib = h.find_next("p")
            if sib:
                main_why = sib.get_text(" ", strip=True)[:600]
        elif any(k in t.lower() for k in ("our take", "einschätzung")):
            sib = h.find_next("p")
            if sib:
                main_take = sib.get_text(" ", strip=True)[:600]
        elif not main_title and t.lower() not in ("here's what happened in ai today",):
            main_title = t
            sib = h.find_next("p")
            if sib:
                main_body = sib.get_text(" ", strip=True)[:1200]

    return {
        "emoji": emoji,
        "headline": headline,
        "subtitle": intro,
        "intro": intro,
        "issue_url": url,
        "bullets": bullets,
        "main_story": {
            "title": main_title,
            "body":  main_body,
            "why":   main_why,
            "take":  main_take,
        },
    }


def load_previous():
    """Return current today's data so we can promote it to 'previous'."""
    if not OUT_FILE.exists():
        return None
    try:
        cur = json.loads(OUT_FILE.read_text())
        return {"date": cur.get("date", ""), "bullets": cur.get("bullets", [])}
    except Exception:
        return None


def main():
    url, date_iso, title = latest_issue_url()
    today = parse_issue(url)
    today["date"] = date_iso

    prev = load_previous()
    if prev and prev.get("date") and prev.get("date") != date_iso:
        today["previous"] = prev

    OUT_FILE.write_text(json.dumps(today, indent=2, ensure_ascii=False))
    print(f"Wrote {OUT_FILE}: date={date_iso}, bullets={len(today['bullets'])}, "
          f"previous={'yes' if today.get('previous') else 'no'}")


if __name__ == "__main__":
    main()
