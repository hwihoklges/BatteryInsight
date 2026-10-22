"""The registry design map and measured-indicator map stay independent."""
import json
from pathlib import Path

import pandas as pd
import pytest

from battery_insight import experiment_updates as updates
from battery_insight.ingest import sha256_file
from test_experiment_updates import (
    fake_pipeline, registration, workspace, write_json, write_registrations, write_run_tables,
)


def design_config():
    return {
        "fields": [
            {"field_id": "cathode_material", "label": "Cathode material", "kind": "categorical",
             "group": "cathode_material", "weight": 1},
            {"field_id": "cathode_active_mass_loading_mg_cm2", "label": "Cathode active mass loading",
             "kind": "numeric", "unit": "mg/cm2", "scale": 10., "group": "cathode_loading", "weight": 1},
            {"field_id": "cathode_composition", "label": "Cathode dry composition", "kind": "composition",
             "unit": "wt%", "composition_basis": "electrode_total", "group": "cathode_composition", "weight": 1,
             "components": ["cam_wt_pct", "se_wt_pct", "carbon_wt_pct", "binder_wt_pct"]},
        ],
        "settings": {"min_fields": 3, "min_coverage": 1.},
    }


def design_row(row, **changes):
    return row | {"design_group": "DESIGN_SPACE", "cathode_material": "CAM_A",
                  "cathode_active_mass_loading_mg_cm2": 10., "cam_wt_pct": 86.,
                  "se_wt_pct": 12., "carbon_wt_pct": 1., "binder_wt_pct": 1.} | changes


def dual_workspace(tmp_path, *, count=3):
    config, inbox, registrations, first, output = workspace(tmp_path)
    rows = [design_row(first)] + [design_row(registration(f"sample_{index}")) for index in range(1, count)]
    for row in rows[1:]:
        (inbox / row["raw_path"]).write_text("valid-input\n", encoding="utf-8")
    write_registrations(registrations, rows)
    settings = json.loads(config.read_text(encoding="utf-8"))
    settings["design"] = design_config()
    write_json(config, settings)
    return config, inbox, registrations, rows, output


def varied_fake_pipeline(monkeypatch, rows):
    """Fixed per-cell observations give the echem map real, nonempty edges."""
    calls = fake_pipeline(monkeypatch)
    base = updates.run_pipeline
    offsets = {row["cell_id"]: index * index for index, row in enumerate(rows)}
    changes = {}

    def run(config_path, destination):
        run_path = base(config_path, destination)
        source = json.loads(Path(config_path).read_text(encoding="utf-8"))["sources"][0]
        cell = source["cell_id"]
        frame = pd.read_parquet(run_path / "gold" / "cycles.parquet")
        frame["coulombic_efficiency_pct"] -= offsets[cell] + changes.get(cell, 0)
        frame["charge_mean_voltage_v"] += offsets[cell] * .01
        frame["discharge_mean_voltage_v"] += offsets[cell] * .02
        write_run_tables(run_path, {"cycles": frame})
        return run_path

    monkeypatch.setattr(updates, "run_pipeline", run)
    return calls, changes


def edge_for(graph, left, right):
    return next(edge for edge in graph["edges"] if {edge["source"], edge["target"]} == {left, right})


def test_design_map_includes_raw_pending_enabled_registrations(tmp_path, monkeypatch):
    config, inbox, registrations, rows, output = dual_workspace(tmp_path)
    pending = inbox / rows[1]["raw_path"]
    pending.unlink()
    rows[2]["include"] = False
    write_registrations(registrations, rows)
    calls, _ = varied_fake_pipeline(monkeypatch, rows)
    payload = updates.update_experiments(config, output)
    assert payload["ingestion"]["failed"] == []
    assert len(calls) == 1
    assert {node["experiment_id"] for node in payload["nodes"]} == {rows[0]["experiment_id"]}
    assert {node["experiment_id"] for node in payload["design_graph"]["nodes"]} == {
        rows[0]["experiment_id"], rows[1]["experiment_id"]}
    assert edge_for(payload["design_graph"], rows[0]["experiment_id"], rows[1]["experiment_id"])["similarity"] == 1.
    assert {entry["reason"] for entry in payload["ingestion"]["pending"]} == {
        "not_found_or_pattern_mismatch", "excluded_by_registration"}
    assert payload["design_graph"]["metadata"]["raw_feature_inference"] is False


def test_design_edit_changes_only_design_scores_and_not_echem_observations(tmp_path, monkeypatch):
    config, inbox, registrations, rows, output = dual_workspace(tmp_path)
    varied_fake_pipeline(monkeypatch, rows)
    raw_hashes = {row["raw_path"]: sha256_file(inbox / row["raw_path"]) for row in rows}
    before = updates.update_experiments(config, output)
    original_observations = pd.read_parquet(output / "observations.parquet")
    assert len(before["edges"]) == 3
    assert len(before["design_graph"]["edges"]) == 3

    rows[1]["cathode_active_mass_loading_mg_cm2"] = 20.
    write_registrations(registrations, rows)
    after = updates.update_experiments(config, output)
    first_id, second_id = (row["experiment_id"] for row in rows[:2])
    assert edge_for(before["design_graph"], first_id, second_id)["similarity"] == 1.
    assert edge_for(after["design_graph"], first_id, second_id)["similarity"] == pytest.approx(2 / 3)
    pd.testing.assert_frame_equal(pd.read_parquet(output / "observations.parquet"), original_observations)
    for key in ("nodes", "edges", "cohorts", "metric_correlations", "excluded_pairs"):
        assert after[key] == before[key], key
    assert before["design_graph"]["metadata"]["registration_sha256"] != after["design_graph"]["metadata"]["registration_sha256"]
    assert after["ingestion"]["failed"] == []
    assert {row["raw_path"]: sha256_file(inbox / row["raw_path"]) for row in rows} == raw_hashes


def test_changed_echem_observations_do_not_change_registered_design_map(tmp_path, monkeypatch):
    config, inbox, _, rows, output = dual_workspace(tmp_path)
    _, changes = varied_fake_pipeline(monkeypatch, rows)
    before = updates.update_experiments(config, output)
    assert len(before["edges"]) == 3
    changes[rows[1]["cell_id"]] = 7.
    (inbox / rows[1]["raw_path"]).write_text("valid-input with revised measurement\n", encoding="utf-8")
    after = updates.update_experiments(config, output)
    first_id, second_id = (row["experiment_id"] for row in rows[:2])
    assert edge_for(after, first_id, second_id)["similarity"] != pytest.approx(edge_for(before, first_id, second_id)["similarity"])
    assert after["design_graph"] == before["design_graph"]


def test_design_snapshot_records_registry_hash_fields_basis_and_values(tmp_path, monkeypatch):
    config, _, registrations, rows, output = dual_workspace(tmp_path)
    varied_fake_pipeline(monkeypatch, rows)
    payload = updates.update_experiments(config, output)
    snapshot = json.loads((output / "design_registry_snapshot.json").read_text(encoding="utf-8"))
    assert snapshot["source"] == str(registrations.resolve())
    assert snapshot["sha256"] == sha256_file(registrations)
    assert snapshot["fields"] == design_config()["fields"]
    composition = next(field for field in snapshot["fields"] if field["kind"] == "composition")
    assert composition["unit"] == "wt%"
    assert composition["composition_basis"] == "electrode_total"
    assert len(snapshot["records"]) == 3
    recorded = next(row for row in snapshot["records"] if row["experiment_id"] == rows[0]["experiment_id"])
    assert recorded["cathode_material"] == "CAM_A"
    assert float(recorded["cathode_active_mass_loading_mg_cm2"]) == 10.
    assert payload["metadata"]["relationship_views"] == ["electrode_design", "electrochemical_metrics"]
    exported = json.loads((output / "dashboard" / "data.json").read_text(encoding="utf-8"))
    assert exported["design_graph"] == payload["design_graph"]


@pytest.mark.parametrize("allow", [False, True])
def test_mixed_chemistry_requires_explicit_echem_opt_in(tmp_path, monkeypatch, allow):
    config, _, registrations, rows, output = dual_workspace(tmp_path)
    rows[1]["chemistry"] = "OTHER_CHEMISTRY"
    write_registrations(registrations, rows)
    settings = json.loads(config.read_text(encoding="utf-8"))
    if allow:
        settings["comparison"]["allow_mixed_chemistry"] = True
    write_json(config, settings)
    calls, _ = varied_fake_pipeline(monkeypatch, rows)
    if not allow:
        with pytest.raises(ValueError, match="mixes chemistry"):
            updates.update_experiments(config, output)
        assert calls == []
    else:
        payload = updates.update_experiments(config, output)
        assert len(payload["nodes"]) == len(rows)
        assert len(payload["edges"]) == 3
        assert len(payload["design_graph"]["edges"]) == 3
        assert payload["metadata"]["allow_mixed_chemistry"] is True


def test_design_can_connect_different_echem_cohorts_without_mixed_opt_in(tmp_path, monkeypatch):
    config, _, registrations, rows, output = dual_workspace(tmp_path, count=2)
    rows[1]["chemistry"] = "OTHER_CHEMISTRY"
    rows[1]["cohort"] = "DIFFERENT_MEASUREMENT_COHORT"
    write_registrations(registrations, rows)
    varied_fake_pipeline(monkeypatch, rows)
    payload = updates.update_experiments(config, output)
    assert len(payload["nodes"]) == 2
    assert payload["edges"] == []
    assert len(payload["design_graph"]["edges"]) == 1
    assert payload["metadata"]["allow_mixed_chemistry"] is False


def test_mixed_chemistry_opt_in_does_not_bypass_protocol_guard(tmp_path, monkeypatch):
    config, _, registrations, rows, output = dual_workspace(tmp_path)
    rows[1]["chemistry"] = "OTHER_CHEMISTRY"
    rows[1]["protocol"] = "INCOMPATIBLE_PROTOCOL"
    write_registrations(registrations, rows)
    settings = json.loads(config.read_text(encoding="utf-8"))
    settings["comparison"]["allow_mixed_chemistry"] = True
    write_json(config, settings)
    calls, _ = varied_fake_pipeline(monkeypatch, rows)
    with pytest.raises(ValueError, match="mixes protocol"):
        updates.update_experiments(config, output)
    assert calls == []


def test_missing_design_values_are_not_inferred_from_raw_or_chemistry(tmp_path, monkeypatch):
    config, _, registrations, rows, output = dual_workspace(tmp_path, count=2)
    rows[1]["cathode_material"] = ""
    rows[1]["binder_wt_pct"] = ""
    write_registrations(registrations, rows)
    varied_fake_pipeline(monkeypatch, rows)
    payload = updates.update_experiments(config, output)
    node = next(node for node in payload["design_graph"]["nodes"] if node["experiment_id"] == rows[1]["experiment_id"])
    features = {feature["metric_id"]: feature for feature in node["features"]}
    assert features["cathode_material"]["value"] is None
    assert features["cathode_composition"]["value"] is None
    assert features["cathode_composition"]["missing_components"] == ["binder_wt_pct"]
    assert payload["design_graph"]["edges"] == []
    assert len(payload["nodes"]) == 2
    assert payload["design_graph"]["metadata"]["raw_feature_inference"] is False


def test_old_config_without_design_remains_compatible(tmp_path, monkeypatch):
    config, _, _, _, output = workspace(tmp_path)
    fake_pipeline(monkeypatch)
    payload = updates.update_experiments(config, output)
    assert payload["ingestion"]["failed"] == []
    assert payload["ingestion"]["processed"] == 1
    assert len(payload["nodes"]) == 1
    assert "design_graph" not in payload
    assert not (output / "design_registry_snapshot.json").exists()
    assert (output / "dashboard" / "index.html").is_file()
