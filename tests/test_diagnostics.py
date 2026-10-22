"""Synthetic behavioral tests: no customer files or trained models are used."""
import json

import numpy as np
import pandas as pd
import pytest

from battery_insight.diagnostics import compare_cells, diagnose, life_summary


def cycles(capacities, *, cell="A", test="run1", kind="Cycle", numbers=None):
    n = len(capacities)
    return pd.DataFrame({
        "cell_id": [cell] * n, "test_id": [test] * n, "test_type": [kind] * n,
        "cycle": numbers if numbers is not None else range(1, n + 1),
        "completion_status": ["complete"] * n, "capacity_complete": [True] * n,
        "discharge_capacity_ah": capacities,
        "capacity_retention_pct": np.asarray(capacities) * 100,
        "coulombic_efficiency_pct": [99.5] * n,
    })


def diagnostic_config(**kwargs):
    settings = {
        "baseline_window": 3, "min_baseline_cycles": 3,
        "metrics": {"discharge_capacity_ah": {"absolute_floor": 0.01, "direction": "low"}},
    }
    settings.update(kwargs)
    return {"diagnostics": settings}


def test_zero_mad_uses_absolute_floor_and_outputs_reviewable_evidence():
    result = diagnose(cycles([1, 1, 1, 0.8]), diagnostic_config())
    last = result.iloc[-1]
    assert last.status == "flagged"
    assert last.baseline_mad == 0
    assert last.robust_score == pytest.approx(-20)
    assert last.review_status == "pending"
    assert "후보" in last.candidate_causes
    assert json.loads(last.evidence)["baseline_cycle_numbers"] == [1, 2, 3]
    assert result.iloc[0].status == "baseline_reference"


def test_unknown_initial_cycle_is_not_replaced_with_a_later_baseline():
    data = cycles([1, 1, 1, 0.8, 0.7])
    data.loc[1, "completion_status"] = "unknown"
    result = diagnose(data, diagnostic_config())
    assert result.iloc[1].status == "excluded_from_screening"
    assert result.iloc[-1].status == "insufficient_baseline"
    assert result.iloc[-1].baseline_n == 2
    assert result.iloc[-1].baseline_cutoff_cycle == 3


def test_missing_completion_is_not_assumed_healthy():
    data = cycles([1, 1, 1, 0.5]).drop(columns=["completion_status", "capacity_complete"])
    result = diagnose(data, diagnostic_config())
    assert set(result.status) == {"excluded_from_screening"}
    assert set(result.baseline_n) == {0}
    summary = life_summary(data, {})
    assert summary.iloc[0].life_status == "insufficient_retention_data"


def test_baselines_never_mix_cell_test_or_test_type():
    a = cycles([1, 1, 1, 1])
    b = cycles([10, 10, 10, 10], cell="B")
    c = cycles([5, 5, 5, 5], test="run2")
    d = cycles([2, 2, 2, 2], kind="RPT")
    result = diagnose(pd.concat([a, b, c, d], ignore_index=True), diagnostic_config())
    assert "flagged" not in set(result.status)
    assert set(result.baseline_median) == {1, 2, 5, 10}


def test_duplicate_cycle_identity_is_not_used_in_baseline():
    data = cycles([1, 1, 1, 0.7], numbers=[1, 1, 2, 3])
    result = diagnose(data, diagnostic_config())
    duplicates = result[result.cycle == 1]
    assert set(duplicates.status) == {"excluded_from_screening"}
    assert all("duplicate_cycle_identity" in reason for reason in duplicates.reasons)
    assert result.iloc[-1].baseline_n == 2


def test_explicit_limit_can_flag_with_insufficient_baseline():
    config = diagnostic_config(metrics={
        "ce_pct": {"absolute_floor": 0.2, "lower": 95, "unit": "%"}
    })
    data = cycles([1, 1]).rename(columns={"coulombic_efficiency_pct": "ce_pct"})
    data.loc[1, "ce_pct"] = 90
    result = diagnose(data, config)
    assert result.iloc[-1].status == "flagged"
    assert "below_configured_limit" in result.iloc[-1].reasons
    assert "insufficient_baseline" in result.iloc[-1].reasons


@pytest.mark.parametrize("floor", [0, -1, float("nan")])
def test_invalid_absolute_floor_is_rejected(floor):
    with pytest.raises(ValueError, match="absolute_floor"):
        diagnose(cycles([1, 1, 1]), diagnostic_config(metrics={
            "discharge_capacity_ah": {"absolute_floor": floor}
        }))


def test_eol_requires_consecutive_confirmed_crossings_and_preserves_censoring():
    config = {"life": {"eol_consecutive_cycles": 3, "eol_threshold_pct": 80}}
    observed = life_summary(cycles([1, 0.9, 0.8, 0.79, 0.78]), config).iloc[0]
    assert observed.observed_eol
    assert not observed.censored
    assert observed.eol_cycle == 3
    assert observed.eol_confirmed_cycle == 5
    unfinished = life_summary(cycles([1, 0.79, 0.78, 0.9, 0.77]), config).iloc[0]
    assert unfinished.censored
    assert np.isnan(unfinished.eol_cycle)
    assert unfinished.last_observed_cycle == 5


def test_cycle_gap_or_unknown_completion_breaks_eol_streak():
    gap = life_summary(cycles([1, 0.79, 0.78, 0.77], numbers=[1, 2, 4, 5]), {}).iloc[0]
    assert gap.censored
    data = cycles([1, 0.79, 0.78, 0.77])
    data.loc[2, "completion_status"] = "unknown"
    assert life_summary(data, {}).iloc[0].censored


def test_early_features_are_invariant_to_future_cycles():
    config = {"life": {"cutoff_cycle": 3, "min_early_cycles": 3}}
    original = cycles([1, 0.99, 0.98, 0.9, 0.8, 0.7])
    changed = original.copy()
    changed.loc[changed.cycle > 3, "discharge_capacity_ah"] = 100
    changed.loc[changed.cycle > 3, "capacity_retention_pct"] = 10000
    changed.loc[changed.cycle > 3, "coulombic_efficiency_pct"] = 1
    a = life_summary(original, config)
    b = life_summary(changed, config)
    early = [name for name in a if name.startswith("early_")]
    pd.testing.assert_frame_equal(a[early], b[early])
    assert a.iloc[0].early_observed_until_cycle == 3
    assert a.iloc[0].early_capacity_slope_ah_per_cycle == pytest.approx(-0.01)


def test_only_explicit_cycle_and_insitu_types_are_life_tests():
    data = pd.concat([
        cycles([1, 0.9, 0.8], kind="Cycle"),
        cycles([1, 0.9, 0.8], kind="Insitu", test="run2"),
        cycles([1, 0.9, 0.8], kind="QC"),
        cycles([1, 0.9, 0.8], kind="RPT"),
    ], ignore_index=True)
    result = life_summary(data, {})
    assert set(result.test_type) == {"Cycle", "Insitu"}


def test_missing_comparison_metadata_stays_unknown_even_on_both_cells():
    registry = pd.DataFrame({"cell_id": ["A", "B"], "pressure_mpa": [0.7, 1.0]})
    result = compare_cells(registry, "A", "B", "pressure_mpa", ["pressure_mpa", "temperature_c"])
    assert result["controlled"] is None
    assert result["missing_variables"] == ["temperature_c"]
    assert "single_cell_pair_no_replication" in result["warnings"]


def test_controlled_pair_and_confounder_are_distinguished():
    registry = pd.DataFrame({
        "cell_id": ["A", "B", "C"], "pressure_mpa": [0.7, 1.0, 1.0],
        "temperature_c": [45, 45, 60],
    })
    variables = ["pressure_mpa", "temperature_c"]
    controlled = compare_cells(registry, "A", "B", "pressure_mpa", variables)
    confounded = compare_cells(registry, "A", "C", "pressure_mpa", variables)
    assert controlled["controlled"] is True
    assert controlled["status"] == "controlled_metadata_only"
    assert confounded["controlled"] is False
    assert confounded["confounders"] == ["temperature_c"]


def test_ambiguous_cell_mapping_cannot_create_a_controlled_comparison():
    registry = pd.DataFrame({"cell_id": ["A", "A", "B"], "pressure_mpa": [0.7, 0.7, 1.0]})
    result = compare_cells(registry, "A", "B", "pressure_mpa", ["pressure_mpa"])
    assert result["controlled"] is None
    assert result["registry_matches"] == {"A": 2, "B": 1}


def test_empty_inputs_return_schema():
    assert "evidence" in diagnose(pd.DataFrame(), {})
    assert "censored" in life_summary(pd.DataFrame(), {})


def test_scoped_config_is_equivalent_to_full_pipeline_config():
    data = cycles([1, 1, 1, 0.8])
    config = diagnostic_config()
    pd.testing.assert_frame_equal(diagnose(data, config), diagnose(data, config["diagnostics"]))
    life = {"cutoff_cycle": 2, "eol_consecutive_cycles": 1}
    pd.testing.assert_frame_equal(life_summary(data, {"life": life}), life_summary(data, life))


def test_mixed_life_types_use_life_index_without_rpt_reset():
    data = cycles([1, 1, 0.79, 0.78, 1, 0.77])
    data["test_type"] = ["Cycle", "RPT", "Insitu", "Cycle", "RPT", "Insitu"]
    data["life_index"] = [1, np.nan, 2, 3, np.nan, 4]
    result = life_summary(data, {"cutoff_cycle": 3}).iloc[0]
    assert result.observed_eol
    assert result.test_type == "Cycle/Insitu"
    assert result.eol_cycle == 3
    assert result.eol_confirmed_cycle == 6
    assert result.eol_life_index == 2
    assert result.eol_confirmed_life_index == 4
    assert result.early_cycle_count == 3
    assert result.early_observed_until_cycle == 4
    assert result.early_observed_until_life_index == 3
    assert result.early_cutoff_basis == "life_index"


def test_invalid_supplied_life_index_is_not_replaced_by_raw_cycle():
    data = cycles([1, 0.79, 0.78, 0.77])
    data["life_index"] = [1, 2, np.nan, 4]
    result = life_summary(data, {}).iloc[0]
    assert result.censored
    assert result.eligible_cycles == 3


def test_electrochemical_default_metrics_are_screened_with_calibration_notice():
    data = cycles([1, 1, 1, 1, 1, 1])
    data["EoC_dchgR_60s_mohm"] = [10, 10, 10, 10, 10, 20]
    data["discharge_dvdq_low_soc_v_per_ah"] = [0.1, 0.1, 0.1, 0.1, 0.1, 0.2]
    data["discharge_dvdq_low_soc_proxy_v_per_ah"] = [0.1, 0.1, 0.1, 0.1, 0.1, 0.2]
    data["discharge_dqdv_peak_voltage_v"] = [3.4, 3.4, 3.4, 3.4, 3.4, 3.5]
    result = diagnose(data, {})
    flags = result[result.status == "flagged"]
    assert set(flags.metric) == {
        "EoC_dchgR_60s_mohm", "discharge_dvdq_low_soc_v_per_ah",
        "discharge_dvdq_low_soc_proxy_v_per_ah", "discharge_dqdv_peak_voltage_v",
    }
    assert all(json.loads(item)["calibration_status"] == "calibration_required" for item in flags.evidence)
    proxy = flags[flags.metric.str.contains("proxy")].iloc[0]
    assert json.loads(proxy.evidence)["proxy_metric"] is True
