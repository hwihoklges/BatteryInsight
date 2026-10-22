"""Registered-file ingestion boundaries, cache refresh and raw-readonly integration."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import time

import pandas as pd
import pytest

from battery_insight import experiment_updates as updates
from battery_insight.ingest import sha256_file


def metric_specs():
    return [
        {"metric_id": "local.ce", "label": "CE", "column": "coulombic_efficiency_pct",
         "unit": "%", "family": "coulombic_efficiency", "role": "indicator"},
        {"metric_id": "local.charge_v", "label": "Charge V", "column": "charge_mean_voltage_v",
         "unit": "V", "family": "charge_voltage", "role": "indicator"},
        {"metric_id": "local.discharge_v", "label": "Discharge V", "column": "discharge_mean_voltage_v",
         "unit": "V", "family": "discharge_voltage", "role": "indicator"},
    ]


def registration(name="registered", **changes):
    row = dict(experiment_id=name, label=name, cell_id="CELL_" + name, test_id="TEST_" + name,
               raw_path=name + ".csv", cohort="COHORT_A", protocol="CC_1A", chemistry="TEST_CHEMISTRY",
               temperature_c=25, test_type="Cycle", include=True)
    return row | changes


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def write_registrations(path, rows):
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8-sig")


def workspace(tmp_path, *, synthetic=False):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    row = registration()
    if synthetic:
        script = Path(__file__).parents[1] / "examples" / "make_demo.py"
        spec = importlib.util.spec_from_file_location("experiment_update_demo", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        template = module.make_demo(inbox)
        row["raw_path"] = "SYNTHETIC_demo.csv"
    else:
        (inbox / row["raw_path"]).write_text("valid-input\n", encoding="utf-8")
        template = tmp_path / "pipeline.json"
        write_json(template, {"sources": [{"path": "placeholder.csv"}], "external_features": []})
    registrations = tmp_path / "experiments.csv"
    write_registrations(registrations, [row])
    config = tmp_path / "relations.json"
    write_json(config, {
        "schema_version": 1, "inbox": "inbox", "registrations": registrations.name,
        "pipeline_template": str(template), "settle_seconds": 0,
        "metrics": metric_specs(), "comparison": {"life_min": 2, "life_max": 5, "min_points": 3},
    })
    return config, inbox, registrations, row, tmp_path / "output"


def write_run_tables(run, tables):
    gold = run / "gold"
    gold.mkdir(parents=True, exist_ok=True)
    artifacts = {}
    for name, table in tables.items():
        path = gold / (name + ".parquet")
        table.to_parquet(path, index=False)
        artifacts["gold/" + name + ".parquet"] = sha256_file(path)
    write_json(run / "run_manifest.json", {"status": "complete", "artifact_sha256": artifacts})


def fake_pipeline(monkeypatch):
    calls = []

    def run_pipeline(config_path, destination):
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        source = config["sources"][0]
        raw = Path(source["path"])
        if raw.read_bytes().startswith(b"malformed"):
            raise ValueError("malformed raw source")
        calls.append(copy.deepcopy(source))
        run = Path(destination) / ("run_" + str(len(calls)))
        cycles = pd.DataFrame({
            "cell_id": [source["cell_id"]] * 8, "test_id": [source["test_id"]] * 8,
            "test_type": [source["test_type"]] * 8, "include": [True] * 8,
            "cycle": range(1, 9), "life_index": range(1, 9),
            "coulombic_efficiency_pct": [100., 100., 100., 96., 92., 88., 84., 80.],
            "charge_mean_voltage_v": [3.5] * 8, "discharge_mean_voltage_v": [3.4] * 8,
        })
        write_run_tables(run, {"cycles": cycles})
        write_json(run / "input_snapshot.json", [{"sha256": sha256_file(raw)}])
        return run

    monkeypatch.setattr(updates, "run_pipeline", run_pipeline)
    monkeypatch.setattr(updates, "code_fingerprint", lambda: "fixed-test-code-version")
    return calls


def test_unregistered_raw_is_pending_and_registered_identity_is_explicit(tmp_path, monkeypatch):
    config, inbox, _, row, output = workspace(tmp_path)
    (inbox / "looks_like_CELL_999.csv").write_text("never infer identity", encoding="utf-8")
    calls = fake_pipeline(monkeypatch)
    payload = updates.update_experiments(config, output)
    assert payload["ingestion"]["processed"] == 1
    assert len(calls) == 1
    assert calls[0]["cell_id"] == row["cell_id"]
    assert calls[0]["test_id"] == row["test_id"]
    assert {entry["reason"] for entry in payload["ingestion"]["pending"]} == {"unregistered"}
    assert [node["experiment_id"] for node in payload["nodes"]] == [row["experiment_id"]]


@pytest.mark.parametrize("changes,match", [
    ({"experiment_id": "registered", "cell_id": "OTHER", "test_id": "OTHER"}, "Duplicate"),
    ({"experiment_id": "other", "cell_id": "CELL_registered", "test_id": "TEST_registered"}, "Duplicate"),
    ({"experiment_id": "other", "raw_path": "registered.csv"}, "One raw file"),
])
def test_registration_identity_and_raw_assignment_are_unique(tmp_path, changes, match):
    _, inbox, registrations, first, _ = workspace(tmp_path)
    second = registration("second", **changes)
    write_registrations(registrations, [first, second])
    with pytest.raises(ValueError, match=match):
        updates.load_registrations(registrations, inbox)


def test_registration_cannot_escape_inbox(tmp_path):
    _, inbox, registrations, row, _ = workspace(tmp_path)
    row["raw_path"] = "../outside.csv"
    write_registrations(registrations, [row])
    with pytest.raises(ValueError, match="inside inbox"):
        updates.load_registrations(registrations, inbox)


def test_recent_raw_and_excluded_registration_remain_pending(tmp_path, monkeypatch):
    config, inbox, registrations, first, output = workspace(tmp_path)
    settings = json.loads(config.read_text(encoding="utf-8"))
    settings["settle_seconds"] = 3600
    write_json(config, settings)
    excluded = registration("excluded", include=False)
    write_registrations(registrations, [first, excluded])
    (inbox / "excluded.csv").write_text("excluded", encoding="utf-8")
    os.utime(inbox / first["raw_path"], (time.time(), time.time()))
    calls = fake_pipeline(monkeypatch)
    payload = updates.update_experiments(config, output)
    assert not calls
    assert payload["nodes"] == []
    assert {p["reason"] for p in payload["ingestion"]["pending"]} == {
        "file_still_settling", "excluded_by_registration"}


def test_cache_refreshes_same_path_new_bytes_and_changed_registration(tmp_path, monkeypatch):
    config, inbox, registrations, row, output = workspace(tmp_path)
    calls = fake_pipeline(monkeypatch)
    raw = inbox / row["raw_path"]
    original_hash = sha256_file(raw)
    first = updates.update_experiments(config, output)
    assert first["ingestion"]["processed"] == 1
    assert sha256_file(raw) == original_hash
    cached = updates.update_experiments(config, output)
    assert cached["ingestion"]["reused"] == 1
    assert len(calls) == 1

    raw.write_text("valid-input-with-new-bytes\n", encoding="utf-8")
    changed_hash = sha256_file(raw)
    changed = updates.update_experiments(config, output)
    assert changed["ingestion"]["processed"] == 1
    assert changed["ingestion"]["records"][0]["raw_sha256"] == changed_hash != original_hash
    assert sha256_file(raw) == changed_hash
    assert len(calls) == 2

    row["label"] = "Updated registered label"
    row["cohort"] = "COHORT_B"
    write_registrations(registrations, [row])
    registered = updates.update_experiments(config, output)
    assert registered["ingestion"]["processed"] == 1
    assert registered["nodes"][0]["label"] == row["label"]
    assert registered["nodes"][0]["cohort"] == row["cohort"]
    assert len(calls) == 3


def test_verify_all_detects_content_change_with_preserved_size_and_mtime(tmp_path, monkeypatch):
    config, inbox, _, row, output = workspace(tmp_path)
    calls = fake_pipeline(monkeypatch)
    raw = inbox / row["raw_path"]
    updates.update_experiments(config, output)
    before = raw.stat()
    raw.write_bytes(raw.read_bytes().replace(b"valid", b"VALID"))
    os.utime(raw, ns=(before.st_atime_ns, before.st_mtime_ns))
    changed = updates.update_experiments(config, output, verify_all=True)
    assert len(calls) == 2
    assert changed["ingestion"]["processed"] == 1
    assert changed["ingestion"]["records"][0]["raw_sha256"] == sha256_file(raw)


def test_malformed_new_file_does_not_remove_healthy_cached_experiment(tmp_path, monkeypatch):
    config, inbox, registrations, first, output = workspace(tmp_path)
    fake_pipeline(monkeypatch)
    updates.update_experiments(config, output)
    second = registration("broken")
    (inbox / second["raw_path"]).write_text("malformed new file", encoding="utf-8")
    write_registrations(registrations, [first, second])
    result = updates.update_experiments(config, output)
    assert result["ingestion"]["reused"] == 1
    assert len(result["ingestion"]["failed"]) == 1
    assert result["ingestion"]["failed"][0]["experiment_id"] == "broken"
    assert [node["experiment_id"] for node in result["nodes"]] == ["registered"]
    state = json.loads((output / "update_state.json").read_text(encoding="utf-8"))
    assert set(state["records"]) == {"registered"}


def test_raw_removed_after_discovery_is_isolated_from_other_experiments(tmp_path, monkeypatch):
    config, inbox, registrations, first, output = workspace(tmp_path)
    fake_pipeline(monkeypatch)
    vanishing = registration("vanishing")
    raw = inbox / vanishing["raw_path"]
    raw.write_text("will vanish after discovery", encoding="utf-8")
    write_registrations(registrations, [first, vanishing])

    def after_discovery():
        raw.unlink()
        return "fixed-test-code-version"

    monkeypatch.setattr(updates, "code_fingerprint", after_discovery)
    result = updates.update_experiments(config, output)
    assert result["ingestion"]["processed"] == 1
    assert [node["experiment_id"] for node in result["nodes"]] == ["registered"]
    isolated = result["ingestion"]["failed"] + result["ingestion"]["pending"]
    assert any(entry["path"] == str(raw.resolve()) for entry in isolated)


def test_raw_changed_during_update_does_not_publish_stale_observations(tmp_path, monkeypatch):
    config, inbox, _, row, output = workspace(tmp_path)
    fake_pipeline(monkeypatch)
    original_run = updates.run_pipeline
    raw = inbox / row["raw_path"]

    def concurrent_writer(config_path, destination):
        run = original_run(config_path, destination)
        raw.write_text("valid-input modified by concurrent writer\n", encoding="utf-8")
        return run

    monkeypatch.setattr(updates, "run_pipeline", concurrent_writer)
    result = updates.update_experiments(config, output)
    assert len(result["ingestion"]["failed"]) == 1
    assert "Raw changed during update" in result["ingestion"]["failed"][0]["error"]
    assert result["nodes"] == []
    assert pd.read_parquet(output / "observations.parquet").empty


@pytest.mark.parametrize("role", ["target", "qc", "covariate", "diagnosis", "meta"])
def test_non_indicator_metric_cannot_enter_relationships(role):
    specs = metric_specs()
    specs[0]["role"] = role
    with pytest.raises(ValueError, match="indicator metrics only"):
        updates.validate_metrics(specs)


def test_unknown_local_metric_units_and_duplicate_family_are_rejected():
    specs = metric_specs()
    specs[0]["column"] = "capacity_retention_pct"
    with pytest.raises(ValueError, match="reviewed local indicator"):
        updates.validate_metrics(specs)
    specs = metric_specs()
    specs[0]["unit"] = "1"
    with pytest.raises(ValueError, match="reviewed local indicator"):
        updates.validate_metrics(specs)
    specs = metric_specs()
    specs.append(specs[0] | {"metric_id": "renamed_same_family"})
    with pytest.raises(ValueError, match="identity and family"):
        updates.validate_metrics(specs)


def test_modified_cached_artifact_is_not_silently_reused(tmp_path, monkeypatch):
    config, _, _, _, output = workspace(tmp_path)
    fake_pipeline(monkeypatch)
    first = updates.update_experiments(config, output)
    artifact = Path(first["ingestion"]["records"][0]["run_path"]) / "gold" / "cycles.parquet"
    artifact.write_bytes(b"modified cached result")
    second = updates.update_experiments(config, output)
    assert len(second["ingestion"]["failed"]) == 1
    assert "missing or modified" in second["ingestion"]["failed"][0]["error"]
    assert second["ingestion"]["reused"] == 0
    assert second["nodes"] == []


def test_external_metrics_observing_future_cycles_are_excluded(tmp_path):
    row = registration()
    cycles = pd.DataFrame({"cell_id": [row["cell_id"]] * 8, "test_id": [row["test_id"]] * 8,
                           "test_type": ["Cycle"] * 8, "include": [True] * 8,
                           "cycle": range(1, 9), "life_index": range(1, 9)})
    external = pd.DataFrame({"cell_id": [row["cell_id"]] * 2, "test_id": [row["test_id"]] * 2,
                            "cycle": [2, 4], "observed_until_cycle": [6, 4], "metric_id": ["external_r"] * 2,
                            "value": [999., 2.], "unit": ["mohm"] * 2, "role": ["indicator"] * 2,
                            "definition_version": ["v1"] * 2, "source_revision": ["sha-test"] * 2})
    run = tmp_path / "run"
    write_run_tables(run, {"cycles": cycles, "external_features": external})
    spec = {"metric_id": "reviewed.external", "label": "External", "column": "external_r",
            "unit": "mohm", "family": "external_r", "table": "external_features",
            "definition_version": "v1", "source_revision": "sha-test"}
    observations, issues = updates.observations_from_run(run, row, [spec], {"life_min": 2, "life_max": 5})
    assert [(point["life_index"], point["value"]) for point in observations] == [(4., 2.)]
    assert issues[0]["reason"] == "observes_beyond_comparison_window"
    assert issues[0]["rows"] == 1


def test_actual_synthetic_raw_pipeline_and_life_window_integration(tmp_path, monkeypatch):
    config, inbox, _, row, output = workspace(tmp_path, synthetic=True)
    monkeypatch.setattr(updates, "code_fingerprint", lambda: "fixed-test-code-version")
    raw = inbox / row["raw_path"]
    before = sha256_file(raw)
    result = updates.update_experiments(config, output)
    assert result["ingestion"]["failed"] == []
    assert result["ingestion"]["processed"] == 1
    assert sha256_file(raw) == before
    assert len(result["nodes"]) == 1
    ce = next(feature for feature in result["nodes"][0]["features"] if feature["metric_id"] == "local.ce")
    assert ce["status"] == "valid"
    assert (ce["first_life"], ce["last_life"], ce["valid_count"]) == (2., 5., 4)
    assert ce["value"] == pytest.approx(98.)
    observations = pd.read_parquet(output / "observations.parquet")
    assert set(observations.experiment_id) == {row["experiment_id"]}
    assert observations.life_index.min() == 1
    assert observations.life_index.max() == 8
    assert result["metadata"]["outside_window_rows_ignored"] == 12
    assert (output / "dashboard" / "index.html").is_file()
    assert json.loads((output / "dashboard" / "data.json").read_text(encoding="utf-8"))["nodes"] == result["nodes"]
    assert updates.update_experiments(config, output)["ingestion"]["reused"] == 1


def test_watch_stops_at_explicit_update_limit_without_scheduling_background_work(monkeypatch):
    calls, sleeps = [], []

    def update(config, output, *, verify_all):
        calls.append((config, output, verify_all))
        return {"update": len(calls)}

    monkeypatch.setattr(updates, "update_experiments", update)
    monkeypatch.setattr(updates.time, "sleep", sleeps.append)
    assert list(updates.watch_experiments("config", "output", interval=1, verify_all=True, max_updates=2)) == [
        {"update": 1}, {"update": 2}]
    assert calls == [("config", "output", True)] * 2
    assert sleeps == [1]


def test_output_and_inbox_must_not_be_nested(tmp_path):
    config, inbox, _, _, _ = workspace(tmp_path)
    with pytest.raises(ValueError, match="non-nested"):
        updates.update_experiments(config, inbox / "generated")


def test_invalid_comparison_window_is_rejected_before_raw_processing(tmp_path, monkeypatch):
    config, _, _, _, output = workspace(tmp_path)
    settings = json.loads(config.read_text(encoding="utf-8"))
    settings["comparison"] = {"life_min": 5, "life_max": 2}
    write_json(config, settings)
    calls = fake_pipeline(monkeypatch)
    with pytest.raises(ValueError, match="life_min"):
        updates.update_experiments(config, output)
    assert calls == []


@pytest.mark.parametrize("field", ["protocol", "chemistry", "test_type"])
def test_mixed_cohort_conditions_are_rejected_before_raw_processing(tmp_path, monkeypatch, field):
    config, inbox, registrations, first, output = workspace(tmp_path)
    second = registration("different", **{field: "DIFFERENT_CONDITION"})
    (inbox / second["raw_path"]).write_text("valid-input\n", encoding="utf-8")
    write_registrations(registrations, [first, second])
    calls = fake_pipeline(monkeypatch)
    with pytest.raises(ValueError, match="mixes " + field):
        updates.update_experiments(config, output)
    assert calls == []
