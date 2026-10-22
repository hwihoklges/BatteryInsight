"""Direct immutable database runs, explicit RPT axes and provenance references."""
import copy
import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from battery_insight.database_workbench import build_database_bundle
from battery_insight.ingest import sha256_file
from battery_insight.workbench_server import Workbench


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")


def database_fixture(tmp_path, types=("RPT", "RPT")):
    root, output = tmp_path / "database", tmp_path / "workbench"
    rows = []
    container = tmp_path / "original_folder"
    container.mkdir()
    (container / "keep.bin").write_bytes(b"immutable raw folder")
    for index, test_type in enumerate(types):
        identity = f"e{index}"
        run = root / "runs" / identity
        row = {"experiment_id": identity, "cell_id": f"c{index}", "test_id": f"t{index}", "test_type": test_type,
               "label": identity, "cohort": "same_input", "protocol": "P1", "chemistry": "NMC", "temperature_c": 25,
               "design_id": "D1", "batch_id": "B1", "rpt_stage": "BOL" if test_type == "RPT" else "",
               "cell_identity_status": "explicit", "source_container": str(container), "source_id": f"source{index}",
               "source_sha256": hashlib.sha256(f"channel{index}".encode()).hexdigest(), "status": "complete", "include": True,
               "run_path": str(run)}
        members = [{"member": "ch/Restore001.dat", "sha256": "a" * 64}, {"member": "ch/Restore002.dat", "sha256": "b" * 64}]
        source = {"kind": "pne_restore", "path": str(container), "sha256": row["source_sha256"],
                  "cell_id": row["cell_id"], "test_id": row["test_id"], "source_id": row["source_id"], "members": members}
        cycles = []
        silver = []
        for cycle in range(1, 4):
            cycles.append({"cell_id": row["cell_id"], "test_id": row["test_id"], "test_type": test_type, "include": True,
                           "cycle": cycle, "life_index": None if test_type == "RPT" else cycle,
                           "charge_mean_voltage_v": 3 + index + cycle / 100,
                           "discharge_mean_voltage_v": 2 + index + cycle / 100,
                           "charge_cc_capacity_ah": 0.8, "charge_cv_capacity_ah": 0.2})
            for point in range(40):
                member = members[point // 20]
                silver.append({"cell_id": row["cell_id"], "test_id": row["test_id"], "test_type": test_type, "include": True,
                               "cycle": cycle, "segment_id": f"cc{cycle}", "step_id": f"cc{cycle}", "time_s": cycle * 100 + point,
                               "step_kind": "charge",
                               "step_time_s": point, "voltage_v": 3 + point * 0.01, "current_a": 1., "mode": "CC",
                               "capacity_ah": point / 3600, "soc_pct": point, "source_path": str(container), "source_hash": member["sha256"],
                               "source_member": member["member"], "source_member_row": point % 20 + 1,
                               "source_row": (cycle - 1) * 40 + point + 1})
        (run / "gold").mkdir(parents=True)
        pd.DataFrame(cycles).to_parquet(run / "gold" / "cycles.parquet", index=False)
        partition = run / "silver" / hashlib.sha256(row["test_id"].encode()).hexdigest()[:16]
        partition.mkdir(parents=True)
        silver_path = partition / "part-000000.parquet"
        pd.DataFrame(silver).convert_dtypes(dtype_backend="pyarrow").to_parquet(silver_path, index=False, row_group_size=40)
        row["raw_path"] = str(silver_path)
        write_json(run / "config_snapshot.json", {"features": {}, "sources": [source | {"test_type": test_type}]})
        artifacts = {p.relative_to(run).as_posix(): sha256_file(p) for p in run.rglob("*") if p.is_file()}
        write_json(run / "run_manifest.json", {"status": "complete", "identity": {"inputs": [source]},
                                               "source_row_basis": "channel_record_index", "artifact_sha256": artifacts})
        rows.append(row)
    write_json(root / "database_manifest.json", {"schema_version": "raw_database_v1", "experiments": rows, "summary": {"complete": len(rows)}})
    pd.DataFrame(rows).to_csv(root / "experiments.csv", index=False, encoding="utf-8-sig")
    config = tmp_path / "relations.json"
    specs = [{"metric_id": "charge_v", "column": "charge_mean_voltage_v", "label": "Charge voltage", "unit": "V", "family": "charge_voltage"},
             {"metric_id": "discharge_v", "column": "discharge_mean_voltage_v", "label": "Discharge voltage", "unit": "V", "family": "discharge_voltage"}]
    write_json(config, {"schema_version": 1, "database_path": str(root), "metrics": specs,
                        "comparison": {"life_min": 2, "life_max": 2, "summary": "snapshot"}, "design": {"fields": []}})
    return config, root, output, rows


def test_database_snapshot_uses_runs_directly_without_copying_raw_or_rewriting_life_axis(tmp_path, monkeypatch):
    config, root, output, rows = database_fixture(tmp_path)
    original = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    from battery_insight import workbench_server
    monkeypatch.setattr(workbench_server, "update_experiments", lambda *a, **k: pytest.fail("DB mode must not invoke raw update pipeline"))
    controller = Workbench(config, output)
    assert not (output / "lake").exists() and not (output / "observations.parquet").exists()
    assert all(path.read_bytes() == data for path, data in original.items())
    assert controller.bundle["axis_kind"] == "rpt_internal_cycle"
    assert controller.bundle["axis_by_cohort"][0]["is_lifetime"] is False
    assert controller.options["min_points"] == 1 and controller.options["summary"] == "snapshot"
    assert controller.analysis["graphs"]["electrochem"]["edges"][0]["shared_metrics"] == 2
    mean = next(row for row in controller.analysis["replicates"] if row["metric_id"] == "charge_v")
    assert mean["mean"] == pytest.approx(3.52) and mean["n_cells"] == 2
    assert pd.read_parquet(Path(rows[0]["run_path"]) / "gold" / "cycles.parquet").life_index.isna().all()
    assert any(spec["metric_id"] == "pack.cc_cv.cc_fraction_pct" for spec in controller.bundle["metrics"])
    assert "pack.cc_cv.cc_fraction_pct" not in controller.options["metric_ids"]
    json.dumps(controller.state(), allow_nan=False)


def test_mixed_axes_and_protocols_cannot_create_cross_axis_or_cross_protocol_edges(tmp_path):
    config, root, output, _ = database_fixture(tmp_path, ("RPT", "Cycle", "RPT"))
    registry = pd.read_csv(root / "experiments.csv", dtype=str, keep_default_na=False)
    registry.loc[2, "protocol"] = "P2"
    registry.to_csv(root / "experiments.csv", index=False, encoding="utf-8-sig")
    controller = Workbench(config, output)
    assert controller.bundle["axis_kind"] == "mixed"
    assert len(controller.bundle["axis_by_cohort"]) == 3
    assert not controller.analysis["graphs"]["electrochem"]["edges"]


def test_registry_metadata_refresh_preserves_selections_and_never_modifies_database(tmp_path):
    config, root, output, _ = database_fixture(tmp_path)
    controller = Workbench(config, output)
    options = copy.deepcopy(controller.options)
    options.update(metric_ids=[], design_field_ids=[], enabled_modules=["network"])
    controller.compare(options)
    registry = pd.read_csv(root / "experiments.csv", dtype=str, keep_default_na=False)
    registry.loc[0, "label"] = "renamed"
    registry.to_csv(root / "experiments.csv", index=False, encoding="utf-8-sig")
    before = (root / "experiments.csv").read_bytes()
    controller.refresh()
    assert controller.options["metric_ids"] == [] and controller.options["design_field_ids"] == []
    assert controller.bundle["experiments"][0]["label"] == "renamed"
    assert (root / "experiments.csv").read_bytes() == before
    assert not controller.state()["capabilities"]["registration"]
    for action in (lambda: controller.preview_upload(b"id\n1\n", "x.csv"), lambda: controller.validate_registration([]),
                   lambda: controller.apply_registration([], expected_registry_hash=controller.bundle["registry_hash"])):
        with pytest.raises(ValueError, match="experiments.csv"):
            action()


@pytest.mark.parametrize("key", ["cell_id", "test_id", "test_type", "source_id"])
def test_registry_cannot_redirect_immutable_source_identity(tmp_path, key):
    config, root, output, _ = database_fixture(tmp_path)
    registry = pd.read_csv(root / "experiments.csv", dtype=str, keep_default_na=False)
    registry.loc[0, key] = "tampered"
    registry.to_csv(root / "experiments.csv", index=False, encoding="utf-8-sig")
    with pytest.raises(ValueError, match="immutable"):
        build_database_bundle(config, output)


def test_stale_registry_source_locations_never_override_current_manifest_and_new_rows_appear(tmp_path):
    config, root, output, rows = database_fixture(tmp_path, ("RPT", "RPT", "RPT"))
    registry = pd.read_csv(root / "experiments.csv", dtype=str, keep_default_na=False).iloc[:2].copy()
    registry.loc[0, ["run_path", "raw_path", "source_sha256", "status"]] = ["old_run", "old_raw", "old_sha", "failed"]
    registry.to_csv(root / "experiments.csv", index=False, encoding="utf-8-sig")
    before = (root / "experiments.csv").read_bytes()
    bundle = build_database_bundle(config, output)
    assert len(bundle["experiments"]) == len(bundle["evidence_references"]) == 3
    assert bundle["experiments"][0]["run_path"] == rows[0]["run_path"]
    assert bundle["experiments"][0]["source_sha256"] == rows[0]["source_sha256"]
    assert {issue["reason"] for issue in bundle["issues"]} >= {"new_database_experiment_uses_manifest_metadata", "stale_registry_source_fields_ignored"}
    assert (root / "experiments.csv").read_bytes() == before


def test_native_evidence_verifies_member_hashes_and_returns_original_member_rows(tmp_path):
    config, root, output, rows = database_fixture(tmp_path)
    controller = Workbench(config, output)
    evidence = controller.evidence("e0", 2)
    assert evidence["axis_kind"] == "rpt_internal_cycle"
    assert evidence["cycles"] == [{"cycle": i, "life_index": i} for i in (1, 2, 3)]
    curve = evidence["curves"][0]
    assert curve["source"]["row_basis"] == "channel_record_index"
    assert curve["source"]["member"] == "ch/Restore001.dat" and curve["source"]["last_member"] == "ch/Restore002.dat"
    assert {point["source_member"] for point in curve["points"]} == {"ch/Restore001.dat", "ch/Restore002.dat"}
    assert curve["points"][0]["source_member_row"] == 1
    run = Path(rows[0]["run_path"])
    (run / "run_manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="manifest changed"):
        controller.evidence("e0", 2)


def test_modified_gold_is_rejected_without_replacing_prior_published_workbench(tmp_path):
    config, root, output, rows = database_fixture(tmp_path)
    controller = Workbench(config, output)
    previous = copy.deepcopy(controller.state())
    published = (output / "dashboard" / "analysis.json").read_bytes()
    gold = Path(rows[0]["run_path"]) / "gold" / "cycles.parquet"
    gold.write_bytes(gold.read_bytes() + b"modified")
    with pytest.raises(ValueError, match="modified"):
        controller.refresh()
    assert controller.state() == previous
    assert (output / "dashboard" / "analysis.json").read_bytes() == published


def test_fixed_reference_survives_database_refresh_and_profile_roundtrip(tmp_path):
    config, _, output, _ = database_fixture(tmp_path)
    controller = Workbench(config, output)
    reference = controller.save_baseline()["reference"]
    profile = controller.save_profile("DB snapshot", controller.options, ["network"])["profile"]
    controller.refresh()
    assert controller.analysis["metadata"]["reference"]["reference_id"] == reference["reference_id"]
    reloaded = Workbench(config, output)
    assert reloaded.options["summary"] == "snapshot" and reloaded.options["scale_mode"] == "fixed"
    assert reloaded.profiles() == [profile]


def test_physical_cell_alias_collapses_repeated_tests_without_changing_source_identity(tmp_path):
    config, root, output, rows = database_fixture(tmp_path)
    registry = pd.read_csv(root / "experiments.csv", dtype=str, keep_default_na=False)
    registry["physical_cell_id"] = "physical_001"
    registry.to_csv(root / "experiments.csv", index=False, encoding="utf-8-sig")
    controller = Workbench(config, output)
    assert {row["cell_id"] for row in controller.bundle["experiments"]} == {"physical_001"}
    assert {row["source_cell_id"] for row in controller.bundle["experiments"]} == {"c0", "c1"}
    assert {row["cell_identity_status"] for row in controller.bundle["experiments"]} == {"mapped_physical_alias"}
    mean = next(row for row in controller.analysis["replicates"] if row["metric_id"] == "charge_v")
    assert mean["n_cells"] == 1 and mean["n_tests"] == 2 and mean["std"] is None
    assert mean["mean"] == pytest.approx(3.52)
    assert controller.evidence("e0", 2)["curves"]
    assert controller.evidence("e1", 2)["curves"]
    for row in rows:
        assert pd.read_parquet(Path(row["run_path"]) / "gold" / "cycles.parquet").cell_id.eq(row["cell_id"]).all()


def test_unconfirmed_channel_identity_keeps_nodes_but_does_not_claim_independent_cell_statistics(tmp_path):
    config, root, output, rows = database_fixture(tmp_path)
    manifest = json.loads((root / "database_manifest.json").read_text())
    for row in manifest["experiments"]:
        row["cell_identity_status"] = "provisional_channel_identity"
    write_json(root / "database_manifest.json", manifest)
    pd.DataFrame(manifest["experiments"]).to_csv(root / "experiments.csv", index=False, encoding="utf-8-sig")
    controller = Workbench(config, output)
    assert len(controller.analysis["graphs"]["electrochem"]["nodes"]) == 2
    assert controller.analysis["graphs"]["electrochem"]["edges"]
    assert controller.analysis["replicates"] == []
    meta = controller.analysis["metadata"]["replicates"]
    assert len(meta["provisional_entities"]) == 2
    assert all("physical_cell_id_unconfirmed" in row["missing"] for row in meta["excluded_entities"])
    assert all(row["n_cells"] == 0 and row["excluded_provisional_entities"] == 2 for row in controller.analysis["graphs"]["electrochem"]["metric_correlations"])


def test_response_capacity_and_retention_share_one_comparison_family():
    from battery_insight.experiment_updates import validate_metrics
    capacity = {"metric_id": "q", "column": "discharge_capacity_ah", "label": "Q", "unit": "Ah", "family": "discharge_capacity"}
    retention = {"metric_id": "soh", "column": "capacity_retention_pct", "label": "Retention", "unit": "%", "family": "discharge_capacity"}
    validate_metrics([capacity])
    validate_metrics([retention])
    with pytest.raises(ValueError, match="one metric per identity and family"):
        validate_metrics([capacity, retention])


def test_voltage_only_retention_preserves_profiles_without_inventing_current_or_derivatives(tmp_path):
    config, root, output, rows = database_fixture(tmp_path)
    for row in rows:
        run = Path(row["run_path"])
        silver = Path(row["raw_path"])
        frame = pd.read_parquet(silver).drop(columns=["current_a", "mode", "soc_pct"])
        frame.to_parquet(silver, index=False, row_group_size=40)
        manifest = json.loads((run / "run_manifest.json").read_text())
        manifest["artifact_sha256"][silver.relative_to(run).as_posix()] = sha256_file(silver)
        write_json(run / "run_manifest.json", manifest)
    controller = Workbench(config, output)
    assert "pack.cc_cv.cc_fraction_pct" in {metric["metric_id"] for metric in controller.bundle["metrics"]}
    assert "pack.cc_cv.cv_duration_s" not in {metric["metric_id"] for metric in controller.bundle["metrics"]}
    evidence = controller.evidence("e0", 2)
    assert {"current_a", "mode", "soc_pct"}.issubset(evidence["unavailable_signals"])
    assert {"voltage_v", "time_s", "capacity_ah"}.issubset(evidence["available_signals"])
    assert evidence["curves"]
    for curve in evidence["curves"]:
        assert curve["leg"] == "charge" and curve["direction_basis"] == "recorded_step_kind"
        assert not curve["dqdv"] and not curve["dvdq"]
        assert curve["derivative"]["reason"] == "current_or_explicit_CC_mode_not_retained"
        assert all(point["current_a"] is None and point["soc_pct"] is None for point in curve["points"])
    json.dumps(evidence, allow_nan=False)


def test_concurrent_database_metadata_change_does_not_publish_a_mixed_snapshot(tmp_path, monkeypatch):
    from battery_insight import database_workbench
    config, root, output, _ = database_fixture(tmp_path)
    real_build = database_workbench.build_workbench_bundle

    def changing_registry(*args, **kwargs):
        bundle = real_build(*args, **kwargs)
        with (root / "experiments.csv").open("a", encoding="utf-8") as stream:
            stream.write("\n")
        return bundle

    monkeypatch.setattr(database_workbench, "build_workbench_bundle", changing_registry)
    with pytest.raises(ValueError, match="changed during analysis"):
        build_database_bundle(config, output)
