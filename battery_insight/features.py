"""Conservative electrochemical features from normalized, chronological records.

Current is positive for charge, time is seconds, capacity is Ah. No unit guessing,
voltage sorting, extrapolation, or integration across segment boundaries is used.
Completion requires evidence; source capacities and integrated capacities remain
separate. Numerical proxies are labelled as such and are not failure diagnoses.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


DEFAULTS = {
    "max_gap_s": 120.0,
    "min_current_a": 1e-6,
    "pulse_times_s": [0.1, 1.0, 10.0, 30.0, 60.0],
    "baseline_cycle": None,
    "min_curve_points": 15,
    "min_voltage_delta_v": 1e-5,
    "low_soc_limit_pct": 20.0,
    "baseline_window_s": 5.0,
    "min_baseline_points": 3,
    "pulse_current_relative_tolerance": 0.05,
    "soc100_tolerance_pct": 0.5,
    "start_time_tolerance_s": 1e-6,
    "complete_end_flags": ["complete", "completed", "normal", "end", "endv", "endq", "endtime"],
    "incomplete_end_flags": ["abort", "aborted", "fail", "failed", "interrupted", "truncated"],
    "assume_complete_bidirectional_cycles": False,
}
ISSUE_COLUMNS = ["test_id", "cell_id", "cycle", "code", "severity", "detail", "source_row", "source_path"]
PULSE_COLUMNS = [
    "test_id", "cell_id", "cycle", "test_type", "feature_type", "segment_id", "step_id",
    "direction", "pulse_time_s", "time_origin", "resistance_ohm", "resistance_mohm",
    "delta_voltage_v", "delta_current_a", "baseline_voltage_v", "baseline_current_a",
    "response_voltage_v", "response_current_a", "relaxation_delta_v", "soc_pct",
    "eoc_discharge_eligible", "source_path", "source_hash", "source_row_start", "source_row_end",
    "baseline_source_row", "response_source_row_left", "response_source_row_right",
]


def _numeric(series):
    return pd.to_numeric(series, errors="coerce").to_numpy(dtype=float, na_value=np.nan)


def _finite_number(value):
    try:
        return np.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _value(row, column, default=np.nan):
    value = row.get(column, default)
    return default if pd.isna(value) else value


def _issue(issues, frame, code, detail, severity="warning", row=None):
    row = frame.iloc[0] if row is None else row
    issues.append({
        "test_id": _value(row, "test_id", ""), "cell_id": _value(row, "cell_id", ""),
        "cycle": _value(row, "cycle"), "code": code, "severity": severity, "detail": detail,
        "source_row": _value(row, "source_row"), "source_path": _value(row, "source_path", ""),
    })


def _segments(frame):
    """Split contiguous occurrences even if a source reuses its segment identifier."""
    if frame.empty:
        return []
    # Nullable/Arrow comparisons have an unknown first value. A null group key
    # would otherwise silently discard the onset observation before integration.
    boundary = np.zeros(len(frame), dtype=bool)
    for name in ("segment_id", "source_path", "step_id"):
        changed = frame[name].ne(frame[name].shift())
        boundary |= changed.to_numpy(dtype=bool, na_value=True)
    boundary[0] = True
    return [part for _, part in frame.groupby(np.cumsum(boundary), sort=False)]


def _declared_kind(segment):
    if "step_kind" not in segment:
        return ""
    kinds = segment["step_kind"].fillna("").astype(str).str.strip().str.lower().unique()
    return str(kinds[0]) if len(kinds) == 1 else ""


def _direction(segment, settings):
    kind = _declared_kind(segment)
    if kind == "end":
        return "inactive"
    current = _numeric(segment["current_a"])
    if not np.isfinite(current).all():
        return "unknown"
    limit = settings["min_current_a"]
    # A declared rest can retain real sensor offset/noise. Never force its
    # observations to zero or include them in charge/discharge capacity.
    if kind == "rest":
        return "rest"
    # Native records include the observed zero-current instant at step onset.
    # Known step semantics permit that sample, but not an opposite active current.
    if kind == "charge":
        return "charge" if np.any(current > limit) and np.all(current >= -limit) else "mixed"
    if kind == "discharge":
        return "discharge" if np.any(current < -limit) and np.all(current <= limit) else "mixed"
    if np.all(np.abs(current) <= limit):
        return "rest"
    if np.all(current > limit):
        return "charge"
    if np.all(current < -limit):
        return "discharge"
    return "mixed"


def _invalid_current_directions(segment, settings):
    """Identify which directional totals an excluded segment can contaminate."""
    kind = _declared_kind(segment)
    if kind in ("charge", "discharge"):
        return {kind}
    if kind in ("rest", "end"):
        return set()
    current = _numeric(segment["current_a"])
    if not np.isfinite(current).all():
        return {"charge", "discharge"}
    result = set()
    if np.any(current > settings["min_current_a"]):
        result.add("charge")
    if np.any(current < -settings["min_current_a"]):
        result.add("discharge")
    return result


def _cycle_clock_valid(segments, settings):
    """Allow an evidenced step end/start at the same instant, never a reset."""
    end_flags = {str(x).strip().lower() for x in settings["complete_end_flags"]}
    for index, segment in enumerate(segments):
        time = _numeric(segment["time_s"])
        if not np.isfinite(time).all() or np.any(np.diff(time) <= 0):
            return False
        if index == 0:
            continue
        previous = segments[index - 1].iloc[-1]
        first = segment.iloc[0]
        gap = float(first["time_s"]) - float(previous["time_s"])
        if gap < 0:
            return False
        if gap == 0:
            onset = _value(first, "step_time_s")
            flag = str(_value(previous, "end_flag", "")).strip().lower()
            same_source = _value(first, "source_path", "") == _value(previous, "source_path", "")
            step_end = _value(previous, "step_end", False)
            ended = flag in end_flags or (isinstance(step_end, (bool, np.bool_)) and bool(step_end))
            if not (same_source and ended and _finite_number(onset)
                    and abs(float(onset)) <= settings["start_time_tolerance_s"]):
                return False
    return True


def _capacity_endpoint_verified(segment, direction, settings):
    """A generic program-step end does not establish a full capacity endpoint."""
    last = segment.iloc[-1]
    flag = str(_value(last, "end_flag", "")).strip().lower()
    if flag not in {"time_end", "endtime", "soc_end", "step_complete", "next_step"}:
        return True
    soc = _value(last, "soc_pct")
    endpoint = 100.0 if direction == "charge" else 0.0
    return bool(_finite_number(soc) and 0 <= float(soc) <= 100
                and abs(float(soc) - endpoint) <= settings["soc100_tolerance_pct"])


def _elapsed(segment, settings, issues):
    """An absent step clock does not justify assuming the pulse onset time."""
    t = _numeric(segment["time_s"])
    if not np.isfinite(t).all() or (len(t) > 1 and np.any(np.diff(t) <= 0)):
        return None, "invalid"
    step = _numeric(segment["step_time_s"])
    if np.isfinite(step).all() and np.all(step >= 0) and (len(step) == 1 or np.all(np.diff(step) > 0)):
        if len(step) > 1 and not np.allclose(np.diff(step), np.diff(t), rtol=1e-3, atol=1e-3):
            _issue(issues, segment, "step_clock_mismatch", "step_time_s increments disagree with time_s; timed response omitted.")
            return None, "invalid"
        return step, "step_time_s"
    if np.isfinite(step).any():
        _issue(issues, segment, "invalid_step_clock", "Partial, negative, or non-increasing step_time_s; timed response omitted.")
    else:
        _issue(issues, segment, "pulse_onset_unknown", "step_time_s is absent; onset-referenced pulse/rest times are not inferred.", "info")
    return None, "unknown"


def _interpolate(t, values, target, max_gap):
    """Return value and bracket indices; require observations on both sides."""
    if t is None or not len(t) or target < t[0] or target > t[-1]:
        return None
    right = int(np.searchsorted(t, target, side="left"))
    if right < len(t) and np.isclose(t[right], target, atol=1e-9, rtol=0):
        if np.isfinite(values[right]):
            return float(values[right]), right, right
        return None
    left = right - 1
    if left < 0 or right >= len(t) or t[right] - t[left] > max_gap:
        return None
    if not np.isfinite(values[[left, right]]).all():
        return None
    ratio = (target - t[left]) / (t[right] - t[left])
    return float(values[left] + ratio * (values[right] - values[left])), left, right


def _curve_features(segment, direction, settings):
    n = len(segment)
    empty = {"curve_points": 0, "dqdv_peak_ah_per_v": np.nan, "dqdv_peak_voltage_v": np.nan,
             "dqdv_peak_count": 0, "dvdq_low_soc_v_per_ah": np.nan,
             "dvdq_low_soc_proxy_v_per_ah": np.nan}
    if n < settings["min_curve_points"]:
        return empty
    t, v, current = (_numeric(segment[name]) for name in ("time_s", "voltage_v", "current_a"))
    if not all(np.isfinite(x).all() for x in (t, v, current)):
        return empty
    dt = np.diff(t)
    if np.any(dt <= 0) or np.any(dt > settings["max_gap_s"]):
        return empty
    dq = 0.5 * (np.abs(current[:-1]) + np.abs(current[1:])) * dt / 3600
    q = np.r_[0.0, np.cumsum(dq)]
    if q[-1] <= 0:
        return empty
    # Use chronological monotonic runs. Reversals split a curve; they are never sorted away.
    expected = 1 if direction == "charge" else -1
    valid_dv = expected * np.diff(v) > settings["min_voltage_delta_v"]
    starts = np.r_[0, np.flatnonzero(~valid_dv) + 1]
    ends = np.r_[np.flatnonzero(~valid_dv) + 1, n]
    runs = [(a, b) for a, b in zip(starts, ends) if b - a >= settings["min_curve_points"]]
    if not runs:
        return empty
    a, b = max(runs, key=lambda run: run[1] - run[0])
    vv, qq = v[a:b], q[a:b]
    gradient = np.abs(np.diff(qq) / np.diff(vv))
    v_mid = (vv[:-1] + vv[1:]) / 2
    # A five-point moving average smooths derivatives without reordering voltage.
    if len(gradient) >= 5:
        derivative = np.convolve(gradient, np.ones(5) / 5, mode="valid")
        vv_mid = v_mid[2:-2]
    else:
        derivative, vv_mid = gradient, v_mid
    from scipy.signal import find_peaks
    peaks, _ = find_peaks(derivative, prominence=max(float(np.ptp(derivative)) * 0.05, 1e-8))
    result = dict(empty, curve_points=int(b - a), dqdv_peak_count=int(len(peaks)))
    if len(peaks):
        peak = int(peaks[np.argmax(derivative[peaks])])
        result["dqdv_peak_ah_per_v"] = float(derivative[peak])
        result["dqdv_peak_voltage_v"] = float(vv_mid[peak])

    def slope(mask):
        # Only a contiguous run of at least three observations can support this slope.
        mask = mask & (np.arange(n) >= a) & (np.arange(n) < b)
        locations = np.flatnonzero(mask)
        if len(locations) < 3 or np.any(np.diff(locations) != 1):
            return np.nan
        qs, vs = q[locations], v[locations]
        if np.ptp(qs) <= 1e-12:
            return np.nan
        return float(abs(np.polyfit(qs, vs, 1)[0]))

    soc = _numeric(segment["soc_pct"])
    actual_low = np.isfinite(soc) & (soc >= 0) & (soc <= settings["low_soc_limit_pct"])
    result["dvdq_low_soc_v_per_ah"] = slope(actual_low)
    proxy = q <= q[-1] * 0.2 if direction == "charge" else q >= q[-1] * 0.8
    result["dvdq_low_soc_proxy_v_per_ah"] = slope(proxy)
    return result


def _qc_time(segment, settings):
    if _direction(segment, settings) != "charge":
        return np.nan
    t, soc = _numeric(segment["time_s"]), _numeric(segment["soc_pct"])
    if len(t) < 2 or not np.isfinite(t).all() or not np.isfinite(soc).all():
        return np.nan
    if np.any(np.diff(t) <= 0) or np.any(np.diff(t) > settings["max_gap_s"]):
        return np.nan
    if np.any(np.diff(soc) < 0) or np.any(soc < 0) or np.any(soc > 100):
        return np.nan
    def crossing(target):
        exact = np.flatnonzero(np.isclose(soc, target, atol=1e-9, rtol=0))
        if len(exact):
            return float(t[exact[0]])
        matches = np.flatnonzero((soc[:-1] < target) & (soc[1:] > target))
        if len(matches) != 1:
            return np.nan
        k = int(matches[0])
        return float(t[k] + (target - soc[k]) / (soc[k + 1] - soc[k]) * (t[k + 1] - t[k]))
    start, end = crossing(10), crossing(80)
    return end - start if np.isfinite(start) and np.isfinite(end) and end >= start else np.nan


def _summarize_direction(segments, direction, settings, issues):
    selected = [part for part in segments if _direction(part, settings) == direction]
    result = {"capacity_ah": np.nan, "capacity_integrated_ah": np.nan, "capacity_measured_ah": np.nan,
              "energy_wh": np.nan, "mean_voltage_v": np.nan, "cc_capacity_ah": np.nan,
              "cv_capacity_ah": np.nan, "duration_s": np.nan,
              "curve_points": 0, "dqdv_peak_ah_per_v": np.nan, "dqdv_peak_voltage_v": np.nan,
              "dqdv_peak_count": 0, "dvdq_low_soc_v_per_ah": np.nan, "dvdq_low_soc_proxy_v_per_ah": np.nan}
    if not selected:
        return result, False, None
    q_total = e_total = duration = measured = cc = cv = 0.0
    q_valid = e_valid = measured_valid = True
    have_cc = have_cv = False
    for part in selected:
        t, current, voltage, cap = (_numeric(part[name]) for name in ("time_s", "current_a", "voltage_v", "capacity_ah"))
        dt = np.diff(t)
        clock_ok = len(part) >= 2 and np.isfinite(t).all() and np.all(dt > 0) and np.all(dt <= settings["max_gap_s"])
        if not clock_ok:
            q_valid = e_valid = measured_valid = False
            continue
        dq = 0.5 * (np.abs(current[:-1]) + np.abs(current[1:])) * dt / 3600
        q_total += float(dq.sum())
        duration += float(dt.sum())
        modes = part["mode"].fillna("unknown").astype(str).str.upper().to_numpy()
        cc_intervals = (modes[:-1] == "CC") & (modes[1:] == "CC")
        cv_intervals = (modes[:-1] == "CV") & (modes[1:] == "CV")
        have_cc |= bool(cc_intervals.any())
        have_cv |= bool(cv_intervals.any())
        cc += float(dq[cc_intervals].sum())
        cv += float(dq[cv_intervals].sum())
        if np.isfinite(voltage).all():
            # Trapezoid of instantaneous power, not product of average I and average V.
            power = np.abs(current) * voltage
            e_total += float(np.sum((power[:-1] + power[1:]) * 0.5 * dt / 3600))
        else:
            e_valid = False
        if np.isfinite(cap).all() and np.all(np.diff(cap) >= -1e-9):
            measured += float(cap[-1] - cap[0])
        else:
            measured_valid = False
            if np.isfinite(cap).any():
                _issue(issues, part, "capacity_counter_invalid", "Measured step capacity is partial or decreases; measured capacity omitted.")
        # Only CC segments support differential curve interpretation here.
        if np.all(modes == "CC"):
            curve = _curve_features(part, direction, settings)
            if curve["curve_points"] > result["curve_points"]:
                result.update(curve)
    if q_valid and q_total > 0:
        result.update(capacity_ah=q_total, capacity_integrated_ah=q_total, duration_s=duration,
                      cc_capacity_ah=cc if have_cc else np.nan, cv_capacity_ah=cv if have_cv else np.nan)
        if e_valid:
            result.update(energy_wh=e_total, mean_voltage_v=e_total / q_total)
    if measured_valid:
        result["capacity_measured_ah"] = measured
    final_flag = str(_value(selected[-1].iloc[-1], "end_flag", "")).strip().lower()
    return result, q_valid and q_total > 0, final_flag


def _pulse_rows(segments, settings, issues):
    rows = []
    for index, segment in enumerate(segments):
        direction = _direction(segment, settings)
        if direction not in ("charge", "discharge", "rest") or index == 0:
            continue
        before = segments[index - 1]
        before_direction = _direction(before, settings)
        # Adjacency includes a checked global clock and same source acquisition.
        elapsed_gap = _value(segment.iloc[0], "time_s") - _value(before.iloc[-1], "time_s")
        same_source = _value(segment.iloc[0], "source_path", "") == _value(before.iloc[-1], "source_path", "")
        if not same_source or not _finite_number(elapsed_gap) or not (0 < elapsed_gap <= settings["max_gap_s"]):
            continue
        is_pulse = direction in ("charge", "discharge") and before_direction == "rest"
        is_rest = direction == "rest" and before_direction in ("charge", "discharge")
        if not is_pulse and not is_rest:
            continue
        elapsed, origin = _elapsed(segment, settings, issues)
        if elapsed is None:
            continue
        onset_time = float(segment["time_s"].iloc[0]) - float(elapsed[0])
        if onset_time < float(before["time_s"].iloc[-1]) - settings["start_time_tolerance_s"]:
            _issue(issues, segment, "pulse_onset_conflict", "Step clock places response onset before the adjacent baseline record; response omitted.", "error")
            continue
        current, voltage = _numeric(segment["current_a"]), _numeric(segment["voltage_v"])
        if not np.isfinite(current).all() or not np.isfinite(voltage).all():
            continue
        baseline = before.iloc[-1]
        baseline_v, baseline_i = _value(baseline, "voltage_v"), _value(baseline, "current_a")
        if not _finite_number(baseline_v) or not _finite_number(baseline_i):
            continue
        if is_pulse:
            baseline_time = _numeric(before["time_s"])
            if not np.isfinite(baseline_time).all() or np.any(np.diff(baseline_time) <= 0):
                continue
            recent = before.loc[before["time_s"] >= before["time_s"].iloc[-1] - settings["baseline_window_s"]]
            if len(recent) < settings["min_baseline_points"]:
                _issue(issues, segment, "pulse_baseline_insufficient", "Adjacent rest has too few baseline observations.", "info")
                continue
        soc = _numeric(before["soc_pct"])
        baseline_soc = float(soc[-1]) if np.isfinite(soc[-1]) else np.nan
        prior_charge_adjacent = False
        if index >= 2:
            prior = segments[index - 2]
            prior_gap = _value(before.iloc[0], "time_s") - _value(prior.iloc[-1], "time_s")
            prior_charge_adjacent = bool(_finite_number(prior_gap) and 0 < prior_gap <= settings["max_gap_s"]
                and _value(prior.iloc[-1], "source_path", "") == _value(before.iloc[0], "source_path", ""))
        eoc_eligible = bool(is_pulse and direction == "discharge" and index >= 2 and prior_charge_adjacent
                            and _direction(segments[index - 2], settings) == "charge"
                            and np.isfinite(baseline_soc)
                            and abs(baseline_soc - 100) <= settings["soc100_tolerance_pct"])
        for target in settings["pulse_times_s"]:
            target = float(target)
            v_at = _interpolate(elapsed, voltage, target, settings["max_gap_s"])
            i_at = _interpolate(elapsed, current, target, settings["max_gap_s"])
            if v_at is None or i_at is None:
                continue
            response_v, left, right = v_at
            if np.any(np.diff(elapsed[:right + 1]) > settings["max_gap_s"]):
                continue
            response_i = i_at[0]
            dv, di = response_v - float(baseline_v), response_i - float(baseline_i)
            resistance = np.nan
            if is_pulse:
                prefix_i = current[:right + 1]
                center = float(np.median(prefix_i))
                tolerance = max(abs(center) * settings["pulse_current_relative_tolerance"], settings["min_current_a"])
                if np.any(np.abs(prefix_i - center) > tolerance) or abs(di) <= settings["min_current_a"]:
                    _issue(issues, segment, "pulse_current_unstable", f"Current not stable through {target:g} s; resistance omitted.", "info")
                    continue
                resistance = dv / di
                if resistance < 0:
                    _issue(issues, segment, "negative_pulse_resistance", f"DeltaV/DeltaI at {target:g} s is negative; inspect polarity and transition.")
                    resistance = np.nan
            first, last = segment.iloc[0], segment.iloc[-1]
            rows.append({
                "test_id": _value(first, "test_id", ""), "cell_id": _value(first, "cell_id", ""),
                "cycle": _value(first, "cycle"), "test_type": _value(first, "test_type", "Unknown"),
                "feature_type": "pulse" if is_pulse else "rest", "segment_id": _value(first, "segment_id"),
                "step_id": _value(first, "step_id", ""), "direction": direction,
                "pulse_time_s": target, "time_origin": origin, "resistance_ohm": resistance,
                "resistance_mohm": resistance * 1000, "delta_voltage_v": dv, "delta_current_a": di,
                "baseline_voltage_v": baseline_v, "baseline_current_a": baseline_i,
                "response_voltage_v": response_v, "response_current_a": response_i,
                "relaxation_delta_v": response_v - voltage[0] if is_rest else np.nan,
                "soc_pct": baseline_soc, "eoc_discharge_eligible": eoc_eligible,
                "source_path": _value(first, "source_path", ""), "source_hash": _value(first, "source_hash", ""),
                "source_row_start": _value(first, "source_row"), "source_row_end": _value(last, "source_row"),
                "baseline_source_row": _value(baseline, "source_row"),
                "response_source_row_left": _value(segment.iloc[left], "source_row"),
                "response_source_row_right": _value(segment.iloc[right], "source_row"),
            })
    return rows


def finalize_cycles(cycles: pd.DataFrame, settings: dict | None = None) -> pd.DataFrame:
    """Recompute type-isolated baselines after streaming per-cycle extraction.

    Source order is authoritative. A configured baseline cycle must be complete;
    absence does not silently fall back to another cycle. life_index counts only
    Cycle/Insitu records together; RPT has its own sequential rpt_index.
    """
    cfg = {**DEFAULTS, **(settings or {})}
    result = cycles.copy()
    for name in ("baseline_cycle", "baseline_discharge_capacity_ah", "capacity_retention_pct", "life_index", "rpt_index"):
        result[name] = np.nan
    if result.empty:
        return result
    result = result.reset_index(drop=True)
    for _, group in result.groupby(["test_id", "cell_id"], sort=False, dropna=False):
        life_mask = group["test_type"].astype(str).str.lower().isin(["cycle", "insitu"])
        life_indices = group.index[life_mask]
        result.loc[life_indices, "life_index"] = np.arange(1, len(life_indices) + 1, dtype=float)
        rpt_indices = group.index[group["test_type"].astype(str).str.lower().eq("rpt")]
        result.loc[rpt_indices, "rpt_index"] = np.arange(1, len(rpt_indices) + 1, dtype=float)
    for _, group in result.groupby(["test_id", "cell_id", "test_type"], sort=False, dropna=False):
        capacity = pd.to_numeric(group["discharge_capacity_ah"], errors="coerce")
        complete = group["capacity_complete"].fillna(False).eq(True) & capacity.gt(0) & np.isfinite(capacity)
        candidates = group.loc[complete]
        if cfg["baseline_cycle"] is not None:
            candidates = candidates.loc[candidates["cycle"].eq(cfg["baseline_cycle"])]
        if candidates.empty:
            continue
        baseline = candidates.iloc[0]
        result.loc[group.index, "baseline_cycle"] = baseline["cycle"]
        result.loc[group.index, "baseline_discharge_capacity_ah"] = baseline["discharge_capacity_ah"]
        # Earlier observations cannot use a future baseline. Source order is preserved.
        valid_index = group.index[complete & (group.index >= baseline.name)]
        result.loc[valid_index, "capacity_retention_pct"] = 100 * capacity.loc[valid_index] / baseline["discharge_capacity_ah"]
    return result


def extract_features(frame: pd.DataFrame, settings: dict | None = None) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return cycle features, long pulse/rest responses, and provenance-linked QC.

    This function may receive one cycle or multiple cycles from one or more tests.
    Caller should call :func:`finalize_cycles` after concatenating streamed outputs.
    """
    cfg = {**DEFAULTS, **(settings or {})}
    for key in ("max_gap_s", "min_current_a", "min_curve_points", "min_voltage_delta_v", "min_baseline_points"):
        if not _finite_number(cfg[key]) or float(cfg[key]) <= 0:
            raise ValueError(f"{key} must be finite and positive")
    if any(not _finite_number(t) or float(t) <= 0 for t in cfg["pulse_times_s"]):
        raise ValueError("pulse_times_s must contain finite positive elapsed times")
    if frame.empty:
        return pd.DataFrame(), pd.DataFrame(columns=PULSE_COLUMNS), pd.DataFrame(columns=ISSUE_COLUMNS)
    required = {"test_id", "cell_id", "cycle", "segment_id", "step_id", "time_s", "voltage_v", "current_a", "test_type"}
    absent = required - set(frame.columns)
    if absent:
        raise ValueError("Missing normalized columns: " + ", ".join(sorted(absent)))
    data = frame.copy().reset_index(drop=True)
    for column, default in {
        "source_path": "", "source_hash": "", "source_row": np.nan, "step_time_s": np.nan,
        "capacity_ah": np.nan, "mode": "unknown", "temperature_c": np.nan, "soc_pct": np.nan, "end_flag": "",
    }.items():
        if column not in data:
            data[column] = default
    for column in ("time_s", "step_time_s", "voltage_v", "current_a", "capacity_ah", "temperature_c", "soc_pct"):
        data[column] = pd.to_numeric(data[column], errors="coerce")
    issues, cycle_rows, responses = [], [], []
    for _, part in data.groupby(["test_id", "cell_id", "cycle", "test_type"], sort=False, dropna=False):
        segments = _segments(part)
        first, last = part.iloc[0], part.iloc[-1]
        summary = {name: _value(first, name, "") for name in ("test_id", "cell_id", "cycle", "test_type", "source_path", "source_hash")}
        summary.update(source_row_start=_value(first, "source_row"), source_row_end=_value(last, "source_row"),
                       sample_count=int(len(part)), segment_count=len(segments))
        for segment in segments:
            times = _numeric(segment["time_s"])
            if not np.isfinite(times).all() or (len(times) > 1 and np.any(np.diff(times) <= 0)):
                _issue(issues, segment, "non_increasing_time", "Non-finite or non-increasing clock; affected capacity/energy omitted.", "error")
            if len(times) > 1 and np.any(np.diff(times) > cfg["max_gap_s"]):
                _issue(issues, segment, "sampling_gap", "Sampling gap exceeds max_gap_s; no interpolation/integration across gap.", "error")
            if len(segment) < 2:
                _issue(issues, segment, "insufficient_segment_points", "One observation cannot support integration or a timed response.", "info")
            if _direction(segment, cfg) in ("mixed", "unknown"):
                _issue(issues, segment, "invalid_segment_current", "Current is missing, crosses polarity, or includes unsegmented rest; segment excluded.", "error")
            if not np.isfinite(_numeric(segment["voltage_v"])).all():
                _issue(issues, segment, "invalid_voltage", "Non-finite voltage; energy and differential features omitted.", "error")
        charge, charge_valid, charge_flag = _summarize_direction(segments, "charge", cfg, issues)
        discharge, discharge_valid, discharge_flag = _summarize_direction(segments, "discharge", cfg, issues)
        summary.update({f"charge_{k}": v for k, v in charge.items()})
        summary.update({f"discharge_{k}": v for k, v in discharge.items()})
        flags = {str(charge_flag).lower(), str(discharge_flag).lower()}
        all_end_flags = set(part["end_flag"].fillna("").astype(str).str.strip().str.lower())
        complete_flags = {str(x).strip().lower() for x in cfg["complete_end_flags"]}
        incomplete_flags = {str(x).strip().lower() for x in cfg["incomplete_end_flags"]}
        invalid_segments = [s for s in segments if _direction(s, cfg) in ("mixed", "unknown")]
        invalid_current = bool(invalid_segments)
        global_clock_valid = _cycle_clock_valid(segments, cfg)
        if not global_clock_valid:
            _issue(issues, part, "cycle_clock_invalid", "Cycle time is non-finite, reverses, or repeats without an evidenced step end/start; completion is invalid.", "error")
        invalid_directions = set().union(*(_invalid_current_directions(s, cfg) for s in invalid_segments))
        for direction in invalid_directions:
            for name in ("capacity_ah", "capacity_integrated_ah", "capacity_measured_ah", "energy_wh", "mean_voltage_v", "cc_capacity_ah", "cv_capacity_ah", "duration_s"):
                summary[f"{direction}_{name}"] = np.nan
        active = [s for s in segments if _direction(s, cfg) in ("charge", "discharge")]
        starts = [_value(s.iloc[0], "step_time_s") for s in active]
        if any(_finite_number(t) and float(t) > cfg["start_time_tolerance_s"] for t in starts):
            start_coverage = "partial"
            _issue(issues, part, "leading_segment_samples_missing", "At least one active segment starts after step_time_s=0; leading capacity is unobserved and full completion is unknown.")
        elif starts and all(_finite_number(t) and abs(float(t)) <= cfg["start_time_tolerance_s"] for t in starts):
            start_coverage = "complete"
        else:
            start_coverage = "unknown"
        summary["capacity_start_coverage"] = start_coverage
        valid = charge_valid and discharge_valid and not invalid_current and global_clock_valid
        sequence = []
        final_segments = {}
        for segment in active:
            direction = _direction(segment, cfg)
            if not sequence or direction != sequence[-1]:
                sequence.append(direction)
            final_segments[direction] = segment
        sequence_verified = sequence == ["charge", "discharge"]
        endpoints_verified = all(_capacity_endpoint_verified(s, direction, cfg)
                                 for direction, s in final_segments.items())
        if valid and not sequence_verified:
            _issue(issues, part, "capacity_sequence_unverified", "Active steps do not form one charge-then-discharge pair; observed leg totals are retained but CE and retention are omitted.", "info")
        if valid and not endpoints_verified:
            _issue(issues, part, "capacity_endpoint_unverified", "A time/SOC/program-step end lacks a measured full-SOC endpoint; observed leg totals are retained but CE and retention are omitted.", "info")
        if not valid or all_end_flags & incomplete_flags:
            completion = "incomplete"
        elif start_coverage == "partial" or not sequence_verified or not endpoints_verified:
            completion = "unknown"
        elif (flags <= complete_flags and start_coverage == "complete") or cfg["assume_complete_bidirectional_cycles"]:
            completion = "complete"
        else:
            completion = "unknown"
        summary["completion_status"] = completion
        summary["capacity_complete"] = completion == "complete"
        summary["completion_assumed"] = bool(completion == "complete" and (not flags <= complete_flags or start_coverage != "complete"))
        if completion != "complete":
            _issue(issues, part, "cycle_completion_unverified" if completion == "unknown" else "cycle_incomplete",
                   "Full charge/discharge completion is not established; CE and retention are omitted.", "info")
        summary["coulombic_efficiency_pct"] = 100 * discharge["capacity_ah"] / charge["capacity_ah"] if completion == "complete" else np.nan
        summary["energy_efficiency_pct"] = (100 * discharge["energy_wh"] / charge["energy_wh"]
            if completion == "complete" and np.isfinite(charge["energy_wh"]) and charge["energy_wh"] > 0 else np.nan)
        temp = _numeric(part["temperature_c"])
        finite_temp = temp[np.isfinite(temp)]
        summary.update(temperature_min_c=float(np.min(finite_temp)) if len(finite_temp) else np.nan,
                       temperature_mean_c=float(np.mean(finite_temp)) if len(finite_temp) else np.nan,
                       temperature_max_c=float(np.max(finite_temp)) if len(finite_temp) else np.nan)
        qc_candidates = [_qc_time(s, cfg) for s in segments]
        qc_candidates = [x for x in qc_candidates if np.isfinite(x)]
        summary["qc_charge_time_10_80_s"] = qc_candidates[0] if len(qc_candidates) == 1 else np.nan
        if len(qc_candidates) > 1:
            _issue(issues, part, "qc_multiple_soc_windows", "Multiple independent charge segments span SOC 10–80; no single QC time selected.")
        for direction in ("charge", "discharge"):
            if summary[f"{direction}_curve_points"] < cfg["min_curve_points"]:
                _issue(issues, part, "curve_insufficient", f"{direction}: no sufficiently long monotonic CC curve; differential features omitted.", "info")
        pulse = _pulse_rows(segments, cfg, issues)
        responses.extend(pulse)
        summary["EoC_dchgR_60s_mohm"] = np.nan
        eoc = [r["resistance_mohm"] for r in pulse if r["eoc_discharge_eligible"] and np.isclose(r["pulse_time_s"], 60)]
        if len(eoc) == 1:
            summary["EoC_dchgR_60s_mohm"] = eoc[0]
        cycle_rows.append(summary)
    return (finalize_cycles(pd.DataFrame(cycle_rows), cfg),
            pd.DataFrame(responses, columns=PULSE_COLUMNS), pd.DataFrame(issues, columns=ISSUE_COLUMNS))
