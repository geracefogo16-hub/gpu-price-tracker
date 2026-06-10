"""Watchlist loading and runtime slug resolution.

Slugs marked `resolve_name:` are resolved against the live catalog on every
run (never guessed — probing showed an unrecognized slug filter can return a
DIFFERENT GPU's data instead of a 404). Catalogs are refreshed each run; a
watchlist slug vanishing from the catalog is a loud WARN, never an error,
and history stays intact.
"""

from __future__ import annotations

import json
import logging

import yaml

from . import paths

log = logging.getLogger("tracker.watchlist")


class Watchlist:
    def __init__(self, doc: dict):
        self.gpus: dict[str, dict] = doc.get("gpus") or {}
        self.llm_models: dict[str, dict] = {
            k: (v or {}) for k, v in (doc.get("llm_models") or {}).items()
        }
        self.pricing_classes: list[str] = doc.get("pricing_classes") or ["on_demand", "spot", "reserved"]
        # local id -> resolved API slug (filled by resolve())
        self.gpu_slugs: dict[str, str] = {}
        self.llm_slugs: dict[str, str] = {}
        self.warnings: list[str] = []

    @staticmethod
    def _aliases(cfg: dict) -> set[str]:
        aliases = (cfg or {}).get("aliases") or []
        return {str(a) for a in aliases}

    def gpu_slug_set(self, local_id: str) -> set[str]:
        """Canonical resolved slug + configured aliases (rename continuity)."""
        out = self._aliases(self.gpus.get(local_id) or {})
        if local_id in self.gpu_slugs:
            out.add(self.gpu_slugs[local_id])
        return out

    def llm_slug_set(self, local_id: str) -> set[str]:
        out = self._aliases(self.llm_models.get(local_id) or {})
        if local_id in self.llm_slugs:
            out.add(self.llm_slugs[local_id])
        return out

    def all_gpu_slugs(self) -> set[str]:
        return set().union(*(self.gpu_slug_set(i) for i in self.gpus)) if self.gpus else set()

    def all_llm_slugs(self) -> set[str]:
        return set().union(*(self.llm_slug_set(i) for i in self.llm_models)) if self.llm_models else set()

    def alias_slugs(self) -> set[str]:
        """Explicitly opted-in former slugs — exempt from the live-catalog
        check, since a renamed-away slug legitimately no longer appears there."""
        out: set[str] = set()
        for section in (self.gpus, self.llm_models):
            for cfg in section.values():
                out |= self._aliases(cfg)
        return out

    @classmethod
    def load(cls, path=None) -> "Watchlist":
        with open(path or paths.WATCHLIST, encoding="utf-8") as f:
            return cls(yaml.safe_load(f))

    def resolve(self, gpu_catalog: list[dict], llm_catalog: list[dict],
                persist: bool = True) -> None:
        self.gpu_slugs = self._resolve_section(self.gpus, gpu_catalog, "GPU")
        self.llm_slugs = self._resolve_section(self.llm_models, llm_catalog, "LLM model")
        if persist:
            resolutions = {"gpus": self.gpu_slugs, "llms": self.llm_slugs}
            paths.SLUG_RESOLUTIONS_JSON.parent.mkdir(parents=True, exist_ok=True)
            paths.SLUG_RESOLUTIONS_JSON.write_text(
                json.dumps(resolutions, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    def _resolve_section(self, section: dict[str, dict], catalog: list[dict],
                         label: str) -> dict[str, str]:
        by_slug = {}
        by_name = {}
        for entry in catalog:
            slug = entry.get("slug") or entry.get("gpu_slug") or entry.get("model_slug")
            name = entry.get("name") or entry.get("gpu") or entry.get("model") or ""
            if slug:
                by_slug[slug] = name
                by_name.setdefault(name.strip().lower(), slug)
        resolved: dict[str, str] = {}
        for local_id, cfg in section.items():
            cfg = cfg or {}
            want_name = (cfg.get("resolve_name") or "").strip().lower()
            if local_id in by_slug:
                resolved[local_id] = local_id
            elif want_name and want_name in by_name:
                resolved[local_id] = by_name[want_name]
                log.info("Resolved %s %r -> slug %r via catalog name match",
                         label, local_id, by_name[want_name])
            elif want_name:
                # Loose match: name contained in a catalog name (unique only).
                hits = [s for n, s in by_name.items() if want_name in n]
                if len(hits) == 1:
                    resolved[local_id] = hits[0]
                    log.info("Resolved %s %r -> slug %r via loose name match",
                             label, local_id, hits[0])
                else:
                    msg = (f"{label} {local_id!r} not resolvable from catalog "
                           f"(resolve_name={cfg.get('resolve_name')!r}, {len(hits)} loose hits) "
                           "— skipping this run, history intact")
                    log.warning(msg)
                    self.warnings.append(msg)
            else:
                msg = (f"{label} slug {local_id!r} vanished from the catalog — keeping history, "
                       "skipping this run (if it was renamed, set resolve_name: to follow it "
                       "and add the old slug under aliases: for series continuity)")
                log.warning(msg)
                self.warnings.append(msg)
        return resolved

    # Featured-series helpers (dashboard only) ----------------------------
    def featured_for(self, local_id: str) -> list[dict]:
        return (self.gpus.get(local_id) or {}).get("featured") or []

    def is_featured(self, local_id: str, provider_slug: str, gpu_count: int) -> bool:
        for entry in self.featured_for(local_id):
            if entry.get("provider") != provider_slug:
                continue
            configs = entry.get("configs")
            if configs == "all" or configs is None:
                return True
            if isinstance(configs, list) and gpu_count in configs:
                return True
        return False
