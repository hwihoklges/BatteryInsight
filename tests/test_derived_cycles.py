"""Unit tests for battery_insight.derived_cycles on synthetic step tables."""
import numpy as np
import pandas as pd
import pytest

from battery_insight import derived_cycles as dc
from battery_insight import derived_steps as ds


def _steps_from_records(step_time, voltage, capacity):
    """Build a step table via the real step reconstruction (no project data)."""
    return ds.step_table(step_time, voltage, capacity)


def _cc_cycle_records(n_cycles, pts=10, cap=70000.0):
    """Synthetic CC charge/rest/discharge/rest cycles as per-record arrays."""
    st, v, q = [], [], []
    for _ in range(n_cycles):
        # charge: rising voltage, accumulating capacity
        st += list(np.linspace(0, 3600, pts)); v += list(np.linspace(3000, 4200, pts)); q += list(np.linspace(0, cap, pts))
        # rest
        st += list(np.linspace(0, 600, pts)); v += [4180] * pts; q += [0.0] * pts
        # discharge: falling voltage
        st += list(np.linspace(0, 3600, pts)); v += list(np.linspace(4180, 2500, pts)); q += list(np.linspace(0, cap, pts))
        # rest
        st += list(np.linspace(0, 600, pts)); v += [2600] * pts; q += [0.0] * pts
    return np.array(st), np.array(v), np.array(q)


def test_naive_cc_cycles_boundaries():
    st, v, q = _cc_cycle_records(3)
    steps = _steps_from_records(st, v, q)
    out = dc.assign_cycles(steps)
    # kinds should alternate chg/rest/dchg/rest
    assert list(out.kind) == ["chg", "rest", "dchg", "rest"] * 3
    # three charges -> three cycles (first charge opens cycle, then each after a full discharge)
    assert list(out.cycle_derived) == [1, 1, 1, 1, 2, 2, 2, 2, 3, 3, 3, 3]
    # discharge_index increments per full discharge, NA elsewhere
    di = out.discharge_index.tolist()
    assert [x for x in di if x is not pd.NA and not pd.isna(x)] == [1, 2, 3]
    assert pd.isna(di[0])  # charge has no discharge index


def test_pulses_do_not_open_cycles():
    # one real cycle, then an HPPC-like train of short charge/discharge pulses
    st, v, q = _cc_cycle_records(1)
    # pulse train: alternate tiny dchg / tiny chg, 30 s, ~600 uAh each
    for _ in range(6):
        st = np.r_[st, np.linspace(0, 30, 5)]; v = np.r_[v, np.linspace(4000, 3900, 5)]; q = np.r_[q, np.linspace(0, 600, 5)]
        st = np.r_[st, np.linspace(0, 30, 5)]; v = np.r_[v, np.linspace(3900, 4000, 5)]; q = np.r_[q, np.linspace(0, 600, 5)]
    steps = _steps_from_records(st, v, q)
    out = dc.assign_cycles(steps)
    # the pulse charges must NOT open new cycles -> at most 2 cycles total
    assert out.cycle_derived.max() <= 2
    roles = set(out.step_role)
    assert "pulse_chg" in roles and "pulse_dchg" in roles
    assert "full_chg" in roles and "full_dchg" in roles


def test_rest_only_is_one_cycle():
    st = np.r_[np.linspace(0, 600, 10), np.linspace(0, 600, 10)]
    v = np.r_[[3100] * 10, [3100] * 10]
    q = np.zeros(20)
    steps = _steps_from_records(st, v, q)
    out = dc.assign_cycles(steps)
    assert set(out.step_role) == {"rest"}
    assert out.cycle_derived.nunique() == 1
    assert out.discharge_index.isna().all()


def test_leading_rest_opens_at_first_charge():
    # rest, then charge, then discharge -> first charge opens cycle 2 (PNE counts lead rest as cycle 1)
    st = np.r_[np.linspace(0, 600, 10), np.linspace(0, 3600, 10), np.linspace(0, 3600, 10)]
    v = np.r_[[3000] * 10, np.linspace(3000, 4200, 10), np.linspace(4200, 2500, 10)]
    q = np.r_[np.zeros(10), np.linspace(0, 70000, 10), np.linspace(0, 70000, 10)]
    steps = _steps_from_records(st, v, q)
    out = dc.assign_cycles(steps)
    assert list(out.kind) == ["rest", "chg", "dchg"]
    assert list(out.cycle_derived) == [1, 2, 2]


def test_cycle_summary_units_and_efficiency():
    st, v, q = _cc_cycle_records(2)
    steps = _steps_from_records(st, v, q)
    out = dc.assign_cycles(steps)
    summary = dc.cycle_summary(out)
    assert list(summary.cycle_derived) == [1, 2]
    row = summary.iloc[0]
    # 70000 uAh -> 0.07 Ah
    assert row.charge_capacity_ah == pytest.approx(0.07, rel=1e-6)
    assert row.discharge_capacity_ah == pytest.approx(0.07, rel=1e-6)
    assert row.coulombic_efficiency == pytest.approx(1.0, rel=1e-6)
    # voltages in volts
    assert 2.4 < row.min_voltage_v < 4.3
    assert 3.0 < row.max_voltage_v <= 4.25
    assert row.has_pulses == False


def test_load_row_cycles_stale_sha_returns_none(tmp_path):
    st, v, q = _cc_cycle_records(2)
    steps = _steps_from_records(st, v, q)
    out = dc.assign_cycles(steps)
    summary = dc.cycle_summary(out)
    import pyarrow as pa
    import pyarrow.parquet as pq
    dataset_id = "deadbeef"
    sha = "a" * 64
    target = dc.cycles_sidecar_path(tmp_path, dataset_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    arrow = pa.Table.from_pandas(out[dc.STEP_OUT_COLUMNS], preserve_index=False)
    arrow = arrow.replace_schema_metadata({"dataset_id": dataset_id, "source_sha256": sha,
                                           "step_method": ds.STEP_METHOD, "cycle_method": dc.CYCLE_METHOD})
    pq.write_table(arrow, target)

    good = dc.load_row_cycles(tmp_path, dataset_id, sha)
    assert good is not None
    assert list(good.columns) == ["start_row", "n_points", "cycle_derived"]
    assert good.start_row.is_monotonic_increasing
    assert str(good.start_row.dtype) == "int64"
    # stale sha -> None
    assert dc.load_row_cycles(tmp_path, dataset_id, "b" * 64) is None
    # missing sidecar -> None
    assert dc.load_row_cycles(tmp_path, "nope", sha) is None


def test_load_row_cycles_method_mismatch_returns_none(tmp_path):
    st, v, q = _cc_cycle_records(1)
    out = dc.assign_cycles(_steps_from_records(st, v, q))
    import pyarrow as pa
    import pyarrow.parquet as pq
    dataset_id = "feed"
    sha = "c" * 64
    target = dc.cycles_sidecar_path(tmp_path, dataset_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    arrow = pa.Table.from_pandas(out[dc.STEP_OUT_COLUMNS], preserve_index=False)
    arrow = arrow.replace_schema_metadata({"dataset_id": dataset_id, "source_sha256": sha,
                                           "step_method": ds.STEP_METHOD, "cycle_method": "other_method"})
    pq.write_table(arrow, target)
    assert dc.load_row_cycles(tmp_path, dataset_id, sha) is None


def test_empty_steps():
    empty = pd.DataFrame(columns=ds.STEP_COLUMNS)
    out = dc.assign_cycles(empty)
    assert len(out) == 0
    assert "cycle_derived" in out.columns
    summary = dc.cycle_summary(out)
    assert len(summary) == 0
