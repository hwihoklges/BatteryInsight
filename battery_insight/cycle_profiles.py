"""Compact analysis-profile store: resampled V-Q curves for selected cycles.

The lossless source Parquet files stay the archive of record.  This module
keeps one fixed-size voltage curve per (dataset, selected cycle, direction)
on a normalized capacity grid ``q/q_total in [0, 1]`` so that voltage-profile
comparisons (V-Q, dQ/dV) do not need the source files.

Output: ``db/cycle_profiles.parquet`` (zstd), one row per profile with columns
``dataset_id, cycle_derived, direction ('chg'|'dchg'), q_total_ah, duration_s,
v_start, v_end, v`` (``v`` = fixed-size list<float32>[points], volts).
Schema metadata carries ``dataset_keys`` (json ``{dataset_id: "sha|every|points|vf|qf"}``)
so unchanged datasets are reused on rebuild.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

from . import derived_cycles, derived_steps

PROFILE_METHOD = "cycle_profile_v2"
VOLTAGE_SENTINEL = derived_steps.VOLTAGE_SENTINEL
SPECIAL_HIGH = 1.5      # full_dchg duration > 1.5x dataset median -> special (slow RPT-like)
SPECIAL_LOW = 1.0 / 1.5  # < 0.67x median -> special (fast check)
EDGE_CYCLES = (1, 2, 3)
MIN_Q_AH = 1e-6


def profiles_path(project):
    return Path(project) / "db" / "cycle_profiles.parquet"


def select_cycles(cycle_numbers, roles_by_cycle, every=10):
    """Deterministically choose the cycles whose profiles are stored.

    ``roles_by_cycle`` maps cycle -> {step_role: duration_s} (the duration of
    that cycle's longest step with that role); a plain iterable of roles is
    accepted too (then no duration-based special cycles exist).  Kept cycles:

    * cycles 1, 2, 3 (those present);
    * every ``every``-th cycle (``cycle % every == 0``);
    * the first and the last *full* cycle (one with a full_chg/full_dchg step);
    * special cycles: ``full_dchg`` duration > 1.5x or < 1/1.5x of the
      dataset's median full_dchg duration (RPT / capacity-check cycles).

    Only cycles present in ``cycle_numbers`` are returned, sorted ascending.
    """
    cycles = sorted({int(c) for c in cycle_numbers if int(c) >= 1})
    present = set(cycles)
    keep = {c for c in cycles if c in EDGE_CYCLES}
    if every and every > 0:
        keep |= {c for c in cycles if c % every == 0}
    full, dchg_dur = [], {}
    for c in cycles:
        roles = roles_by_cycle.get(c, ())
        if isinstance(roles, dict):
            if roles.get("full_chg") is not None or roles.get("full_dchg") is not None:
                full.append(c)
            d = roles.get("full_dchg")
            if d is not None and np.isfinite(d):
                dchg_dur[c] = float(d)
        elif {"full_chg", "full_dchg"} & set(roles):
            full.append(c)
    if full:
        keep |= {full[0], full[-1]}
    if dchg_dur:
        median = float(np.median(list(dchg_dur.values())))
        if median > 0:
            keep |= {c for c, d in dchg_dur.items() if d > SPECIAL_HIGH * median or d < SPECIAL_LOW * median}
    return sorted(keep & present)


def resample_curve(voltage_v, capacity_ah, points=128):
    """Resample voltage onto q/q_total grid; returns (v_grid, q_total) or None."""
    v = np.asarray(voltage_v, dtype=float)
    q = np.abs(np.asarray(capacity_ah, dtype=float))
    ok = np.isfinite(v) & np.isfinite(q) & (v > VOLTAGE_SENTINEL)
    v, q = v[ok], q[ok]
    if q.size < 2:
        return None
    q = np.maximum.accumulate(q)
    q_total = float(q[-1])
    if q_total < MIN_Q_AH:
        return None
    x, first = np.unique(q / q_total, return_index=True)
    if x.size < 2:
        return None
    grid = np.linspace(0.0, 1.0, points)
    # 0.1 mV rounding (source resolution is 1 mV) shrinks zstd output ~45%
    return np.round(np.interp(grid, x, v[first]), 4).astype(np.float32), q_total


def dqdv(q_total_ah, v_grid, bins=64):
    """dQ/dV (Ah/V) from a stored profile by binning on voltage.

    Each of the ``len(v_grid)-1`` grid segments carries ``q_total/(n-1)`` Ah and
    is assigned to the voltage bin of its mid-point.  Returns (v_centers, dqdv).
    """
    v = np.asarray(v_grid, dtype=float)
    dq = float(q_total_ah) / (len(v) - 1)
    mid = 0.5 * (v[1:] + v[:-1])
    lo, hi = float(np.min(v)), float(np.max(v))
    if hi <= lo:
        return np.array([lo]), np.array([np.nan])
    edges = np.linspace(lo, hi, bins + 1)
    counts, _ = np.histogram(mid, bins=edges)
    return 0.5 * (edges[1:] + edges[:-1]), counts * dq / np.diff(edges)


def _cycle_steps(step_path, cycle_path):
    steps = pd.read_parquet(step_path, columns=["step_derived", "duration_s", "kind", "q_max"])
    cycles = pd.read_parquet(cycle_path, columns=["step_derived", "start_row", "n_points", "cycle_derived", "step_role"])
    return cycles.merge(steps, on="step_derived", how="left")


def _roles_by_cycle(frame):
    out = {}
    for c, role, dur in zip(frame.cycle_derived.to_numpy(), frame.step_role.to_numpy(), frame.duration_s.to_numpy()):
        d = out.setdefault(int(c), {})
        d[role] = max(d.get(role, 0.0), float(dur))
    return out


def _pick_step(group, direction):
    full = group[group.step_role == f"full_{direction}"]
    if len(full):
        return full.loc[full.duration_s.idxmax()]
    rest = group[(group.kind == direction) & (group.step_role != "cv")]
    return rest.loc[rest.q_max.idxmax()] if len(rest) else None


def _profile_one(source_path, step_path, cycle_path, dataset_id, sha256, factors, every, points):
    import pyarrow.parquet as pq
    vf, qf = factors
    frame = _cycle_steps(step_path, cycle_path)
    if frame.empty:
        return {"dataset_id": dataset_id, "status": "built", "rows": []}
    chosen = select_cycles(frame.cycle_derived.unique(), _roles_by_cycle(frame), every)
    table = pq.ParquetFile(source_path).read(columns=["voltage_native", "capacity_native"])
    volt = table.column("voltage_native").to_numpy(zero_copy_only=False)
    cap = table.column("capacity_native").to_numpy(zero_copy_only=False)
    rows = []
    for cycle, group in frame[frame.cycle_derived.isin(chosen)].groupby("cycle_derived", sort=True):
        for direction in ("chg", "dchg"):
            step = _pick_step(group, direction)
            if step is None:
                continue
            a, b = int(step.start_row), int(step.start_row) + int(step.n_points)
            if b > len(volt):
                continue
            sv = volt[a:b]
            res = resample_curve(np.where(sv > VOLTAGE_SENTINEL, sv * vf, np.nan), cap[a:b] * qf, points)
            if res is None:
                continue
            grid, q_total = res
            rows.append({"dataset_id": dataset_id, "cycle_derived": int(cycle), "direction": direction,
                         "q_total_ah": q_total, "duration_s": float(step.duration_s),
                         "v_start": float(grid[0]), "v_end": float(grid[-1]), "v": grid})
    return {"dataset_id": dataset_id, "status": "built", "rows": rows}


def _to_arrow(frame, points, metadata):
    import pyarrow as pa
    n = len(frame)
    flat = (np.concatenate(list(frame["v"])) if n else np.empty(0, np.float32)).astype(np.float32)
    v = pa.FixedSizeListArray.from_arrays(pa.array(flat, pa.float32()), points)
    table = pa.table({
        "dataset_id": pa.array(frame["dataset_id"].astype(str), pa.string()),
        "cycle_derived": pa.array(frame["cycle_derived"].astype("int32"), pa.int32()),
        "direction": pa.array(frame["direction"].astype(str), pa.string()),
        "q_total_ah": pa.array(frame["q_total_ah"].astype("float32"), pa.float32()),
        "duration_s": pa.array(frame["duration_s"].astype("float32"), pa.float32()),
        "v_start": pa.array(frame["v_start"].astype("float32"), pa.float32()),
        "v_end": pa.array(frame["v_end"].astype("float32"), pa.float32()),
        "v": v})
    return table.replace_schema_metadata(metadata)


def _derived_methods():
    return f"{derived_steps.STEP_METHOD}|{derived_cycles.CYCLE_METHOD}"


def _existing(project, points):
    import pyarrow.parquet as pq
    path = profiles_path(project)
    if not path.exists():
        return {}, {}
    try:
        table = pq.read_table(path)
        meta = table.schema.metadata or {}
        if (meta.get(b"method", b"").decode() != PROFILE_METHOD
                or meta.get(b"derived_methods", b"").decode() != _derived_methods()):
            return {}, {}
        keys = json.loads(meta.get(b"dataset_keys", b"{}").decode())
        frame = table.to_pandas()
    except Exception:
        return {}, {}
    frame["v"] = list(np.stack(frame["v"].to_numpy()).astype(np.float32)) if len(frame) else []
    return {k: g for k, g in frame.groupby("dataset_id", sort=False)}, keys


def build_cycle_profiles(project, *, every=10, points=128, workers=8, progress=print):
    """Build/refresh ``db/cycle_profiles.parquet`` incrementally; returns a summary dict."""
    from .library import library_rows
    project = Path(project).resolve()
    rows = [r for r in library_rows(project) if r.get("format") == "native_cyc"]
    previous, previous_keys = _existing(project, points)
    step_index = derived_steps.SidecarIndex(project / "derived" / "steps")
    cycle_index = derived_steps.SidecarIndex(project / "derived" / "cycles")
    frames, keys, statuses, jobs = [], {}, {}, []
    for row in rows:
        did, sha = row["dataset_id"], row["sha256"]
        factors = derived_cycles.unit_factors(row)
        if None in factors:
            statuses[did] = "units_unconfirmed"
            continue
        key = f"{sha}|{every}|{points}|{factors[0]}|{factors[1]}"
        if previous_keys.get(did) == key:
            if did in previous:
                frames.append(previous[did])
            keys[did] = key
            statuses[did] = "reused"
            continue
        step_path = derived_steps.sidecar_path(project, did)
        cycle_path = derived_cycles.cycles_sidecar_path(project, did)
        if not (derived_steps.is_current(step_path, sha, step_index)
                and derived_cycles.is_current(cycle_path, sha, cycle_index)):
            statuses[did] = "sidecar_missing_or_stale"
            continue
        jobs.append((row["path"], str(step_path), str(cycle_path), did, sha, factors, every, points))
        keys[did] = key
    if jobs:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_profile_one, *job): job for job in jobs}
            for i, future in enumerate(as_completed(futures), 1):
                did = futures[future][3]
                try:
                    res = future.result()
                    statuses[did] = res["status"]
                    if res["rows"]:
                        frames.append(pd.DataFrame(res["rows"]))
                except Exception as error:
                    keys.pop(did, None)
                    statuses[did] = f"failed: {error}"
                if progress and (i % 100 == 0 or i == len(jobs)):
                    progress(f"profiles {i}/{len(jobs)}")
    path = profiles_path(project)
    if jobs or set(keys) != set(previous_keys) or not path.exists():
        import pyarrow.parquet as pq
        frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
            columns=["dataset_id", "cycle_derived", "direction", "q_total_ah", "duration_s", "v_start", "v_end", "v"])
        frame = frame.sort_values(["dataset_id", "cycle_derived", "direction"], kind="stable").reset_index(drop=True)
        meta = {"method": PROFILE_METHOD, "derived_methods": _derived_methods(), "points": str(points), "every": str(every),
                "dataset_keys": json.dumps(keys, sort_keys=True)}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        pq.write_table(_to_arrow(frame, points, meta), tmp, compression="zstd", compression_level=19,
                       row_group_size=20000)
        tmp.replace(path)
    n_profiles = int(sum(len(f) for f in frames))
    counts = {}
    for s in statuses.values():
        s = s.split(":")[0]
        counts[s] = counts.get(s, 0) + 1
    return {"n_datasets": len(keys), "n_profiles": n_profiles,
            "bytes": path.stat().st_size if path.exists() else 0, "statuses": counts}


def load_profiles(project, dataset_ids=None, cycles=None):
    """Read stored profiles as a DataFrame (``v`` column holds float32 arrays)."""
    import pyarrow.parquet as pq
    path = profiles_path(project)
    filters = []
    if dataset_ids is not None:
        filters.append(("dataset_id", "in", [str(d) for d in dataset_ids]))
    if cycles is not None:
        filters.append(("cycle_derived", "in", [int(c) for c in cycles]))
    frame = pq.read_table(path, filters=filters or None).to_pandas()
    frame["v"] = [np.asarray(x, dtype=np.float32) for x in frame["v"]]
    return frame


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the resampled cycle-profile store.")
    parser.add_argument("--project", required=True)
    parser.add_argument("--every", type=int, default=10)
    parser.add_argument("--points", type=int, default=128)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    start = time.time()
    result = build_cycle_profiles(args.project, every=args.every, points=args.points, workers=args.workers)
    result["seconds"] = round(time.time() - start, 1)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())