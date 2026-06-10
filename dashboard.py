#!/usr/bin/env python3
"""
dashboard.py — Local web dashboard for the gpus.io price tracker
================================================================

Runs a Flask web server (default http://localhost:5000) that visualises the
SQLite price history written by ``scraper.py``:

  * a time-series line chart of price-over-time per GPU model / config
    (Plotly — click legend entries to toggle individual lines on/off);
  * a summary table of the latest prices vs. the previous scrape, with deltas;
  * a manual "Refresh" button, an optional auto-refresh, and a "Scrape now"
    button that runs the scraper on demand.

Run:        python dashboard.py            # or: python dashboard.py --port 5000
Then open:  http://localhost:5000
"""

from __future__ import annotations

import argparse
import json
import os
import socket
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from flask import Flask, redirect, render_template_string, request, url_for

import scraper  # local module — safe to import (no side effects at import time)

DB_PATH = Path(os.environ.get("GPU_DB", scraper.DB_PATH))
ALL_MODELS = [display for _slug, display in scraper.MODELS]

# A distinct colour per GPU model (consistent across the whole dashboard).
MODEL_COLORS = {
    "A100": "#2563eb",  # blue
    "H100": "#16a34a",  # green
    "H200": "#9333ea",  # purple
    "B200": "#ea580c",  # orange
    "B300": "#dc2626",  # red
}
# Line style per aggregate tier so same-coloured model lines stay readable.
TIER_DASH = {"Cheapest": "solid"}  # "(min)" -> dash, "(median)" -> dot (below)

app = Flask(__name__)


# --------------------------------------------------------------------------- #
# Data access
# --------------------------------------------------------------------------- #

def load_df() -> pd.DataFrame:
    """Load the full price history into a DataFrame (empty if no DB yet)."""
    import sqlite3
    if not DB_PATH.exists():
        return pd.DataFrame()
    conn = sqlite3.connect(str(DB_PATH))
    try:
        df = pd.read_sql_query("SELECT * FROM prices", conn)
    finally:
        conn.close()
    if df.empty:
        return df
    df["scrape_date"] = pd.to_datetime(df["scrape_date"])
    return df


def aggregate_categories(df: pd.DataFrame) -> list[str]:
    """The non-vendor 'tier' categories (Cheapest / *(min) / *(median))."""
    cats = [c for c in df["category"].unique()
            if c == "Cheapest" or c.endswith("(min)") or c.endswith("(median)")]
    # Stable order: Cheapest first, then the rest alphabetically.
    return ["Cheapest"] + sorted(c for c in cats if c != "Cheapest")


# --------------------------------------------------------------------------- #
# Chart
# --------------------------------------------------------------------------- #

def _dash_for(category: str) -> str:
    if category.endswith("(median)"):
        return "dot"
    if category.endswith("(min)"):
        return "dash"
    return TIER_DASH.get(category, "solid")


def build_figure(df: pd.DataFrame, view: str, models: list[str]) -> go.Figure:
    fig = go.Figure()
    if df.empty:
        fig.update_layout(
            title="No data yet — run <code>python scraper.py</code> first.",
            height=420)
        return fig

    df = df[df["gpu_model"].isin(models)]
    if view == "provider":
        sub = df[df["category"].str.contains("·", regex=False)]
    else:
        sub = df[df["category"].isin(aggregate_categories(df))]

    for (model, category), g in sub.groupby(["gpu_model", "category"]):
        g = g.sort_values("scrape_date")
        color = MODEL_COLORS.get(model, "#374151")
        if view == "aggregate":
            visible = True if category == "Cheapest" else "legendonly"
            dash = _dash_for(category)
        else:
            visible = True
            dash = "solid"
        customdata = list(zip(
            g["provider"].fillna("—"), g["rental_type"].fillna("—"),
            g["gpu_count"].fillna("—"), g["vram_gb"].fillna("—")))
        fig.add_trace(go.Scatter(
            x=g["scrape_date"], y=g["price"],
            mode="lines+markers",
            name=f"{model} — {category}",
            legendgroup=model,
            line=dict(color=color, dash=dash, width=2),
            marker=dict(size=6),
            visible=visible,
            customdata=customdata,
            hovertemplate=(
                f"<b>{model}</b> — {category}<br>"
                "%{x|%Y-%m-%d}: <b>$%{y:.4f}</b>/GPU/hr<br>"
                "provider=%{customdata[0]} · %{customdata[1]}<br>"
                "GPUs=%{customdata[2]} · VRAM=%{customdata[3]}GB"
                "<extra></extra>"),
        ))

    fig.update_layout(
        title=("Cloud GPU rental price over time "
               f"({'vendor breakdown' if view == 'provider' else 'price tiers'})"),
        xaxis_title="Scrape date",
        yaxis_title="USD per GPU per hour",
        yaxis=dict(tickprefix="$"),
        height=560,
        hovermode="closest",
        legend=dict(title="Click to toggle · double-click to isolate",
                    orientation="v", x=1.02, y=1, font=dict(size=11)),
        margin=dict(l=60, r=240, t=60, b=50),
        template="plotly_white",
    )
    return fig


# --------------------------------------------------------------------------- #
# Summary table (latest vs. previous scrape)
# --------------------------------------------------------------------------- #

SUMMARY_TIERS = ["Cheapest", "On-demand (min)", "Spot (min)"]


def build_summary(df: pd.DataFrame, models: list[str]):
    if df.empty:
        return None, None, []
    dates = sorted(df["scrape_date"].unique())
    latest = dates[-1]
    prev = dates[-2] if len(dates) > 1 else None

    def price_of(model, cat, date):
        if date is None:
            return None
        m = df[(df.gpu_model == model) & (df.category == cat) & (df.scrape_date == date)]
        return float(m["price"].iloc[0]) if not m.empty else None

    rows = []
    for model in models:
        for cat in SUMMARY_TIERS:
            cur = price_of(model, cat, latest)
            old = price_of(model, cat, prev)
            if cur is None:
                continue
            delta = (cur - old) if old is not None else None
            pct = (delta / old * 100) if (delta is not None and old) else None
            rows.append(dict(model=model, category=cat, current=cur,
                             previous=old, delta=delta, pct=pct))
    fmt = lambda d: pd.Timestamp(d).strftime("%Y-%m-%d") if d is not None else None
    return fmt(latest), fmt(prev), rows


# --------------------------------------------------------------------------- #
# HTML template
# --------------------------------------------------------------------------- #

PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{% if auto %}<meta http-equiv="refresh" content="60">{% endif %}
<title>GPU Price Tracker — gpus.io</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  :root { --fg:#111827; --muted:#6b7280; --line:#e5e7eb; --bg:#f9fafb; }
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         margin: 0; color: var(--fg); background: var(--bg); }
  header { background:#111827; color:#fff; padding:16px 24px; display:flex;
           align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; }
  header h1 { font-size:18px; margin:0; font-weight:600; }
  header .meta { color:#9ca3af; font-size:13px; }
  main { max-width:1200px; margin:0 auto; padding:20px 24px 60px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:10px;
          padding:18px; margin-bottom:20px; }
  form.controls { display:flex; gap:18px; align-items:center; flex-wrap:wrap; }
  .controls fieldset { border:1px solid var(--line); border-radius:8px; padding:8px 12px; margin:0; }
  .controls legend { font-size:12px; color:var(--muted); padding:0 4px; }
  label.chk { margin-right:10px; font-size:14px; white-space:nowrap; }
  .btn { border:1px solid var(--line); background:#fff; border-radius:8px;
         padding:7px 14px; font-size:14px; cursor:pointer; text-decoration:none;
         color:var(--fg); display:inline-block; }
  .btn.primary { background:#2563eb; color:#fff; border-color:#2563eb; }
  .btn.dark { background:#111827; color:#fff; border-color:#111827; }
  table { border-collapse:collapse; width:100%; font-size:14px; }
  th, td { text-align:right; padding:8px 10px; border-bottom:1px solid var(--line); }
  th:first-child, td:first-child, th:nth-child(2), td:nth-child(2) { text-align:left; }
  thead th { color:var(--muted); font-weight:600; font-size:12px; text-transform:uppercase; }
  .up { color:#dc2626; } .down { color:#16a34a; } .flat { color:var(--muted); }
  .pill { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; vertical-align:middle; }
  .hint { color:var(--muted); font-size:13px; margin-top:6px; }
  h2 { font-size:15px; margin:0 0 12px; }
</style>
</head>
<body>
<header>
  <h1>⚡ GPU Price Tracker <span style="font-weight:400;color:#9ca3af">· gpus.io cloud rental</span></h1>
  <div class="meta">
    {% if latest %}Latest scrape: <b style="color:#fff">{{ latest }}</b>
      {% if prev %} · vs {{ prev }}{% endif %}
    {% else %}No data yet{% endif %}
  </div>
</header>
<main>

  <div class="card">
    <form class="controls" method="get" action="{{ url_for('index') }}">
      <fieldset>
        <legend>GPU models</legend>
        {% for m in all_models %}
          <label class="chk"><input type="checkbox" name="models" value="{{ m }}"
            {% if m in sel_models %}checked{% endif %}>
            <span class="pill" style="background:{{ colors[m] }}"></span>{{ m }}</label>
        {% endfor %}
      </fieldset>
      <fieldset>
        <legend>View</legend>
        <label class="chk"><input type="radio" name="view" value="aggregate"
          {% if view=='aggregate' %}checked{% endif %}> Price tiers</label>
        <label class="chk"><input type="radio" name="view" value="provider"
          {% if view=='provider' %}checked{% endif %}> Vendor breakdown</label>
      </fieldset>
      <fieldset>
        <legend>Auto-refresh</legend>
        <label class="chk"><input type="checkbox" name="auto" value="1"
          {% if auto %}checked{% endif %}> every 60s</label>
      </fieldset>
      <button class="btn primary" type="submit">Apply</button>
      <a class="btn" href="{{ refresh_url }}">↻ Refresh</a>
      <button class="btn dark" type="submit" formmethod="post"
        formaction="{{ url_for('scrape_now') }}">⟳ Scrape now</button>
    </form>
    <div class="hint">Tip: click a legend entry to toggle a line on/off; double-click to isolate one.</div>
  </div>

  <div class="card">
    <div id="chart" style="width:100%;"></div>
  </div>

  <div class="card">
    <h2>Today vs. previous scrape{% if prev %} ({{ latest }} vs {{ prev }}){% endif %}</h2>
    {% if rows %}
    <table>
      <thead><tr>
        <th>Model</th><th>Tier</th><th>Now ($/GPU/hr)</th>
        <th>Previous</th><th>Δ</th><th>Δ%</th>
      </tr></thead>
      <tbody>
      {% for r in rows %}
        <tr>
          <td><span class="pill" style="background:{{ colors.get(r.model,'#888') }}"></span>{{ r.model }}</td>
          <td>{{ r.category }}</td>
          <td>${{ '%.4f'|format(r.current) }}</td>
          <td>{{ '$%.4f'|format(r.previous) if r.previous is not none else '—' }}</td>
          {% if r.delta is none %}
            <td class="flat">—</td><td class="flat">—</td>
          {% else %}
            {% set cls = 'down' if r.delta < 0 else ('up' if r.delta > 0 else 'flat') %}
            {% set arrow = '▼' if r.delta < 0 else ('▲' if r.delta > 0 else '·') %}
            <td class="{{ cls }}">{{ arrow }} ${{ '%.4f'|format(r.delta|abs) }}</td>
            <td class="{{ cls }}">{{ '%+.1f'|format(r.pct) }}%</td>
          {% endif %}
        </tr>
      {% endfor %}
      </tbody>
    </table>
    <div class="hint">Green = price dropped, red = price rose. Tiers shown:
      {{ summary_tiers|join(', ') }}. A "—" previous means no earlier scrape for that tier yet.</div>
    {% else %}
      <p class="hint">No rows to summarise. Run <code>python scraper.py</code> to collect data.</p>
    {% endif %}
  </div>

</main>
<script>
  var fig = {{ chart_json|safe }};
  Plotly.newPlot('chart', fig.data, fig.layout, {responsive:true, displaylogo:false});
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

def _selected_models() -> list[str]:
    sel = request.args.getlist("models")
    sel = [m for m in sel if m in ALL_MODELS]
    return sel or list(ALL_MODELS)


@app.route("/")
def index():
    view = request.args.get("view", "aggregate")
    if view not in ("aggregate", "provider"):
        view = "aggregate"
    auto = request.args.get("auto") == "1"
    sel_models = _selected_models()

    df = load_df()
    fig = build_figure(df, view, sel_models)
    latest, prev, rows = build_summary(df, sel_models)

    # "Refresh" link keeps current filters but is a fresh GET.
    refresh_args = {"view": view, "models": sel_models}
    if auto:
        refresh_args["auto"] = "1"
    refresh_url = url_for("index", **refresh_args)

    return render_template_string(
        PAGE,
        chart_json=pio.to_json(fig),
        all_models=ALL_MODELS, sel_models=sel_models, colors=MODEL_COLORS,
        view=view, auto=auto, latest=latest, prev=prev, rows=rows,
        summary_tiers=SUMMARY_TIERS, refresh_url=refresh_url,
    )


@app.route("/scrape", methods=["POST"])
def scrape_now():
    """Run the scraper synchronously, then return to the dashboard."""
    try:
        scraper.run(scraper.DB_PATH, quiet=True)
    except Exception as exc:  # noqa: BLE001 - surface but don't crash the server
        app.logger.error("Manual scrape failed: %s", exc)
    return redirect(url_for("index", **{k: v for k, v in request.form.items()
                                        if k in ("view", "auto")}))


@app.route("/health")
def health():
    return {"status": "ok", "db": str(DB_PATH), "exists": DB_PATH.exists()}


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def _find_free_port(host: str, start: int, attempts: int = 25) -> int | None:
    for port in range(start, start + attempts):
        if _port_is_free(host, port):
            return port
    return None


def main():
    p = argparse.ArgumentParser(description="GPU price tracker dashboard")
    p.add_argument("--host", default="127.0.0.1")
    # NB: default is 5050, not 5000 — on macOS port 5000 is held by Control
    # Center / AirPlay Receiver (incl. IPv6 ::1, where `localhost` resolves
    # first), so http://localhost:5000 would hit AirPlay instead of this app.
    p.add_argument("--port", type=int, default=5050)
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()

    host, port = args.host, args.port
    # If the chosen port is busy, fall back to the next free one and say so.
    if not _port_is_free(host, port):
        alt = _find_free_port(host, port + 1)
        note = ("often macOS Control Center/AirPlay Receiver"
                if port == 5000 else "already in use")
        if alt is None:
            print(f"Port {port} is busy ({note}) and no free port was found nearby.")
            print("Try a specific one, e.g.:  python dashboard.py --port 8000")
            return
        print(f"⚠  Port {port} is busy ({note}); using {alt} instead.")
        print(f"   (To silence this, run: python dashboard.py --port {alt})")
        port = alt

    print(f"GPU Price Tracker dashboard → http://{host}:{port}  (DB: {DB_PATH})")
    print("   Press Ctrl+C to stop.")
    app.run(host=host, port=port, debug=args.debug)


if __name__ == "__main__":
    main()
