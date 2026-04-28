"""Update neuron.json — runs daily via GitHub Actions.

Workflow:
  1. Fetch theneurondaily.com homepage
  2. Find the URL of the latest issue (most recent /p/ post)
  3. Fetch that issue's full HTML, strip to article text
  4. Use Haiku 4.5 to extract structured JSON matching the PWA schema
  5. Compare to existing — skip write if same issue already captured
  6. Write neuron.json (committed back to repo by workflow)

Schema matches the embedded NEURON_FALLBACK in the PWA:
  date, issue_url, emoji, headline, subtitle, intro,
  bullets[{emoji, text, url, source, summary}],
  main_story{title, body, why, take},
  around_the_horn[{text, url}]
"""
import os, sys, json, re
from datetime import datetime, timezone
from pathlib import Path
import requests
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


def fetch(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; myAInews-bot/1.0; +https://github.com/frankknebeljanssen-create/myAInews)",
        "Accept": "text/html,application/xhtml+xml",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.text


def find_latest_issue_url(home_html):
    """Parse the homepage to find the URL of the latest issue (Beehiiv /p/ pattern)."""
    soup = BeautifulSoup(home_html, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/p/" not in href:
            continue
        full = href if href.startswith("http") else f"https://www.theneurondaily.com{href.lstrip('/')}"
        # de-dup while preserving order
        if full not in candidates:
            candidates.append(full)
    if not candidates:
        raise RuntimeError("no /p/ post links found on homepage")
    return candidates[0]  # most recent is typically first


def extract_article_text(html):
    """Strip HTML chrome, return clean readable text capped at 50k chars."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "noscript", "svg"]):
        tag.decompose()
    main = soup.find("article") or soup.find("main") or soup.body
    text = main.get_text("\n", strip=True) if main else soup.get_text("\n", strip=True)
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
- Use the EXACT issue_url string provided by the user
- date in YYYY-MM-DD format, taken from the issue itself
- bullets: 3-5 items, the main news roundup
- around_the_horn: 3-6 items, the secondary mentions
- All text fields must be PARAPHRASED in your own words — never copy direct quotes from the source longer than 8 words
- If a section is missing in the source, use empty string or empty array — never invent content
- emoji must be a single Unicode emoji character, never text or shortcode"""


def extract_structured(article_text, issue_url):
    user_msg = f"Issue URL: {issue_url}\n\n--- ARTICLE TEXT ---\n\n{article_text}"
    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[
            {"role": "user", "content": user_msg},
            {"role": "assistant", "content": "{"},  # prefill forces JSON output
        ],
    )
    raw_text = resp.content[0].text
    full = "{" + raw_text
    # Strip optional trailing fences just in case
    full = re.sub(r"```\s*$", "", full).strip()
    # If there's text after the closing brace, cut it
    # Find the first complete JSON object
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
    print(f"[neuron] fetching {NEURON_HOME}...")
    try:
        home_html = fetch(NEURON_HOME)
    except Exception as e:
        print(f"[neuron] FAIL: cannot fetch homepage: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        issue_url = find_latest_issue_url(home_html)
    except Exception as e:
        print(f"[neuron] FAIL: cannot find latest issue: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[neuron] latest issue: {issue_url}")

    # Skip if same issue already captured
    if NEURON_FILE.exists():
        try:
            existing = json.loads(NEURON_FILE.read_text())
            if existing.get("issue_url") == issue_url:
                print("[neuron] same issue already in neuron.json, skipping")
                return
        except Exception:
            pass  # malformed existing file, continue and overwrite

    try:
        issue_html = fetch(issue_url)
    except Exception as e:
        print(f"[neuron] FAIL: cannot fetch issue: {e}", file=sys.stderr)
        sys.exit(1)
    article_text = extract_article_text(issue_html)
    print(f"[neuron] article text: {len(article_text)} chars")

    if len(article_text) < 500:
        print(f"[neuron] FAIL: article text too short ({len(article_text)} chars), aborting", file=sys.stderr)
        sys.exit(1)

    print(f"[neuron] extracting structured data via {MODEL}...")
    try:
        data = extract_structured(article_text, issue_url)
    except json.JSONDecodeError as e:
        print(f"[neuron] FAIL: Haiku returned invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[neuron] FAIL: extraction error: {e}", file=sys.stderr)
        sys.exit(1)

    # Sanity checks
    required = ["date", "issue_url", "headline", "intro", "bullets", "main_story", "around_the_horn"]
    missing = [k for k in required if not data.get(k)]
    if missing:
        print(f"[neuron] FAIL: extracted data missing fields: {missing}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(data.get("bullets"), list) or len(data["bullets"]) < 1:
        print("[neuron] FAIL: no bullets extracted", file=sys.stderr)
        sys.exit(1)

    # Force the issue_url to match what we fetched (defense against Haiku rewriting it)
    data["issue_url"] = issue_url

    NEURON_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(f"[neuron] wrote {NEURON_FILE}")
    print(f"[neuron] date: {data.get('date')} | headline: {data.get('headline')[:80]}")
    print(f"[neuron] {len(data['bullets'])} bullets, {len(data.get('around_the_horn', []))} around-the-horn")


if __name__ == "__main__":
    main()
