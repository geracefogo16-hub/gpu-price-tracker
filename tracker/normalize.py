"""Row validation / normalization for computeprices API responses.

Core rule — date attribution: a price belongs to the UTC date of its own
`last_updated` timestamp (the data's update date), NEVER the collection
date. Rows in a single response have been observed ranging from same-day to
3+ weeks stale, so daily runs repeatedly see identical old records; the
storage layer's upserts are idempotent against that.

Validation philosophy: structurally broken rows (missing identity fields,
unparseable timestamp, non-numeric price) are REJECTED to rejected.ndjson;
merely suspicious values (price <= 0, per-GPU > $200/hr, gpu_count outside
1-8) are INGESTED but flagged — odd-but-real values like gpu_count=3 exist
in the wild. Unknown extra fields are tolerated (the API promises additive
changes only); unknown pricing_type values are stored as-is, not rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

PER_GPU_PRICE_SANITY_MAX = 200.0  # $/GPU/hr
GPU_COUNT_SANE = range(1, 9)


class RowError(ValueError):
    """Row is structurally unusable and must be quarantined."""


def parse_last_updated(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        raise RowError(f"missing/non-string last_updated: {value!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as e:
        raise RowError(f"unparseable last_updated {value!r}: {e}") from None
    if dt.tzinfo is None:
        # The API emits offset-aware timestamps; treat a bare one as UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def effective_date(last_updated: datetime) -> str:
    """The date a price is attributed to: DATE(last_updated) in UTC."""
    return last_updated.astimezone(timezone.utc).date().isoformat()


def pricing_class(pricing_type: str, commitment_months: int | None) -> str:
    """Dashboard grouping for the Demand / Spot / Reserved tabs.

    The website shows a Reserved tab but the API docs only document
    on_demand|spot filters; reserved may surface either as a literal
    pricing_type or as commitment rows. Any row carrying a commitment is
    classed reserved regardless of its pricing_type label."""
    if commitment_months is not None and commitment_months > 0:
        return "reserved"
    return pricing_type


def _require_str(raw: dict, key: str) -> str:
    v = raw.get(key)
    if not isinstance(v, str) or not v.strip():
        raise RowError(f"missing/empty required field {key!r}")
    return v.strip()


def _number(raw: dict, key: str, required: bool = True) -> float | None:
    v = raw.get(key)
    if v is None:
        if required:
            raise RowError(f"missing required numeric field {key!r}")
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise RowError(f"non-numeric {key!r}: {v!r}")
    return float(v)


def _opt_int(raw: dict, key: str) -> int | None:
    v = raw.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int):
        if isinstance(v, float) and v.is_integer():
            return int(v)
        raise RowError(f"non-integer {key!r}: {v!r}")
    return v


@dataclass
class GpuRow:
    effective_date: str
    provider_slug: str
    provider: str
    gpu_slug: str
    gpu: str
    gpu_count: int
    pricing_type: str
    commitment_months: int | None
    price_per_hour_usd: float
    total_hourly_usd: float | None
    vram_gb: float | None
    architecture: str
    max_gpus_per_node: int | None
    currency: str
    exchange_rate_to_usd: float | None
    source_url: str
    last_updated: datetime
    flags: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple:
        """Natural key; commitment_months is part of record identity."""
        return (self.effective_date, self.provider_slug, self.gpu_slug,
                self.gpu_count, self.pricing_type, self.commitment_months)

    @property
    def pricing_class(self) -> str:
        return pricing_class(self.pricing_type, self.commitment_months)


@dataclass
class LlmRow:
    effective_date: str
    provider_slug: str
    provider: str
    model_slug: str
    model: str
    creator: str
    context_window: int | None
    price_per_1m_input_usd: float | None
    price_per_1m_output_usd: float | None
    price_per_1m_cached_input_usd: float | None
    pricing_type: str
    source_url: str
    last_updated: datetime
    flags: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple:
        return (self.effective_date, self.provider_slug, self.model_slug,
                self.pricing_type)


def normalize_gpu_row(raw: dict) -> GpuRow:
    last_updated = parse_last_updated(raw.get("last_updated"))
    gpu_count = _opt_int(raw, "gpu_count")
    if gpu_count is None:
        raise RowError("missing gpu_count")
    price = _number(raw, "price_per_hour_usd")
    row = GpuRow(
        effective_date=effective_date(last_updated),
        provider_slug=_require_str(raw, "provider_slug"),
        provider=str(raw.get("provider") or ""),
        gpu_slug=_require_str(raw, "gpu_slug"),
        gpu=str(raw.get("gpu") or ""),
        gpu_count=gpu_count,
        pricing_type=_require_str(raw, "pricing_type"),
        commitment_months=_opt_int(raw, "commitment_months"),
        price_per_hour_usd=price,
        total_hourly_usd=_number(raw, "total_hourly_usd", required=False),
        vram_gb=_number(raw, "vram_gb", required=False),
        architecture=str(raw.get("architecture") or ""),
        max_gpus_per_node=_opt_int(raw, "max_gpus_per_node"),
        currency=str(raw.get("currency") or "USD"),
        exchange_rate_to_usd=_number(raw, "exchange_rate_to_usd", required=False),
        source_url=str(raw.get("source_url") or ""),
        last_updated=last_updated,
    )
    if price <= 0:
        row.flags.append("nonpositive_price")
    if price > PER_GPU_PRICE_SANITY_MAX:
        row.flags.append("price_above_sanity_max")
    if gpu_count not in GPU_COUNT_SANE:
        row.flags.append("gpu_count_out_of_range")
    return row


def normalize_llm_row(raw: dict) -> LlmRow:
    last_updated = parse_last_updated(raw.get("last_updated"))
    p_in = _number(raw, "price_per_1m_input_usd", required=False)
    p_out = _number(raw, "price_per_1m_output_usd", required=False)
    if p_in is None and p_out is None:
        raise RowError("row has neither input nor output price")
    row = LlmRow(
        effective_date=effective_date(last_updated),
        provider_slug=_require_str(raw, "provider_slug"),
        provider=str(raw.get("provider") or ""),
        model_slug=_require_str(raw, "model_slug"),
        model=str(raw.get("model") or ""),
        creator=str(raw.get("creator") or ""),
        context_window=_opt_int(raw, "context_window"),
        price_per_1m_input_usd=p_in,
        price_per_1m_output_usd=p_out,
        price_per_1m_cached_input_usd=_number(raw, "price_per_1m_cached_input_usd", required=False),
        pricing_type=_require_str(raw, "pricing_type"),
        source_url=str(raw.get("source_url") or ""),
        last_updated=last_updated,
    )
    for name, p in (("input", p_in), ("output", p_out)):
        if p is not None and p < 0:
            row.flags.append(f"negative_{name}_price")
    return row


def dedupe_keep_newest(rows: list) -> list:
    """Dedupe rows sharing a natural key within one response, keeping the
    newest last_updated (ties keep the first seen)."""
    best: dict[tuple, Any] = {}
    for r in rows:
        cur = best.get(r.key)
        if cur is None or r.last_updated > cur.last_updated:
            best[r.key] = r
    return list(best.values())
