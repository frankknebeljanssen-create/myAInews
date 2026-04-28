"""Update neuron.json — runs daily via GitHub Actions.

Beehiiv's /feed endpoint now returns the homepage HTML instead of RSS,
so we scrape the homepage to find the latest issue, then fetch that issue
directly. Cloudscraper bypasses Cloudflare's bot challenges that block
plain requests.

Workflow:
  1. cloudscraper.get(homepage) → find latest /p/ post URL
  2. cloudscraper.get(issue_url) → full HTML
  3. Strip to clean text
  4. Send to Haiku 4.5 for structured extraction
  5. Compare to existing — skip if same issue_url
  6. Write neuron.json
"""
import os, sys, json, re
from datetime import datetime, timezone
from pathlib import Path
import cloudscraper
from bs4 import BeautifulSoup
from anthropic import Anthropic

REPO_ROOT = Path(__file__).parent
NEURON_FILE = REPO_ROOT / "neuron.json"

NEURON_HOME = "https://www.theneurondaily.com/"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"

if not ANTHROPIC_API_KEY:
    print("[neuron] FAIL: ANTHROPIC_API_KEY not set", file=sys.stderr)
    sys.exit(1)

client = Anthropic(api_key=ANTHROPIC_API_KEY)

# Single scraper instance — reuses Cloudflare cookies across requests
SCRAPER = cloudscraper.create_scraper(
    browser={"browser": "chrome", "platform": "darwin", "desktop": True}
)


def fetch(url):
    print(f"[neuron] GET {url}")
    r = SCRAPER.get(url, timeout=30)
    r.raise_for_status()
    return r.text


def find_latest_issue_url(home_html):
    """Parse homepage for the URL of the latest /p/ post (Beehiiv pattern)."""
    soup = BeautifulSoup(home_html, "html.parser")
    seen = set()
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/p/" not in href:
            continue
        full = href if href.startswith("http") else f"https://www.theneurondaily.com{href if href.startswith('/') else '/' + href}"
        # de-dup, preserve discovery order
        if full not in seen:
            seen.add(full)
            candidates.append(full)
    if not candidates:
        raise RuntimeError("no /p/ post links found on homepage")
    print(f"[neuron] found {len(candidates)} /p/ links, picking first as latest")
    return candidates[0]


def html_to_text(html):
    """Strip HTML chrome, return clean readable text capped at 50k chars."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "svg"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body
    text = main.get_text("\n", strip=True) if main else soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:50000]


def extract_title_from_html(html):
    """Pull the og:title or <title> from the issue page for use as original_title."""
    soup = BeautifulSoup(html, "html.parser")
    og = soup.find("meta", property="og:title")
    if og and og.get("content"):
        return og["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return ""


def extract_published_date(html):
    """Try to find the publish date from meta tags. Returns YYYY-MM-DD or empty."""
    soup = BeautifulSoup(html, "html.parser")
    for prop in ["article:published_time", "og:article:published_time"]:
        m = soup.find("meta", property=prop)
        if m and m.get("content"):
            try:
                # ISO 8601 — first 10 chars are YYYY-MM-DD
                return m["content"][:10]
            except Exception:
                pass
    return ""


SYSTEM_PROMPT = """You extract structured data from issues of "The Neuron Daily", an AI newsletter.

Output ONLY a single valid JSON object matching this exact schema. No markdown fences, no commentary, just JSON:

{
  "date": "YYYY-MM-DD",
  "issue_url": "the issue URL exactly as provided in the input",
  "emoji": "single Unicode emoji capturing the day's theme",
  "headline": "the issue's actual headline, ~60 chars",
  "subtitle": "one-line summary listing 2-3 key items, separated by space-middot-space ( · )",
  "intro": "1-3 sentence intro paragraph capturing the issue's hook, paraphrased in your own words",
  "bullets": [
    {
      "emoji": "single Unicode emoji",
      "text": "headline of this story (paraphrased, under 80 chars)",
      "url": "source URL if mentioned, else empty string",
      "source": "publication or company name",
      "summary": "2-3 sentence summary in your own words, never quoted directly"
    }
  ],
  "main_story": {
    "title": "the main story's title (paraphrased)",
    "body": "the narrative, 3-5 sentences in your own words",
    "why": "why this matters, 1-2 sentences",
    "take": "the editorial take or conclusion, 1-2 sentences"
  },
  "around_the_horn": [
    { "text": "one-line summary of the linked story (paraphrased, under 120 chars)", "url": "source URL" }
  ]
}

Hard rules:
- Use the EXACT issue_url string and date provided by the user (don't recompute)
- bullets: 3-5 items, the main news roundup
- around_the_horn: 3-6 items, the secondary mentions
- All text fields must be PARAPHRASED in your own words — never copy direct quotes longer than 8 words
- If a section is missing in the source, use empty string or empty array — never invent content
- emoji must be a single Unicode emoji character, never text or shortcode"""


def extract_structured(article_text, issue_url, pub_date, original_title):
    user_msg = (
        f"Issue URL: {issue_url}\n"
        f"Date: {pub_date}\n"
        f"Original title: {original_title}\n\n"
        f"--- ARTICLE TEXT ---\n\n{article_text}"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": "{"},
        ],
    )
    raw_text = resp.content[0].text
    full = "{" + raw_text
    depth = 0
    end = -1
    for i, ch in enumerate(full):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end > 0:
        full = full[:end]
    return json.loads(full)


def main():
    try:
        home_html = fetch(NEURON_HOME)
    except Exception as e:
        print(f"[neuron] FAIL: cannot fetch homepage: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[neuron] homepage: {len(home_html)} chars")

    try:
        issue_url = find_latest_issue_url(home_html)
    except Exception as e:
        print(f"[neuron] FAIL: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[neuron] latest issue URL: {issue_url}")

    # Read existing file once — used both for same-issue check AND for preserving as "previous"
    existing = None
    if NEURON_FILE.exists():
        try:
            existing = json.loads(NEURON_FILE.read_text())
        except Exception:
            pass

    # Skip if same issue already captured
    if existing and existing.get("issue_url") == issue_url:
        print("[neuron] same issue already in neuron.json, skipping")
        return

    try:
        issue_html = fetch(issue_url)
    except Exception as e:
        print(f"[neuron] FAIL: cannot fetch issue: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[neuron] issue page: {len(issue_html)} chars")

    article_text = html_to_text(issue_html)
    print(f"[neuron] article text: {len(article_text)} chars")
    if len(article_text) < 500:
        print(f"[neuron] FAIL: article text too short ({len(article_text)} chars)", file=sys.stderr)
        sys.exit(1)

    original_title = extract_title_from_html(issue_html)
    pub_date = extract_published_date(issue_html) or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    print(f"[neuron] date: {pub_date} | title: {original_title[:80]}")

    print(f"[neuron] extracting structured data via {MODEL}...")
    try:
        data = extract_structured(article_text, issue_url, pub_date, original_title)
    except json.JSONDecodeError as e:
        print(f"[neuron] FAIL: Haiku returned invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[neuron] FAIL: extraction error: {e}", file=sys.stderr)
        sys.exit(1)

    required = ["headline", "intro", "bullets", "main_story", "around_the_horn"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        print(f"[neuron] FAIL: missing fields: {missing}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data.get("bullets"), list) or len(data["bullets"]) < 1:
        print("[neuron] FAIL: no bullets extracted", file=sys.stderr)
        sys.exit(1)

    # Force trusted fields
    data["issue_url"] = issue_url
    data["date"] = pub_date

    # Preserve yesterday — take existing data (without its own nested previous) as "previous"
    if existing and existing.get("issue_url") and existing.get("issue_url") != issue_url:
        data["previous"] = {k: v for k, v in existing.items() if k != "previous"}
        print(f"[neuron] preserved previous issue: {existing.get('date')} | {existing.get('headline','')[:60]}")

    NEURON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"[neuron] wrote {NEURON_FILE}")
    print(f"[neuron] headline: {data.get('headline')[:80]}")
    print(f"[neuron] {len(data['bullets'])} bullets, {len(data.get('around_the_horn', []))} around-the-horn")


if __name__ == "__main__":
    main()
