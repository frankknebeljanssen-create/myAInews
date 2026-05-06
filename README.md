# Phase B — Daily auto-update for myAInews

These files belong in your **`frankknebeljanssen-create/myAInews`** GitHub repo (the one
that hosts `news.json` / `neuron.json` on GitHub Pages), not in the myAI app folder.

## Layout to copy

Copy the contents of this directory into the **root of your `myAInews` repo**:

```
myAInews/
├── .github/
│   └── workflows/
│       └── daily-update.yml      ← new
├── scripts/
│   ├── requirements.txt          ← new
│   ├── generate_news.py          ← new
│   └── generate_neuron.py        ← new
├── news.json                      (existing — will be overwritten daily)
└── neuron.json                    (existing — will be overwritten daily)
```

## What it does

* **Schedule**: runs every day at **05:00 UTC** (= 06:00 CET / 07:00 CEST), shortly
  before morning users open the app.
* **`generate_news.py`** fetches RSS from 13 AI sources (Anthropic, OpenAI, Google AI,
  DeepMind, MIT Tech Review, VentureBeat, TechCrunch, Import AI, The Neuron, Mistral,
  Meta AI, Hugging Face, AI Index). Filters non-AI items by keyword, deduplicates by
  URL, sorts newest-first, keeps top 30. Writes `news.json`.
* **`generate_neuron.py`** fetches the latest issue of *The Neuron Daily* via its RSS
  feed, parses the HTML (BeautifulSoup), extracts emoji + headline + bullets +
  main_story (title / body / why / take). Promotes the previous day's bullets into a
  `previous` block. Writes `neuron.json`.
* **Commit step** auto-commits any changes back to the repo as user `myai-bot`.
  GitHub Pages picks up the new files within ~60 seconds.

## One-time setup

1. Drop these files into the `myAInews` repo (mirror the structure above).
2. `git add . && git commit -m "ci: add daily news+neuron auto-update" && git push`
3. On GitHub: **Settings → Actions → General → Workflow permissions** must be
   *"Read and write permissions"* (so the workflow can `git push` back). If it is
   currently *"Read-only"*, switch to read-write.
4. Test it once manually: **Actions → Daily news + neuron update → Run workflow**.
   Verify that `news.json` and `neuron.json` got updated and the commit appears in
   the repo's history.

## Customisation

* **Add / remove news sources** → edit the `SOURCES` list at the top of
  `generate_news.py`.
* **Change schedule** → edit the `cron` line in `daily-update.yml`. GitHub Actions
  uses standard cron syntax (UTC).
* **Change item caps** → `MAX_ITEMS_TOTAL` and `MAX_ITEMS_PER_FEED` constants in
  `generate_news.py`.
* **Twice a day** → add a second `cron` entry (e.g. one at 05:00, one at 17:00 UTC).

## Failure modes

* **Source RSS feed dies** → that source's entries vanish from news.json silently;
  other sources still come through. No crash.
* **Neuron RSS dies** → workflow logs the error but doesn't block (uses
  `continue-on-error: true`). The existing `neuron.json` stays as-is.
* **HTML structure of Neuron changes** → `generate_neuron.py` may extract garbage.
  Add defensive checks if you see weird output.

## Monitoring

* GitHub Actions has email notifications on workflow failure (configurable in
  account settings). 
* In the app: the staleness badge `⚠ Nd` shows when the Today-in-AI data is older
  than 2 days. If you see that, the cron probably failed for ≥2 days — check the
  Actions tab.
