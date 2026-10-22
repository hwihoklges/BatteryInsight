"""Guard against deleting distinct electrochemical measurements as duplicates."""
import json

import pandas as pd
import pytest

from battery_insight.dedup import prune_catalog, read_registry_policy


def catalog(*metrics, namespace="cyclediag", role="indicator", origin="core_declared"):
    records = []
    for metric in metrics:
        if isinstance(metric, dict):
            record = dict(metric)
            name = record["feature_name"]
        else:
            name = metric
            record = {"feature_name": name}
        record.setdefault("namespace", namespace)
        record.setdefault("metric_id", record["namespace"] + "." + name)
        record.setdefault("role", role)
        record.setdefault("origin", origin)
        records.append(record)
    return pd.DataFrame(records, columns=["namespace", "metric_id", "feature_name", "role", "origin"])


def policy(*, aliases=None, families=None):
    return {"EXACT_ALIASES": aliases or {}, "FAMILIES": families or {}}


def decision(result, metric_id):
    return result.ledger.set_index("metric_id").loc[metric_id]


def test_numerical_alias_records_formula_without_modifying_input():
    frame = catalog("CI", "CE")
    frame["example_value"] = [0.4, 99.6]
    before = frame.copy(deep=True)
    result = prune_catalog(frame, policy(aliases={"CI": ["CE", "CI == 100 - CE"]}))
    retired = decision(result, "cyclediag.CI")
    assert retired.decision == "remove_duplicate"
    assert retired.representative_metric_id == "cyclediag.CE"
    assert json.loads(retired.decision_chain_json)[0]["formula"] == "CI == 100 - CE"
    assert set(result.primary.metric_id) == {"cyclediag.CE"}
    pd.testing.assert_frame_equal(frame, before)
    pd.testing.assert_frame_equal(result.ledger[frame.columns], before)
    assert not result.ledger.numerical_values_modified.any()
    assert not result.ledger.independent_causal_evidence.any()


def test_alias_without_present_representative_is_not_deleted():
    result = prune_catalog(catalog("CI"), policy(aliases={"CI": ["CE", "100 - CE"]}))
    assert set(result.primary.feature_name) == {"CI"}
    assert decision(result, "cyclediag.CI").decision == "keep"


def test_alias_baseline_chain_resolves_to_existing_terminal_representative():
    frame = catalog("ocv_spread_compression", "delta_ocv_spread_20_80", "ocv_spread_20_80")
    result = prune_catalog(frame, policy(aliases={
        "ocv_spread_compression": ["delta_ocv_spread_20_80", "same delta spread"]}))
    retired = decision(result, "cyclediag.ocv_spread_compression")
    assert retired.representative_metric_id == "cyclediag.ocv_spread_20_80"
    assert len(json.loads(retired.decision_chain_json)) == 2
    assert set(retired.removal_basis.split("|")) == {"declared_identity", "baseline_representation"}
    assert set(result.primary.feature_name) == {"ocv_spread_20_80"}


def test_delta_of_retired_alias_resolves_to_live_measurement():
    frame = catalog("delta_ocv_V_inf_soc20", "V_inf_rest_soc20")
    result = prune_catalog(frame, policy(aliases={
        "ocv_V_inf_soc20": ["V_inf_rest_soc20", "same rest fit"]}))
    assert decision(result, "cyclediag.delta_ocv_V_inf_soc20").representative_metric_id == "cyclediag.V_inf_rest_soc20"


def test_legacy_cc_capacity_retention_and_cc_fraction_delta_remain_distinct():
    frame = catalog("chgCapa_CCratio", "delta_chgCapa_CCratio", "CCcapa", "CCratio",
                    namespace="studio_legacy", origin="legacy_initialized")
    result = prune_catalog(frame, policy())
    retention = decision(result, "studio_legacy.chgCapa_CCratio")
    fraction_delta = decision(result, "studio_legacy.delta_chgCapa_CCratio")
    assert retention.representative_metric_id == "studio_legacy.CCcapa"
    assert fraction_delta.representative_metric_id == "studio_legacy.CCratio"
    assert "Qcc(n)/Qcc(0)" in json.loads(retention.decision_chain_json)[0]["formula"]
    assert "CCratio(n)-CCratio(0)" in json.loads(fraction_delta.decision_chain_json)[0]["formula"]
    assert set(result.primary.feature_name) == {"CCcapa", "CCratio"}


@pytest.mark.parametrize("removed,retained", [
    ("SoHQ", "capacity"), ("SoHE", "dchgEnergy"), ("chgRetention", "chgCapa"),
    ("capa_loss_ret", "capa_loss"), ("enrg_loss_ret", "enrg_loss"),
    ("delta_EoC_V10", "EoC_V10"), ("delta_EoD_V10", "EoD_V10"),
    ("EoC_dchgR_inc", "EoC_dchgR"), ("EoD_chgR_inc", "EoD_chgR"),
])
def test_legacy_fixed_baseline_representations(removed, retained):
    frame = catalog(removed, retained, namespace="studio_legacy", origin="legacy_initialized")
    result = prune_catalog(frame, policy())
    row = decision(result, "studio_legacy." + removed)
    assert row.representative_metric_id == "studio_legacy." + retained
    assert row.removal_basis == "baseline_representation"
    assert set(result.primary.feature_name) == {retained}


def test_previous_cycle_efficiencies_are_not_reciprocals_of_same_cycle_efficiency():
    legacy = catalog("chg_dchg_capa", "dchg_chg_capa", "chg_dchg_enrg", "dchg_chg_enrg",
                     namespace="studio_legacy", origin="legacy_initialized")
    frame = pd.concat([legacy, catalog("CE", "EE")], ignore_index=True)
    result = prune_catalog(frame, policy())
    assert {"studio_legacy.chg_dchg_capa", "studio_legacy.chg_dchg_enrg"} <= set(result.primary.metric_id)
    assert decision(result, "studio_legacy.dchg_chg_capa").removal_basis == "implementation_replaced"
    assert decision(result, "studio_legacy.dchg_chg_enrg").removal_basis == "implementation_replaced"


@pytest.mark.parametrize("legacy,current", [
    ("chg_peak1_V", "chg_dQdV_peak1_V"),
    ("dchg_peak2_dQdV", "dchg_dQdV_peak2"),
    ("chg_peak3_Q", "chg_dVdQ_peak3_Q"),
    ("dchg_peak4_dVdQ", "dchg_dVdQ_peak4"),
])
def test_legacy_peak_numbers_are_not_directly_mapped_to_new_peak_numbers(legacy, current):
    frame = pd.concat([
        catalog(legacy, namespace="studio_legacy", origin="legacy_initialized"),
        catalog(current),
    ], ignore_index=True)
    result = prune_catalog(frame, policy())
    row = decision(result, "studio_legacy." + legacy)
    assert row.decision == "review_required"
    assert row.representative_metric_id != "cyclediag." + current
    assert row.removal_basis == ""
    assert "cyclediag." + current in set(result.primary.metric_id)


def test_soc_pulse_time_direction_and_peak_index_are_preserved():
    names = [
        "R_ohmic_soc20", "R_ohmic_soc50", "R_ohmic_soc80",
        "EoC_dchgR_0p1s", "EoC_dchgR_10s", "EoC_dchgR_30s", "EoC_dchgR_60s",
        "EoD_chgR_0p1s", "EoD_chgR_10s", "EoD_chgR_30s", "EoD_chgR_60s",
        "chg_dQdV_peak1_V", "chg_dQdV_peak2_V", "dchg_dQdV_peak1_V",
        "hyst_area", "hyst_area_low", "hyst_area_mid", "hyst_area_high",
        "EoD_restV_60s", "EoD_restV_end", "CE", "CI_per_hour",
        "V_inf_rest_soc50", "V_inf_est_soc50",
    ]
    result = prune_catalog(catalog(*names), policy())
    assert set(result.primary.feature_name) == set(names)
    assert result.summary["removed_duplicates"] == 0


def test_within_measurement_family_does_not_collapse_other_axis_positions():
    frame = catalog("EoC_dchgR_10s", "EoC_dchgR_10s_T25", "EoC_dchgR_30s", "EoD_chgR_10s")
    result = prune_catalog(frame, policy(families={
        "r_eoc_dchg_10s": ["EoC_dchgR_10s", "EoC_dchgR_10s_T25"]}))
    assert set(result.primary.feature_name) == {"EoC_dchgR_10s", "EoC_dchgR_30s", "EoD_chgR_10s"}
    assert decision(result, "cyclediag.EoC_dchgR_10s_T25").removal_basis == "family_representative"


def test_missing_family_primary_uses_present_representative_without_dangling_id():
    result = prune_catalog(catalog("hyst_max_dV_mid"), policy(families={
        "hysteresis_mid": ["hyst_area_mid", "hyst_max_dV_mid"]}))
    assert set(result.primary.feature_name) == {"hyst_max_dV_mid"}


def test_capacity_outcome_representation_remains_available():
    result = prune_catalog(catalog("SoHQ", "dchgCapa", "dchg_E", "dchg_dVdQ_SOC0_Q", role="target"),
                           policy(families={"capacity_discharge": ["dchgCapa", "SoHQ", "dchg_E", "dchg_dVdQ_SOC0_Q"]}))
    assert set(result.primary.feature_name) == {"SoHQ"}
    assert set(result.ledger.representative_metric_id) == {"cyclediag.SoHQ"}


def test_context_unverified_output_and_duplicate_decisions_are_distinct():
    frame = catalog(
        "CE", "CI", "unverified_unit_measurement",
        {"feature_name": "temperature", "role": "covariate"},
        {"feature_name": "qc_valid", "role": "qc"},
        {"feature_name": "cell_id", "role": "meta"},
        {"feature_name": "diagnosis_score", "role": "diagnosis"},
        {"feature_name": "lookup_only", "origin": "source_column_loop"},
        {"feature_name": "reference_only", "namespace": "studio_legacy", "origin": "legacy_reference"},
    )
    frame["unit_status"] = "needs_review"
    result = prune_catalog(frame, policy(aliases={"CI": ["CE", "100 - CE"]}))
    assert decision(result, "cyclediag.CI").decision == "remove_duplicate"
    for name in ("temperature", "qc_valid", "cell_id", "diagnosis_score"):
        assert decision(result, "cyclediag." + name).decision == "support_only"
    assert decision(result, "cyclediag.lookup_only").decision == "review_required"
    assert decision(result, "studio_legacy.reference_only").decision == "review_required"
    assert decision(result, "cyclediag.unverified_unit_measurement").decision == "keep"
    assert result.summary["removed_duplicates"] == 1
    assert result.summary["support_only"] == 4
    assert result.summary["review_required"] == 2


def test_observed_export_evidence_is_not_treated_as_reference_only():
    frame = catalog({"feature_name": "old_metric", "namespace": "studio_legacy",
                     "origin": "legacy_reference|observed_export_header"},
                    {"feature_name": "live_metric", "origin": "source_column_loop|observed_export_header"})
    result = prune_catalog(frame, policy())
    assert len(result.primary) == 2


def test_family_policy_is_labelled_without_claiming_fresh_equivalence():
    result = prune_catalog(catalog("chgCapa_CCratio", "chgCapa_CCratio_norm", "Q_relax_pct", "Q_relax_rpt_pct"),
                           policy(families={
                               "cv_amount": ["chgCapa_CCratio", "chgCapa_CCratio_norm"],
                               "q_relax": ["Q_relax_pct", "Q_relax_rpt_pct"],
                           }))
    cv = json.loads(decision(result, "cyclediag.chgCapa_CCratio_norm").decision_chain_json)[0]
    relaxation = json.loads(decision(result, "cyclediag.Q_relax_rpt_pct").decision_chain_json)[0]
    assert cv["basis"] == relaxation["basis"] == "family_representative"
    assert "T_ref" in cv["conditions"]
    assert "Q_relax_source" in relaxation["conditions"]
    assert not result.ledger.independent_causal_evidence.any()


def test_alias_cycle_is_rejected():
    with pytest.raises(ValueError, match="Cyclic"):
        prune_catalog(catalog("a", "b"), policy(aliases={"a": ["b", "alias"], "b": ["a", "alias"]}))


def test_all_rows_are_accounted_for_and_terminal_representatives_exist():
    frame = pd.concat([
        catalog("SoHQ", "dchgCapa", "CE", "CI", {"feature_name": "cell_id", "role": "meta"}),
        catalog("SoHQ", "capacity", "odd_reference", namespace="studio_legacy", origin="legacy_initialized"),
    ], ignore_index=True)
    result = prune_catalog(frame, policy(aliases={"CI": ["CE", "100 - CE"]},
                                         families={"capacity_discharge": ["dchgCapa", "SoHQ"]}))
    assert len(result.ledger) == len(frame)
    assert not result.ledger.metric_id.duplicated().any()
    assert set(result.ledger.metric_id) == set(frame.metric_id)
    assert set(result.ledger.representative_metric_id) <= set(frame.metric_id)
    assert result.summary["before"] == sum(result.summary["decision_counts"].values())
    assert result.summary["before"] == (result.summary["primary"] + result.summary["removed_duplicates"]
                                         + result.summary["support_only"] + result.summary["review_required"])
    assert not result.summary["original_values_modified"]
    assert not result.summary["source_code_modified"]
    assert decision(result, "studio_legacy.SoHQ").representative_metric_id == "cyclediag.SoHQ"
    for row in result.ledger.loc[result.ledger.decision.eq("remove_duplicate")].itertuples():
        terminal = decision(result, row.representative_metric_id)
        assert terminal.decision != "remove_duplicate"
        assert row.representative_status == terminal.decision


def test_duplicate_metric_ids_and_missing_columns_are_rejected():
    with pytest.raises(ValueError, match="Duplicate metric_id"):
        prune_catalog(catalog("CE", "CE"), policy())
    with pytest.raises(ValueError, match="Missing catalog columns"):
        prune_catalog(pd.DataFrame({"feature_name": ["CE"]}), policy())


def test_registry_policy_is_read_as_data_without_executing_source(tmp_path):
    source = tmp_path / "registry.py"
    marker = tmp_path / "executed"
    source.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
        "EXACT_ALIASES: dict = {'CI': ('CE', '100 - CE')}\n"
        "FAMILIES = {'rest': ('rest_end', 'rest_30m')}\n", encoding="utf-8")
    loaded = read_registry_policy(source)
    assert loaded["EXACT_ALIASES"]["CI"] == ("CE", "100 - CE")
    assert len(loaded["source_sha256"]) == 64
    assert not marker.exists()


def test_incomplete_registry_policy_is_rejected(tmp_path):
    source = tmp_path / "registry.py"
    source.write_text("FAMILIES = {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="FAMILIES and EXACT_ALIASES"):
        read_registry_policy(source)
