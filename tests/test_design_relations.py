import copy
import json

import numpy as np
import pandas as pd
import pytest

from battery_insight.design_relations import build_design_relations


def design_fixture():
    experiments = pd.DataFrame([
        {"experiment_id": "a", "cell_id": "c1", "label": "Design A", "chemistry": "NMC", "design_group": "g1",
         "active": "NMC811", "loading": 4.0, "active_wt": 96.0, "carbon_wt": 2.0, "binder_wt": 2.0},
        {"experiment_id": "b", "cell_id": "c2", "label": "Design B", "chemistry": "NMC", "design_group": "g1",
         "active": "NMC811", "loading": 4.5, "active_wt": 94.0, "carbon_wt": 3.0, "binder_wt": 3.0},
        {"experiment_id": "c", "cell_id": "c3", "label": "Design C", "chemistry": "LFP", "design_group": "g1",
         "active": "LFP", "loading": 6.0, "active_wt": 90.0, "carbon_wt": 5.0, "binder_wt": 5.0},
    ])
    fields = [
        {"field_id": "active", "label": "Active material", "kind": "categorical", "group": "active_identity"},
        {"field_id": "loading", "label": "Areal capacity", "kind": "numeric", "unit": "mAh/cm2", "scale": 2, "group": "cathode_loading"},
        {"field_id": "electrode_composition", "label": "Electrode composition", "kind": "composition", "unit": "wt%",
         "composition_basis": "electrode_total", "components": ["active_wt", "carbon_wt", "binder_wt"], "group": "cathode_composition"},
    ]
    return experiments, fields, {}


def edge(payload, source="a", target="b"):
    return next(e for e in payload["edges"] if e["source"] == source and e["target"] == target)


def feature(payload, entity, field):
    return next(f for node in payload["nodes"] if node["experiment_id"] == entity for f in node["features"] if f["metric_id"] == field)


def test_design_weighted_gower_matches_declared_formula():
    payload = build_design_relations(*design_fixture())
    pair = edge(payload)
    # Identity=0, loading=0.5/2, composition=(2+1+1)/200.
    assert pair["similarity"] == pytest.approx(1 - (0 + 0.25 + 0.02) / 3)
    assert pair["shared_metrics"] == 3  # Composition vector counts once.
    assert pair["coverage"] == 1
    assert payload["metadata"]["similarity_is_correlation"] is False
    assert payload["metadata"]["compositions_renormalized"] is False
    assert payload["metric_correlations"] == []
    assert all(f["scaled"] is None for n in payload["nodes"] for f in n["features"])
    json.dumps(payload, allow_nan=False)


def test_category_exact_mismatch_is_not_chemical_distance():
    payload = build_design_relations(*design_fixture())
    contribution = next(c for c in edge(payload, "a", "c")["contributors"] if c["metric_id"] == "active")
    assert contribution["left"] == "NMC811" and contribution["right"] == "LFP"
    assert contribution["standardized_distance"] == 1
    assert contribution["raw_delta"] is None


def test_numeric_scale_is_fixed_and_distance_clipped_at_one():
    experiments, fields, settings = design_fixture()
    initial = build_design_relations(experiments, fields, settings)
    experiments.loc[2, "loading"] = 1000
    added_outlier = build_design_relations(experiments, fields, settings)
    assert edge(initial)["similarity"] == edge(added_outlier)["similarity"]
    contribution = next(c for c in edge(added_outlier, "a", "c")["contributors"] if c["metric_id"] == "loading")
    assert contribution["standardized_distance"] == 1
    assert contribution["raw_delta"] == 996


def test_missing_composition_component_drops_entire_vector_not_filled_zero():
    experiments, fields, settings = design_fixture()
    experiments.loc[0, "binder_wt"] = np.nan
    settings["min_fields"] = 2
    payload = build_design_relations(experiments, fields, settings)
    item = feature(payload, "a", "electrode_composition")
    assert item["status"] == "missing" and item["value"] is None
    assert item["missing_components"] == ["binder_wt"]
    assert edge(payload)["shared_metrics"] == 2
    assert edge(payload)["coverage"] == pytest.approx(2 / 3)


def test_observed_zero_is_valid_composition_data():
    experiments, fields, settings = design_fixture()
    experiments.loc[0, ["active_wt", "carbon_wt", "binder_wt"]] = [98, 0, 2]
    payload = build_design_relations(experiments, fields, settings)
    assert feature(payload, "a", "electrode_composition")["value"]["carbon_wt"] == 0


def test_component_missing_and_invalid_known_component_still_rejected():
    experiments, fields, settings = design_fixture()
    experiments.loc[0, "binder_wt"] = np.nan
    experiments.loc[0, "carbon_wt"] = -1
    with pytest.raises(ValueError, match="within 0..100"):
        build_design_relations(experiments, fields, settings)


def test_composition_sum_is_validated_without_renormalization():
    experiments, fields, settings = design_fixture()
    experiments.loc[0, "active_wt"] = 95.5
    payload = build_design_relations(experiments, fields, settings)
    assert sum(feature(payload, "a", "electrode_composition")["value"].values()) == 99.5
    settings["composition_sum_tolerance"] = 0
    with pytest.raises(ValueError, match="sums to 99.5"):
        build_design_relations(experiments, fields, settings)


def test_active_material_basis_is_explicitly_supported_without_conversion():
    experiments, fields, settings = design_fixture()
    fields[2]["composition_basis"] = "active_material_total"
    payload = build_design_relations(experiments, fields, settings)
    assert payload["field_specs"][2]["composition_basis"] == "active_material_total"
    assert feature(payload, "a", "electrode_composition")["value"]["active_wt"] == 96


@pytest.mark.parametrize("change,match", [
    ("basis_missing", "explicit composition_basis"), ("basis_unknown", "explicit composition_basis"),
    ("unit", "explicit composition_basis"), ("sum", "sums to"),
    ("range", "within 0..100"), ("duplicate_component", "Duplicate component"),
])
def test_invalid_composition_configuration_or_values_rejected(change, match):
    experiments, fields, settings = design_fixture()
    if change == "basis_missing":
        del fields[2]["composition_basis"]
    elif change == "basis_unknown":
        fields[2]["composition_basis"] = "automatically_inferred"
    elif change == "unit":
        fields[2]["unit"] = "fraction"
    elif change == "sum":
        experiments.loc[0, "active_wt"] = 90
    elif change == "range":
        experiments.loc[0, "binder_wt"] = -2
    else:
        fields[2]["components"].append("active_wt")
    with pytest.raises(ValueError, match=match):
        build_design_relations(experiments, fields, settings)


def test_composition_components_cannot_also_receive_individual_weights():
    experiments, fields, settings = design_fixture()
    fields.append({"field_id": "binder_wt", "label": "Binder amount", "kind": "numeric", "unit": "wt%", "scale": 2, "group": "binder_amount"})
    with pytest.raises(ValueError, match="duplicate weight"):
        build_design_relations(experiments, fields, settings)


def test_mass_and_areal_capacity_loading_cannot_both_weight_same_family():
    experiments, fields, settings = design_fixture()
    fields.append({"field_id": "mass_loading", "kind": "numeric", "unit": "mg/cm2", "scale": 1, "group": "cathode_loading"})
    with pytest.raises(ValueError, match="one representative field per design group"):
        build_design_relations(experiments, fields, settings)


def test_pair_coverage_uses_all_configured_weights():
    experiments, fields, settings = design_fixture()
    fields[2]["weight"] = 8
    experiments.loc[0, "binder_wt"] = np.nan
    settings["min_fields"] = 2
    payload = build_design_relations(experiments, fields, settings)
    excluded = next(p for p in payload["excluded_pairs"] if p["source"] == "a" and p["target"] == "b")
    assert excluded["coverage"] == 0.2
    assert excluded["reason"] == "insufficient_weighted_coverage"
    assert excluded["shared_weight"] == 2 and excluded["total_weight"] == 10


def test_weighted_similarity_uses_one_weight_for_composition_vector():
    experiments, fields, settings = design_fixture()
    fields[2]["weight"] = 8
    payload = build_design_relations(experiments, fields, settings)
    assert edge(payload)["similarity"] == pytest.approx(1 - (0.25 + 8 * 0.02) / 10)


def test_missing_design_group_defaults_to_all_designs_but_explicit_groups_do_not_mix():
    experiments, fields, settings = design_fixture()
    experiments = experiments.drop(columns="design_group")
    payload = build_design_relations(experiments, fields, settings)
    assert len(payload["edges"]) == 3
    assert payload["cohorts"][0]["cohort"] == "전체 설계"
    experiments["design_group"] = ["one", "one", "two"]
    payload = build_design_relations(experiments, fields, settings)
    assert len(payload["edges"]) == 1
    assert edge(payload)["cohort"] == "one"


def test_graph_does_not_require_raw_data_or_electrochemical_cohort():
    experiments, fields, settings = design_fixture()
    experiments["status"] = ["pending", "failed", "ready"]
    experiments["cohort"] = ["different_a", "different_b", "different_c"]
    payload = build_design_relations(experiments, fields, settings)
    assert len(payload["nodes"]) == 3 and len(payload["edges"]) == 3


def test_unknown_numeric_and_category_values_remain_missing():
    experiments, fields, settings = design_fixture()
    experiments.loc[0, "active"] = "unknown"
    experiments.loc[0, "loading"] = np.inf
    payload = build_design_relations(experiments, fields, settings)
    assert feature(payload, "a", "active")["value"] is None
    assert feature(payload, "a", "loading")["value"] is None
    assert all(e["source"] != "a" for e in payload["edges"])
    json.dumps(payload, allow_nan=False)


def test_disabled_fields_do_not_enter_duplicate_group_check_or_weight():
    experiments, fields, settings = design_fixture()
    fields.append({"field_id": "mass_loading", "kind": "numeric", "unit": "mg/cm2", "scale": 1, "group": "cathode_loading", "include": False})
    experiments["include"] = [True, True, False]
    payload = build_design_relations(experiments, fields, settings)
    assert len(payload["metrics"]) == 3
    assert len(payload["nodes"]) == 2
    assert payload["metadata"]["total_field_weight"] == 3


def test_no_fields_or_no_experiments_yield_safe_empty_graphs():
    experiments, fields, settings = design_fixture()
    payload = build_design_relations(experiments, [], settings)
    assert len(payload["nodes"]) == 3 and payload["edges"] == []
    assert payload["metadata"]["disabled_reason"] == "no_design_fields_configured"
    payload = build_design_relations(experiments.iloc[:0], fields, settings)
    assert payload["nodes"] == [] and payload["edges"] == []


def test_input_frames_and_configuration_are_not_mutated():
    experiments, fields, settings = design_fixture()
    frame_before, fields_before, settings_before = experiments.copy(deep=True), copy.deepcopy(fields), settings.copy()
    build_design_relations(experiments, fields, settings)
    pd.testing.assert_frame_equal(frame_before, experiments)
    assert fields_before == fields and settings_before == settings


@pytest.mark.parametrize("change,match", [
    ("scale_missing", "engineering difference scale"), ("scale_zero", "scale must be positive"),
    ("weight", "weight must be positive"), ("unit_unknown", "Unknown unit"),
    ("kind", "Unknown design field kind"), ("duplicate", "Duplicate field_id"),
])
def test_invalid_field_specs_rejected(change, match):
    experiments, fields, settings = design_fixture()
    if change == "scale_missing":
        del fields[1]["scale"]
    elif change == "scale_zero":
        fields[1]["scale"] = 0
    elif change == "weight":
        fields[1]["weight"] = -1
    elif change == "unit_unknown":
        fields[1]["unit"] = "unknown"
    elif change == "kind":
        fields[1]["kind"] = "embedding"
    else:
        fields.append(fields[1].copy())
    with pytest.raises(ValueError, match=match):
        build_design_relations(experiments, fields, settings)


@pytest.mark.parametrize("settings", [{"min_fields": 0}, {"min_coverage": 0}, {"min_similarity": 1.1}, {"composition_sum_tolerance": 2}, {"unknown": True}])
def test_invalid_design_settings_rejected(settings):
    experiments, fields, _ = design_fixture()
    with pytest.raises(ValueError):
        build_design_relations(experiments, fields, settings)


def test_numeric_percent_range_and_categorical_numeric_coding_rejected():
    experiments, fields, settings = design_fixture()
    fields[1]["unit"] = "%"
    experiments.loc[0, "loading"] = 102
    with pytest.raises(ValueError, match="Percentage field"):
        build_design_relations(experiments, fields, settings)
    experiments, fields, settings = design_fixture()
    experiments["active"] = [1, 2, 3]
    with pytest.raises(ValueError, match="nonempty strings"):
        build_design_relations(experiments, fields, settings)


def test_minimum_similarity_threshold_is_explicitly_applied():
    experiments, fields, settings = design_fixture()
    settings["min_similarity"] = 0.9
    payload = build_design_relations(experiments, fields, settings)
    assert len(payload["edges"]) == 1
    assert len(payload["excluded_pairs"]) == 2
    assert all(p["reason"] == "below_similarity_threshold" for p in payload["excluded_pairs"])
