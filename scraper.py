#!/usr/bin/env python3
"""
scraper.py — Daily cloud-GPU rental price scraper for gpus.io
=============================================================

Scrapes the per-model pages on https://gpus.io for the target data-center GPUs
(A100, H100, H200, B200, B300) and writes daily price tiers into a local SQLite
database (data/gpu_prices.db).

WHY requests INSTEAD OF Playwright (by default)
-----------------------------------------------
gpus.io is a Next.js application, so at first glance it looks like a
JavaScript-rendered site.  However, each *per-model* page
(e.g. https://gpus.io/en/gpus/h100) SERVER-RENDERS its full pricing dataset into
the initial HTML as a React Server Components ("flight") payload — the
``self.__next_f.push([...])`` script calls.  We reconstruct that payload and
parse it directly with ``requests``, which is faster, lighter and more polite
than driving a headless browser.

A Playwright fallback (``fetch_html_playwright``) auto-engages if the
server-rendered payload is ever missing (for example, if the site moves to
client-only rendering).  ``setup.sh`` installs Playwright + Chromium so the
fallback works; the scraper still runs fine without them (it just logs that the
fallback is unavailable).

WHAT GETS STORED
----------------
For every model we record several price *categories* (tiers) per day:

  * "Cheapest"                – cheapest available offer across all providers
  * "On-demand (min)" / "(median)"  – aggregate over on-demand offers
  * "Spot (min)" / "(median)"       – aggregate over spot offers
  * "<Provider> · <Rental type>"    – cheapest offer per vendor + rental type

All prices are USD per GPU per hour (the site's source-of-truth field is
``pricePerGpuHour.usd``).  See the README for field notes.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sqlite3
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "gpu_prices.db"

# (url-slug, display name).  Order is preserved in the dashboard.
MODELS = [
    ("a100", "A100"),
    ("h100", "H100"),
    ("h200", "H200"),
    ("b200", "B200"),
    ("b300", "B300"),
]

MODEL_URL = "https://gpus.io/en/gpus/{slug}"

# Polite scraping knobs ------------------------------------------------------ #
REQUEST_TIMEOUT = 30          # seconds
MAX_RETRIES = 3               # per page
RETRY_BACKOFF = 4             # seconds, multiplied by attempt number
MIN_DELAY, MAX_DELAY = 2.0, 5.0   # random delay between model requests

# A small pool of realistic desktop user agents to rotate through.
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) "
    "Gecko/20100101 Firefox/123.0",
]

PRETTY_RENTAL = {
    "on_demand": "On-demand",
    "spot": "Spot",
    "reserved": "Reserved",
    "preemptible": "Preemptible",
}


def pretty_rental(rt: str | None) -> str:
    if not rt:
        return "Unknown"
    return PRETTY_RENTAL.get(rt, rt.replace("_", " ").title())


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #

def get_logger(quiet: bool = False) -> logging.Logger:
    logger = logging.getLogger("gpus-scraper")
    if logger.handlers:               # already configured (e.g. re-import)
        return logger
    logger.setLevel(logging.WARNING if quiet else logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    logger.addHandler(handler)
    return logger


# --------------------------------------------------------------------------- #
# Networking
# --------------------------------------------------------------------------- #

def fetch_html(slug: str, logger: logging.Logger) -> str | None:
    """Fetch a per-model page with rotating UA, retries and polite backoff."""
    import requests  # imported lazily so `--help` works without deps installed

    url = MODEL_URL.format(slug=slug)
    for attempt in range(1, MAX_RETRIES + 1):
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
            "Referer": "https://gpus.io/en/gpus",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and resp.text:
                return resp.text
            logger.warning("[%s] HTTP %s (attempt %d/%d)",
                           slug, resp.status_code, attempt, MAX_RETRIES)
        except Exception as exc:  # noqa: BLE001 - network errors are expected
            logger.warning("[%s] request error: %s (attempt %d/%d)",
                           slug, exc, attempt, MAX_RETRIES)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    return None


def fetch_html_playwright(slug: str, logger: logging.Logger) -> str | None:
    """Fallback: render the page in headless Chromium and return the HTML.

    Only used when the plain-``requests`` payload is missing the data.  Reuses
    the same RSC parser on the fully-rendered document, and also captures any
    JSON responses the page fetches (so a future client-side API would still be
    parsed).  Requires `pip install playwright` + `playwright install chromium`.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("[%s] Playwright not installed; browser fallback unavailable. "
                       "Run `playwright install chromium` (see setup.sh).", slug)
        return None

    url = MODEL_URL.format(slug=slug)
    captured: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent=random.choice(USER_AGENTS), locale="en-US")
            page = context.new_page()

            def _on_response(response):
                try:
                    ct = response.headers.get("content-type", "")
                    if "application/json" in ct:
                        body = response.text()
                        if "pricePerGpuHour" in body or "groupKey" in body:
                            captured.append(body)
                except Exception:  # noqa: BLE001 - best effort capture
                    pass

            page.on("response", _on_response)
            page.goto(url, wait_until="networkidle", timeout=60_000)
            page.wait_for_timeout(2_000)
            html = page.content()
            browser.close()
        return html + "\n".join(captured)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[%s] Playwright fallback failed: %s", slug, exc)
        return None


# --------------------------------------------------------------------------- #
# Parsing  (React Server Components "flight" payload)
# --------------------------------------------------------------------------- #

_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[\d+,\s*("(?:[^"\\]|\\.)*")\]\)', re.S)
_PROVIDER_RE = re.compile(r'"provider":\{"id":"([^"]+)","name":("(?:[^"\\]|\\.)*")')
_GROUP_RE = re.compile(r'\{"groupKey":')


def reconstruct_rsc(html: str) -> str:
    """Concatenate every ``self.__next_f.push([_, "..."])`` string fragment."""
    parts = []
    for raw in _PUSH_RE.findall(html):
        try:
            parts.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return "".join(parts)


def _match_object(s: str, start: int) -> str | None:
    """Return the balanced ``{...}`` substring beginning at ``start`` (a '{').

    String-aware so braces inside JSON strings (URLs, descriptions) don't break
    the depth count.
    """
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i + 1]
    return None


def _provider_name_map(stream: str) -> dict:
    """Build providerId -> display name from inline provider definitions.

    RSC de-duplicates repeated objects as reference strings (e.g.
    ``"provider":"$1f"``), so a given group may not carry the full provider
    object.  Every provider is defined inline at least once, though, so we
    harvest all definitions up front and resolve by id via the group key.
    """
    mapping: dict = {}
    for pid, name_json in _PROVIDER_RE.findall(stream):
        if pid in mapping:
            continue
        try:
            mapping[pid] = json.loads(name_json)
        except json.JSONDecodeError:
            mapping[pid] = pid
    return mapping


def _iter_offers_in_group(group: dict):
    """Yield the offering dicts that carry a usable price within one group."""
    offerings = []
    primary = group.get("primaryOffering")
    if isinstance(primary, dict):
        offerings.append(primary)
    collapsed = group.get("collapsedOfferings")
    if isinstance(collapsed, list):
        offerings.extend(o for o in collapsed if isinstance(o, dict))
    for off in offerings:
        ppg = off.get("pricePerGpuHour")
        if isinstance(ppg, dict) and isinstance(ppg.get("usd"), (int, float)):
            yield off


def parse_offers(blob: str, model: str, logger: logging.Logger) -> list[dict]:
    """Extract a flat list of offer dicts from an HTML page or raw JSON blob.

    Each offer dict has keys: provider, provider_id, rental_type, commitment,
    price, currency, gpu_count, vram_gb, total_vram, regions, available.
    Returns ``[]`` if no priced groups are found (signals the caller to try the
    Playwright fallback).
    """
    stream = reconstruct_rsc(blob) if "self.__next_f" in blob else blob
    if not stream:
        return []
    pmap = _provider_name_map(stream)

    offers: list[dict] = []
    seen_ids = set()
    for m in _GROUP_RE.finditer(stream):
        obj = _match_object(stream, m.start())
        if not obj:
            continue
        try:
            group = json.loads(obj)
        except json.JSONDecodeError:
            continue

        group_key = group.get("groupKey", "")
        parts = group_key.split("::")
        provider_id = parts[0] if parts else ""
        gk_rental = parts[1] if len(parts) > 1 else None
        gk_commit = parts[2] if len(parts) > 2 and parts[2] else None
        provider_name = pmap.get(provider_id) or provider_id.replace("-", " ").title()

        for off in _iter_offers_in_group(group):
            oid = off.get("id")
            if oid is not None:
                if oid in seen_ids:
                    continue
                seen_ids.add(oid)
            gpu_count = off.get("gpuCount")
            total_vram = off.get("totalVram")
            vram_gb = None
            if isinstance(total_vram, (int, float)) and gpu_count:
                vram_gb = round(total_vram / gpu_count)
            regions = off.get("regions")
            if isinstance(regions, list):
                regions = ",".join(str(r) for r in regions)
            offers.append({
                "provider": provider_name,
                "provider_id": provider_id,
                "rental_type": off.get("rentalType") or gk_rental,
                "commitment": off.get("commitmentTermMonths", gk_commit),
                "price": float(off["pricePerGpuHour"]["usd"]),
                "currency": "USD",
                "gpu_count": gpu_count,
                "vram_gb": vram_gb,
                "total_vram": total_vram,
                "regions": regions,
                "available": off.get("available", True),
            })

    if not offers:
        logger.warning("[%s] no priced offers parsed from payload", model)
    return offers


# --------------------------------------------------------------------------- #
# Build the daily price categories (tiers) from raw offers
# --------------------------------------------------------------------------- #

def build_records(model: str, offers: list[dict], scrape_date: str,
                  scraped_at: str, source_url: str) -> list[dict]:
    """Turn raw offers into one record per price category for this model."""
    # Prefer currently-available offers; fall back to everything if none are.
    avail = [o for o in offers if o.get("available")] or offers
    if not avail:
        return []

    def base(offer: dict) -> dict:
        return {
            "scrape_date": scrape_date,
            "scraped_at": scraped_at,
            "gpu_model": model,
            "currency": "USD",
            "unit": "per GPU per hour",
            "source_url": source_url,
            "provider": offer.get("provider"),
            "rental_type": offer.get("rental_type"),
            "gpu_count": offer.get("gpu_count"),
            "vram_gb": offer.get("vram_gb"),
            "regions": offer.get("regions"),
        }

    records: list[dict] = []

    # 1) Cheapest overall ---------------------------------------------------- #
    cheapest = min(avail, key=lambda o: o["price"])
    rec = base(cheapest)
    rec.update(category="Cheapest", price=round(cheapest["price"], 4))
    records.append(rec)

    # 2) Per-rental-type aggregates (min + median) --------------------------- #
    rental_types = sorted({o["rental_type"] for o in avail if o["rental_type"]})
    for rt in rental_types:
        subset = [o for o in avail if o["rental_type"] == rt]
        label = pretty_rental(rt)

        cheap_rt = min(subset, key=lambda o: o["price"])
        rec = base(cheap_rt)
        rec.update(category=f"{label} (min)", price=round(cheap_rt["price"], 4))
        records.append(rec)

        med = statistics.median(o["price"] for o in subset)
        rec = {**base(cheap_rt), "provider": None, "gpu_count": None,
               "vram_gb": None, "regions": None, "rental_type": rt}
        rec.update(category=f"{label} (median)", price=round(med, 4))
        records.append(rec)

    # 3) Per-provider x rental-type cheapest (vendor breakdown) -------------- #
    by_combo: dict = {}
    for o in avail:
        key = (o["provider"], o["rental_type"])
        if key not in by_combo or o["price"] < by_combo[key]["price"]:
            by_combo[key] = o
    for (provider, rt), o in by_combo.items():
        rec = base(o)
        rec.update(category=f"{provider} · {pretty_rental(rt)}",
                   price=round(o["price"], 4))
        records.append(rec)

    return records


# --------------------------------------------------------------------------- #
# SQLite storage
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scrape_date TEXT    NOT NULL,            -- YYYY-MM-DD (local date of scrape)
    scraped_at  TEXT    NOT NULL,            -- full ISO-8601 timestamp
    gpu_model   TEXT    NOT NULL,            -- A100, H100, ...
    category    TEXT    NOT NULL,            -- price tier / config label
    price       REAL    NOT NULL,            -- USD per GPU per hour
    currency    TEXT    NOT NULL,            -- always 'USD'
    unit        TEXT,                        -- 'per GPU per hour'
    provider    TEXT,                        -- vendor (NULL for aggregate tiers)
    rental_type TEXT,                        -- on_demand / spot / ...
    gpu_count   INTEGER,
    vram_gb     INTEGER,                     -- per-GPU memory in GB
    regions     TEXT,
    source_url  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uniq_daily
    ON prices(scrape_date, gpu_model, category);
CREATE INDEX IF NOT EXISTS idx_model_date ON prices(gpu_model, scrape_date);
"""

_COLUMNS = ["scrape_date", "scraped_at", "gpu_model", "category", "price",
            "currency", "unit", "provider", "rental_type", "gpu_count",
            "vram_gb", "regions", "source_url"]


def connect_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def upsert_records(conn: sqlite3.Connection, records: list[dict]) -> int:
    """Insert records; re-running the same day overwrites that day's row."""
    if not records:
        return 0
    placeholders = ", ".join("?" for _ in _COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in _COLUMNS
                        if c not in ("scrape_date", "gpu_model", "category"))
    sql = (f"INSERT INTO prices ({', '.join(_COLUMNS)}) VALUES ({placeholders}) "
           f"ON CONFLICT(scrape_date, gpu_model, category) DO UPDATE SET {updates}")
    rows = [[rec.get(c) for c in _COLUMNS] for rec in records]
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def scrape_model(slug: str, display: str, logger: logging.Logger,
                 use_fallback: bool = True) -> list[dict]:
    """Fetch + parse a single model, returning its raw offer list."""
    source_url = MODEL_URL.format(slug=slug)
    html = fetch_html(slug, logger)
    offers = parse_offers(html, display, logger) if html else []

    if not offers and use_fallback:
        logger.info("[%s] server payload empty; trying Playwright fallback...", display)
        rendered = fetch_html_playwright(slug, logger)
        if rendered:
            offers = parse_offers(rendered, display, logger)
    return offers


def run(db_path: Path, models=MODELS, quiet=False, use_fallback=True) -> int:
    """Scrape every model and persist. Returns process exit code (0 = all ok)."""
    logger = get_logger(quiet)
    now = datetime.now()
    scrape_date = now.strftime("%Y-%m-%d")
    scraped_at = now.isoformat(timespec="seconds")

    conn = connect_db(db_path)
    init_db(conn)

    total_rows = 0
    failures = []
    for i, (slug, display) in enumerate(models):
        if i:                                   # polite gap between requests
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))
        try:
            offers = scrape_model(slug, display, logger, use_fallback)
            if not offers:
                failures.append(display)
                logger.error("[%s] FAILED: no data scraped", display)
                continue
            records = build_records(display, offers, scrape_date, scraped_at,
                                    MODEL_URL.format(slug=slug))
            n = upsert_records(conn, records)
            total_rows += n
            cheapest = min(o["price"] for o in offers)
            logger.info("[%s] OK: %d offers -> %d categories (cheapest $%.4f/GPU/hr)",
                        display, len(offers), n, cheapest)
        except Exception as exc:  # noqa: BLE001 - never let one model abort the rest
            failures.append(display)
            logger.exception("[%s] FAILED with error: %s", display, exc)

    conn.close()
    ok = len(models) - len(failures)
    if failures:
        logger.error("Done %s: %d/%d models OK, %d rows written. FAILED: %s",
                     scrape_date, ok, len(models), total_rows, ", ".join(failures))
        return 1
    logger.info("Done %s: %d/%d models OK, %d rows written to %s",
                scrape_date, ok, len(models), total_rows, db_path)
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Scrape gpus.io GPU rental prices into SQLite.")
    p.add_argument("--db", type=Path, default=DB_PATH, help="SQLite DB path")
    p.add_argument("--models", nargs="*", metavar="MODEL",
                   help="subset of models to scrape (e.g. H100 B200); default all")
    p.add_argument("--no-fallback", action="store_true",
                   help="disable the Playwright browser fallback")
    p.add_argument("--quiet", action="store_true", help="only log warnings/errors")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    models = MODELS
    if args.models:
        wanted = {m.upper() for m in args.models}
        models = [(s, d) for (s, d) in MODELS if d.upper() in wanted]
        if not models:
            print(f"No known models match {args.models}; choices: "
                  f"{[d for _, d in MODELS]}", file=sys.stderr)
            return 2
    return run(args.db, models=models, quiet=args.quiet,
               use_fallback=not args.no_fallback)


if __name__ == "__main__":
    raise SystemExit(main())
