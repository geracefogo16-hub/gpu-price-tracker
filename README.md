# ⚡ GPU Price Tracker — gpus.io

An automated daily price tracker for cloud **GPU rental** prices, scraped from
[gpus.io](https://gpus.io). It records prices for **A100, H100, H200, B200, and
B300**, stores them in a local SQLite database, and serves an interactive web
dashboard with price-over-time charts and day-over-day deltas. A macOS cron job
runs the scrape once a day.

---

## What it tracks

gpus.io is a cloud-GPU rental price **aggregator** (≈19 providers). For each of
the five target models the scraper records, per day, several **price tiers**
(all prices in **USD per GPU per hour**):

| Category | Meaning |
|---|---|
| `Cheapest` | Cheapest available offer across all providers/rental types |
| `On-demand (min)` / `(median)` | Min / median across on-demand offers |
| `Spot (min)` / `(median)` | Min / median across spot offers |
| `<Provider> · <Rental type>` | Cheapest offer per **vendor + rental type** (e.g. `Vast.ai · Spot`) — the "by vendor / configuration" breakdown |

Each record also keeps the representative `provider`, `rental_type`,
`gpu_count`, per-GPU `vram_gb`, and `regions` for context.

### A note on the data source (why no headless browser by default)

gpus.io is a Next.js site, so it looks JavaScript-rendered. However, each
**per-model page** (e.g. `https://gpus.io/en/gpus/h100`) *server-renders* its
full pricing dataset into the initial HTML (a React Server Components payload).
The scraper reconstructs and parses that payload with plain `requests`, which is
faster and more polite than driving a browser.

A **Playwright (headless Chromium) fallback** is included and **auto-engages**
if that server-rendered payload is ever missing (e.g. the site switches to
client-only rendering). `setup.sh` installs it; the scraper runs fine without it.

### Fields that are / aren't available

- ✅ **Available:** model, provider/vendor, hourly price, currency (USD), rental
  type (on-demand / spot), GPU count, per-GPU VRAM, regions.
- ➖ **Currency** is always `USD` — that is gpus.io's source-of-truth field
  (`pricePerGpuHour.usd`). The site only converts to other currencies for
  *display* via a live FX endpoint, so we store the canonical USD value.
- ➖ **Per-GPU memory** is effectively fixed per model (A100/H100 = 80 GB,
  H200 = 141 GB, B200 = 180 GB, B300 = 288 GB), so the meaningful "category"
  variation the site exposes is **provider** and **rental type** (captured above).
- ➖ **Reserved / committed-term** pricing columns exist in the data model
  (`commitment`) but are rarely populated for these models day-to-day; they are
  stored when present.

---

## Requirements

- **macOS** (Apple Silicon or Intel).
- **Python 3.10+** recommended. *(The code is written to also run on 3.9; if no
  3.10+ interpreter is found, `setup.sh` falls back to 3.9 and prints a note.)*
- No paid APIs. Everything runs locally.

---

## Quick start

Daily price collection runs **in the cloud** (GitHub Actions — see
[Scheduling](#scheduling--runs-in-the-cloud-github-actions)), so you don't need
`setup.sh` for scheduling. `setup.sh` just builds the local environment for
*viewing* the dashboard:

```bash
cd "GPU Scraper"
./setup.sh --no-cron       # creates .venv and installs the dashboard's deps
```

Then start the dashboard. **Easiest way:** double-click **`Start Dashboard.command`**
in Finder — it pulls the latest cloud-collected data, launches the dashboard using
the project's own Python, and opens your browser. (Keep the window open; press
Ctrl+C or close it to stop.)

Or from a terminal — note you must use the venv's Python, **not** a bare
`python`/`python3` (those may point at Anaconda/Homebrew, which don't have the
dependencies installed):

```bash
./.venv/bin/python dashboard.py
# → http://localhost:5050
```

That's it. The dashboard reads whatever the scraper has collected so far. After
a few days you'll see real trend lines.

> **Why 5050 and not 5000?** On macOS, port 5000 is occupied by Control Center /
> AirPlay Receiver (it even answers on IPv6 `::1`, where `localhost` resolves
> first), so `localhost:5000` shows AirPlay's "403 Forbidden" instead of the
> dashboard. The dashboard defaults to **5050** to avoid this, and auto-falls
> back to the next free port if 5050 is taken too. Override with `--port`.

### Files

| File | Purpose |
|---|---|
| `scraper.py` | Scrapes gpus.io and writes to `data/gpu_prices.db` |
| `dashboard.py` | Flask + Plotly web dashboard (localhost:5050) |
| `setup.sh` | Installs dependencies and registers the daily cron job |
| `requirements.txt` | Python dependencies |
| `data/gpu_prices.db` | SQLite database (created on first scrape) |
| `logs/scraper.log` | Cron run output (success/errors) |

---

## The dashboard

- **Time-series chart** — price over time, one line per model/config.
- **Toggle lines** — click a legend entry to hide/show it; double-click to
  isolate one. Use the **GPU models** checkboxes to filter, and switch between
  **Price tiers** and **Vendor breakdown** views.
- **Summary table** — latest scrape vs. the previous one, with Δ and Δ%
  (green = price dropped, red = price rose).
- **Refresh** — a manual **↻ Refresh** button, an optional **auto-refresh every
  60s** toggle, and a **⟳ Scrape now** button that runs the scraper on demand.

Run on a different port/host:

```bash
./.venv/bin/python dashboard.py --port 8000 --host 0.0.0.0
```

---

## The scraper (run manually)

```bash
./.venv/bin/python scraper.py                 # all five models
./.venv/bin/python scraper.py --models H100 B200
./.venv/bin/python scraper.py --no-fallback   # disable the Playwright fallback
./.venv/bin/python scraper.py --help
```

Re-running on the same day **overwrites** that day's rows (idempotent), so it's
safe to run as often as you like. Exit code is non-zero if any model failed,
which makes failures visible in the cron log.

**Polite scraping:** the scraper rotates a small pool of realistic user agents,
sets normal browser headers, waits a random 2–5 s between model requests, and
retries with backoff. It fetches just five pages per day.

---

## Scheduling — runs in the cloud (GitHub Actions)

The daily scrape runs on **GitHub's servers**, not your Mac — so it happens
whether your Mac is on, asleep, or off. The workflow is
[`.github/workflows/daily-scrape.yml`](.github/workflows/daily-scrape.yml):

1. Every day (cron `0 1 * * *` UTC ≈ 9 PM US-Eastern) GitHub runs `scraper.py`.
2. It commits the updated `data/gpu_prices.db` and `data/prices.csv` back to the repo.
3. Your Mac gets that data automatically — **`Start Dashboard.command`** runs
   `git pull` before opening the dashboard.

So the whole routine is: **double-click `Start Dashboard.command`** whenever you
want to look. No need to keep your Mac awake.

### View / run the workflow manually

- On GitHub: the repo's **Actions** tab → *daily-gpu-scrape* → **Run workflow**.
- From a terminal: `gh workflow run daily-scrape.yml` then `gh run watch`.

### Change the cloud schedule

Edit the `cron:` line in `.github/workflows/daily-scrape.yml` and push:

```bash
git commit -am "scrape at 13:00 UTC" && git push
```

GitHub cron is always **UTC** and does not observe daylight saving; scheduled
runs can be delayed a few minutes to ~1 h under load (fine for a daily snapshot).

### Optional: a local cron job instead

If you'd rather collect on *this Mac* (only useful while it's awake at the set
time), `setup.sh` can register a local `cron` job:

```bash
./setup.sh --time 22:00      # daily at 10 PM, logs to logs/scraper.log
```

Caveats: macOS `cron` silently skips runs while the Mac is asleep, and it writes
to the same `data/gpu_prices.db` the cloud commits — running both can cause
`git pull` conflicts. Pick one; the cloud workflow is recommended. Remove a local
job with `crontab -l | grep -v gpus-io-tracker | crontab -`. (If `cron` runs but
`logs/scraper.log` stays empty, grant **Full Disk Access** to `/usr/sbin/cron` in
System Settings → Privacy & Security.)

---

## Database

SQLite at `data/gpu_prices.db`, single table `prices`:

| column | notes |
|---|---|
| `scrape_date` | `YYYY-MM-DD` (local date) |
| `scraped_at` | full ISO-8601 timestamp |
| `gpu_model` | A100 / H100 / H200 / B200 / B300 |
| `category` | price tier / config label (see table above) |
| `price` | USD per GPU per hour |
| `currency` | always `USD` |
| `unit` | `per GPU per hour` |
| `provider`, `rental_type`, `gpu_count`, `vram_gb`, `regions` | context for the row |
| `source_url` | the scraped page |

`UNIQUE(scrape_date, gpu_model, category)` enforces one row per tier per day.

Quick peek:

```bash
sqlite3 data/gpu_prices.db \
  "SELECT scrape_date, gpu_model, price FROM prices WHERE category='Cheapest' ORDER BY scrape_date;"
```

---

## Troubleshooting

- **"Python 3.10+ not found"** — `setup.sh` falls back to 3.9 automatically. To
  match the target, `brew install python@3.12` (or grab it from python.org),
  delete `.venv`, and re-run `./setup.sh`.
- **Dashboard port** — defaults to **5050** (port 5000 is taken by macOS AirPlay
  Receiver). It auto-falls back to the next free port if 5050 is busy, and prints
  the URL it actually used. Force a specific port with
  `./.venv/bin/python dashboard.py --port 8000`.
- **Dashboard shows "No data yet"** — run `./.venv/bin/python scraper.py` once.
- **Scrape returns no data** — gpus.io may have changed. The scraper will try
  the Playwright fallback automatically; ensure Chromium is installed with
  `./.venv/bin/python -m playwright install chromium`.
- **cron job not running** — see the Full Disk Access note above; confirm with
  `crontab -l`.
