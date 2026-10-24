"""Derived step tables for native PNE CYC Parquet files.

The original ``cycle``/``step`` columns are never modified. Steps are
reconstructed from ``step_time_s`` resets and summarised one row per step in a
sidecar Parquet file (``<project>/derived/steps/<dataset_id>.parquet``) that
records the source SHA-256 and the method version, so stale tables are
detectable and the method can be revised without touching source data.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd

STEP_METHOD = "step_time_reset_v1"
VOLTAGE_SENTINEL = -1.0e5
STEP_COLUMNS = ["step_derived", "start_row", "n_points", "elapsed_start_s", "duration_s",
                "v_start", "v_q1", "v_mid", "v_q3", "v_end", "v_min", "v_max",
                "q_mid", "q_end", "q_max", "kind", "label_cycle", "label_cycle_end", "label_step"]


def step_table(step_time, voltage, capacity, cycle=None, step=None):
    """Return one row per reconstructed step (arrays are per-record, in record order)."""
    step_time = np.asarray(step_time, dtype=float)
    n = len(step_time)
    if n == 0:
        return pd.DataFrame(columns=STEP_COLUMNS)
    voltage = np.asarray(voltage, dtype=float)
    capacity = np.asarray(capacity, dtype=float)
    starts = np.flatnonzero(np.r_[True, np.diff(step_time) < 0])
    ends = np.r_[starts[1:], n]
    last = ends - 1
    pick = lambda frac: starts + ((last - starts) * frac).astype(int)
    bad = ~np.isfinite(voltage) | (voltage < VOLTAGE_SENTINEL)
    vmin = np.minimum.reduceat(np.where(bad, np.inf, voltage), starts)
    vmax = np.maximum.reduceat(np.where(bad, -np.inf, voltage), starts)
    vclean = np.where(bad, np.nan, voltage)
    qabs = np.abs(np.nan_to_num(capacity))
    duration = np.maximum.reduceat(np.nan_to_num(step_time), starts)
    frame = pd.DataFrame({
        "step_derived": np.arange(1, len(starts) + 1, dtype=np.int32),
        "start_row": starts.astype(np.int64),
        "n_points": (ends - starts).astype(np.int64),
        "elapsed_start_s": np.r_[0.0, np.cumsum(duration)[:-1]],
        "duration_s": duration,
        "v_start": vclean[starts], "v_q1": vclean[pick(.25)], "v_mid": vclean[pick(.5)],
        "v_q3": vclean[pick(.75)], "v_end": vclean[last],
        "v_min": np.where(np.isfinite(vmin), vmin, np.nan), "v_max": np.where(np.isfinite(vmax), vmax, np.nan),
        "q_mid": capacity[pick(.5)], "q_end": capacity[last], "q_max": np.maximum.reduceat(qabs, starts),
    })
    threshold = 1.0 + 1e-6 * max(1.0, float(frame.q_max.max()))
    rising = frame.v_end.fillna(frame.v_max) >= frame.v_start.fillna(frame.v_min)
    frame["kind"] = np.where(frame.q_max < threshold, "rest", np.where(rising, "chg", "dchg"))
    for name, values, index in (("label_cycle", cycle, starts), ("label_cycle_end", cycle, last), ("label_step", step, starts)):
        if values is None:
            frame[name] = pd.array([pd.NA] * len(frame), dtype="Int64")
        else:
            frame[name] = pd.array(pd.Series(values).iloc[index].to_numpy(), dtype="Int64")
    return frame[STEP_COLUMNS]


def sidecar_path(project, dataset_id):
    return Path(project) / "derived" / "steps" / f"{dataset_id}.parquet"


def _build_one(path, out, dataset_id, sha256):
    import pyarrow as pa
    import pyarrow.parquet as pq
    file = pq.ParquetFile(path)
    names = file.schema_arrow.names
    if not {"step_time_s", "voltage_native", "capacity_native"} <= set(names):
        return {"dataset_id": dataset_id, "status": "skipped_not_native"}
    cols = [c for c in ("step_time_s", "voltage_native", "capacity_native", "cycle", "step") if c in names]
    table = file.read(columns=cols)
    get = lambda c: table.column(c).to_numpy(zero_copy_only=False) if c in names else None
    cyc = table.column("cycle").to_pandas() if "cycle" in names else None
    stp = table.column("step").to_pandas() if "step" in names else None
    frame = step_table(get("step_time_s"), get("voltage_native"), get("capacity_native"), cyc, stp)
    arrow = pa.Table.from_pandas(frame, preserve_index=False)
    arrow = arrow.replace_schema_metadata({"dataset_id": dataset_id, "source_sha256": sha256, "method": STEP_METHOD,
                                           "source_rows": str(table.num_rows)})
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(".tmp")
    pq.write_table(arrow, temporary, compression="zstd")
    temporary.replace(out)
    kinds = frame.kind.value_counts().to_dict()
    return {"dataset_id": dataset_id, "status": "built", "steps": len(frame), "rows": table.num_rows, **{f"n_{k}": int(v) for k, v in kinds.items()}}


def is_current(out, sha256):
    import pyarrow.parquet as pq
    if not out.exists():
        return False
    try:
        meta = pq.read_schema(out).metadata or {}
    except Exception:
        return False
    return meta.get(b"source_sha256", b"").decode() == sha256 and meta.get(b"method", b"").decode() == STEP_METHOD


def build_step_tables(project, *, workers=4, limit=None, progress=print):
    from .library import library_rows
    project = Path(project).resolve()
    rows = [r for r in library_rows(project) if r.get("format") == "native_cyc"]
    if limit:
        rows = rows[:limit]
    todo, results = [], []
    for row in rows:
        out = sidecar_path(project, row["dataset_id"])
        if is_current(out, row["sha256"]):
            results.append({"dataset_id": row["dataset_id"], "status": "current"})
        else:
            todo.append((row["path"], out, row["dataset_id"], row["sha256"]))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_build_one, *job): job for job in todo}
        for i, future in enumerate(as_completed(futures), 1):
            try:
                results.append(future.result())
            except Exception as error:  # keep going; report per file
                results.append({"dataset_id": futures[future][2], "status": "failed", "error": str(error)})
            if progress and (i % 100 == 0 or i == len(todo)):
                progress(f"steps {i}/{len(todo)}")
    manifest = pd.DataFrame(results)
    manifest.to_csv(project / "derived" / "steps_manifest.csv", index=False, encoding="utf-8-sig")
    return manifest


def load_all_steps(project):
    """Concatenate every current sidecar into one frame with a dataset_id column."""
    frames = []
    for path in sorted((Path(project) / "derived" / "steps").glob("*.parquet")):
        frame = pd.read_parquet(path)
        frame.insert(0, "dataset_id", path.stem)
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["dataset_id"] + STEP_COLUMNS)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build derived step tables (sidecar Parquet, source untouched).")
    parser.add_argument("--project", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args(argv)
    manifest = build_step_tables(args.project, workers=args.workers, limit=args.limit)
    print(json.dumps(manifest.status.value_counts().to_dict(), ensure_ascii=False))


if __name__ == "__main__":
    main()
