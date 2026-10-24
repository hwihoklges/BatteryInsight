"""Unit tests for battery_insight.run_metrics on small synthetic frames."""
import numpy as np
import pandas as pd
import pytest

from battery_insight import run_metrics as rm


def _summary_row(dataset_id, cyc, chg, dchg, *, cdur=36000.0, ddur=36000.0,
                 mv=3.5, vmin=2.5, vmax=4.2, has_pulses=False):
    ce = (dchg / chg) if chg else np.nan
    return {
        "dataset_id": dataset_id, "cycle_derived": cyc, "start_step": cyc,
        "n_steps": 2, "start_elapsed_s": 0.0,
        "charge_capacity_ah": chg, "discharge_capacity_ah": dchg,
        "coulombic_efficiency": ce, "charge_duration_s": cdur,
        "discharge_duration_s": ddur, "mean_discharge_voltage_v": mv,
        "max_voltage_v": vmax, "min_voltage_v": vmin, "has_pulses": has_pulses,
    }


def _write_sidecar(project, dataset_id, roles_by_cycle):
    """roles_by_cycle: {cycle_derived: 'full'|'pulse'|'rest'}."""
    rows = []
    step = 1
    for cyc, kind in roles_by_cycle.items():
        if kind == "full":
            steps = ["full_chg", "full_dchg"]
        elif kind == "pulse":
            steps = ["pulse_chg", "pulse_dchg"]
        else:
            steps = ["rest"]
        for role in steps:
            rows.append({"step_derived": step, "start_row": 0, "n_points": 1,
                         "cycle_derived": cyc, "discharge_index": np.nan, "step_role": role})
            step += 1
    side = pd.DataFrame(rows)
    (project / "derived" / "cycles").mkdir(parents=True, exist_ok=True)
    side.to_parquet(project / "derived" / "cycles" / f"{dataset_id}.parquet", index=False)


def _build_project(tmp_path, summary_rows, dx_rows, sidecars):
    project = tmp_path
    (project / "derived").mkdir(parents=True, exist_ok=True)
    (project / "dx").mkdir(parents=True, exist_ok=True)
    pd.DataFrame(summary_rows).to_parquet(
        project / "derived" / "cycles_summary.parquet", index=False)
    pd.DataFrame(dx_rows).to_csv(project / "dx" / "dx_files.csv", index=False)
    for ds, roles in sidecars.items():
        _write_sidecar(project, ds, roles)
    return project


def test_fragment_stitching_continuous_index(tmp_path):
    # two disjoint, non-_org fragments of one run -> stitched continuously
    rows, roles_a, roles_b = [], {}, {}
    for i in range(1, 6):
        rows.append(_summary_row("A", i, 1.0, 0.9)); roles_a[i] = "full"
    for i in range(1, 5):
        rows.append(_summary_row("B", i, 1.0, 0.85)); roles_b[i] = "full"
    dx = [
        {"dataset_id": "A", "test_key": "T1", "channel": "Ch1", "fragment": "",
         "exclude": "", "physical_cell_id": ""},
        {"dataset_id": "B", "test_key": "T1", "channel": "Ch1", "fragment": "_2",
         "exclude": "", "physical_cell_id": ""},
    ]
    project = _build_project(tmp_path, rows, dx, {"A": roles_a, "B": roles_b})
    rm.build_run_metrics(project, workers=2)

    cycles = pd.read_parquet(project / "db" / "run_cycles.parquet")
    run = cycles[cycles.run_id == "T1:Ch1"].sort_values("cycle_index")
    assert list(run.cycle_index) == list(range(1, 10))  # 5 + 4 continuous
    assert list(run.dataset_id) == ["A"] * 5 + ["B"] * 4

    prof = rm.load_run_profile(project)
    assert prof.loc[prof.run_id == "T1:Ch1", "n_full_cycles"].iloc[0] == 9


def test_combined_supersedes_org_fragments(tmp_path):
    # _org originals are superseded by the combined (no-suffix) fragment
    rows, roles_main, roles_org = [], {}, {}
    for i in range(1, 6):
        rows.append(_summary_row("MAIN", i, 1.0, 0.9)); roles_main[i] = "full"
    for i in range(1, 4):
        rows.append(_summary_row("ORG", i, 1.0, 0.9)); roles_org[i] = "full"
    dx = [
        {"dataset_id": "MAIN", "test_key": "T1", "channel": "Ch1", "fragment": "",
         "exclude": "", "physical_cell_id": ""},
        {"dataset_id": "ORG", "test_key": "T1", "channel": "Ch1", "fragment": "_org",
         "exclude": "", "physical_cell_id": ""},
    ]
    project = _build_project(tmp_path, rows, dx, {"MAIN": roles_main, "ORG": roles_org})
    rm.build_run_metrics(project, workers=2)
    prof = rm.load_run_profile(project)
    row = prof[prof.run_id == "T1:Ch1"].iloc[0]
    assert row.n_cycles == 5  # only MAIN used
    assert "superseded_originals:1" in row.reasons


def test_formation_cycle_skipped(tmp_path):
    rows, roles = [], {}
    caps = [0.5, 1.0, 1.0, 1.0, 0.99, 0.98]  # cycle 1 is formation (< 0.8 * median next 3)
    for i, d in enumerate(caps, start=1):
        rows.append(_summary_row("A", i, d / 0.95, d)); roles[i] = "full"
    dx = [{"dataset_id": "A", "test_key": "T", "channel": "C", "fragment": "",
           "exclude": "", "physical_cell_id": ""}]
    project = _build_project(tmp_path, rows, dx, {"A": roles})
    rm.build_run_metrics(project, workers=2)
    prof = rm.load_run_profile(project)
    row = prof[prof.run_id == "T:C"].iloc[0]
    assert "formation_skipped" in row.reasons
    assert row.nominal_capacity_ah == pytest.approx(1.0)  # median of cycles 2..4


def test_ce_gate_only_full_cycles(tmp_path):
    # pulse cycles excluded; CE median over full cycles after first 3
    rows, roles = [], {}
    pattern = ["full", "full", "full", "full", "pulse", "full"]
    for i, kind in enumerate(pattern, start=1):
        if kind == "pulse":
            rows.append(_summary_row("A", i, 0.3, 0.1, has_pulses=True))
        else:
            rows.append(_summary_row("A", i, 1.0, 0.95))
        roles[i] = kind
    dx = [{"dataset_id": "A", "test_key": "T", "channel": "C", "fragment": "",
           "exclude": "", "physical_cell_id": ""}]
    project = _build_project(tmp_path, rows, dx, {"A": roles})
    rm.build_run_metrics(project, workers=2)
    met = rm.load_run_metrics(project)
    ce = met[(met.run_id == "T:C") & (met.metric_id == "ce_median_pct")]
    assert len(ce) == 1
    assert ce.value.iloc[0] == pytest.approx(95.0, abs=0.5)  # pulse (very low CE) excluded
    # pulse cycle is not counted as full
    prof = rm.load_run_profile(project)
    assert prof.loc[prof.run_id == "T:C", "n_full_cycles"].iloc[0] == 5


def test_cycles_to_80pct_censored_vs_reached(tmp_path):
    # flat run never drops below 80% -> censored
    rows, roles = [], {}
    for i in range(1, 7):
        rows.append(_summary_row("A", i, 1.0, 0.95)); roles[i] = "full"
    dx = [{"dataset_id": "A", "test_key": "T", "channel": "C", "fragment": "",
           "exclude": "", "physical_cell_id": ""}]
    project = _build_project(tmp_path, rows, dx, {"A": roles})
    rm.build_run_metrics(project, workers=2)
    met = rm.load_run_metrics(project)
    c80 = met[(met.run_id == "T:C") & (met.metric_id == "cycles_to_80pct")].iloc[0]
    assert bool(c80.censored) is True
    assert c80.value == 6

    # fading run drops below 80% -> not censored
    rows2, roles2 = [], {}
    caps = [1.0, 0.95, 0.9, 0.82, 0.7, 0.6]
    for i, d in enumerate(caps, start=1):
        rows2.append(_summary_row("B", i, d / 0.95, d)); roles2[i] = "full"
    dx2 = [{"dataset_id": "B", "test_key": "T2", "channel": "C", "fragment": "",
            "exclude": "", "physical_cell_id": ""}]
    project2 = _build_project(tmp_path / "p2", rows2, dx2, {"B": roles2})
    rm.build_run_metrics(project2, workers=2)
    met2 = rm.load_run_metrics(project2)
    c80b = met2[(met2.run_id == "T2:C") & (met2.metric_id == "cycles_to_80pct")].iloc[0]
    assert bool(c80b.censored) is False
    assert c80b.value == 5  # first cycle under 0.8 nominal (nominal ~ 0.95)


def test_c_rate_ladder_half_c(tmp_path):
    # capacity 1.0 Ah over 2 h charge & discharge -> 0.5 A -> 0.5C vs 1.0 nominal
    rows, roles = [], {}
    for i in range(1, 5):
        rows.append(_summary_row("A", i, 1.0, 1.0, cdur=7200.0, ddur=7200.0))
        roles[i] = "full"
    dx = [{"dataset_id": "A", "test_key": "T", "channel": "C", "fragment": "",
           "exclude": "", "physical_cell_id": ""}]
    project = _build_project(tmp_path, rows, dx, {"A": roles})
    rm.build_run_metrics(project, workers=2)
    prof = rm.load_run_profile(project)
    row = prof[prof.run_id == "T:C"].iloc[0]
    assert row.c_rate_class == "0.5C/0.5C"
    assert row.chg_c_rate == pytest.approx(0.5, abs=0.01)


def test_dataset_not_in_dx_gets_ds_run_id(tmp_path):
    rows, roles = [], {}
    for i in range(1, 4):
        rows.append(_summary_row("LONE", i, 1.0, 0.9)); roles[i] = "full"
    project = _build_project(tmp_path, rows, [], {"LONE": roles})
    # overwrite empty dx with a header-only frame handled by _run_assignments
    rm.build_run_metrics(project, workers=2)
    prof = rm.load_run_profile(project)
    assert "ds:LONE" in set(prof.run_id)
