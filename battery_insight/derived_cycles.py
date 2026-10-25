"""Derived cycle tables for native PNE CYC files that lack PNE cycle labels.

The original ``cycle`` column (PNE CTS TotalCycle, often NULL) is never read or
overwritten here.  Cycles are *reconstructed* from the derived step sidecars
produced by :mod:`battery_insight.derived_steps` and written to a separate set
of columns (``cycle_derived`` / ``discharge_index`` / ``step_role``) in their
own sidecar Parquet files, so the reconstruction can be revised without
touching source data or the step tables.

Method (``cycle_time_role_v1``), deterministic and explainable:

* A reference capacity is the 80th percentile of ``q_max`` over the file's
  non-rest steps.  A charge/discharge step is "full" when its ``q_max`` is at
  least ``CAP_FRAC`` of that reference *and* it lasts at least ``MIN_DUR_S``
  seconds; otherwise it is a "pulse" (HPPC/DCIR probe, formation blip, ...).
* A new derived cycle opens at a ``chg`` step when a *full* discharge has
  occurred since the current cycle opened, or at the first charge that follows
  any lead-in step (rest/discharge) -- this mirrors PNE counting an initial
  OCV/rest as cycle 1 before cycling begins.  It also opens at a *second*
  consecutive ``full_dchg`` with no charge between (back-to-back capacity
  checks, common in DCIR/RPT), which PNE's TotalCycle likewise increments.
* Pulses never open a cycle and never arm the "a discharge happened" flag, so
  HPPC/DCIR pulse trains stay inside one cycle.

PNE's TotalCycle counts *schedule loops*, so pulse-loop protocols (HPPC, DCIR)
can never agree exactly; this is measured and reported per protocol by
``--validate`` rather than hidden.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from . import derived_steps

CYCLE_METHOD = "cycle_time_role_v1"

# Tuned on the ~950 fully-labelled files (boundary F1, see --validate).
CAP_FRAC = 0.3          # share of the file's reference capacity to count as a "full" step
MIN_DUR_S = 60.0        # a full step must last at least this long
CV_TOL_MV = 10.0        # charge within this of the file max voltage, barely rising, is a CV hold

STEP_OUT_COLUMNS = ["step_derived", "start_row", "n_points", "cycle_derived",
                    "discharge_index", "step_role"]
SUMMARY_COLUMNS = ["cycle_derived", "start_step", "n_steps", "start_elapsed_s",
                   "charge_capacity_ah", "discharge_capacity_ah", "coulombic_efficiency",
                   "charge_duration_s", "discharge_duration_s", "mean_discharge_voltage_v",
                   "max_voltage_v", "min_voltage_v", "has_pulses", "is_full"]
# Bump when SUMMARY_COLUMNS change so reused summary rows are recomputed.
SUMMARY_VERSION = "2"
VOLTAGE_FACTORS = {"V": 1., "mV": 1e-3}
CAPACITY_FACTORS = {"Ah": 1., "mAh": 1e-3, "uAh": 1e-6}


def unit_factors(row):
    """(voltage, capacity) factors to V/Ah from confirmed library metadata, else (None, None)."""
    metadata = (row.get("metadata") or {}) if row.get("metadata_current", True) else {}
    return VOLTAGE_FACTORS.get(metadata.get("voltage_unit")), CAPACITY_FACTORS.get(metadata.get("capacity_unit"))


# --------------------------------------------------------------------------- #
# Core reconstruction
# --------------------------------------------------------------------------- #
def _roles(kind, q_max, duration, v_start, v_min, v_max):
    """Return (step_role array, full boolean array) for one file's steps."""
    kind = np.asarray(kind)
    q = np.asarray(q_max, dtype=float)
    dur = np.asarray(duration, dtype=float)
    v_start = np.asarray(v_start, dtype=float)
    v_min = np.asarray(v_min, dtype=float)
    v_max = np.asarray(v_max, dtype=float)
    is_rest = kind == "rest"
    nonrest = q[~is_rest]
    cap = float(np.percentile(nonrest, 80)) if nonrest.size else 0.0
    full = (q >= CAP_FRAC * cap) & (dur >= MIN_DUR_S)
    file_vmax = np.nanmax(v_max) if np.isfinite(v_max).any() else np.nan
    span = np.where(np.isfinite(v_max) & np.isfinite(v_min), v_max - v_min, np.inf)
    is_cv = (kind == "chg") & np.isfinite(v_start) & (v_start >= file_vmax - CV_TOL_MV) & (span <= CV_TOL_MV)
    roles = np.empty(len(kind), dtype=object)
    for i in range(len(kind)):
        if is_rest[i]:
            roles[i] = "rest"
        elif is_cv[i]:
            roles[i] = "cv"
        elif kind[i] == "chg":
            roles[i] = "full_chg" if full[i] else "pulse_chg"
        else:  # dchg
            roles[i] = "full_dchg" if full[i] else "pulse_dchg"
    return roles, full


def assign_cycles(steps: pd.DataFrame) -> pd.DataFrame:
    """Add ``cycle_derived`` (Int, 1-based), ``discharge_index`` and ``step_role``.

    ``steps`` is one file's step table (columns of
    :data:`battery_insight.derived_steps.STEP_COLUMNS`).  The input ``cycle``
    (never present here) and ``label_cycle`` columns are left untouched.
    """
    steps = steps.reset_index(drop=True).copy()
    n = len(steps)
    if n == 0:
        steps["cycle_derived"] = pd.array([], dtype="Int64")
        steps["discharge_index"] = pd.array([], dtype="Int64")
        steps["step_role"] = pd.array([], dtype="object")
        return steps
    kind = steps["kind"].to_numpy()
    roles, _ = _roles(kind, steps["q_max"], steps["duration_s"],
                      steps["v_start"], steps["v_min"], steps["v_max"])

    cycle = np.ones(n, dtype=np.int64)
    disch = np.array([pd.NA] * n, dtype=object)
    current = 1
    seen_full_dchg = False
    seen_chg = False
    dcount = 0
    for i in range(n):
        open_new = False
        if i > 0 and kind[i] == "chg" and (seen_full_dchg or not seen_chg):
            open_new = True
        elif i > 0 and roles[i] == "full_dchg" and seen_full_dchg:
            # a second full discharge with no charge between (DCIR/RPT capacity
            # checks) -- PNE's TotalCycle increments here, so we do too.
            open_new = True
        if open_new:
            current += 1
            seen_full_dchg = False
        cycle[i] = current
        if kind[i] == "chg":
            seen_chg = True
        if roles[i] == "full_dchg":
            seen_full_dchg = True
            dcount += 1
            disch[i] = dcount
    steps["cycle_derived"] = pd.array(cycle, dtype="Int64")
    steps["discharge_index"] = pd.array(disch, dtype="Int64")
    steps["step_role"] = roles
    return steps


def cycle_summary(steps_with_cycles: pd.DataFrame, voltage_factor=0.001,
                  capacity_factor=1e-6) -> pd.DataFrame:
    """One row per ``cycle_derived``; capacities in Ah, voltages in V."""
    df = steps_with_cycles
    if len(df) == 0:
        return pd.DataFrame(columns=SUMMARY_COLUMNS)
    rows = []
    for cyc, g in df.groupby("cycle_derived", sort=True):
        kind = g["kind"].to_numpy()
        role = g["step_role"].to_numpy()
        chg = kind == "chg"
        dchg = kind == "dchg"
        q = g["q_max"].to_numpy(dtype=float)
        dur = g["duration_s"].to_numpy(dtype=float)
        ccap = float(np.nansum(q[chg])) * capacity_factor
        dcap = float(np.nansum(q[dchg])) * capacity_factor
        full_d = role == "full_dchg"
        vmid = g["v_mid"].to_numpy(dtype=float)
        if full_d.any():
            mean_dv = float(np.nanmean(vmid[full_d]))
        elif dchg.any():
            mean_dv = float(np.nanmean(vmid[dchg]))
        else:
            mean_dv = np.nan
        vmax = g["v_max"].to_numpy(dtype=float)
        vmin = g["v_min"].to_numpy(dtype=float)
        has_pulses = bool(np.isin(role, ("pulse_chg", "pulse_dchg")).any())
        rows.append({
            "cycle_derived": int(cyc),
            "start_step": int(g["step_derived"].iloc[0]),
            "n_steps": int(len(g)),
            "start_elapsed_s": float(g["elapsed_start_s"].iloc[0]),
            "charge_capacity_ah": ccap,
            "discharge_capacity_ah": dcap,
            "coulombic_efficiency": (dcap / ccap) if ccap > 0 else np.nan,
            "charge_duration_s": float(np.nansum(dur[chg])),
            "discharge_duration_s": float(np.nansum(dur[dchg])),
            "mean_discharge_voltage_v": mean_dv * voltage_factor if np.isfinite(mean_dv) else np.nan,
            "max_voltage_v": (float(np.nanmax(vmax)) * voltage_factor) if np.isfinite(vmax).any() else np.nan,
            "min_voltage_v": (float(np.nanmin(vmin)) * voltage_factor) if np.isfinite(vmin).any() else np.nan,
            "has_pulses": has_pulses,
            "is_full": bool(full_d.any() and (role == "full_chg").any() and not has_pulses),
        })
    return pd.DataFrame(rows, columns=SUMMARY_COLUMNS)


# --------------------------------------------------------------------------- #
# Naive baseline (for validation reporting only)
# --------------------------------------------------------------------------- #
def assign_cycles_naive(kind):
    """New cycle at the first charge after any discharge (the described baseline)."""
    kind = np.asarray(kind)
    n = len(kind)
    cycle = np.ones(n, dtype=np.int64)
    current = 1
    seen_dchg = False
    for i in range(n):
        if i > 0 and kind[i] == "chg" and seen_dchg:
            current += 1
            seen_dchg = False
        cycle[i] = current
        if kind[i] == "dchg":
            seen_dchg = True
    return cycle


# --------------------------------------------------------------------------- #
# Sidecar IO
# --------------------------------------------------------------------------- #
def cycles_sidecar_path(project, dataset_id):
    return Path(project) / "derived" / "cycles" / f"{dataset_id}.parquet"


def cycles_summary_path(project):
    """Single combined summary (one row per cycle, with a ``dataset_id`` column)."""
    return Path(project) / "derived" / "cycles_summary.parquet"


def is_current(out, sha256, index=None):
    meta = index.meta(out) if index is not None else (
        derived_steps.read_meta(out) if Path(out).exists() else None)
    return (bool(meta) and meta.get("source_sha256") == sha256
            and meta.get("cycle_method") == CYCLE_METHOD
            and meta.get("step_method") == derived_steps.STEP_METHOD)


def _build_one(step_path, out, dataset_id, sha256, write, factors=(1e-3, 1e-6), step_meta=None):
    """Compute cycles for one file; write the per-file sidecar when ``write``.

    Returns the per-cycle ``summary`` (with a ``dataset_id`` column) for the
    combined ``cycles_summary.parquet``; ``None`` when units are unconfirmed.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq
    meta = step_meta if step_meta is not None else (derived_steps.read_meta(step_path) or {})
    if meta.get("method") != derived_steps.STEP_METHOD or meta.get("source_sha256") != sha256:
        return {"dataset_id": dataset_id, "status": "step_sidecar_stale", "summary": None}
    steps = pd.read_parquet(step_path)
    enriched = assign_cycles(steps)
    summary = None
    if None not in factors:
        summary = cycle_summary(enriched, *factors)
        summary.insert(0, "dataset_id", dataset_id)

    status = "current"
    if write:
        out_frame = enriched[STEP_OUT_COLUMNS].copy()
        schema_meta = {"dataset_id": dataset_id, "source_sha256": sha256,
                       "step_method": derived_steps.STEP_METHOD, "cycle_method": CYCLE_METHOD}
        out.parent.mkdir(parents=True, exist_ok=True)
        arrow = pa.Table.from_pandas(out_frame, preserve_index=False)
        arrow = arrow.replace_schema_metadata(schema_meta)
        tmp = out.with_suffix(".tmp")
        pq.write_table(arrow, tmp, compression="zstd")
        tmp.replace(out)
        status = "built"
    return {"dataset_id": dataset_id, "status": status if summary is not None else status + "_units_unconfirmed",
            "steps": len(enriched), "cycles": len(summary) if summary is not None else 0, "summary": summary}


def _existing_summary(project):
    """Reusable rows of the combined summary keyed by dataset_id, with their identity keys."""
    import pyarrow.parquet as pq
    path = cycles_summary_path(project)
    if not path.exists():
        return {}, {}
    try:
        table = pq.read_table(path)
        meta = table.schema.metadata or {}
        if (meta.get(b"cycle_method", b"").decode() != CYCLE_METHOD
                or meta.get(b"step_method", b"").decode() != derived_steps.STEP_METHOD
                or meta.get(b"summary_version", b"").decode() != SUMMARY_VERSION):
            return {}, {}
        keys = json.loads(meta.get(b"dataset_keys", b"{}").decode())
        frame = table.to_pandas()
    except Exception:
        return {}, {}
    return {k: g for k, g in frame.groupby("dataset_id", sort=False)}, keys


def build_cycle_tables(project, *, workers=4, limit=None, progress=print):
    """Build per-file cycle sidecars and the combined summary incrementally.

    Summary rows are reused when the file's sha256 and unit factors match the
    previous build, so a no-change rebuild reads no step sidecars.
    """
    from .library import library_rows
    project = Path(project).resolve()
    rows = [r for r in library_rows(project) if r.get("format") == "native_cyc"]
    if limit:
        rows = rows[:limit]
    previous, previous_keys = _existing_summary(project)
    step_index = derived_steps.SidecarIndex(project / "derived" / "steps")
    cycle_index = derived_steps.SidecarIndex(project / "derived" / "cycles")
    jobs, results, summaries, keys = [], [], [], {}
    for row in rows:
        dataset_id = row["dataset_id"]
        out = cycles_sidecar_path(project, dataset_id)
        step_path = derived_steps.sidecar_path(project, dataset_id)
        factors = unit_factors(row)
        key = f"{row['sha256']}|{factors[0]}|{factors[1]}"
        if step_path.name not in step_index.listing:
            results.append({"dataset_id": dataset_id, "status": "no_step_sidecar"})
            continue
        write = not is_current(out, row["sha256"], cycle_index)
        if not write and previous_keys.get(dataset_id) == key and dataset_id in previous:
            summaries.append(previous[dataset_id])
            keys[dataset_id] = key
            results.append({"dataset_id": dataset_id, "status": "reused", "cycles": len(previous[dataset_id])})
            continue
        if not write and None in factors and previous_keys.get(dataset_id) == key:
            results.append({"dataset_id": dataset_id, "status": "current_units_unconfirmed"})
            keys[dataset_id] = key
            continue
        jobs.append((step_path, out, dataset_id, row["sha256"], write, factors, step_index.meta(step_path)))
        keys[dataset_id] = key
    step_index.save()
    cycle_index.save()

    if jobs:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_build_one, *job): job for job in jobs}
            for i, future in enumerate(as_completed(futures), 1):
                try:
                    res = future.result()
                    summary = res.pop("summary", None)
                    if summary is not None and len(summary):
                        summaries.append(summary)
                    results.append(res)
                except Exception as error:
                    keys.pop(futures[future][2], None)
                    results.append({"dataset_id": futures[future][2], "status": "failed", "error": str(error)})
                if progress and (i % 100 == 0 or i == len(jobs)):
                    progress(f"cycles {i}/{len(jobs)}")
        derived_steps.refresh_index(project / "derived" / "cycles", [job[1] for job in jobs if job[4]])

    (project / "derived").mkdir(parents=True, exist_ok=True)
    if jobs or set(keys) != set(previous_keys):
        import pyarrow as pa
        import pyarrow.parquet as pq
        combined = pd.concat(summaries, ignore_index=True) if summaries else pd.DataFrame(columns=["dataset_id"] + SUMMARY_COLUMNS)
        arrow = pa.Table.from_pandas(combined, preserve_index=False)
        arrow = arrow.replace_schema_metadata(
            {"step_method": derived_steps.STEP_METHOD, "cycle_method": CYCLE_METHOD,
             "summary_version": SUMMARY_VERSION, "dataset_keys": json.dumps(keys, sort_keys=True)})
        target = cycles_summary_path(project)
        tmp = target.with_suffix(".tmp")
        pq.write_table(arrow, tmp, compression="zstd")
        tmp.replace(target)

    manifest = pd.DataFrame(results)
    manifest.to_csv(project / "derived" / "cycles_manifest.csv", index=False, encoding="utf-8-sig")
    return manifest


# --------------------------------------------------------------------------- #
# Public contract for the app integration agent
# --------------------------------------------------------------------------- #
def load_row_cycles(project, dataset_id, sha256):
    """Return [start_row, n_points, cycle_derived] sorted by start_row, or None.

    ``None`` when the sidecar is missing, its ``source_sha256`` differs from
    ``sha256``, or the method metadata does not match the current methods.
    """
    import pyarrow.parquet as pq
    out = cycles_sidecar_path(project, dataset_id)
    if not out.exists():
        return None
    try:
        meta = pq.read_schema(out).metadata or {}
    except Exception:
        return None
    if (meta.get(b"source_sha256", b"").decode() != sha256
            or meta.get(b"cycle_method", b"").decode() != CYCLE_METHOD
            or meta.get(b"step_method", b"").decode() != derived_steps.STEP_METHOD):
        return None
    frame = pd.read_parquet(out, columns=["start_row", "n_points", "cycle_derived"])
    frame = frame.astype({"start_row": "int64", "n_points": "int64", "cycle_derived": "int64"})
    return frame.sort_values("start_row").reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _boundaries(values):
    arr = np.asarray(values, dtype=float)
    if len(arr) < 2:
        return set()
    return set((np.flatnonzero(np.diff(arr) != 0) + 1).tolist())


def _prf(tp, fp, fn):
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
    return round(prec, 4), round(rec, 4), round(f1, 4)


def validate(project, progress=print):
    """Compare cycle_derived vs label_cycle on fully-labelled files."""
    project = Path(project).resolve()
    step_dir = project / "derived" / "steps"
    files_csv = project / "dx" / "dx_files.csv"
    tests_csv = project / "dx" / "dx_tests.csv"
    type_by_file = {}
    if files_csv.exists() and tests_csv.exists():
        files = pd.read_csv(files_csv)
        tests = pd.read_csv(tests_csv)
        tt = dict(zip(tests["test_key"], tests.get("test_type")))
        for _, r in files.iterrows():
            value = tt.get(r.get("test_key"))
            if value is None or (isinstance(value, float) and pd.isna(value)) or value == "":
                value = "(empty)"
            type_by_file[r["dataset_id"]] = value

    totals = {"tp": 0, "fp": 0, "fn": 0, "exact": 0, "files": 0}
    base = {"tp": 0, "fp": 0, "fn": 0, "exact": 0}
    by_type = {}
    per_file = []
    paths = sorted(step_dir.glob("*.parquet"))
    for i, path in enumerate(paths, 1):
        steps = pd.read_parquet(path)
        if "label_cycle" not in steps or steps["label_cycle"].isna().any() or len(steps) == 0:
            continue
        enriched = assign_cycles(steps)
        lab = _boundaries(steps["label_cycle"].astype("int64"))
        der = _boundaries(enriched["cycle_derived"].astype("int64"))
        nav = _boundaries(assign_cycles_naive(steps["kind"].to_numpy()))
        tp, fp, fn = len(lab & der), len(der - lab), len(lab - der)
        exact = lab == der
        totals["tp"] += tp; totals["fp"] += fp; totals["fn"] += fn
        totals["exact"] += int(exact); totals["files"] += 1
        base["tp"] += len(lab & nav); base["fp"] += len(nav - lab); base["fn"] += len(lab - nav)
        base["exact"] += int(lab == nav)
        tt = type_by_file.get(path.stem, "(empty)")
        d = by_type.setdefault(tt, {"tp": 0, "fp": 0, "fn": 0, "exact": 0, "files": 0})
        d["tp"] += tp; d["fp"] += fp; d["fn"] += fn; d["exact"] += int(exact); d["files"] += 1
        per_file.append({"dataset_id": path.stem, "test_type": tt, "n_steps": len(steps),
                         "n_label_cycles": int(steps["label_cycle"].max()),
                         "n_derived_cycles": int(enriched["cycle_derived"].max()),
                         "boundary_tp": tp, "boundary_fp": fp, "boundary_fn": fn,
                         "exact_match": bool(exact)})
        if progress and (i % 200 == 0 or i == len(paths)):
            progress(f"validate {i}/{len(paths)}")

    prec, rec, f1 = _prf(totals["tp"], totals["fp"], totals["fn"])
    bprec, brec, bf1 = _prf(base["tp"], base["fp"], base["fn"])
    per_type_out = {}
    for tt, d in sorted(by_type.items()):
        p, r, f = _prf(d["tp"], d["fp"], d["fn"])
        per_type_out[tt] = {"files": d["files"], "precision": p, "recall": r, "f1": f,
                            "exact_files": d["exact"], "fp": d["fp"], "fn": d["fn"]}
    result = {
        "cycle_method": CYCLE_METHOD,
        "params": {"cap_frac": CAP_FRAC, "min_dur_s": MIN_DUR_S, "cv_tol_mv": CV_TOL_MV},
        "labelled_files": totals["files"],
        "final": {"precision": prec, "recall": rec, "f1": f1,
                  "exact_files": totals["exact"],
                  "exact_match_rate": round(totals["exact"] / totals["files"], 4) if totals["files"] else 0.0,
                  "boundary_tp": totals["tp"], "boundary_fp": totals["fp"], "boundary_fn": totals["fn"]},
        "naive_baseline": {"precision": bprec, "recall": brec, "f1": bf1,
                           "exact_files": base["exact"],
                           "exact_match_rate": round(base["exact"] / totals["files"], 4) if totals["files"] else 0.0,
                           "boundary_fp": base["fp"], "boundary_fn": base["fn"]},
        "by_test_type": per_type_out,
    }
    out_dir = project / "derived"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "cycles_validation.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    pd.DataFrame(per_file).to_csv(out_dir / "cycles_validation.csv", index=False, encoding="utf-8-sig")
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None):
    parser = argparse.ArgumentParser(description="Reconstruct cycles from derived step sidecars (source untouched).")
    parser.add_argument("--project", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--validate", action="store_true", help="validate against label_cycle and write cycles_validation.json")
    args = parser.parse_args(argv)
    if args.validate:
        result = validate(args.project)
        print(json.dumps({"final": result["final"], "naive_baseline": result["naive_baseline"]},
                         ensure_ascii=False, indent=2))
    else:
        manifest = build_cycle_tables(args.project, workers=args.workers, limit=args.limit)
        print(json.dumps(manifest.status.value_counts().to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    main()
