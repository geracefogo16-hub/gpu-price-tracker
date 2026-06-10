"""Edge case (spec §7 addendum): GPU slug renames/merges.

If computeprices renames a slug, `aliases:` in watchlist.yaml must keep
(1) rows under the old slug flowing through the collector even after the
old slug vanishes from the live catalog, and (2) the dashboard rendering
old-slug history and new-slug data as ONE continuous series."""

import yaml

from build_dashboard import build_gpu_page_data
from collector import validate_slugs
from tracker.watchlist import Watchlist


def make_watchlist():
    doc = yaml.safe_load("""
gpus:
  h100:
    aliases: [h100-old]
    featured:
      - {provider: coreweave, configs: [8]}
llm_models: {}
""")
    w = Watchlist(doc)
    w.gpu_slugs = {"h100": "h100"}  # canonical resolution
    return w


def test_alias_rows_survive_catalog_disappearance():
    rows = [
        {"gpu_slug": "h100-old", "provider_slug": "coreweave"},   # renamed away
        {"gpu_slug": "h100", "provider_slug": "coreweave"},       # current
        {"gpu_slug": "h100-impostor", "provider_slug": "x"},      # untracked
    ]
    w = make_watchlist()
    kept, rejected = validate_slugs(rows, "gpu_slug", w.all_gpu_slugs(),
                                    catalog_slugs={"h100"},  # old slug gone
                                    alias_slugs=w.alias_slugs())
    assert {r["gpu_slug"] for r in kept} == {"h100-old", "h100"}
    assert rejected == []


def test_renamed_slug_renders_as_one_continuous_series():
    w = make_watchlist()

    def pres(pull_date, slug, lu):
        return {"pull_date": pull_date, "kind": "gpu", "provider_slug": "coreweave",
                "item_slug": slug, "gpu_count": "8", "pricing_type": "on_demand",
                "commitment_months": "", "last_updated": lu}

    def price(eff, slug, value, lu):
        return {"effective_date": eff, "provider_slug": "coreweave", "gpu_slug": slug,
                "gpu_count": "8", "pricing_type": "on_demand", "commitment_months": "",
                "price_per_hour_usd": value, "last_updated": lu}

    # Day 1-2 under the old slug, day 3 onward under the new one.
    presence = [
        pres("2026-06-01", "h100-old", "2026-06-01T01:00:00+00:00"),
        pres("2026-06-02", "h100-old", "2026-06-01T01:00:00+00:00"),  # stale, present
        pres("2026-06-03", "h100", "2026-06-03T01:00:00+00:00"),
    ]
    prices = [
        price("2026-06-01", "h100-old", "4.20", "2026-06-01T01:00:00+00:00"),
        price("2026-06-03", "h100", "3.99", "2026-06-03T01:00:00+00:00"),
    ]
    data = build_gpu_page_data("h100", w.gpu_slug_set("h100"), w, presence, prices,
                               {"coreweave": "CoreWeave"})
    (series,) = data["classes"]["on_demand"]
    assert series["label"] == "CoreWeave 8x"
    # One series spanning the rename, no gap: 4.20, 4.20 (stale), 3.99.
    assert series["y"] == [4.2, 4.2, 3.99]
    assert series["featured"] is True


def test_transition_day_keeps_fresher_quote():
    w = make_watchlist()
    presence = [
        {"pull_date": "2026-06-03", "kind": "gpu", "provider_slug": "coreweave",
         "item_slug": "h100-old", "gpu_count": "8", "pricing_type": "on_demand",
         "commitment_months": "", "last_updated": "2026-06-01T01:00:00+00:00"},
        {"pull_date": "2026-06-03", "kind": "gpu", "provider_slug": "coreweave",
         "item_slug": "h100", "gpu_count": "8", "pricing_type": "on_demand",
         "commitment_months": "", "last_updated": "2026-06-03T01:00:00+00:00"},
    ]
    prices = [
        {"effective_date": "2026-06-01", "provider_slug": "coreweave", "gpu_slug": "h100-old",
         "gpu_count": "8", "pricing_type": "on_demand", "commitment_months": "",
         "price_per_hour_usd": "4.20", "last_updated": "2026-06-01T01:00:00+00:00"},
        {"effective_date": "2026-06-03", "provider_slug": "coreweave", "gpu_slug": "h100",
         "gpu_count": "8", "pricing_type": "on_demand", "commitment_months": "",
         "price_per_hour_usd": "3.99", "last_updated": "2026-06-03T01:00:00+00:00"},
    ]
    data = build_gpu_page_data("h100", w.gpu_slug_set("h100"), w, presence, prices,
                               {})
    (series,) = data["classes"]["on_demand"]
    assert series["y"] == [3.99]  # fresher quote wins on the overlap day
