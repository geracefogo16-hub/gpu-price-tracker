#!/usr/bin/env python3
"""computeprices.com daily collector (API-first, git-scraping).

Usage:
  python collector.py --dry-run   # fetch + validate, print what would be
                                  # ingested, write NOTHING
  python collector.py --once      # full run: raw snapshots, CSV upserts,
                                  # presence, registry, runs log, heartbeat

Running --once twice back-to-back produces zero changes to the data tables
the second time (raw snapshots and upserts are idempotent); only the
heartbeat (data/last_run.json) and the run log advance, which is deliberate:
every run must produce a commit so GitHub's 60-day scheduled-workflow
inactivity timer keeps resetting.

Exit status: non-zero when a price endpoint cannot be fetched at all (the
workflow fails loudly and the second daily cron retries). One bad ROW never
aborts a run — it is quarantined in data/rejected.ndjson.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone

from tracker import paths, store
from tracker.api import ApiError, Client
from tracker.normalize import (RowError, dedupe_keep_newest,
                               normalize_gpu_row, normalize_llm_row)
from tracker.watchlist import Watchlist

log = logging.getLogger("collector")

# An unfiltered feed of at least this many rows is treated as truncated by a
# server-side cap (observed: exactly 1000 rows, meta.count == 1000).
PROBABLE_ROW_CAP = 1000


def dedupe_exact(rows: list[dict]) -> list[dict]:
    """Drop byte-identical rows (overlap between the unfiltered pull and
    per-slug supplements)."""
    seen: set[str] = set()
    out = []
    for r in rows:
        k = json.dumps(r, sort_keys=True)
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def setup_logging() -> None:
    paths.LOGS_DIR.mkdir(exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(paths.LOGS_DIR / "computeprices.log", encoding="utf-8"),
        ],
    )


def validate_slugs(rows: list[dict], slug_field: str, allowed: set[str],
                   catalog_slugs: set[str],
                   alias_slugs: set[str] = frozenset()) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Filter rows to tracked slugs; reject rows whose slug is absent from
    the live catalog (never trust a filter — a bad slug has been observed to
    return a different GPU's data rather than 404). Slugs explicitly listed
    as aliases in the watchlist are exempt from the catalog check: a
    renamed-away slug legitimately no longer appears in the catalog but its
    rows must keep flowing for series continuity."""
    kept, rejected = [], []
    for row in rows:
        slug = row.get(slug_field)
        if slug not in allowed:
            continue  # untracked, simply not ingested
        if catalog_slugs and slug not in catalog_slugs and slug not in alias_slugs:
            rejected.append((row, f"{slug_field}={slug!r} not in live catalog"))
            continue
        kept.append(row)
    return kept, rejected


def collect(dry_run: bool) -> int:
    run_ts = datetime.now(timezone.utc)
    pull_date = run_ts.date().isoformat()
    warnings: list[str] = []
    status = "ok"

    client = Client()
    try:
        # Catalogs are refreshed every run (slug/name drift defense).
        gpu_catalog = client.gpu_catalog()
        llm_catalog = client.llm_catalog()
        provider_catalog = client.provider_catalog()

        watch = Watchlist.load()
        watch.resolve(gpu_catalog, llm_catalog, persist=not dry_run)
        warnings.extend(watch.warnings)

        tracked_gpu = watch.all_gpu_slugs()
        tracked_llm = watch.all_llm_slugs()
        alias_slugs = watch.alias_slugs()
        catalog_gpu_slugs = {e.get("slug") or e.get("gpu_slug") for e in gpu_catalog} - {None}
        catalog_llm_slugs = {e.get("slug") or e.get("model_slug") for e in llm_catalog} - {None}

        # One unfiltered call per dataset; filter locally. Fewest requests,
        # future-proof against new providers, immune to filter mistrust.
        gpu_raw, gpu_meta = client.gpu_prices()
        llm_raw, llm_meta = client.llm_prices()

        # Observed 2026-06-10: the unfiltered feed returned exactly 1000 rows
        # (meta.count == 1000, no pagination fields) — almost certainly a
        # server-side cap. When a feed hits it, supplement with per-tracked-
        # slug filtered calls so tracked rows can't silently fall off as the
        # site grows; row-level slug validation below guards the filtered
        # responses, and exact-duplicate rows are dropped.
        if len(gpu_raw) >= PROBABLE_ROW_CAP:
            msg = (f"gpu-prices returned {len(gpu_raw)} rows (probable server cap) — "
                   "supplementing with per-slug filtered calls")
            log.warning(msg)
            warnings.append(msg)
            for slug in sorted(tracked_gpu):
                gpu_raw.extend(client.get_all("gpu-prices", {"gpu": slug})[0])
        if len(llm_raw) >= PROBABLE_ROW_CAP:
            msg = (f"llm-prices returned {len(llm_raw)} rows (probable server cap) — "
                   "supplementing with per-slug filtered calls")
            log.warning(msg)
            warnings.append(msg)
            for slug in sorted(tracked_llm):
                llm_raw.extend(client.get_all("llm-prices", {"model": slug})[0])
        gpu_raw = dedupe_exact(gpu_raw)
        llm_raw = dedupe_exact(llm_raw)
    except ApiError as e:
        log.error("FATAL: API collection failed: %s", e)
        if not dry_run:
            store.write_last_run({
                "ts": run_ts.isoformat(), "status": "error", "error": str(e),
                "requests_used": client.requests_used,
            })
            store.append_run({"run_ts_utc": run_ts.isoformat(), "status": "error",
                              "requests_used": str(client.requests_used),
                              "notes": str(e)[:300]})
        return 1
    finally:
        client.close()

    gpu_kept_raw, gpu_slug_rejects = validate_slugs(gpu_raw, "gpu_slug", tracked_gpu,
                                                    catalog_gpu_slugs, alias_slugs)
    llm_kept_raw, llm_slug_rejects = validate_slugs(llm_raw, "model_slug", tracked_llm,
                                                    catalog_llm_slugs, alias_slugs)

    gpu_rows, llm_rows = [], []
    row_rejects: list[tuple[str, dict, str]] = []
    for raw in gpu_kept_raw:
        try:
            gpu_rows.append(normalize_gpu_row(raw))
        except RowError as e:
            row_rejects.append(("gpu", raw, str(e)))
    for raw in llm_kept_raw:
        try:
            llm_rows.append(normalize_llm_row(raw))
        except RowError as e:
            row_rejects.append(("llm", raw, str(e)))

    gpu_rows = dedupe_keep_newest(gpu_rows)
    llm_rows = dedupe_keep_newest(llm_rows)

    flagged = [r for r in gpu_rows + llm_rows if r.flags]
    for r in flagged:
        log.warning("Sanity flag(s) %s on %s", r.flags, r.key)

    pt_combos = Counter((r.pricing_type, r.commitment_months) for r in gpu_rows)
    gpu_counts = Counter(r.gpu_slug for r in gpu_rows)

    summary = {
        "pull_date": pull_date,
        "api_rows_returned": {"gpu": len(gpu_raw), "llm": len(llm_raw)},
        "tracked_rows": {"gpu": len(gpu_rows), "llm": len(llm_rows)},
        "gpu_rows_by_slug": dict(sorted(gpu_counts.items())),
        "pricing_type_combos": {f"{pt}|{cm}": n for (pt, cm), n in sorted(pt_combos.items(), key=str)},
        "rejected": len(row_rejects) + len(gpu_slug_rejects) + len(llm_slug_rejects),
        "sanity_flagged": len(flagged),
        "requests_used": client.requests_used,
    }

    if dry_run:
        print(json.dumps(summary, indent=2, sort_keys=True))
        print(f"\nDRY RUN: would ingest {len(gpu_rows)} GPU rows and "
              f"{len(llm_rows)} LLM rows; nothing was written.")
        return 0

    # ---- raw snapshots (source of truth; canonical => idempotent) -------
    store.write_raw_snapshot(pull_date, "gpu-prices", {
        "fetched_at_date": pull_date, "meta": gpu_meta,
        "total_rows_returned": len(gpu_raw),
        "rows": store.raw_rows_sorted(gpu_kept_raw,
            ["provider_slug", "gpu_slug", "gpu_count", "pricing_type", "commitment_months"]),
    })
    store.write_raw_snapshot(pull_date, "llm-prices", {
        "fetched_at_date": pull_date, "meta": llm_meta,
        "total_rows_returned": len(llm_raw),
        "rows": store.raw_rows_sorted(llm_kept_raw,
            ["provider_slug", "model_slug", "pricing_type"]),
    })
    store.write_raw_snapshot(pull_date, "catalogs", {
        "fetched_at_date": pull_date,
        "gpus": store.raw_rows_sorted(gpu_catalog, ["slug"]),
        "llm_models": store.raw_rows_sorted(llm_catalog, ["slug"]),
        "providers": store.raw_rows_sorted(provider_catalog, ["slug"]),
    })

    # ---- upserts ---------------------------------------------------------
    gpu_table = store.Table(paths.GPU_PRICES_CSV, store.GPU_COLUMNS, store.GPU_KEY_FIELDS)
    for r in gpu_rows:
        gpu_table.upsert_price(store.gpu_row_to_csv(r))
    gpu_table.save()

    llm_table = store.Table(paths.LLM_PRICES_CSV, store.LLM_COLUMNS, store.LLM_KEY_FIELDS)
    for r in llm_rows:
        llm_table.upsert_price(store.llm_row_to_csv(r))
    llm_table.save()

    presence = store.Table(paths.PRESENCE_CSV, store.PRESENCE_COLUMNS,
                           ["pull_date", "kind", "provider_slug", "item_slug",
                            "gpu_count", "pricing_type", "commitment_months"])
    for r in gpu_rows:
        presence.put(store.presence_row(pull_date, "gpu", r))
    for r in llm_rows:
        presence.put(store.presence_row(pull_date, "llm", r))
    presence.save()

    seen_providers = {r.provider_slug: r.provider for r in gpu_rows + llm_rows}
    new_providers = store.update_provider_registry(pull_date, seen_providers, provider_catalog)
    if new_providers:
        warnings.append(f"new provider(s) detected: {', '.join(new_providers)}")

    store.append_rejected(
        [(pull_date, kind, raw, reason) for kind, raw, reason in row_rejects]
        + [(pull_date, "gpu", raw, reason) for raw, reason in gpu_slug_rejects]
        + [(pull_date, "llm", raw, reason) for raw, reason in llm_slug_rejects])

    # ---- volume anomaly check vs trailing 7-run median --------------------
    anomalies = store.volume_anomalies(dict(gpu_counts), store.recent_gpu_counts())
    if anomalies:
        status = "suspicious"
        warnings.extend(f"volume anomaly — {a}" for a in anomalies)
        log.warning("Run marked suspicious: %s", "; ".join(anomalies))

    store.append_run({
        "run_ts_utc": run_ts.isoformat(),
        "status": status,
        "requests_used": str(client.requests_used),
        "gpu_rows": str(len(gpu_rows)),
        "llm_rows": str(len(llm_rows)),
        "gpu_new": str(gpu_table.inserted),
        "gpu_updated": str(gpu_table.updated),
        "llm_new": str(llm_table.inserted),
        "llm_updated": str(llm_table.updated),
        "rejected": str(summary["rejected"]),
        "suspicious": "; ".join(anomalies),
        "notes": "; ".join(warnings)[:500],
        "gpu_rows_by_slug": json.dumps(dict(sorted(gpu_counts.items())), sort_keys=True),
    })

    # Heartbeat: ALWAYS written (timestamp changes every run) so every run
    # commits — keeps the 60-day scheduled-workflow inactivity clock reset.
    store.write_last_run({
        "ts": run_ts.isoformat(),
        "status": status,
        **summary,
        "new_rows": {"gpu": gpu_table.inserted, "llm": llm_table.inserted},
        "updated_rows": {"gpu": gpu_table.updated, "llm": llm_table.updated},
        "new_providers": new_providers,
        "warnings": warnings,
    })

    log.info("Run complete: %s | gpu %d rows (%d new, %d updated) | llm %d rows "
             "(%d new, %d updated) | %d rejected | %d requests",
             status, len(gpu_rows), gpu_table.inserted, gpu_table.updated,
             len(llm_rows), llm_table.inserted, llm_table.updated,
             summary["rejected"], client.requests_used)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="fetch + validate, print summary, write nothing")
    mode.add_argument("--once", action="store_true", help="do one full collection run")
    args = ap.parse_args(argv)
    setup_logging()
    return collect(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
