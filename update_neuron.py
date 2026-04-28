"""Update neuron.json — runs daily via GitHub Actions.

Uses The Neuron's Beehiiv RSS feed (already proven to work in curate.py).
Bypasses the homepage's Cloudflare protection entirely.

Workflow:
  1. Fetch RSS feed via feedparser
  2. Pick newest entry (Beehiiv RSS is sorted newest-first)
  3. Extract content:encoded HTML (full issue body)
  4. Strip to clean text
  5. Send to Haiku 4.5 to extract structured JSON matching the PWA schema
  6. Compare to existing — skip if same issue_url
  7. Write neuron.json
"""
import os, sys, json, re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
import feedparser
from bs4 import BeautifulSoup
from anthropic import Anthropic

REPO_ROOT = Path(__file__).parent
NEURON_FILE = REPO_ROOT / "neuron.json"

NEURON_RSS = "https://www.theneurondaily.com/feed"
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"

# Use a realistic UA — feedparser default sometimes gets rate-limited
feedparser.USER_AGENT = "Mozilla/5.0 (compatible; myAInews-bot/1.0; +https://github.com/frankknebeljanssen-create/myAInews)"

if not ANTHROPIC_API_KEY:
    print("[neuron] FAIL: ANTHROPIC_API_KEY not set", file=sys.stderr)
    sys.exit(1)

client = Anthropic(api_key=ANTHROPIC_API_KEY)


def fetch_latest_issue():
    """Parse RSS feed, return latest entry's URL, title, content HTML, and date."""
    print(f"[neuron] fetching RSS feed: {NEURON_RSS}")
    fp = feedparser.parse(NEURON_RSS)
    if fp.bozo and not fp.entries:
        raise RuntimeError(f"RSS parse error: {fp.bozo_exception}")
    if not fp.entries:
        raise RuntimeError("RSS feed has no entries")
    latest = fp.entries[0]

    issue_url = latest.get("link", "")
    if not issue_url:
        raise RuntimeError("latest entry has no link")

    title = latest.get("title", "").strip()

    # Beehiiv RSS includes content:encoded with full body
    content_html = ""
    if hasattr(latest, "content") and latest.content:
        content_html = latest.content[0].get("value", "")
    if not content_html and hasattr(latest, "summary"):
        content_html = latest.summary
    if not content_html and hasattr(latest, "description"):
        content_html = latest.description
    if not content_html:
        raise RuntimeError("latest entry has no content/summary")

    # pubDate is RFC2822, convert to YYYY-MM-DD
    pub_date = ""
    if hasattr(latest, "published"):
        try:
            dt = parsedate_to_datetime(latest.published)
            pub_date = dt.strftime("%Y-%m-%d")
        except Exception:
            pass
    if not pub_date:
        pub_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return {
        "issue_url": issue_url,
        "title": title,
        "content_html": content_html,
        "pub_date": pub_date,
    }


def html_to_text(html):
    """Strip HTML chrome, return clean readable text capped at 50k chars."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:50000]


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
    # Cut at the first complete JSON object
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
        issue = fetch_latest_issue()
    except Exception as e:
        print(f"[neuron] FAIL: {e}", file=sys.stderr)
        sys.exit(1)

    issue_url = issue["issue_url"]
    pub_date = issue["pub_date"]
    print(f"[neuron] latest issue: {pub_date} | {issue['title'][:80]}")
    print(f"[neuron] url: {issue_url}")

    # Skip if same issue already captured
    if NEURON_FILE.exists():
        try:
            existing = json.loads(NEURON_FILE.read_text())
            if existing.get("issue_url") == issue_url:
                print("[neuron] same issue already in neuron.json, skipping")
                return
        except Exception:
            pass

    article_text = html_to_text(issue["content_html"])
    print(f"[neuron] article text: {len(article_text)} chars")
    if len(article_text) < 500:
        print(f"[neuron] FAIL: article text too short ({len(article_text)} chars)", file=sys.stderr)
        sys.exit(1)

    print(f"[neuron] extracting structured data via {MODEL}...")
    try:
        data = extract_structured(article_text, issue_url, pub_date, issue["title"])
    except json.JSONDecodeError as e:
        print(f"[neuron] FAIL: Haiku returned invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[neuron] FAIL: extraction error: {e}", file=sys.stderr)
        sys.exit(1)

    # Sanity checks
    required = ["headline", "intro", "bullets", "main_story", "around_the_horn"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        print(f"[neuron] FAIL: missing fields: {missing}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data.get("bullets"), list) or len(data["bullets"]) < 1:
        print("[neuron] FAIL: no bullets extracted", file=sys.stderr)
        sys.exit(1)

    # Force trusted fields (defense against Haiku rewriting)
    data["issue_url"] = issue_url
    data["date"] = pub_date

    NEURON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"[neuron] wrote {NEURON_FILE}")
    print(f"[neuron] headline: {data.get('headline')[:80]}")
    print(f"[neuron] {len(data['bullets'])} bullets, {len(data.get('around_the_horn', []))} around-the-horn")


if __name__ == "__main__":
    main()
