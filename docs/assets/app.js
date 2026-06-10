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
