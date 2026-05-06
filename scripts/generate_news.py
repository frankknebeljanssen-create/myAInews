#!/usr/bin/env python3
"""Aggregate AI news from a list of RSS sources, write news.json.

Output schema (matches what myAI's renderTicker / renderNewsScreen expect):
  {
    "updated": "2026-05-06T05:00:00Z",
    "items": [
      {
        "id": "<sha1 of url>",
        "source": "Anthropic",
        "headline": "...",
        "body": "## Section\n...",
        "url": "https://...",
        "published": "2026-05-06T08:30:00Z",
        "pinned": false
      },
      ...
    ]
  }
"""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import feedparser
from dateutil import parser as dtparse

# ----- SOURCES (label, RSS URL) -----
# Add or remove freely. The "label" appears as the source pill in the news ticker.
SOURCES = [
    ("Anthropic",       "https://www.anthropic.com/news/rss.xml"),
    ("OpenAI",          "https://openai.com/blog/rss/"),
    ("Google AI",       "https://blog.google/technology/ai/rss/"),
    ("DeepMind",        "https://deepmind.google/blog/rss.xml"),
    ("MIT Tech Review", "https://www.technologyreview.com/topic/artificial-intelligence/feed"),
    ("VentureBeat AI",  "https://venturebeat.com/category/ai/feed/"),
    ("TechCrunch AI",   "https://techcrunch.com/category/artificial-intelligence/feed/"),
    ("Import AI",       "https://importai.substack.com/feed"),
    ("The Neuron",      "https://www.theneurondaily.com/rss"),
    ("Mistral",         "https://mistral.ai/news/rss.xml"),
    ("Meta AI",         "https://ai.meta.com/blog/rss/"),
    ("Hugging Face",    "https://huggingface.co/blog/feed.xml"),
    ("AI Index",        "https://aiindex.stanford.edu/feed/"),
]

# Filter: keep only entries that look AI-related (some general feeds need this).
AI_KEYWORDS = re.compile(
    r'\b(ai|artificial intelligence|machine learning|llm|gpt|claude|gemini|grok|'
    r'openai|anthropic|deepmind|mistral|deepseek|huggingface|hugging face|'
    r'transformer|agent|agents|agentic|rag|fine[- ]?tune|prompt engineering|'
    r'neural net|generative|diffusion|stable diffusion|midjourney|chatbot)\b',
    re.IGNORECASE,
)

MAX_ITEMS_TOTAL    = 30   # how many news items to keep
MAX_ITEMS_PER_FEED = 6    # cap per source so one outlet doesn't dominate

REPO_ROOT  = Path(__file__).resolve().parent.parent
OUT_FILE   = REPO_ROOT / "news.json"


def make_id(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16]


def parse_published(entry) -> datetime:
    for key in ("published", "updated", "created"):
        v = entry.get(key)
        if v:
            try:
                dt = dtparse.parse(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                pass
    return datetime.now(timezone.utc)


def clean_summary(html: str) -> str:
    """Strip HTML tags from the RSS summary, keep paragraph breaks as \\n."""
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:1200]


def fetch_feed(label: str, url: str):
    print(f"  [{label}] fetching {url}")
    try:
        feed = feedparser.parse(url, agent="myai-bot/1.0 (+https://github.com/frankknebeljanssen-create/myAInews)")
    except Exception as e:
        print(f"    error: {e}")
        return []
    items = []
    for entry in feed.entries[:MAX_ITEMS_PER_FEED]:
        title = (entry.get("title") or "").strip()
        link  = (entry.get("link")  or "").strip()
        summary = clean_summary(entry.get("summary", "") or entry.get("description", ""))
        if not title or not link:
            continue
        if not AI_KEYWORDS.search(title + " " + summary):
            # keep entries from sources that are 100% AI-focused regardless of keyword
            if label not in {"Anthropic", "OpenAI", "Google AI", "DeepMind", "Mistral",
                             "Meta AI", "Hugging Face", "Import AI", "The Neuron"}:
                continue
        published = parse_published(entry)
        items.append({
            "id": make_id(link),
            "source": label,
            "headline": title,
            "body": summary,
            "url": link,
            "published": published.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "pinned": False,
        })
    return items


def main():
    print(f"Generating {OUT_FILE} ...")
    all_items = []
    for label, url in SOURCES:
        all_items.extend(fetch_feed(label, url))
    # Deduplicate by URL — different feeds can syndicate the same story.
    seen, deduped = set(), []
    for it in all_items:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        deduped.append(it)
    # Newest first
    deduped.sort(key=lambda x: x["published"], reverse=True)
    deduped = deduped[:MAX_ITEMS_TOTAL]

    out = {
        "updated": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "items": deduped,
    }
    OUT_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"Wrote {len(deduped)} items to {OUT_FILE}")


if __name__ == "__main__":
    main()
