#!/usr/bin/env python3
"""One-shot verification of the computeprices.com v1 API assumptions
(the pre-coding checks from the project spec, §3). Run anywhere with open
network access — e.g. `python verify_api.py` locally, or via the manual
`workflow_dispatch` of .github/workflows/verify.yml if you add one.

Checks:
1. Resolve watchlist slugs (incl. H100 NVL by name) against /api/v1/gpus.
2. Determine empirically how RESERVED pricing is represented — distinct
   (pricing_type, commitment_months) combos across the full feed.
3. Resolve LLM model slugs against /api/v1/llm-models.
4. Measure unfiltered response sizes for both price endpoints.
5. Filter-mistrust probe: request gpu-prices with a bogus slug filter and
   report what comes back (a known hazard: data for a DIFFERENT GPU instead
   of a 404).

Uses ~7 requests, well within the 60/hr keyless budget.
"""

from __future__ import annotations

import json
import sys
from collections import Counter

from tracker.api import Client
from tracker.watchlist import Watchlist


def main() -> int:
    client = Client()
    watch = Watchlist.load()
    try:
        print("== 1. GPU slug resolution ==")
        gpu_catalog = client.gpu_catalog()
        print(f"catalog: {len(gpu_catalog)} GPUs")
        llm_catalog = client.llm_catalog()
        watch.resolve(gpu_catalog, llm_catalog, persist=False)
        for local_id in watch.gpus:
            print(f"  {local_id:10s} -> {watch.gpu_slugs.get(local_id, '!! UNRESOLVED')}")

        print("\n== 3. LLM slug resolution ==")
        print(f"catalog: {len(llm_catalog)} models")
        for local_id in watch.llm_models:
            print(f"  {local_id:20s} -> {watch.llm_slugs.get(local_id, '!! UNRESOLVED')}")

        print("\n== 2 & 4. Unfiltered pulls: size + reserved representation ==")
        gpu_rows, gpu_meta = client.gpu_prices()
        raw_len = len(json.dumps(gpu_rows))
        print(f"gpu-prices: {len(gpu_rows)} rows, ~{raw_len/1e6:.2f} MB JSON, meta={gpu_meta}")
        combos = Counter((r.get("pricing_type"), r.get("commitment_months")) for r in gpu_rows)
        print("distinct (pricing_type, commitment_months):")
        for (pt, cm), n in sorted(combos.items(), key=str):
            print(f"  {pt!r} commitment={cm!r}  x{n}")
        llm_rows, llm_meta = client.llm_prices()
        print(f"llm-prices: {len(llm_rows)} rows, ~{len(json.dumps(llm_rows))/1e6:.2f} MB JSON")
        llm_types = Counter(r.get("pricing_type") for r in llm_rows)
        print(f"llm pricing_type values: {dict(llm_types)}")

        print("\n== 5. Filter-mistrust probe (bogus slug) ==")
        bogus_rows, _ = client.get_all("gpu-prices", {"gpu": "definitely-not-a-gpu-xyz"})
        returned = Counter(r.get("gpu_slug") for r in bogus_rows)
        print(f"bogus filter returned {len(bogus_rows)} rows; gpu_slugs: {dict(returned) or 'none'}")
        if bogus_rows:
            print("  !! CONFIRMED: unrecognized filters return other GPUs' data — "
                  "row-level slug validation is mandatory.")

        print(f"\nTotal requests used: {client.requests_used}")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
