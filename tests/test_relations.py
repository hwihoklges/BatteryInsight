import json

import numpy as np
import pandas as pd
import pytest

from battery_insight.relations import build_relations


def fixture_data(n=6, n_metrics=3):
    experiments = pd.DataFrame([{"experiment_id": f"e{i}", "label": f"Cell {i}", "cell_id": f"c{i}",
                                 "cohort": "A", "protocol": "p1", "chemistry": "NMC"} for i in range(n)])
    metrics = [{"metric_id": f"m{k}", "label": f"Metric {k}", "unit": "V", "family": f"f{k}"} for k in range(n_metrics)]
    observations = pd.DataFrame([{"experiment_id": f"e{i}", "life_index": float(life),
                                  "metric_id": f"m{k}", "value": (i + 1) * (k + 1) + life / 100,
                                  "unit": "V"} for i in range(n) for k in range(n_metrics) for life in (1, 2, 3)])
    settings = {"life_min": 1, "life_max": 3}
    return experiments, observations, metrics, settings


def feature(result, entity, metric):
    return next(f for n in result["nodes"] if n["experiment_id"] == entity for f in n["features"] if f["metric_id"] == metric)


def test_network_is_descriptive_and_json_safe():
    result = build_relations(*fixture_data())
    assert len(result["nodes"]) == 6
    assert len(result["edges"]) == 15
    assert result["metadata"]["similarity_is_correlation"] is False
    assert result["metadata"]["missing_values_imputed"] is False
    edge = result["edges"][0]
    assert edge["coverage"] == 1
    assert edge["shared_metrics"] == 3
    assert all(c["raw_delta"] > 0 for c in edge["contributors"])
    assert 0 < edge["similarity"] < 1
    assert all(c["rho"] == pytest.approx(1) for c in result["metric_correlations"])
    json.dumps(result, allow_nan=False)


def test_no_interpolation_or_outside_window_endpoint_coverage():
    experiments, observations, metrics, settings = fixture_data()
    observations.loc[(observations.experiment_id == "e0") & (observations.life_index == 1), "life_index"] = 0
    result = build_relations(experiments, observations, metrics, settings)
    assert feature(result, "e0", "m0")["value"] is None
    assert feature(result, "e0", "m0")["status"] == "insufficient_points"
    assert all(edge["source"] != "e0" and edge["target"] != "e0" for edge in result["edges"])
    assert result["metadata"]["outside_window_rows_ignored"] == 3


def test_full_window_coverage_required_even_with_many_points():
    experiments, observations, metrics, settings = fixture_data()
    observations.loc[(observations.experiment_id == "e0") & (observations.life_index == 1), "life_index"] = 1.1
    result = build_relations(experiments, observations, metrics, settings)
    assert feature(result, "e0", "m0")["status"] == "incomplete_window"
    settings["window_tolerance"] = 0.11
    result = build_relations(experiments, observations, metrics, settings)
    assert feature(result, "e0", "m0")["status"] == "valid"


def test_nonfinite_values_are_missing_and_never_zero_imputed():
    experiments, observations, metrics, settings = fixture_data()
    observations.loc[(observations.experiment_id == "e0") & (observations.metric_id == "m0"), "value"] = np.inf
    result = build_relations(experiments, observations, metrics, settings)
    assert feature(result, "e0", "m0")["value"] is None
    assert feature(result, "e0", "m0")["scaled"] is None
    assert result["metadata"]["nonfinite_values_treated_as_missing"] == 3
    assert all(edge["source"] != "e0" for edge in result["edges"])


def test_missing_fraction_denominator_is_full_informative_cohort():
    experiments, observations, metrics, settings = fixture_data(n_metrics=4)
    observations = observations.loc[~((observations.experiment_id == "e0") & observations.metric_id.isin(["m2", "m3"]))]
    settings.update(min_overlap=2, min_overlap_fraction=0.6)
    result = build_relations(experiments, observations, metrics, settings)
    pair = next(p for p in result["excluded_pairs"] if p["source"] == "e0")
    assert pair["coverage"] == 0.5
    assert pair["reason"] == "insufficient_metric_coverage"
    assert pair["missing_metrics"] == ["m2", "m3"]


@pytest.mark.parametrize("mutation,match", [
    ("duplicate", "Duplicate experiment_id \\+ life_index"),
    ("units", "Unit mismatch"),
    ("unknown_metric", "Unknown metric_id"),
    ("unknown_entity", "Unknown experiment_id"),
    ("bad_life", "life_index must be finite"),
])
def test_invalid_observations_fail_explicitly(mutation, match):
    experiments, observations, metrics, settings = fixture_data()
    if mutation == "duplicate":
        observations = pd.concat([observations, observations.iloc[[0]]], ignore_index=True)
    elif mutation == "units":
        observations.loc[0, "unit"] = "mV"
    elif mutation == "unknown_metric":
        observations.loc[0, "metric_id"] = "unconfigured"
    elif mutation == "unknown_entity":
        observations.loc[0, "experiment_id"] = "unregistered"
    else:
        observations.loc[0, "life_index"] = np.nan
    with pytest.raises(ValueError, match=match):
        build_relations(experiments, observations, metrics, settings)


@pytest.mark.parametrize("mutation,match", [("family", "one representative"), ("unit", "Unknown unit"), ("role", "Only indicator"), ("id", "Duplicate metric_id")])
def test_metric_selection_validation(mutation, match):
    experiments, observations, metrics, settings = fixture_data()
    if mutation == "family":
        metrics[1]["family"] = metrics[0]["family"]
    elif mutation == "unit":
        metrics[0]["unit"] = "needs_review"
    elif mutation == "role":
        metrics[0]["role"] = "target"
    else:
        metrics[1]["metric_id"] = metrics[0]["metric_id"]
    with pytest.raises(ValueError, match=match):
        build_relations(experiments, observations, metrics, settings)


def test_change_uses_chronological_last_minus_first():
    experiments, observations, metrics, settings = fixture_data()
    observations["value"] = observations.life_index.map({1: 10, 2: 30, 3: 5})
    settings["summary"] = "change"
    result = build_relations(experiments, observations.sample(frac=1, random_state=1), metrics, settings)
    assert feature(result, "e0", "m0")["value"] == -5
    assert not result["edges"]  # All change summaries are constant across experiments.


def test_snapshot_reads_exactly_one_explicit_point_and_does_not_use_nearest_neighbors():
    experiments, observations, metrics, settings = fixture_data()
    settings.update(summary="snapshot", life_min=2, life_max=2)
    observations = observations.loc[~((observations.experiment_id == "e0") & (observations.life_index == 2))]
    result = build_relations(experiments, observations, metrics, settings)
    assert result["settings"]["min_points"] == 1
    assert feature(result, "e0", "m0")["value"] is None
    assert feature(result, "e0", "m0")["status"] == "no_finite_values"
    assert feature(result, "e1", "m0")["value"] == pytest.approx(2.02)
    assert feature(result, "e1", "m0")["valid_count"] == 1
    assert all("e0" not in (edge["source"], edge["target"]) for edge in result["edges"])


@pytest.mark.parametrize("changes,match", [({"life_min": 1, "life_max": 2}, "life_min == life_max"),
                                           ({"window_tolerance": 0.1}, "no nearest-point"),
                                           ({"min_points": 0}, "integer >= 1")])
def test_snapshot_requires_a_strict_single_point_contract(changes, match):
    experiments, observations, metrics, settings = fixture_data()
    settings.update(summary="snapshot", life_min=2, life_max=2)
    settings.update(changes)
    with pytest.raises(ValueError, match=match):
        build_relations(experiments, observations, metrics, settings)


def test_constants_do_not_inflate_overlap_or_spearman():
    experiments, observations, metrics, settings = fixture_data()
    observations.loc[observations.metric_id == "m2", "value"] = 7
    settings["min_overlap"] = 2
    result = build_relations(experiments, observations, metrics, settings)
    assert result["cohorts"][0]["informative_metrics"] == ["m0", "m1"]
    assert result["cohorts"][0]["excluded_metrics"] == [{"metric_id": "m2", "reason": "constant"}]
    assert all(edge["shared_metrics"] == 2 and edge["coverage"] == 1 for edge in result["edges"])
    assert all(c["rho"] is None and c["status"] == "constant" for c in result["metric_correlations"] if "m2" in (c["left_metric"], c["right_metric"]))


def test_cohorts_are_never_pooled_for_edges_or_correlations():
    experiments, observations, metrics, settings = fixture_data()
    experiments.loc[experiments.experiment_id.isin(["e3", "e4", "e5"]), "cohort"] = "B"
    result = build_relations(experiments, observations, metrics, settings)
    cohorts = dict(zip(experiments.experiment_id, experiments.cohort))
    assert len(result["edges"]) == 6
    assert all(cohorts[e["source"]] == cohorts[e["target"]] for e in result["edges"])
    assert all(c["n_cells"] == 3 and c["rho"] is None for c in result["metric_correlations"])


@pytest.mark.parametrize("condition", ["chemistry", "protocol"])
def test_incompatible_cohort_metadata_is_rejected(condition):
    experiments, observations, metrics, settings = fixture_data()
    experiments.loc[0, condition] = "different"
    with pytest.raises(ValueError, match=f"mixes {condition}"):
        build_relations(experiments, observations, metrics, settings)


def test_repeated_cell_tests_cannot_satisfy_minimum_cell_count():
    experiments, observations, metrics, settings = fixture_data(n=6)
    experiments["cell_id"] = ["c0", "c0", "c1", "c1", "c2", "c2"]
    result = build_relations(experiments, observations, metrics, settings)
    assert len(result["edges"]) == 15  # Network entities remain cell-specific tests.
    assert result["cohorts"][0]["repeated_cell_ids"] == ["c0", "c1", "c2"]
    assert all(c["n_entities"] == 6 and c["n_cells"] == 3 and c["rho"] is None for c in result["metric_correlations"])


def test_correlations_can_be_skipped_without_changing_graph_or_cell_identity_guards(monkeypatch):
    import battery_insight.relations as module

    data = fixture_data()
    data[0]["cell_id"] = ["c0", "c0", "c1", "c1", "c2", "c2"]
    expected = build_relations(*data)
    expected["metric_correlations"] = []

    def unexpected(*args, **kwargs):
        raise AssertionError("Disabled correlation must not perform cell aggregation")

    monkeypatch.setattr(module, "_cell_correlations", unexpected)
    assert build_relations(*data, compute_correlations=False) == expected
    with pytest.raises(ValueError, match="compute_correlations"):
        build_relations(*data, compute_correlations="false")


def test_cell_medians_preserve_complementary_missing_tests_and_entity_pair_counts():
    experiments, observations, metrics, settings = fixture_data(n=12)
    experiments["cell_id"] = [f"c{i // 2}" for i in range(12)]
    for i in range(12):
        metric = "m0" if i % 2 else "m1"
        observations.loc[(observations.experiment_id == f"e{i}") & (observations.metric_id == metric), "value"] = np.nan
    result = build_relations(experiments, observations, metrics, settings)
    pair = next(row for row in result["metric_correlations"] if (row["left_metric"], row["right_metric"]) == ("m0", "m1"))
    assert pair["n_entities"] == 0
    assert pair["n_cells"] == 6
    assert pair["rho"] == pytest.approx(1.0)


def test_missing_cell_identity_does_not_imply_independent_cells():
    experiments, observations, metrics, settings = fixture_data()
    experiments = experiments.drop(columns="cell_id")
    result = build_relations(experiments, observations, metrics, settings)
    assert all(c["n_cells"] == 0 and c["excluded_entities_without_cell_id"] == 6 and c["rho"] is None for c in result["metric_correlations"])


def test_spearman_is_across_cell_summaries_and_preserves_sign():
    experiments, observations, metrics, settings = fixture_data()
    observations.loc[observations.metric_id == "m1", "value"] *= -1
    result = build_relations(experiments, observations, metrics, settings)
    pair = next(c for c in result["metric_correlations"] if c["left_metric"] == "m0" and c["right_metric"] == "m1")
    assert pair["rho"] == pytest.approx(-1)
    assert pair["n_cells"] == 6
    assert pair["n_entities"] == 6


def test_include_false_disables_entity_metric_and_observation():
    experiments, observations, metrics, settings = fixture_data()
    experiments["include"] = experiments.experiment_id != "e0"
    observations["include"] = True
    observations.loc[(observations.experiment_id == "e1") & (observations.metric_id == "m0"), "include"] = False
    metrics[2]["include"] = False
    settings["min_overlap"] = 2
    result = build_relations(experiments, observations, metrics, settings)
    assert len(result["nodes"]) == 5
    assert len(result["metrics"]) == 2
    assert feature(result, "e1", "m0")["status"] == "no_finite_values"


def test_fallback_scale_handles_sparse_outlier_without_zero_mad_division():
    experiments, observations, metrics, settings = fixture_data(n=6)
    observations["value"] = np.where(observations.experiment_id == "e5", 10, 1)
    result = build_relations(experiments, observations, metrics, settings)
    assert all(s["method"] == "median_population_std" for s in result["cohorts"][0]["scales"])
    assert result["edges"][0]["similarity"] == 1


@pytest.mark.parametrize("settings", [
    {}, {"life_min": 3, "life_max": 1}, {"life_min": 1, "life_max": 3, "summary": "maximum"},
    {"life_min": 1, "life_max": 3, "min_overlap_fraction": 0},
    {"life_min": 1, "life_max": 3, "min_points": 1},
    {"life_min": 1, "life_max": 3, "metric_spearman_min_n": 2},
    {"life_min": 1, "life_max": 3, "window_tolerance": 1},
])
def test_invalid_config_fails(settings):
    experiments, observations, metrics, _ = fixture_data()
    with pytest.raises(ValueError):
        build_relations(experiments, observations, metrics, settings)


def test_empty_pending_registry_returns_empty_dashboard_payload():
    experiments, observations, metrics, settings = fixture_data()
    result = build_relations(experiments.iloc[:0], observations.iloc[:0], metrics, settings)
    assert result["nodes"] == []
    assert result["edges"] == []
    assert result["cohorts"] == []
    assert result["metric_correlations"] == []
    json.dumps(result, allow_nan=False)


def test_registered_entities_without_observations_remain_visible_with_missing_features():
    experiments, observations, metrics, settings = fixture_data()
    result = build_relations(experiments, observations.iloc[:0], metrics, settings)
    assert len(result["nodes"]) == 6
    assert all(node["available_metrics"] == 0 for node in result["nodes"])
    assert all(feature["value"] is None for node in result["nodes"] for feature in node["features"])
    assert result["edges"] == []


def test_order_of_observation_rows_does_not_change_results():
    experiments, observations, metrics, settings = fixture_data()
    original = build_relations(experiments, observations, metrics, settings)
    shuffled = build_relations(experiments.sample(frac=1, random_state=21), observations.sample(frac=1, random_state=12), metrics, settings)
    assert original == shuffled


def test_mixed_chemistry_requires_explicit_opt_in_and_preserves_protocol_guard():
    experiments, observations, metrics, settings = fixture_data()
    experiments.loc[0, "chemistry"] = "LFP"
    settings["allow_mixed_chemistry"] = True
    result = build_relations(experiments, observations, metrics, settings)
    assert len(result["edges"]) == 15
    assert result["metadata"]["allow_mixed_chemistry"] is True
    experiments.loc[0, "protocol"] = "different"
    with pytest.raises(ValueError, match="mixes protocol"):
        build_relations(experiments, observations, metrics, settings)


def test_mixed_chemistry_string_is_not_implicit_permission():
    experiments, observations, metrics, settings = fixture_data()
    settings["allow_mixed_chemistry"] = "true"
    with pytest.raises(ValueError, match="explicit boolean"):
        build_relations(experiments, observations, metrics, settings)
