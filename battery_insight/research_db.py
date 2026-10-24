"""Module B: research identity + metrics database (agent researchdb).

Builds ``db\\research.sqlite3`` from the dx catalogue and the optional Module A
Parquet outputs. Identity is modelled conservatively: inferred / candidate values
are always labelled and never silently promoted to confirmed, repeats on one
physical cell collapse to a median, and the user-owned ``db\\design_map.csv`` is
never overwritten by a rebuild.

CLI::

    python -m battery_insight.research_db --project <dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

CODE_VERSION = "research_db_v1"

DESIGN_KEYS = ("material", "process", "cell_format", "supplier", "geometry", "anode")
CONDITION_KEYS = ("storage", "condition", "equipment")

# name-derived test type keywords (lowercase substring -> canonical type)
NAME_TYPE_RULES = (
    ("formation", "Formation"), ("form", "Formation"),
    ("hppc", "HPPC"), ("dcir", "DCIR"), ("gitt", "GITT"),
    ("insitu", "Insitu"), ("in-situ", "Insitu"),
    ("storage", "Storage"), ("stor", "Storage"),
    ("rpt", "RPT"), ("cyc", "Cycle"),
)

DESIGN_MAP_TEMPLATE = (
    "# db/design_map.csv  (user-owned; never overwritten by a rebuild)\n"
    "# Map tests to a confirmed design. First match wins by priority:\n"
    "#   match_type: test_key > series_key > candidate > tags\n"
    "#   match_value: the test_key / series_key / a candidate design_id (cand:...) from\n"
    "#                design_candidates.csv / a single normalised tag token (e.g. process=dry)\n"
    "#   design_id / design_label: your stable identifier + human label\n"
    "#   note: free text\n"
    "match_type,match_value,design_id,design_label,note\n"
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project(project) -> Path:
    return Path(project)


def _db_dir(project) -> Path:
    d = _project(project) / "db"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_csv(path: Path):
    if not Path(path).exists():
        return None
    return pd.read_csv(path, dtype=str, keep_default_na=True)


def _read_parquet(path: Path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        return pd.read_parquet(p)
    except Exception:
        return None


def _s(value) -> str:
    """Normalise any cell to a clean string ('' for missing)."""
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except (TypeError, ValueError):
        pass
    text = str(value).strip()
    if text.lower() in ("", "nan", "none", "null"):
        return ""
    return text


def _num(value):
    text = _s(value)
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _short_hash(tokens) -> str:
    joined = "|".join(sorted(tokens))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:10]


def parse_tags(raw):
    """Parse ``key=value; key=value`` into ordered (key, value) pairs."""
    pairs = []
    text = _s(raw)
    if not text:
        return pairs
    for chunk in text.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if key and value:
            pairs.append((key, value))
    return pairs


def _tag_fields(pairs):
    """Split pairs into design fields / condition fields and detect conflicts."""
    design, condition, allt = {}, {}, {}
    for key, value in pairs:
        allt.setdefault(key, [])
        if value not in allt[key]:
            allt[key].append(value)
        if key in DESIGN_KEYS:
            design.setdefault(key, [])
            if value not in design[key]:
                design[key].append(value)
        elif key in CONDITION_KEYS:
            condition.setdefault(key, [])
            if value not in condition[key]:
                condition[key].append(value)
    conflicts = sorted(k for k, v in design.items() if len(v) > 1)
    return design, condition, allt, conflicts


def _design_tokens(design_fields):
    tokens = []
    for key in sorted(design_fields):
        for value in sorted(design_fields[key]):
            tokens.append(f"{key}={value}".lower())
    return tokens


def _design_label(design_fields, fallback=""):
    if not design_fields:
        return fallback
    parts = []
    for key in DESIGN_KEYS:
        if key in design_fields:
            parts.append(f"{key}:{'/'.join(sorted(design_fields[key]))}")
    return "; ".join(parts) or fallback


def resolve_test_type(row):
    """confirmed test_type > name-derived > inferred(conf) > unknown."""
    confirmed = _s(row.get("test_type"))
    if confirmed:
        return confirmed, "confirmed", 1.0
    name = (_s(row.get("test_name")) + " " + _s(row.get("sample_name"))).lower()
    for needle, canon in NAME_TYPE_RULES:
        if needle in name:
            return canon, "name", None
    inferred = _s(row.get("test_type_inferred"))
    if inferred:
        return inferred, "inferred", _num(row.get("test_type_inferred_conf"))
    return "unknown", "unknown", None


# --------------------------------------------------------------------------- #
# design map (user-owned)
# --------------------------------------------------------------------------- #
def _load_design_map(project):
    path = _db_dir(project) / "design_map.csv"
    if not path.exists():
        path.write_text(DESIGN_MAP_TEMPLATE, encoding="utf-8")
        return []
    rows = []
    df = pd.read_csv(path, dtype=str, comment="#", keep_default_na=True)
    for _, r in df.iterrows():
        mt, mv = _s(r.get("match_type")), _s(r.get("match_value"))
        did = _s(r.get("design_id"))
        if not mt or not mv or not did:
            continue
        rows.append({
            "match_type": mt.lower(), "match_value": mv,
            "design_id": did, "design_label": _s(r.get("design_label")),
            "note": _s(r.get("note")),
        })
    return rows


def _map_lookup(mapping, *, test_key, series_key, tokens, candidate=""):
    token_set = set(tokens)
    for mt in ("test_key", "series_key", "candidate", "tags"):
        for row in mapping:
            if row["match_type"] != mt:
                continue
            value = row["match_value"]
            if mt == "test_key" and value == test_key:
                return row
            if mt == "series_key" and value == series_key:
                return row
            if mt == "candidate" and candidate and value == candidate:
                return row
            if mt == "tags" and value.lower() in token_set:
                return row
    return None


# --------------------------------------------------------------------------- #
# cohort key
# --------------------------------------------------------------------------- #
def _cohort_key(test_type, temp, c_rate_class, v_window):
    return "|".join([
        test_type or "unknown",
        temp or "T?",
        c_rate_class or "C?",
        v_window or "V?",
    ])


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def build_research_db(project) -> dict:
    project = _project(project)
    dx = project / "dx"
    dbdir = _db_dir(project)

    files = _read_csv(dx / "dx_files.csv")
    tests = _read_csv(dx / "dx_tests.csv")
    if files is None or tests is None:
        raise FileNotFoundError(f"dx_files.csv / dx_tests.csv not found under {dx}")

    mapping = _load_design_map(project)

    # Module A outputs (optional)
    run_profile_df = _read_parquet(dbdir / "run_profile.parquet")
    run_metrics_df = _read_parquet(dbdir / "run_metrics.parquet")
    metric_defs = _load_metric_defs()

    # run_profile lookup: run_id -> (c_rate_class, v_window, quality)
    profile_map = {}
    if run_profile_df is not None and len(run_profile_df):
        for _, r in run_profile_df.iterrows():
            v_lo, v_hi = _num(r.get("v_lo")), _num(r.get("v_hi"))
            v_window = f"{v_lo}-{v_hi}" if v_lo is not None and v_hi is not None else ""
            profile_map[_s(r.get("run_id"))] = (
                _s(r.get("c_rate_class")), v_window, _s(r.get("quality")) or "unknown",
            )

    # --- per-test resolution -------------------------------------------------
    test_rows, design_catalog = {}, {}
    # design_catalog: design_id -> dict(label,status,source,tags,fields,conflicts)
    series_fallback = {}  # series_key -> design_id

    for _, row in tests.iterrows():
        test_key = _s(row.get("test_key"))
        if not test_key:
            continue
        series_key = _s(row.get("series_key"))
        pairs = parse_tags(row.get("tags"))
        design_fields, _cond, all_tags, conflicts = _tag_fields(pairs)
        tokens = _design_tokens(design_fields)
        ttype, tsource, tconf = resolve_test_type(row)
        temp = _num(row.get("temperature_c"))
        temp_label = ("%g" % temp) if temp is not None else ""

        if tokens:
            candidate_id = "cand:" + _short_hash(tokens)
        elif series_key:
            candidate_id = series_fallback.setdefault(series_key, "cand:series:" + _short_hash([series_key]))
        else:
            candidate_id = "cand:series:" + _short_hash([test_key])
        hit = _map_lookup(mapping, test_key=test_key, series_key=series_key, tokens=tokens,
                          candidate=candidate_id)
        if hit:
            design_id = hit["design_id"]
            label = hit["design_label"] or _design_label(design_fields, series_key)
            status, source = "user", "design_map"
        elif tokens:
            design_id = candidate_id
            label = _design_label(design_fields, _s(row.get("sample_name")))
            status, source = "candidate", "tags"
        else:
            design_id = candidate_id
            label = _s(row.get("sample_name")) or _s(row.get("test_name")) or series_key
            status, source = "candidate", "series_key"

        entry = design_catalog.setdefault(design_id, {
            "design_label": label, "status": status, "source": source,
            "tags": {}, "fields": {}, "conflicts": set(),
        })
        # merge tags/fields across tests of the same design
        for k, vs in all_tags.items():
            entry["tags"].setdefault(k, [])
            for v in vs:
                if v not in entry["tags"][k]:
                    entry["tags"][k].append(v)
        for k, vs in design_fields.items():
            entry["fields"].setdefault(k, [])
            for v in vs:
                if v not in entry["fields"][k]:
                    entry["fields"][k].append(v)
        entry["conflicts"].update(conflicts)
        if entry["status"] != "user" and status == "user":
            entry.update(design_label=label, status=status, source=source)

        test_rows[test_key] = {
            "test_key": test_key, "test_type": ttype, "test_type_source": tsource,
            "test_type_conf": tconf, "test_date": _s(row.get("test_date")),
            "temperature_c": temp, "temperature_label": temp_label,
            "pressure_mpa": _num(row.get("pressure_mpa")),
            "protocol": _s(row.get("protocol")), "rpt_stage": _s(row.get("rpt_stage")),
            "owner": _s(row.get("owner_name")), "set_no": _s(row.get("set_no")),
            "batch": _s(row.get("batch_candidate")), "sample_name": _s(row.get("sample_name")),
            "tags": _s(row.get("tags")), "design_id": design_id, "series_key": series_key,
            "anomaly": _s(row.get("anomaly")), "candidate_id": candidate_id,
            "test_name": _s(row.get("test_name")),
        }

    # --- runs / cells / run_datasets ----------------------------------------
    cells, runs, run_datasets, skipped_excluded = {}, {}, [], 0
    files_sorted = files.copy()
    files_sorted["_frag"] = files_sorted["fragment"].fillna("")
    for (test_key, channel), grp in files_sorted.groupby(["test_key", "channel"], dropna=False):
        test_key, channel = _s(test_key), _s(channel)
        if not test_key or not channel:
            continue
        trow = test_rows.get(test_key)
        if trow is None:
            continue
        run_id = f"{test_key}:{channel}"
        design_id = trow["design_id"]

        grp = grp.sort_values(["_frag", "dataset_id"])
        pcid = ""
        order = 0
        for _, fr in grp.iterrows():
            if _s(fr.get("exclude")):
                skipped_excluded += 1
                continue
            dsid = _s(fr.get("dataset_id"))
            if not dsid:
                continue
            if not pcid:
                pcid = _s(fr.get("physical_cell_id"))
            run_datasets.append({
                "run_id": run_id, "dataset_id": dsid, "fragment_order": order,
                "sha256": _s(fr.get("source_sha256")),
            })
            order += 1
        if order == 0:
            continue

        if pcid:
            cell_id, cstatus = "pc:" + pcid, "confirmed"
        else:
            cell_id, cstatus = "run:" + run_id, "provisional"
        cells.setdefault(cell_id, {
            "cell_id": cell_id, "status": cstatus, "design_id": design_id,
            "physical_cell_id": pcid, "sample_name": trow["sample_name"],
        })

        c_rate_class, v_window, quality = profile_map.get(run_id, ("", "", "unknown"))
        cohort_key = _cohort_key(trow["test_type"], trow["temperature_label"], c_rate_class, v_window)
        runs[run_id] = {
            "run_id": run_id, "test_key": test_key, "channel": channel,
            "cell_id": cell_id, "design_id": design_id, "cohort_key": cohort_key,
            "n_datasets": order, "quality": quality,
            "test_type": trow["test_type"], "temperature_label": trow["temperature_label"],
            "c_rate_class": c_rate_class, "v_window": v_window, "anomaly": trow["anomaly"],
        }

    # design n_cells / n_runs
    for did, entry in design_catalog.items():
        entry["n_cells"] = sum(1 for c in cells.values() if c["design_id"] == did)
        entry["n_runs"] = sum(1 for r in runs.values() if r["design_id"] == did)

    # --- observations + metrics ---------------------------------------------
    observations = []
    run_ids = set(runs)
    if run_metrics_df is not None and len(run_metrics_df):
        for _, r in run_metrics_df.iterrows():
            rid = _s(r.get("run_id"))
            if rid not in run_ids:
                continue
            observations.append({
                "run_id": rid, "metric_id": _s(r.get("metric_id")),
                "window": _s(r.get("window")), "value": _num(r.get("value")),
                "unit": _s(r.get("unit")), "censored": int(bool(r.get("censored"))),
                "quality": _s(r.get("quality")), "method": _s(r.get("method")),
            })

    cell_metrics, design_metrics = _aggregate(observations, runs, cells)

    # --- cohorts -------------------------------------------------------------
    cohorts = {}
    for r in runs.values():
        ck = r["cohort_key"]
        c = cohorts.setdefault(ck, {
            "cohort_key": ck, "test_type": r["test_type"],
            "temperature_c": r["temperature_label"] or None,
            "c_rate_class": r["c_rate_class"] or None, "v_window": r["v_window"] or None,
            "designs": set(), "cells": set(),
        })
        c["designs"].add(r["design_id"])
        c["cells"].add(r["cell_id"])

    # --- write sqlite atomically --------------------------------------------
    counts = {
        "designs": len(design_catalog),
        "designs_candidate": sum(1 for e in design_catalog.values() if e["status"] == "candidate"),
        "designs_user": sum(1 for e in design_catalog.values() if e["status"] == "user"),
        "cells": len(cells),
        "cells_confirmed": sum(1 for c in cells.values() if c["status"] == "confirmed"),
        "cells_provisional": sum(1 for c in cells.values() if c["status"] == "provisional"),
        "runs": len(runs),
        "tests": len(test_rows),
        "observations": len(observations),
        "cell_metrics": len(cell_metrics),
        "design_metrics": len(design_metrics),
        "cohorts": len(cohorts),
        "cohorts_ge2_designs": sum(1 for c in cohorts.values() if len(c["designs"]) >= 2),
        "excluded_files": skipped_excluded,
        "anomaly_runs": sum(1 for r in runs.values() if r["anomaly"]),
        "has_module_a": run_metrics_df is not None,
    }

    final = dbdir / "research.sqlite3"
    tmp = dbdir / ".research.tmp.sqlite3"
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        _write_schema(conn)
        _fill_tables(conn, design_catalog, cells, test_rows, runs, run_datasets,
                     run_profile_df, observations, cell_metrics, design_metrics,
                     metric_defs, cohorts)
        inputs = {
            "dx_files": str(dx / "dx_files.csv"), "dx_tests": str(dx / "dx_tests.csv"),
            "run_metrics": run_metrics_df is not None, "run_profile": run_profile_df is not None,
        }
        conn.execute(
            "INSERT INTO build_log(run_at,code_version,inputs_json,counts_json) VALUES(?,?,?,?)",
            (_now(), CODE_VERSION, json.dumps(inputs, ensure_ascii=False),
             json.dumps(counts, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp, final)

    # --- side outputs --------------------------------------------------------
    _write_xlsx(final, dbdir / "research.xlsx")
    _write_identity_gaps(dbdir / "identity_gaps.csv", test_rows, cells, runs, design_catalog)
    _write_design_candidates(dbdir / "design_candidates.csv", test_rows, design_catalog)

    summary = {"project": str(project), "db": str(final), **counts}
    summary["top_cohorts"] = sorted(
        ({"cohort_key": c["cohort_key"], "n_designs": len(c["designs"]),
          "n_cells": len(c["cells"])} for c in cohorts.values()),
        key=lambda x: (-x["n_designs"], -x["n_cells"]),
    )[:10]
    return summary


def _aggregate(observations, runs, cells):
    """cell_metrics (median over runs) and design_metrics (equal weight/cell)."""
    if not observations:
        return [], []
    obs = pd.DataFrame(observations)
    if "quality" in obs.columns:
        obs = obs.rename(columns={"quality": "obs_quality"})
    rmap = pd.DataFrame([
        {"run_id": r["run_id"], "cell_id": r["cell_id"], "cohort_key": r["cohort_key"],
         "design_id": r["design_id"], "test_type": r["test_type"], "quality": r["quality"],
         "anomaly": r.get("anomaly", "")}
        for r in runs.values()
    ])
    obs = obs.merge(rmap, on="run_id", how="inner")
    obs = obs[obs["value"].notna()]
    obs = obs[(obs["quality"] != "insufficient") & (obs["test_type"] != "unknown") & (obs["anomaly"] == "")]
    if obs.empty:
        return [], []

    cell_rows = []
    gcols = ["cell_id", "cohort_key", "metric_id", "window"]
    for keys, grp in obs.groupby(gcols, dropna=False):
        cid, ck, mid, win = keys
        cell_rows.append({
            "cell_id": cid, "cohort_key": ck, "metric_id": mid, "window": win,
            "value": float(np.median(grp["value"])), "n_runs": int(grp["run_id"].nunique()),
        })
    cell_df = pd.DataFrame(cell_rows)

    cell_design = {c["cell_id"]: c["design_id"] for c in cells.values()}
    cell_df["design_id"] = cell_df["cell_id"].map(cell_design)

    design_rows = []
    gcols = ["design_id", "cohort_key", "metric_id", "window"]
    for keys, grp in cell_df.groupby(gcols, dropna=False):
        did, ck, mid, win = keys
        vals = grp["value"].to_numpy(dtype=float)
        n = len(vals)
        design_rows.append({
            "design_id": did, "cohort_key": ck, "metric_id": mid, "window": win,
            "n_cells": n, "mean": float(np.mean(vals)),
            "sd": float(np.std(vals, ddof=1)) if n >= 2 else None,
            "median": float(np.median(vals)), "min": float(np.min(vals)),
            "max": float(np.max(vals)),
        })
    return cell_rows, design_rows


def _load_metric_defs():
    try:
        from battery_insight.run_metrics import METRIC_DEFS  # type: ignore
        return list(METRIC_DEFS)
    except Exception:
        return []


# --------------------------------------------------------------------------- #
# sqlite schema + fill
# --------------------------------------------------------------------------- #
def _write_schema(conn):
    conn.executescript(
        """
        CREATE TABLE designs(design_id TEXT PRIMARY KEY, design_label TEXT, status TEXT,
            tags_json TEXT, design_fields_json TEXT, conflicts TEXT, source TEXT,
            n_cells INTEGER, n_runs INTEGER);
        CREATE TABLE cells(cell_id TEXT PRIMARY KEY, status TEXT, design_id TEXT,
            physical_cell_id TEXT, sample_name TEXT);
        CREATE TABLE tests(test_key TEXT PRIMARY KEY, test_type TEXT, test_type_source TEXT,
            test_type_conf REAL, test_date TEXT, temperature_c REAL, pressure_mpa REAL,
            protocol TEXT, rpt_stage TEXT, owner TEXT, set_no TEXT, batch TEXT,
            sample_name TEXT, tags TEXT, design_id TEXT, anomaly TEXT, candidate_id TEXT);
        CREATE TABLE runs(run_id TEXT PRIMARY KEY, test_key TEXT, channel TEXT, cell_id TEXT,
            design_id TEXT, cohort_key TEXT, n_datasets INTEGER, quality TEXT);
        CREATE TABLE run_datasets(run_id TEXT, dataset_id TEXT, fragment_order INTEGER, sha256 TEXT);
        CREATE TABLE observations(run_id TEXT, metric_id TEXT, window TEXT, value REAL,
            unit TEXT, censored INTEGER, quality TEXT, method TEXT);
        CREATE TABLE cell_metrics(cell_id TEXT, cohort_key TEXT, metric_id TEXT, window TEXT,
            value REAL, n_runs INTEGER);
        CREATE TABLE design_metrics(design_id TEXT, cohort_key TEXT, metric_id TEXT, window TEXT,
            n_cells INTEGER, mean REAL, sd REAL, median REAL, min REAL, max REAL);
        CREATE TABLE metric_defs(metric_id TEXT PRIMARY KEY, name_ko TEXT, unit TEXT,
            higher_is_better INTEGER, definition TEXT);
        CREATE TABLE cohorts(cohort_key TEXT PRIMARY KEY, test_type TEXT, temperature_c TEXT,
            c_rate_class TEXT, v_window TEXT, n_designs INTEGER, n_cells INTEGER);
        CREATE TABLE build_log(run_at TEXT, code_version TEXT, inputs_json TEXT, counts_json TEXT);
        CREATE INDEX ix_runs_design ON runs(design_id);
        CREATE INDEX ix_runs_cohort ON runs(cohort_key);
        CREATE INDEX ix_dm_cohort ON design_metrics(cohort_key);
        CREATE INDEX ix_obs_run ON observations(run_id);
        """
    )


def _write_many(conn, sql, rows):
    if rows:
        conn.executemany(sql, rows)


def _fill_tables(conn, designs, cells, tests, runs, run_datasets, run_profile_df,
                 observations, cell_metrics, design_metrics, metric_defs, cohorts):
    _write_many(conn,
        "INSERT INTO designs VALUES(?,?,?,?,?,?,?,?,?)",
        [(did, e["design_label"], e["status"],
          json.dumps({k: v for k, v in e["tags"].items()}, ensure_ascii=False),
          json.dumps({k: v for k, v in e["fields"].items()}, ensure_ascii=False),
          "; ".join(sorted(e["conflicts"])), e["source"], e["n_cells"], e["n_runs"])
         for did, e in designs.items()])

    _write_many(conn,
        "INSERT INTO cells VALUES(?,?,?,?,?)",
        [(c["cell_id"], c["status"], c["design_id"], c["physical_cell_id"], c["sample_name"])
         for c in cells.values()])

    _write_many(conn,
        "INSERT INTO tests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(t["test_key"], t["test_type"], t["test_type_source"], t["test_type_conf"],
          t["test_date"], t["temperature_c"], t["pressure_mpa"], t["protocol"],
          t["rpt_stage"], t["owner"], t["set_no"], t["batch"], t["sample_name"],
          t["tags"], t["design_id"], t.get("anomaly", ""), t.get("candidate_id", "")) for t in tests.values()])

    _write_many(conn,
        "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)",
        [(r["run_id"], r["test_key"], r["channel"], r["cell_id"], r["design_id"],
          r["cohort_key"], r["n_datasets"], r["quality"]) for r in runs.values()])

    _write_many(conn,
        "INSERT INTO run_datasets VALUES(?,?,?,?)",
        [(d["run_id"], d["dataset_id"], d["fragment_order"], d["sha256"]) for d in run_datasets])

    # run_profile copy (if present)
    if run_profile_df is not None and len(run_profile_df):
        rp = run_profile_df.copy()
        rp.columns = [str(c) for c in rp.columns]
        rp.to_sql("run_profile", conn, if_exists="replace", index=False)
    else:
        conn.execute("CREATE TABLE IF NOT EXISTS run_profile(run_id TEXT)")

    _write_many(conn,
        "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?)",
        [(o["run_id"], o["metric_id"], o["window"], o["value"], o["unit"],
          o["censored"], o["quality"], o["method"]) for o in observations])

    _write_many(conn,
        "INSERT INTO cell_metrics VALUES(?,?,?,?,?,?)",
        [(m["cell_id"], m["cohort_key"], m["metric_id"], m["window"], m["value"], m["n_runs"])
         for m in cell_metrics])

    _write_many(conn,
        "INSERT INTO design_metrics VALUES(?,?,?,?,?,?,?,?,?,?)",
        [(m["design_id"], m["cohort_key"], m["metric_id"], m["window"], m["n_cells"],
          m["mean"], m["sd"], m["median"], m["min"], m["max"]) for m in design_metrics])

    _write_many(conn,
        "INSERT INTO metric_defs VALUES(?,?,?,?,?)",
        [(_s(d.get("metric_id")), _s(d.get("name_ko")), _s(d.get("unit")),
          (None if d.get("higher_is_better") is None else int(bool(d.get("higher_is_better")))),
          _s(d.get("definition"))) for d in metric_defs if _s(d.get("metric_id"))])

    _write_many(conn,
        "INSERT INTO cohorts VALUES(?,?,?,?,?,?,?)",
        [(c["cohort_key"], c["test_type"], c["temperature_c"], c["c_rate_class"],
          c["v_window"], len(c["designs"]), len(c["cells"])) for c in cohorts.values()])


# --------------------------------------------------------------------------- #
# side outputs
# --------------------------------------------------------------------------- #
def _write_xlsx(sqlite_path, xlsx_path):
    try:
        import openpyxl  # noqa: F401
    except Exception:
        return
    conn = sqlite3.connect(sqlite_path)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            for name in names:
                df = pd.read_sql_query(f"SELECT * FROM {name} LIMIT 50000", conn)
                df.to_excel(writer, sheet_name=name[:31], index=False)
    finally:
        conn.close()


def _write_identity_gaps(path, tests, cells, runs, designs):
    rows = []
    cell_status = {c["cell_id"]: c["status"] for c in cells.values()}
    for r in runs.values():
        trow = tests.get(r["test_key"], {})
        design = designs.get(r["design_id"], {})
        gaps = []
        if cell_status.get(r["cell_id"]) == "provisional":
            gaps.append("no_physical_cell_id")
        if design.get("status") == "candidate":
            gaps.append("candidate_design")
        if design.get("conflicts"):
            gaps.append("tag_conflict")
        if r["test_type"] == "unknown":
            gaps.append("unknown_test_type")
        if not r["temperature_label"]:
            gaps.append("unknown_temperature")
        if r.get("anomaly"):
            gaps.append("anomaly:" + r["anomaly"])
        if gaps:
            rows.append({
                "run_id": r["run_id"], "test_key": r["test_key"],
                "design_id": r["design_id"], "cell_id": r["cell_id"],
                "gaps": ";".join(gaps),
            })
    pd.DataFrame(rows, columns=["run_id", "test_key", "design_id", "cell_id", "gaps"]).to_csv(
        path, index=False, encoding="utf-8-sig")


def _write_design_candidates(path, tests, designs):
    """One row per design with ready-to-copy design_map columns (match_type=candidate)."""
    by_candidate = {}
    for t in tests.values():
        entry = by_candidate.setdefault(t["candidate_id"], {"tests": [], "samples": set(), "assigned": set()})
        entry["tests"].append(t["test_key"])
        if t["sample_name"]:
            entry["samples"].add(t["sample_name"])
        entry["assigned"].add(t["design_id"])
    rows = []
    for candidate_id, entry in by_candidate.items():
        design = designs.get(candidate_id) or designs.get(next(iter(entry["assigned"])), {})
        assigned = sorted(entry["assigned"])
        rows.append({
            "match_type": "candidate", "match_value": candidate_id,
            "design_id": "" if assigned == [candidate_id] else ";".join(assigned),
            "design_label": "" if assigned == [candidate_id] else design.get("design_label", ""),
            "note": "",
            "candidate_label": design.get("design_label", ""),
            "status": design.get("status", ""),
            "n_tests": len(entry["tests"]), "n_cells": design.get("n_cells", 0),
            "tags": json.dumps(design.get("tags", {}), ensure_ascii=False),
            "conflicts": "; ".join(sorted(design.get("conflicts", ()))),
            "sample_names": " | ".join(sorted(entry["samples"])[:5]),
            "test_keys": " ".join(sorted(entry["tests"])[:10]),
        })
    frame = pd.DataFrame(rows)
    if len(frame):
        frame = frame.sort_values(["n_cells", "n_tests"], ascending=False)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


# --------------------------------------------------------------------------- #
# read helpers
# --------------------------------------------------------------------------- #
def connect_ro(project):
    """Open the research db read-only."""
    path = _project(project) / "db" / "research.sqlite3"
    if not path.exists():
        raise FileNotFoundError(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def design_table(project, cohort_key=None):
    """Return designs joined with their metrics (optionally filtered by cohort)."""
    conn = connect_ro(project)
    try:
        if cohort_key:
            return pd.read_sql_query(
                "SELECT d.*, m.cohort_key, m.metric_id, m.window, m.n_cells, m.mean, m.sd, "
                "m.median, m.min, m.max FROM designs d LEFT JOIN design_metrics m "
                "ON d.design_id=m.design_id AND m.cohort_key=? ORDER BY d.design_id",
                conn, params=(cohort_key,))
        return pd.read_sql_query("SELECT * FROM designs ORDER BY n_cells DESC", conn)
    finally:
        conn.close()


def cohort_table(project, min_designs=1):
    conn = connect_ro(project)
    try:
        return pd.read_sql_query(
            "SELECT * FROM cohorts WHERE n_designs>=? ORDER BY n_designs DESC, n_cells DESC",
            conn, params=(min_designs,))
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _report(summary):
    print(f"research.sqlite3 -> {summary['db']}")
    print(f"  designs:  {summary['designs']} "
          f"(candidate={summary['designs_candidate']}, user={summary['designs_user']})")
    print(f"  cells:    {summary['cells']} "
          f"(confirmed={summary['cells_confirmed']}, provisional={summary['cells_provisional']})")
    print(f"  runs:     {summary['runs']}   tests: {summary['tests']}   "
          f"excluded files: {summary['excluded_files']}")
    print(f"  cohorts:  {summary['cohorts']} (>=2 designs: {summary['cohorts_ge2_designs']})")
    print(f"  module A metrics present: {summary['has_module_a']}  "
          f"observations={summary['observations']} "
          f"cell_metrics={summary['cell_metrics']} design_metrics={summary['design_metrics']}")
    if summary.get("top_cohorts"):
        print("  top cohorts by #designs:")
        for c in summary["top_cohorts"]:
            print(f"    {c['n_designs']:>3}d {c['n_cells']:>3}c  {c['cohort_key']}")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="battery_insight.research_db")
    ap.add_argument("--project", required=True, help="project directory")
    args = ap.parse_args(argv)
    summary = build_research_db(args.project)
    _report(summary)
    return summary


if __name__ == "__main__":
    main()
