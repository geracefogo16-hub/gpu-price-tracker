#!/usr/bin/env python3
"""Best-effort scrape of speed (tok/s) and $/perf from
computeprices.com/inference — the only two fields the v1 API lacks
(benchmarks are slated for a future v1.2 tier).

Strategy, per tracked model:
1. Fetch /inference?model=<slug> with plain HTTP and try to parse values
   embedded in the Next.js payload (__NEXT_DATA__ script tag or RSC flight
   chunks) — no browser needed when that works.
2. Otherwise render the page with Playwright headless Chromium and read the
   per-provider table from the DOM.
3. On ANY failure: log a warning and continue. This script always exits 0 —
   API-sourced prices must land regardless of benchmark availability (the CI
   step is additionally continue-on-error).

Provider display names are joined to provider_slug via aliases.yaml plus the
provider registry; unmatched names are stored with an empty slug and logged
so the alias map can be extended — data is never dropped silently.

Results land in data/llm_benchmarks.csv keyed by
(scrape_date, model_slug, provider_name_raw); benchmark values are
point-in-time site readings, so they are attributed to the scrape date.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone

import httpx
import yaml

from tracker import paths, store
from tracker.api import TIMEOUT, USER_AGENT
from tracker.watchlist import Watchlist

log = logging.getLogger("benchmarks")

INFERENCE_URL = "https://computeprices.com/inference"

BENCH_COLUMNS = ["scrape_date", "model_slug", "provider_slug", "provider_name_raw",
                 "tokens_per_sec", "price_perf", "source_method"]
BENCH_KEY_FIELDS = ["scrape_date", "model_slug", "provider_name_raw"]

# Keys that plausibly carry the two benchmark fields in embedded JSON.
SPEED_KEYS = ("tokens_per_second", "tokensPerSecond", "tokens_per_sec", "tok_per_sec", "speed")
PERF_KEYS = ("price_perf", "pricePerf", "price_performance", "usd_per_perf", "dollarPerPerf")
PROVIDER_KEYS = ("provider", "provider_name", "providerName")


def load_aliases() -> dict[str, str]:
    aliases: dict[str, str] = {}
    if paths.ALIASES.exists():
        with paths.ALIASES.open(encoding="utf-8") as f:
            doc = yaml.safe_load(f) or {}
        aliases.update({str(k).strip().lower(): str(v) for k, v in doc.items()})
    # Registry display names are aliases too.
    for row in store.read_csv(paths.REGISTRY_CSV):
        name = row.get("display_name", "").strip().lower()
        if name:
            aliases.setdefault(name, row["provider_slug"])
    return aliases


def name_to_slug(name: str, aliases: dict[str, str]) -> str:
    """Map a display name to provider_slug; '' when unknown (kept, logged)."""
    norm = name.strip().lower()
    if norm in aliases:
        return aliases[norm]
    slugified = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    known_slugs = {r["provider_slug"] for r in store.read_csv(paths.REGISTRY_CSV)}
    if slugified in known_slugs:
        return slugified
    log.warning("Unmatched provider name %r in benchmarks — extend aliases.yaml", name)
    return ""


def _to_float(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"[\d,]+(?:\.\d+)?", value)
        if m:
            try:
                return float(m.group(0).replace(",", ""))
            except ValueError:
                return None
    return None


# ---------------------------------------------------- embedded-JSON parsing

def _walk(node, hits: list[dict]) -> None:
    if isinstance(node, dict):
        has_speed = any(k in node for k in SPEED_KEYS)
        has_provider = any(k in node for k in PROVIDER_KEYS)
        if has_speed and has_provider:
            hits.append(node)
        for v in node.values():
            _walk(v, hits)
    elif isinstance(node, list):
        for v in node:
            _walk(v, hits)


def _rows_from_objects(objects: list[dict]) -> list[dict]:
    rows = []
    for obj in objects:
        provider = next((str(obj[k]) for k in PROVIDER_KEYS if obj.get(k)), "")
        speed = next((_to_float(obj[k]) for k in SPEED_KEYS if obj.get(k) is not None), None)
        perf = next((_to_float(obj[k]) for k in PERF_KEYS if obj.get(k) is not None), None)
        if provider and speed is not None:
            rows.append({"provider": provider, "tokens_per_sec": speed, "price_perf": perf})
    return rows


def parse_embedded(html: str) -> list[dict]:
    """Look for benchmark rows inside __NEXT_DATA__ or RSC flight payloads."""
    hits: list[dict] = []
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if m:
        try:
            _walk(json.loads(m.group(1)), hits)
        except json.JSONDecodeError:
            pass
    if not hits:
        # RSC flight data: self.__next_f.push([1,"...escaped json..."])
        chunks = re.findall(r'self\.__next_f\.push\(\[1,\s*"((?:[^"\\]|\\.)*)"\]\)', html)
        blob = ""
        for c in chunks:
            try:
                blob += json.loads(f'"{c}"')  # unescape JS string
            except json.JSONDecodeError:
                continue
        for frag in re.findall(r'\{[^{}]*(?:"%s")[^{}]*\}' % '"|"'.join(SPEED_KEYS), blob):
            try:
                _walk(json.loads(frag), hits)
            except json.JSONDecodeError:
                continue
    return _rows_from_objects(hits)


# -------------------------------------------------------- Playwright fallback

def parse_with_playwright(url: str) -> list[dict]:
    from playwright.sync_api import sync_playwright

    rows: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(user_agent=USER_AGENT)
            page.goto(url, wait_until="networkidle", timeout=60_000)
            for table in page.query_selector_all("table"):
                headers = [h.inner_text().strip().lower()
                           for h in table.query_selector_all("thead th, tr:first-child th")]
                if not headers:
                    continue
                def col(*pats):
                    for i, h in enumerate(headers):
                        if any(re.search(p_, h) for p_ in pats):
                            return i
                    return None
                i_prov = col(r"provider")
                i_speed = col(r"tok/s", r"speed", r"tokens")
                i_perf = col(r"\$/perf", r"perf", r"value")
                if i_prov is None or i_speed is None:
                    continue
                for tr in table.query_selector_all("tbody tr"):
                    cells = [td.inner_text().strip() for td in tr.query_selector_all("td")]
                    if len(cells) <= max(i_prov, i_speed):
                        continue
                    speed = _to_float(cells[i_speed])
                    perf = _to_float(cells[i_perf]) if (i_perf is not None and i_perf < len(cells)) else None
                    if cells[i_prov] and speed is not None:
                        rows.append({"provider": cells[i_prov],
                                     "tokens_per_sec": speed, "price_perf": perf})
                if rows:
                    break
        finally:
            browser.close()
    return rows


def scrape_model(client: httpx.Client, slug: str) -> tuple[list[dict], str]:
    url = f"{INFERENCE_URL}?model={slug}"
    try:
        resp = client.get(url)
        resp.raise_for_status()
        rows = parse_embedded(resp.text)
        if rows:
            return rows, "embedded"
    except Exception as e:  # noqa: BLE001 — best-effort by design
        log.warning("Embedded-payload fetch failed for %s: %s", slug, e)
    try:
        rows = parse_with_playwright(url)
        if rows:
            return rows, "playwright"
    except Exception as e:  # noqa: BLE001
        log.warning("Playwright scrape failed for %s: %s", slug, e)
    return [], "none"


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    scrape_date = datetime.now(timezone.utc).date().isoformat()
    watch = Watchlist.load()

    # Use persisted resolutions from the collector run when available.
    slugs = list(watch.llm_models.keys())
    if paths.SLUG_RESOLUTIONS_JSON.exists():
        try:
            resolved = json.loads(paths.SLUG_RESOLUTIONS_JSON.read_text(encoding="utf-8"))
            slugs = sorted(set(resolved.get("llms", {}).values())) or slugs
        except json.JSONDecodeError:
            pass

    aliases = load_aliases()
    table = store.Table(paths.LLM_BENCH_CSV, BENCH_COLUMNS, BENCH_KEY_FIELDS)
    client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT,
                          follow_redirects=True)
    try:
        for slug in slugs:
            rows, method = scrape_model(client, slug)
            if not rows:
                log.warning("No benchmark data for %s — storing nothing, prices unaffected", slug)
                continue
            for r in rows:
                table.put({
                    "scrape_date": scrape_date,
                    "model_slug": slug,
                    "provider_slug": name_to_slug(r["provider"], aliases),
                    "provider_name_raw": r["provider"],
                    "tokens_per_sec": store._fmt(r["tokens_per_sec"]),
                    "price_perf": store._fmt(r["price_perf"]),
                    "source_method": method,
                })
            log.info("Benchmarks for %s: %d providers via %s", slug, len(rows), method)
    finally:
        client.close()

    table.save()
    return 0  # never fail the pipeline


if __name__ == "__main__":
    sys.exit(main())
