import pandas as pd
import pytest

from battery_insight.compact_storage import (
    CompactWriter, CORE_COLUMNS, VOLTAGE_PROFILE_COLUMNS, save_compact_table, storage_policy,
)


def records():
    return pd.DataFrame({"cell_id": ["0001"] * 4, "test_id": ["001"] * 4, "cycle": [1, 1, 2, 2],
        "step_id": ["charge", "rest", "charge", "rest"], "segment_id": [1, 2, 3, 4],
        "time_s": [.01, .02, .03, .04], "step_time_s": [.01] * 4,
        "voltage_v": [3.123456, 3.234567, 3.345678, 3.456789],
        "current_a": [0.00491383, -0.000000001, 0.00512345, 0.], "capacity_ah": [0.012345678] * 4,
        "mode": ["unknown"] * 4, "end_flag": [""] * 4, "step_kind": ["charge", "rest", "charge", "rest"],
        "source_member": ["Restore/a.csv"] * 4, "source_row": [1, 2, 3, 4], "record_index": [1, 2, 3, 4],
        "unused_vendor_counter": [12345] * 4})


def test_default_preserves_all_voltage_profiles_and_identifiers_across_batches(tmp_path):
    frame = records()
    writer = CompactWriter(tmp_path / "signal.parquet")
    writer.write(frame.iloc[:2]); writer.write(frame.iloc[2:])
    report = writer.close()
    restored = pd.read_parquet(writer.path)
    assert report["source_rows"] == report["saved_rows"] == 4
    assert "unused_vendor_counter" in report["omitted_columns"]
    assert restored.cell_id.tolist() == ["0001"] * 4
    assert restored.test_id.tolist() == ["001"] * 4
    assert report["profile"] == "voltage_profiles"
    assert "current_a" not in restored
    assert "mode" not in restored and "end_flag" not in restored
    for col in VOLTAGE_PROFILE_COLUMNS + ["source_member", "source_row", "record_index"]:
        assert restored[col].tolist() == frame[col].tolist()
    assert report["precision"] == "float64_or_original_integer_no_rounding"
    assert report["row_selection"] == {"keep_cycles": None, "keep_steps": None}
    assert report["downsampled"] is False
    future = report["future_recalculation"]
    assert future["voltage_profile_columns_retained"] is True
    assert future["all_source_rows_retained"] is True
    assert future["current_column_retained"] is False
    limitations = {item["code"]: item for item in future["limitations"]}
    assert limitations["missing_current"]["requires_original_raw_for"] == ["dcir", "current_integration"]


def test_curves_option_preserves_current_and_feature_support_columns(tmp_path):
    frame = records()
    writer = CompactWriter(tmp_path / "signal.parquet", {"profile": "curves"})
    writer.write(frame)
    report = writer.close()
    restored = pd.read_parquet(writer.path)
    for column in CORE_COLUMNS:
        assert restored[column].tolist() == frame[column].tolist()
    assert report["future_recalculation"]["current_column_retained"] is True
    assert report["future_recalculation"]["limitations"] == []


def test_voltage_profiles_allow_optional_current_without_other_optional_fields(tmp_path):
    frame = records()
    writer = CompactWriter(tmp_path / "signal.parquet", {
        "keep_columns": VOLTAGE_PROFILE_COLUMNS + ["current_a"],
    })
    writer.write(frame)
    report = writer.close()
    restored = pd.read_parquet(writer.path)
    assert restored.current_a.tolist() == frame.current_a.tolist()
    assert "mode" not in restored and "unused_vendor_counter" not in restored
    assert "missing_current" not in {v["code"] for v in report["future_recalculation"]["limitations"]}
    assert "missing_mode_or_end_flag" in {v["code"] for v in report["future_recalculation"]["limitations"]}


def test_retention_uses_explicit_cycle_and_step_filters(tmp_path):
    writer = CompactWriter(tmp_path / "signal.parquet", {"keep_cycles": [2], "keep_steps": ["charge"]})
    writer.write(records())
    result = writer.close()
    saved = pd.read_parquet(writer.path)
    assert saved.source_row.tolist() == [3]
    assert result["saved_rows"] == 1 and result["discarded_rows"] == 3
    assert result["future_recalculation"]["all_source_rows_retained"] is False
    assert "omitted_rows" in {v["code"] for v in result["future_recalculation"]["limitations"]}


def test_features_only_writes_no_signal_file(tmp_path):
    writer = CompactWriter(tmp_path / "signal.parquet", {"profile": "features_only"})
    writer.write(records())
    report = writer.close()
    assert not writer.path.exists()
    assert report["saved_rows"] == 0 and report["source_rows"] == 4
    assert report["future_recalculation"]["voltage_profile_columns_retained"] is False


def test_full_fields_can_preserve_vendor_fields(tmp_path):
    writer = CompactWriter(tmp_path / "signal.parquet", {"profile": "full_fields"})
    writer.write(records())
    writer.close()
    assert "unused_vendor_counter" in pd.read_parquet(writer.path)


def test_signal_projection_does_not_project_gold_metric_tables(tmp_path):
    gold = pd.DataFrame({"cycle": [1, 2], "charge_capacity_ah": [0.1, 0.09], "dcir_10s_ohm": [1.1, 1.3]})
    policy = {"keep_cycles": [2], "keep_steps": ["charge"]}
    save_compact_table(gold, tmp_path / "cycles.parquet", policy)
    pd.testing.assert_frame_equal(pd.read_parquet(tmp_path / "cycles.parquet"), gold)


@pytest.mark.parametrize("policy", [{"keep_columns": ["voltage_v"]},
                                    {"profile": "curves", "keep_columns": VOLTAGE_PROFILE_COLUMNS},
                                    {"profile": "voltage_profiles", "keep_columns": [c for c in VOLTAGE_PROFILE_COLUMNS if c != "capacity_ah"]},
                                    {"compression_level": 30},
                                    {"keep_cycles": []}, {"keep_cycles": [1.5]}, {"delete_raw": True}])
def test_invalid_or_destructive_policies_rejected(policy):
    with pytest.raises(ValueError):
        storage_policy(policy)
