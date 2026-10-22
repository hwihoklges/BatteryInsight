"""Optional measured CC/CV, SOC-pulse and rest indicators; no fitted predictors."""
from __future__ import annotations

from pathlib import Path
import json
from itertools import chain

import numpy as np
import pandas as pd

from .features import DEFAULTS
from .ingest import sha256_file

PACKS = {
    "cc_cv": {"label": "CC/CV 충전 특성", "description": "명시된 CC/CV 충전 용량 비율과 실측 CV 유지 시간입니다. 같은 계열에서 하나만 선택합니다."},
    "soc_dcir": {"label": "SOC별 펄스 저항", "description": "SOC 20/50/80 ±2%p에서 측정한 저항입니다. 충방전 방향과 측정 시간을 구분합니다."},
    "rest": {"label": "휴지 전압 변화", "description": "실측 휴지 전압의 부호 있는 변화입니다. 직전 충방전 방향, SOC, 측정 시간을 구분합니다."},
}
PULSE_TIMES = (0.1, 1., 10., 30., 60.)
SOC_CENTERS = (20., 50., 80.)
SOC_TOLERANCE = 2.


def _finite(value):
    try:
        return np.isfinite(float(value))
    except (ValueError, TypeError):
        return False


def _spec(metric_id, label, unit, pack, family, formula, revision):
    return {"metric_id": metric_id, "column": metric_id, "label": label, "unit": unit,
            "table": "workbench_pack", "role": "indicator", "pack_id": pack, "family": family,
            "selected_by_default": False, "definition_version": "measured_pack_v1", "source_revision": revision,
            "formula": formula, "weight": 1.0}


def _cv_durations(run, row):
    """Stream contiguous segments, retaining only one boundary sample per segment."""
    from .workbench_data import checked_artifact, silver_batches
    batches = iter(silver_batches(run, row, columns=["cycle", "time_s", "current_a", "mode", "segment_id", "step_id", "source_path", "source_hash", "source_row"],
                                 required_signals={"current_a", "mode", "time_s"}))
    first = next(batches, None)
    if first is None:
        return {}
    if not {"current_a", "mode", "time_s"}.issubset(first):
        # Voltage-only DB retention cannot reconstruct a measured CV duration.
        return {}
    snapshot = json.loads(checked_artifact(run, "config_snapshot.json").read_text(encoding="utf-8-sig"))
    settings = DEFAULTS | snapshot.get("features", {})
    totals, active = {}, None

    def finish(segment):
        if segment is None or not segment["has_cv"]:
            return
        target = totals[segment["cycle"]]
        if segment["negative"]:
            return
        if not segment["positive"] or not segment["valid_time"] or segment["count"] < 2:
            target["invalid"] = True
        elif segment["seen"]:
            target["seen"] = True
            target["duration"] += segment["duration"]

    for batch in chain([first], batches):
        boundary = batch.cycle.ne(batch.cycle.shift())
        for column in ("segment_id", "source_path", "step_id"):
            boundary |= batch[column].ne(batch[column].shift())
        for _, part in batch.groupby(boundary.cumsum(), sort=False):
            start, end = part.iloc[0], part.iloc[-1]
            cycle = start.cycle
            key = tuple(start[k] for k in ("cycle", "segment_id", "source_path", "step_id"))
            target = totals.setdefault(cycle, {"duration": 0., "seen": False, "invalid": False,
                                             "source_hash": start.get("source_hash"), "source_row_start": start.get("source_row")})
            target["source_row_end"] = end.get("source_row")
            if active is None or active["key"] != key:
                finish(active)
                active = {"key": key, "cycle": cycle, "count": 0, "has_cv": False, "positive": True,
                          "negative": True, "valid_time": True, "seen": False, "duration": 0., "last": None}
            mode = part.get("mode", pd.Series("", index=part.index)).fillna("").astype(str).str.upper().to_numpy()
            current = pd.to_numeric(part.current_a, errors="coerce").to_numpy(float)
            time = pd.to_numeric(part.time_s, errors="coerce").to_numpy(float)
            active["count"] += len(part)
            active["has_cv"] |= bool(np.any(mode == "CV"))
            active["positive"] &= bool(np.isfinite(current).all() and np.all(current > settings["min_current_a"]))
            active["negative"] &= bool(np.isfinite(current).all() and np.all(current < -settings["min_current_a"]))
            active["valid_time"] &= bool(np.isfinite(time).all())
            if active["last"] is not None:
                time = np.r_[active["last"][0], time]
                mode = np.concatenate(([active["last"][1]], mode))
            dt = np.diff(time)
            active["valid_time"] &= bool(np.isfinite(dt).all() and np.all(dt > 0) and np.all(dt <= settings["max_gap_s"]))
            selected = (mode[:-1] == "CV") & (mode[1:] == "CV")
            if np.any(selected):
                active["seen"] = True
                active["duration"] += float(dt[selected].sum())
            active["last"] = (time[-1], mode[-1])
    finish(active)
    return totals


def extract_metric_packs(run: Path, row: dict, *, cycles=None, source_revision=None):
    """Return available observations/specs plus explicit omissions for one registered test.

    Existing validated gold pulse values are selected, never refitted. Ambiguous
    repeated steps at a cycle/SOC/time are omitted rather than silently pooled.
    """
    from .workbench_data import MAX_TABLE_ROWS, checked_table, identity_rows, mapped_cycles
    run = Path(run)
    # Callers may share an already verified mapped table within one refresh.
    # Direct calls retain their original verification and identity checks.
    if cycles is None:
        cycles = mapped_cycles(run, row)
    observations, definitions, issues = [], {}, []
    revision = source_revision if source_revision is not None else sha256_file(Path(__file__))

    def issue(pack, reason, cycle=None, **extra):
        issues.append({"experiment_id": row["experiment_id"], "pack_id": pack, "reason": reason, "cycle": cycle, **extra})

    def add(cycle, life, value, spec, **source):
        if not _finite(value):
            return
        if len(observations) >= MAX_TABLE_ROWS:
            raise ValueError("Optional metric pack observation row limit exceeded")
        definitions[spec["metric_id"]] = spec
        observations.append({"experiment_id": row["experiment_id"], "metric_id": spec["metric_id"],
                             "cycle": float(cycle), "life_index": float(life), "observed_until_life": float(life),
                             "value": float(value), "unit": spec["unit"], **source})

    if cycles.empty:
        issue("all", "no_registered_test_cycles_with_life_index")
        return observations, [], issues
    mapping = dict(zip(cycles.cycle, cycles.life_index))
    fraction_spec = _spec("pack.cc_cv.cc_fraction_pct", "CC 충전 용량 비율(CC+CV 기준)", "%", "cc_cv",
                          "cv_amount", "100 * Q_CC / (Q_CC + Q_CV), both capacities explicitly available", revision)
    duration_spec = _spec("pack.cc_cv.cv_duration_s", "충전 CV 유지 시간", "s", "cc_cv", "cv_amount",
                          "Sum dt for consecutive positive-current samples both explicitly labelled CV; valid bounded clock", revision)
    for point in cycles.to_dict("records"):
        cc, cv = point.get("charge_cc_capacity_ah"), point.get("charge_cv_capacity_ah")
        if _finite(cc) and _finite(cv) and float(cc) >= 0 and float(cv) >= 0 and float(cc) + float(cv) > 0:
            add(point["cycle"], point["life_index"], 100 * float(cc) / (float(cc) + float(cv)), fraction_spec,
                source_hash=point.get("source_hash"), source_row_start=point.get("source_row_start"),
                source_row_end=point.get("source_row_end"))
        else:
            issue("cc_cv", "explicit_CC_and_CV_capacities_required", point["cycle"])

    # The original gold cycle table has no CV duration. Integrate time only in
    # explicit CV intervals of normalized silver, keeping full segments intact.
    durations = _cv_durations(run, row)
    if durations:
        for cycle, result in durations.items():
            if cycle not in mapping:
                continue
            if result["seen"] and not result["invalid"]:
                add(cycle, mapping[cycle], result["duration"], duration_spec,
                    **{k: result[k] for k in ("source_hash", "source_row_start", "source_row_end")})
            else:
                issue("cc_cv", "invalid_CV_clock_or_current" if result["invalid"] else "no_explicit_charge_CV_intervals", cycle)
    else:
        issue("cc_cv", "normalized_CV_samples_unavailable")

    pulses = identity_rows(checked_table(run, "pulses", optional=True), row, test_type=True)
    required = {"cycle", "feature_type", "direction", "pulse_time_s", "soc_pct"}
    if pulses.empty or not required.issubset(pulses):
        issue("soc_dcir", "validated_pulse_responses_unavailable")
        issue("rest", "validated_rest_responses_unavailable")
        return observations, list(definitions.values()), issues
    candidates = {}
    for point in pulses.to_dict("records"):
        cycle = point["cycle"]
        if cycle not in mapping:
            continue
        kind = point["feature_type"]
        if kind not in {"pulse", "rest"}:
            continue
        pack = "soc_dcir" if kind == "pulse" else "rest"
        seconds, soc = point.get("pulse_time_s"), point.get("soc_pct")
        if not _finite(seconds) or not any(np.isclose(float(seconds), x, rtol=0, atol=1e-9) for x in PULSE_TIMES):
            issue(pack, "unsupported_response_time", cycle)
            continue
        if not _finite(soc) or not 0 <= float(soc) <= 100:
            issue(pack, "explicit_SOC_required", cycle)
            continue
        nearest = min(SOC_CENTERS, key=lambda x: abs(float(soc) - x))
        if abs(float(soc) - nearest) > SOC_TOLERANCE:
            issue(pack, "SOC_outside_declared_bin", cycle, soc_pct=float(soc))
            continue
        direction = point["direction"]
        if kind == "pulse":
            value, unit = point.get("resistance_mohm"), "mΩ"
            if direction not in {"charge", "discharge"} or not _finite(value) or float(value) < 0:
                issue(pack, "valid_nonnegative_pulse_resistance_required", cycle)
                continue
            formula = "Existing validated gold resistance_mohm = 1000 * delta_voltage_v / delta_current_a"
        else:
            prior = point.get("baseline_current_a")
            if not _finite(prior) or abs(float(prior)) <= DEFAULTS["min_current_a"] or direction != "rest":
                issue(pack, "preceding_rest_current_direction_unknown", cycle)
                continue
            direction = "after_charge" if float(prior) > 0 else "after_discharge"
            value, unit = point.get("relaxation_delta_v"), "V"
            if not _finite(value):
                issue(pack, "validated_rest_voltage_change_required", cycle)
                continue
            formula = "Existing validated gold relaxation_delta_v = V(t) - V(first rest sample), signed"
        time_key = f"{float(seconds):g}".replace(".", "p")
        metric_id = f"pack.{pack}.{direction}.soc{nearest:g}.{time_key}s"
        direction_label = {"charge": "충전", "discharge": "방전", "after_charge": "충전 후", "after_discharge": "방전 후"}[direction]
        response_label = "펄스 저항" if kind == "pulse" else "휴지 전압 변화"
        label = f"{direction_label} · SOC {nearest:g}±{SOC_TOLERANCE:g}%p · {float(seconds):g}초 {response_label}"
        spec = _spec(metric_id, label, unit,
                     pack, metric_id, formula, revision)
        spec.update({"soc_center_pct": nearest, "soc_tolerance_pct": SOC_TOLERANCE,
                     "response_time_s": float(seconds), "direction": direction})
        candidates.setdefault((cycle, metric_id), []).append((point, spec, value))
    for (cycle, metric_id), rows in candidates.items():
        if len(rows) != 1:
            issue(rows[0][1]["pack_id"], "ambiguous_repeated_responses", cycle, metric_id=metric_id, count=len(rows))
            continue
        point, spec, value = rows[0]
        source_keys = ("source_path", "source_hash", "source_row_start", "source_row_end", "baseline_source_row",
                       "response_source_row_left", "response_source_row_right", "soc_pct")
        add(cycle, mapping[cycle], value, spec, **{k: point[k] for k in source_keys if k in point})
    for pack in ("soc_dcir", "rest"):
        if not any(spec["pack_id"] == pack for spec in definitions.values()):
            issue(pack, "no_unambiguous_responses_in_declared_SOC_time_bins")
    return observations, list(definitions.values()), issues
