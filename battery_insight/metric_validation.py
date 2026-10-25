"""Metric-level validation of derived cycles vs recorded PNE cycle labels (G9).

:mod:`battery_insight.derived_cycles` validates reconstructed cycle *boundaries*
against the recorded ``cycle`` column (PNE TotalCycle).  This module validates
one level up: it checks that the per-run *metrics* a user actually reads
(initial discharge capacity, capacity retention, Coulombic efficiency, number of
full cycles) come out the same whether cycles are grouped by the reconstructed
``cycle_derived`` or by the recorded label, on the datasets that *have* recorded
labels.

Design -- the only thing under test is the cycle assignment
------------------------------------------------------------
Both the recorded and the derived metric series are computed from the *same*
derived step sidecar (:mod:`battery_insight.derived_steps`), which already holds
per-step ``kind`` (chg/dchg/rest), ``q_max`` (capacity magnitude), durations,
voltages, *and* ``label_cycle`` (the recorded PNE cycle at the step's start).
The file-global step-role classification (full vs pulse) from
:mod:`battery_insight.derived_cycles` is computed once and reused for both
series.  The recorded series groups steps by ``label_cycle``; the derived series
groups by ``cycle_derived`` from :func:`derived_cycles.assign_cycles`.  Capacity
per cycle is ``sum(q_max of discharge steps) x capacity_factor`` -- identical
arithmetic on both sides -- so any metric difference is attributable purely to
which steps land in which cycle.

Only the local step sidecars are read (never the slow NAS source Parquet): they
already contain every column needed and are keyed to the source SHA-256, so
stale sidecars are skipped.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from . import derived_cycles, derived_steps

METHOD = "metric_validation_v1"
ZERO_AH = 1e-3            # a cycle with both capacities below this is "empty"
FORMATION_FRAC = 0.8      # cycle 1 is a formation outlier below this x median(next 3)
MIN_LABEL_DISTINCT = 3    # a dataset is "labelled" with >= this many distinct cycle labels
RETENTION_WINDOWS = (50, 100)

# Metrics compared per dataset and how their relative difference is scaled.
METRIC_KEYS = ["n_full_cycles", "cap_initial_ah", "retention_50_pct",
               "retention_100_pct", "ce_median_pct"]


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable without any real project)
# --------------------------------------------------------------------------- #
def step_roles(steps: pd.DataFrame) -> np.ndarray:
    """File-global step-role array (full_chg/full_dchg/pulse_*/rest/cv).

    Reuses :func:`derived_cycles._roles`; the roles depend only on the file's
    steps, not on any cycle assignment, so the same array serves both series.
    """
    roles, _ = derived_cycles._roles(
        steps["kind"].to_numpy(), steps["q_max"], steps["duration_s"],
        steps["v_start"], steps["v_min"], steps["v_max"])
    return np.asarray(roles, dtype=object)


def per_cycle_series(steps: pd.DataFrame, cycle_ids, roles,
                     capacity_factor: float, voltage_factor: float) -> pd.DataFrame:
    """One row per cycle id with capacities (Ah), CE, full flag, mean dchg V.

    ``cycle_ids`` is a per-step array (recorded ``label_cycle`` or derived
    ``cycle_derived``).  Steps whose cycle id is NA are ignored.
    """
    kind = steps["kind"].to_numpy()
    q = steps["q_max"].to_numpy(dtype=float)
    vmid = steps["v_mid"].to_numpy(dtype=float)
    roles = np.asarray(roles, dtype=object)
    cyc = pd.Series(cycle_ids)
    valid = cyc.notna().to_numpy()
    rows = []
    frame = pd.DataFrame({"cycle": pd.to_numeric(cyc[valid], errors="coerce").astype("int64"),
                          "kind": kind[valid], "q": q[valid],
                          "vmid": vmid[valid], "role": roles[valid]})
    for cid, g in frame.groupby("cycle", sort=True):
        k = g["kind"].to_numpy()
        qg = g["q"].to_numpy(dtype=float)
        rset = set(g["role"])
        chg = k == "chg"
        dchg = k == "dchg"
        ccap = float(np.nansum(qg[chg])) * capacity_factor
        dcap = float(np.nansum(qg[dchg])) * capacity_factor
        full_d = g["role"].to_numpy() == "full_dchg"
        if full_d.any():
            mean_dv = float(np.nanmean(g["vmid"].to_numpy(dtype=float)[full_d]))
        elif dchg.any():
            mean_dv = float(np.nanmean(g["vmid"].to_numpy(dtype=float)[dchg]))
        else:
            mean_dv = np.nan
        is_full = ("full_chg" in rset) and ("full_dchg" in rset) and not (
            rset & {"pulse_chg", "pulse_dchg"})
        rows.append({
            "cycle": int(cid),
            "charge_capacity_ah": ccap,
            "discharge_capacity_ah": dcap,
            "coulombic_efficiency": (dcap / ccap) if ccap > 0 else np.nan,
            "mean_discharge_voltage_v": mean_dv * voltage_factor if np.isfinite(mean_dv) else np.nan,
            "is_full": bool(is_full),
        })
    return pd.DataFrame(rows, columns=["cycle", "charge_capacity_ah", "discharge_capacity_ah",
                                       "coulombic_efficiency", "mean_discharge_voltage_v", "is_full"])


def dataset_level_metrics(series: pd.DataFrame) -> dict:
    """Comparable per-dataset metrics restricted to full cycles.

    Mirrors :mod:`battery_insight.run_metrics`: drop empty cycles, index the
    remainder 1..n, then on the full subset skip a formation cycle 1, take the
    nominal (initial) discharge capacity as the median of the next <=3 full
    cycles, compute retention at cycle 50/100 and the median CE after cycle 3.
    """
    out = {k: None for k in METRIC_KEYS}
    if series.empty:
        out["n_full_cycles"] = 0
        return out
    df = series.sort_values("cycle").reset_index(drop=True)
    empty = (df["charge_capacity_ah"].fillna(0).abs() < ZERO_AH) & \
            (df["discharge_capacity_ah"].fillna(0).abs() < ZERO_AH)
    df = df[~empty].reset_index(drop=True)
    df["cycle_index"] = np.arange(1, len(df) + 1)
    full = df[df["is_full"]].reset_index(drop=True)
    n_full = len(full)
    out["n_full_cycles"] = n_full
    if n_full < 3:
        return out

    dchg = full["discharge_capacity_ah"].to_numpy(dtype=float)
    cidx = full["cycle_index"].to_numpy()
    ce = full["coulombic_efficiency"].to_numpy(dtype=float)

    start = 0
    if n_full >= 4 and np.isfinite(dchg[0]) and dchg[0] < FORMATION_FRAC * np.nanmedian(dchg[1:4]):
        start = 1
    nominal = float(np.nanmedian(dchg[start:start + 3]))
    out["cap_initial_ah"] = nominal if np.isfinite(nominal) else None

    if nominal and nominal > 0:
        for n in RETENTION_WINDOWS:
            mask = cidx <= n
            if mask.any() and cidx.max() >= n:
                cap = float(dchg[mask][-1])
                out[f"retention_{n}_pct"] = 100.0 * cap / nominal

    ce_tail = ce[3:]
    ce_tail = ce_tail[np.isfinite(ce_tail)]
    if ce_tail.size:
        out["ce_median_pct"] = float(np.nanmedian(ce_tail)) * 100.0
    return out


def compare_steps(steps: pd.DataFrame, capacity_factor: float,
                  voltage_factor: float) -> dict | None:
    """Compute recorded-vs-derived metrics for one dataset's step table.

    Returns ``None`` when the dataset is not labelled (fewer than
    :data:`MIN_LABEL_DISTINCT` distinct recorded cycle labels).
    """
    if "label_cycle" not in steps.columns:
        return None
    labels = pd.to_numeric(steps["label_cycle"], errors="coerce")
    if labels.dropna().nunique() < MIN_LABEL_DISTINCT:
        return None
    roles = step_roles(steps)
    enriched = derived_cycles.assign_cycles(steps)

    rec = per_cycle_series(steps, labels.to_numpy(), roles, capacity_factor, voltage_factor)
    der = per_cycle_series(steps, enriched["cycle_derived"].to_numpy(), roles,
                           capacity_factor, voltage_factor)
    rec_m = dataset_level_metrics(rec)
    der_m = dataset_level_metrics(der)

    has_pulses = bool(np.isin(roles, ("pulse_chg", "pulse_dchg")).any())
    result = {
        "n_label_cycles": int(labels.dropna().nunique()),
        "n_derived_cycles": int(enriched["cycle_derived"].max()) if len(enriched) else 0,
        "has_pulses": has_pulses,
    }
    for k in METRIC_KEYS:
        rv, dv = rec_m[k], der_m[k]
        result[f"{k}_recorded"] = rv
        result[f"{k}_derived"] = dv
        result[f"{k}_abs_diff"] = _abs_diff(rv, dv)
        result[f"{k}_rel_diff"] = _rel_diff(rv, dv)
    result["reason"] = _reason(result)
    result["worst_rel_diff"] = _max_rel(result)
    return result


def _abs_diff(a, b):
    if a is None or b is None or not np.isfinite(a) or not np.isfinite(b):
        return None
    return abs(float(a) - float(b))


def _rel_diff(a, b):
    if a is None or b is None or not np.isfinite(a) or not np.isfinite(b):
        return None
    denom = abs(float(a))
    if denom == 0:
        return 0.0 if float(b) == 0 else None
    return abs(float(a) - float(b)) / denom


def _max_rel(result):
    vals = [result.get(f"{k}_rel_diff") for k in METRIC_KEYS]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def _reason(result):
    """Heuristic explanation for a disagreement (for the worst-offenders list)."""
    worst = _max_rel(result)
    if worst is None or worst < 0.02:
        return "agree"
    if result["n_derived_cycles"] != result["n_label_cycles"]:
        if result["has_pulses"]:
            return "pulse_protocol_cycle_count_differs"
        return "cycle_count_differs"
    if result["has_pulses"]:
        return "pulse_protocol"
    nf_r = result.get("n_full_cycles_recorded")
    nf_d = result.get("n_full_cycles_derived")
    if nf_r is not None and nf_d is not None and nf_r != nf_d:
        return "full_cycle_count_differs"
    return "metric_drift"


# --------------------------------------------------------------------------- #
# Per-dataset worker (reads one local step sidecar)
# --------------------------------------------------------------------------- #
def _process_one(job):
    dataset_id, step_path, factors = job
    try:
        vf, cf = factors  # unit_factors() order is (voltage, capacity)
        if cf is None or vf is None:
            return {"dataset_id": dataset_id, "status": "units_unconfirmed"}
        steps = pd.read_parquet(step_path)
        if len(steps) == 0:
            return {"dataset_id": dataset_id, "status": "empty"}
        res = compare_steps(steps, cf, vf)
        if res is None:
            return {"dataset_id": dataset_id, "status": "unlabelled"}
        res["dataset_id"] = dataset_id
        res["status"] = "ok"
        return res
    except Exception as error:  # keep the batch going
        return {"dataset_id": dataset_id, "status": "failed", "error": str(error)}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def validate_metrics(project, *, limit=None, workers=8) -> dict:
    """Validate derived vs recorded metrics for every labelled native_cyc dataset.

    Writes ``db/metric_validation.csv`` (one row per compared dataset) and
    ``db/metric_validation.json`` (summary), and returns the summary dict.
    """
    from .library import library_rows
    project = Path(project).resolve()
    rows = [r for r in library_rows(project) if r.get("format") == "native_cyc"]
    if limit:
        rows = rows[:limit]

    step_index = derived_steps.SidecarIndex(project / "derived" / "steps")
    jobs, skipped = [], {}
    for row in rows:
        step_path = derived_steps.sidecar_path(project, row["dataset_id"])
        meta = step_index.meta(step_path)
        if meta is None:
            continue
        if not derived_steps.is_current(step_path, row["sha256"], step_index):
            skipped["step_sidecar_stale"] = skipped.get("step_sidecar_stale", 0) + 1
        elif meta.get("_label_nulls") == meta.get("_rows"):  # no recorded labels: skip without reading
            skipped["unlabelled"] = skipped.get("unlabelled", 0) + 1
        else:
            jobs.append((row["dataset_id"], str(step_path), derived_cycles.unit_factors(row)))
    step_index.save()
    n_candidates = len(jobs) + sum(skipped.values())

    records = []
    if jobs:
        with ProcessPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(_process_one, job) for job in jobs]
            for future in as_completed(futures):
                res = future.result()
                if res.get("status") == "ok":
                    records.append(res)
                else:
                    skipped[res["status"]] = skipped.get(res["status"], 0) + 1

    frame = pd.DataFrame(records)
    summary = _summarise(frame, skipped, n_candidates=n_candidates)

    db = project / "db"
    db.mkdir(parents=True, exist_ok=True)
    _write_csv(frame, db / "metric_validation.csv")
    (db / "metric_validation.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


CSV_COLUMNS = (["dataset_id", "status", "n_label_cycles", "n_derived_cycles", "has_pulses"]
               + [f"{k}_{s}" for k in METRIC_KEYS
                  for s in ("recorded", "derived", "abs_diff", "rel_diff")]
               + ["worst_rel_diff", "reason"])


def _write_csv(frame: pd.DataFrame, path: Path):
    if frame.empty:
        pd.DataFrame(columns=CSV_COLUMNS).to_csv(path, index=False, encoding="utf-8-sig")
        return
    out = frame.copy()
    for col in CSV_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA
    out = out[CSV_COLUMNS].sort_values("worst_rel_diff", ascending=False, na_position="last")
    out.to_csv(path, index=False, encoding="utf-8-sig")


def _pct(values, q):
    arr = np.asarray([v for v in values if v is not None and np.isfinite(v)], dtype=float)
    return float(np.percentile(arr, q)) if arr.size else None


def _summarise(frame: pd.DataFrame, skipped: dict, n_candidates: int) -> dict:
    per_metric = {}
    n = len(frame)
    for k in METRIC_KEYS:
        rel_col = f"{k}_rel_diff"
        rels = frame[rel_col].tolist() if rel_col in frame.columns else []
        finite = [v for v in rels if v is not None and np.isfinite(v)]
        per_metric[k] = {
            "n_compared": len(finite),
            "median_abs_rel_diff": _pct(finite, 50),
            "p90_abs_rel_diff": _pct(finite, 90),
            "share_within_2pct": (float(np.mean([v <= 0.02 for v in finite])) if finite else None),
            "share_within_5pct": (float(np.mean([v <= 0.05 for v in finite])) if finite else None),
        }

    worst = []
    if n:
        ranked = frame.sort_values("worst_rel_diff", ascending=False, na_position="last").head(10)
        for _, r in ranked.iterrows():
            if r.get("worst_rel_diff") is None or not np.isfinite(r.get("worst_rel_diff", np.nan)):
                continue
            worst.append({
                "dataset_id": r["dataset_id"],
                "worst_rel_diff": round(float(r["worst_rel_diff"]), 4),
                "reason": r.get("reason"),
                "n_label_cycles": int(r["n_label_cycles"]),
                "n_derived_cycles": int(r["n_derived_cycles"]),
                "has_pulses": bool(r["has_pulses"]),
            })

    reason_counts = (frame["reason"].value_counts().to_dict() if "reason" in frame.columns else {})
    return {
        "method": METHOD,
        "n_candidates": n_candidates,
        "n_datasets_compared": n,
        "skipped": skipped,
        "per_metric": per_metric,
        "reason_counts": reason_counts,
        "worst_datasets": worst,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate derived-cycle metrics against recorded PNE cycle labels (G9).")
    parser.add_argument("--project", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    summary = validate_metrics(args.project, limit=args.limit, workers=args.workers)
    print(json.dumps({"n_datasets_compared": summary["n_datasets_compared"],
                      "skipped": summary["skipped"],
                      "per_metric": summary["per_metric"]},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
