"""Update models.json — runs daily via GitHub Actions.

Workflow:
  1. Fetch OpenRouter /api/v1/models (single API call, public endpoint)
  2. Filter to Big Five flagship models by ID
  3. Convert pricing + context to display format
  4. Compare to existing models.json — skip write if no functional change
  5. Write models.json (committed back to repo by workflow)
"""
import os, sys, json, requests
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).parent
MODELS_FILE = REPO_ROOT / "models.json"

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY", "")

# Big Five flagship models. Update IDs when a new flagship ships.
# Verify current IDs at: https://openrouter.ai/models
BIG_FIVE = [
    {"id": "openai/gpt-5",                 "provider": "OpenAI",    "color": "#10A37F"},
    {"id": "anthropic/claude-opus-4.7",    "provider": "Anthropic", "color": "#D97757"},
    {"id": "google/gemini-3.1-pro-preview","provider": "Google",    "color": "#4285F4"},
    {"id": "x-ai/grok-4",                  "provider": "xAI",       "color": "#6B7280"},
    {"id": "deepseek/deepseek-v4",         "provider": "DeepSeek",  "color": "#4D6BFE"},
]

# When fuzzy-matching falls back, exclude IDs containing these markers —
# they're variants/modalities, not text-flagship models.
EXCLUDE_MARKERS = [
    "-mini", "-free", "-nano", "-lite", "-tiny", "-flash", "-haiku", "-instant",
    "image", "audio", "tts", "embedding", "video", "lyria", "veo", "imagen", "whisper",
]


def is_text_flagship(model_id):
    """True if the ID looks like a text-flagship (no modality/variant markers)."""
    lower = model_id.lower()
    return not any(marker in lower for marker in EXCLUDE_MARKERS)


def fetch_openrouter():
    """Fetch the public model catalog. Auth optional but reduces rate-limit risk."""
    headers = {
        "User-Agent": "myAInews-bot/1.0 (+https://github.com/frankknebeljanssen-create/myAInews)",
        "Accept": "application/json",
    }
    if OPENROUTER_KEY:
        headers["Authorization"] = f"Bearer {OPENROUTER_KEY}"
    r = requests.get(OPENROUTER_URL, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json().get("data", [])


def fmt_context(n):
    """Render context window size compactly + consistently.
    200000 -> '200k', 1000000 -> '1M', 1048576 -> '1M' (snaps near-integers),
    1500000 -> '1.5M', 2000000 -> '2M', 10000000 -> '10M'"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "?"
    if n <= 0:
        return "?"
    if n >= 1_000_000:
        v = n / 1_000_000
        v_int = round(v)
        # If close to a whole number (within 10%), show as integer — keeps "1M" not "1.0M"
        if abs(v - v_int) < 0.1:
            return f"{v_int}M"
        return f"{v:.1f}M"
    if n >= 1000:
        return f"{int(round(n / 1000))}k"
    return str(n)


def fmt_price(p):
    """OpenRouter prices are USD per token (string). Display as input price per 1M tokens."""
    try:
        per_token = float(p)
    except (TypeError, ValueError):
        return "?"
    per_million = per_token * 1_000_000
    if per_million == 0:
        return "free"
    if per_million >= 10:
        return f"${per_million:.0f}/M"
    if per_million >= 1:
        return f"${per_million:.2f}/M"
    if per_million >= 0.01:
        return f"${per_million:.2f}/M"
    return f"${per_million:.4f}/M"


def find_model(or_models, target_id):
    """Find best match for target ID. Tries exact match, then fuzzy by author+slug prefix.
    Excludes image/audio/video/variant IDs to avoid matching wrong modalities."""
    # exact match — always preferred
    for m in or_models:
        if m.get("id") == target_id:
            return m
    # fuzzy: same author/, slug starts-with, but only text-flagship candidates
    author, _, slug = target_id.partition("/")
    if not author or not slug:
        return None
    starts_with = [
        m for m in or_models
        if m.get("id", "").startswith(f"{author}/{slug}") and is_text_flagship(m.get("id", ""))
    ]
    if starts_with:
        # prefer the shortest matching ID (likely base flagship, not variants)
        return sorted(starts_with, key=lambda m: len(m.get("id", "")))[0]
    # final fallback: any text-flagship from same author
    same_author = [
        m for m in or_models
        if m.get("id", "").startswith(f"{author}/") and is_text_flagship(m.get("id", ""))
    ]
    return sorted(same_author, key=lambda m: len(m.get("id", "")))[0] if same_author else None


def clean_name(raw_name, provider):
    """OpenRouter prepends 'Provider: '; strip it for compact display."""
    if not raw_name:
        return ""
    prefix = f"{provider}: "
    if raw_name.startswith(prefix):
        return raw_name[len(prefix):]
    # also handle e.g. "OpenAI: GPT-5" when provider config says "OpenAI"
    return raw_name.split(": ", 1)[-1] if ": " in raw_name else raw_name


def main():
    print(f"[models] auth: {'with key' if OPENROUTER_KEY else 'public (no key)'}")
    print(f"[models] fetching {OPENROUTER_URL}...")
    try:
        or_models = fetch_openrouter()
    except Exception as e:
        print(f"[models] FAIL: fetch error: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"[models] received {len(or_models)} models from OpenRouter")

    out_models = []
    for cfg in BIG_FIVE:
        m = find_model(or_models, cfg["id"])
        if not m:
            print(f"[models] WARN: no match for {cfg['id']}, skipping")
            continue
        pricing = m.get("pricing") or {}
        actual_id = m.get("id", "?")
        out_models.append({
            "provider": cfg["provider"],
            "model": clean_name(m.get("name"), cfg["provider"]) or actual_id,
            "context": fmt_context(m.get("context_length")),
            "price": fmt_price(pricing.get("prompt")),
            "color": cfg["color"],
        })
        last = out_models[-1]
        match_note = "" if actual_id == cfg["id"] else f"  (matched: {actual_id})"
        print(f"[models] {cfg['provider']:>10s}: {last['model']} | ctx {last['context']} | in {last['price']}{match_note}")

    if len(out_models) < 3:
        print(f"[models] FAIL: only {len(out_models)} matches found, refusing to overwrite", file=sys.stderr)
        sys.exit(1)

    payload = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "note": "Big Five frontier models. Auto-updated daily from OpenRouter /v1/models.",
        "models": out_models,
    }

    # Compare to existing — skip write if no functional change (avoid empty commits)
    if MODELS_FILE.exists():
        try:
            old = json.loads(MODELS_FILE.read_text())
            if old.get("models") == out_models:
                print("[models] no changes vs current models.json, skipping write")
                return
        except Exception:
            pass  # malformed existing file, overwrite

    MODELS_FILE.write_text(json.dumps(payload, indent=2) + "\n")
    print(f"[models] wrote {MODELS_FILE} ({len(out_models)} models)")


if __name__ == "__main__":
    main()
