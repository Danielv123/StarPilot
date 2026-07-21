#!/usr/bin/env python3
from __future__ import annotations

import argparse
import bz2
from collections import defaultdict
from datetime import datetime
import json
import math
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from statistics import mean
import sys
from time import perf_counter
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(REPO_ROOT))

try:
  import capnp
  import zstandard as zstd
  from cereal import log
except ModuleNotFoundError as e:
  missing = e.name or str(e)
  raise SystemExit(
    f"Missing dependency {missing!r}. Run with:\n"
    "  uv run --no-project --with pycapnp==2.1.0 --with zstandard python tools/tuning_viewer.py\n"
  ) from e


DEFAULT_LOG_ROOT = Path(r"D:\comma_driving_logs\10.30.1.75\realdata")
LOG_TYPES = ("qlog", "rlog")
SEGMENT_RE = re.compile(r"^(?P<trip>.+)--(?P<segment>\d+)$")
ACTIVE_SAMPLE_CACHE: dict[tuple[str, int, int], int] = {}
LOG_START_TIME_CACHE: dict[tuple[str, int, int], tuple[float | None, str]] = {}
START_TIME_PREFIX_BYTES = 3 * 1024 * 1024
START_TIME_MAX_EVENTS = 20000
START_TIME_FILE_BUDGET_S = 0.5
START_TIME_MAX_SEGMENTS = 3


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>StarPilot Tuning Viewer</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101316;
      --panel: #181d22;
      --panel-2: #20262c;
      --line: #313a43;
      --text: #e9eef2;
      --muted: #94a1ad;
      --accent: #4dd4ac;
      --warn: #ffcc66;
      --bad: #ff6b7a;
      --blue: #6ca8ff;
      --purple: #c792ea;
    }

    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: Inter, "Segoe UI", system-ui, sans-serif;
      letter-spacing: 0;
    }

    .app {
      display: grid;
      grid-template-columns: 340px minmax(0, 1fr);
      min-height: 100vh;
    }

    aside {
      border-right: 1px solid var(--line);
      background: #14181d;
      display: flex;
      flex-direction: column;
      min-height: 100vh;
    }

    .side-head {
      padding: 18px;
      border-bottom: 1px solid var(--line);
    }

    h1 {
      margin: 0 0 10px;
      font-size: 20px;
      font-weight: 650;
    }

    .controls {
      display: grid;
      gap: 10px;
    }

    label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
    }

    .check-row {
      display: flex;
      align-items: center;
      gap: 8px;
      text-transform: none;
      font-size: 13px;
      color: var(--text);
    }

    .check-row input {
      width: 16px;
      height: 16px;
      padding: 0;
      accent-color: var(--accent);
    }

    select, input, button {
      width: 100%;
      border: 1px solid var(--line);
      background: var(--panel-2);
      color: var(--text);
      padding: 9px 10px;
      border-radius: 6px;
      font: inherit;
    }

    button {
      cursor: pointer;
      background: #263039;
      font-weight: 650;
    }

    button:hover { border-color: var(--accent); }

    .route-list {
      overflow: auto;
      padding: 10px;
      display: grid;
      gap: 8px;
    }

    .route {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 10px;
      cursor: pointer;
    }

    .route.active {
      border-color: var(--accent);
      background: #1b2827;
    }

    .route-name {
      font-size: 13px;
      font-weight: 650;
      overflow-wrap: anywhere;
    }

    .route-meta {
      margin-top: 5px;
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      color: var(--muted);
      font-size: 12px;
    }

    main {
      min-width: 0;
      padding: 18px;
      display: grid;
      gap: 16px;
      align-content: start;
    }

    .topbar {
      display: flex;
      justify-content: space-between;
      gap: 14px;
      align-items: end;
    }

    .title-block h2 {
      margin: 0 0 5px;
      font-size: 22px;
    }

    .subtitle {
      color: var(--muted);
      font-size: 13px;
      overflow-wrap: anywhere;
    }

    .metrics {
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 10px;
    }

    .metric {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-height: 86px;
    }

    .metric .k {
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      text-transform: uppercase;
    }

    .metric .v {
      margin-top: 8px;
      font-size: 24px;
      font-weight: 720;
    }

    .metric .s {
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
    }

    .chart-panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      min-width: 0;
    }

    .chart-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      margin-bottom: 8px;
    }

    .chart-title {
      font-weight: 700;
      font-size: 14px;
    }

    .legend {
      display: flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 10px;
      color: var(--muted);
      font-size: 12px;
    }

    .legend span {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      white-space: nowrap;
    }

    .swatch {
      width: 12px;
      height: 3px;
      border-radius: 999px;
      display: inline-block;
    }

    canvas {
      display: block;
      width: 100%;
      height: 260px;
      background: #11161a;
      border-radius: 6px;
    }

    .notice {
      border: 1px solid var(--line);
      background: var(--panel);
      border-radius: 8px;
      padding: 14px;
      color: var(--muted);
    }

    .table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }

    .table th, .table td {
      border-bottom: 1px solid var(--line);
      padding: 8px;
      text-align: left;
    }

    .table th { color: var(--muted); font-weight: 650; }

    .tune-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(140px, 1fr));
      gap: 10px;
      margin-bottom: 12px;
    }

    .tune-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #151a1f;
      padding: 11px;
      min-width: 0;
    }

    .tune-card .k {
      color: var(--muted);
      font-size: 11px;
      font-weight: 650;
      text-transform: uppercase;
    }

    .tune-card .v {
      margin-top: 7px;
      font-size: 19px;
      font-weight: 720;
    }

    .tune-card .s {
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.35;
    }

    .param-list {
      display: grid;
      gap: 8px;
    }

    .param-row {
      display: grid;
      grid-template-columns: minmax(210px, 1fr) 120px 120px minmax(220px, 1.3fr);
      gap: 10px;
      align-items: baseline;
      border-top: 1px solid var(--line);
      padding-top: 8px;
      font-size: 13px;
    }

    .param-row:first-child {
      border-top: 0;
      padding-top: 0;
    }

    .param-name {
      font-family: Consolas, "SFMono-Regular", monospace;
      font-size: 12px;
      overflow-wrap: anywhere;
    }

    .param-reason {
      color: var(--muted);
      line-height: 1.35;
    }

    .code-preview {
      margin: 12px 0 0;
      overflow: auto;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #0e1216;
      padding: 10px;
      color: #dce6ee;
      font: 12px Consolas, "SFMono-Regular", monospace;
      white-space: pre;
    }

    .warn-list {
      margin-top: 10px;
      display: grid;
      gap: 5px;
      color: var(--warn);
      font-size: 12px;
    }

    @media (max-width: 1000px) {
      .app { grid-template-columns: 1fr; }
      aside { min-height: auto; max-height: 48vh; }
      .metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .tune-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .param-row { grid-template-columns: 1fr; }
      .topbar { display: grid; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="side-head">
        <h1>Torque Tune Data</h1>
        <div class="controls">
          <label>Log Root
            <input id="rootInput" />
          </label>
          <label>Log Type
            <select id="logType">
              <option value="rlog" selected>rlog, higher resolution</option>
              <option value="qlog">qlog, faster</option>
            </select>
          </label>
          <label class="check-row">
            <input id="activeOnly" type="checkbox" />
            <span>Only Active Trips</span>
          </label>
          <button id="refreshBtn">Refresh Routes</button>
        </div>
      </div>
      <div id="routes" class="route-list"></div>
    </aside>
    <main>
      <div class="topbar">
        <div class="title-block">
          <h2 id="routeTitle">Select a route segment</h2>
          <div id="routeSub" class="subtitle">Tracking score uses active torque-state samples only.</div>
        </div>
      </div>

      <section class="metrics" id="metrics"></section>

      <section class="chart-panel">
        <div class="chart-head">
          <div class="chart-title">Tune Fit</div>
        </div>
        <div id="tunePanel" class="notice">Load active rlog data to fit a personalized torque model.</div>
      </section>

      <section class="chart-panel">
        <div class="chart-head">
          <div class="chart-title">Lateral Acceleration Tracking</div>
          <div class="legend" id="latLegend"></div>
        </div>
        <canvas id="latChart"></canvas>
      </section>

      <section class="chart-panel">
        <div class="chart-head">
          <div class="chart-title">Torque Controller Terms</div>
          <div class="legend" id="torqueLegend"></div>
        </div>
        <canvas id="torqueChart"></canvas>
      </section>

      <section class="chart-panel">
        <div class="chart-head">
          <div class="chart-title">Steering And Speed Context</div>
          <div class="legend" id="steerLegend"></div>
        </div>
        <canvas id="steerChart"></canvas>
      </section>

      <section class="chart-panel">
        <div class="chart-head">
          <div class="chart-title">Scores By Phase</div>
        </div>
        <table class="table" id="phaseTable"></table>
      </section>

      <div class="notice" id="status">Waiting for a route.</div>
    </main>
  </div>

  <script>
    const state = {
      routes: [],
      selectedPath: "",
      data: null,
    };

    const colors = {
      desired: "#4dd4ac",
      actual: "#6ca8ff",
      error: "#ff6b7a",
      output: "#c792ea",
      p: "#5ad1ff",
      i: "#ffcc66",
      d: "#ff7db6",
      f: "#8ee36f",
      steer: "#e9eef2",
      speed: "#8ab4f8",
      torque: "#f78c6c",
    };

    function fmt(value, digits = 3) {
      if (value === null || value === undefined || Number.isNaN(value)) return "n/a";
      if (Math.abs(value) >= 100) return Number(value).toFixed(0);
      if (Math.abs(value) >= 10) return Number(value).toFixed(1);
      return Number(value).toFixed(digits);
    }

    function setStatus(text) {
      document.getElementById("status").textContent = text;
    }

    async function fetchJson(url) {
      const res = await fetch(url);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    }

    function routeSize(route, type) {
      const key = type + "_bytes";
      if (!route[key]) return "";
      const mb = route[key] / (1024 * 1024);
      return `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
    }

    function renderRoutes() {
      const box = document.getElementById("routes");
      const logType = document.getElementById("logType").value;
      box.replaceChildren();
      for (const route of state.routes) {
        const hasLog = route[logType];
        const activeSamples = route.active_samples;
        const activeLabel = Number.isFinite(activeSamples) ? `${activeSamples} active` : "";
        const el = document.createElement("div");
        el.className = "route" + (route.path === state.selectedPath ? " active" : "");
        el.innerHTML = `
          <div class="route-name">${route.name}</div>
          <div class="route-meta">
            <span>${route.segment_count || 1} segments</span>
            <span>${routeSize(route, "qlog") || "no qlog"}</span>
            <span>${routeSize(route, "rlog") || "no rlog"}</span>
            ${activeLabel ? `<span>${activeLabel}</span>` : ""}
            <span>${route.started || "no log start time"}</span>
          </div>`;
        el.style.opacity = hasLog ? "1" : "0.45";
        el.onclick = () => hasLog && loadRoute(route.path);
        box.appendChild(el);
      }
    }

    async function refreshRoutes() {
      const root = document.getElementById("rootInput").value;
      const logType = document.getElementById("logType").value;
      const activeOnly = document.getElementById("activeOnly").checked;
      setStatus(activeOnly ? "Scanning trips and active rlog samples..." : "Scanning trip directory...");
      const data = await fetchJson(`/api/routes?root=${encodeURIComponent(root)}&log=${encodeURIComponent(logType)}&active_only=${activeOnly ? "1" : "0"}`);
      state.routes = data.routes;
      document.getElementById("rootInput").value = data.root;
      renderRoutes();
      setStatus(`${data.routes.length} trips found.`);
    }

    async function loadRoute(path) {
      state.selectedPath = path;
      renderRoutes();
      const logType = document.getElementById("logType").value;
      document.getElementById("routeTitle").textContent = path.split(/[\\/]/).pop();
      document.getElementById("routeSub").textContent = `${logType}.zst, grouped trip`;
      setStatus("Parsing and scoring log data...");
      const data = await fetchJson(`/api/analyze?path=${encodeURIComponent(path)}&log=${encodeURIComponent(logType)}`);
      state.data = data;
      renderData(data);
      setStatus(`Loaded ${data.samples.length} aligned control samples from ${data.segment_count || 1} ${data.log_type} segments.`);
    }

    function renderMetrics(data) {
      const s = data.summary;
      const metricDefs = [
        ["Tracking", `${fmt(s.tracking_score, 1)}`, "0-100, higher is better"],
        ["RMSE", `${fmt(s.rmse)} m/s^2`, "desired vs actual"],
        ["MAE", `${fmt(s.mae)} m/s^2`, "absolute tracking error"],
        ["P95 Error", `${fmt(s.p95_abs_error)} m/s^2`, "active samples"],
        ["Straight Osc.", `${fmt(s.straight_oscillations_per_min, 1)}/min`, "near-zero desired accel"],
        ["Steer Snaps", `${fmt(s.steering_snap_count, 0)}`, "angle jumps over 5 deg"],
      ];
      const box = document.getElementById("metrics");
      box.replaceChildren();
      for (const [k, v, sub] of metricDefs) {
        const el = document.createElement("div");
        el.className = "metric";
        el.innerHTML = `<div class="k">${k}</div><div class="v">${v}</div><div class="s">${sub}</div>`;
        box.appendChild(el);
      }
    }

    function makeLegend(id, series) {
      const el = document.getElementById(id);
      el.replaceChildren();
      for (const s of series) {
        const item = document.createElement("span");
        item.innerHTML = `<span class="swatch" style="background:${s.color}"></span>${s.label}`;
        el.appendChild(item);
      }
    }

    function drawChart(canvas, samples, series, options = {}) {
      const rect = canvas.getBoundingClientRect();
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.max(1, Math.floor(rect.width * dpr));
      canvas.height = Math.max(1, Math.floor(rect.height * dpr));
      const ctx = canvas.getContext("2d");
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      const w = rect.width;
      const h = rect.height;
      const pad = { l: 58, r: 14, t: 12, b: 28 };
      ctx.clearRect(0, 0, w, h);

      if (!samples.length) return;
      const x0 = samples[0].t;
      const x1 = samples[samples.length - 1].t || 1;
      let yMin = Infinity;
      let yMax = -Infinity;
      for (const row of samples) {
        for (const s of series) {
          const v = row[s.key];
          if (typeof v === "number" && Number.isFinite(v)) {
            yMin = Math.min(yMin, v);
            yMax = Math.max(yMax, v);
          }
        }
      }
      if (options.zero) {
        yMin = Math.min(yMin, 0);
        yMax = Math.max(yMax, 0);
      }
      if (!Number.isFinite(yMin) || !Number.isFinite(yMax)) return;
      if (Math.abs(yMax - yMin) < 1e-6) {
        yMax += 1;
        yMin -= 1;
      }
      const yPad = (yMax - yMin) * 0.08;
      yMin -= yPad;
      yMax += yPad;

      const plotW = w - pad.l - pad.r;
      const plotH = h - pad.t - pad.b;
      const xScale = (t) => pad.l + ((t - x0) / Math.max(x1 - x0, 1e-6)) * plotW;
      const yScale = (v) => pad.t + (1 - (v - yMin) / (yMax - yMin)) * plotH;

      ctx.fillStyle = "rgba(77, 212, 172, 0.08)";
      let spanStart = null;
      for (let i = 0; i <= samples.length; i++) {
        const row = samples[i];
        const active = row && row.active;
        if (active && spanStart === null) {
          spanStart = row.t;
        } else if ((!active || i === samples.length) && spanStart !== null) {
          const endT = samples[Math.max(0, i - 1)].t;
          const x = xScale(spanStart);
          const xEnd = xScale(endT);
          ctx.fillRect(x, pad.t, Math.max(1, xEnd - x), plotH);
          spanStart = null;
        }
      }

      ctx.strokeStyle = "#28313a";
      ctx.lineWidth = 1;
      ctx.fillStyle = "#94a1ad";
      ctx.font = "12px Segoe UI, sans-serif";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";
      for (let i = 0; i <= 4; i++) {
        const y = pad.t + (plotH * i / 4);
        const val = yMax - ((yMax - yMin) * i / 4);
        ctx.beginPath();
        ctx.moveTo(pad.l, y);
        ctx.lineTo(w - pad.r, y);
        ctx.stroke();
        ctx.fillText(fmt(val, 2), pad.l - 8, y);
      }
      if (yMin < 0 && yMax > 0) {
        ctx.strokeStyle = "#4a5661";
        const y = yScale(0);
        ctx.beginPath();
        ctx.moveTo(pad.l, y);
        ctx.lineTo(w - pad.r, y);
        ctx.stroke();
      }
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (let i = 0; i <= 5; i++) {
        const t = x0 + ((x1 - x0) * i / 5);
        const x = xScale(t);
        ctx.fillText(`${fmt(t, 1)}s`, x, h - pad.b + 8);
      }

      for (const s of series) {
        ctx.strokeStyle = s.color;
        ctx.lineWidth = s.width || 1.8;
        ctx.beginPath();
        let started = false;
        for (const row of samples) {
          const v = row[s.key];
          if (typeof v !== "number" || !Number.isFinite(v)) {
            started = false;
            continue;
          }
          const x = xScale(row.t);
          const y = yScale(v);
          if (!started) {
            ctx.moveTo(x, y);
            started = true;
          } else {
            ctx.lineTo(x, y);
          }
        }
        ctx.stroke();
      }
    }

    function renderPhaseTable(data) {
      const table = document.getElementById("phaseTable");
      table.innerHTML = `<thead><tr><th>Phase</th><th>Samples</th><th>RMSE</th><th>MAE</th><th>P95 Abs Error</th><th>Bias</th></tr></thead>`;
      const body = document.createElement("tbody");
      for (const row of data.phase_scores) {
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>${row.phase}</td>
          <td>${row.samples}</td>
          <td>${fmt(row.rmse)}</td>
          <td>${fmt(row.mae)}</td>
          <td>${fmt(row.p95_abs_error)}</td>
          <td>${fmt(row.bias)}</td>`;
        body.appendChild(tr);
      }
      table.appendChild(body);
    }

    function renderTunePanel(data) {
      const panel = document.getElementById("tunePanel");
      const tune = data.tune_fit;
      if (!tune || tune.status !== "ok") {
        const reason = tune && tune.reason ? tune.reason : "Not enough clean active samples to fit a model.";
        panel.className = "notice";
        panel.textContent = reason;
        return;
      }

      panel.className = "";
      const m = tune.model;
      const b = tune.backtest;
      const cards = [
        ["Command Signal", tune.command_signal, `${tune.sample_count} clean samples, ${fmt(tune.delay_s, 2)}s delay`],
        ["Fitted Gain", `${fmt(m.gain)} m/s^2/torque`, `logged median ${fmt(m.current_lat_accel_factor)} m/s^2/torque`],
        ["Torque Scale", `${fmt(m.recommended_torque_scale, 3)}x`, "model-optimal scale on logged torque commands"],
        ["Model Backtest", `${fmt(b.phase_candidate_score, 1)}`, `score ${fmt(b.logged_score, 1)} -> ${fmt(b.phase_candidate_score, 1)}`],
      ];
      const cardHtml = cards.map(([k, v, s]) => `
        <div class="tune-card">
          <div class="k">${k}</div>
          <div class="v">${v}</div>
          <div class="s">${s}</div>
        </div>`).join("");

      const recHtml = tune.recommendations.map(r => `
        <div class="param-row">
          <div class="param-name">${r.parameter}</div>
          <div>${fmt(r.current, 4)}</div>
          <div>${fmt(r.recommended, 4)}</div>
          <div class="param-reason">${r.reason}</div>
        </div>`).join("");
      const warnings = (tune.warnings || []).map(w => `<div>${w}</div>`).join("");
      panel.innerHTML = `
        <div class="tune-grid">${cardHtml}</div>
        <div class="param-list">${recHtml || "<div class='param-reason'>No parameter deltas recommended from this sample set.</div>"}</div>
        ${tune.patch_preview && tune.patch_preview.length ? `<pre class="code-preview">${tune.patch_preview.join("\n")}</pre>` : ""}
        ${warnings ? `<div class="warn-list">${warnings}</div>` : ""}`;
    }

    function renderData(data) {
      renderMetrics(data);
      renderTunePanel(data);
      const samples = data.samples;
      const latSeries = [
        { key: "desired", label: "desired lat accel", color: colors.desired, width: 2.2 },
        { key: "actual", label: "actual lat accel", color: colors.actual, width: 2.2 },
        { key: "error", label: "error", color: colors.error, width: 1.3 },
      ];
      const torqueSeries = [
        { key: "output", label: "controller output", color: colors.output, width: 2 },
        { key: "commanded_torque", label: "commanded torque", color: colors.torque, width: 1.6 },
        { key: "p", label: "P", color: colors.p, width: 1 },
        { key: "i", label: "I", color: colors.i, width: 1 },
        { key: "d", label: "D", color: colors.d, width: 1 },
        { key: "f", label: "F", color: colors.f, width: 1 },
      ];
      const steerSeries = [
        { key: "steering_angle", label: "steering angle deg", color: colors.steer, width: 1.8 },
        { key: "v_ego_mps", label: "speed m/s", color: colors.speed, width: 1.4 },
        { key: "steering_torque_eps", label: "EPS torque", color: colors.torque, width: 1.2 },
      ];
      makeLegend("latLegend", latSeries);
      makeLegend("torqueLegend", torqueSeries);
      makeLegend("steerLegend", steerSeries);
      for (const id of ["latLegend", "torqueLegend", "steerLegend"]) {
        const el = document.getElementById(id);
        const item = document.createElement("span");
        item.innerHTML = `<span class="swatch" style="background:rgba(77, 212, 172, 0.35)"></span>active interval`;
        el.appendChild(item);
      }
      drawChart(document.getElementById("latChart"), samples, latSeries, { zero: true });
      drawChart(document.getElementById("torqueChart"), samples, torqueSeries, { zero: true });
      drawChart(document.getElementById("steerChart"), samples, steerSeries, { zero: true });
      renderPhaseTable(data);
    }

    window.addEventListener("resize", () => state.data && renderData(state.data));
    document.getElementById("refreshBtn").onclick = () => refreshRoutes().catch(e => setStatus(e.message));
    document.getElementById("logType").onchange = () => {
      refreshRoutes().catch(e => setStatus(e.message));
      if (state.selectedPath) loadRoute(state.selectedPath).catch(e => setStatus(e.message));
    };
    document.getElementById("activeOnly").onchange = () => refreshRoutes().catch(e => setStatus(e.message));

    fetchJson("/api/config").then(data => {
      document.getElementById("rootInput").value = data.root;
      return refreshRoutes();
    }).catch(e => setStatus(e.message));
  </script>
</body>
</html>
"""


def finite_float(value: Any, default: float | None = None) -> float | None:
  try:
    out = float(value)
  except Exception:
    return default
  return out if math.isfinite(out) else default


def safe_get(obj: Any, attr: str, default: Any = None) -> Any:
  try:
    return getattr(obj, attr)
  except Exception:
    return default


def percentile(values: list[float], pct: float) -> float | None:
  if not values:
    return None
  vals = sorted(values)
  idx = (len(vals) - 1) * pct / 100.0
  lo = int(math.floor(idx))
  hi = int(math.ceil(idx))
  if lo == hi:
    return vals[lo]
  return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)


def stats_for(rows: list[dict[str, Any]], phase: str) -> dict[str, Any]:
  errors = [float(r["error"]) for r in rows if finite_float(r.get("error")) is not None]
  if not errors:
    return {
      "phase": phase,
      "samples": 0,
      "rmse": None,
      "mae": None,
      "p95_abs_error": None,
      "bias": None,
    }
  abs_errors = [abs(e) for e in errors]
  return {
    "phase": phase,
    "samples": len(errors),
    "rmse": math.sqrt(mean([e * e for e in errors])),
    "mae": mean(abs_errors),
    "p95_abs_error": percentile(abs_errors, 95),
    "bias": mean(errors),
  }


def score_from_rmse(rmse: float | None) -> float | None:
  if rmse is None:
    return None
  tolerance = 0.15
  return max(0.0, min(100.0, 100.0 / (1.0 + (rmse / tolerance) ** 2)))


IONIQ_5_TUNE_BASE = {
  "IONIQ_5_BASE_LAT_ACCEL_FACTOR_MULT": 1.2446,
  "IONIQ_5_TURN_IN_BOOST_LEFT": 0.14,
  "IONIQ_5_TURN_IN_BOOST_RIGHT": 0.06,
  "IONIQ_5_UNWIND_TAPER_LEFT": 0.76,
  "IONIQ_5_UNWIND_TAPER_RIGHT": 0.85,
  "IONIQ_5_CENTER_TAPER_MAX": 0.28,
}


def median_float(values: list[float]) -> float | None:
  vals = sorted(v for v in values if math.isfinite(v))
  if not vals:
    return None
  mid = len(vals) // 2
  if len(vals) % 2:
    return vals[mid]
  return (vals[mid - 1] + vals[mid]) / 2.0


def clamp(value: float, lo: float, hi: float) -> float:
  return min(max(value, lo), hi)


def solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
  n = len(vector)
  a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
  for col in range(n):
    pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
    if abs(a[pivot][col]) < 1e-9:
      return None
    a[col], a[pivot] = a[pivot], a[col]
    div = a[col][col]
    for j in range(col, n + 1):
      a[col][j] /= div
    for r in range(n):
      if r == col:
        continue
      factor = a[r][col]
      if factor == 0.0:
        continue
      for j in range(col, n + 1):
        a[r][j] -= factor * a[col][j]
  return [a[i][n] for i in range(n)]


def fit_linear_model(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
  if len(rows) < 25:
    return None

  xtx = [[0.0, 0.0, 0.0] for _ in range(3)]
  xty = [0.0, 0.0, 0.0]
  y_values: list[float] = []
  for row in rows:
    cmd = float(row["cmd"])
    x = [1.0, cmd, 1.0 if cmd >= 0.0 else -1.0]
    y = float(row["actual"])
    y_values.append(y)
    for i in range(3):
      xty[i] += x[i] * y
      for j in range(3):
        xtx[i][j] += x[i] * x[j]
  for i in range(3):
    xtx[i][i] += 1e-6
  coeffs = solve_linear_system(xtx, xty)
  if coeffs is None:
    return None

  bias, gain, friction = coeffs
  residuals = []
  for row in rows:
    pred = bias + gain * float(row["cmd"]) + friction * (1.0 if float(row["cmd"]) >= 0.0 else -1.0)
    residuals.append(pred - float(row["actual"]))
  rmse = math.sqrt(mean([e * e for e in residuals])) if residuals else None
  mae = mean([abs(e) for e in residuals]) if residuals else None
  y_mean = mean(y_values)
  sst = sum((y - y_mean) ** 2 for y in y_values)
  sse = sum(e * e for e in residuals)
  r2 = 1.0 - (sse / sst) if sst > 1e-9 else None
  return {
    "bias": bias,
    "gain": gain,
    "friction": friction,
    "rmse": rmse,
    "mae": mae,
    "r2": r2,
  }


def clean_tuning_rows(samples: list[dict[str, Any]], command_key: str, command_sign: float, delay_s: float) -> list[dict[str, Any]]:
  rows: list[dict[str, Any]] = []
  if not samples:
    return rows

  command_idx = 0
  for sample in samples:
    t = finite_float(sample.get("t"))
    if t is None:
      continue
    target_t = t - delay_s
    while command_idx + 1 < len(samples) and finite_float(samples[command_idx + 1].get("t"), -1e9) <= target_t:
      command_idx += 1
    command_source = samples[command_idx]
    cmd_raw = finite_float(command_source.get(command_key))
    actual = finite_float(sample.get("actual"))
    desired = finite_float(sample.get("desired"))
    error = finite_float(sample.get("error"))
    speed = finite_float(sample.get("v_ego_mps"), 0.0)
    if (
      not sample.get("active")
      or sample.get("steering_pressed")
      or sample.get("saturated")
      or cmd_raw is None
      or actual is None
      or desired is None
      or error is None
      or speed is None
      or speed < 3.0
    ):
      continue
    cmd = command_sign * cmd_raw
    if abs(cmd) < 0.01:
      continue
    rows.append({
      "t": t,
      "cmd": cmd,
      "actual": actual,
      "desired": desired,
      "error": error,
      "desired_jerk": finite_float(sample.get("desired_jerk"), 0.0) or 0.0,
      "v_ego_mps": speed,
      "lat_accel_factor": finite_float(sample.get("lat_accel_factor")),
    })
  return rows


def classify_tune_phase(row: dict[str, Any]) -> str:
  desired = float(row.get("desired") or 0.0)
  jerk = float(row.get("desired_jerk") or 0.0)
  if abs(desired) < 0.08:
    return "center"
  if abs(jerk) < 0.08:
    return "steady"
  if desired * jerk > 0.0:
    return "turn_in_left" if desired >= 0.0 else "turn_in_right"
  if desired * jerk < 0.0:
    return "unwind_left" if desired >= 0.0 else "unwind_right"
  return "steady"


def model_predict(row: dict[str, Any], model: dict[str, Any], torque_scale: float = 1.0) -> float:
  cmd = float(row["cmd"]) * torque_scale
  return (
    float(model["bias"])
    + float(model["gain"]) * cmd
    + float(model["friction"]) * (1.0 if cmd >= 0.0 else -1.0)
  )


def model_rmse(rows: list[dict[str, Any]], model: dict[str, Any], torque_scales: dict[str, float] | None = None) -> float | None:
  if not rows:
    return None
  errors = []
  for row in rows:
    scale = 1.0
    if torque_scales is not None:
      phase = classify_tune_phase(row)
      scale = torque_scales.get(phase, torque_scales.get("global", 1.0))
    pred = model_predict(row, model, scale)
    errors.append(pred - float(row["desired"]))
  return math.sqrt(mean([e * e for e in errors])) if errors else None


def best_torque_scale(rows: list[dict[str, Any]], model: dict[str, Any]) -> float | None:
  if len(rows) < 10:
    return None
  gain = float(model["gain"])
  if abs(gain) < 1e-6:
    return None
  numerator = 0.0
  denominator = 0.0
  for row in rows:
    cmd = float(row["cmd"])
    sign = 1.0 if cmd >= 0.0 else -1.0
    x = gain * cmd
    target = float(row["desired"]) - float(model["bias"]) - float(model["friction"]) * sign
    numerator += x * target
    denominator += x * x
  if denominator < 1e-9:
    return None
  return clamp(numerator / denominator, 0.60, 1.45)


def score_from_model_rmse(rmse: float | None) -> float | None:
  if rmse is None:
    return None
  tolerance = 0.15
  return max(0.0, min(100.0, 100.0 / (1.0 + (rmse / tolerance) ** 2)))


def add_recommendation(recs: list[dict[str, Any]], patch_lines: list[str], parameter: str, current: float, recommended: float, reason: str) -> None:
  recommended = round(recommended, 4)
  recs.append({
    "parameter": parameter,
    "current": current,
    "recommended": recommended,
    "reason": reason,
  })
  patch_lines.append(f"{parameter} = {recommended:.4f}".rstrip("0").rstrip("."))


def fit_tune(samples: list[dict[str, Any]]) -> dict[str, Any]:
  warnings: list[str] = []
  best: dict[str, Any] | None = None
  for command_key in ("commanded_torque", "torque_output_can", "output"):
    for command_sign in (1.0, -1.0):
      for delay_s in (0.00, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30):
        rows = clean_tuning_rows(samples, command_key, command_sign, delay_s)
        model = fit_linear_model(rows)
        if model is None or model["rmse"] is None:
          continue
        gain = float(model["gain"])
        if gain <= 0.0:
          continue
        candidate = {
          "command_signal": command_key,
          "command_sign": command_sign,
          "delay_s": delay_s,
          "rows": rows,
          "model": model,
        }
        if best is None or float(model["rmse"]) < float(best["model"]["rmse"]):
          best = candidate

  if best is None:
    return {
      "status": "insufficient_data",
      "reason": "Need at least 25 clean active samples with command, desired, and actual lateral acceleration.",
    }

  rows = best["rows"]
  model = best["model"]
  current_lat_factor = median_float([
    float(r["lat_accel_factor"]) for r in rows
    if finite_float(r.get("lat_accel_factor")) is not None and float(r["lat_accel_factor"]) > 0.0
  ])
  if current_lat_factor is None:
    current_lat_factor = float(model["gain"])
    warnings.append("No live latAccelFactor found in the log; recommendations use fitted gain as the baseline.")

  global_scale = best_torque_scale(rows, model) or 1.0
  recommended_lat_factor = current_lat_factor / global_scale if global_scale > 1e-6 else current_lat_factor

  phase_scales: dict[str, float] = {"global": global_scale}
  phase_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
  for row in rows:
    phase_rows[classify_tune_phase(row)].append(row)
  for phase, phase_row_list in phase_rows.items():
    scale = best_torque_scale(phase_row_list, model)
    if scale is not None and len(phase_row_list) >= 20:
      phase_scales[phase] = scale

  logged_rmse = model_rmse(rows, model)
  global_rmse = model_rmse(rows, model, {"global": global_scale})
  phase_rmse = model_rmse(rows, model, phase_scales)
  recs: list[dict[str, Any]] = []
  patch_lines: list[str] = []

  base_ratio = recommended_lat_factor / current_lat_factor if current_lat_factor > 1e-6 else 1.0
  if abs(base_ratio - 1.0) >= 0.025:
    add_recommendation(
      recs,
      patch_lines,
      "IONIQ_5_BASE_LAT_ACCEL_FACTOR_MULT",
      IONIQ_5_TUNE_BASE["IONIQ_5_BASE_LAT_ACCEL_FACTOR_MULT"],
      clamp(IONIQ_5_TUNE_BASE["IONIQ_5_BASE_LAT_ACCEL_FACTOR_MULT"] * base_ratio, 0.85, 1.75),
      f"global model wants {global_scale:.3f}x logged torque, so latAccelFactor moves {base_ratio:.3f}x the opposite way",
    )

  center_scale = phase_scales.get("center")
  if center_scale is not None:
    rel = center_scale / global_scale
    if abs(rel - 1.0) >= 0.05:
      add_recommendation(
        recs,
        patch_lines,
        "IONIQ_5_CENTER_TAPER_MAX",
        IONIQ_5_TUNE_BASE["IONIQ_5_CENTER_TAPER_MAX"],
        clamp(IONIQ_5_TUNE_BASE["IONIQ_5_CENTER_TAPER_MAX"] + (1.0 - rel) * 0.25, 0.0, 0.28),
        "near-center samples want less torque than global" if rel < 1.0 else "near-center samples want more torque than global",
      )

  for phase, parameter in (
    ("turn_in_left", "IONIQ_5_TURN_IN_BOOST_LEFT"),
    ("turn_in_right", "IONIQ_5_TURN_IN_BOOST_RIGHT"),
    ("unwind_left", "IONIQ_5_UNWIND_TAPER_LEFT"),
    ("unwind_right", "IONIQ_5_UNWIND_TAPER_RIGHT"),
  ):
    scale = phase_scales.get(phase)
    if scale is None:
      continue
    rel = scale / global_scale
    if abs(rel - 1.0) < 0.06:
      continue
    current = IONIQ_5_TUNE_BASE[parameter]
    if phase.startswith("turn_in"):
      recommended = clamp(current + (rel - 1.0) * 0.50, 0.0, 0.28)
      reason = "turn-in wants more torque than global" if rel > 1.0 else "turn-in wants less torque than global"
    else:
      recommended = clamp(current + (1.0 - rel) * 0.45, 0.35, 1.10)
      reason = "unwind wants less torque than global" if rel < 1.0 else "unwind wants more torque than global"
    add_recommendation(recs, patch_lines, parameter, current, recommended, reason)

  if len(rows) < 150:
    warnings.append("Small active sample set; treat phase-specific constants as directional until more logs are added.")
  if model.get("r2") is not None and float(model["r2"]) < 0.45:
    warnings.append("Command-to-actual fit is weak; tire/load/wind/road effects may dominate this route.")
  if best["command_signal"] != "commanded_torque":
    warnings.append(f"Fell back to {best['command_signal']} because it fit the response better than commanded_torque.")

  phase_scale_rows = []
  for phase, scale in sorted(phase_scales.items()):
    if phase == "global":
      continue
    phase_scale_rows.append({
      "phase": phase,
      "samples": len(phase_rows.get(phase, [])),
      "torque_scale": scale,
      "relative_to_global": scale / global_scale if global_scale else None,
    })

  return {
    "status": "ok",
    "sample_count": len(rows),
    "command_signal": best["command_signal"],
    "command_sign": best["command_sign"],
    "delay_s": best["delay_s"],
    "model": {
      **model,
      "current_lat_accel_factor": current_lat_factor,
      "recommended_lat_accel_factor": recommended_lat_factor,
      "recommended_torque_scale": global_scale,
    },
    "backtest": {
      "logged_rmse": logged_rmse,
      "global_candidate_rmse": global_rmse,
      "phase_candidate_rmse": phase_rmse,
      "logged_score": score_from_model_rmse(logged_rmse),
      "global_candidate_score": score_from_model_rmse(global_rmse),
      "phase_candidate_score": score_from_model_rmse(phase_rmse),
    },
    "phase_scales": phase_scale_rows,
    "recommendations": recs,
    "patch_preview": patch_lines,
    "warnings": warnings,
  }


def decompress_log(path: Path) -> bytes:
  data = path.read_bytes()
  if path.suffix == ".bz2" or data.startswith(b"BZh9"):
    return bz2.decompress(data)
  if path.suffix == ".zst" or data.startswith(b"\x28\xB5\x2F\xFD"):
    with zstd.ZstdDecompressor().stream_reader(data) as reader:
      return reader.read()
  return data


def decompress_log_prefix(path: Path, max_bytes: int = START_TIME_PREFIX_BYTES) -> bytes:
  with path.open("rb") as f:
    header = f.read(4)
    f.seek(0)
    if path.suffix == ".zst" or header == b"\x28\xB5\x2F\xFD":
      with zstd.ZstdDecompressor().stream_reader(f) as reader:
        return reader.read(max_bytes)
    data = f.read()
  if path.suffix == ".bz2" or header == b"BZh9":
    return bz2.decompress(data)[:max_bytes]
  return data[:max_bytes]


def read_events(path: Path):
  data = decompress_log(path)
  try:
    yield from log.Event.read_multiple_bytes(data)
  except capnp.KjException:
    return


def read_events_prefix(path: Path):
  data = decompress_log_prefix(path)
  try:
    yield from log.Event.read_multiple_bytes(data)
  except capnp.KjException:
    return


def record_controls_state(evt: Any) -> dict[str, Any] | None:
  cs = safe_get(evt, "controlsState")
  lateral = safe_get(cs, "lateralControlState")
  try:
    if lateral.which() != "torqueState":
      return None
  except Exception:
    return None
  ts = safe_get(lateral, "torqueState")
  desired = finite_float(safe_get(ts, "desiredLateralAccel"), 0.0)
  actual = finite_float(safe_get(ts, "actualLateralAccel"), 0.0)
  return {
    "mono": int(safe_get(evt, "logMonoTime", 0)),
    "active": bool(safe_get(ts, "active", False)),
    "desired": desired,
    "actual": actual,
    "error": (actual - desired) if desired is not None and actual is not None else None,
    "controller_error": finite_float(safe_get(ts, "error")),
    "desired_jerk": finite_float(safe_get(ts, "desiredLateralJerk")),
    "p": finite_float(safe_get(ts, "p")),
    "i": finite_float(safe_get(ts, "i")),
    "d": finite_float(safe_get(ts, "d")),
    "f": finite_float(safe_get(ts, "f")),
    "output": finite_float(safe_get(ts, "output")),
    "saturated": bool(safe_get(ts, "saturated", False)),
  }


def record_car_state(evt: Any) -> dict[str, Any]:
  cs = safe_get(evt, "carState")
  return {
    "mono": int(safe_get(evt, "logMonoTime", 0)),
    "v_ego_mps": finite_float(safe_get(cs, "vEgo")),
    "steering_angle": finite_float(safe_get(cs, "steeringAngleDeg")),
    "steering_rate": finite_float(safe_get(cs, "steeringRateDeg")),
    "steering_torque": finite_float(safe_get(cs, "steeringTorque")),
    "steering_torque_eps": finite_float(safe_get(cs, "steeringTorqueEps")),
    "steering_pressed": bool(safe_get(cs, "steeringPressed", False)),
  }


def record_car_control(evt: Any) -> dict[str, Any]:
  cc = safe_get(evt, "carControl")
  actuators = safe_get(cc, "actuators")
  return {
    "mono": int(safe_get(evt, "logMonoTime", 0)),
    "lat_active": bool(safe_get(cc, "latActive", False)),
    "commanded_torque": finite_float(safe_get(actuators, "torque")),
    "torque_output_can": finite_float(safe_get(actuators, "torqueOutputCan")),
    "commanded_curvature": finite_float(safe_get(actuators, "curvature")),
  }


def record_live_torque(evt: Any) -> dict[str, Any]:
  ltp = safe_get(evt, "liveTorqueParameters")
  return {
    "mono": int(safe_get(evt, "logMonoTime", 0)),
    "lat_accel_factor": finite_float(safe_get(ltp, "latAccelFactorFiltered")),
    "lat_accel_offset": finite_float(safe_get(ltp, "latAccelOffsetFiltered")),
    "friction": finite_float(safe_get(ltp, "frictionCoefficientFiltered")),
    "live_valid": bool(safe_get(ltp, "liveValid", False)),
    "use_params": bool(safe_get(ltp, "useParams", False)),
    "cal_perc": finite_float(safe_get(ltp, "calPerc")),
  }


def record_live_params(evt: Any) -> dict[str, Any]:
  lp = safe_get(evt, "liveParameters")
  return {
    "mono": int(safe_get(evt, "logMonoTime", 0)),
    "steer_ratio": finite_float(safe_get(lp, "steerRatio")),
    "stiffness_factor": finite_float(safe_get(lp, "stiffnessFactor")),
    "angle_offset_deg": finite_float(safe_get(lp, "angleOffsetDeg")),
    "roll": finite_float(safe_get(lp, "roll")),
  }


def latest_merger(records: list[dict[str, Any]]):
  records = sorted(records, key=lambda r: r["mono"])
  idx = 0
  latest: dict[str, Any] = {}

  def merge_until(mono: int) -> dict[str, Any]:
    nonlocal idx, latest
    while idx < len(records) and records[idx]["mono"] <= mono:
      latest = records[idx]
      idx += 1
    return latest

  return merge_until


def analyze_logs(log_paths: list[Path], log_type: str | None = None, label: str | None = None) -> dict[str, Any]:
  controls: list[dict[str, Any]] = []
  car_states: list[dict[str, Any]] = []
  car_controls: list[dict[str, Any]] = []
  live_torque: list[dict[str, Any]] = []
  live_params: list[dict[str, Any]] = []
  service_counts: dict[str, int] = {}

  for log_path in log_paths:
    for evt in read_events(log_path):
      try:
        which = evt.which()
      except Exception:
        continue
      service_counts[which] = service_counts.get(which, 0) + 1
      if which == "controlsState":
        row = record_controls_state(evt)
        if row is not None:
          controls.append(row)
      elif which == "carState":
        car_states.append(record_car_state(evt))
      elif which == "carControl":
        car_controls.append(record_car_control(evt))
      elif which == "liveTorqueParameters":
        live_torque.append(record_live_torque(evt))
      elif which == "liveParameters":
        live_params.append(record_live_params(evt))

  controls.sort(key=lambda r: r["mono"])
  if not controls:
    raise ValueError(f"No torque controlsState samples found in {label or log_paths[0]}")

  car_at = latest_merger(car_states)
  command_at = latest_merger(car_controls)
  torque_at = latest_merger(live_torque)
  params_at = latest_merger(live_params)
  start_mono = controls[0]["mono"]

  samples: list[dict[str, Any]] = []
  for row in controls:
    mono = row["mono"]
    enriched = {
      **row,
      **{k: v for k, v in car_at(mono).items() if k != "mono"},
      **{k: v for k, v in command_at(mono).items() if k != "mono"},
      **{k: v for k, v in torque_at(mono).items() if k != "mono"},
      **{k: v for k, v in params_at(mono).items() if k != "mono"},
      "t": (mono - start_mono) / 1e9,
    }
    enriched["active"] = bool(enriched.get("active") or enriched.get("lat_active"))
    samples.append(enriched)

  active = [s for s in samples if s.get("active") and finite_float(s.get("error")) is not None]
  overall = stats_for(active, "active")
  overall["tracking_score"] = score_from_rmse(overall["rmse"])

  straight = [s for s in active if abs(float(s.get("desired") or 0.0)) < 0.08]
  unwind = [
    s for s in active
    if finite_float(s.get("desired_jerk")) is not None
    and abs(float(s.get("desired") or 0.0)) > 0.08
    and float(s.get("desired") or 0.0) * float(s.get("desired_jerk") or 0.0) < 0.0
  ]
  turn_in = [
    s for s in active
    if finite_float(s.get("desired_jerk")) is not None
    and abs(float(s.get("desired") or 0.0)) > 0.08
    and float(s.get("desired") or 0.0) * float(s.get("desired_jerk") or 0.0) > 0.0
  ]
  steady_turn = [
    s for s in active
    if abs(float(s.get("desired") or 0.0)) >= 0.08
    and abs(float(s.get("desired_jerk") or 0.0)) < 0.08
  ]

  straight_errors = [float(s["error"]) for s in straight if finite_float(s.get("error")) is not None]
  zero_crossings = 0
  prev_sign = 0
  for err in straight_errors:
    sign = 1 if err > 0 else -1 if err < 0 else 0
    if sign and prev_sign and sign != prev_sign:
      zero_crossings += 1
    if sign:
      prev_sign = sign

  active_duration_min = 0.0
  if active:
    active_duration_min = max((active[-1]["t"] - active[0]["t"]) / 60.0, 1e-6)

  steering_snap_count = 0
  last_angle = None
  for s in active:
    angle = finite_float(s.get("steering_angle"))
    if angle is not None and last_angle is not None and abs(angle - last_angle) >= 5.0:
      steering_snap_count += 1
    if angle is not None:
      last_angle = angle

  summary = {
    **overall,
    "duration_s": samples[-1]["t"] - samples[0]["t"] if len(samples) > 1 else 0.0,
    "active_samples": len(active),
    "sample_count": len(samples),
    "chart_samples": len(samples),
    "straight_oscillations_per_min": zero_crossings / active_duration_min if active_duration_min > 0 else 0.0,
    "steering_snap_count": steering_snap_count,
    "log_path": label or ", ".join(str(p) for p in log_paths),
  }

  phase_scores = [
    stats_for(active, "all active"),
    stats_for(straight, "straight / near center"),
    stats_for(turn_in, "turn-in"),
    stats_for(unwind, "unwind"),
    stats_for(steady_turn, "steady turn"),
  ]

  return {
    "log_path": label or ", ".join(str(p) for p in log_paths),
    "log_type": log_type or log_paths[0].name.replace(".zst", ""),
    "segment_count": len(log_paths),
    "summary": summary,
    "phase_scores": phase_scores,
    "samples": samples,
    "tune_fit": fit_tune(samples),
    "service_counts": service_counts,
    "last_live_torque": live_torque[-1] if live_torque else {},
    "last_live_params": live_params[-1] if live_params else {},
  }


def analyze_log(log_path: Path) -> dict[str, Any]:
  return analyze_logs([log_path], log_type=log_path.name.replace(".zst", ""), label=str(log_path))


def split_segment_name(name: str) -> tuple[str, int]:
  match = SEGMENT_RE.match(name)
  if not match:
    return name, 0
  return match.group("trip"), int(match.group("segment"))


def count_active_samples(log_path: Path, prefer_qlog_probe: bool = False) -> int:
  if prefer_qlog_probe and log_path.name == "rlog.zst":
    qlog_path = log_path.with_name("qlog.zst")
    if qlog_path.exists():
      log_path = qlog_path

  if not log_path.exists():
    return 0
  stat = log_path.stat()
  cache_key = (str(log_path), stat.st_size, stat.st_mtime_ns)
  cached = ACTIVE_SAMPLE_CACHE.get(cache_key)
  if cached is not None:
    return cached

  active = 0
  for evt in read_events(log_path):
    try:
      if evt.which() != "controlsState":
        continue
      row = record_controls_state(evt)
      if row is not None and row.get("active"):
        active += 1
    except Exception:
      continue

  ACTIVE_SAMPLE_CACHE[cache_key] = active
  return active


def event_unix_time_s(evt: Any) -> float | None:
  service = None
  try:
    service = evt.which()
  except Exception:
    return None

  if service == "gpsLocation":
    ms = finite_float(safe_get(safe_get(evt, "gpsLocation"), "unixTimestampMillis"))
    if ms is not None and ms > 1_000_000_000_000:
      return ms / 1000.0
  if service == "liveLocationKalman":
    ms = finite_float(safe_get(safe_get(evt, "liveLocationKalman"), "unixTimestampMillis"))
    if ms is not None and ms > 1_000_000_000_000:
      return ms / 1000.0

  nanos = finite_float(safe_get(evt, "unixTimestampNanos"))
  if nanos is not None and nanos > 1_000_000_000_000_000_000:
    return nanos / 1e9
  return None


def log_segment_start_time(log_path: Path) -> tuple[float | None, str]:
  if not log_path.exists():
    return None, "missing"
  stat = log_path.stat()
  cache_key = (str(log_path), stat.st_size, stat.st_mtime_ns)
  cached = LOG_START_TIME_CACHE.get(cache_key)
  if cached is not None:
    return cached

  first_mono: int | None = None
  started = perf_counter()
  for event_count, evt in enumerate(read_events_prefix(log_path), start=1):
    if event_count > START_TIME_MAX_EVENTS or (event_count % 256 == 0 and perf_counter() - started > START_TIME_FILE_BUDGET_S):
      result = (None, "missing_early_wall_time")
      LOG_START_TIME_CACHE[cache_key] = result
      return result
    mono = int(safe_get(evt, "logMonoTime", 0))
    if mono > 0 and first_mono is None:
      first_mono = mono

    unix_s = event_unix_time_s(evt)
    if unix_s is not None and first_mono is not None:
      start_s = unix_s - ((mono - first_mono) / 1e9)
      result = (start_s, safe_get(evt, "which", lambda: "wall")())
      LOG_START_TIME_CACHE[cache_key] = result
      return result

  result = (None, "missing_wall_time")
  LOG_START_TIME_CACHE[cache_key] = result
  return result


def route_start_time(ordered_dirs: list[Path]) -> tuple[float | None, str]:
  log_names = ("qlog.zst",) if any((segment_dir / "qlog.zst").exists() for segment_dir in ordered_dirs) else ("rlog.zst",)
  for log_name in log_names:
    for segment_dir in ordered_dirs[:START_TIME_MAX_SEGMENTS]:
      trip_id, segment = split_segment_name(segment_dir.name)
      del trip_id
      log_path = segment_dir / log_name
      if not log_path.exists():
        continue
      segment_start_s, source = log_segment_start_time(log_path)
      if segment_start_s is not None:
        return segment_start_s - (segment * 60.0), f"{source}, inferred from segment {segment}"
  return None, "missing_wall_time"


def format_route_time(timestamp_s: float | None) -> str:
  if timestamp_s is None:
    return ""
  return datetime.fromtimestamp(timestamp_s).strftime("%Y-%m-%d %H:%M")


def trip_log_paths(root: Path, trip_id: str, log_type: str) -> list[Path]:
  paths: list[tuple[int, Path]] = []
  for child in root.iterdir():
    if not child.is_dir():
      continue
    child_trip, segment = split_segment_name(child.name)
    if child_trip != trip_id:
      continue
    log_path = child / f"{log_type}.zst"
    if log_path.exists():
      paths.append((segment, log_path))
  return [p for _, p in sorted(paths, key=lambda item: item[0])]


def list_routes(root: Path, active_log_type: str | None = None, active_only: bool = False) -> list[dict[str, Any]]:
  if not root.exists():
    raise FileNotFoundError(f"Log root does not exist: {root}")

  groups: dict[str, list[tuple[int, Path]]] = defaultdict(list)
  for child in root.iterdir():
    if not child.is_dir():
      continue
    qlog = child / "qlog.zst"
    rlog = child / "rlog.zst"
    if not qlog.exists() and not rlog.exists():
      continue
    match = SEGMENT_RE.match(child.name)
    if not match:
      continue
    trip_id, segment = match.group("trip"), int(match.group("segment"))
    groups[trip_id].append((segment, child))

  routes: list[dict[str, Any]] = []
  for trip_id, segment_dirs in groups.items():
    ordered_dirs = [p for _, p in sorted(segment_dirs, key=lambda item: item[0])]
    qlogs = [p / "qlog.zst" for p in ordered_dirs if (p / "qlog.zst").exists()]
    rlogs = [p / "rlog.zst" for p in ordered_dirs if (p / "rlog.zst").exists()]
    active_samples = None
    if active_log_type is not None:
      active_paths = rlogs if active_log_type == "rlog" else qlogs
      active_samples = sum(count_active_samples(p, prefer_qlog_probe=active_log_type == "rlog") for p in active_paths)
      if active_only and active_samples <= 0:
        continue

    all_logs = qlogs + rlogs
    modified_ts = max((p.stat().st_mtime for p in all_logs), default=max(p.stat().st_mtime for p in ordered_dirs))
    start_ts, start_source = route_start_time(ordered_dirs)
    sort_ts = start_ts if start_ts is not None else 0.0
    routes.append({
      "name": trip_id,
      "path": trip_id,
      "path_quoted": quote(trip_id),
      "segment_count": len(ordered_dirs),
      "segments": [p.name for p in ordered_dirs],
      "qlog": len(qlogs) > 0,
      "rlog": len(rlogs) > 0,
      "qlog_bytes": sum(p.stat().st_size for p in qlogs),
      "rlog_bytes": sum(p.stat().st_size for p in rlogs),
      "active_samples": active_samples,
      "start_unix_ms": round(start_ts * 1000.0) if start_ts is not None else None,
      "started": format_route_time(start_ts),
      "time_source": start_source,
      "modified": format_route_time(modified_ts),
      "sort_ts": sort_ts,
    })
  return sorted(routes, key=lambda route: route["sort_ts"], reverse=True)


class TuningHandler(BaseHTTPRequestHandler):
  server_version = "StarPilotTuningViewer/0.1"

  def send_json(self, payload: Any, status: int = 200) -> None:
    data = json.dumps(payload, allow_nan=False).encode("utf-8")
    self.send_response(status)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)

  def send_html(self, html: str) -> None:
    data = html.encode("utf-8")
    self.send_response(200)
    self.send_header("Content-Type", "text/html; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)

  def do_GET(self) -> None:
    parsed = urlparse(self.path)
    qs = parse_qs(parsed.query)
    try:
      if parsed.path == "/":
        self.send_html(INDEX_HTML)
      elif parsed.path == "/api/config":
        self.send_json({"root": str(self.server.log_root)})
      elif parsed.path == "/api/routes":
        root = Path(unquote(qs.get("root", [str(self.server.log_root)])[0])).expanduser()
        self.server.log_root = root
        log_type = qs.get("log", ["rlog"])[0]
        if log_type not in LOG_TYPES:
          raise ValueError(f"Unsupported log type: {log_type}")
        active_only = qs.get("active_only", ["0"])[0] in ("1", "true", "yes")
        active_log_type = log_type if active_only else None
        self.send_json({"root": str(root), "routes": list_routes(root, active_log_type=active_log_type, active_only=active_only)})
      elif parsed.path == "/api/analyze":
        raw_path = qs.get("path", [""])[0]
        if not raw_path:
          raise ValueError("Missing path")
        raw_path = unquote(raw_path)
        log_type = qs.get("log", ["rlog"])[0]
        if log_type not in LOG_TYPES:
          raise ValueError(f"Unsupported log type: {log_type}")
        segment_path = Path(raw_path)
        if segment_path.exists():
          log_paths = [segment_path / f"{log_type}.zst"]
          label = str(segment_path)
        else:
          log_paths = trip_log_paths(self.server.log_root, raw_path, log_type)
          label = raw_path
        log_paths = [p for p in log_paths if p.exists()]
        if not log_paths:
          raise FileNotFoundError(f"Missing {log_type}.zst files for {raw_path}")
        self.send_json(analyze_logs(log_paths, log_type=log_type, label=label))
      else:
        self.send_json({"error": "not found"}, 404)
    except Exception as e:
      self.send_json({"error": str(e)}, 500)

  def log_message(self, fmt: str, *args: Any) -> None:
    print(f"{self.address_string()} - {fmt % args}")


class TuningServer(ThreadingHTTPServer):
  log_root: Path


def main() -> None:
  parser = argparse.ArgumentParser(description="Visualize StarPilot lateral torque tuning logs.")
  parser.add_argument("--root", type=Path, default=Path(os.environ.get("TUNING_LOG_ROOT", DEFAULT_LOG_ROOT)))
  parser.add_argument("--host", default="127.0.0.1")
  parser.add_argument("--port", type=int, default=8765)
  args = parser.parse_args()

  server = TuningServer((args.host, args.port), TuningHandler)
  server.log_root = args.root
  print(f"Serving tuning viewer at http://{args.host}:{args.port}")
  print(f"Log root: {args.root}")
  server.serve_forever()


if __name__ == "__main__":
  main()
