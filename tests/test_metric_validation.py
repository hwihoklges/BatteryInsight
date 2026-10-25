"""Tests for battery_insight.metric_validation (G9), on synthetic data only."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from battery_insight import derived_steps, metric_validation as mv


# --------------------------------------------------------------------------- #
# Synthetic step-table builders
# --------------------------------------------------------------------------- #
def _cycle_records(n_cycles, q_chg=100000.0, q_dchg=95000.0, m=40,
                   label_fn=lambda k: k):
    """Raw per-record arrays for ``n_cycles`` charge+discharge cycles.

    Each cycle is a rising charge step then a falling discharge step; capacity
    resets to zero and ramps to q each step; recorded ``cycle`` label per record
    is ``label_fn(k)`` for cycle ``k`` (1-based).
    """
    step_time, voltage, capacity, cycle, step = [], [], [], [], []
    sidx = 0
    for k in range(1, n_cycles + 1):
        for kind, q, v0, v1 in (("chg", q_chg, 2500.0, 4200.0),
                                ("dchg", q_dchg, 4200.0, 2500.0)):
            sidx += 1
            step_time += list(np.arange(1, m + 1) * 60.0)
            voltage += list(np.linspace(v0, v1, m))
            capacity += list(np.linspace(0, q, m))
            cycle += [label_fn(k)] * m
            step += [sidx] * m
    return (np.array(step_time), np.array(voltage), np.array(capacity),
            np.array(cycle), np.array(step))


def _steps(n_cycles, **kw):
    st, v, c, cyc, stp = _cycle_records(n_cycles, **kw)
    return derived_steps.step_table(st, v, c, cyc, stp)


# --------------------------------------------------------------------------- #
# Pure functions
# --------------------------------------------------------------------------- #
def test_per_cycle_series_capacities_and_ce():
    steps = _steps(3)
    roles = mv.step_roles(steps)
    labels = steps["label_cycle"].to_numpy()
    series = mv.per_cycle_series(steps, labels, roles, 1e-6, 1e-3)
    assert list(series["cycle"]) == [1, 2, 3]
    # charge 100000 uAh -> 0.1 Ah, discharge 95000 -> 0.095 Ah
    assert series["charge_capacity_ah"].iloc[0] == pytest.approx(0.1, rel=1e-6)
    assert series["discharge_capacity_ah"].iloc[0] == pytest.approx(0.095, rel=1e-6)
    assert series["coulombic_efficiency"].iloc[0] == pytest.approx(0.95, rel=1e-6)
    assert series["is_full"].all()


def test_dataset_level_metrics_full_and_retention():
    steps = _steps(120)
    roles = mv.step_roles(steps)
    series = mv.per_cycle_series(steps, steps["label_cycle"].to_numpy(), roles, 1e-6, 1e-3)
    m = mv.dataset_level_metrics(series)
    assert m["n_full_cycles"] == 120
    assert m["cap_initial_ah"] == pytest.approx(0.095, rel=1e-6)
    # constant capacity -> 100% retention at 50 and 100
    assert m["retention_50_pct"] == pytest.approx(100.0, rel=1e-6)
    assert m["retention_100_pct"] == pytest.approx(100.0, rel=1e-6)
    assert m["ce_median_pct"] == pytest.approx(95.0, rel=1e-6)


def test_compare_steps_recorded_equals_derived():
    """When recorded labels match the natural chg+dchg grouping, metrics agree."""
    steps = _steps(120)
    res = mv.compare_steps(steps, 1e-6, 1e-3)
    assert res is not None
    for k in mv.METRIC_KEYS:
        rel = res[f"{k}_rel_diff"]
        assert rel is not None and rel < 1e-9
    assert res["reason"] == "agree"
    assert res["n_label_cycles"] == 120


def test_compare_steps_unlabelled_returns_none():
    steps = _steps(2)  # only 2 distinct labels -> below MIN_LABEL_DISTINCT
    assert mv.compare_steps(steps, 1e-6, 1e-3) is None


def test_compare_steps_mismatched_labels_flagged():
    """Merging two real cycles under one recorded label creates a disagreement."""
    # recorded label groups pairs of cycles together -> half as many recorded cycles
    steps = _steps(40, label_fn=lambda k: (k + 1) // 2)
    res = mv.compare_steps(steps, 1e-6, 1e-3)
    assert res is not None
    assert res["n_label_cycles"] != res["n_derived_cycles"]
    assert res["worst_rel_diff"] is not None and res["worst_rel_diff"] > 0.02
    assert res["reason"] in ("cycle_count_differs", "full_cycle_count_differs",
                             "pulse_protocol_cycle_count_differs")


def test_rel_and_abs_diff_helpers():
    assert mv._abs_diff(2.0, 3.0) == pytest.approx(1.0)
    assert mv._rel_diff(2.0, 3.0) == pytest.approx(0.5)
    assert mv._rel_diff(0.0, 0.0) == 0.0
    assert mv._rel_diff(None, 3.0) is None


# --------------------------------------------------------------------------- #
# End-to-end orchestration with fake project
# --------------------------------------------------------------------------- #
def _write_sidecar(project, dataset_id, frame, sha256):
    out = derived_steps.sidecar_path(project, dataset_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    arrow = pa.Table.from_pandas(frame, preserve_index=False)
    arrow = arrow.replace_schema_metadata(
        {"dataset_id": dataset_id, "source_sha256": sha256,
         "method": derived_steps.STEP_METHOD})
    pq.write_table(arrow, out)


def test_validate_metrics_end_to_end(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    meta = {"voltage_unit": "mV", "capacity_unit": "uAh"}

    datasets = {
        "good": (_steps(30), "sha_good"),               # labelled, agrees
        "mismatch": (_steps(30, label_fn=lambda k: (k + 1) // 2), "sha_mm"),
        "unlabelled": (_steps(2), "sha_un"),            # too few labels
    }
    rows = []
    for ds, (frame, sha) in datasets.items():
        _write_sidecar(project, ds, frame, sha)
        rows.append({"dataset_id": ds, "format": "native_cyc", "sha256": sha,
                     "path": str(project / f"{ds}.parquet"),
                     "metadata": meta, "metadata_current": True})

    monkeypatch.setattr("battery_insight.library.library_rows", lambda p, **k: rows)

    summary = mv.validate_metrics(project, workers=2)
    assert summary["n_candidates"] == 3
    assert summary["n_datasets_compared"] == 2          # unlabelled skipped
    assert summary["skipped"].get("unlabelled") == 1

    csv = pd.read_csv(project / "db" / "metric_validation.csv")
    assert set(csv["dataset_id"]) == {"good", "mismatch"}
    good = csv[csv["dataset_id"] == "good"].iloc[0]
    assert good["reason"] == "agree"
    assert float(good["n_full_cycles_rel_diff"]) < 1e-9
    # uAh -> Ah (guards the (voltage, capacity) factor order)
    assert float(good["cap_initial_ah_recorded"]) == pytest.approx(0.095)

    assert (project / "db" / "metric_validation.json").exists()
    # the mismatch dataset is the worst offender
    assert summary["worst_datasets"][0]["dataset_id"] == "mismatch"


def test_validate_metrics_units_unconfirmed_skipped(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    project.mkdir()
    _write_sidecar(project, "x", _steps(10), "sha_x")
    rows = [{"dataset_id": "x", "format": "native_cyc", "sha256": "sha_x",
             "path": str(project / "x.parquet"),
             "metadata": {}, "metadata_current": False}]
    monkeypatch.setattr("battery_insight.library.library_rows", lambda p, **k: rows)
    summary = mv.validate_metrics(project, workers=1)
    assert summary["n_datasets_compared"] == 0
    assert summary["skipped"].get("units_unconfirmed") == 1
