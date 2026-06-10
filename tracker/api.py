"""HTTP client for the computeprices.com v1 API.

Etiquette per https://computeprices.com/docs/api:
- descriptive User-Agent on every request
- honor Retry-After on 429
- read X-RateLimit-* headers (logged when running low)
- optional bearer key via the COMPUTEPRICES_API_KEY env var (works without)

Keyless budget is 60 requests/hour/IP; a normal run uses ~5 requests
(3 catalogs + 2 unfiltered price pulls).
"""

from __future__ import annotations

import logging
import os
import random
import time

import httpx

log = logging.getLogger("tracker.api")

BASE_URL = "https://computeprices.com/api/v1"
USER_AGENT = "computeprices-tracker/1.0 (github.com/geracefogo16-hub/gpu-price-tracker)"

MAX_RETRIES = 4
BACKOFF_BASE = 2.0  # 2s, 4s, 8s, 16s (+ jitter)
TIMEOUT = 30.0
MAX_PAGES = 10  # defensive cap if the API ever paginates
REQUEST_BUDGET_WARN = 20  # warn well before the 60/hr keyless limit


class ApiError(RuntimeError):
    pass


class Client:
    def __init__(self, base_url: str = BASE_URL, api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        key = api_key if api_key is not None else os.environ.get("COMPUTEPRICES_API_KEY")
        if key:
            headers["Authorization"] = f"Bearer {key}"
            log.info("Using COMPUTEPRICES_API_KEY (keyed limits apply)")
        self._http = httpx.Client(headers=headers, timeout=TIMEOUT, follow_redirects=True)
        self.requests_used = 0
        self.meta_versions: set[str] = set()

    def close(self) -> None:
        self._http.close()

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_err: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            if attempt:
                delay = BACKOFF_BASE ** attempt + random.uniform(0, 1)
                log.warning("Retrying %s in %.1fs (attempt %d/%d): %s",
                            path, delay, attempt, MAX_RETRIES, last_err)
                time.sleep(delay)
            try:
                self.requests_used += 1
                if self.requests_used == REQUEST_BUDGET_WARN:
                    log.warning("Request budget high: %d requests this run", self.requests_used)
                resp = self._http.get(url, params=params)
            except httpx.HTTPError as e:
                last_err = e
                continue

            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) < 10:
                log.warning("X-RateLimit-Remaining is low: %s", remaining)

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 60.0
                log.warning("429 rate limited on %s; honoring Retry-After=%.0fs", path, wait)
                time.sleep(min(wait, 300.0))
                last_err = ApiError("429 rate limited")
                continue
            if resp.status_code >= 500:
                last_err = ApiError(f"{resp.status_code} from {path}")
                continue
            if resp.status_code != 200:
                raise ApiError(f"GET {path} -> HTTP {resp.status_code}: {resp.text[:200]}")

            try:
                body = resp.json()
            except ValueError as e:
                last_err = ApiError(f"non-JSON body from {path}: {e}")
                continue
            if not isinstance(body, dict) or "data" not in body:
                raise ApiError(f"unexpected envelope from {path}: keys={list(body)[:8] if isinstance(body, dict) else type(body)}")
            meta = body.get("meta") or {}
            version = str(meta.get("version", "")) if isinstance(meta, dict) else ""
            if version:
                self.meta_versions.add(version)
                if version != "v1":
                    log.warning("API meta.version is %r (expected 'v1') on %s", version, path)
            return body
        raise ApiError(f"GET {path} failed after {MAX_RETRIES} retries: {last_err}")

    def get_all(self, path: str, params: dict | None = None) -> tuple[list[dict], dict]:
        """Fetch every row from an endpoint, defensively following pagination
        if `meta` ever advertises it (the v1 API currently returns everything
        in one response). Returns (rows, last_meta)."""
        params = dict(params or {})
        rows: list[dict] = []
        meta: dict = {}
        for page in range(1, MAX_PAGES + 1):
            body = self._get(path, params or None)
            data = body.get("data")
            if not isinstance(data, list):
                raise ApiError(f"{path}: 'data' is not a list")
            rows.extend(r for r in data if isinstance(r, dict))
            meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
            total_pages = meta.get("total_pages") or meta.get("totalPages")
            cur = meta.get("page") or page
            if not (isinstance(total_pages, int) and isinstance(cur, int) and cur < total_pages):
                break
            params["page"] = cur + 1
            log.info("%s: following pagination to page %d/%d", path, cur + 1, total_pages)
        return rows, meta

    # Convenience wrappers -------------------------------------------------
    def gpu_catalog(self) -> list[dict]:
        return self.get_all("gpus")[0]

    def llm_catalog(self) -> list[dict]:
        return self.get_all("llm-models")[0]

    def provider_catalog(self) -> list[dict]:
        return self.get_all("providers")[0]

    def gpu_prices(self) -> tuple[list[dict], dict]:
        return self.get_all("gpu-prices")

    def llm_prices(self) -> tuple[list[dict], dict]:
        return self.get_all("llm-prices")
