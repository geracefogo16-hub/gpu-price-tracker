"""Offline end-to-end test: run the full collector pipeline against a fake
API client twice and prove the second run changes none of the data tables
(the idempotency guarantee that makes the dual-cron schedule safe), then
prove the dashboard builder runs on the result."""

import json

import pytest

import build_dashboard
import collector
from tracker import paths

GPU_CATALOG = [
    {"slug": "h100", "name": "H100 SXM"},
    {"slug": "a100sxm", "name": "A100 SXM"},
    {"slug": "h100-nvl-actual", "name": "H100 NVL"},  # resolved by name
    {"slug": "h200", "name": "H200"}, {"slug": "b200", "name": "B200"},
    {"slug": "mi300x", "name": "MI300X"}, {"slug": "gb200", "name": "GB200"},
    {"slug": "gb300", "name": "GB300"},
]
LLM_CATALOG = [
    {"slug": "gpt-oss-120b", "name": "GPT-OSS-120B"},
    {"slug": "claude-opus-4-5", "name": "Claude Opus 4.5"},
]
PROVIDERS = [
    {"slug": "lambda", "name": "Lambda Labs"},
    {"slug": "coreweave", "name": "CoreWeave"},
    {"slug": "runpod", "name": "RunPod"},
    {"slug": "aws", "name": "Amazon AWS"},
]

GPU_PRICES = [
    # fresh on_demand row
    {"provider": "Lambda Labs", "provider_slug": "lambda", "gpu": "H100 SXM",
     "gpu_slug": "h100", "vram_gb": 80, "architecture": "Hopper", "gpu_count": 8,
     "max_gpus_per_node": 8, "price_per_hour_usd": 3.99, "total_hourly_usd": 31.92,
     "pricing_type": "on_demand", "commitment_months": None, "currency": "USD",
     "exchange_rate_to_usd": 1, "source_url": "https://x", "last_updated": "2026-06-10T00:00:46+00:00"},
    # stale row (3 weeks old) — attributed to ITS date
    {"provider": "CoreWeave", "provider_slug": "coreweave", "gpu": "H100 SXM",
     "gpu_slug": "h100", "gpu_count": 8, "price_per_hour_usd": 4.76,
     "pricing_type": "on_demand", "commitment_months": None, "currency": "USD",
     "source_url": "https://x", "last_updated": "2026-05-19T08:00:00+00:00"},
    # reserved-as-commitment row — separate identity
    {"provider": "CoreWeave", "provider_slug": "coreweave", "gpu": "H100 SXM",
     "gpu_slug": "h100", "gpu_count": 8, "price_per_hour_usd": 3.10,
     "pricing_type": "on_demand", "commitment_months": 6, "currency": "USD",
     "source_url": "https://x", "last_updated": "2026-06-10T00:00:46+00:00"},
    # row for the name-resolved H100 NVL slug
    {"provider": "RunPod", "provider_slug": "runpod", "gpu": "H100 NVL",
     "gpu_slug": "h100-nvl-actual", "gpu_count": 1, "price_per_hour_usd": 2.79,
     "pricing_type": "on_demand", "commitment_months": None, "currency": "USD",
     "source_url": "https://x", "last_updated": "2026-06-10T03:00:00+00:00"},
    # untracked slug — ignored, not rejected
    {"provider": "Lambda Labs", "provider_slug": "lambda", "gpu": "RTX 4090",
     "gpu_slug": "rtx4090", "gpu_count": 1, "price_per_hour_usd": 0.50,
     "pricing_type": "on_demand", "commitment_months": None, "currency": "USD",
     "source_url": "https://x", "last_updated": "2026-06-10T00:00:00+00:00"},
    # structurally broken tracked row — quarantined, must not abort the run
    {"provider": "Lambda Labs", "provider_slug": "lambda", "gpu": "H100 SXM",
     "gpu_slug": "h100", "gpu_count": 8, "price_per_hour_usd": 3.99,
     "pricing_type": "on_demand", "commitment_months": None,
     "source_url": "https://x", "last_updated": "garbage"},
]

LLM_PRICES = [
    {"provider": "Amazon AWS", "provider_slug": "aws", "model": "GPT-OSS-120B",
     "model_slug": "gpt-oss-120b", "creator": "OpenAI", "context_window": 131072,
     "price_per_1m_input_usd": 0.15, "price_per_1m_output_usd": 0.6,
     "price_per_1m_cached_input_usd": None, "pricing_type": "standard",
     "source_url": "https://x", "last_updated": "2026-06-10T05:00:35+00:00"},
    {"provider": "Amazon AWS", "provider_slug": "aws", "model": "GPT-OSS-120B",
     "model_slug": "gpt-oss-120b", "creator": "OpenAI", "context_window": 131072,
     "price_per_1m_input_usd": 0.075, "price_per_1m_output_usd": 0.3,
     "price_per_1m_cached_input_usd": None, "pricing_type": "batch",
     "source_url": "https://x", "last_updated": "2026-06-10T05:00:35+00:00"},
]


class FakeClient:
    def __init__(self, *a, **k):
        self.requests_used = 0
        self.meta_versions = {"v1"}

    def close(self):
        pass

    def _count(self):
        self.requests_used += 1

    def gpu_catalog(self):
        self._count()
        return GPU_CATALOG

    def llm_catalog(self):
        self._count()
        return LLM_CATALOG

    def provider_catalog(self):
        self._count()
        return PROVIDERS

    def gpu_prices(self):
        self._count()
        return GPU_PRICES, {"version": "v1"}

    def llm_prices(self):
        self._count()
        return LLM_PRICES, {"version": "v1"}


DATA_TABLES = ["gpu_prices.csv", "llm_prices.csv", "daily_presence.csv",
               "provider_registry.csv", "rejected.ndjson"]


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(collector, "Client", FakeClient)
    for attr, sub in [("RAW_DIR", "raw"), ("DATA_DIR", "data"),
                      ("DOCS_DIR", "docs"), ("LOGS_DIR", "logs")]:
        monkeypatch.setattr(paths, attr, tmp_path / sub)
    for attr, rel in [("GPU_PRICES_CSV", "data/gpu_prices.csv"),
                      ("LLM_PRICES_CSV", "data/llm_prices.csv"),
                      ("LLM_BENCH_CSV", "data/llm_benchmarks.csv"),
                      ("PRESENCE_CSV", "data/daily_presence.csv"),
                      ("REGISTRY_CSV", "data/provider_registry.csv"),
                      ("RUNS_CSV", "data/runs.csv"),
                      ("REJECTED_NDJSON", "data/rejected.ndjson"),
                      ("LAST_RUN_JSON", "data/last_run.json"),
                      ("SLUG_RESOLUTIONS_JSON", "data/slug_resolutions.json")]:
        monkeypatch.setattr(paths, attr, tmp_path / rel)
    return tmp_path


def snapshot(tmp_path):
    out = {}
    for name in DATA_TABLES:
        p = tmp_path / "data" / name
        out[name] = p.read_text() if p.exists() else None
    for p in sorted((tmp_path / "raw").rglob("*.json")):
        out[str(p.relative_to(tmp_path))] = p.read_text()
    return out


def test_full_run_twice_is_idempotent(sandbox):
    assert collector.collect(dry_run=False) == 0
    first = snapshot(sandbox)

    gpu_csv = first["gpu_prices.csv"]
    assert "h100-nvl-actual" in gpu_csv          # slug resolved by name
    assert "2026-05-19,coreweave" in gpu_csv     # stale row got ITS OWN date
    assert ",6," in gpu_csv                      # commitment row kept separately
    assert "rtx4090" not in gpu_csv              # untracked slug not ingested
    assert "garbage" in first["rejected.ndjson"]  # broken row quarantined

    resolutions = json.loads((sandbox / "data" / "slug_resolutions.json").read_text())
    assert resolutions["gpus"]["h100nvl"] == "h100-nvl-actual"

    last_run_1 = (sandbox / "data" / "last_run.json").read_text()
    assert collector.collect(dry_run=False) == 0
    second = snapshot(sandbox)
    assert first == second, "second back-to-back run must change no data tables"
    # ...but the heartbeat must still advance (always-commit guarantee).
    assert (sandbox / "data" / "last_run.json").read_text() != last_run_1
    run_info = json.loads((sandbox / "data" / "last_run.json").read_text())
    assert run_info["new_rows"] == {"gpu": 0, "llm": 0}
    assert run_info["updated_rows"] == {"gpu": 0, "llm": 0}


def test_dry_run_writes_nothing(sandbox, capsys):
    assert collector.collect(dry_run=True) == 0
    assert not (sandbox / "data").exists() or not list((sandbox / "data").iterdir())
    assert not (sandbox / "raw").exists()
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "tracked_rows" in out


def test_dashboard_builds_from_collected_data(sandbox, monkeypatch):
    assert collector.collect(dry_run=False) == 0
    assert build_dashboard.main() == 0
    docs = sandbox / "docs"
    h100 = json.loads((docs / "data" / "gpu_h100.json").read_text())
    labels = [s["label"] for cls in h100["classes"].values() for s in cls]
    assert any("Lambda Labs 8x" in l for l in labels)
    assert "reserved" in h100["classes"]  # commitment row classed as reserved
    series_by_class = h100["classes"]
    od = next(s for s in series_by_class["on_demand"] if s["provider_slug"] == "lambda")
    assert od["featured"] is False or isinstance(od["featured"], bool)
    llm = json.loads((docs / "data" / "llm_gpt-oss-120b.json").read_text())
    assert {s["pricing_type"] for s in llm["series"]} == {"standard", "batch"}
    assert (docs / "index.html").exists()
    health = json.loads((docs / "data" / "health.json").read_text())
    assert health["status"] == "ok"
