import copy
import json

import numpy as np
import pytest

from battery_insight.workbench_analysis import analyze_bundle


def bundle_fixture(n=6):
    experiments = [{"experiment_id": f"e{i}", "label": f"Cell {i}", "cell_id": f"c{i}", "test_id": "T1",
                    "cohort": "A", "protocol": "P1", "chemistry": "NMC", "test_type": "Cycle", "temperature_c": 25,
                    "design_group": "DG", "design_id": "D1", "batch_id": "B1",
                    "active": "NMC811", "loading": float(4 + i / 10), "active_pct": 96.0, "binder_pct": 4.0} for i in range(n)]
    metrics = [{"metric_id": f"m{k}", "label": f"Metric {k}", "unit": "V", "family": f"f{k}"} for k in range(3)]
    observations = [{"experiment_id": f"e{i}", "metric_id": f"m{k}", "life_index": life,
                     "value": (i + 1) * (k + 1) + life * (i + 1) / 100, "unit": "V"}
                    for i in range(n) for k in range(3) for life in range(1, 7)]
    design_fields = [{"field_id": "active", "label": "Active identity", "kind": "categorical", "group": "active"},
                     {"field_id": "loading", "label": "Loading", "kind": "numeric", "unit": "mAh/cm2", "scale": 1, "group": "loading"},
                     {"field_id": "composition", "label": "Composition", "kind": "composition", "unit": "wt%",
                      "composition_basis": "electrode_total", "components": ["active_pct", "binder_pct"], "group": "composition"}]
    return {"experiments": experiments, "metrics": metrics, "observations": observations, "design_fields": design_fields,
            "comparison": {"life_min": 1, "life_max": 3, "min_points": 3}, "design_settings": {"min_fields": 3}, "base_payload": {}}


def feature(graph, entity, metric):
    return next(f for node in graph["nodes"] if node["experiment_id"] == entity for f in node["features"] if f["metric_id"] == metric)


def pair(graph, a="e0", b="e1"):
    return next(edge for edge in graph["edges"] if edge["source"] == a and edge["target"] == b)


def baseline(bundle):
    payload = analyze_bundle(bundle, {"enabled_modules": ["network"]})
    return {"reference_id": "r1", "summary": "median", "metrics": bundle["metrics"],
            "scales": {cohort["cohort"]: cohort["scales"] for cohort in payload["graphs"]["electrochem"]["cohorts"]}}


def test_default_analysis_is_json_safe_and_uses_two_independent_graphs():
    payload = analyze_bundle(bundle_fixture(), {})
    assert len(payload["graphs"]["electrochem"]["edges"]) == 15
    assert len(payload["graphs"]["design"]["edges"]) == 15
    assert len(payload["cross_pairs"]) == 15
    assert payload["controlled_pairs"] == []
    assert payload["metadata"]["controlled"]["status"] == "target_not_selected"
    assert payload["metadata"]["scores_combined_or_subtracted"] is False
    json.dumps(payload, allow_nan=False)


def test_explicit_empty_selections_keep_nodes_without_edges():
    payload = analyze_bundle(bundle_fixture(), {"metric_ids": [], "design_field_ids": []})
    for graph in payload["graphs"].values():
        assert len(graph["nodes"]) == 6
        assert graph["metrics"] == [] and graph["edges"] == []
    assert payload["cross_pairs"] == [] and payload["trajectories"] == [] and payload["replicates"] == []


def test_metric_selection_recomputes_edge_distance_and_never_reuses_base_payload():
    bundle = bundle_fixture()
    for point in bundle["observations"]:
        if point["metric_id"] == "m2" and point["experiment_id"] == "e1":
            point["value"] += 100
    all_metrics = analyze_bundle(bundle, {"enabled_modules": ["network"]})
    bundle["base_payload"] = all_metrics
    selected = analyze_bundle(bundle, {"enabled_modules": ["network"], "metric_ids": ["m0"], "min_overlap": 1})
    assert pair(selected["graphs"]["electrochem"])["shared_metrics"] == 1
    assert pair(selected["graphs"]["electrochem"])["similarity"] != pair(all_metrics["graphs"]["electrochem"])["similarity"]


def test_design_selection_recomputes_distance():
    bundle = bundle_fixture()
    all_fields = analyze_bundle(bundle, {"enabled_modules": ["network"]})
    selected = analyze_bundle(bundle, {"enabled_modules": ["network"], "design_field_ids": ["loading"]})
    assert pair(selected["graphs"]["design"])["shared_metrics"] == 1
    assert pair(selected["graphs"]["design"])["similarity"] < pair(all_fields["graphs"]["design"])["similarity"]


def test_optional_pack_is_off_by_default_but_explicitly_selectable():
    bundle = bundle_fixture()
    bundle["metrics"].append({"metric_id": "optional", "label": "Optional", "unit": "V", "family": "optional", "selected_by_default": False})
    bundle["observations"].extend([dict(point, metric_id="optional") for point in bundle["observations"] if point["metric_id"] == "m0"])
    assert "optional" not in analyze_bundle(bundle, {})["options"]["metric_ids"]
    explicit = analyze_bundle(bundle, {"metric_ids": ["optional"]})
    assert explicit["options"]["metric_ids"] == ["optional"]
    assert len(explicit["graphs"]["electrochem"]["edges"]) == 15


def test_off_modules_return_empty_analysis_arrays():
    payload = analyze_bundle(bundle_fixture(), {"enabled_modules": []})
    assert payload["graphs"]["electrochem"]["edges"]
    assert not payload["graphs"]["electrochem"]["metric_correlations"]
    for key in ("cross_pairs", "controlled_pairs", "trajectories", "window_graphs", "replicates", "evidence"):
        assert payload[key] == []


def test_identical_trajectory_windows_reuse_graph_without_clearing_main_correlation(monkeypatch):
    import battery_insight.workbench_analysis as module

    original, calls = module.build_relations, []

    def tracked(*args, **kwargs):
        calls.append((args[3]["life_min"], args[3]["life_max"], kwargs["compute_correlations"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "build_relations", tracked)
    payload = analyze_bundle(bundle_fixture(), {"windows": [
        {"label": "Current A", "life_min": 1, "life_max": 3},
        {"label": "Current B", "life_min": 1, "life_max": 3},
        {"label": "Later A", "life_min": 4, "life_max": 6},
        {"label": "Later B", "life_min": 4, "life_max": 6}]})
    assert calls == [(1.0, 3.0, True), (4.0, 6.0, False)]
    assert payload["graphs"]["electrochem"]["metric_correlations"]
    assert all(not row["electrochem"]["metric_correlations"] for row in payload["window_graphs"])
    assert payload["window_graphs"][0]["electrochem"] == payload["window_graphs"][1]["electrochem"]


def test_disabled_correlations_are_not_computed_for_main_or_trajectory(monkeypatch):
    import battery_insight.relations as module

    def unexpected(*args, **kwargs):
        raise AssertionError("Disabled correlation must not be calculated")

    monkeypatch.setattr(module, "_cell_correlations", unexpected)
    payload = analyze_bundle(bundle_fixture(), {"enabled_modules": ["network", "trajectory"], "windows": [
        {"label": "Earlier", "life_min": 1, "life_max": 3},
        {"label": "Later", "life_min": 4, "life_max": 6}]})
    assert len(payload["window_graphs"]) == 2
    assert not payload["graphs"]["electrochem"]["metric_correlations"]


def test_controlled_metadata_is_parsed_once_per_participating_test(monkeypatch):
    import battery_insight.workbench_analysis as module

    bundle = bundle_fixture()
    bundle["control_fields"] = [{"field_id": "pressure", "kind": "numeric", "unit": "MPa"}]
    for row in bundle["experiments"]:
        row["pressure"] = 2.0
    original, calls = module._control_value, []

    def tracked(value, spec):
        calls.append(spec["field_id"])
        return original(value, spec)

    monkeypatch.setattr(module, "_control_value", tracked)
    payload = analyze_bundle(bundle, {"enabled_modules": ["controlled"], "controlled": {"target": "loading", "controls": ["pressure"]}})
    assert len(payload["controlled_pairs"]) == 15
    assert calls == ["pressure"] * 6


def test_crossview_uses_only_pairs_available_in_both_views_and_filters_independently():
    bundle = bundle_fixture()
    bundle["experiments"][0]["loading"] = None
    payload = analyze_bundle(bundle, {"enabled_modules": ["crossview"], "cross_filter": {"design_min": 0.98}})
    assert all("e0" not in (row["source"], row["target"]) for row in payload["cross_pairs"])
    assert all(row["design_similarity"] >= 0.98 for row in payload["cross_pairs"])
    assert all("delta" not in row and "combined_score" not in row for row in payload["cross_pairs"])


def test_reference_experiment_filters_cross_and_controlled_pairs():
    payload = analyze_bundle(bundle_fixture(), {"reference_experiment_id": "e2", "controlled": {"target": "loading"}})
    assert len(payload["cross_pairs"]) == 5
    assert len(payload["controlled_pairs"]) == 5
    assert all("e2" in (row["source"], row["target"]) for row in payload["controlled_pairs"])


def test_controlled_matches_require_nonmissing_recorded_controls_and_conditions():
    bundle = bundle_fixture()
    bundle["experiments"][2]["active"] = None
    bundle["experiments"][3]["active"] = "LFP"
    bundle["experiments"][4]["test_type"] = "Different"
    payload = analyze_bundle(bundle, {"controlled": {"target": "loading", "controls": ["active", "composition"]}})
    pairs = {(row["source"], row["target"]): row for row in payload["controlled_pairs"]}
    assert pairs[("e0", "e1")]["status"] == "matched"
    assert pairs[("e0", "e2")]["status"] == "missing"
    assert "active" in pairs[("e0", "e2")]["missing"]
    assert pairs[("e0", "e3")]["status"] == "confounded"
    assert "test_type" in pairs[("e0", "e4")]["confounders"]


def test_controlled_numeric_tolerance_is_in_declared_raw_units():
    payload = analyze_bundle(bundle_fixture(), {"controlled": {"target": "loading", "controls": ["active"], "tolerances": {"loading": 0.11}}})
    row = next(row for row in payload["controlled_pairs"] if row["source"] == "e0" and row["target"] == "e1")
    assert row["status"] == "unchanged_target"
    assert row["delta"]["loading"]["delta"] == pytest.approx(0.1)
    assert row["delta"]["loading"]["unit"] == "mAh/cm2"


def test_unknown_context_and_replicate_group_are_not_assumed_equal():
    bundle = bundle_fixture()
    for row in bundle["experiments"]:
        row["protocol"] = "unknown"
        row["batch_id"] = "미상"
    payload = analyze_bundle(bundle, {"controlled": {"target": "loading"}})
    assert all(row["status"] == "missing" and "protocol" in row["missing"] for row in payload["controlled_pairs"])
    assert payload["replicates"] == []


def test_controlled_composition_reports_compensating_components():
    bundle = bundle_fixture()
    bundle["experiments"][1].update(active_pct=95.0, binder_pct=5.0)
    payload = analyze_bundle(bundle, {"controlled": {"target": "composition", "controls": ["active"]}})
    row = next(row for row in payload["controlled_pairs"] if row["source"] == "e0" and row["target"] == "e1")
    composition = row["delta"]["composition"]
    assert composition["coupled"] is True
    assert composition["absolute_difference"] == 1
    assert composition["component_deltas"] == {"active_pct": -1.0, "binder_pct": 1.0}


def test_metadata_controls_use_raw_units_without_entering_design_distance():
    bundle = bundle_fixture()
    original = analyze_bundle(bundle, {})
    bundle["control_fields"] = [{"field_id": "temperature_c", "label": "Temperature", "kind": "numeric", "unit": "degC"},
                                {"field_id": "charge_rate", "label": "Charge rate", "kind": "numeric", "unit": "C"},
                                {"field_id": "pressure_method", "label": "Pressure method", "kind": "categorical", "unit": ""}]
    for row in bundle["experiments"]:
        row.update(charge_rate="0.5", pressure_method="constant")
    bundle["experiments"][1]["temperature_c"] = 25.5
    bundle["experiments"][2]["charge_rate"] = "1.0"
    options = {"controlled": {"target": "loading", "controls": ["temperature_c", "charge_rate"], "tolerances": {"temperature_c": 1.0}}}
    payload = analyze_bundle(bundle, options)
    pairs = {(row["source"], row["target"]): row for row in payload["controlled_pairs"]}
    assert pairs[("e0", "e1")]["status"] == "matched"
    assert pairs[("e0", "e1")]["delta"]["temperature_c"]["delta"] == 0.5
    assert pairs[("e0", "e1")]["delta"]["temperature_c"]["unit"] == "degC"
    assert pairs[("e0", "e2")]["status"] == "confounded"
    assert "charge_rate" in pairs[("e0", "e2")]["confounders"]
    assert "pressure_method" in payload["metadata"]["controlled"]["uncontrolled_fields"]
    assert payload["graphs"]["design"]["edges"] == original["graphs"]["design"]["edges"]
    assert payload["graphs"]["design"]["metrics"] == original["graphs"]["design"]["metrics"]
    assert [node["features"] for node in payload["graphs"]["design"]["nodes"]] == [node["features"] for node in original["graphs"]["design"]["nodes"]]
    assert len(payload["graphs"]["design"]["metrics"]) == 3


def test_metadata_unknown_and_categorical_controls_are_not_assumed_equal():
    bundle = bundle_fixture()
    bundle["control_fields"] = [{"field_id": "pressure_method", "kind": "categorical", "unit": ""}]
    for row in bundle["experiments"]:
        row["pressure_method"] = "unknown"
    bundle["experiments"][0]["pressure_method"] = "constant"
    bundle["experiments"][1]["pressure_method"] = "spring"
    payload = analyze_bundle(bundle, {"controlled": {"target": "loading", "controls": ["pressure_method"]}})
    pairs = {(row["source"], row["target"]): row for row in payload["controlled_pairs"]}
    assert pairs[("e0", "e1")]["status"] == "confounded"
    assert pairs[("e2", "e3")]["status"] == "missing"
    assert "pressure_method" in pairs[("e2", "e3")]["missing"]


def test_metadata_controls_cannot_be_targets_or_omit_numeric_units():
    bundle = bundle_fixture()
    bundle["control_fields"] = [{"field_id": "temperature_c", "kind": "numeric", "unit": "degC"}]
    with pytest.raises(ValueError, match="selected design field"):
        analyze_bundle(bundle, {"controlled": {"target": "temperature_c"}})
    del bundle["control_fields"][0]["unit"]
    with pytest.raises(ValueError, match="explicit unit"):
        analyze_bundle(bundle, {"controlled": {"target": "loading", "controls": ["temperature_c"]}})


def test_trajectory_windows_keep_missing_windows_missing_without_extrapolation():
    bundle = bundle_fixture()
    options = {"windows": [{"label": "early", "life_min": 1, "life_max": 3}, {"label": "late", "life_min": 4, "life_max": 6}, {"label": "future", "life_min": 7, "life_max": 9}]}
    payload = analyze_bundle(bundle, options)
    row = next(row for row in payload["trajectories"] if row["experiment_id"] == "e0" and row["metric_id"] == "m0")
    assert row["points"][0]["value"] == pytest.approx(1.02)
    assert row["points"][1]["value"] == pytest.approx(1.05)
    assert row["points"][2]["value"] is None
    assert row["points"][2]["status"] == "no_finite_values"
    assert len(payload["window_graphs"][2]["electrochem"]["nodes"]) == 6


def test_minimum_points_is_configurable_but_endpoint_coverage_remains_required():
    bundle = bundle_fixture()
    payload = analyze_bundle(bundle, {"life_min": 1, "life_max": 2, "min_points": 2})
    assert feature(payload["graphs"]["electrochem"], "e0", "m0")["status"] == "valid"
    payload = analyze_bundle(bundle, {"life_min": 1, "life_max": 2, "min_points": 3})
    assert feature(payload["graphs"]["electrochem"], "e0", "m0")["status"] == "insufficient_points"


def test_fixed_reference_does_not_change_existing_pair_when_new_outlier_added():
    bundle = bundle_fixture()
    bundle["baseline"] = baseline(bundle)
    original = analyze_bundle(bundle, {"scale_mode": "fixed", "enabled_modules": ["network"]})
    extra = copy.deepcopy(bundle["experiments"][0])
    extra.update(experiment_id="new", cell_id="new")
    bundle["experiments"].append(extra)
    bundle["observations"].extend([dict(row, experiment_id="new", value=row["value"] * 10000) for row in bundle["observations"] if row["experiment_id"] == "e0"])
    changed = analyze_bundle(bundle, {"scale_mode": "fixed", "enabled_modules": ["network"]})
    assert pair(original["graphs"]["electrochem"])["similarity"] == pair(changed["graphs"]["electrochem"])["similarity"]
    assert all(scale["method"].startswith("fixed:") for scale in changed["graphs"]["electrochem"]["cohorts"][0]["scales"])


def test_fixed_missing_reference_dimension_never_falls_back_to_current_scale():
    bundle = bundle_fixture()
    bundle["baseline"] = baseline(bundle)
    bundle["baseline"]["scales"]["A"] = bundle["baseline"]["scales"]["A"][:2]
    payload = analyze_bundle(bundle, {"scale_mode": "fixed", "min_overlap": 2})
    graph = payload["graphs"]["electrochem"]
    assert graph["cohorts"][0]["excluded_metrics"] == [{"metric_id": "m2", "reason": "fixed_scale_unavailable"}]
    assert feature(graph, "e0", "m2")["value"] is not None
    assert feature(graph, "e0", "m2")["scaled"] is None


def test_fixed_window_graphs_share_reference_dimensions_even_if_window_constant():
    bundle = bundle_fixture()
    bundle["baseline"] = baseline(bundle)
    for point in bundle["observations"]:
        if point["life_index"] >= 4:
            point["value"] = 10
    payload = analyze_bundle(bundle, {"scale_mode": "fixed", "windows": [{"label": "late", "life_min": 4, "life_max": 6}]})
    graph = payload["window_graphs"][0]["electrochem"]
    assert graph["cohorts"][0]["informative_metrics"] == ["m0", "m1", "m2"]
    assert pair(graph)["similarity"] == 1


@pytest.mark.parametrize("mutation,match", [("missing", "requires a saved baseline"), ("summary", "summary differs"), ("units", "unit mismatch"), ("identity", "reference_id does not match"), ("scale", "must be positive")])
def test_fixed_reference_contract_errors(mutation, match):
    bundle = bundle_fixture()
    bundle["baseline"] = baseline(bundle)
    options = {"scale_mode": "fixed"}
    if mutation == "missing":
        del bundle["baseline"]
    elif mutation == "summary":
        options["summary"] = "change"
    elif mutation == "units":
        bundle["baseline"]["metrics"] = copy.deepcopy(bundle["metrics"])
        bundle["baseline"]["metrics"][0]["unit"] = "mV"
    elif mutation == "identity":
        options["reference_id"] = "not_this_reference"
    else:
        bundle["baseline"]["scales"]["A"][0]["scale"] = 0
    with pytest.raises(ValueError, match=match):
        analyze_bundle(bundle, options)


def test_replicates_count_cells_not_repeated_tests_or_cycles():
    bundle = bundle_fixture()
    bundle["experiments"][1]["cell_id"] = "c0"
    bundle["experiments"][1]["test_id"] = "T2"
    payload = analyze_bundle(bundle, {})
    row = next(row for row in payload["replicates"] if row["metric_id"] == "m0")
    assert row["n_cells"] == 5 and row["n_tests"] == 6
    assert row["values"][0]["cell_id"] == "c0"
    assert row["values"][0]["value"] == pytest.approx(1.53)
    assert row["values"][0]["n_tests"] == 2
    assert row["values"][0]["experiment_ids"] == ["e0", "e1"]
    assert row["group"]["cohort"] == "A"


def test_missing_replicate_group_not_pooled_as_unknown_group():
    bundle = bundle_fixture()
    for row in bundle["experiments"]:
        row.pop("batch_id")
    payload = analyze_bundle(bundle, {})
    assert payload["replicates"] == []
    assert len(payload["metadata"]["replicates"]["excluded_entities"]) == 6


def test_single_cell_replicate_group_has_no_fake_zero_spread():
    bundle = bundle_fixture(n=1)
    payload = analyze_bundle(bundle, {})
    assert all(row["q1"] is None and row["q3"] is None and row["spread_status"] == "single_cell" for row in payload["replicates"])
    assert all(row["std"] is None and row["mean"] == row["median"] and row["missing_count"] == 0 for row in payload["replicates"])


def test_group_mean_weights_physical_cells_equally_after_repeat_test_medians():
    bundle = bundle_fixture(n=4)
    for row in bundle["experiments"][:3]:
        row["cell_id"] = "same_physical_cell"
    for point in bundle["observations"]:
        if point["metric_id"] == "m0":
            point["value"] = {"e0": 0, "e1": 10, "e2": 100, "e3": 20}[point["experiment_id"]]
    # Extra valid rows on one repeated test must not give that test/cell more weight.
    bundle["observations"].extend({"experiment_id": "e2", "metric_id": "m0", "life_index": life, "value": 100, "unit": "V"}
                                  for life in np.linspace(1.01, 2.99, 99) if life != 2)
    payload = analyze_bundle(bundle, {"enabled_modules": ["replicates"]})
    row = next(row for row in payload["replicates"] if row["metric_id"] == "m0")
    assert row["mean"] == 15  # mean(median(0,10,100),20), not mean(0,10,100,20).
    assert row["std"] == pytest.approx(np.sqrt(50))
    assert row["median"] == 15 and row["q1"] == 12.5 and row["q3"] == 17.5
    assert row["n_cells"] == row["n_total_cells"] == 2
    assert row["n_tests"] == row["n_total_tests"] == 4 and row["missing_count"] == 0
    assert row["aggregation"] == "cell_equal_weight_mean" and row["cell_summary"] == "median"
    assert row["experiment_ids"] == ["e0", "e1", "e2", "e3"]
    assert len(payload["graphs"]["electrochem"]["nodes"]) == 4


def test_group_mean_retains_missing_cells_without_zero_imputation():
    bundle = bundle_fixture(n=4)
    bundle["experiments"][1]["cell_id"] = "c0"
    bundle["observations"] = [point for point in bundle["observations"] if not (point["metric_id"] == "m0" and point["experiment_id"] in {"e1", "e3"})]
    payload = analyze_bundle(bundle, {"enabled_modules": ["replicates"]})
    row = next(row for row in payload["replicates"] if row["metric_id"] == "m0")
    assert row["n_cells"] == 2 and row["n_total_cells"] == 3 and row["missing_count"] == 1
    assert row["n_tests"] == 2 and row["n_total_tests"] == 4
    assert row["mean"] == pytest.approx((1.02 + 3.06) / 2)
    assert row["experiment_ids"] == ["e0", "e1", "e2", "e3"]
    same_cell = next(value for value in row["values"] if value["cell_id"] == "c0")
    assert same_cell["experiment_ids"] == ["e0", "e1"] and same_cell["valid_experiment_ids"] == ["e0"]
    assert same_cell["n_total_tests"] == 2 and same_cell["n_tests"] == 1
    missing_cell = next(value for value in row["values"] if value["cell_id"] == "c3")
    assert missing_cell["value"] is None and missing_cell["n_tests"] == 0


def test_skewed_group_reports_arithmetic_mean_separately_from_median():
    bundle = bundle_fixture(n=3)
    for point in bundle["observations"]:
        if point["metric_id"] == "m0":
            point["value"] = 9 if point["experiment_id"] == "e2" else 0
    payload = analyze_bundle(bundle, {"enabled_modules": ["replicates"]})
    row = next(row for row in payload["replicates"] if row["metric_id"] == "m0")
    assert row["mean"] == 3 and row["median"] == 0
    assert row["std"] == pytest.approx(np.sqrt(27))
    assert row["n_cells"] == 3 and row["missing_count"] == 0  # Zero is a measurement.


def test_unknown_physical_cell_is_not_counted_as_an_independent_replicate():
    bundle = bundle_fixture(n=2)
    bundle["experiments"][1]["cell_id"] = "unknown"
    payload = analyze_bundle(bundle, {"enabled_modules": ["replicates"]})
    assert all(row["n_cells"] == row["n_total_cells"] == 1 and row["experiment_ids"] == ["e0"] for row in payload["replicates"])
    assert payload["metadata"]["replicates"]["excluded_entities"] == [{"experiment_id": "e1", "missing": ["cell_id"]}]


def test_all_missing_metric_group_is_reported_and_window_failure_stays_missing():
    bundle = bundle_fixture(n=2)
    payload = analyze_bundle(bundle, {"enabled_modules": ["replicates"], "life_min": 7, "life_max": 9})
    assert len(payload["replicates"]) == 3
    for row in payload["replicates"]:
        assert row["mean"] is None and row["std"] is None and row["median"] is None
        assert row["n_cells"] == 0 and row["missing_count"] == row["n_total_cells"] == 2
        assert row["spread_status"] == "no_valid_cells"
        assert row["life_min"] == 7 and row["life_max"] == 9
    json.dumps(payload, allow_nan=False)


@pytest.mark.parametrize("key,different", [("cohort", "B"), ("protocol", "P2"), ("chemistry", "LFP"),
                                             ("test_type", "Insitu"), ("temperature_c", 45),
                                             ("pressure_mpa", 0.8), ("charge_rate_c", 1.0)])
def test_group_means_automatically_separate_recorded_test_conditions(key, different):
    bundle = bundle_fixture(n=2)
    if key not in bundle["experiments"][0]:
        bundle["experiments"][0][key] = 0.5
    bundle["experiments"][1][key] = different
    if key == "protocol":
        # The core network already rejects mixed protocols within one cohort.
        bundle["experiments"][1]["cohort"] = "B"
    bundle["comparison"]["allow_mixed_chemistry"] = True
    payload = analyze_bundle(bundle, {"enabled_modules": ["replicates"], "replicate_group_by": ["design_id"]})
    rows = [row for row in payload["replicates"] if row["metric_id"] == "m0"]
    assert len(rows) == 2 and all(row["n_cells"] == 1 for row in rows)
    assert key in payload["metadata"]["replicates"]["automatic_group_by"]


def test_mean_groups_normalize_numeric_conditions_and_exclude_unknown_peer():
    bundle = bundle_fixture(n=3)
    bundle["control_fields"] = [{"field_id": "hold_minutes", "kind": "numeric", "unit": "min"}]
    bundle["experiments"][0].update(hold_minutes=30, temperature_c=25)
    bundle["experiments"][1].update(hold_minutes="30.0", temperature_c="25.0")
    bundle["experiments"][2]["hold_minutes"] = None
    payload = analyze_bundle(bundle, {"enabled_modules": ["replicates"]})
    row = next(row for row in payload["replicates"] if row["metric_id"] == "m0")
    assert row["n_cells"] == 2 and row["group"]["hold_minutes"] == 30
    assert payload["metadata"]["replicates"]["excluded_entities"] == [{"experiment_id": "e2", "missing": ["hold_minutes"]}]
    assert payload["metadata"]["replicates"]["condition_units"]["hold_minutes"] == "min"


def test_rpt_group_means_separate_explicit_stage_and_life_cycle_without_inference():
    bundle = bundle_fixture(n=5)
    for index, row in enumerate(bundle["experiments"]):
        row.update(test_type="RPT", rpt_stage=["BOL", "100cyc", "before_WIP", "after_WIP", None][index],
                   rpt_life_cycle=[0, 100, 100, 100, 100][index])
    payload = analyze_bundle(bundle, {"enabled_modules": ["replicates"]})
    groups = [row for row in payload["replicates"] if row["metric_id"] == "m0"]
    assert len(groups) == 4 and all(row["n_cells"] == 1 for row in groups)
    assert {row["group"]["rpt_stage"] for row in groups} == {"BOL", "100cyc", "before_WIP", "after_WIP"}
    assert payload["metadata"]["replicates"]["excluded_entities"] == [{"experiment_id": "e4", "missing": ["rpt_stage"]}]
    assert payload["metadata"]["replicates"]["rpt_stage_inferred"] is False
    bundle["experiments"][1].update(rpt_stage="BOL")
    groups = [row for row in analyze_bundle(bundle, {"enabled_modules": ["replicates"]})["replicates"] if row["metric_id"] == "m0"]
    assert len(groups) == 4  # Identical stage labels with different life counts still do not merge.


def test_rpt_without_stage_is_excluded_while_cycle_groups_remain_available():
    bundle = bundle_fixture(n=2)
    bundle["experiments"][1]["test_type"] = "RPT"
    payload = analyze_bundle(bundle, {"enabled_modules": ["replicates"]})
    assert all(row["experiment_ids"] == ["e0"] for row in payload["replicates"])
    assert payload["metadata"]["replicates"]["excluded_entities"] == [{"experiment_id": "e1", "missing": ["rpt_stage"]}]


def test_group_identity_is_shared_across_metrics_stable_to_order_and_changes_with_window():
    bundle = bundle_fixture(n=2)
    original = analyze_bundle(bundle, {"enabled_modules": ["replicates"]})
    ids = {row["group_id"] for row in original["replicates"]}
    assert len(ids) == 1
    bundle["experiments"].reverse()
    bundle["observations"].reverse()
    reordered = analyze_bundle(bundle, {"enabled_modules": ["replicates"]})
    assert reordered["replicates"] == original["replicates"]
    later = analyze_bundle(bundle, {"enabled_modules": ["replicates"], "life_min": 4, "life_max": 6})
    assert {row["group_id"] for row in later["replicates"]}.isdisjoint(ids)


def test_change_summary_is_preserved_before_cross_cell_arithmetic_mean():
    payload = analyze_bundle(bundle_fixture(n=2), {"enabled_modules": ["replicates"], "summary": "change"})
    row = next(row for row in payload["replicates"] if row["metric_id"] == "m0")
    assert row["summary"] == "change" and row["mean"] == pytest.approx((0.02 + 0.04) / 2)
    assert row["std"] == pytest.approx(np.std([0.02, 0.04], ddof=1))


def test_external_cutoff_is_rechecked_for_each_window():
    bundle = bundle_fixture()
    bundle["metrics"][0]["table"] = "external_features"
    for row in bundle["observations"]:
        if row["metric_id"] == "m0":
            row["observed_until_life"] = 6
    payload = analyze_bundle(bundle, {"windows": [{"label": "early", "life_min": 1, "life_max": 3}, {"label": "late", "life_min": 4, "life_max": 6}]})
    assert feature(payload["graphs"]["electrochem"], "e0", "m0")["value"] is None
    early, late = payload["window_graphs"]
    assert feature(early["electrochem"], "e0", "m0")["value"] is None
    assert feature(late["electrochem"], "e0", "m0")["value"] is not None
    assert early["electrochem"]["metadata"]["cutoff_exclusions"][0]["reason"] == "observes_beyond_window"


def test_snapshot_trajectory_uses_exact_points_and_rechecks_external_observation_boundary():
    bundle = bundle_fixture(n=2)
    bundle["metrics"][0]["table"] = "external_features"
    for point in bundle["observations"]:
        if point["metric_id"] == "m0":
            point["observed_until_life"] = 3
    payload = analyze_bundle(bundle, {"summary": "snapshot", "life_min": 2, "life_max": 2,
                                      "windows": [{"label": "at2", "life_min": 2, "life_max": 2},
                                                  {"label": "at3", "life_min": 3, "life_max": 3}]})
    assert payload["options"]["min_points"] == 1
    assert feature(payload["graphs"]["electrochem"], "e0", "m0")["value"] is None
    assert feature(payload["graphs"]["electrochem"], "e0", "m1")["value"] == pytest.approx(2.02)
    assert feature(payload["window_graphs"][0]["electrochem"], "e0", "m0")["value"] is None
    assert feature(payload["window_graphs"][1]["electrochem"], "e0", "m0")["value"] == pytest.approx(1.03)
    assert next(row for row in payload["replicates"] if row["metric_id"] == "m0")["n_cells"] == 0


def test_external_missing_cutoff_is_unavailable_not_assumed_contemporaneous():
    bundle = bundle_fixture()
    bundle["metrics"][0]["table"] = "external_features"
    payload = analyze_bundle(bundle, {})
    assert feature(payload["graphs"]["electrochem"], "e0", "m0")["value"] is None
    assert payload["graphs"]["electrochem"]["metadata"]["cutoff_exclusions"][0]["reason"] == "missing_observed_until_life"


def test_include_false_observations_are_ignored_in_workbench():
    bundle = bundle_fixture()
    for row in bundle["observations"]:
        row["include"] = row["experiment_id"] != "e0"
    payload = analyze_bundle(bundle, {})
    assert feature(payload["graphs"]["electrochem"], "e0", "m0")["value"] is None


def test_input_bundle_options_are_not_mutated():
    bundle = bundle_fixture()
    options = {"controlled": {"target": "loading"}}
    before_bundle, before_options = copy.deepcopy(bundle), copy.deepcopy(options)
    analyze_bundle(bundle, options)
    assert bundle == before_bundle and options == before_options


def test_duplicate_metric_family_remains_rejected():
    bundle = bundle_fixture()
    bundle["metrics"][1]["family"] = "f0"
    with pytest.raises(ValueError, match="one representative metric per family"):
        analyze_bundle(bundle, {})


def test_empty_bundle_with_explicit_empty_selections_is_supported():
    bundle = bundle_fixture()
    bundle["experiments"] = []
    bundle["observations"] = []
    payload = analyze_bundle(bundle, {"metric_ids": [], "design_field_ids": []})
    assert payload["graphs"]["electrochem"]["nodes"] == []
    assert payload["graphs"]["design"]["nodes"] == []


@pytest.mark.parametrize("options", [
    {"metric_ids": ["missing"]}, {"metric_ids": ["m0", "m0"]}, {"enabled_modules": ["unknown"]},
    {"life_min": 10, "life_max": 1}, {"min_points": 1}, {"metric_ids": ["m0"], "min_overlap": 3},
    {"scale_mode": "automatic"}, {"cross_filter": {"design_min": 0.8, "design_max": 0.2}},
    {"controlled": {"target": "unknown"}}, {"controlled": {"target": "loading", "controls": ["loading"]}},
    {"controlled": {"target": "loading", "controls": ["active"], "tolerances": {"active": 0.1}}},
    {"replicate_group_by": []}, {"reference_experiment_id": "missing"},
])
def test_invalid_options_fail_clearly(options):
    with pytest.raises(ValueError):
        analyze_bundle(bundle_fixture(), options)
