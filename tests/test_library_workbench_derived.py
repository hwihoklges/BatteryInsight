"""Derived-cycle fallback for files without PNE cycle labels.

Covers library_workbench.profile_metrics / build_library_bundle behaviour
when the Parquet 'cycle' column is absent or entirely NULL: it should fall
back to a DERIVED cycle sidecar (battery_insight.derived_cycles), marking the
result's provenance ("derived_cycle") without ever touching the source file
or mixing in with files that already carry real PNE labels.
"""
import sys
import types

import pandas as pd
import pytest

from battery_insight import library
from battery_insight.library_workbench import build_library_bundle, profile_metrics, workbench_config
from cyc2parquet.selftest import fixture_folder


@pytest.fixture
def project(tmp_path):
    raw = tmp_path / "raw_261023_set001"
    fixture_folder(raw)
    project = tmp_path / "project"
    library.convert_and_register(project, raw)
    return project


def _rows_by_path(project):
    return {row["relative_path"]: row for row in library.library_rows(project)}


def _unlabelled_row(project):
    rows = _rows_by_path(project)
    key = next(path for path in rows if "하위" in path)
    return rows[key]


def _labelled_row(project):
    return _rows_by_path(project)["sample.parquet"]


def _install_fake_derived_cycles(monkeypatch, mapping):
    """Install a stand-in battery_insight.derived_cycles module.

    mapping: dataset_id -> pandas.DataFrame(start_row, n_points, cycle_derived) or None
    """
    module = types.ModuleType("battery_insight.derived_cycles")

    def load_row_cycles(project, dataset_id, sha256):
        return mapping.get(dataset_id)

    module.load_row_cycles = load_row_cycles
    monkeypatch.setitem(sys.modules, "battery_insight.derived_cycles", module)
    return module


def test_unlabelled_file_without_sidecar_module_stays_empty(project, monkeypatch):
    monkeypatch.delitem(sys.modules, "battery_insight.derived_cycles", raising=False)
    orphan = _unlabelled_row(project)
    library.assign_metadata(project, orphan["dataset_id"], {"voltage_unit": "mV", "capacity_unit": "uAh"})
    orphan = library.get_dataset(project, orphan["dataset_id"])
    frame = profile_metrics(project, orphan)
    assert frame.empty
    assert frame.attrs["axis_kind"] == "recorded_cycle"


def test_unlabelled_file_uses_derived_cycles(project, monkeypatch):
    orphan = _unlabelled_row(project)
    library.assign_metadata(project, orphan["dataset_id"], {"voltage_unit": "mV", "capacity_unit": "uAh"})
    orphan = library.get_dataset(project, orphan["dataset_id"])
    assert orphan["format"] == "native_cyc"

    # orphan parquet has 4 rows (no CTS match -> cycle all NULL); split 2+2.
    derived = pd.DataFrame({"start_row": [0, 2], "n_points": [2, 2], "cycle_derived": [101, 102]})
    _install_fake_derived_cycles(monkeypatch, {orphan["dataset_id"]: derived})

    frame = profile_metrics(project, orphan)
    assert frame.attrs["axis_kind"] == "derived_cycle"
    assert sorted(frame["cycle"].tolist()) == [101, 102]
    assert len(frame) == 2
    assert not frame["mean_voltage_v"].isna().all()


def test_derived_cycle_cache_invalidates_when_sidecar_changes(project, monkeypatch):
    orphan = _unlabelled_row(project)
    library.assign_metadata(project, orphan["dataset_id"], {"voltage_unit": "mV", "capacity_unit": "uAh"})
    orphan = library.get_dataset(project, orphan["dataset_id"])

    derived_a = pd.DataFrame({"start_row": [0, 2], "n_points": [2, 2], "cycle_derived": [1, 2]})
    _install_fake_derived_cycles(monkeypatch, {orphan["dataset_id"]: derived_a})
    first = profile_metrics(project, orphan)
    assert sorted(first["cycle"].tolist()) == [1, 2]

    derived_b = pd.DataFrame({"start_row": [0], "n_points": [4], "cycle_derived": [7]})
    _install_fake_derived_cycles(monkeypatch, {orphan["dataset_id"]: derived_b})
    second = profile_metrics(project, orphan)
    assert second["cycle"].tolist() == [7]
    assert second.attrs["axis_kind"] == "derived_cycle"


def test_labelled_file_ignores_derived_sidecar_and_is_unaffected(project, monkeypatch):
    sample = _labelled_row(project)
    library.assign_metadata(project, sample["dataset_id"], {"voltage_unit": "mV", "capacity_unit": "uAh"})
    sample = library.get_dataset(project, sample["dataset_id"])
    expected = profile_metrics(project, sample)
    assert expected.attrs["axis_kind"] == "recorded_cycle"
    assert sorted(expected["cycle"].tolist()) == [1, 2]

    # Even if a (bogus) derived sidecar exists for this labelled file, it must
    # be ignored entirely -- labelled files never mix with derived cycles.
    derived = pd.DataFrame({"start_row": [0], "n_points": [6], "cycle_derived": [999]})
    _install_fake_derived_cycles(monkeypatch, {sample["dataset_id"]: derived})
    actual = profile_metrics(project, sample)
    pd.testing.assert_frame_equal(actual, expected)
    assert actual.attrs["axis_kind"] == "recorded_cycle"


def test_build_library_bundle_marks_derived_axis_per_row(project, monkeypatch):
    orphan = _unlabelled_row(project)
    sample = _labelled_row(project)
    library.assign_metadata(project, orphan["dataset_id"], {"voltage_unit": "mV", "capacity_unit": "uAh"})
    library.assign_metadata(project, sample["dataset_id"], {"voltage_unit": "mV", "capacity_unit": "uAh"})

    derived = pd.DataFrame({"start_row": [0, 2], "n_points": [2, 2], "cycle_derived": [1, 2]})
    _install_fake_derived_cycles(monkeypatch, {orphan["dataset_id"]: derived})

    bundle = build_library_bundle(workbench_config(project), project / "workbench")
    by_id = {row["experiment_id"]: row for row in bundle["experiments"]}
    assert by_id[orphan["dataset_id"]]["axis_kind"] == "derived_cycle"
    assert "재구성" in by_id[orphan["dataset_id"]]["axis_label"]
    assert by_id[sample["dataset_id"]]["axis_kind"] == "recorded_cycle"
    # Both an unlabelled (now derived) and a labelled test are present -> mixed.
    assert bundle["axis_kind"] == "mixed"
    derived_observations = [item for item in bundle["observations"] if item["experiment_id"] == orphan["dataset_id"]]
    assert {item["life_index"] for item in derived_observations} == {1.0, 2.0}


def test_build_library_bundle_all_recorded_when_no_derived_module(project, monkeypatch):
    monkeypatch.delitem(sys.modules, "battery_insight.derived_cycles", raising=False)
    sample = _labelled_row(project)
    library.assign_metadata(project, sample["dataset_id"], {"voltage_unit": "mV", "capacity_unit": "uAh"})
    bundle = build_library_bundle(workbench_config(project), project / "workbench")
    assert bundle["axis_kind"] == "recorded_cycle"
