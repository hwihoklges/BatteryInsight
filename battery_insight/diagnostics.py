"""Auditable screening and observed-life summaries; no trained AIM model.

All diagnostic baselines are isolated by cell, test and test type. Incomplete
or unconfirmed cycles remain visible, but cannot define a healthy reference.
Thresholds and robust deviations are screening evidence, not root-cause proof.
"""
from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pandas as pd


GROUP_KEYS = ["cell_id", "test_id", "test_type"]
ALIASES = {
    "coulombic_efficiency_pct": "ce_pct",
    "capacity_retention_pct": "retention_pct",
}
# Floors are numerical screening tolerances, not validated chemistry limits.
DEFAULT_METRICS = {
    "charge_capacity_ah": {"absolute_floor": 0.001, "unit": "Ah", "direction": "both"},
    "discharge_capacity_ah": {"absolute_floor": 0.001, "unit": "Ah", "direction": "low"},
    "coulombic_efficiency_pct": {"absolute_floor": 0.2, "unit": "%", "direction": "both"},
    "capacity_retention_pct": {"absolute_floor": 0.5, "unit": "%", "direction": "low"},
    "temperature_max_c": {"absolute_floor": 0.5, "unit": "degC", "direction": "high"},
    "EoC_dchgR_60s_mohm": {"absolute_floor": 1.0, "unit": "mohm", "direction": "high"},
    "discharge_dvdq_low_soc_v_per_ah": {"absolute_floor": 0.001, "unit": "V/Ah", "direction": "high"},
    "discharge_dvdq_low_soc_proxy_v_per_ah": {"absolute_floor": 0.001, "unit": "V/Ah", "direction": "high"},
    "charge_dqdv_peak_voltage_v": {"absolute_floor": 0.005, "unit": "V", "direction": "both"},
    "discharge_dqdv_peak_voltage_v": {"absolute_floor": 0.005, "unit": "V", "direction": "both"},
}
DIAGNOSTIC_COLUMNS = GROUP_KEYS + [
    "cycle", "metric", "value", "unit", "status", "severity", "reasons",
    "candidate_causes", "evidence", "confidence", "review_status", "baseline_n",
    "baseline_median", "baseline_mad", "robust_score", "baseline_cutoff_cycle",
    "source_path", "source_hash", "source_row_start", "source_row_end",
]
LIFE_COLUMNS = GROUP_KEYS + [
    "observed_eol", "censored", "eol_cycle", "eol_confirmed_cycle",
    "eol_life_index", "eol_confirmed_life_index", "life_index_basis",
    "eol_threshold_pct", "eol_consecutive_cycles", "last_observed_cycle",
    "last_eligible_cycle", "eligible_cycles", "life_status", "early_cutoff_cycle",
    "early_observed_until_cycle", "early_cutoff_index", "early_cutoff_basis",
    "early_observed_until_life_index", "early_cycle_count", "early_status",
    "early_capacity_first_ah", "early_capacity_last_ah",
    "early_capacity_slope_ah_per_cycle", "early_mean_ce_pct",
]


def _number(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return math.nan
    return parsed if math.isfinite(parsed) else math.nan


def _positive_int(value: Any, name: str) -> int:
    parsed = _number(value)
    if not math.isfinite(parsed) or parsed < 1 or int(parsed) != parsed:
        raise ValueError(f"{name} must be a positive integer")
    return int(parsed)


def _known(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    try:
        missing = pd.isna(value)
        return not bool(missing) if np.ndim(missing) == 0 else False
    except (TypeError, ValueError):
        return False


def _is_true(value: Any) -> bool:
    return isinstance(value, (bool, np.bool_)) and bool(value)


def _complete(row: pd.Series) -> bool:
    # Explicit unknown/failed status cannot be overridden by a capacity flag.
    if "completion_status" in row.index:
        if str(row["completion_status"]).strip().lower() != "complete":
            return False
        if "capacity_complete" in row.index and not _is_true(row["capacity_complete"]):
            return False
        return True
    return _is_true(row.get("capacity_complete", row.get("complete", False)))


def _prepare(cycles: pd.DataFrame) -> pd.DataFrame:
    frame = cycles.copy()
    if frame.empty:
        return frame
    missing = [key for key in ["cell_id", "test_id", "cycle"] if key not in frame]
    if missing:
        raise ValueError("cycles missing required columns: " + ", ".join(missing))
    if "test_type" not in frame:
        frame["test_type"] = "unknown"
    for canonical, alias in ALIASES.items():
        if canonical not in frame and alias in frame:
            frame[canonical] = frame[alias]
    frame["_cycle"] = pd.to_numeric(frame["cycle"], errors="coerce")
    frame["_complete"] = frame.apply(_complete, axis=1)
    frame["_identity_known"] = frame[GROUP_KEYS].apply(
        lambda row: all(_known(value) for value in row), axis=1
    )
    frame["_duplicate_cycle"] = frame.duplicated(GROUP_KEYS + ["_cycle"], keep=False)
    frame["_eligible"] = (
        frame["_complete"] & frame["_identity_known"] & ~frame["_duplicate_cycle"]
        & np.isfinite(frame["_cycle"]) & (frame["_cycle"] >= 0)
    )
    return frame.sort_values("_cycle", kind="stable", na_position="last")


def _candidates(metric: str) -> str:
    if "efficiency" in metric or metric == "ce_pct":
        return "전류 오프셋; 충방전 기록 누락; 비가역 반응 후보 — 추가 확인 필요"
    if "capacity" in metric or "retention" in metric:
        return "시험 조건 변화; 기록/적분 오류; 용량 저하 후보 — 추가 확인 필요"
    if "temperature" in metric:
        return "환경 온도 변화; 센서 이상; 발열 증가 후보 — 추가 확인 필요"
    if "dvdq" in metric.lower() or "dqdv" in metric.lower():
        return '시험 온도/전류/SOC 차이; 미분/피크 매칭 영향; 전압-용량 곡선 변화 후보 - 추가 확인 필요'
    if "resistance" in metric.lower() or "dcir" in metric.lower() or "dchgr" in metric.lower():
        return "온도/SOC 차이; 접촉 변화; 저항 증가 후보 — 추가 확인 필요"
    return "측정 오류; 시험 조건 변화; 전기화학적 변화 후보 — 추가 확인 필요"


def diagnose(cycles: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Return one reviewable screening row per cycle and configured metric.

    ``diagnostics.metrics`` maps column names to ``absolute_floor`` (required,
    positive), ``direction`` (both/high/low), optional lower/upper thresholds,
    and unit. Default floors are uncalibrated screening tolerances. A baseline
    contains the first ``baseline_window`` cycles, with only complete cycles
    contributing. It never slides forward to replace bad initial cycles.
    """
    frame = _prepare(cycles)
    if frame.empty:
        return pd.DataFrame(columns=DIAGNOSTIC_COLUMNS)
    cfg = config.get("diagnostics", config)
    window = _positive_int(cfg.get("baseline_window", 5), "baseline_window")
    minimum = _positive_int(cfg.get("min_baseline_cycles", 3), "min_baseline_cycles")
    if minimum > window:
        raise ValueError("min_baseline_cycles must not exceed baseline_window")
    z_limit = _number(cfg.get("robust_z_threshold", 4.0))
    if not math.isfinite(z_limit) or z_limit <= 0:
        raise ValueError("robust_z_threshold must be finite and positive")
    metric_specs = cfg.get("metrics", DEFAULT_METRICS)
    if not isinstance(metric_specs, dict):
        raise ValueError("diagnostics.metrics must be a mapping")
    validated = {}
    for metric, spec in metric_specs.items():
        if not isinstance(spec, dict):
            raise ValueError(f"metric {metric} must have a configuration mapping")
        canonical = next((key for key, alias in ALIASES.items() if metric == alias), metric)
        defaults = DEFAULT_METRICS.get(canonical, {})
        spec = {**defaults, **spec}
        floor = _number(spec.get("absolute_floor"))
        if not math.isfinite(floor) or floor <= 0:
            raise ValueError(f"{metric}.absolute_floor must be finite and positive")
        direction = spec.get("direction", "both")
        if direction not in {"both", "high", "low"}:
            raise ValueError(f"{metric}.direction must be both, high, or low")
        bounds = {name: _number(spec[name]) for name in ("lower", "upper") if name in spec}
        if any(not math.isfinite(value) for value in bounds.values()):
            raise ValueError(f"{metric} thresholds must be finite")
        if bounds.get("lower", -math.inf) > bounds.get("upper", math.inf):
            raise ValueError(f"{metric}.lower must not exceed upper")
        validated[canonical] = {**spec, **bounds, "absolute_floor": floor, "direction": direction}

    records = []
    for _, group in frame.groupby(GROUP_KEYS, dropna=False, sort=False):
        unique_cycles = group.loc[np.isfinite(group["_cycle"]), "_cycle"].drop_duplicates()
        initial_cycles = unique_cycles.iloc[:window]
        cutoff = float(initial_cycles.max()) if len(initial_cycles) else math.nan
        initial = group[group["_eligible"] & group["_cycle"].isin(initial_cycles)]
        for metric, spec in validated.items():
            if metric not in group:
                continue
            baseline = pd.to_numeric(initial[metric], errors="coerce")
            baseline = baseline[np.isfinite(baseline)]
            count = len(baseline)
            median = float(baseline.median()) if count else math.nan
            mad = float((baseline - median).abs().median()) if count else math.nan
            scale = max(1.4826 * mad, spec["absolute_floor"]) if count else math.nan
            for _, row in group.iterrows():
                value = _number(row[metric])
                reasons = []
                score = math.nan
                status, severity = "ok", "info"
                if not row["_eligible"]:
                    status = "excluded_from_screening"
                    if not row["_complete"]:
                        reasons.append("completion_not_confirmed")
                    if not row["_identity_known"]:
                        reasons.append("identity_missing")
                    if row["_duplicate_cycle"]:
                        reasons.append("duplicate_cycle_identity")
                    if not math.isfinite(_number(row["_cycle"])) or row["_cycle"] < 0:
                        reasons.append("invalid_cycle")
                elif not math.isfinite(value):
                    status = "invalid_value"
                    reasons.append("metric_missing_or_nonfinite")
                else:
                    if count < minimum:
                        status = "insufficient_baseline"
                        reasons.append("insufficient_baseline")
                    elif row["_cycle"] <= cutoff:
                        status = "baseline_reference"
                    else:
                        score = (value - median) / scale
                        directional_score = {"both": abs(score), "high": score, "low": -score}[spec["direction"]]
                        if directional_score >= z_limit:
                            reasons.append("robust_deviation")
                            status = "flagged"
                    if "lower" in spec and value < spec["lower"]:
                        reasons.append("below_configured_limit")
                        status = "flagged"
                    if "upper" in spec and value > spec["upper"]:
                        reasons.append("above_configured_limit")
                        status = "flagged"
                flagged = status == "flagged"
                if flagged:
                    severity = "warning"
                evidence = {
                    "method": "initial_window_median_mad", "baseline_n": count,
                    "baseline_window": window, "minimum_baseline_cycles": minimum,
                    "absolute_floor": spec["absolute_floor"], "direction": spec["direction"],
                    "robust_z_threshold": z_limit,
                    "baseline_cycle_numbers": [float(v) for v in initial.loc[np.isfinite(pd.to_numeric(initial[metric], errors="coerce")), "_cycle"]],
                    "configured_limits": {name: spec[name] for name in ("lower", "upper") if name in spec},
                    "interpretation": "screening_only_not_causal_diagnosis",
                    "calibration_status": spec.get("calibration_status", "calibration_required"),
                    "proxy_metric": "proxy" in metric.lower(),
                    "aggregation": "independent_flags_no_weighted_sum",
                }
                record = {key: row.get(key) for key in GROUP_KEYS}
                record.update({
                    "cycle": row["cycle"], "metric": metric, "value": value,
                    "unit": spec.get("unit", "unspecified"), "status": status,
                    "severity": severity, "reasons": "; ".join(reasons),
                    "candidate_causes": _candidates(metric) if flagged else "",
                    "evidence": json.dumps(evidence, ensure_ascii=False, allow_nan=False),
                    "confidence": "screening_only" if flagged else "not_applicable",
                    "review_status": "pending" if flagged else "not_required",
                    "baseline_n": count, "baseline_median": median, "baseline_mad": mad,
                    "robust_score": score, "baseline_cutoff_cycle": cutoff,
                })
                record.update({key: row.get(key) for key in DIAGNOSTIC_COLUMNS if key.startswith("source_")})
                records.append(record)
    return pd.DataFrame(records, columns=DIAGNOSTIC_COLUMNS)


def life_summary(cycles: pd.DataFrame, config: dict) -> pd.DataFrame:
    """Summarize observed EOL and fixed-horizon features for Cycle/Insitu only.

    EOL requires k consecutive *numbered* complete life cycles at or below the
    configured retention threshold. Missing, duplicate, unknown-completion,
    or non-finite observations break a streak. ``eol_cycle`` is the first
    crossing in the confirmed streak; ``eol_confirmed_cycle`` is its last.
    Cycle/Insitu are combined within each cell/test. Supplied positive
    ``life_index`` provides the chronological index; without it, raw cycle
    numbers are the conservative fallback. RPT and other test types are never
    life observations. Raw EOL cycle numbers and life indices are both kept.
    Early fields use no indices beyond ``life.cutoff_cycle`` (the legacy
    configuration name); ``early_cutoff_basis`` explicitly records the basis.
    This function does not predict lifetime or fit a model.
    """
    frame = _prepare(cycles)
    if frame.empty:
        return pd.DataFrame(columns=LIFE_COLUMNS)
    cfg = config.get("life", config)
    cutoff = _number(cfg.get("cutoff_cycle", 100))
    threshold = _number(cfg.get("eol_threshold_pct", 80.0))
    consecutive = _positive_int(cfg.get("eol_consecutive_cycles", 3), "eol_consecutive_cycles")
    min_early = _positive_int(cfg.get("min_early_cycles", 3), "min_early_cycles")
    if not math.isfinite(cutoff) or cutoff < 0:
        raise ValueError("life.cutoff_cycle must be finite and nonnegative")
    if not math.isfinite(threshold) or not 0 < threshold <= 100:
        raise ValueError("life.eol_threshold_pct must be within (0, 100]")
    frame = frame[frame["test_type"].astype(str).str.strip().str.casefold().isin({"cycle", "insitu"})]
    index_basis = "life_index" if "life_index" in frame else "cycle"
    frame["_life_index"] = pd.to_numeric(frame[index_basis], errors="coerce")
    valid_index = np.isfinite(frame["_life_index"]) & (frame["_life_index"] > 0)
    valid_index &= frame["_life_index"].eq(np.floor(frame["_life_index"]))
    frame["_eligible"] &= valid_index
    frame["_eligible"] &= ~frame.duplicated(["cell_id", "test_id", "_life_index"], keep=False)
    frame = frame.sort_values("_life_index", kind="stable", na_position="last")
    records = []
    for _, group in frame.groupby(["cell_id", "test_id"], dropna=False, sort=False):
        streak = []
        first_crossing = confirmed = math.nan
        first_life_index = confirmed_life_index = math.nan
        usable_retention = 0
        for _, row in group.iterrows():
            retention = _number(row.get("capacity_retention_pct"))
            cycle = _number(row["_cycle"])
            life_index = _number(row["_life_index"])
            if not row["_eligible"] or not math.isfinite(retention) or retention < 0:
                streak = []
                continue
            usable_retention += 1
            if retention > threshold:
                streak = []
                continue
            if streak and life_index != streak[-1][0] + 1:
                streak = []
            streak.append((life_index, cycle))
            if len(streak) == consecutive and not math.isfinite(first_crossing):
                first_life_index, first_crossing = streak[0]
                confirmed_life_index, confirmed = streak[-1]
        eligible = group[group["_eligible"]]
        early = eligible[eligible["_life_index"] <= cutoff]
        capacity = pd.to_numeric(early.get("discharge_capacity_ah", pd.Series(index=early.index, dtype=float)), errors="coerce")
        valid_capacity = early[np.isfinite(capacity)]
        capacity = capacity[np.isfinite(capacity)]
        enough = len(capacity) >= min_early
        slope = math.nan
        if enough and valid_capacity["_life_index"].nunique() >= 2:
            x = valid_capacity["_life_index"].to_numpy(dtype=float)
            y = capacity.to_numpy(dtype=float)
            centered = x - np.mean(x)
            slope = float(np.dot(centered, y - np.mean(y)) / np.dot(centered, centered))
        ce = pd.to_numeric(early.get("coulombic_efficiency_pct", pd.Series(index=early.index, dtype=float)), errors="coerce")
        ce = ce[np.isfinite(ce)]
        eol = math.isfinite(first_crossing)
        record = {key: group.iloc[0][key] for key in GROUP_KEYS}
        record["test_type"] = "/".join(dict.fromkeys(group["test_type"].astype(str)))
        record.update({
            "observed_eol": eol, "censored": not eol,
            "eol_cycle": first_crossing, "eol_confirmed_cycle": confirmed,
            "eol_life_index": first_life_index,
            "eol_confirmed_life_index": confirmed_life_index,
            "life_index_basis": index_basis,
            "eol_threshold_pct": threshold, "eol_consecutive_cycles": consecutive,
            "last_observed_cycle": float(group["_cycle"].max()),
            "last_eligible_cycle": float(eligible["_cycle"].max()) if len(eligible) else math.nan,
            "eligible_cycles": len(eligible),
            "life_status": "observed_eol" if eol else ("right_censored" if usable_retention else "insufficient_retention_data"),
            "early_cutoff_cycle": cutoff,
            "early_cutoff_index": cutoff, "early_cutoff_basis": index_basis,
            "early_observed_until_cycle": float(early["_cycle"].max()) if len(early) else math.nan,
            "early_observed_until_life_index": float(early["_life_index"].max()) if len(early) else math.nan,
            "early_cycle_count": len(early),
            "early_status": "available" if enough else "insufficient_early_data",
            "early_capacity_first_ah": float(capacity.iloc[0]) if len(capacity) else math.nan,
            "early_capacity_last_ah": float(capacity.iloc[-1]) if len(capacity) else math.nan,
            "early_capacity_slope_ah_per_cycle": slope,
            "early_mean_ce_pct": float(ce.mean()) if len(ce) else math.nan,
        })
        records.append(record)
    return pd.DataFrame(records, columns=LIFE_COLUMNS)


def compare_cells(
    registry: pd.DataFrame, cell_a: str, cell_b: str,
    target_variable: str, variables: list[str],
) -> dict:
    """Check whether one cell pair differs only in the declared variable.

    Missing values remain unknown even when both are missing. This checks
    registry metadata only, and cannot establish causality or replication.
    """
    if "cell_id" not in registry:
        raise ValueError("registry requires a cell_id column")
    if not target_variable or target_variable == "cell_id":
        raise ValueError("target_variable must be a design/condition variable")
    if cell_a == cell_b:
        raise ValueError("compare_cells requires two different cells")
    fields = list(dict.fromkeys([*variables, target_variable]))
    fields = [field for field in fields if field != "cell_id"]
    rows_a = registry[registry["cell_id"] == cell_a]
    rows_b = registry[registry["cell_id"] == cell_b]
    result = {
        "cell_a": cell_a, "cell_b": cell_b, "target_variable": target_variable,
        "status": "unknown", "controlled": None, "target_changed": None,
        "confounders": [], "missing_variables": [], "differences": {}, "shared": {},
        "warnings": ["single_cell_pair_no_replication", "metadata_check_is_not_causal_evidence"],
        "review_status": "pending", "comparison_n_cells": 2,
    }
    if len(rows_a) != 1 or len(rows_b) != 1:
        result["warnings"].append("cell_mapping_missing_or_ambiguous")
        result["registry_matches"] = {str(cell_a): len(rows_a), str(cell_b): len(rows_b)}
        return result
    a, b = rows_a.iloc[0], rows_b.iloc[0]
    for field in fields:
        av, bv = a.get(field), b.get(field)
        if not _known(av) or not _known(bv):
            result["missing_variables"].append(field)
            continue
        equal = bool(av == bv)
        if equal:
            result["shared"][field] = av.item() if isinstance(av, np.generic) else av
        else:
            result["differences"][field] = {
                "cell_a": av.item() if isinstance(av, np.generic) else av,
                "cell_b": bv.item() if isinstance(bv, np.generic) else bv,
            }
    result["confounders"] = [field for field in result["differences"] if field != target_variable]
    if target_variable not in result["missing_variables"]:
        result["target_changed"] = target_variable in result["differences"]
    if result["missing_variables"]:
        result["warnings"].append("unknown_metadata_prevents_controlled_comparison")
    elif result["confounders"]:
        result["status"], result["controlled"] = "confounded", False
    elif not result["target_changed"]:
        result["status"], result["controlled"] = "target_unchanged", False
    else:
        result["status"], result["controlled"] = "controlled_metadata_only", True
    return result
