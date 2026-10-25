"""Module A: per-run cycling metrics (agent metrics).

Builds ``db\\run_profile.parquet``, ``db\\run_metrics.parquet`` and
``db\\run_cycles.parquet`` from the derived cycle summary plus the dx run
grouping. Vectorised pandas, no ML. See db_contract.md.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd

METHOD = "run_metrics_v1"
C_LADDER = np.array([0.05, 0.1, 0.2, 0.33, 0.5, 1.0, 2.0, 3.0])
CAP_WINDOWS = [50, 100, 200, 500]
ZERO_AH = 1e-3  # a cycle with both capacities below this is empty

METRIC_DEFS = [
    {"metric_id": "cap_initial_ah", "name_ko": "초기 방전용량", "unit": "Ah",
     "higher_is_better": True,
     "definition": "형성 사이클 제외 후 첫 ≤3 full 방전용량의 중앙값(nominal)."},
    {"metric_id": "cap_at", "name_ko": "지정 사이클 방전용량", "unit": "Ah",
     "higher_is_better": True,
     "definition": "window 사이클 N에서의 full 방전용량."},
    {"metric_id": "retention_pct", "name_ko": "용량 유지율", "unit": "%",
     "higher_is_better": True,
     "definition": "사이클 N 방전용량 / nominal × 100 (full 사이클 ≥ N+1 필요)."},
    {"metric_id": "cycles_to_80pct", "name_ko": "80% 도달 사이클", "unit": "cycles",
     "higher_is_better": True,
     "definition": "방전용량이 nominal의 80% 미만으로 처음 떨어진 full 사이클. 미도달 시 censored."},
    {"metric_id": "fade_pct_per_100", "name_ko": "100사이클당 열화율", "unit": "%",
     "higher_is_better": False,
     "definition": "full 사이클(≥10) 방전용량-사이클 선형회귀 기울기를 nominal로 정규화한 100사이클당 %감소."},
    {"metric_id": "ce_median_pct", "name_ko": "쿨롱효율 중앙값", "unit": "%",
     "higher_is_better": True,
     "definition": "처음 3 사이클 이후 full chg+dchg 쌍의 쿨롱효율 중앙값 × 100."},
    {"metric_id": "mean_dchg_v_initial", "name_ko": "초기 평균 방전전압", "unit": "V",
     "higher_is_better": None,
     "definition": "초기 full 사이클의 평균 방전전압(근사)."},
    {"metric_id": "dchg_v_drop_100", "name_ko": "100사이클 전압강하", "unit": "V",
     "higher_is_better": False,
     "definition": "초기 대비 100사이클 평균 방전전압 강하."},
    {"metric_id": "n_full_cycles", "name_ko": "full 사이클 수", "unit": "cycles",
     "higher_is_better": None,
     "definition": "full chg+dchg 로 구성된 사이클 수."},
]


def _ladder(value):
    if value is None or not np.isfinite(value) or value <= 0:
        return None
    return float(C_LADDER[int(np.argmin(np.abs(C_LADDER - value)))])


def _fmt_c(value):
    v = _ladder(value)
    return f"{v:g}C" if v is not None else "?C"


def _round05(value):
    if value is None or not np.isfinite(value):
        return None
    return round(round(value / 0.05) * 0.05, 2)


def _is_original(fragment):
    return bool(re.fullmatch(r"_org\d*", str(fragment).strip()))


def _frag_key(fragment):
    frag = str(fragment).strip()
    if frag in ("", "nan", "<NA>", "None"):
        return (0, 0, "")
    digits = re.findall(r"\d+", frag)
    return (1, int(digits[-1]) if digits else 0, frag)


def _run_assignments(project, datasets):
    """Map each dataset to (run_id, test_key, channel, fragment, physical_cell_id).

    Excluded datasets are dropped and returned separately.
    """
    dx_path = Path(project) / "dx" / "dx_files.csv"
    excluded = []
    assign = {}
    try:
        dx = pd.read_csv(dx_path, dtype=str) if dx_path.exists() else None
    except pd.errors.EmptyDataError:
        dx = None
    if dx is not None and "dataset_id" in dx.columns:
        dx = dx[dx["dataset_id"].isin(datasets)]
        for _, row in dx.iterrows():
            ds = row["dataset_id"]
            exclude = str(row.get("exclude", "")).strip().lower()
            if exclude not in ("", "nan", "0", "false", "no", "none"):
                excluded.append(ds)
                continue
            tk = str(row.get("test_key", "")).strip()
            ch = str(row.get("channel", "")).strip()
            run_id = f"{tk}:{ch}" if tk and ch else f"ds:{ds}"
            assign[ds] = {
                "run_id": run_id, "test_key": tk, "channel": ch,
                "fragment": row.get("fragment", ""),
                "physical_cell_id": row.get("physical_cell_id", ""),
            }
    for ds in datasets:
        if ds in assign or ds in excluded:
            continue
        assign[ds] = {"run_id": f"ds:{ds}", "test_key": "", "channel": "",
                      "fragment": "", "physical_cell_id": ""}
    return assign, excluded


def _stitch(run_rows):
    """Order a run's datasets and build continuous cycle_index frame.

    A combined (non ``_org``) fragment supersedes its original ``_org*``
    fragments; genuinely disjoint fragments are stitched in order.
    """
    summary_by_ds = {ds: g for ds, g in run_rows.groupby("dataset_id", sort=False)}
    frags = run_rows[["dataset_id", "fragment"]].drop_duplicates()
    primaries = frags[~frags["fragment"].map(_is_original)]
    used = primaries if len(primaries) else frags
    superseded = len(frags) - len(used)
    ordered = sorted(used["dataset_id"], key=lambda d: _frag_key(
        frags.loc[frags.dataset_id == d, "fragment"].iloc[0]))

    pieces, dropped = [], 0
    for ds in ordered:
        part = summary_by_ds.get(ds)
        if part is None or part.empty:
            continue
        part = part.sort_values("cycle_derived")
        empty = (part["charge_capacity_ah"].fillna(0).abs() < ZERO_AH) & \
                (part["discharge_capacity_ah"].fillna(0).abs() < ZERO_AH)
        dropped += int(empty.sum())
        pieces.append(part[~empty])
    if not pieces:
        return pd.DataFrame(), superseded, dropped
    frame = pd.concat(pieces, ignore_index=True)
    frame.insert(0, "cycle_index", np.arange(1, len(frame) + 1))
    return frame, superseded, dropped


def _slope(x, y):
    if len(x) < 2:
        return 0.0
    return float(np.polyfit(x, y, 1)[0])


def _build_one(run_id, run_rows):
    frame, superseded, dropped = _stitch(run_rows)
    reasons = []
    if superseded:
        reasons.append(f"superseded_originals:{superseded}")
    if dropped:
        reasons.append(f"zero_cycles_dropped:{dropped}")

    if frame.empty:
        profile = {"run_id": run_id, "n_cycles": 0, "n_full_cycles": 0,
                   "quality": "insufficient", "reasons": ";".join(reasons + ["no_cycles"])}
        return profile, pd.DataFrame(), pd.DataFrame()

    frame = frame.assign(is_full=frame["is_full"].fillna(False).astype(bool))

    run_cycles = pd.DataFrame({
        "run_id": run_id,
        "cycle_index": frame["cycle_index"].to_numpy(),
        "dataset_id": frame["dataset_id"].to_numpy(),
        "cycle_derived": frame["cycle_derived"].to_numpy(),
        "discharge_capacity_ah": frame["discharge_capacity_ah"].to_numpy(),
        "charge_capacity_ah": frame["charge_capacity_ah"].to_numpy(),
        "ce_pct": (frame["coulombic_efficiency"] * 100).to_numpy(),
        "mean_discharge_voltage_v": frame["mean_discharge_voltage_v"].to_numpy(),
        "is_full": frame["is_full"].to_numpy(),
    })

    full = frame[frame["is_full"]].reset_index(drop=True)
    n_cycles, n_full = len(frame), len(full)
    reasons.append(f"full={n_full}")

    row = run_rows.iloc[0]
    profile = {
        "run_id": run_id, "test_key": row["test_key"], "channel": row["channel"],
        "dataset_ids": ";".join(dict.fromkeys(frame["dataset_id"])),
        "n_datasets": frame["dataset_id"].nunique(),
        "n_cycles": n_cycles, "n_full_cycles": n_full,
        "nominal_capacity_ah": None, "chg_c_rate": None, "dchg_c_rate": None,
        "c_rate_class": None, "v_lo": None, "v_hi": None,
        "has_pulses": bool(frame["has_pulses"].any()),
        "protocol_signature": None, "quality": "insufficient", "reasons": None,
    }
    metrics = []

    def add(metric_id, value, *, window="", unit="", censored=False):
        if value is None or (isinstance(value, float) and not np.isfinite(value)):
            return
        metrics.append({"run_id": run_id, "metric_id": metric_id, "window": window,
                        "value": float(value), "unit": unit, "censored": bool(censored),
                        "quality": profile["quality"], "method": METHOD})

    if n_full < 3:
        profile["reasons"] = ";".join(reasons + ["insufficient_full_cycles"])
        add("n_full_cycles", n_full, unit="cycles")
        for m in metrics:
            m["quality"] = "insufficient"
        return profile, pd.DataFrame(metrics), run_cycles

    dchg = full["discharge_capacity_ah"].to_numpy()
    chg = full["charge_capacity_ah"].to_numpy()
    cidx = full["cycle_index"].to_numpy()
    ce = full["coulombic_efficiency"].to_numpy()
    mv = full["mean_discharge_voltage_v"].to_numpy()

    # formation skip
    start = 0
    if n_full >= 4 and dchg[0] < 0.8 * np.nanmedian(dchg[1:4]):
        start = 1
        reasons.append("formation_skipped")
    nominal = float(np.nanmedian(dchg[start:start + 3]))
    profile["nominal_capacity_ah"] = nominal

    # C-rates (derived_avg) from full chg/dchg capacity / duration
    def crate(cap, dur):
        hrs = full[dur].to_numpy() / 3600.0
        with np.errstate(divide="ignore", invalid="ignore"):
            amps = np.where(hrs > 0, cap / hrs, np.nan)
        return float(np.nanmedian(amps) / nominal) if nominal > 0 else None
    profile["chg_c_rate"] = crate(chg, "charge_duration_s")
    profile["dchg_c_rate"] = crate(dchg, "discharge_duration_s")
    profile["c_rate_class"] = f"{_fmt_c(profile['chg_c_rate'])}/{_fmt_c(profile['dchg_c_rate'])}"

    profile["v_lo"] = _round05(np.nanmedian(full["min_voltage_v"]))
    profile["v_hi"] = _round05(np.nanmedian(full["max_voltage_v"]))
    profile["protocol_signature"] = (
        f"{profile['c_rate_class']}|{profile['v_lo']}-{profile['v_hi']}V|"
        f"{'pulse' if profile['has_pulses'] else 'cont'}")

    profile["quality"] = "ok" if n_full >= 10 else "partial"

    add("cap_initial_ah", nominal, unit="Ah")
    add("n_full_cycles", n_full, unit="cycles")

    v_init = float(mv[start]) if np.isfinite(mv[start]) else None
    add("mean_dchg_v_initial", v_init, unit="V")

    def cap_at(n):  # discharge capacity at the last full cycle <= n
        mask = cidx <= n
        return float(dchg[mask][-1]) if mask.any() and cidx.max() >= n else None

    for n in CAP_WINDOWS:
        if cidx.max() >= n:
            c = cap_at(n)
            add("cap_at", c, window=str(n), unit="Ah")
            if nominal > 0:
                add("retention_pct", 100 * c / nominal, window=str(n), unit="%")
            if v_init is not None and n == 100:
                vn = full.loc[full["cycle_index"] <= n, "mean_discharge_voltage_v"].iloc[-1]
                if np.isfinite(vn):
                    add("dchg_v_drop_100", v_init - float(vn), unit="V")

    # cycles_to_80pct (right-censored if never reached)
    if nominal > 0 and n_full >= 5:
        below = np.where(dchg < 0.8 * nominal)[0]
        if below.size:
            add("cycles_to_80pct", int(cidx[below[0]]), unit="cycles")
        else:
            add("cycles_to_80pct", int(cidx[-1]), unit="cycles", censored=True)

    # fade %/100cyc over full cycles (>=10)
    if n_full >= 10 and nominal > 0:
        valid = np.isfinite(dchg)
        slope = _slope(cidx[valid].astype(float), dchg[valid])
        add("fade_pct_per_100", -slope * 100.0 / nominal * 100.0, unit="%")

    # CE median over full cycles after the first 3
    ce_tail = ce[3:]
    ce_tail = ce_tail[np.isfinite(ce_tail)]
    if ce_tail.size:
        add("ce_median_pct", float(np.nanmedian(ce_tail)) * 100.0, unit="%")

    profile["reasons"] = ";".join(reasons)
    for m in metrics:
        m["quality"] = profile["quality"]
    return profile, pd.DataFrame(metrics), run_cycles


def _build_chunk(groups):
    return [_build_one(run_id, rows) for run_id, rows in groups]


def build_run_metrics(project, *, workers=8):
    project = Path(project)
    derived = project / "derived"
    summary = pd.read_parquet(derived / "cycles_summary.parquet")
    if "is_full" not in summary.columns:
        raise RuntimeError("cycles_summary.parquet predates is_full; rebuild with `db-build`")
    datasets = sorted(summary["dataset_id"].unique())

    assign, excluded = _run_assignments(project, datasets)

    summary = summary[summary["dataset_id"].isin(assign)].copy()
    meta = pd.DataFrame.from_dict(assign, orient="index")
    for col in ("run_id", "test_key", "channel", "fragment"):
        summary[col] = summary["dataset_id"].map(meta[col])
    # numpy object strings: pyarrow-backed str columns make the many small per-run slices slower
    text = summary.select_dtypes(include=["string", "str", "object"]).columns
    summary[text] = summary[text].astype(object)

    groups = list(summary.groupby("run_id"))
    if workers > 1 and len(groups) > 200:
        from concurrent.futures import ProcessPoolExecutor
        size = -(-len(groups) // (workers * 4))
        with ProcessPoolExecutor(max_workers=workers) as pool:
            built = [r for chunk in pool.map(_build_chunk, [groups[i:i + size] for i in range(0, len(groups), size)])
                     for r in chunk]
    else:
        built = _build_chunk(groups)

    profiles, all_metrics, all_cycles = [], [], []
    for prof, met, cyc in built:
        profiles.append(prof)
        if not met.empty:
            all_metrics.append(met)
        if not cyc.empty:
            all_cycles.append(cyc)

    prof_df = pd.DataFrame(profiles)
    met_df = pd.concat(all_metrics, ignore_index=True) if all_metrics else pd.DataFrame(
        columns=["run_id", "metric_id", "window", "value", "unit", "censored", "quality", "method"])
    cyc_df = pd.concat(all_cycles, ignore_index=True) if all_cycles else pd.DataFrame()

    db = project / "db"
    db.mkdir(exist_ok=True)
    prof_df.to_parquet(db / "run_profile.parquet", index=False)
    met_df.to_parquet(db / "run_metrics.parquet", index=False)
    cyc_df.to_parquet(db / "run_cycles.parquet", index=False)

    counts = prof_df["quality"].value_counts().to_dict() if len(prof_df) else {}
    return {
        "n_runs": len(prof_df),
        "n_datasets": len(datasets),
        "n_excluded": len(excluded),
        "n_metrics": len(met_df),
        "n_cycles": len(cyc_df),
        "quality_counts": counts,
        "outputs": [str(db / p) for p in
                    ("run_profile.parquet", "run_metrics.parquet", "run_cycles.parquet")],
    }


def load_run_profile(project):
    return pd.read_parquet(Path(project) / "db" / "run_profile.parquet")


def load_run_metrics(project):
    return pd.read_parquet(Path(project) / "db" / "run_metrics.parquet")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build per-run cycling metrics (Module A).")
    parser.add_argument("--project", required=True, help="project directory")
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    summary = build_run_metrics(args.project, workers=args.workers)
    print(f"runs={summary['n_runs']} metrics={summary['n_metrics']} "
          f"cycles={summary['n_cycles']} quality={summary['quality_counts']}")
    for path in summary["outputs"]:
        print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
