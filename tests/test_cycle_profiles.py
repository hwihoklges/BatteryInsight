"""Tests for battery_insight.cycle_profiles on a synthetic project."""
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from battery_insight import cycle_profiles as cp
from battery_insight import derived_cycles as dc
from battery_insight import derived_steps as ds

CAP_UAH = 70000.0


def _source(n_cycles=25, pts=40, slow_cycle=12):
    st, v, q = [], [], []
    for c in range(1, n_cycles + 1):
        dur = 10800 if c == slow_cycle else 3600
        st += list(np.linspace(0, 3600, pts)); v += list(np.linspace(3000, 4200, pts)); q += list(np.linspace(0, CAP_UAH, pts))
        st += list(np.linspace(0, 600, pts)); v += [4180.0] * pts; q += [0.0] * pts
        st += list(np.linspace(0, dur, pts)); v += list(np.linspace(4180, 2500, pts)); q += list(np.linspace(0, CAP_UAH, pts))
        st += list(np.linspace(0, 600, pts)); v += [2600.0] * pts; q += [0.0] * pts
    return pd.DataFrame({"step_time_s": st, "voltage_native": v, "capacity_native": q})


@pytest.fixture
def project(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    src = proj / "src.parquet"
    proj.mkdir()
    _source().to_parquet(src, index=False)
    sha = "abc123"
    ds._build_one(src, ds.sidecar_path(proj, "d1"), "d1", sha)
    steps = ds.sidecar_path(proj, "d1")
    dc._build_one(steps, dc.cycles_sidecar_path(proj, "d1"), "d1", sha, True)
    state = {"unit": "uAh"}

    def fake_rows(project, **kw):
        return [{"dataset_id": "d1", "sha256": sha, "format": "native_cyc", "path": str(src),
                 "metadata": {"voltage_unit": "mV", "capacity_unit": state["unit"]}}]
    monkeypatch.setattr("battery_insight.library.library_rows", fake_rows)
    return proj, state


def test_select_cycles_rules():
    roles = {c: {"full_chg": 3600.0, "full_dchg": 3600.0} for c in range(1, 31)}
    roles[17] = {"full_chg": 3600.0, "full_dchg": 10800.0}   # slow RPT
    roles[22] = {"full_chg": 3600.0, "full_dchg": 1000.0}    # fast check
    roles[0] = {"rest": 10.0}
    got = cp.select_cycles(range(0, 31), roles, every=10)
    assert got == [1, 2, 3, 10, 17, 20, 22, 30]
    # first/last full cycle kept when others are not full
    roles = {c: {"rest": 1.0} for c in range(1, 40)}
    roles[7] = {"full_dchg": 5.0}
    roles[33] = {"full_chg": 5.0}
    assert cp.select_cycles(range(1, 40), roles, every=50) == [1, 2, 3, 7, 33]
    assert cp.select_cycles(range(1, 40), roles, every=50) == cp.select_cycles(range(39, 0, -1), roles, every=50)


def test_resample_linear_curve():
    q = np.linspace(0, 0.07, 50)
    v = 3.0 + (q / 0.07) * 1.2
    grid, total = cp.resample_curve(v, q, points=11)
    assert total == pytest.approx(0.07)
    assert np.allclose(grid, 3.0 + np.linspace(0, 1, 11) * 1.2, atol=1e-5)
    assert cp.resample_curve([3.0], [0.0]) is None


def test_dqdv_constant_slope():
    v = np.linspace(3.0, 4.2, 129)
    centers, d = cp.dqdv(0.07, v, bins=8)
    assert np.allclose(d, 0.07 / 1.2, rtol=1e-6)
    assert centers[0] > 3.0 and centers[-1] < 4.2


def test_build_and_incremental(project):
    proj, state = project
    first = cp.build_cycle_profiles(proj, every=10, points=32, workers=1, progress=None)
    assert first["n_datasets"] == 1 and first["statuses"] == {"built": 1}
    frame = cp.load_profiles(proj)
    # cycles 1,2,3,10,12(slow),20,25(last): chg + dchg each
    assert sorted(frame.cycle_derived.unique()) == [1, 2, 3, 10, 12, 20, 25]
    assert first["n_profiles"] == len(frame) == 14
    dch = frame[(frame.direction == "dchg") & (frame.cycle_derived == 10)].iloc[0]
    assert dch.q_total_ah == pytest.approx(0.07, rel=1e-5)
    assert dch.v[0] == pytest.approx(4.18, abs=1e-4) and dch.v[-1] == pytest.approx(2.5, abs=1e-4)
    assert np.all(np.diff(dch.v) < 0)
    chg = frame[(frame.direction == "chg") & (frame.cycle_derived == 2)].iloc[0]
    assert np.allclose(chg.v, np.linspace(3.0, 4.2, 32), atol=1e-4)
    assert len(pq.read_schema(cp.profiles_path(proj)).metadata[b"dataset_keys"]) > 0
    assert len(cp.load_profiles(proj, ["d1"], [10])) == 2
    assert cp.load_profiles(proj, ["nope"]).empty

    mtime = cp.profiles_path(proj).stat().st_mtime_ns
    again = cp.build_cycle_profiles(proj, every=10, points=32, workers=1, progress=None)
    assert again["statuses"] == {"reused": 1} and again["n_profiles"] == 14
    assert cp.profiles_path(proj).stat().st_mtime_ns == mtime

    third = cp.build_cycle_profiles(proj, every=5, points=32, workers=1, progress=None)
    assert third["statuses"] == {"built": 1} and third["n_profiles"] > 14

    state["unit"] = None
    skipped = cp.build_cycle_profiles(proj, every=5, points=32, workers=1, progress=None)
    assert skipped["statuses"] == {"units_unconfirmed": 1} and skipped["n_datasets"] == 0