import hashlib
import json

import pandas as pd
import pytest

from battery_insight.adapters import import_external_features, inspect_assets, known_export_mapping


def mapping():
    return {"namespace": "legacy", "layout": "wide",
            "columns": {"cell_id": "Cell", "cycle": "Cycle", "observed_until_cycle": "Seen"},
            "constants": {"test_id": "test-1", "definition_version": "reviewed-v1",
                          "source_revision": "a" * 40},
            "metrics": {"Q": {"metric_id": "discharge_capacity", "unit": "mAh",
                               "role": "target", "family": "capacity_discharge"}}}


def test_wide_preserves_ids_units_cutoff_and_exact_file_hash(tmp_path):
    source = tmp_path / "features.csv"
    source.write_text("Cell,Cycle,Seen,Q\n001,1,12,1300\n001,2,12,\n", encoding="utf-8")
    result = import_external_features(source, mapping())
    assert result["cell_id"].tolist() == ["001", "001"]
    assert result["unit"].tolist() == ["mAh", "mAh"]
    assert result["observed_until_cycle"].tolist() == [12, 12]
    assert result["source_sha256"].unique().tolist() == [hashlib.sha256(source.read_bytes()).hexdigest()]
    assert result["source_row"].tolist() == [1, 2]
    assert result["quality_status"].tolist() == ["ok", "missing"]
    assert result.loc[0, "metric_id"] == "legacy.discharge_capacity"
    assert pd.isna(result.loc[1, "value"])


@pytest.mark.parametrize("field", ["source_revision", "definition_version", "test_id"])
def test_required_metadata_not_inferred(tmp_path, field):
    source = tmp_path / "features.csv"
    source.write_text("Cell,Cycle,Seen,Q\na,1,1,2\n")
    config = mapping()
    config["constants"].pop(field)
    with pytest.raises(ValueError, match=field):
        import_external_features(source, config)


@pytest.mark.parametrize("rows", ["a,1,1,2\na,1,1,2\n", "a,1,1,2\na,1,1,3\n"])
def test_duplicate_keys_even_equal_values_are_rejected(tmp_path, rows):
    source = tmp_path / "features.csv"
    source.write_text("Cell,Cycle,Seen,Q\n" + rows)
    with pytest.raises(ValueError, match="duplicate external feature keys"):
        import_external_features(source, mapping())


def test_short_revision_and_future_cycle_contradiction_rejected(tmp_path):
    source = tmp_path / "features.csv"
    source.write_text("Cell,Cycle,Seen,Q\na,2,1,2\n")
    with pytest.raises(ValueError, match="cannot precede"):
        import_external_features(source, mapping())
    config = mapping()
    config["constants"]["source_revision"] = "abc1234"
    with pytest.raises(ValueError, match="full 40-char"):
        import_external_features(source, config)


def test_ragged_and_duplicate_header_csv_are_not_silently_loaded(tmp_path):
    source = tmp_path / "features.csv"
    source.write_text("Cell,Cycle,Seen,Q\na,2,2\n")
    with pytest.raises(ValueError, match="expected 4 fields"):
        import_external_features(source, mapping())
    source.write_text("Cell,Cycle,Seen,Q,Q\na,2,2,3,4\n")
    with pytest.raises(ValueError, match="duplicate column"):
        import_external_features(source, mapping())


@pytest.mark.parametrize("value", ["unknown", "inf", "-inf"])
def test_nonnumeric_and_infinite_metrics_are_rejected(tmp_path, value):
    source = tmp_path / "features.csv"
    source.write_text(f"Cell,Cycle,Seen,Q\na,2,2,{value}\n")
    with pytest.raises(ValueError):
        import_external_features(source, mapping())


def test_long_requires_allowlist_and_unit_match(tmp_path):
    source = tmp_path / "features.csv"
    source.write_text("Cell,Cycle,Seen,Feature,Value,Unit\na,1,1,Q,2,Ah\n")
    config = mapping()
    config["layout"] = "long"
    config["columns"].update({"metric_id": "Feature", "value": "Value", "unit": "Unit"})
    with pytest.raises(ValueError, match="unit mismatch"):
        import_external_features(source, config)
    config["metrics"]["Q"]["unit"] = "Ah"
    result = import_external_features(source, config)
    assert result["value"].tolist() == [2.0]
    source.write_text("Cell,Cycle,Seen,Feature,Value,Unit\na,1,1,Unknown,2,Ah\n")
    with pytest.raises(ValueError, match="undeclared metrics"):
        import_external_features(source, config)


def test_existing_l2_jsonl_is_imported_without_ratio_percent_confusion(tmp_path):
    source = tmp_path / "L2_cycles.jsonl"
    source.write_text(json.dumps({"cell_id": "001", "CycleNum": 7,
                                 "Q_discharge_mAh": 12, "Q_charge_mAh": 12.1,
                                 "CE": 12 / 12.1, "retention": 0.98}) + "\n")
    config = known_export_mapping("data_analysis_l2_cycles", test_id="XRD-cycle",
                                  source_revision="b" * 64, definition_version="l2-reviewed-1",
                                  observed_until_cycle=100)
    result = import_external_features(source, config).set_index("metric_id")
    assert result.loc["l2.CE", "unit"] == "1"
    assert result.loc["l2.retention", "value"] == 0.98
    assert result.loc["l2.Q_discharge_mAh", "unit"] == "mAh"
    with pytest.raises(ValueError, match="AIM requires its actual schema"):
        known_export_mapping("aim", test_id="t", source_revision="a" * 40,
                             definition_version="v1", observed_until_cycle=10)


def test_pickle_is_never_loaded(tmp_path):
    source = tmp_path / "features.pkl"
    source.write_bytes(b"not a pickle")
    with pytest.raises(ValueError, match="pickle is never loaded"):
        import_external_features(source, mapping())


def test_inventory_ast_does_not_execute_code_and_reports_dynamic_limit(tmp_path):
    marker = tmp_path / "EXECUTED"
    source = tmp_path / "lges_catalog.py"
    source.write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('bad')\n"
        "CAPACITY_COLS = ['CE', 'SoHQ']\n"
        "def AIM_life_predict():\n    return None\n"
        "FAMILIES = {'efficiency': ('CE', 'CI')}\n"
        "EXACT_ALIASES = {'CI': ('CE', '100-CE')}\n"
        "x = [f'peak_{i}' for i in range(4)]\n", encoding="utf-8")
    (tmp_path / "raw.csv").write_text("private,data\n1,2\n")
    report = inspect_assets([str(tmp_path)])
    assert not marker.exists()
    assert report["complete"]
    assert report["counts"]["files"] == 1
    assert report["files"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert {row["name"] for row in report["feature_candidates"]} == {"CE", "CI", "SoHQ"}
    assert report["symbols"][0]["name"] == "AIM_life_predict"
    assert all("source_sha256" in row for row in report["feature_candidates"])


def test_inventory_missing_and_limited_inputs_explicitly_incomplete(tmp_path):
    report = inspect_assets([str(tmp_path / "missing")])
    assert not report["complete"]
    (tmp_path / "one.py").write_text("A=1\n")
    (tmp_path / "two.py").write_text("B=2\n")
    report = inspect_assets([str(tmp_path)], max_files=1)
    assert not report["complete"]
    assert report["counts"]["files"] == 1
