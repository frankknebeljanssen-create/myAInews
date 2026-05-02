"""Update neuron.json — runs daily via GitHub Actions.

Dual-source fetcher:
  PRIMARY:   The Rundown AI — homepage scraping (no bot protection)
  SECONDARY: The Neuron Daily — via ScraperAPI (bypasses Cloudflare)

If The Neuron fails, the script continues with Rundown only (exit 0).
If Rundown fails entirely, the script exits 1 (real failure).
"""
import os, sys, json, re, xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

REPO_ROOT   = Path(__file__).parent
NEURON_FILE = REPO_ROOT / "neuron.json"

RUNDOWN_RSS_URLS = [
    "https://www.therundown.ai/feed",
    "https://www.therundown.ai/rss.xml",
    "https://www.therundown.ai/rss",
]
RUNDOWN_HOME = "https://www.therundown.ai/"
NEURON_HOME  = "https://www.theneurondaily.com/"

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
SCRAPERAPI_KEY     = os.environ.get("SCRAPERAPI_KEY", "")
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def http_get(url, timeout=30):
    """Plain request — for Rundown and RSS."""
    print(f"[today] GET {url}")
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def scraperapi_get(url, timeout=60):
    """Route through ScraperAPI to bypass Cloudflare. Uses render_js=true."""
    if not SCRAPERAPI_KEY:
        raise RuntimeError("SCRAPERAPI_KEY not set")
    api_url = "https://api.scraperapi.com/"
    params = {
        "api_key": SCRAPERAPI_KEY,
        "url": url,
        "render_js": "true",   # handles JS challenges
        "premium": "true",     # residential IPs — needed for Cloudflare Enterprise
    }
    print(f"[today] ScraperAPI GET {url}")
    r = requests.get(api_url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.text


def html_to_text(html, max_chars=50000):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script","style","nav","footer","header","aside","noscript","svg"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body
    text = main.get_text("\n", strip=True) if main else soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:max_chars]


def extract_meta(html, prop):
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property=prop)
    return og["content"].strip() if og and og.get("content") else ""


# ── The Rundown (primary) ──────────────────────────────────────────────────────

def fetch_rundown_issue_url():
    """Try RSS feeds; fall back to scraping homepage."""
    for rss_url in RUNDOWN_RSS_URLS:
        try:
            text = http_get(rss_url)
            root = ET.fromstring(text)
            ns = {"atom": "http://www.w3.org/2005/Atom"}
            items = root.findall(".//item")
            if items:
                link = items[0].findtext("link", "").strip()
                if link:
                    print(f"[rundown] RSS → {link}")
                    return link
            entries = root.findall("atom:entry", ns)
            if entries:
                link_el = entries[0].find("atom:link", ns)
                if link_el is not None:
                    href = link_el.get("href", "").strip()
                    if href:
                        print(f"[rundown] Atom → {href}")
                        return href
        except Exception as e:
            print(f"[rundown] RSS {rss_url} failed: {e}")

    print("[rundown] RSS unavailable, scraping homepage…")
    home_html = http_get(RUNDOWN_HOME)
    soup = BeautifulSoup(home_html, "html.parser")
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/p/" not in href: continue
        full = (href if href.startswith("http")
                else f"https://www.therundown.ai{href if href.startswith('/') else '/'+href}")
        if full not in seen:
            seen.add(full)
            print(f"[rundown] homepage fallback → {full}")
            return full

    raise RuntimeError("Could not find The Rundown latest issue URL")


# ── The Neuron (secondary via ScraperAPI) ─────────────────────────────────────

def try_fetch_neuron():
    """Returns (issue_url, html) or None if unavailable."""
    if not SCRAPERAPI_KEY:
        print("[neuron] SKIP: SCRAPERAPI_KEY not set")
        return None

    try:
        home_html = scraperapi_get(NEURON_HOME)
    except Exception as e:
        print(f"[neuron] SKIP: homepage fetch failed: {e}")
        return None

    soup = BeautifulSoup(home_html, "html.parser")
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/p/" not in href: continue
        full = (href if href.startswith("http")
                else f"https://www.theneurondaily.com{href if href.startswith('/') else '/'+href}")
        if full not in seen:
            seen.add(full)
            try:
                issue_html = scraperapi_get(full)
                print(f"[neuron] fetched issue: {full}")
                return full, issue_html
            except Exception as e:
                print(f"[neuron] SKIP: issue fetch failed: {e}")
                return None

    print("[neuron] SKIP: no /p/ links found on homepage")
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
- Use the EXACT issue_url and date from input — never recompute
- bullets: 3-5 items; around_the_horn: 3-6 items
- All text PARAPHRASED — never copy quotes longer than 8 words
- Missing sections → empty string / empty array, never invented
- emoji = single Unicode character only"""


def extract_structured(article_text, issue_url, pub_date, original_title, source_name):
    prompt = SYSTEM_PROMPT.replace("SOURCE_NAME", source_name)
    user_msg = (f"Issue URL: {issue_url}\nDate: {pub_date}\n"
                f"Original title: {original_title}\n\n"
                f"--- ARTICLE TEXT ---\n\n{article_text}")
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
    if end: raw = raw[:end]
    return json.loads(raw)


def extract_from_html(html, issue_url, source_name):
    text = html_to_text(html)
    if len(text) < 500:
        raise RuntimeError(f"article text too short ({len(text)} chars)")
    pub_date = (extract_meta(html, "article:published_time") or
                extract_meta(html, "og:article:published_time") or
                datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
    title = (extract_meta(html, "og:title") or
             getattr(BeautifulSoup(html,"html.parser").title, "string", "") or "")
    print(f"[today] {source_name}: {len(text)} chars | {pub_date} | {title[:60]}")
    return extract_structured(text, issue_url, pub_date, title.strip(), source_name)


# ── Merge ──────────────────────────────────────────────────────────────────────

def merge_sources(primary, secondary):
    """Interleave bullets P,S,P,S…; keep primary headline + main_story."""
    if not secondary:
        return primary
    merged = dict(primary)
    pb, sb = primary.get("bullets",[]), secondary.get("bullets",[])
    interleaved = []
    for i in range(max(len(pb), len(sb))):
        if i < len(pb): interleaved.append(pb[i])
        if i < len(sb): interleaved.append(sb[i])
    merged["bullets"] = interleaved
    merged["around_the_horn"] = (primary.get("around_the_horn",[]) +
                                  secondary.get("around_the_horn",[]))
    merged["sources_fetched"] = ["rundown", "neuron"]
    return merged


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    existing = None
    if NEURON_FILE.exists():
        try: existing = json.loads(NEURON_FILE.read_text())
        except Exception: pass

    # ── PRIMARY: The Rundown AI ───────────────────────────────────────────────
    try:
        rundown_url = fetch_rundown_issue_url()
    except Exception as e:
        print(f"[rundown] FAIL: {e}", file=sys.stderr)
        sys.exit(1)

    # Skip if already have this issue with both sources
    if (existing and existing.get("issue_url") == rundown_url and
            "neuron" in existing.get("sources_fetched", [])):
        print("[today] already have rundown+neuron for this issue, skipping")
        return

    try:
        rundown_html = http_get(rundown_url)
    except Exception as e:
        print(f"[rundown] FAIL: cannot fetch issue: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        rundown_data = extract_from_html(rundown_html, rundown_url, "The Rundown AI")
    except Exception as e:
        print(f"[rundown] FAIL: extraction: {e}", file=sys.stderr)
        sys.exit(1)

    rundown_data["issue_url"] = rundown_url
    rundown_data["date"]      = rundown_data.get("date") or today

    required = ["headline","intro","bullets","main_story","around_the_horn"]
    missing  = [k for k in required if not rundown_data.get(k)]
    if missing:
        print(f"[rundown] FAIL: missing fields: {missing}", file=sys.stderr)
        sys.exit(1)

    print(f"[rundown] ✓ {len(rundown_data['bullets'])} bullets | {rundown_data['headline'][:70]}")

    # ── SECONDARY: The Neuron Daily via ScraperAPI ────────────────────────────
    neuron_data = None
    result = try_fetch_neuron()
    if result:
        neuron_url, neuron_html = result
        try:
            neuron_data = extract_from_html(neuron_html, neuron_url, "The Neuron Daily")
            print(f"[neuron] ✓ {len(neuron_data['bullets'])} bullets | {neuron_data['headline'][:70]}")
        except Exception as e:
            print(f"[neuron] SKIP: extraction error: {e}")

    # ── Merge & write ─────────────────────────────────────────────────────────
    data = merge_sources(rundown_data, neuron_data)
    label = "rundown+neuron" if neuron_data else "rundown only"
    print(f"[today] merged: {len(data['bullets'])} bullets ({label})")

    if existing and existing.get("issue_url") and existing.get("issue_url") != rundown_url:
        data["previous"] = {k: v for k, v in existing.items() if k != "previous"}
        print(f"[today] preserved previous: {existing.get('date')} | {existing.get('headline','')[:60]}")

    NEURON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"[today] wrote {NEURON_FILE} | {data['headline'][:80]}")
    print(f"[today] {len(data['bullets'])} bullets, {len(data.get('around_the_horn',[]))} around_the_horn")


if __name__ == "__main__":
    main()
