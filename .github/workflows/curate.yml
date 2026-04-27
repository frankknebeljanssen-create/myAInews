"""myAInews curator — runs hourly via GitHub Actions.

Workflow:
  1. Load feeds.yml + previous news.json (cache by URL hash)
  2. Fetch all feeds; collect items
  3. For each NEW item (not in cache):
     a. Filter step: AI-relevant? (Haiku, ~50 tokens out)
     b. If keep: fetch article, write 400-word body + headline (Haiku, ~600 tokens out)
  4. Drop items older than cache_days, keep newest max_items
  5. Write news.json + run_log.json (committed back to repo by workflow)
"""

import os, sys, json, time, hashlib, re
from datetime import datetime, timezone, timedelta
from pathlib import Path
import feedparser
import yaml
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic

REPO_ROOT = Path(__file__).parent
NEWS_FILE = REPO_ROOT / "news.json"
LOG_FILE = REPO_ROOT / "run_log.json"
FEEDS_FILE = REPO_ROOT / "feeds.yml"

# Claude Haiku 4.5 pricing (USD per 1M tokens)
HAIKU_INPUT_COST = 0.80
HAIKU_OUTPUT_COST = 4.00
MODEL = "claude-haiku-4-5-20251001"

client = Anthropic()  # picks up ANTHROPIC_API_KEY from env


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config():
    with open(FEEDS_FILE) as f:
        return yaml.safe_load(f)


def load_existing_news():
    if NEWS_FILE.exists():
        try:
            with open(NEWS_FILE) as f:
                data = json.load(f)
            return data.get("items", [])
        except Exception as e:
            log(f"could not read news.json: {e}")
    return []


def url_id(url):
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def fetch_feed(feed_def):
    name = feed_def["name"]
    url = feed_def["url"]
    log(f"  fetching {name}")
    try:
        parsed = feedparser.parse(
            url,
            request_headers={"User-Agent": "myAInews/1.0 (+https://github.com/frankknebeljanssen-create/myAInews)"}
        )
        if parsed.bozo and not parsed.entries:
            log(f"    feed parse warning: {parsed.bozo_exception}")
        items = []
        for entry in parsed.entries[:25]:  # latest 25 per feed
            link = entry.get("link", "")
            if not link:
                continue
            published = entry.get("published_parsed") or entry.get("updated_parsed")
            if published:
                pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
            else:
                pub_dt = datetime.now(timezone.utc)
            items.append({
                "id": url_id(link),
                "url": link,
                "title": entry.get("title", "").strip(),
                "summary": entry.get("summary", "").strip(),
                "published": pub_dt.isoformat(),
                "source": name,
                "source_weight": feed_def.get("weight", "medium"),
                "priority_keywords": feed_def.get("priority_keywords", []),
            })
        log(f"    got {len(items)} items")
        return items
    except Exception as e:
        log(f"    ERROR fetching {name}: {e}")
        return []


def fetch_article_text(url, max_chars=8000):
    """Fetch article HTML and extract main text content."""
    try:
        r = requests.get(
            url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (compatible; myAInews/1.0)"}
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        article = soup.find("article") or soup.find("main") or soup.body
        if not article:
            return ""
        text = article.get_text(separator="\n", strip=True)
        text = re.sub(r"\n+", "\n", text)
        text = re.sub(r" +", " ", text)
        return text[:max_chars]
    except Exception as e:
        log(f"    fetch_article error: {e}")
        return ""


def filter_relevance(item, full_text):
    """Quick AI-relevance filter. Returns (keep, reason, usage)."""

    # Priority keyword fast-path (e.g. The Neuron's "what happened in ai today")
    if item.get("priority_keywords"):
        title_lower = item["title"].lower()
        for kw in item["priority_keywords"]:
            if kw.lower() in title_lower:
                return True, f"priority_kw:{kw}", None

    title = item["title"]
    summary = item.get("summary", "")[:500]
    snippet = full_text[:1500] if full_text else ""

    prompt = f"""You filter AI/tech news for an enterprise AI ticker. Strictness: medium.

KEEP if the article is about:
- AI research, models, products, deployments
- Major company moves in AI (launches, partnerships, acquisitions)
- AI policy/regulation that affects business
- Substantive tooling for agents, infrastructure, MLOps

DROP if it is:
- Pure PR / vendor fluff with no substance
- Listicles ("Top 10 AI tools to try")
- Stock/finance pieces only mentioning AI in passing
- Personality/celebrity gossip
- Rumor mills, recycled coverage
- Non-AI content that just uses "AI" as a buzzword

Title: {title}
Summary: {summary}
Snippet: {snippet}

Reply with exactly one line in format: KEEP: <5-word reason>  OR  DROP: <5-word reason>
Example: KEEP: anthropic launches new agent SDK"""

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=50,
            messages=[{"role": "user", "content": prompt}],
        )
        response = msg.content[0].text.strip()
        keep = response.upper().startswith("KEEP")
        return keep, response, msg.usage
    except Exception as e:
        log(f"    filter error: {e}")
        return False, f"error:{e}", None


def write_curated(item, full_text):
    """Generate curated headline + ~400 word body. Returns (headline, body, usage)."""
    title = item["title"]
    summary = item.get("summary", "")[:1000]
    article = full_text[:6000] if full_text else summary

    prompt = f"""You write for an enterprise AI news ticker. Tone: clear, substantive, no hype, no marketing fluff.

Source title: {title}

Source content:
{article}

Write a curated version with:
1. A new HEADLINE (max 80 chars, sharper than the original, signals what matters)
2. A BODY of approximately 400 words (~2 mobile screens). Structure:
   - Opening sentence: what happened, why it matters
   - 2-3 paragraphs of key facts, specifics, numbers, names
   - End with the concrete signal/implication for AI practitioners or enterprise readers

Avoid: marketing language, listicle format, padding phrases like "in this article we explore", clickbait, hedging.
Use: direct prose, specific numbers/names, plain English, paragraph breaks (not bullet points).

Reply in this exact format:
HEADLINE: <your headline>

BODY:
<your ~400-word body>"""

    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        response = msg.content[0].text.strip()
        m = re.match(r"HEADLINE:\s*(.+?)\n+BODY:\s*(.+)", response, re.DOTALL)
        if m:
            return m.group(1).strip(), m.group(2).strip(), msg.usage
        log(f"    parse fail; using original title")
        return title, response, msg.usage
    except Exception as e:
        log(f"    write error: {e}")
        return None, None, None


def calc_cost(usage):
    if not usage:
        return 0.0
    return (usage.input_tokens / 1_000_000) * HAIKU_INPUT_COST + \
           (usage.output_tokens / 1_000_000) * HAIKU_OUTPUT_COST


def main():
    log("=== curator run start ===")
    cfg = load_config()
    feed_defs = cfg["feeds"]
    config = cfg.get("config", {})
    max_items = config.get("max_items", 30)
    cache_days = config.get("cache_days", 90)
    max_new_per_run = config.get("max_new_per_run", 30)

    existing_items = load_existing_news()
    existing_ids = {it["id"] for it in existing_items}
    log(f"loaded {len(existing_items)} cached items")

    # 1. Fetch all feeds
    all_raw = []
    for fd in feed_defs:
        all_raw.extend(fetch_feed(fd))
    log(f"total fetched: {len(all_raw)} items")

    # 2. Identify new items
    new_items = [it for it in all_raw if it["id"] not in existing_ids]
    log(f"new items: {len(new_items)}")

    # Safety cap: process at most N newest per run
    if len(new_items) > max_new_per_run:
        log(f"  cap: processing only {max_new_per_run} newest")
        new_items.sort(key=lambda x: x["published"], reverse=True)
        new_items = new_items[:max_new_per_run]

    # 3. Filter + curate each
    total_cost = 0.0
    total_input = 0
    total_output = 0
    kept = 0
    dropped = 0
    errors = 0
    curated_new = []

    for item in new_items:
        log(f"processing: {item['title'][:60]}...")
        full_text = fetch_article_text(item["url"])

        keep, reason, usage = filter_relevance(item, full_text)
        if usage:
            total_cost += calc_cost(usage)
            total_input += usage.input_tokens
            total_output += usage.output_tokens

        if not keep:
            log(f"  DROP: {reason}")
            dropped += 1
            continue
        log(f"  KEEP: {reason}")

        headline, body, usage2 = write_curated(item, full_text)
        if usage2:
            total_cost += calc_cost(usage2)
            total_input += usage2.input_tokens
            total_output += usage2.output_tokens

        if not headline or not body:
            errors += 1
            continue

        curated_new.append({
            "id": item["id"],
            "url": item["url"],
            "headline": headline,
            "body": body,
            "source": item["source"],
            "published": item["published"],
            "curated_at": datetime.now(timezone.utc).isoformat(),
            "original_title": item["title"],
        })
        kept += 1

    # 4. Merge + dedupe + age cutoff
    all_items = existing_items + curated_new
    cutoff = datetime.now(timezone.utc) - timedelta(days=cache_days)

    def parse_dt(s):
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)

    all_items = [it for it in all_items if parse_dt(it["published"]) > cutoff]
    all_items.sort(key=lambda x: x["published"], reverse=True)
    all_items = all_items[:max_items]

    # 5. Write news.json
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": all_items,
    }
    with open(NEWS_FILE, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # 6. Append run log
    log_entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "feeds_checked": len(feed_defs),
        "items_fetched": len(all_raw),
        "new_items": len(new_items),
        "kept": kept,
        "dropped": dropped,
        "errors": errors,
        "input_tokens": total_input,
        "output_tokens": total_output,
        "cost_usd": round(total_cost, 4),
        "total_in_news": len(all_items),
    }

    log_data = []
    if LOG_FILE.exists():
        try:
            with open(LOG_FILE) as f:
                log_data = json.load(f)
        except Exception:
            log_data = []
    log_data.append(log_entry)
    log_data = log_data[-200:]  # keep last 200 runs (~8 days)
    with open(LOG_FILE, "w") as f:
        json.dump(log_data, f, indent=2)

    log(f"=== done: kept={kept} dropped={dropped} errors={errors} cost=${total_cost:.4f} ===")


if __name__ == "__main__":
    main()
