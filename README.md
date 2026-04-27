# myAInews

Automated AI news curator for the myAI app.

Runs hourly on GitHub Actions, fetches 13 AI feeds, filters and curates with Claude Haiku 4.5, publishes `news.json`. The myAI app reads that single JSON instead of fetching 20 RSS feeds itself.

---

## What's in this repo

| File | Purpose |
|------|---------|
| `feeds.yml` | The 13 sources + curation config — edit this to add/remove feeds |
| `curate.py` | Main curator script (Python) |
| `requirements.txt` | Python deps |
| `.github/workflows/curate.yml` | Hourly cron + manual trigger |
| `dashboard.html` | Web dashboard (password-gated) |
| `news.json` | Output, regenerated each run, served via GitHub Pages |
| `run_log.json` | Last 200 runs (cost, kept/dropped counts) |

---

## Setup — once

1. **Upload all files** to this repo (drag & drop in browser):
   - On the repo's Code tab, click **"Add file" → "Upload files"**
   - Drag the entire contents of the unzipped folder, **including the `.github` folder**, into the upload area
   - Commit message: `initial curator setup` → **Commit changes**
   - GitHub will preserve the folder structure (`.github/workflows/curate.yml`)
   - If `.github` doesn't upload via drag-drop, use **"Add file → Create new file"** with filename `.github/workflows/curate.yml` and paste the contents

2. **Verify Pages is live**: visit `https://frankknebeljanssen-create.github.io/myAInews/news.json` → should return JSON (initially empty `items: []`)

3. **Trigger the first run manually**:
   - Go to the **Actions** tab of this repo
   - Left sidebar → click **"Curate news"**
   - Right side → **"Run workflow"** dropdown → green **"Run workflow"** button
   - Wait 1–3 minutes; reload the page; the run appears with a green check (or red X if something broke)

4. **Open the dashboard**: `https://frankknebeljanssen-create.github.io/myAInews/dashboard.html` → enter password → see curated items

After this, the workflow runs every hour at :00 automatically. You can re-trigger manually anytime via the Actions tab.

---

## Cost

- Caching by URL: items already curated cost **$0** on subsequent runs
- New items: ~$0.01 (filter) + ~$0.04 (400-word body) = **~$0.05 per kept item**
- Typical day: 5–15 new AI items across 13 feeds → **~$0.25–$0.75/day**, **~$8–$15/month**
- First run is the most expensive (~$1) because all 30 items are "new"
- Hard cap: max 30 new items processed per run (in `feeds.yml` → `max_new_per_run`)

Track actual cost in the dashboard's "Cost 24h / 30d" tiles.

---

## Editing feeds

Just edit `feeds.yml` directly in the GitHub web editor. The next run picks it up automatically — no redeploy needed.

To add a feed:
```yaml
- name: My New Source
  url: https://example.com/rss
  weight: medium
```

To prioritize specific topics (always keep, skip filter LLM call):
```yaml
- name: Some Source
  url: ...
  priority_keywords:
    - "what happened in ai today"
    - "weekly recap"
```

---

## Troubleshooting

**A feed shows errors in the run log.** RSS URLs change. Open the run output (Actions tab → click the run → curator step), find the failing feed, update its URL in `feeds.yml`. Bad feeds don't break the run — they're skipped and logged.

**The workflow fails with "permission denied" on push.** Repo → Settings → Actions → General → scroll to "Workflow permissions" → set to **"Read and write permissions"** → Save.

**Dashboard shows "no runs yet"** but Actions show successful runs. Hard-reload (Cmd+Shift+R) to bust the browser cache. Each successful run commits new `news.json` and `run_log.json`.

**Costs higher than expected.** Check the dashboard's run table — if "new items" is consistently >20, lower `max_new_per_run` in `feeds.yml`, or tighten filter strictness in `curate.py`.

---

## App integration

The myAI app reads:
```
https://frankknebeljanssen-create.github.io/myAInews/news.json
```

JSON shape:
```json
{
  "generated_at": "2026-04-27T14:00:00Z",
  "items": [
    {
      "id": "abc123def456",
      "url": "https://...",
      "headline": "...",
      "body": "...400 words...",
      "source": "Anthropic News",
      "published": "2026-04-27T13:30:00Z",
      "curated_at": "2026-04-27T14:00:23Z",
      "original_title": "..."
    }
  ]
}
```

User-side date filter (default 60 days) and pin logic (max 2 items in `localStorage`) live in the app, not here.
