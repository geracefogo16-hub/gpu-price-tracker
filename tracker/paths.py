"""Repository-relative paths used by the computeprices tracker."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RAW_DIR = ROOT / "raw"
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
LOGS_DIR = ROOT / "logs"

WATCHLIST = ROOT / "watchlist.yaml"
ALIASES = ROOT / "aliases.yaml"

GPU_PRICES_CSV = DATA_DIR / "gpu_prices.csv"
LLM_PRICES_CSV = DATA_DIR / "llm_prices.csv"
LLM_BENCH_CSV = DATA_DIR / "llm_benchmarks.csv"
PRESENCE_CSV = DATA_DIR / "daily_presence.csv"
REGISTRY_CSV = DATA_DIR / "provider_registry.csv"
RUNS_CSV = DATA_DIR / "runs.csv"
REJECTED_NDJSON = DATA_DIR / "rejected.ndjson"
LAST_RUN_JSON = DATA_DIR / "last_run.json"
SLUG_RESOLUTIONS_JSON = DATA_DIR / "slug_resolutions.json"

CACHE_DB = DATA_DIR / "cache.db"  # derived, .gitignored
