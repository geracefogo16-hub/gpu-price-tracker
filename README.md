# computeprices.com daily price tracker

Cloud-hosted daily tracker for **GPU rental prices** and **LLM API prices**
from [computeprices.com](https://computeprices.com). Runs entirely on GitHub
Actions (zero dependence on any laptop), stores history in this repo keyed by
the **data's own update date**, and publishes an interactive dashboard via
GitHub Pages — viewable from a phone or any browser.

> The older gpus.io scraper still lives in this repo
> ([README-gpusio-legacy.md](README-gpusio-legacy.md), `scraper.py`,
> `.github/workflows/daily-scrape.yml`); the two trackers are independent.

## Architecture (git scraping)

- **Source: the official free JSON API** (`/api/v1/...`) — not HTML scraping.
  The site's `/gpu` and `/inference` pages are client-rendered Next.js, and
  the API is explicitly designed for automated tools.
  Docs: <https://computeprices.com/docs/api>.
- **[`collect.yml`](.github/workflows/collect.yml)** runs twice daily
  (04:17 & 09:17 UTC — GitHub cron is UTC, can be delayed or occasionally
  skipped, hence two triggers off minute 0). Runs are **idempotent**: the
  second trigger is a no-op when the first succeeded, a recovery when it
  didn't. Manual runs: *Actions → collect-computeprices → Run workflow*.
- **Always-commit heartbeat:** every run rewrites `data/last_run.json`, so
  every run commits. That keeps GitHub's **60-day scheduled-workflow
  inactivity timer** permanently reset and gives a visible pulse.
- **Failures are loud:** if the API collection fails, the job fails and
  GitHub emails you; the second cron retries. Only the optional benchmark
  scrape is `continue-on-error`.
- A run uses **~5 API requests** (3 catalogs + 2 unfiltered price pulls) —
  far below the keyless 60/hour budget — with a descriptive `User-Agent`,
  `Retry-After` honored on 429, and exponential backoff with jitter.

### Public vs private repo

This repo should stay **public**: unlimited free Actions minutes on standard
runners, free GitHub Pages, and the data is public pricing — nothing
sensitive. If you make it private: free accounts get a limited monthly
Actions-minutes allowance (a ~3-minute daily run still fits easily), but
**GitHub Pages requires a paid plan for private repos** — you'd open
`docs/index.html` from a local clone instead.

## Data layout & the date-correctness rule

| Path | What it is |
|---|---|
| `raw/YYYY/MM/DD/*.json` | Canonicalized raw API snapshots — immutable source of truth; everything below can be rebuilt from these |
| `data/gpu_prices.csv` | One row per `(effective_date, provider_slug, gpu_slug, gpu_count, pricing_type, commitment_months)` |
| `data/llm_prices.csv` | One row per `(effective_date, provider_slug, model_slug, pricing_type)` |
| `data/llm_benchmarks.csv` | Best-effort speed (tok/s) and $/perf from `/inference` (nullable; never blocks prices) |
| `data/daily_presence.csv` | Which series appeared in each day's pull — distinguishes *provider disappeared* from *price unchanged* |
| `data/provider_registry.csv` | `slug, display_name, first_seen, last_seen, is_active`; new slugs auto-register |
| `data/runs.csv` + `data/last_run.json` | Run log + heartbeat (status, counts, suspicious flags) |
| `data/rejected.ndjson` | Quarantined rows (raw JSON + reason); one bad row never aborts a run |
| `docs/` | Generated dashboard (GitHub Pages) |

**Date attribution:** a price belongs to `DATE(last_updated)` in **UTC** —
the data's own update date, never the collection date. Rows in one response
range from same-day to weeks stale, so upserts only overwrite when the
incoming `last_updated` is *strictly newer*. Running the collector twice
back-to-back changes nothing in the data tables the second time.

**Honest limitation:** the public API exposes only *latest* prices (plus
cross-provider daily *averages* via `/trends`). If every trigger in a day
fails, that day's per-provider prices are unrecoverable — the dashboard
shows a gap rather than interpolating. The dual-cron schedule on
GitHub-hosted runners makes this rare.

## Dashboard

Enable Pages (below) and open `https://<user>.github.io/<repo>/`.

- **Overview**: run-health panel (last run, status, row counts, suspicious
  flags, new providers) + links to every GPU and LLM page.
- **Per-GPU pages**: $/GPU/hr over time, one line per (provider, config),
  labeled like "CoreWeave 8x". Tabs for **Demand / Spot / Reserved**;
  featured series (from `watchlist.yaml`) shown by default, a
  *Show all providers* button reveals the rest. True gaps (from
  `daily_presence`) break the line — no interpolation. Hover shows the
  quote's own `last_updated`, so fresh quotes are distinguishable from stale.
- **Per-LLM pages**: input/output $/1M per provider (standard pricing by
  default, batch when present), plus speed and $/perf charts where benchmark
  data exists.
- Pages stay lightweight: HTML fetches small per-page JSON from
  `docs/data/` (same Pages origin) instead of embedding data.

## One-time setup

1. **Enable GitHub Pages**: repo *Settings → Pages → Source: Deploy from a
   branch → Branch: `main`, folder `/docs`*. The dashboard URL appears there.
2. *(Optional)* **API key**: get a free key from computeprices.com (5,000
   req/hr vs 60 keyless) and add it as repo secret
   `COMPUTEPRICES_API_KEY` (*Settings → Secrets and variables → Actions*).
   Everything works fine without it.
3. **First run**: *Actions → collect-computeprices → Run workflow*. After it
   commits, the Pages site updates within a minute or two.

## Operating it

- **Add/remove a GPU, model, or featured provider**: edit
  [`watchlist.yaml`](watchlist.yaml). GPU keys are computeprices slugs; if
  you don't know the exact slug, set `resolve_name: "<display name>"` and it
  is resolved against the live catalog every run (never guessed — a wrong
  slug filter has been observed to return a *different* GPU's data, which is
  also why every ingested row's slug is validated). The collector ingests
  **everything** for tracked slugs — all providers/configs/pricing types —
  so the watchlist only controls what is *featured* in the dashboard.
- **Change the schedule**: edit the two `cron:` lines in
  [`collect.yml`](.github/workflows/collect.yml) (UTC).
- **Local commands**:
  ```bash
  pip install -r requirements.txt
  python collector.py --dry-run     # fetch + validate, print, write nothing
  python collector.py --once        # one full collection run
  python build_dashboard.py         # regenerate docs/
  python rebuild_db.py              # build data/cache.db (SQLite, gitignored)
  python rebuild_db.py --from-raw   # regenerate the CSVs from raw/ snapshots,
                                    # then build the SQLite cache
  python verify_api.py              # re-run the API assumption checks
  pytest                            # unit tests (no network needed)
  ```

## Robustness notes

The site is young and evolving fast (31 → 83 providers within months), so the
collector is defensive by design: catalogs are refreshed every run and a
vanished watchlist slug WARNs loudly while history stays intact; unknown
extra fields are tolerated (`meta.version` is checked, additive-only changes
are promised with a 6-month deprecation window); unknown `pricing_type`
values are stored as-is; *reserved* pricing is recognized both as a literal
`pricing_type` and as rows carrying `commitment_months` (which is part of
record identity either way); structurally broken rows are quarantined to
`data/rejected.ndjson` while suspicious-but-real values (price ≤ 0,
per-GPU > $200/hr, `gpu_count` outside 1–8 — odd-real values like 3 exist)
are ingested and flagged; duplicates within a response keep the newest
`last_updated`; and if a tracked GPU's row count drops >50% vs the trailing
7-run median the run completes but is marked **suspicious** in `runs.csv`
and on the dashboard health panel.
