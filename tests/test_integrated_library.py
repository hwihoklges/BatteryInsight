import json
from pathlib import Path
import sqlite3

import pandas as pd
import pytest

from battery_insight import library
from battery_insight.integrated_selftest import run_self_test
from battery_insight.native_cyc.selftest import fixture_folder
from battery_insight.library_workbench import build_library_bundle, workbench_config, profile_metrics, profile_review
from battery_insight.unified_entry import main


@pytest.fixture
def project(tmp_path):
    raw = tmp_path / "raw_261022_set001"
    fixture_folder(raw)
    project = tmp_path / "project"
    library.convert_and_register(project, raw)
    return project


def test_integrated_selftest():
    result = run_self_test()
    assert result["status"] == "success", result
    assert all(result["checks"].values())


def test_hints_do_not_assign_experiment_facts(project):
    row = library.library_rows(project)[0]
    assert row["metadata"] == {}
    assert row["review_status"] == "unclassified"
    hints = library.filename_hints("260522_set001/M01Ch001[001]/RPT.parquet")
    assert hints["date_candidates"] == ["2026-05-22"]
    assert hints["set_candidates"] == ["001"]


@pytest.mark.parametrize("metadata", [{"test_type": "guessed"}, {"voltage_unit": "guess"},
    {"capacity_unit": "mV"}, {"date_kind": "test"}, {"set_no": 1}, {"pressure_mpa": -1},
    {"cathode_active_wt_pct": 101}, {"time_s": 1}, {"temperature_c": float("inf")}])
def test_invalid_metadata_rejected(metadata):
    with pytest.raises((ValueError, TypeError)):
        library.validate_metadata(metadata)


def test_source_revision_requires_review(project):
    import pyarrow as pa
    import pyarrow.parquet as pq
    row = library.library_rows(project)[0]
    library.assign_metadata(project, row["dataset_id"], {"sample_name": "original", "set_no": "001"})
    table = pq.read_table(row["path"])
    table = table.set_column(table.schema.get_field_index("voltage_native"), table.schema.field("voltage_native"), pa.array([4000.] * table.num_rows))
    pq.write_table(table, row["path"])
    with pytest.raises(ValueError, match="변경"):
        library.get_dataset(project, row["dataset_id"])
    root = Path(row["path"])
    while root.name != "parquet":
        root = root.parent
    library.scan_folder(project, root)
    updated = library.get_dataset(project, row["dataset_id"])
    assert updated["review_status"] == "needs_review"
    assert updated["metadata"]["sample_name"] == "original"
    assert not updated["metadata_current"]


def test_rules_conflicts_do_not_override_reviewed_metadata(project):
    row = library.library_rows(project)[0]
    library.assign_metadata(project, row["dataset_id"], {"test_type": "RPT"})
    library.set_rules(project, [{"rule_id": "cycle", "path_glob": "*", "metadata": {"test_type": "Cycle"}}])
    result = library.apply_rules(project)
    assert row["dataset_id"] in [item["dataset_id"] for item in result["conflicts"]]
    assert library.get_dataset(project, row["dataset_id"])["metadata"]["test_type"] == "RPT"
    with pytest.raises(ValueError, match="복제"):
        library.set_rules(project, [{"rule_id": "bad", "path_glob": "*", "metadata": {"physical_cell_id": "same"}}])


def test_registration_sheet_all_validated_before_apply(project, tmp_path):
    path = library.export_registration(project, tmp_path / "registry.csv")
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    frame["confirmed"] = "true"
    frame.loc[0, "sample_name"] = "must-not-commit"
    frame.loc[1, "source_sha256"] = "0" * 64
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    with pytest.raises(ValueError, match="해시"):
        library.import_registration(project, path)
    assert all(not row["metadata"] for row in library.library_rows(project))


def test_unknown_sqlite_not_modified(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    with sqlite3.connect(project / "library.sqlite3") as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
    with pytest.raises(ValueError):
        library.library_rows(project)
    with sqlite3.connect(project / "library.sqlite3") as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("unrelated",)]


def test_profile_cache_tampering_recomputed(project):
    row = library.library_rows(project)[0]
    library.assign_metadata(project, row["dataset_id"], {"voltage_unit": "mV", "capacity_unit": "uAh"})
    row = library.get_dataset(project, row["dataset_id"])
    expected = profile_metrics(project, row)
    path = next((project / "profile_metrics").glob(row["dataset_id"] + "*.parquet"))
    path.write_bytes(b"corrupted")
    pd.testing.assert_frame_equal(profile_metrics(project, row), expected)


def test_external_import_explicit_identity_and_future_cutoff(project, tmp_path):
    row = next(row for row in library.library_rows(project) if row["relative_path"] == "sample.parquet")
    export = tmp_path / "external.csv"
    pd.DataFrame({"cycle": [1, 2], "metric": [.8, .9]}).to_csv(export, index=False)
    spec = {"path": str(export), "expected_cell_id": "A", "expected_test_id": "T", "mapping": {
        "namespace": "verified", "layout": "wide", "columns": {"cycle": "cycle"},
        "constants": {"cell_id": "A", "test_id": "T", "definition_version": "1", "source_revision": "a" * 40, "observed_until_cycle": 10},
        "metrics": {"metric": {"metric_id": "indicator", "unit": "1", "role": "indicator", "family": "independent"}}}}
    config = tmp_path / "external.json"
    config.write_text(json.dumps(spec), encoding="utf-8")
    assert library.import_feature_export(project, row["dataset_id"], config)["rows"] == 2
    bundle = build_library_bundle(workbench_config(project), project / "workbench")
    external = [item for item in bundle["observations"] if item["metric_id"].startswith("external.")]
    assert len(external) == 2
    assert all(item["observed_until_life"] == 10 for item in external)
    assert not next(item for item in bundle["metrics"] if item["metric_id"].startswith("external."))["selected_by_default"]
    from battery_insight.workbench_analysis import analyze_bundle
    result = analyze_bundle(bundle, {"metric_ids": [external[0]["metric_id"]], "design_field_ids": [],
        "enabled_modules": ["network"], "life_min": 1, "life_max": 2, "min_overlap": 1, "min_points": 2})
    assert result["graphs"]["electrochem"]["metadata"]["cutoff_exclusions"]
    spec["expected_test_id"] = "wrong"
    config.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ValueError, match="이외"):
        library.import_feature_export(project, row["dataset_id"], config)


def test_profile_review_no_causal_claim_or_constant_baseline_threshold():
    frame = pd.DataFrame({"cycle": [1, 2, 3, 4], "mean_voltage_v": [3., 3.01, 2.99, 4.5]})
    flags = profile_review(frame)
    flag = next(item for item in flags if item["status"] == "needs_review")
    assert flag["cycle"] == 4 and flag["causal_diagnosis"] is False
    frame["mean_voltage_v"] = [3., 3., 3., 4.5]
    assert not any(item["status"] == "needs_review" for item in profile_review(frame))


def test_result_json_protects_input_and_existing_file(project, tmp_path):
    original = project / "library.sqlite3"
    before = original.read_bytes()
    assert main(["library-scan", "--project", str(project), "--input", str(project), "--result-json", str(original)]) == 1
    assert original.read_bytes() == before
    config = tmp_path / "config.json"
    config.write_text('{"important": true}')
    assert main(["version", "--result-json", str(config)]) == 1
    assert json.loads(config.read_text())["important"] is True


def test_result_json_protects_new_project_before_creation(tmp_path):
    project = tmp_path / "not-created-yet"
    assert main(["version", "--project", str(project), "--result-json", str(project / "library_workbench.json")]) == 1
    assert not project.exists()


def test_cli_parse_failure_exported_for_windowed_exe(tmp_path):
    result = tmp_path / "error.json"
    assert main(["version", "--unknown", "--result-json", str(result)]) == 1
    assert json.loads(result.read_text())["status"] == "failed"


def test_metadata_change_blocks_stale_curve_reference(project):
    from battery_insight.library_workbench import library_evidence
    row = next(row for row in library.library_rows(project) if row["relative_path"] == "sample.parquet")
    bundle = build_library_bundle(workbench_config(project), project / "workbench")
    reference = bundle["evidence_references"][row["dataset_id"]]
    library.assign_metadata(project, row["dataset_id"], {"voltage_unit": "mV", "capacity_unit": "uAh"})
    with pytest.raises(ValueError, match="単位|단위"):
        library_evidence(reference)


def test_catalogue_read_does_not_mutate_db(project):
    before = library.file_hash(project / "library.sqlite3")
    library.library_rows(project)
    library.get_dataset(project, library.library_rows(project)[0]["dataset_id"])
    assert before == library.file_hash(project / "library.sqlite3")


def test_diagnostic_encoding_error_cannot_change_success(monkeypatch, tmp_path):
    import sys
    class Cp949Stream:
        def write(self, text):
            text.encode("cp949")
        def flush(self):
            pass
    monkeypatch.setattr(sys, "stdout", Cp949Stream())
    monkeypatch.setattr(library, "scan_folder", lambda *args, **kwargs: {"status": "success", "path": "╕.parquet"})
    result = tmp_path / "result.json"
    assert main(["library-scan", "--project", str(tmp_path / "project"), "--input", str(tmp_path / "input"), "--result-json", str(result)]) == 0
    assert json.loads(result.read_text(encoding="utf-8"))["status"] == "success"
