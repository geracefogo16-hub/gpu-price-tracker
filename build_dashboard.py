#!/usr/bin/env python3
"""Regenerate the static dashboard in docs/ (served by GitHub Pages).

Design notes:
- GitHub Pages (deploy-from-branch, /docs folder) serves ONLY docs/, so the
  per-page series data is written as small JSON files under docs/data/ and
  fetched by the pages at view time — HTML stays tiny as history grows.
- Charts are driven by daily_presence joined to price rows: a series present
  on a pull date draws a point (even when the price is stale/unchanged),
  while true absence yields null -> Plotly breaks the line. Never
  interpolate across gaps. Hover shows the row's own last_updated so fresh
  quotes are distinguishable from stale ones.
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime, timezone

from tracker import paths, store
from tracker.normalize import pricing_class
from tracker.watchlist import Watchlist

PLOTLY_CDN = "https://cdn.plot.ly/plotly-2.35.2.min.js"


def load_resolutions() -> dict:
    if paths.SLUG_RESOLUTIONS_JSON.exists():
        try:
            return json.loads(paths.SLUG_RESOLUTIONS_JSON.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"gpus": {}, "llms": {}}


def registry_names() -> dict[str, str]:
    return {r["provider_slug"]: r.get("display_name") or r["provider_slug"]
            for r in store.read_csv(paths.REGISTRY_CSV)}


def _commitment(v: str) -> int | None:
    return int(v) if v.strip().isdigit() else None


# ------------------------------------------------------------- GPU series

def build_gpu_page_data(local_id: str, slug_set: set[str], watch: Watchlist,
                        presence: list[dict], prices: list[dict],
                        names: dict[str, str]) -> dict:
    """slug_set = canonical slug + any configured aliases: rows under a
    renamed-away slug merge into the same continuous series."""
    pres = [p for p in presence if p["kind"] == "gpu" and p["item_slug"] in slug_set]
    pull_dates = sorted({p["pull_date"] for p in presence})  # global run dates

    price_by_key = {
        (r["effective_date"], r["provider_slug"], r["gpu_slug"], r["gpu_count"],
         r["pricing_type"], r["commitment_months"]): r
        for r in prices if r["gpu_slug"] in slug_set
    }

    series_presence: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for p in pres:
        skey = (p["provider_slug"], p["gpu_count"], p["pricing_type"], p["commitment_months"])
        cur = series_presence[skey].get(p["pull_date"])
        # Same series under old + new slug on one day: keep the fresher quote.
        if cur is None or p["last_updated"] > cur["last_updated"]:
            series_presence[skey][p["pull_date"]] = p

    classes: dict[str, list[dict]] = defaultdict(list)
    for (provider, count, ptype, commitment), by_date in sorted(series_presence.items()):
        ys, lus = [], []
        for d in pull_dates:
            p = by_date.get(d)
            if p is None:
                ys.append(None)
                lus.append(None)
                continue
            eff = p["last_updated"][:10]
            row = price_by_key.get((eff, provider, p["item_slug"], count, ptype, commitment))
            if row is None:
                ys.append(None)
                lus.append(None)
            else:
                ys.append(float(row["price_per_hour_usd"]))
                lus.append(row["last_updated"])
        cls = pricing_class(ptype, _commitment(commitment))
        label = f"{names.get(provider, provider)} {count}x"
        if _commitment(commitment):
            label += f" ({commitment}mo)"
        classes[cls].append({
            "label": label,
            "provider_slug": provider,
            "gpu_count": int(count) if count.isdigit() else count,
            "pricing_type": ptype,
            "commitment_months": commitment or None,
            "featured": watch.is_featured(local_id, provider,
                                          int(count) if count.isdigit() else -1),
            "y": ys,
            "lu": lus,
        })
    for lst in classes.values():
        lst.sort(key=lambda s: (not s["featured"], s["label"]))
    return {"id": local_id, "slugs": sorted(slug_set), "pull_dates": pull_dates,
            "classes": classes}


# ------------------------------------------------------------- LLM series

def build_llm_page_data(model_slug: str, slug_set: set[str], presence: list[dict],
                        prices: list[dict], bench: list[dict],
                        names: dict[str, str]) -> dict:
    pres = [p for p in presence if p["kind"] == "llm" and p["item_slug"] in slug_set]
    pull_dates = sorted({p["pull_date"] for p in presence})

    price_by_key = {
        (r["effective_date"], r["provider_slug"], r["model_slug"], r["pricing_type"]): r
        for r in prices if r["model_slug"] in slug_set
    }
    series_presence: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for p in pres:
        skey = (p["provider_slug"], p["pricing_type"])
        cur = series_presence[skey].get(p["pull_date"])
        if cur is None or p["last_updated"] > cur["last_updated"]:
            series_presence[skey][p["pull_date"]] = p

    series = []
    for (provider, ptype), by_date in sorted(series_presence.items()):
        rec = {"provider_slug": provider, "pricing_type": ptype,
               "label": names.get(provider, provider) + ("" if ptype == "standard" else f" ({ptype})"),
               "input": [], "output": [], "cached": [], "lu": []}
        for d in pull_dates:
            p = by_date.get(d)
            row = None
            if p is not None:
                row = price_by_key.get((p["last_updated"][:10], provider, p["item_slug"], ptype))
            for field, col in (("input", "price_per_1m_input_usd"),
                               ("output", "price_per_1m_output_usd"),
                               ("cached", "price_per_1m_cached_input_usd")):
                v = row.get(col) if row else None
                rec[field].append(float(v) if v not in (None, "") else None)
            rec["lu"].append(row["last_updated"] if row else None)
        series.append(rec)

    bench_rows = [b for b in bench if b["model_slug"] in slug_set]
    bench_dates = sorted({b["scrape_date"] for b in bench_rows})
    bench_by_provider: dict[str, dict[str, dict]] = defaultdict(dict)
    for b in bench_rows:
        key = b["provider_slug"] or b["provider_name_raw"]
        bench_by_provider[key][b["scrape_date"]] = b
    bench_series = []
    for key, by_date in sorted(bench_by_provider.items()):
        speeds, perfs = [], []
        for d in bench_dates:
            b = by_date.get(d)
            speeds.append(float(b["tokens_per_sec"]) if b and b["tokens_per_sec"] else None)
            perfs.append(float(b["price_perf"]) if b and b["price_perf"] else None)
        bench_series.append({"label": names.get(key, key), "speed": speeds, "perf": perfs})

    return {"slug": model_slug, "pull_dates": pull_dates, "series": series,
            "bench": {"dates": bench_dates, "series": bench_series}}


# ------------------------------------------------------------------ HTML

PAGE_TMPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<script src="{plotly}"></script>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<header><a href="index.html">&larr; overview</a><h1>{title}</h1></header>
<main id="main" data-page="{page_kind}" data-src="data/{data_file}">
<noscript>This dashboard needs JavaScript.</noscript>
</main>
<script src="assets/app.js"></script>
</body>
</html>
"""

APP_JS = r"""
"use strict";
const main = document.getElementById("main");
const PLOT_CFG = {responsive: true, displaylogo: false};
const LAYOUT = {
  margin: {l: 55, r: 10, t: 30, b: 40},
  hovermode: "closest",
  legend: {orientation: "h", y: -0.25},
  xaxis: {type: "date"},
};

function el(tag, attrs, text) {
  const e = document.createElement(tag);
  for (const k in (attrs || {})) e.setAttribute(k, attrs[k]);
  if (text) e.textContent = text;
  return e;
}

function gpuTraces(seriesList, dates, showAll) {
  return seriesList.map(s => ({
    x: dates, y: s.y, name: s.label,
    customdata: s.lu,
    mode: "lines+markers", connectgaps: false,
    visible: (s.featured || showAll) ? true : "legendonly",
    hovertemplate: s.label + "<br>%{x|%Y-%m-%d}: $%{y:.2f}/GPU/hr" +
                   "<br>quote updated: %{customdata}<extra></extra>",
  }));
}

function renderGpu(data) {
  const classes = Object.keys(data.classes);
  const order = ["on_demand", "spot", "reserved"];
  classes.sort((a, b) => (order.indexOf(a) + 99 * (order.indexOf(a) < 0)) -
                         (order.indexOf(b) + 99 * (order.indexOf(b) < 0)));
  if (!classes.length) { main.appendChild(el("p", {}, "No data yet — first collection run pending.")); return; }

  const tabs = el("div", {class: "tabs"});
  const tools = el("div", {class: "tools"});
  const showAllBtn = el("button", {}, "Show all providers");
  tools.appendChild(showAllBtn);
  const plot = el("div", {id: "plot"});
  main.append(tabs, tools, plot);

  let active = classes[0], showAll = false;
  const labelFor = c => ({on_demand: "Demand", spot: "Spot", reserved: "Reserved"}[c] || c);

  function draw() {
    const list = data.classes[active] || [];
    Plotly.react(plot, gpuTraces(list, data.pull_dates, showAll),
      Object.assign({}, LAYOUT, {yaxis: {title: "$ / GPU / hr", rangemode: "tozero"}}), PLOT_CFG);
    [...tabs.children].forEach(b => b.classList.toggle("active", b.dataset.c === active));
    showAllBtn.textContent = showAll ? "Featured only" : "Show all providers";
  }
  for (const c of classes) {
    const b = el("button", {"data-c": c}, labelFor(c));
    b.onclick = () => { active = c; draw(); };
    tabs.appendChild(b);
  }
  showAllBtn.onclick = () => { showAll = !showAll; draw(); };
  draw();
}

function priceTraces(series, dates, field, unit) {
  return series.map(s => ({
    x: dates, y: s[field], name: s.label, customdata: s.lu,
    mode: "lines+markers", connectgaps: false,
    hovertemplate: s.label + "<br>%{x|%Y-%m-%d}: $%{y}" + unit +
                   "<br>quote updated: %{customdata}<extra></extra>",
  }));
}

function section(title) {
  const h = el("h2", {}, title), d = el("div", {});
  main.append(h, d);
  return d;
}

function renderLlm(data) {
  if (!data.pull_dates.length) { main.appendChild(el("p", {}, "No data yet — first collection run pending.")); return; }
  const std = data.series.filter(s => s.pricing_type === "standard");
  const used = std.length ? std : data.series;
  Plotly.newPlot(section("Input $/1M tokens"), priceTraces(used, data.pull_dates, "input", "/1M in"),
    Object.assign({}, LAYOUT, {yaxis: {rangemode: "tozero"}}), PLOT_CFG);
  Plotly.newPlot(section("Output $/1M tokens"), priceTraces(used, data.pull_dates, "output", "/1M out"),
    Object.assign({}, LAYOUT, {yaxis: {rangemode: "tozero"}}), PLOT_CFG);
  const batch = data.series.filter(s => s.pricing_type === "batch");
  if (batch.length)
    Plotly.newPlot(section("Batch input $/1M tokens"), priceTraces(batch, data.pull_dates, "input", "/1M in (batch)"),
      Object.assign({}, LAYOUT, {yaxis: {rangemode: "tozero"}}), PLOT_CFG);
  if (data.bench.series.length) {
    Plotly.newPlot(section("Speed (tok/s) — site benchmark, best-effort"),
      data.bench.series.map(s => ({x: data.bench.dates, y: s.speed, name: s.label,
        mode: "lines+markers", connectgaps: false})),
      Object.assign({}, LAYOUT, {yaxis: {title: "tok/s", rangemode: "tozero"}}), PLOT_CFG);
    Plotly.newPlot(section("$ / performance — site benchmark, best-effort"),
      data.bench.series.map(s => ({x: data.bench.dates, y: s.perf, name: s.label,
        mode: "lines+markers", connectgaps: false})),
      Object.assign({}, LAYOUT, {yaxis: {rangemode: "tozero"}}), PLOT_CFG);
  }
}

function renderHealth(h) {
  const panel = el("div", {class: "health " + (h.status || "unknown")});
  panel.appendChild(el("h2", {}, "Last run"));
  const dl = el("dl", {});
  const add = (k, v) => { dl.appendChild(el("dt", {}, k)); dl.appendChild(el("dd", {}, String(v))); };
  add("When (UTC)", h.ts || "never");
  add("Status", h.status || "unknown");
  if (h.tracked_rows) add("Rows ingested", `GPU ${h.tracked_rows.gpu} · LLM ${h.tracked_rows.llm}`);
  if (h.new_rows) add("New / updated", `GPU +${h.new_rows.gpu}/${h.updated_rows.gpu} · LLM +${h.new_rows.llm}/${h.updated_rows.llm}`);
  if (h.rejected) add("Rejected rows", h.rejected);
  if (h.new_providers && h.new_providers.length) add("New providers", h.new_providers.join(", "));
  panel.appendChild(dl);
  if (h.warnings && h.warnings.length) {
    const ul = el("ul", {class: "warnings"});
    h.warnings.forEach(w => ul.appendChild(el("li", {}, w)));
    panel.appendChild(ul);
  }
  main.prepend(panel);
}

const kind = main.dataset.page;
fetch(main.dataset.src, {cache: "no-cache"})
  .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
  .then(data => {
    if (kind === "gpu") renderGpu(data);
    else if (kind === "llm") renderLlm(data);
    else renderHealth(data);
  })
  .catch(err => main.appendChild(el("p", {}, "No data yet (" + err.message + ") — first collection run pending.")));
"""

STYLE_CSS = """
:root { color-scheme: light dark; }
body { font: 16px/1.5 -apple-system, system-ui, sans-serif; margin: 0; padding: 0 12px 40px; max-width: 1000px; margin-inline: auto; }
header { display: flex; align-items: baseline; gap: 14px; flex-wrap: wrap; padding-top: 10px; }
header a { text-decoration: none; }
h1 { font-size: 1.3rem; margin: .4em 0; }
.tabs, .tools { display: flex; gap: 8px; margin: 8px 0; flex-wrap: wrap; }
button { padding: 6px 14px; border-radius: 8px; border: 1px solid #8884; background: transparent; cursor: pointer; font: inherit; }
.tabs button.active { background: #3b82f6; color: #fff; border-color: #3b82f6; }
#plot, main > div[id^=plot] { width: 100%; min-height: 420px; }
.health { border: 1px solid #8884; border-radius: 10px; padding: 10px 16px; margin: 12px 0; }
.health.suspicious, .health.error { border-color: #dc2626; }
.health dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 16px; margin: 0; }
.health dt { font-weight: 600; }
.health dd { margin: 0; }
.warnings { color: #b45309; }
ul.nav { list-style: none; padding: 0; display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 8px; }
ul.nav a { display: block; padding: 10px 14px; border: 1px solid #8884; border-radius: 10px; text-decoration: none; }
"""

INDEX_TMPL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>computeprices tracker</title>
<link rel="stylesheet" href="assets/style.css">
</head>
<body>
<header><h1>computeprices.com price tracker</h1></header>
<main id="main" data-page="health" data-src="data/health.json">
<h2>GPUs</h2>
<ul class="nav">
{gpu_links}
</ul>
<h2>LLM APIs</h2>
<ul class="nav">
{llm_links}
</ul>
</main>
<script src="assets/app.js"></script>
</body>
</html>
"""


def write_json(name: str, payload: dict) -> None:
    out = paths.DOCS_DIR / "data" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, separators=(",", ":"), sort_keys=True) + "\n",
                   encoding="utf-8")


def main() -> int:
    watch = Watchlist.load()
    resolutions = load_resolutions()
    # Adopt the collector's persisted resolutions (or fall back to the local
    # id) so slug sets include the canonical slug plus configured aliases.
    watch.gpu_slugs = {lid: resolutions.get("gpus", {}).get(lid, lid) for lid in watch.gpus}
    watch.llm_slugs = {lid: resolutions.get("llms", {}).get(lid, lid) for lid in watch.llm_models}
    names = registry_names()
    presence = store.read_csv(paths.PRESENCE_CSV)
    gpu_prices = store.read_csv(paths.GPU_PRICES_CSV)
    llm_prices = store.read_csv(paths.LLM_PRICES_CSV)
    bench = store.read_csv(paths.LLM_BENCH_CSV)

    assets = paths.DOCS_DIR / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "app.js").write_text(APP_JS.strip() + "\n", encoding="utf-8")
    (assets / "style.css").write_text(STYLE_CSS.strip() + "\n", encoding="utf-8")

    gpu_links, llm_links = [], []
    for local_id in watch.gpus:
        api_slug = watch.gpu_slugs[local_id]
        data = build_gpu_page_data(local_id, watch.gpu_slug_set(local_id), watch,
                                   presence, gpu_prices, names)
        write_json(f"gpu_{local_id}.json", data)
        title = f"GPU: {local_id}" if api_slug == local_id else f"GPU: {local_id} ({api_slug})"
        page = f"gpu_{local_id}.html"
        (paths.DOCS_DIR / page).write_text(PAGE_TMPL.format(
            title=html.escape(title), plotly=PLOTLY_CDN, page_kind="gpu",
            data_file=f"gpu_{local_id}.json"), encoding="utf-8")
        gpu_links.append(f'<li><a href="{page}">{html.escape(local_id)}</a></li>')

    for local_id in watch.llm_models:
        api_slug = watch.llm_slugs[local_id]
        data = build_llm_page_data(api_slug, watch.llm_slug_set(local_id),
                                   presence, llm_prices, bench, names)
        write_json(f"llm_{local_id}.json", data)
        page = f"llm_{local_id}.html"
        (paths.DOCS_DIR / page).write_text(PAGE_TMPL.format(
            title=html.escape(f"LLM: {local_id}"), plotly=PLOTLY_CDN, page_kind="llm",
            data_file=f"llm_{local_id}.json"), encoding="utf-8")
        llm_links.append(f'<li><a href="{page}">{html.escape(local_id)}</a></li>')

    health: dict = {"status": "unknown", "generated_at": datetime.now(timezone.utc).isoformat()}
    if paths.LAST_RUN_JSON.exists():
        try:
            health.update(json.loads(paths.LAST_RUN_JSON.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    write_json("health.json", health)

    (paths.DOCS_DIR / "index.html").write_text(INDEX_TMPL.format(
        gpu_links="\n".join(gpu_links), llm_links="\n".join(llm_links)),
        encoding="utf-8")
    # Pages serves docs/ as-is; .nojekyll skips the Jekyll build.
    (paths.DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    print(f"Dashboard written to {paths.DOCS_DIR} "
          f"({len(gpu_links)} GPU pages, {len(llm_links)} LLM pages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
