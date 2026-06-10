#!/usr/bin/env python3
"""Rebuild derived artifacts.

  python rebuild_db.py             # build data/cache.db (SQLite, .gitignored)
                                   # from the committed CSVs
  python rebuild_db.py --from-raw  # first regenerate the price CSVs from the
                                   # immutable raw/ snapshots (source of
                                   # truth), then build cache.db

The SQLite cache is purely derived — never committed; the CSVs and raw/
snapshots in git are canonical.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from tracker import paths, store
from tracker.normalize import (RowError, dedupe_keep_newest,
                               normalize_gpu_row, normalize_llm_row)

TABLES = {
    "gpu_prices": (paths.GPU_PRICES_CSV, store.GPU_COLUMNS),
    "llm_prices": (paths.LLM_PRICES_CSV, store.LLM_COLUMNS),
    "llm_benchmarks": (paths.LLM_BENCH_CSV,
                       ["scrape_date", "model_slug", "provider_slug", "provider_name_raw",
                        "tokens_per_sec", "price_perf", "source_method"]),
    "daily_presence": (paths.PRESENCE_CSV, store.PRESENCE_COLUMNS),
    "provider_registry": (paths.REGISTRY_CSV, store.REGISTRY_COLUMNS),
    "runs": (paths.RUNS_CSV, store.RUNS_COLUMNS),
}


def rebuild_csvs_from_raw() -> None:
    """Replay every raw/YYYY/MM/DD snapshot through the same normalize +
    upsert logic the collector uses, regenerating the two price CSVs."""
    gpu_table = store.Table(paths.GPU_PRICES_CSV, store.GPU_COLUMNS, store.GPU_KEY_FIELDS)
    llm_table = store.Table(paths.LLM_PRICES_CSV, store.LLM_COLUMNS, store.LLM_KEY_FIELDS)
    gpu_table.rows.clear()
    llm_table.rows.clear()

    snapshots = sorted(paths.RAW_DIR.glob("*/*/*/gpu-prices.json")) + \
                sorted(paths.RAW_DIR.glob("*/*/*/llm-prices.json"))
    for snap in snapshots:
        payload = json.loads(snap.read_text(encoding="utf-8"))
        rows = payload.get("rows", [])
        is_gpu = snap.name.startswith("gpu")
        normalized = []
        for raw in rows:
            try:
                normalized.append(normalize_gpu_row(raw) if is_gpu else normalize_llm_row(raw))
            except RowError:
                continue  # already quarantined when first collected
        for r in dedupe_keep_newest(normalized):
            if is_gpu:
                gpu_table.upsert_price(store.gpu_row_to_csv(r))
            else:
                llm_table.upsert_price(store.llm_row_to_csv(r))
    gpu_table.save()
    llm_table.save()
    print(f"Rebuilt CSVs from {len(snapshots)} raw snapshots: "
          f"{len(gpu_table.rows)} GPU rows, {len(llm_table.rows)} LLM rows")


def build_sqlite() -> None:
    paths.CACHE_DB.unlink(missing_ok=True)
    conn = sqlite3.connect(paths.CACHE_DB)
    try:
        for table, (csv_path, columns) in TABLES.items():
            cols = ", ".join(f'"{c}" TEXT' for c in columns)
            conn.execute(f'CREATE TABLE "{table}" ({cols})')
            rows = store.read_csv(csv_path)
            if rows:
                placeholders = ", ".join("?" for _ in columns)
                conn.executemany(
                    f'INSERT INTO "{table}" VALUES ({placeholders})',
                    [[r.get(c, "") for c in columns] for r in rows])
            print(f"  {table}: {len(rows)} rows")
        conn.execute('CREATE INDEX idx_gpu ON gpu_prices(gpu_slug, provider_slug, effective_date)')
        conn.execute('CREATE INDEX idx_llm ON llm_prices(model_slug, provider_slug, effective_date)')
        conn.commit()
    finally:
        conn.close()
    print(f"SQLite cache written to {paths.CACHE_DB}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-raw", action="store_true",
                    help="regenerate price CSVs from raw/ snapshots first")
    args = ap.parse_args(argv)
    if args.from_raw:
        rebuild_csvs_from_raw()
    build_sqlite()
    return 0


if __name__ == "__main__":
    sys.exit(main())
