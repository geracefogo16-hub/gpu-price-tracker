"""Unit tests for the computeprices tracker core rules:

- effective-date attribution (UTC date of last_updated, never collection date)
- idempotent upsert conflict rules (strictly-newer last_updated wins)
- disappear/reappear presence logic (gap vs stale price)
- slug-mismatch rejection (never trust the API filter)
- reserved-pricing representation (either a literal pricing_type or
  commitment_months on on_demand rows — both supported, both part of identity)
"""

import copy
from datetime import datetime, timezone

import pytest

from tracker import normalize, store
from tracker.normalize import (RowError, dedupe_keep_newest, effective_date,
                               normalize_gpu_row, normalize_llm_row,
                               parse_last_updated, pricing_class)

GPU_ROW = {
    "provider": "Lambda Labs", "provider_slug": "lambda",
    "gpu": "H100 SXM", "gpu_slug": "h100",
    "vram_gb": 80, "architecture": "Hopper",
    "gpu_count": 8, "max_gpus_per_node": 8,
    "price_per_hour_usd": 3.99, "total_hourly_usd": 31.92,
    "pricing_type": "on_demand", "commitment_months": None,
    "currency": "USD", "exchange_rate_to_usd": 1,
    "source_url": "https://example.com",
    "last_updated": "2026-06-10T00:00:46.516976+00:00",
}

LLM_ROW = {
    "provider": "Amazon AWS", "provider_slug": "aws",
    "model": "Claude 3.5 Sonnet", "model_slug": "claude-3-5-sonnet",
    "creator": "Anthropic", "context_window": 200000,
    "price_per_1m_input_usd": 3, "price_per_1m_output_usd": 15,
    "price_per_1m_cached_input_usd": None,
    "pricing_type": "standard",
    "source_url": "https://example.com",
    "last_updated": "2026-06-10T05:00:35+00:00",
}


def gpu(**overrides):
    row = copy.deepcopy(GPU_ROW)
    row.update(overrides)
    return row


# ----------------------------------------------------- date attribution

class TestEffectiveDate:
    def test_utc_date_of_last_updated(self):
        r = normalize_gpu_row(gpu())
        assert r.effective_date == "2026-06-10"

    def test_offset_timezone_converts_to_utc(self):
        # 01:30+05:30 on June 10 is June 9, 20:00 UTC.
        r = normalize_gpu_row(gpu(last_updated="2026-06-10T01:30:00+05:30"))
        assert r.effective_date == "2026-06-09"

    def test_z_suffix(self):
        assert effective_date(parse_last_updated("2026-06-10T23:59:59Z")) == "2026-06-10"

    def test_stale_row_keeps_its_own_date(self):
        # A 3-week-old quote seen today is attributed to ITS date.
        r = normalize_gpu_row(gpu(last_updated="2026-05-19T12:00:00+00:00"))
        assert r.effective_date == "2026-05-19"

    def test_unparseable_timestamp_rejected(self):
        with pytest.raises(RowError):
            normalize_gpu_row(gpu(last_updated="not-a-date"))
        with pytest.raises(RowError):
            normalize_gpu_row(gpu(last_updated=None))


# ------------------------------------------------------------ upsert rules

class TestUpsert:
    def make_table(self, tmp_path):
        return store.Table(tmp_path / "gpu.csv", store.GPU_COLUMNS, store.GPU_KEY_FIELDS)

    def test_insert_then_idempotent_reupsert(self, tmp_path):
        t = self.make_table(tmp_path)
        row = store.gpu_row_to_csv(normalize_gpu_row(gpu()))
        t.upsert_price(row)
        t.upsert_price(dict(row))  # same record seen again (second cron)
        assert t.inserted == 1 and t.updated == 0
        t.save()
        # Reload and replay: still no changes — idempotency proof.
        t2 = store.Table(tmp_path / "gpu.csv", store.GPU_COLUMNS, store.GPU_KEY_FIELDS)
        before = copy.deepcopy(t2.rows)
        t2.upsert_price(dict(row))
        assert t2.rows == before and t2.updated == 0

    def test_strictly_newer_wins(self, tmp_path):
        t = self.make_table(tmp_path)
        old = store.gpu_row_to_csv(normalize_gpu_row(
            gpu(price_per_hour_usd=3.99, last_updated="2026-06-10T01:00:00+00:00")))
        newer = store.gpu_row_to_csv(normalize_gpu_row(
            gpu(price_per_hour_usd=3.49, last_updated="2026-06-10T09:00:00+00:00")))
        t.upsert_price(old)
        t.upsert_price(newer)
        assert t.updated == 1
        (stored,) = t.rows.values()
        assert stored["price_per_hour_usd"] == "3.49"
        # Older record arriving later must NOT overwrite.
        t.upsert_price(dict(old))
        (stored,) = t.rows.values()
        assert stored["price_per_hour_usd"] == "3.49"

    def test_different_dates_are_different_records(self, tmp_path):
        t = self.make_table(tmp_path)
        t.upsert_price(store.gpu_row_to_csv(normalize_gpu_row(
            gpu(last_updated="2026-06-09T12:00:00+00:00"))))
        t.upsert_price(store.gpu_row_to_csv(normalize_gpu_row(
            gpu(last_updated="2026-06-10T12:00:00+00:00"))))
        assert len(t.rows) == 2

    def test_dedupe_within_response_keeps_newest(self):
        rows = [normalize_gpu_row(gpu(price_per_hour_usd=4.10,
                                      last_updated="2026-06-10T01:00:00+00:00")),
                normalize_gpu_row(gpu(price_per_hour_usd=3.99,
                                      last_updated="2026-06-10T09:00:00+00:00"))]
        (kept,) = dedupe_keep_newest(rows)
        assert kept.price_per_hour_usd == 3.99


# --------------------------------------------- presence / disappear-reappear

class TestPresence:
    def test_gap_vs_stale(self, tmp_path):
        """Stale price (present, old last_updated) is NOT a gap;
        absence from a day's pull IS a gap."""
        presence = store.Table(tmp_path / "p.csv", store.PRESENCE_COLUMNS,
                               ["pull_date", "kind", "provider_slug", "item_slug",
                                "gpu_count", "pricing_type", "commitment_months"])
        r = normalize_gpu_row(gpu())
        presence.put(store.presence_row("2026-06-10", "gpu", r))
        # Day 2: provider absent (no row). Day 3: reappears, price unchanged.
        presence.put(store.presence_row("2026-06-12", "gpu", r))
        present_days = {row["pull_date"] for row in presence.rows.values()}
        assert present_days == {"2026-06-10", "2026-06-12"}
        assert "2026-06-11" not in present_days  # true gap, line must break

    def test_same_day_reput_is_idempotent(self, tmp_path):
        presence = store.Table(tmp_path / "p.csv", store.PRESENCE_COLUMNS,
                               ["pull_date", "kind", "provider_slug", "item_slug",
                                "gpu_count", "pricing_type", "commitment_months"])
        r = normalize_gpu_row(gpu())
        presence.put(store.presence_row("2026-06-10", "gpu", r))
        presence.put(store.presence_row("2026-06-10", "gpu", r))
        assert len(presence.rows) == 1 and presence.inserted == 1


# ------------------------------------------------------ slug-mismatch guard

class TestSlugValidation:
    def test_rows_not_in_catalog_are_rejected(self):
        from collector import validate_slugs
        rows = [gpu(), gpu(gpu_slug="h200-but-fake")]
        kept, rejected = validate_slugs(rows, "gpu_slug",
                                        allowed={"h100", "h200-but-fake"},
                                        catalog_slugs={"h100", "h200"})
        assert [r["gpu_slug"] for r in kept] == ["h100"]
        assert len(rejected) == 1 and "not in live catalog" in rejected[0][1]

    def test_untracked_rows_silently_skipped(self):
        from collector import validate_slugs
        kept, rejected = validate_slugs([gpu(gpu_slug="a100sxm")], "gpu_slug",
                                        allowed={"h100"}, catalog_slugs={"a100sxm", "h100"})
        assert kept == [] and rejected == []


# ------------------------------------------- reserved-pricing representation

class TestReservedRepresentation:
    """The API may represent Reserved either as a literal pricing_type or as
    on_demand rows carrying commitment_months — support both, and keep
    commitment_months in the natural key."""

    def test_literal_reserved_pricing_type(self):
        r = normalize_gpu_row(gpu(pricing_type="reserved", commitment_months=12))
        assert r.pricing_class == "reserved"

    def test_on_demand_with_commitment_is_reserved_class(self):
        r = normalize_gpu_row(gpu(pricing_type="on_demand", commitment_months=6))
        assert r.pricing_class == "reserved"
        assert pricing_class("on_demand", None) == "on_demand"

    def test_commitment_months_part_of_identity(self):
        no_commit = normalize_gpu_row(gpu(commitment_months=None))
        six = normalize_gpu_row(gpu(commitment_months=6))
        twelve = normalize_gpu_row(gpu(commitment_months=12))
        assert len({no_commit.key, six.key, twelve.key}) == 3

    def test_unknown_pricing_type_is_stored_not_rejected(self):
        r = normalize_gpu_row(gpu(pricing_type="preemptible-weird"))
        assert r.pricing_type == "preemptible-weird"
        assert r.pricing_class == "preemptible-weird"


# ------------------------------------------------------------- sanity flags

class TestSanity:
    def test_flags_do_not_reject(self):
        r = normalize_gpu_row(gpu(price_per_hour_usd=250.0, gpu_count=16))
        assert "price_above_sanity_max" in r.flags
        assert "gpu_count_out_of_range" in r.flags

    def test_odd_real_gpu_count_accepted_unflagged(self):
        r = normalize_gpu_row(gpu(gpu_count=3))  # observed on UpCloud
        assert r.flags == []

    def test_llm_row_roundtrip(self):
        r = normalize_llm_row(copy.deepcopy(LLM_ROW))
        assert r.key == ("2026-06-10", "aws", "claude-3-5-sonnet", "standard")
        csv_row = store.llm_row_to_csv(r)
        assert csv_row["price_per_1m_input_usd"] == "3"
        assert csv_row["price_per_1m_cached_input_usd"] == ""

    def test_extra_unknown_fields_tolerated(self):
        r = normalize_gpu_row(gpu(brand_new_field_v1_2="whatever"))
        assert r.gpu_slug == "h100"


# ------------------------------------------------------------ volume check

class TestVolumeAnomaly:
    def test_drop_over_half_flags(self):
        history = [{"h100": 40}] * 7
        assert store.volume_anomalies({"h100": 15}, history)
        assert not store.volume_anomalies({"h100": 25}, history)

    def test_insufficient_history_is_quiet(self):
        assert store.volume_anomalies({"h100": 1}, [{"h100": 40}]) == []
