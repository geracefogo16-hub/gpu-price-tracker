"""Git-friendly, text-first storage for the computeprices tracker.

Layout:
- raw/YYYY/MM/DD/{gpu-prices,llm-prices,catalogs}.json — canonicalized raw
  snapshots (sorted keys, sorted rows, stable formatting). Immutable source
  of truth; every derived table can be rebuilt from these.
- data/*.csv — normalized, append-mostly tables with fixed column order and
  deterministic row order, so daily diffs stay small and reviewable.

Upserts are idempotent: on a natural-key conflict the stored row is replaced
only if the incoming `last_updated` is STRICTLY newer; otherwise the sighting
is still recorded in daily_presence. Running the collector twice back-to-back
therefore produces zero data-table changes the second time.
"""

from __future__ import annotations

import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from . import paths
from .normalize import GpuRow, LlmRow

log = logging.getLogger("tracker.store")

GPU_COLUMNS = [
    "effective_date", "provider_slug", "gpu_slug", "gpu_count",
    "pricing_type", "commitment_months", "price_per_hour_usd",
    "total_hourly_usd", "vram_gb", "architecture", "max_gpus_per_node",
    "currency", "exchange_rate_to_usd", "provider", "gpu",
    "source_url", "last_updated", "flags",
]
LLM_COLUMNS = [
    "effective_date", "provider_slug", "model_slug", "pricing_type",
    "price_per_1m_input_usd", "price_per_1m_output_usd",
    "price_per_1m_cached_input_usd", "context_window", "creator",
    "provider", "model", "source_url", "last_updated", "flags",
]
PRESENCE_COLUMNS = [
    "pull_date", "kind", "provider_slug", "item_slug", "gpu_count",
    "pricing_type", "commitment_months", "last_updated",
]
REGISTRY_COLUMNS = ["provider_slug", "display_name", "first_seen", "last_seen", "is_active"]
RUNS_COLUMNS = [
    "run_ts_utc", "status", "requests_used", "gpu_rows", "llm_rows",
    "gpu_new", "gpu_updated", "llm_new", "llm_updated", "rejected",
    "suspicious", "notes", "gpu_rows_by_slug",
]

GPU_KEY_FIELDS = ["effective_date", "provider_slug", "gpu_slug", "gpu_count",
                  "pricing_type", "commitment_months"]
LLM_KEY_FIELDS = ["effective_date", "provider_slug", "model_slug", "pricing_type"]

ACTIVE_WINDOW_DAYS = 7  # a provider unseen for this long is marked inactive


def _fmt(value) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return format(value, "g")  # 3.99 -> "3.99", 1.0 -> "1"
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, list):
        return ";".join(str(v) for v in value)
    return str(value)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n", extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({c: row.get(c, "") for c in columns})


def _sort_key(row: dict[str, str], key_fields: list[str]):
    out = []
    for k in key_fields:
        v = row.get(k, "")
        out.append((0, int(v)) if v.lstrip("-").isdigit() else (1, v))
    return out


class Table:
    """A CSV table keyed by a tuple of (string-rendered) key columns."""

    def __init__(self, path: Path, columns: list[str], key_fields: list[str]):
        self.path = path
        self.columns = columns
        self.key_fields = key_fields
        self.rows: dict[tuple, dict[str, str]] = {}
        for row in read_csv(path):
            self.rows[self._key_of(row)] = row
        self.inserted = 0
        self.updated = 0

    def _key_of(self, row: dict[str, str]) -> tuple:
        return tuple(row.get(k, "") for k in self.key_fields)

    def upsert_price(self, row: dict[str, str]) -> None:
        """Insert, or replace only when incoming last_updated is strictly newer."""
        key = self._key_of(row)
        existing = self.rows.get(key)
        if existing is None:
            self.rows[key] = row
            self.inserted += 1
        elif row.get("last_updated", "") > existing.get("last_updated", ""):
            self.rows[key] = row
            self.updated += 1

    def put(self, row: dict[str, str]) -> None:
        """Unconditional upsert (counted as insert only when new)."""
        key = self._key_of(row)
        if key not in self.rows:
            self.inserted += 1
        elif self.rows[key] != row:
            self.updated += 1
        self.rows[key] = row

    def save(self) -> None:
        ordered = sorted(self.rows.values(), key=lambda r: _sort_key(r, self.key_fields))
        write_csv(self.path, self.columns, ordered)


# ---------------------------------------------------------------- raw files

def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def write_raw_snapshot(pull_date: str, name: str, payload: dict) -> Path:
    """raw/YYYY/MM/DD/<name>.json with canonical formatting. Overwriting with
    identical content on the second daily run yields no git diff."""
    y, m, d = pull_date.split("-")
    path = paths.RAW_DIR / y / m / d / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(payload), encoding="utf-8")
    return path


def raw_rows_sorted(rows: list[dict], key_names: list[str]) -> list[dict]:
    return sorted(rows, key=lambda r: tuple(str(r.get(k, "")) for k in key_names))


# ------------------------------------------------------------- conversions

def gpu_row_to_csv(row: GpuRow) -> dict[str, str]:
    return {
        "effective_date": row.effective_date,
        "provider_slug": row.provider_slug,
        "gpu_slug": row.gpu_slug,
        "gpu_count": _fmt(row.gpu_count),
        "pricing_type": row.pricing_type,
        "commitment_months": _fmt(row.commitment_months),
        "price_per_hour_usd": _fmt(row.price_per_hour_usd),
        "total_hourly_usd": _fmt(row.total_hourly_usd),
        "vram_gb": _fmt(row.vram_gb),
        "architecture": row.architecture,
        "max_gpus_per_node": _fmt(row.max_gpus_per_node),
        "currency": row.currency,
        "exchange_rate_to_usd": _fmt(row.exchange_rate_to_usd),
        "provider": row.provider,
        "gpu": row.gpu,
        "source_url": row.source_url,
        "last_updated": _fmt(row.last_updated),
        "flags": _fmt(row.flags),
    }


def llm_row_to_csv(row: LlmRow) -> dict[str, str]:
    return {
        "effective_date": row.effective_date,
        "provider_slug": row.provider_slug,
        "model_slug": row.model_slug,
        "pricing_type": row.pricing_type,
        "price_per_1m_input_usd": _fmt(row.price_per_1m_input_usd),
        "price_per_1m_output_usd": _fmt(row.price_per_1m_output_usd),
        "price_per_1m_cached_input_usd": _fmt(row.price_per_1m_cached_input_usd),
        "context_window": _fmt(row.context_window),
        "creator": row.creator,
        "provider": row.provider,
        "model": row.model,
        "source_url": row.source_url,
        "last_updated": _fmt(row.last_updated),
        "flags": _fmt(row.flags),
    }


def presence_row(pull_date: str, kind: str, row) -> dict[str, str]:
    """daily_presence records which series appeared in each day's pull —
    this is what distinguishes 'provider disappeared' (absent here) from
    'price unchanged' (present, stale last_updated)."""
    if kind == "gpu":
        item_slug, gpu_count = row.gpu_slug, _fmt(row.gpu_count)
        commitment = _fmt(row.commitment_months)
    else:
        item_slug, gpu_count, commitment = row.model_slug, "", ""
    return {
        "pull_date": pull_date,
        "kind": kind,
        "provider_slug": row.provider_slug,
        "item_slug": item_slug,
        "gpu_count": gpu_count,
        "pricing_type": row.pricing_type,
        "commitment_months": commitment,
        "last_updated": _fmt(row.last_updated),
    }


# --------------------------------------------------------------- registry

def update_provider_registry(pull_date: str, seen: dict[str, str],
                             catalog: list[dict]) -> list[str]:
    """Update provider_registry.csv. `seen` maps provider_slug -> display
    name observed in this pull (rows or catalog). Returns newly registered
    slugs (INFO-logged 'new provider detected')."""
    table = Table(paths.REGISTRY_CSV, REGISTRY_COLUMNS, ["provider_slug"])
    for entry in catalog:
        slug = entry.get("slug") or entry.get("provider_slug")
        if slug and slug not in seen:
            seen[slug] = entry.get("name") or entry.get("provider") or slug
    new_slugs = []
    for slug, name in sorted(seen.items()):
        existing = table.rows.get((slug,))
        if existing is None:
            new_slugs.append(slug)
            log.info("New provider detected: %s (%s)", slug, name)
            table.put({"provider_slug": slug, "display_name": name,
                       "first_seen": pull_date, "last_seen": pull_date,
                       "is_active": "true"})
        else:
            row = dict(existing)
            row["display_name"] = name or row["display_name"]
            row["last_seen"] = max(row.get("last_seen", ""), pull_date)
            table.put(row)
    # Recompute is_active for everyone based on last_seen recency.
    pull = datetime.fromisoformat(pull_date)
    for key, row in table.rows.items():
        try:
            age = (pull - datetime.fromisoformat(row["last_seen"])).days
        except ValueError:
            age = ACTIVE_WINDOW_DAYS + 1
        row["is_active"] = "true" if age <= ACTIVE_WINDOW_DAYS else "false"
    table.save()
    return new_slugs


# ------------------------------------------------------------ rejects/runs

def append_rejected(records: list[tuple[str, str, dict, str]]) -> int:
    """Append (pull_date, kind, raw, reason) quarantine records, skipping
    exact duplicates already present (the same broken row re-seen by the
    second daily cron must not be quarantined twice)."""
    if not records:
        return 0
    paths.REJECTED_NDJSON.parent.mkdir(parents=True, exist_ok=True)
    existing = set()
    if paths.REJECTED_NDJSON.exists():
        existing = set(paths.REJECTED_NDJSON.read_text(encoding="utf-8").splitlines())
    added = 0
    with paths.REJECTED_NDJSON.open("a", encoding="utf-8") as f:
        for pull_date, kind, raw, reason in records:
            line = json.dumps({"pull_date": pull_date, "kind": kind,
                               "reason": reason, "raw": raw},
                              sort_keys=True, ensure_ascii=False)
            if line not in existing:
                f.write(line + "\n")
                existing.add(line)
                added += 1
    return added


def append_run(row: dict[str, str]) -> None:
    exists = paths.RUNS_CSV.exists()
    paths.RUNS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with paths.RUNS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RUNS_COLUMNS, lineterminator="\n", extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow({c: row.get(c, "") for c in RUNS_COLUMNS})


def recent_gpu_counts(n_runs: int = 7) -> list[dict[str, int]]:
    """Per-slug GPU row counts from the trailing n successful runs."""
    out = []
    for row in read_csv(paths.RUNS_CSV):
        if row.get("status") not in ("ok", "suspicious"):
            continue
        try:
            counts = json.loads(row.get("gpu_rows_by_slug") or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(counts, dict):
            out.append({k: int(v) for k, v in counts.items() if str(v).isdigit()})
    return out[-n_runs:]


def volume_anomalies(current: dict[str, int], history: list[dict[str, int]]) -> list[str]:
    """Flag tracked GPUs whose row count dropped >50% vs the trailing median.
    The run still completes; this only marks it suspicious."""
    import statistics
    notes = []
    if len(history) < 3:
        return notes  # not enough history to judge
    for slug, count in sorted(current.items()):
        past = [h[slug] for h in history if slug in h]
        if len(past) < 3:
            continue
        med = statistics.median(past)
        if med >= 2 and count < 0.5 * med:
            notes.append(f"{slug}: {count} rows vs trailing median {med:g}")
    return notes


def write_last_run(payload: dict) -> None:
    paths.LAST_RUN_JSON.parent.mkdir(parents=True, exist_ok=True)
    paths.LAST_RUN_JSON.write_text(canonical_json(payload), encoding="utf-8")
