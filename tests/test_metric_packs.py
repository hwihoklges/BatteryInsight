"""Optional packs preserve physical axes and omit unsupported measurements."""
import json

import pandas as pd
import pytest

from battery_insight.metric_packs import extract_metric_packs
from test_workbench_data import measured_run, seal_run


def pulse(row, **changes):
    return dict(cell_id=row["cell_id"], test_id=row["test_id"], test_type="Cycle", cycle=10.,
                feature_type="pulse", direction="discharge", pulse_time_s=1., soc_pct=50.,
                resistance_mohm=10., baseline_current_a=0., relaxation_delta_v=None,
                source_hash="hash", source_row_start=40, source_row_end=43,
                baseline_source_row=39, response_source_row_left=41, response_source_row_right=42) | changes


def write_pulses(run, values):
    pd.DataFrame(values).to_parquet(run / "gold" / "pulses.parquet", index=False)
    seal_run(run)


def test_cc_fraction_and_explicit_cv_time_share_one_family(tmp_path):
    _, run, row, raw, _ = measured_run(tmp_path)
    original = raw.read_bytes()
    observations, specs, issues = extract_metric_packs(run, row)
    values = {r["metric_id"]: r["value"] for r in observations if r["cycle"] == 10.}
    assert values == {"pack.cc_cv.cc_fraction_pct": 80., "pack.cc_cv.cv_duration_s": 2.}
    assert {spec["family"] for spec in specs} == {"cv_amount"}
    assert all(s["table"] == "workbench_pack" and s["role"] == "indicator" and not s["selected_by_default"] for s in specs)
    assert all(o["observed_until_life"] == o["life_index"] for o in observations)
    assert {i["reason"] for i in issues} >= {"validated_pulse_responses_unavailable", "validated_rest_responses_unavailable"}
    assert raw.read_bytes() == original


def test_missing_cv_capacity_is_omitted_not_imputed_zero(tmp_path):
    _, run, row, _, _ = measured_run(tmp_path)
    path = run / "gold" / "cycles.parquet"
    cycles = pd.read_parquet(path)
    cycles["charge_cv_capacity_ah"] = float("nan")
    cycles.to_parquet(path, index=False)
    seal_run(run)
    observations, _, issues = extract_metric_packs(run, row)
    assert all(o["metric_id"] != "pack.cc_cv.cc_fraction_pct" for o in observations)
    assert any(i["reason"] == "explicit_CC_and_CV_capacities_required" for i in issues)


def test_cv_duration_respects_saved_clock_settings(tmp_path):
    _, run, row, _, _ = measured_run(tmp_path)
    path = run / "config_snapshot.json"
    config = json.loads(path.read_text())
    config["features"] = {"max_gap_s": .5}
    path.write_text(json.dumps(config), encoding="utf-8")
    seal_run(run)
    observations, _, issues = extract_metric_packs(run, row)
    assert all(o["metric_id"] != "pack.cc_cv.cv_duration_s" for o in observations)
    assert any(i["reason"] == "invalid_CV_clock_or_current" for i in issues)


def test_cv_duration_is_identical_when_segments_span_stream_batches(tmp_path, monkeypatch):
    from battery_insight import workbench_data
    _, run, row, _, silver = measured_run(tmp_path)
    expected = extract_metric_packs(run, row)[0]
    frame = pd.read_parquet(silver)
    def fragmented(*args, **kwargs):
        for start in range(0, len(frame), 2):
            yield frame.iloc[start:start + 2].copy()
    monkeypatch.setattr(workbench_data, "silver_batches", fragmented)
    assert extract_metric_packs(run, row)[0] == expected


def test_dcir_preserves_soc_time_direction_and_test_identity(tmp_path):
    _, run, row, _, _ = measured_run(tmp_path)
    values = [pulse(row, soc_pct=soc, pulse_time_s=seconds, direction=direction, resistance_mohm=soc + seconds)
              for soc in (20., 50., 80.) for seconds in (.1, 1., 10., 30., 60.) for direction in ("charge", "discharge")]
    values += [pulse(row, test_id="other", resistance_mohm=999), pulse(row, test_type="RPT", resistance_mohm=999)]
    write_pulses(run, values)
    observations, specs, _ = extract_metric_packs(run, row)
    resistance = [o for o in observations if o["metric_id"].startswith("pack.soc_dcir.")]
    resistance_specs = [s for s in specs if s["pack_id"] == "soc_dcir"]
    assert len(resistance) == len(resistance_specs) == 30
    assert len({s["family"] for s in resistance_specs}) == 30
    assert all(o["unit"] == "mΩ" and o["value"] != 999 for o in resistance)
    assert all(o["baseline_source_row"] == 39 for o in resistance)


def test_ambiguous_missing_soc_outside_bin_and_negative_resistance_have_reasons(tmp_path):
    _, run, row, _, _ = measured_run(tmp_path)
    write_pulses(run, [pulse(row), pulse(row, source_row_start=100), pulse(row, soc_pct=None),
                       pulse(row, soc_pct=35.), pulse(row, soc_pct=20., resistance_mohm=-1.)])
    observations, _, issues = extract_metric_packs(run, row)
    assert not any(o["metric_id"].startswith("pack.soc_dcir.") for o in observations)
    assert {i["reason"] for i in issues} >= {"ambiguous_repeated_responses", "explicit_SOC_required", "SOC_outside_declared_bin", "valid_nonnegative_pulse_resistance_required"}


def test_rest_uses_measured_signed_delta_and_prior_direction(tmp_path):
    _, run, row, _, _ = measured_run(tmp_path)
    write_pulses(run, [pulse(row, feature_type="rest", direction="rest", pulse_time_s=60., baseline_current_a=1., relaxation_delta_v=-.03),
                       pulse(row, feature_type="rest", direction="rest", pulse_time_s=60., baseline_current_a=-1., relaxation_delta_v=.02),
                       pulse(row, feature_type="rest", direction="rest", pulse_time_s=10., baseline_current_a=0., relaxation_delta_v=.01)])
    observations, _, issues = extract_metric_packs(run, row)
    values = {o["metric_id"]: o["value"] for o in observations if o["metric_id"].startswith("pack.rest.")}
    assert values == {"pack.rest.after_charge.soc50.60s": -.03, "pack.rest.after_discharge.soc50.60s": .02}
    assert any(i["reason"] == "preceding_rest_current_direction_unknown" for i in issues)


def test_modified_gold_is_rejected_and_duplicate_cycle_mapping_is_rejected(tmp_path):
    _, run, row, _, _ = measured_run(tmp_path)
    write_pulses(run, [pulse(row)])
    path = run / "gold" / "pulses.parquet"
    pd.DataFrame([pulse(row, resistance_mohm=500.)]).to_parquet(path, index=False)
    with pytest.raises(ValueError, match="modified"):
        extract_metric_packs(run, row)
    seal_run(run)
    path = run / "gold" / "cycles.parquet"
    frame = pd.read_parquet(path)
    pd.concat([frame, frame.iloc[[0]]]).to_parquet(path, index=False)
    seal_run(run)
    with pytest.raises(ValueError, match="Ambiguous"):
        extract_metric_packs(run, row)
