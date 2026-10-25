"""Module C: design-network builder (agent ``network``).

Reads ``db/research.sqlite3`` only (tables: designs, design_metrics,
metric_defs, cohorts, cells, runs) and writes:

- ``db/network.json`` -- structured graph + per-cohort metrics.
- ``db/network.html`` -- a self-contained, offline, Korean-language viewer
  (vanilla JS + SVG, no CDN).  The JSON payload is embedded with ``</`` escaped
  so a stray ``</script>`` in the data cannot break out of the script tag.

Similarity maths (per the 2026-10-24 contract):

- **Design similarity** -- mean, over the design keys present in *either*
  design, of the Jaccard overlap of that key's tokens.  Keys are
  ``material, process, cell_format, supplier, geometry, anode``.  No key present
  in either design -> no edge.
- **Electrochemical similarity** (within a single cohort) -- per
  ``metric+window`` a robust z (median / MAD across the cohort's designs;
  ``MAD == 0`` -> skip that metric).  RMS of the z-differences over the shared
  metrics (need >= 2) gives ``sim = 1 / (1 + rms)``; fewer than 2 shared
  metrics -> no edge.

Nothing here mutates research.sqlite3 or any file owned by another agent.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

CODE_VERSION = "design_network_v1"

# Design-relevant tag keys (equal weight) used for structural similarity.
DESIGN_KEYS = ("material", "process", "cell_format", "supplier", "geometry", "anode")

_TOKEN_SPLIT = re.compile(r"[\s,;/|]+")


# --------------------------------------------------------------------------- #
# Tokenisation + design similarity
# --------------------------------------------------------------------------- #
def _tokenise(value) -> frozenset:
    """Return a lower-cased token set for a tag value (str or list of str)."""
    if value is None:
        return frozenset()
    if isinstance(value, (list, tuple, set, frozenset)):
        parts = []
        for item in value:
            parts.extend(_TOKEN_SPLIT.split(str(item).strip().lower()))
    else:
        parts = _TOKEN_SPLIT.split(str(value).strip().lower())
    return frozenset(p for p in parts if p)


def _jaccard(a: frozenset, b: frozenset) -> float:
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def design_similarity(tags_a: dict, tags_b: dict) -> float | None:
    """Mean Jaccard over the design keys present in either design.

    Returns ``None`` when neither design carries any design-relevant key (so the
    caller can omit the edge entirely).
    """
    tags_a = tags_a or {}
    tags_b = tags_b or {}
    scores = []
    for key in DESIGN_KEYS:
        ta = _tokenise(tags_a.get(key))
        tb = _tokenise(tags_b.get(key))
        if not ta and not tb:
            continue  # key absent from both designs
        scores.append(_jaccard(ta, tb))
    if not scores:
        return None
    return sum(scores) / len(scores)


# --------------------------------------------------------------------------- #
# Electrochemical similarity (robust-z RMS within a cohort)
# --------------------------------------------------------------------------- #
def _median(values):
    s = sorted(values)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def electrochem_z(metrics_by_design: dict) -> dict:
    """Robust z-scores per design per metric_key within one cohort.

    ``metrics_by_design`` maps ``design_id -> {metric_key: value}``.  Metrics
    with zero MAD across designs are skipped.  Returns
    ``design_id -> {metric_key: z}``.
    """
    # collect values per metric_key
    by_metric: dict[str, list] = {}
    for mvals in metrics_by_design.values():
        for mkey, val in mvals.items():
            if val is None:
                continue
            by_metric.setdefault(mkey, []).append(float(val))

    stats = {}
    for mkey, vals in by_metric.items():
        if len(vals) < 2:
            continue
        med = _median(vals)
        mad = _median([abs(v - med) for v in vals])
        if mad is None or mad == 0:
            continue  # no spread -> uninformative, skip metric
        stats[mkey] = (med, 1.4826 * mad)

    z_by_design: dict[str, dict] = {}
    for design_id, mvals in metrics_by_design.items():
        zrow = {}
        for mkey, (med, scale) in stats.items():
            val = mvals.get(mkey)
            if val is None:
                continue
            zrow[mkey] = (float(val) - med) / scale
        z_by_design[design_id] = zrow
    return z_by_design


def electrochem_edges(metrics_by_design: dict) -> list[dict]:
    """Return undirected electrochem similarity edges for one cohort.

    An edge requires >= 2 shared (non-skipped) metrics.  ``sim = 1/(1+rms)``.
    """
    z_by_design = electrochem_z(metrics_by_design)
    designs = sorted(z_by_design)
    edges = []
    for i in range(len(designs)):
        for j in range(i + 1, len(designs)):
            za = z_by_design[designs[i]]
            zb = z_by_design[designs[j]]
            shared = [m for m in za if m in zb]
            if len(shared) < 2:
                continue
            rms = math.sqrt(sum((za[m] - zb[m]) ** 2 for m in shared) / len(shared))
            edges.append({
                "source": designs[i],
                "target": designs[j],
                "weight": 1.0 / (1.0 + rms),
                "shared": len(shared),
            })
    return edges


# --------------------------------------------------------------------------- #
# Defensive sqlite reading
# --------------------------------------------------------------------------- #
def _table_columns(conn, table: str) -> set:
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
    except sqlite3.Error:
        return set()
    return {row[1] for row in cur.fetchall()}


def _read_table(conn, table: str) -> list[dict]:
    cols = _table_columns(conn, table)
    if not cols:
        return []
    cur = conn.execute(f"SELECT * FROM {table}")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _loads(value):
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


def _num(value):
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _metric_key(metric_id, window) -> str:
    w = "" if window is None or str(window) == "" else str(window)
    return f"{metric_id}|{w}" if w else str(metric_id)


# --------------------------------------------------------------------------- #
# Core build
# --------------------------------------------------------------------------- #
def _design_slides(conn, per_design=6) -> dict:
    """design_id -> best slide references (from slide_link's v_test_slides view, if present)."""
    try:
        rows = conn.execute(
            "SELECT design_id, file_name, slide_no, set_label, confidence, max(score) AS s FROM v_test_slides "
            "WHERE design_id IS NOT NULL AND confidence IN ('verified','matched') "
            "GROUP BY design_id, file_name, slide_no ORDER BY design_id, (confidence='verified') DESC, s DESC").fetchall()
    except sqlite3.Error:
        return {}
    out = {}
    for did, fname, sno, label, conf, _ in rows:
        lst = out.setdefault(did, [])
        if len(lst) < per_design:
            lst.append({"file": fname, "slide": sno, "set": label or "", "confidence": conf})
    return out


def _collect(conn, cohort_filter=None, design_threshold=0.3):
    designs = _read_table(conn, "designs")
    metric_defs = _read_table(conn, "metric_defs")
    design_metrics = _read_table(conn, "design_metrics")
    cohorts = _read_table(conn, "cohorts")
    cells = _read_table(conn, "cells")
    runs = _read_table(conn, "runs")

    deviations = []
    for tbl, rows in (("designs", designs), ("design_metrics", design_metrics)):
        if not rows:
            deviations.append(f"table '{tbl}' missing or empty")

    # ---- designs ----
    design_nodes = {}
    for d in designs:
        did = d.get("design_id")
        if did is None:
            continue
        tags = _loads(d.get("tags_json"))
        fields = _loads(d.get("design_fields_json"))
        design_nodes[did] = {
            "design_id": did,
            "label": d.get("design_label") or did,
            "status": d.get("status") or "unknown",
            "n_cells": int(d.get("n_cells") or 0),
            "n_runs": int(d.get("n_runs") or 0),
            "tags": tags if isinstance(tags, dict) else {},
            "fields": fields if isinstance(fields, dict) else {},
            "conflicts": d.get("conflicts"),
            "source": d.get("source"),
        }

    # ---- metric defs ----
    metric_def_list = []
    hib = {}
    for m in metric_defs:
        mid = m.get("metric_id")
        if mid is None:
            continue
        direction = m.get("higher_is_better")
        if isinstance(direction, (int, float)) and not isinstance(direction, bool):
            direction = bool(direction) if direction in (0, 1) else None
        metric_def_list.append({
            "metric_id": mid,
            "name_ko": m.get("name_ko") or mid,
            "unit": m.get("unit"),
            "higher_is_better": direction,
            "definition": m.get("definition"),
        })
        hib[mid] = direction

    # ---- design_metrics indexed by cohort/design/metric_key ----
    # metrics_index[cohort][design][metric_key] = {mean, sd, median, min, max, n_cells, metric_id, window}
    metrics_index: dict = {}
    for row in design_metrics:
        ck = row.get("cohort_key")
        did = row.get("design_id")
        if ck is None or did is None:
            continue
        mid = row.get("metric_id")
        mkey = _metric_key(mid, row.get("window"))
        metrics_index.setdefault(ck, {}).setdefault(did, {})[mkey] = {
            "metric_id": mid,
            "window": None if row.get("window") in (None, "") else str(row.get("window")),
            "n_cells": int(row.get("n_cells") or 0),
            "mean": _num(row.get("mean")),
            "sd": _num(row.get("sd")),
            "median": _num(row.get("median")),
            "min": _num(row.get("min")),
            "max": _num(row.get("max")),
        }

    # ---- cohorts ----
    cohort_list = []
    for c in cohorts:
        ck = c.get("cohort_key")
        if ck is None:
            continue
        design_ids = sorted(metrics_index.get(ck, {}).keys())
        cohort_list.append({
            "cohort_key": ck,
            "test_type": c.get("test_type"),
            "temperature_c": _num(c.get("temperature_c")),
            "c_rate_class": c.get("c_rate_class"),
            "v_window": c.get("v_window"),
            "n_designs": int(c.get("n_designs") or 0),
            "n_cells": int(c.get("n_cells") or 0),
            "design_ids": design_ids,
            "useful": len(design_ids) >= 2,
        })
    # include cohorts that only appear in design_metrics (defensive)
    known = {c["cohort_key"] for c in cohort_list}
    for ck, dmap in metrics_index.items():
        if ck in known:
            continue
        design_ids = sorted(dmap.keys())
        cohort_list.append({
            "cohort_key": ck, "test_type": None, "temperature_c": None,
            "c_rate_class": None, "v_window": None, "n_designs": len(design_ids),
            "n_cells": 0, "design_ids": design_ids, "useful": len(design_ids) >= 2,
        })
    cohort_list.sort(key=lambda c: (not c["useful"], c["cohort_key"]))

    if cohort_filter is not None:
        cohort_list = [c for c in cohort_list if c["cohort_key"] == cohort_filter]
        keep = {c["cohort_key"] for c in cohort_list}
        metrics_index = {k: v for k, v in metrics_index.items() if k in keep}

    # keep only designs that appear somewhere (node set) -- but keep all designs
    # so isolated nodes still show.
    node_list = sorted(design_nodes.values(), key=lambda d: d["design_id"])
    slides_by_design = _design_slides(conn)
    for n in node_list:
        n["slides"] = slides_by_design.get(n["design_id"], [])

    # ---- design edges (global, structural) ----
    design_edges = []
    ids = [n["design_id"] for n in node_list]
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            sim = design_similarity(design_nodes[ids[i]]["tags"], design_nodes[ids[j]]["tags"])
            if sim is None:
                continue
            if sim >= design_threshold:
                design_edges.append({
                    "source": ids[i], "target": ids[j], "weight": round(sim, 6),
                })

    # ---- electrochem edges per cohort ----
    ec_edges = {}
    metrics_payload = {}
    percentiles = {}
    for ck, dmap in metrics_index.items():
        means = {did: {mk: cell["mean"] for mk, cell in mrow.items() if cell["mean"] is not None}
                 for did, mrow in dmap.items()}
        edges = electrochem_edges(means)
        for e in edges:
            e["weight"] = round(e["weight"], 6)
        ec_edges[ck] = edges
        metrics_payload[ck] = dmap
        percentiles[ck] = _percentiles(dmap, hib)

    payload = {
        "project": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "code_version": CODE_VERSION,
        "design_threshold": design_threshold,
        "cohort_filter": cohort_filter,
        "designs": node_list,
        "metric_defs": metric_def_list,
        "cohorts": cohort_list,
        "design_edges": design_edges,
        "electrochem_edges": ec_edges,
        "design_metrics": metrics_payload,
        "percentiles": percentiles,
        "deviations": deviations,
    }
    return payload


def _percentiles(dmap: dict, hib: dict) -> dict:
    """Cohort percentile (0-100) per design per metric_key, direction-corrected.

    Returns ``design_id -> {metric_key: {pct, flipped}}`` where ``flipped`` is
    True/False/None (None = unknown direction, left un-flipped and marked).
    """
    # gather values per metric_key
    by_metric: dict[str, dict] = {}
    for did, mrow in dmap.items():
        for mk, cell in mrow.items():
            if cell["mean"] is None:
                continue
            by_metric.setdefault(mk, {})[did] = cell["mean"]

    out: dict[str, dict] = {did: {} for did in dmap}
    for mk, vals in by_metric.items():
        mid = mk.split("|", 1)[0]
        direction = hib.get(mid)
        items = list(vals.items())
        n = len(items)
        for did, v in items:
            if n <= 1:
                pct = 50.0
            else:
                below = sum(1 for _, o in items if o < v)
                pct = below / (n - 1) * 100.0
            flipped = None
            if direction is False:
                pct = 100.0 - pct
                flipped = True
            elif direction is True:
                flipped = False
            out[did][mk] = {"pct": round(pct, 2), "flipped": flipped}
    return out


def _resolve_db(project) -> Path:
    project = Path(project)
    # accept either a project dir or a direct path to the sqlite file
    if project.suffix == ".sqlite3" and project.is_file():
        return project
    return project / "db" / "research.sqlite3"


def build_network(project, *, cohort=None, design_threshold=0.3) -> Path:
    """Build ``db/network.json`` and ``db/network.html`` from research.sqlite3."""
    db_path = _resolve_db(project)
    if not db_path.is_file():
        raise FileNotFoundError(f"research.sqlite3 not found at {db_path}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        payload = _collect(conn, cohort_filter=cohort, design_threshold=design_threshold)
    finally:
        conn.close()

    out_dir = db_path.parent
    payload["project"] = str(Path(project))
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "network.json"
    json_text = json.dumps(payload, ensure_ascii=False, indent=2)
    json_path.write_text(json_text, encoding="utf-8")

    html_path = out_dir / "network.html"
    html_path.write_text(render_html(payload), encoding="utf-8")
    return html_path


# --------------------------------------------------------------------------- #
# HTML rendering (self-contained, offline, Korean UI)
# --------------------------------------------------------------------------- #
def _embed_json(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False)
    # Prevent an embedded "</script>" (or any "</") from closing the tag.
    return text.replace("</", "<\\/").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def render_html(payload: dict) -> str:
    data = _embed_json(payload)
    return _HTML_TEMPLATE.replace("__DATA__", data)


_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>설계 네트워크 뷰어</title>
<style>
  :root { --bg:#0f1115; --panel:#1b1f27; --ink:#e8ecf1; --muted:#8a93a3;
          --cand:#e0a93b; --conf:#4da3ff; --edge1:#6c8; --edge2:#c97; }
  * { box-sizing:border-box; }
  body { margin:0; font-family:"Malgun Gothic","Segoe UI",sans-serif;
         background:var(--bg); color:var(--ink); font-size:13px; }
  header { padding:8px 14px; background:var(--panel); display:flex;
           gap:14px; align-items:center; flex-wrap:wrap; }
  header h1 { font-size:15px; margin:0 10px 0 0; }
  select, input, button { background:#262b35; color:var(--ink); border:1px solid #333c49;
          border-radius:4px; padding:4px 7px; font-size:12px; }
  label { color:var(--muted); margin-right:4px; }
  #layout { display:flex; height:calc(100vh - 46px); }
  #graph { flex:1 1 60%; position:relative; border-right:1px solid #262b35; }
  #side { flex:1 1 40%; overflow:auto; padding:10px 14px; }
  svg { width:100%; height:100%; display:block; }
  .node circle { cursor:pointer; stroke:#0b0d11; stroke-width:1.2; }
  .node text { font-size:10px; fill:var(--ink); pointer-events:none; }
  .node.sel circle { stroke:#fff; stroke-width:2.5; }
  .edge { stroke-opacity:0.55; }
  .badge { display:inline-block; padding:1px 6px; border-radius:9px; font-size:10px;
           margin-left:4px; }
  .b-cand { background:var(--cand); color:#231a00; }
  .b-conf { background:var(--conf); color:#031424; }
  .b-prov { background:#555; color:#eee; }
  h2 { font-size:13px; border-bottom:1px solid #2a2f3a; padding-bottom:3px; margin:14px 0 6px; }
  table { border-collapse:collapse; width:100%; font-size:11px; margin-bottom:8px; }
  th,td { border:1px solid #2a2f3a; padding:2px 5px; text-align:right; }
  th:first-child, td:first-child { text-align:left; }
  td:last-child, th:last-child { white-space:nowrap; }
  tr.disc-dd { background:rgba(224,169,59,0.12); }
  tr.disc-ss { background:rgba(77,163,255,0.12); }
  .muted { color:var(--muted); }
  .legend span { margin-right:10px; }
  .dot { display:inline-block; width:10px; height:10px; border-radius:50%; vertical-align:middle; }
</style>
</head>
<body>
<header>
  <h1>설계 네트워크 뷰어</h1>
  <span><label>코호트</label><select id="cohort"></select></span>
  <span><label>엣지</label>
    <select id="edgeMode">
      <option value="design">설계 유사도</option>
      <option value="ec">전기화학 유사도</option>
      <option value="both" selected>둘 다</option>
    </select></span>
  <span><label>검색</label><input id="search" placeholder="설계 이름/ID"></span>
  <span class="legend">
    <span><span class="dot" style="background:var(--cand)"></span> 후보(candidate)</span>
    <span><span class="dot" style="background:var(--conf)"></span> 확정(confirmed)</span>
  </span>
  <span id="counts" class="muted"></span>
</header>
<div id="layout">
  <div id="graph"><svg id="svg"></svg></div>
  <div id="side">
    <div id="selinfo"><p class="muted">노드를 클릭해 선택하세요 (최대 3개, 레이더/비교).</p></div>
    <h2>불일치(discordance) 테이블
      <span class="muted" style="font-weight:normal">
        설계유사≥<input id="hi" type="number" step="0.05" value="0.6" style="width:52px">
        &amp; 전기화학≤<input id="lo" type="number" step="0.05" value="0.4" style="width:52px">
        (및 반대)</span>
    </h2>
    <div id="disc"></div>
    <h2>레이더 (코호트 내 백분위, 방향보정)</h2>
    <div id="radar"></div>
    <h2>쌍 비교</h2>
    <div id="pair"><p class="muted">두 노드를 선택하면 지표별 비교가 표시됩니다.</p></div>
  </div>
</div>
<script id="payload" type="application/json">__DATA__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("payload").textContent);
const SVG_NS = "http://www.w3.org/2000/svg";
const byId = {}; DATA.designs.forEach(d => byId[d.design_id] = d);
const metricName = {}; DATA.metric_defs.forEach(m => metricName[m.metric_id] = m.name_ko || m.metric_id);
let selected = [];
let positions = {};

function statusColor(s){ return s==="confirmed" ? getCss("--conf") : (s==="candidate" ? getCss("--cand") : "#9aa"); }
function getCss(v){ return getComputedStyle(document.documentElement).getPropertyValue(v).trim() || "#888"; }
function metricLabel(mk){ const id = mk.split("|")[0]; const w = mk.split("|")[1];
  return (metricName[id]||id) + (w ? " ("+w+")" : ""); }

/* ---- cohort selector: useful ones first, with counts ---- */
function initCohorts(){
  const sel = document.getElementById("cohort");
  const ordered = DATA.cohorts.slice().sort((a,b)=> (b.useful-a.useful) || (b.design_ids.length-a.design_ids.length));
  ordered.forEach(c => {
    const o = document.createElement("option");
    o.value = c.cohort_key;
    const n = c.design_ids.length;
    o.textContent = (c.useful ? "★ " : "   ") + c.cohort_key + "  ["+n+"개 설계]";
    sel.appendChild(o);
  });
  if (ordered[0]) sel.value = ordered[0].cohort_key;
}

function currentCohort(){ return document.getElementById("cohort").value; }
function ecEdges(ck){ return (DATA.electrochem_edges[ck]) || []; }
function designEdgesFor(ids){
  const set = new Set(ids);
  return DATA.design_edges.filter(e => set.has(e.source) && set.has(e.target));
}

/* ---- deterministic force layout in JS ---- */
function layout(ids, edges){
  const n = ids.length; positions = {};
  if (n === 0) return;
  const R = Math.max(230, n*7), cx = 300, cy = 260;
  ids.forEach((id,i) => {
    const a = 2*Math.PI*i/n;
    positions[id] = { x: cx + R*Math.cos(a), y: cy + R*Math.sin(a) };
  });
  const adj = {}; ids.forEach(id => adj[id]=[]);
  edges.forEach(e => { if(adj[e.source]&&adj[e.target]){ adj[e.source].push(e.target); adj[e.target].push(e.source);} });
  for (let it=0; it<160; it++){
    const fx={}, fy={}; ids.forEach(id=>{fx[id]=0;fy[id]=0;});
    for (let i=0;i<n;i++) for (let j=i+1;j<n;j++){
      const a=ids[i], b=ids[j];
      let dx=positions[a].x-positions[b].x, dy=positions[a].y-positions[b].y;
      let d2=dx*dx+dy*dy+0.01, d=Math.sqrt(d2);
      const rep=(n>30?9000:2400)/d2; fx[a]+=rep*dx/d; fy[a]+=rep*dy/d; fx[b]-=rep*dx/d; fy[b]-=rep*dy/d;
    }
    edges.forEach(e=>{ const a=e.source,b=e.target; if(!positions[a]||!positions[b])return;
      let dx=positions[b].x-positions[a].x, dy=positions[b].y-positions[a].y;
      let d=Math.sqrt(dx*dx+dy*dy)+0.01; const spr=(d-120)*0.02*(0.3+e.weight);
      fx[a]+=spr*dx/d; fy[a]+=spr*dy/d; fx[b]-=spr*dx/d; fy[b]-=spr*dy/d;
    });
    ids.forEach(id=>{ positions[id].x+=Math.max(-14,Math.min(14,fx[id]));
      positions[id].y+=Math.max(-14,Math.min(14,fy[id])); });
  }
}

function render(){
  const ck = currentCohort();
  const cohort = DATA.cohorts.find(c=>c.cohort_key===ck);
  const ids = cohort ? cohort.design_ids.slice() : DATA.designs.map(d=>d.design_id);
  const mode = document.getElementById("edgeMode").value;
  const q = document.getElementById("search").value.trim().toLowerCase();

  const dEdges = designEdgesFor(ids);
  const eEdges = ecEdges(ck);
  const K = ids.length > 30 ? 3 : Infinity;
  const knn = list => {
    if (!isFinite(K)) return list;
    const best = {};
    list.forEach(e => [e.source, e.target].forEach(id => (best[id] = best[id] || []).push(e)));
    const keep = new Set();
    Object.values(best).forEach(arr => arr.sort((a,b)=>b.weight-a.weight).slice(0,K).forEach(e=>keep.add(e)));
    return list.filter(e => keep.has(e));
  };
  let edges = [];
  if (mode==="design"||mode==="both") edges=edges.concat(knn(dEdges).map(e=>({...e,kind:"design"})));
  if (mode==="ec"||mode==="both") edges=edges.concat(knn(eEdges).map(e=>({...e,kind:"ec"})));

  layout(ids, edges);

  const svg = document.getElementById("svg");
  svg.innerHTML = "";
  let x0=Infinity,y0=Infinity,x1=-Infinity,y1=-Infinity;
  ids.forEach(id=>{ const p=positions[id]; if(!p) return; x0=Math.min(x0,p.x); y0=Math.min(y0,p.y); x1=Math.max(x1,p.x); y1=Math.max(y1,p.y); });
  if (isFinite(x0)) svg.setAttribute("viewBox",`${x0-40} ${y0-40} ${Math.max(600,x1-x0+260)} ${Math.max(520,y1-y0+80)}`);
  else svg.setAttribute("viewBox","0 0 600 520");
  const showLabels = ids.length <= 30;
  edges.forEach(e=>{
    if(!positions[e.source]||!positions[e.target]) return;
    const l=document.createElementNS(SVG_NS,"line");
    l.setAttribute("x1",positions[e.source].x); l.setAttribute("y1",positions[e.source].y);
    l.setAttribute("x2",positions[e.target].x); l.setAttribute("y2",positions[e.target].y);
    l.setAttribute("class","edge");
    l.setAttribute("stroke", e.kind==="design"?getCss("--edge1"):getCss("--edge2"));
    l.setAttribute("stroke-width", (0.5+2.5*e.weight).toFixed(2));
    svg.appendChild(l);
  });
  ids.forEach(id=>{
    const d=byId[id]; if(!d||!positions[id]) return;
    const g=document.createElementNS(SVG_NS,"g");
    g.setAttribute("class","node"+(selected.includes(id)?" sel":""));
    g.setAttribute("transform",`translate(${positions[id].x},${positions[id].y})`);
    const c=document.createElementNS(SVG_NS,"circle");
    const r=(ids.length>30?3:6)+Math.sqrt(d.n_cells||1)*(ids.length>30?1.6:3.2);
    c.setAttribute("r",r.toFixed(1)); c.setAttribute("fill",statusColor(d.status));
    const hit = q && (d.label.toLowerCase().includes(q)||id.toLowerCase().includes(q));
    if(hit) c.setAttribute("stroke","#fff");
    const tip=document.createElementNS(SVG_NS,"title"); tip.textContent=`${d.label} (${id}, 셀 ${d.n_cells||0})`;
    c.appendChild(tip);
    g.appendChild(c);
    if (showLabels || hit || selected.includes(id)) {
      const t=document.createElementNS(SVG_NS,"text");
      t.setAttribute("x",r+3); t.setAttribute("y",3);
      t.textContent = d.label.length>40 ? d.label.slice(0,38)+"…" : d.label;
      g.appendChild(t);
    }
    g.addEventListener("click",()=>toggleSelect(id));
    svg.appendChild(g);
  });

  document.getElementById("counts").textContent =
    `설계 ${ids.length} · 설계엣지 ${dEdges.length} · 전기화학엣지 ${eEdges.length}` + (isFinite(K) ? ` (표시: 노드별 상위 ${K}개)` : "");
  renderDisc(ck, ids, dEdges, eEdges);
  renderSelection();
}

function toggleSelect(id){
  const i=selected.indexOf(id);
  if(i>=0) selected.splice(i,1);
  else { selected.push(id); if(selected.length>3) selected.shift(); }
  render();
}

function badge(status){
  if(status==="candidate") return '<span class="badge b-cand">후보</span>';
  if(status==="confirmed") return '<span class="badge b-conf">확정</span>';
  return '<span class="badge b-prov">'+status+'</span>';
}

/* ---- discordance ---- */
function renderDisc(ck, ids, dEdges, eEdges){
  const hi=parseFloat(document.getElementById("hi").value);
  const lo=parseFloat(document.getElementById("lo").value);
  const dMap={}, eMap={};
  dEdges.forEach(e=>dMap[key(e.source,e.target)]=e.weight);
  eEdges.forEach(e=>eMap[key(e.source,e.target)]=e.weight);
  const rows=[]; const keys=new Set([...Object.keys(dMap),...Object.keys(eMap)]);
  keys.forEach(k=>{
    const ds=dMap[k], es=eMap[k];
    if(ds===undefined||es===undefined) return;
    let type=null;
    if(ds>=hi && es<=lo) type="dd";      // 설계 유사 but 응답 상이
    else if(ds<=lo && es>=hi) type="ss"; // 설계 상이 but 응답 유사
    if(!type) return;
    const [a,b]=k.split("\u0001");
    rows.push({a,b,ds,es,type});
  });
  rows.sort((x,y)=>Math.abs(y.ds-y.es)-Math.abs(x.ds-x.es));
  let h='<table><tr><th>설계 A</th><th>설계 B</th><th>설계유사</th><th>전기화학</th><th>유형</th></tr>';
  if(!rows.length) h+='<tr><td colspan="5" class="muted">해당 임계값에서 불일치 없음</td></tr>';
  rows.forEach(r=>{
    h+=`<tr class="disc-${r.type}"><td>${lab(r.a)}</td><td>${lab(r.b)}</td>`+
       `<td>${r.ds.toFixed(3)}</td><td>${r.es.toFixed(3)}</td>`+
       `<td>${r.type==="dd"?"유사설계·상이응답":"상이설계·유사응답"}</td></tr>`;
  });
  h+='</table>';
  document.getElementById("disc").innerHTML=h;
}
function key(a,b){ return a<b ? a+"\u0001"+b : b+"\u0001"+a; }
function lab(id){ const d=byId[id]; return d?d.label:id; }

/* ---- selection panel: info + radar + pair ---- */
function renderSelection(){
  const info=document.getElementById("selinfo");
  if(!selected.length){ info.innerHTML='<p class="muted">노드를 클릭해 선택하세요 (최대 3개).</p>'; }
  else {
    info.innerHTML='<b>선택:</b> '+selected.map(id=>{
      const d=byId[id]; return `${d.label} ${badge(d.status)} <span class="muted">(n_cells=${d.n_cells})</span>`;
    }).join(" · ");
    const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
    const refs=selected.map(id=>{
      const sl=byId[id].slides||[];
      if(!sl.length) return "";
      return `<div class="muted" style="margin-top:4px">📄 ${esc(byId[id].label)} 장표: `+
        sl.map(s=>`${esc(s.file)} p${s.slide}${s.set?" ("+esc(s.set)+")":""}${s.confidence==="verified"?" ✓":""}`).join(" · ")+"</div>";
    }).join("");
    info.innerHTML+=refs;
  }
  renderRadar();
  renderPair();
}

function cohortMetricKeys(ck){
  const dm=DATA.design_metrics[ck]||{};
  const set=new Set();
  Object.values(dm).forEach(mrow=>Object.keys(mrow).forEach(k=>set.add(k)));
  return [...set].sort();
}

function renderRadar(){
  const box=document.getElementById("radar");
  const ck=currentCohort();
  const pc=DATA.percentiles[ck]||{};
  const sel=selected.filter(id=>pc[id]);
  if(!sel.length){ box.innerHTML='<p class="muted">선택된 설계의 코호트 지표가 없습니다.</p>'; return; }
  const keys=cohortMetricKeys(ck).filter(k=> sel.some(id=>pc[id]&&pc[id][k]));
  if(keys.length<3){ box.innerHTML='<p class="muted">레이더에 필요한 지표(≥3)가 부족합니다.</p>'; }
  const cx=160,cy=150,R=110,n=Math.max(keys.length,3);
  let svg=`<svg viewBox="0 0 440 310" style="height:300px">`;
  for(let ring=1;ring<=4;ring++){
    let pts="";
    for(let i=0;i<n;i++){ const a=-Math.PI/2+2*Math.PI*i/n;
      pts+=`${cx+R*ring/4*Math.cos(a)},${cy+R*ring/4*Math.sin(a)} `; }
    svg+=`<polygon points="${pts}" fill="none" stroke="#2a2f3a"/>`;
  }
  keys.forEach((k,i)=>{ const a=-Math.PI/2+2*Math.PI*i/n;
    const lx=cx+(R+18)*Math.cos(a), ly=cy+(R+18)*Math.sin(a);
    const md=DATA.metric_defs.find(m=>m.metric_id===k.split("|")[0]);
    const flip=(md&&md.higher_is_better===null)?" *":"";
    svg+=`<line x1="${cx}" y1="${cy}" x2="${cx+R*Math.cos(a)}" y2="${cy+R*Math.sin(a)}" stroke="#2a2f3a"/>`;
    svg+=`<text x="${lx}" y="${ly}" font-size="9" fill="#8a93a3" text-anchor="middle">${metricLabel(k)}${flip}</text>`;
  });
  const colors=["#4da3ff","#e0a93b","#6cc070"];
  sel.forEach((id,si)=>{ let pts="";
    keys.forEach((k,i)=>{ const a=-Math.PI/2+2*Math.PI*i/n;
      const p=(pc[id]&&pc[id][k])?pc[id][k].pct:0; const rr=R*p/100;
      pts+=`${cx+rr*Math.cos(a)},${cy+rr*Math.sin(a)} `; });
    svg+=`<polygon points="${pts}" fill="${colors[si]}22" stroke="${colors[si]}" stroke-width="1.6"/>`;
  });
  svg+=`</svg><div class="muted">* 방향(higher_is_better) 미정 지표 — 미보정 표시</div>`;
  box.innerHTML=svg;
}

function renderPair(){
  const box=document.getElementById("pair");
  if(selected.length<2){ box.innerHTML='<p class="muted">두 노드를 선택하면 비교됩니다.</p>'; return; }
  const [a,b]=selected.slice(0,2);
  const ck=currentCohort();
  const dm=DATA.design_metrics[ck]||{};
  const ma=dm[a]||{}, mb=dm[b]||{};
  const keys=[...new Set([...Object.keys(ma),...Object.keys(mb)])].sort();
  let h=`<div><b>${lab(a)}</b> ${badge(byId[a].status)} vs <b>${lab(b)}</b> ${badge(byId[b].status)}</div>`;
  h+='<table><tr><th>지표</th><th>'+lab(a)+' (평균±sd, n)</th><th>'+lab(b)+' (평균±sd, n)</th><th>Δ</th><th>비고</th></tr>';
  keys.forEach(k=>{
    const ca=ma[k], cb=mb[k];
    const fa=fmtCell(ca), fb=fmtCell(cb);
    let delta="-", note="";
    if(ca&&cb&&ca.mean!=null&&cb.mean!=null) delta=(ca.mean-cb.mean).toPrecision(4);
    else note = !ca||ca.mean==null ? lab(a)+" 결측" : lab(b)+" 결측";
    h+=`<tr><td>${metricLabel(k)}</td><td>${fa}</td><td>${fb}</td><td>${delta}</td><td class="muted">${note}</td></tr>`;
  });
  h+='</table>';
  box.innerHTML=h;
}
function fmtCell(c){
  if(!c||c.mean==null) return '<span class="muted">결측</span>';
  const sd=c.sd!=null?"±"+(+c.sd).toPrecision(3):"";
  return `${(+c.mean).toPrecision(4)}${sd} <span class="muted">(n=${c.n_cells})</span>`;
}

/* ---- wiring ---- */
["cohort","edgeMode"].forEach(id=>document.getElementById(id).addEventListener("change",()=>{selected=[];render();}));
document.getElementById("search").addEventListener("input",render);
["hi","lo"].forEach(id=>document.getElementById(id).addEventListener("input",render));
initCohorts();
render();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m battery_insight.design_network",
        description="Build the offline design-network viewer from research.sqlite3.",
    )
    parser.add_argument("--project", required=True, help="Project directory (contains db/research.sqlite3).")
    parser.add_argument("--cohort", default=None, help="Restrict to a single cohort_key.")
    parser.add_argument("--design-threshold", type=float, default=0.3,
                        help="Minimum design similarity to draw a structural edge.")
    args = parser.parse_args(argv)

    html = build_network(args.project, cohort=args.cohort, design_threshold=args.design_threshold)
    json_path = html.with_name("network.json")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    n_designs = len(payload["designs"])
    n_design_edges = len(payload["design_edges"])
    n_ec = sum(len(v) for v in payload["electrochem_edges"].values())
    n_useful = sum(1 for c in payload["cohorts"] if c["useful"])
    print(f"[design_network] designs={n_designs} design_edges={n_design_edges} "
          f"ec_edges={n_ec} cohorts={len(payload['cohorts'])} (useful={n_useful})")
    if payload.get("deviations"):
        print("[design_network] schema deviations: " + "; ".join(payload["deviations"]))
    print(f"[design_network] wrote {json_path}")
    print(f"[design_network] wrote {html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
