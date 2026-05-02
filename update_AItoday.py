"""Update neuron.json — runs daily via GitHub Actions.

Multi-source fetcher (in priority order):
  1. The Rundown AI  — homepage scraping (primary, exit 1 if fails)
  2. TLDR AI         — tldr.tech/ai (no bot protection)
  3. Ben's Bites     — beehiiv/homepage scraping (bensbites.com)
  4. The Neuron      — ScraperAPI (optional, often blocked)

Bullets are deduplicated across sources (~70% title similarity = same story).
The best-source version of each story is kept (Rundown > TLDR > Ben's > Neuron).
"""
import os, sys, json, re, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

REPO_ROOT   = Path(__file__).parent
NEURON_FILE = REPO_ROOT / "neuron.json"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SCRAPERAPI_KEY    = os.environ.get("SCRAPERAPI_KEY", "")
MODEL = "claude-haiku-4-5-20251001"

if not ANTHROPIC_API_KEY:
    print("[today] FAIL: ANTHROPIC_API_KEY not set", file=sys.stderr)
    sys.exit(1)

client = Anthropic(api_key=ANTHROPIC_API_KEY)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def http_get(url, timeout=30):
    print(f"[today] GET {url}")
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def scraperapi_get(url, timeout=60):
    if not SCRAPERAPI_KEY:
        raise RuntimeError("SCRAPERAPI_KEY not set")
    r = requests.get("https://api.scraperapi.com/", timeout=timeout, params={
        "api_key": SCRAPERAPI_KEY,
        "url": url,
        "render_js": "true",
        "premium": "true",
    })
    r.raise_for_status()
    return r.text


def html_to_text(html, max_chars=50000):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style","nav","footer","header","aside","noscript","svg"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body
    text = main.get_text("\n", strip=True) if main else soup.get_text("\n", strip=True)
    return re.sub(r"\n{3,}", "\n\n", text)[:max_chars]


def extract_meta(html, prop):
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property=prop)
    return og["content"].strip() if og and og.get("content") else ""



def is_article_url(url):
    """Returns False for bare domain roots — only keep real article URLs."""
    if not url: return False
    try:
        from urllib.parse import urlparse
        p = urlparse(url)
        path = p.path.rstrip('/')
        # Bare domain: path is empty or just '/'
        if not path or path == '': return False
        # Very short path like /about /home = not an article
        if len(path) < 4: return False
        return True
    except Exception:
        return False


def rss_latest_url(rss_urls, base):
    """Try RSS/Atom feeds, return latest post URL or None."""
    for rss_url in rss_urls:
        try:
            text = http_get(rss_url)
            root = ET.fromstring(text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = root.findall(".//item")
            if items:
                link = items[0].findtext("link","").strip()
                if link: return link
            entries = root.findall("atom:entry", ns)
            if entries:
                el = entries[0].find("atom:link", ns)
                if el is not None and el.get("href"):
                    return el.get("href").strip()
        except Exception as e:
            print(f"[today] RSS {rss_url} failed: {e}")
    return None


# ── Claude extraction ──────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You extract structured data from AI newsletters.

Output ONLY a single valid JSON object. No markdown fences, no commentary:

{
  "date": "YYYY-MM-DD",
  "issue_url": "the issue URL exactly as provided",
  "emoji": "single Unicode emoji capturing the day's theme",
  "headline": "the issue's actual headline, ~60 chars",
  "subtitle": "2-3 key items separated by · ",
  "intro": "1-3 sentence hook, paraphrased in your own words",
  "bullets": [
    {
      "emoji": "single Unicode emoji",
      "text": "story headline, paraphrased, under 80 chars",
      "url": "source URL if available, else empty string",
      "source": "SOURCE_NAME",
      "summary": "2-3 sentences in your own words, never directly quoted"
    }
  ],
  "main_story": {
    "title": "paraphrased title",
    "body": "3-5 sentences in your own words",
    "why": "why this matters, 1-2 sentences",
    "take": "editorial take, 1-2 sentences"
  },
  "around_the_horn": [
    { "text": "paraphrased one-liner under 120 chars", "url": "source URL" }
  ]
}

Rules:
- Use the EXACT issue_url and date from input
- bullets: 3-6 items; around_the_horn: 3-6 items
- All text PARAPHRASED — never copy quotes longer than 8 words
- Missing sections → empty string / empty array, never invented
- emoji = single Unicode character only"""


def extract_from_html(html, issue_url, source_name):
    text = html_to_text(html)
    if len(text) < 300:
        raise RuntimeError(f"too short: {len(text)} chars")
    pub_date = (extract_meta(html, "article:published_time") or
                extract_meta(html, "og:article:published_time") or
                datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
    title = (extract_meta(html, "og:title") or
             getattr(BeautifulSoup(html,"html.parser").title, "string", "") or "")
    print(f"[today] {source_name}: {len(text)} chars | {pub_date} | {title[:55]}")

    prompt = SYSTEM_PROMPT.replace("SOURCE_NAME", source_name)
    user_msg = (f"Issue URL: {issue_url}\nDate: {pub_date}\n"
                f"Original title: {title.strip()}\n\n--- ARTICLE TEXT ---\n\n{text}")
    resp = client.messages.create(
        model=MODEL, max_tokens=4000, system=prompt,
        messages=[
            {"role": "user",      "content": user_msg},
            {"role": "assistant", "content": "{"},
        ],
    )
    raw = "{" + resp.content[0].text
    depth = end = 0
    for i, ch in enumerate(raw):
        if ch == "{":   depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0: end = i + 1; break
    data = json.loads(raw[:end] if end else raw)
    data["issue_url"] = issue_url
    data["date"]      = pub_date
    # Strip bare domain URLs — only keep real article links
    for b in data.get("bullets", []):
        if not is_article_url(b.get("url", "")):
            b["url"] = ""
    for h in data.get("around_the_horn", []):
        if not is_article_url(h.get("url", "")):
            h["url"] = ""
    return data


# ── Deduplication ──────────────────────────────────────────────────────────────

def title_tokens(text):
    """Lowercase words, no punctuation, no stop words."""
    stops = {"the","a","an","in","of","to","and","is","for","on","its","as",
             "at","by","with","from","that","this","how","why","are","will",
             "has","it","be","was","have","not","but","or","what","who","new"}
    words = re.findall(r"[a-z0-9]+", text.lower())
    return set(w for w in words if w not in stops and len(w) > 2)


def similarity(a, b):
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb: return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def deduplicate(all_bullets, threshold=0.55):
    """Keep first (highest-priority) version of each story."""
    kept = []
    for b in all_bullets:
        is_dup = any(similarity(b["text"], k["text"]) >= threshold for k in kept)
        if not is_dup:
            kept.append(b)
    return kept


# ── Individual source fetchers ────────────────────────────────────────────────

def fetch_rundown():
    """PRIMARY — exits 1 on failure."""
    rss_urls = [
        "https://www.therundown.ai/feed",
        "https://www.therundown.ai/rss.xml",
        "https://www.therundown.ai/rss",
    ]
    url = rss_latest_url(rss_urls, "https://www.therundown.ai/")
    if not url:
        # Fallback: scrape homepage
        print("[rundown] RSS unavailable, scraping homepage…")
        home = http_get("https://www.therundown.ai/")
        soup = BeautifulSoup(home, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/p/" not in href: continue
            url = href if href.startswith("http") else f"https://www.therundown.ai{href if href.startswith('/') else '/'+href}"
            break
    if not url:
        print("[rundown] FAIL: cannot find latest issue", file=sys.stderr)
        sys.exit(1)
    print(f"[rundown] issue → {url}")
    html = http_get(url)
    data = extract_from_html(html, url, "The Rundown AI")
    print(f"[rundown] ✓ {len(data['bullets'])} bullets | {data['headline'][:65]}")
    return data


def fetch_tldr():
    """TLDR AI — use today's date URL directly (tldr.tech/ai/YYYY-MM-DD)."""
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        issue_url = f"https://tldr.tech/ai/{today}"
        issue_html = http_get(issue_url)
        if len(issue_html) < 2000:
            raise RuntimeError(f"page too short ({len(issue_html)} chars) — issue may not be published yet")
        data = extract_from_html(issue_html, issue_url, "TLDR AI")
        print(f"[tldr] ✓ {len(data['bullets'])} bullets | {data['headline'][:65]}")
        return data
    except Exception as e:
        print(f"[tldr] SKIP: {e}")
        return None


def fetch_bensbites():
    """Ben's Bites — try multiple URLs including direct homepage scrape."""
    try:
        # Try RSS feeds first
        rss_urls = [
            "https://bensbites.beehiiv.com/feed",
            "https://bensbites.com/feed",
            "https://www.bensbites.co/feed",
        ]
        url = rss_latest_url(rss_urls, "https://bensbites.beehiiv.com/")
        if url:
            html = http_get(url)
            data = extract_from_html(html, url, "Ben's Bites")
            print(f"[bensbites] ✓ {len(data['bullets'])} bullets | {data['headline'][:65]}")
            return data

        # Fallback: scrape homepage for /p/ links
        for home_url in ["https://bensbites.beehiiv.com/", "https://bensbites.com/"]:
            try:
                home_html = http_get(home_url)
                soup = BeautifulSoup(home_html, "html.parser")
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/p/" not in href: continue
                    issue_url = href if href.startswith("http") else f"{home_url.rstrip('/')}{href}"
                    html = http_get(issue_url)
                    data = extract_from_html(html, issue_url, "Ben's Bites")
                    print(f"[bensbites] ✓ {len(data['bullets'])} bullets | {data['headline'][:65]}")
                    return data
            except Exception:
                continue

        print("[bensbites] SKIP: all URLs failed")
        return None
    except Exception as e:
        print(f"[bensbites] SKIP: {e}")
        return None


def fetch_neuron():
    """The Neuron — via ScraperAPI (often blocked, always optional)."""
    if not SCRAPERAPI_KEY:
        print("[neuron] SKIP: no SCRAPERAPI_KEY")
        return None
    try:
        home_html = scraperapi_get("https://www.theneurondaily.com/")
        soup = BeautifulSoup(home_html, "html.parser")
        url = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/p/" not in href: continue
            url = href if href.startswith("http") else f"https://www.theneurondaily.com{href if href.startswith('/') else '/'+href}"
            break
        if not url:
            print("[neuron] SKIP: no /p/ links found")
            return None
        issue_html = scraperapi_get(url)
        data = extract_from_html(issue_html, url, "The Neuron Daily")
        print(f"[neuron] ✓ {len(data['bullets'])} bullets | {data['headline'][:65]}")
        return data
    except Exception as e:
        print(f"[neuron] SKIP: {e}")
        return None


# ── Merge + deduplicate ────────────────────────────────────────────────────────

def merge_all(sources):
    """
    sources = [primary_data, tldr_data|None, bensbites_data|None, neuron_data|None]
    Primary headline + main_story always wins.
    Bullets interleaved then deduplicated.
    """
    primary = sources[0]
    all_bullets = []
    for s in sources:
        if s and s.get("bullets"):
            all_bullets.extend(s["bullets"])

    unique_bullets = deduplicate(all_bullets)
    print(f"[today] dedup: {len(all_bullets)} → {len(unique_bullets)} unique bullets")

    merged = dict(primary)
    merged["bullets"] = unique_bullets
    merged["sources_fetched"] = [s["issue_url"].split("/")[2] for s in sources if s]
    return merged


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    existing = None
    if NEURON_FILE.exists():
        try: existing = json.loads(NEURON_FILE.read_text())
        except Exception: pass

    # ── Fetch all sources ─────────────────────────────────────────────────────
    rundown   = fetch_rundown()                    # exits 1 if fails
    tldr      = fetch_tldr()
    bensbites = fetch_bensbites()
    neuron    = fetch_neuron()

    fetched_count = sum(1 for s in [rundown, tldr, bensbites, neuron] if s)
    print(f"[today] {fetched_count}/4 sources fetched")

    # ── Skip if already have same issue with same sources ─────────────────────
    if existing and existing.get("issue_url") == rundown["issue_url"]:
        prev_sources = set(existing.get("sources_fetched", []))
        curr_sources = set(s["issue_url"].split("/")[2] for s in [rundown, tldr, bensbites, neuron] if s)
        if prev_sources >= curr_sources:
            print("[today] already have this issue with all available sources, skipping")
            return

    # ── Merge & deduplicate ───────────────────────────────────────────────────
    data = merge_all([s for s in [rundown, tldr, bensbites, neuron] if s])

    # ── Preserve previous ─────────────────────────────────────────────────────
    if existing and existing.get("issue_url") and existing.get("issue_url") != rundown["issue_url"]:
        data["previous"] = {k: v for k, v in existing.items() if k != "previous"}
        print(f"[today] preserved previous: {existing.get('date')} | {existing.get('headline','')[:55]}")

    # ── Write ─────────────────────────────────────────────────────────────────
    NEURON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"[today] wrote {NEURON_FILE}")
    print(f"[today] headline: {data['headline'][:80]}")
    print(f"[today] {len(data['bullets'])} unique bullets from {fetched_count} sources")


if __name__ == "__main__":
    main()
