"""Unit tests for battery_insight.derived_steps.step_table on synthetic arrays."""
import numpy as np
import pandas as pd

from battery_insight import derived_steps as ds


def test_boundaries_on_step_time_reset():
    # three steps delimited by step_time_s resets (decreases)
    step_time = np.r_[np.linspace(0, 100, 5), np.linspace(0, 200, 5), np.linspace(0, 50, 5)]
    voltage = np.r_[np.linspace(3000, 4200, 5), np.linspace(4200, 2500, 5), [3000] * 5]
    capacity = np.r_[np.linspace(0, 1000, 5), np.linspace(0, 1000, 5), np.zeros(5)]
    frame = ds.step_table(step_time, voltage, capacity)
    assert list(frame.step_derived) == [1, 2, 3]
    assert list(frame.start_row) == [0, 5, 10]
    assert list(frame.n_points) == [5, 5, 5]
    # durations are the per-step max step_time
    assert frame.duration_s.tolist() == [100.0, 200.0, 50.0]
    # elapsed_start is cumulative sum of previous durations
    assert frame.elapsed_start_s.tolist() == [0.0, 100.0, 300.0]


def test_kind_classification_rest_chg_dchg():
    # chg (rising, capacity), dchg (falling, capacity), rest (no capacity)
    step_time = np.r_[np.linspace(0, 100, 5), np.linspace(0, 100, 5), np.linspace(0, 100, 5)]
    voltage = np.r_[np.linspace(3000, 4200, 5), np.linspace(4200, 2500, 5), [3500] * 5]
    capacity = np.r_[np.linspace(0, 5000, 5), np.linspace(0, 5000, 5), np.zeros(5)]
    frame = ds.step_table(step_time, voltage, capacity)
    assert list(frame.kind) == ["chg", "dchg", "rest"]


def test_sentinel_voltage_ignored():
    # a sentinel voltage row must not become v_min
    step_time = np.linspace(0, 100, 6)
    voltage = np.array([3000.0, 3100.0, -2.1e6, 3200.0, 3300.0, 3400.0])
    capacity = np.linspace(0, 1000, 6)
    frame = ds.step_table(step_time, voltage, capacity)
    assert len(frame) == 1
    # sentinel excluded from min/max
    assert frame.v_min.iloc[0] >= 3000.0
    assert frame.v_max.iloc[0] == 3400.0


def test_rest_detection_zero_capacity():
    step_time = np.linspace(0, 100, 5)
    voltage = np.array([3500.0, 3501.0, 3499.0, 3500.0, 3500.0])
    capacity = np.zeros(5)
    frame = ds.step_table(step_time, voltage, capacity)
    assert frame.kind.iloc[0] == "rest"


def test_empty_input():
    frame = ds.step_table([], [], [])
    assert len(frame) == 0
    assert list(frame.columns) == ds.STEP_COLUMNS


def test_sidecar_index_caches_footer_and_detects_change(tmp_path, monkeypatch):
    import pyarrow as pa
    import pyarrow.parquet as pq
    frame = ds.step_table(np.linspace(0, 100, 5), np.linspace(3000, 4200, 5), np.linspace(0, 1000, 5))
    out = tmp_path / "steps" / "a.parquet"
    out.parent.mkdir()

    def write(sha):
        table = pa.Table.from_pandas(frame, preserve_index=False).replace_schema_metadata(
            {"source_sha256": sha, "method": ds.STEP_METHOD})
        pq.write_table(table, out)

    write("s1")
    index = ds.SidecarIndex(out.parent)
    assert ds.is_current(out, "s1", index)
    assert index.meta(out)["_label_nulls"] == index.meta(out)["_rows"] == 1
    index.save()

    calls = []
    monkeypatch.setattr(ds, "read_meta", lambda p: calls.append(p))
    assert ds.is_current(out, "s1", ds.SidecarIndex(out.parent))   # served from _index.json
    assert calls == []
    monkeypatch.undo()

    import os
    write("s2-longer-sha")
    os.utime(out, ns=(1, 1))
    fresh = ds.SidecarIndex(out.parent)
    assert not ds.is_current(out, "s1", fresh) and ds.is_current(out, "s2-longer-sha", fresh)
    assert not ds.is_current(tmp_path / "steps" / "missing.parquet", "s1", fresh)
