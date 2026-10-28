"""Tests for battery_insight.research_db (Module B)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from battery_insight import research_db as rdb


def _write_dx(project, files_rows, tests_rows):
    dx = Path(project) / "dx"
    dx.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(files_rows).to_csv(dx / "dx_files.csv", index=False)
    pd.DataFrame(tests_rows).to_csv(dx / "dx_tests.csv", index=False)


def _base_project(tmp_path, *, with_metrics=False):
    """Synthetic project: 2 designs, one confirmed cell with two runs, cohort grouping."""
    files = [
        # test A: confirmed physical cell, two runs (channels) -> collapse to 1 cell
        dict(dataset_id="dA1", source_sha256="sa1", test_key="A", owner_name="o",
             test_date="2026-01-01", set_no="1", test_type="", test_name="A_cyc",
             channel="ch1", fragment="", physical_cell_id="CELL1", exclude=""),
        dict(dataset_id="dA2", source_sha256="sa2", test_key="A", owner_name="o",
             test_date="2026-01-01", set_no="1", test_type="", test_name="A_cyc",
             channel="ch2", fragment="", physical_cell_id="CELL1", exclude=""),
        # test B: provisional (no physical cell)
        dict(dataset_id="dB1", source_sha256="sb1", test_key="B", owner_name="o",
             test_date="2026-01-02", set_no="1", test_type="", test_name="B_cyc",
             channel="ch3", fragment="", physical_cell_id="", exclude=""),
    ]
    tests = [
        dict(test_key="A", test_type="Cycle", test_type_inferred="", test_type_inferred_conf="",
             temperature_c="25.0", tags="process=dry; material=NCM", series_key="sA",
             sample_name="A sample", test_name="A_cyc", confirmed="False"),
        dict(test_key="B", test_type="Cycle", test_type_inferred="", test_type_inferred_conf="",
             temperature_c="25.0", tags="process=wet; material=NCM", series_key="sB",
             sample_name="B sample", test_name="B_cyc", confirmed="False"),
    ]
    _write_dx(tmp_path, files, tests)

    if with_metrics:
        dbdir = Path(tmp_path) / "db"
        dbdir.mkdir(parents=True, exist_ok=True)
        # two runs on the confirmed cell -> values 10 and 20 ; median 15 counts once
        pd.DataFrame([
            dict(run_id="A:ch1", metric_id="cap_initial_ah", window="", value=10.0,
                 unit="Ah", censored=False, quality="ok", method="run_metrics_v1"),
            dict(run_id="A:ch2", metric_id="cap_initial_ah", window="", value=20.0,
                 unit="Ah", censored=False, quality="ok", method="run_metrics_v1"),
            dict(run_id="B:ch3", metric_id="cap_initial_ah", window="", value=30.0,
                 unit="Ah", censored=False, quality="ok", method="run_metrics_v1"),
        ]).to_parquet(dbdir / "run_metrics.parquet")
        pd.DataFrame([
            dict(run_id="A:ch1", c_rate_class="0.1C/0.1C", v_lo=3.0, v_hi=4.2, quality="ok"),
            dict(run_id="A:ch2", c_rate_class="0.1C/0.1C", v_lo=3.0, v_hi=4.2, quality="ok"),
            dict(run_id="B:ch3", c_rate_class="0.1C/0.1C", v_lo=3.0, v_hi=4.2, quality="ok"),
        ]).to_parquet(dbdir / "run_profile.parquet")
    return tmp_path


def test_provisional_vs_confirmed_and_collapse(tmp_path):
    rdb.build_research_db(_base_project(tmp_path))
    conn = rdb.connect_ro(tmp_path)
    try:
        cells = {r["cell_id"]: r["status"] for r in conn.execute("SELECT cell_id,status FROM cells")}
    finally:
        conn.close()
    # confirmed physical cell collapses the two channels/runs into one cell
    assert "pc:CELL1" in cells and cells["pc:CELL1"] == "confirmed"
    assert any(k.startswith("run:") and v == "provisional" for k, v in cells.items())


def test_cohort_grouping(tmp_path):
    rdb.build_research_db(_base_project(tmp_path))
    df = rdb.cohort_table(tmp_path, min_designs=2)
    # both designs share one cohort (Cycle|25|C?|V? with no module A)
    assert len(df) == 1
    assert df.iloc[0]["n_designs"] == 2


def test_equal_weight_per_cell(tmp_path):
    rdb.build_research_db(_base_project(tmp_path, with_metrics=True))
    conn = rdb.connect_ro(tmp_path)
    try:
        cm = conn.execute(
            "SELECT value,n_runs FROM cell_metrics WHERE cell_id='pc:CELL1' "
            "AND metric_id='cap_initial_ah'").fetchone()
        dm = conn.execute(
            "SELECT d.n_cells AS n_cells,d.mean AS mean FROM design_metrics d JOIN designs g USING(design_id) "
            "WHERE metric_id='cap_initial_ah' AND g.design_label LIKE '%dry%'").fetchone()
    finally:
        conn.close()
    # two runs (10,20) median -> 15, counted once (n_runs=2 but one cell)
    assert cm["n_runs"] == 2
    assert cm["value"] == pytest.approx(15.0)
    # design A has a single confirmed cell -> n_cells 1, mean 15
    assert dm["n_cells"] == 1
    assert dm["mean"] == pytest.approx(15.0)


def test_missing_metrics_tolerated(tmp_path):
    summary = rdb.build_research_db(_base_project(tmp_path, with_metrics=False))
    assert summary["has_module_a"] is False
    assert summary["observations"] == 0
    assert summary["cell_metrics"] == 0
    # identity tables still built
    assert summary["runs"] == 3 and summary["designs"] == 2


def test_design_map_override_and_template_not_overwritten(tmp_path):
    project = _base_project(tmp_path)
    # first build creates the template
    rdb.build_research_db(project)
    map_path = Path(project) / "db" / "design_map.csv"
    assert map_path.exists()
    original = map_path.read_text(encoding="utf-8")
    assert original.startswith("#")

    # user supplies an override for test A via test_key
    user_map = original + "test_key,A,DESIGN_USER_A,My Mapped A,manual\n"
    map_path.write_text(user_map, encoding="utf-8")

    rdb.build_research_db(project)
    # template/user file untouched
    assert map_path.read_text(encoding="utf-8") == user_map

    conn = rdb.connect_ro(project)
    try:
        trow = conn.execute("SELECT design_id FROM tests WHERE test_key='A'").fetchone()
        drow = conn.execute(
            "SELECT design_label,status FROM designs WHERE design_id='DESIGN_USER_A'").fetchone()
    finally:
        conn.close()
    assert trow["design_id"] == "DESIGN_USER_A"
    assert drow is not None
    assert drow["design_label"] == "My Mapped A"
    assert drow["status"] == "user"


def test_candidate_series_grouping_without_tags(tmp_path):
    files = [
        dict(dataset_id="d1", source_sha256="s1", test_key="T1", owner_name="o",
             test_date="2026-01-01", set_no="1", test_type="", test_name="x",
             channel="c1", fragment="", physical_cell_id="", exclude=""),
    ]
    tests = [
        dict(test_key="T1", test_type="", test_type_inferred="Cycle",
             test_type_inferred_conf="0.9", temperature_c="", tags="",
             series_key="S1", sample_name="sample one", test_name="x", confirmed="False"),
    ]
    _write_dx(tmp_path, files, tests)
    rdb.build_research_db(tmp_path)
    conn = rdb.connect_ro(tmp_path)
    try:
        d = conn.execute("SELECT design_id,status FROM designs").fetchone()
        t = conn.execute("SELECT test_type,test_type_source FROM tests").fetchone()
    finally:
        conn.close()
    assert d["design_id"].startswith("cand:series:")
    assert d["status"] == "candidate"
    assert t["test_type"] == "Cycle" and t["test_type_source"] == "inferred"


def test_candidate_mapping_merges_designs_and_lists_candidates(tmp_path):
    project = _base_project(tmp_path, with_metrics=True)
    rdb.build_research_db(project)
    candidates = pd.read_csv(Path(project) / "db" / "design_candidates.csv", dtype=str, keep_default_na=False)
    assert set(candidates.match_type) == {"candidate"} and len(candidates) == 2
    map_path = Path(project) / "db" / "design_map.csv"
    lines = "".join(f"candidate,{c},MERGED,NCM family,\n" for c in candidates.match_value)
    map_path.write_text(map_path.read_text(encoding="utf-8") + lines, encoding="utf-8")
    summary = rdb.build_research_db(project)
    assert summary["designs"] == 1 and summary["designs_user"] == 1
    again = pd.read_csv(Path(project) / "db" / "design_candidates.csv", dtype=str, keep_default_na=False)
    assert set(again.design_id) == {"MERGED"}


def test_anomaly_runs_excluded_from_aggregates(tmp_path):
    project = _base_project(tmp_path, with_metrics=True)
    tests = pd.read_csv(Path(project) / "dx" / "dx_tests.csv", dtype=str, keep_default_na=False)
    tests["anomaly"] = ["", "channel_fault"]
    tests.to_csv(Path(project) / "dx" / "dx_tests.csv", index=False)
    summary = rdb.build_research_db(project)
    assert summary["anomaly_runs"] == 1
    conn = rdb.connect_ro(project)
    try:
        cells = {r["cell_id"] for r in conn.execute("SELECT cell_id FROM cell_metrics")}
    finally:
        conn.close()
    assert cells == {"pc:CELL1"}
    gaps = pd.read_csv(Path(project) / "db" / "identity_gaps.csv", dtype=str)
    assert gaps.gaps.str.contains("anomaly:channel_fault").sum() == 1


def test_defect_runs_excluded_from_aggregates(tmp_path):
    project = _base_project(tmp_path, with_metrics=True)
    pd.DataFrame([dict(run_id="A:ch2", qc_status="defect", qc_reasons="low_ice: ICE 40%"),
                  dict(run_id="B:ch3", qc_status="suspect", qc_reasons="low_capacity")]
                 ).to_parquet(Path(project) / "db" / "run_qc.parquet")
    summary = rdb.build_research_db(project)
    assert summary["qc_defect_runs"] == 1 and summary["qc_suspect_runs"] == 1
    assert summary["qc_not_assessed_runs"] == 1
    conn = rdb.connect_ro(project)
    try:
        cm = conn.execute("SELECT value,n_runs FROM cell_metrics WHERE cell_id='pc:CELL1'").fetchone()
        runs = {r["run_id"]: r["qc_status"] for r in conn.execute("SELECT run_id,qc_status FROM runs")}
        n_qc = conn.execute("SELECT COUNT(*) FROM run_qc").fetchone()[0]
    finally:
        conn.close()
    assert cm["n_runs"] == 1 and cm["value"] == pytest.approx(10.0)   # defect A:ch2 (20) dropped
    assert runs["A:ch2"] == "defect" and runs["B:ch3"] == "suspect" and n_qc == 2
    assert runs["A:ch1"] == "not_assessed"


def _life_project(tmp_path, values, censored):
    project = _base_project(tmp_path, with_metrics=True)
    tests_path = project / "dx" / "dx_tests.csv"
    tests = pd.read_csv(tests_path, dtype=str, keep_default_na=False)
    tests.loc[1, "tags"] = tests.loc[0, "tags"]
    tests.to_csv(tests_path, index=False)
    metrics_path = project / "db" / "run_metrics.parquet"
    metrics = pd.read_parquet(metrics_path)
    metrics["metric_id"] = "cycles_to_80pct"
    metrics["value"] = values
    metrics["unit"] = "cycles"
    metrics["censored"] = censored
    metrics.to_parquet(metrics_path)
    return project


@pytest.mark.parametrize("flags,observed_cells", [
    ([True, True, False], 1),
    ([True, True, True], 0),
    ([False, True, False], 1),
])
def test_censored_life_keeps_followup_without_failure_statistics(tmp_path, flags, observed_cells):
    project = _life_project(tmp_path, [100, 200, 50], flags)
    rdb.build_research_db(project)
    conn = rdb.connect_ro(project)
    try:
        observations = [dict(r) for r in conn.execute("SELECT * FROM observations ORDER BY run_id")]
        cell = conn.execute("SELECT * FROM cell_metrics WHERE cell_id='pc:CELL1'").fetchone()
        design = conn.execute("SELECT * FROM design_metrics").fetchone()
        cohort = conn.execute("SELECT cohort_key FROM cohorts").fetchone()[0]
    finally:
        conn.close()
    assert [r["value"] for r in observations] == [100, 200, 50]
    assert [r["censored"] for r in observations] == [int(flag) for flag in flags]
    assert cell["value"] is None and cell["censored"] == 1
    assert cell["n_runs"] == 2 and cell["n_censored_runs"] == sum(flags[:2])
    assert cell["censor_lower_bound"] == 200
    assert design["n_cells"] == 2
    assert design["n_observed_cells"] == observed_cells
    assert design["n_censored_cells"] == 2 - observed_cells
    assert all(design[k] is None for k in ("mean", "sd", "median", "min", "max"))
    assert design["censor_lower_bound_min"] == (50 if flags[2] else 200)
    assert design["censor_lower_bound_max"] == 200
    table = rdb.design_table(project, cohort)
    assert table.iloc[0]["n_censored_cells"] == 2 - observed_cells


def test_uncensored_life_retains_equal_cell_weight(tmp_path):
    project = _life_project(tmp_path, [100, 200, 50], [False, False, False])
    rdb.build_research_db(project)
    conn = rdb.connect_ro(project)
    try:
        row = conn.execute("SELECT * FROM design_metrics").fetchone()
    finally:
        conn.close()
    assert row["n_cells"] == row["n_observed_cells"] == 2
    assert row["n_censored_cells"] == 0
    assert row["mean"] == pytest.approx(100)
    assert row["censor_lower_bound_min"] is None
    assert row["censor_lower_bound_max"] is None


def test_text_censor_flags_do_not_use_python_truthiness(tmp_path):
    project = _life_project(tmp_path, [100, 200, 50], ["False", "0", "True"])
    rdb.build_research_db(project)
    conn = rdb.connect_ro(project)
    try:
        flags = [r[0] for r in conn.execute("SELECT censored FROM observations ORDER BY run_id")]
        cell = conn.execute("SELECT value,censored FROM cell_metrics WHERE cell_id='pc:CELL1'").fetchone()
    finally:
        conn.close()
    assert flags == [0, 0, 1]
    assert tuple(cell) == (150, 0)


@pytest.mark.parametrize("flag", [None, float("nan"), "", "unknown"])
def test_missing_censor_flag_remains_unknown(flag):
    assert rdb._censor_flag(flag) is None


def test_observation_quality_excluded_without_erasing_raw(tmp_path):
    project = _base_project(tmp_path, with_metrics=True)
    metrics_path = project / "db" / "run_metrics.parquet"
    metrics = pd.read_parquet(metrics_path)
    metrics.loc[1, "quality"] = "insufficient"
    metrics.loc[2, "quality"] = "partial"
    metrics.to_parquet(metrics_path)
    rdb.build_research_db(project)
    conn = rdb.connect_ro(project)
    try:
        raw = conn.execute("SELECT value,quality FROM observations WHERE run_id='A:ch2'").fetchone()
        cell = conn.execute("SELECT value,n_runs FROM cell_metrics WHERE cell_id='pc:CELL1'").fetchone()
        n_cells = conn.execute("SELECT count(*) FROM cell_metrics").fetchone()[0]
    finally:
        conn.close()
    assert tuple(raw) == (20, "insufficient")
    assert tuple(cell) == (10, 1)
    assert n_cells == 2


def test_physical_cell_conflicting_designs_are_not_assigned_to_first_design(tmp_path):
    project = _base_project(tmp_path, with_metrics=True)
    files_path = project / "dx" / "dx_files.csv"
    files = pd.read_csv(files_path, dtype=str, keep_default_na=False)
    files.loc[2, "physical_cell_id"] = "CELL1"
    files.to_csv(files_path, index=False)
    summary = rdb.build_research_db(project)
    assert summary["cells_design_conflict"] == 1
    conn = rdb.connect_ro(project)
    try:
        cell = conn.execute("SELECT design_conflict,status FROM cells").fetchone()
        assert tuple(cell) == (1, "confirmed")
        assert conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 3
        assert conn.execute("SELECT count(*) FROM cell_metrics").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM design_metrics").fetchone()[0] == 0
        assert [r[0] for r in conn.execute("SELECT n_cells FROM designs")] == [1, 1]
        assert conn.execute("SELECT count(DISTINCT design_id) FROM runs").fetchone()[0] == 2
    finally:
        conn.close()
    gaps = pd.read_csv(project / "db" / "identity_gaps.csv")
    assert gaps.gaps.str.contains("physical_cell_design_conflict").all()


def test_repeats_do_not_pool_different_cohorts(tmp_path):
    project = _base_project(tmp_path, with_metrics=True)
    profile_path = project / "db" / "run_profile.parquet"
    profile = pd.read_parquet(profile_path)
    profile.loc[1, "c_rate_class"] = "1C/1C"
    profile.to_parquet(profile_path)
    rdb.build_research_db(project)
    conn = rdb.connect_ro(project)
    try:
        cells = conn.execute(
            "SELECT value,n_runs,censored FROM cell_metrics WHERE cell_id='pc:CELL1' ORDER BY value"
        ).fetchall()
    finally:
        conn.close()
    assert [tuple(r) for r in cells] == [(10, 1, 0), (20, 1, 0)]


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_nonfinite_values_do_not_enter_statistics(tmp_path, value):
    project = _base_project(tmp_path, with_metrics=True)
    metrics_path = project / "db" / "run_metrics.parquet"
    metrics = pd.read_parquet(metrics_path)
    metrics.loc[1, "value"] = value
    metrics.to_parquet(metrics_path)
    rdb.build_research_db(project)
    conn = rdb.connect_ro(project)
    try:
        cell = conn.execute("SELECT value,n_runs FROM cell_metrics WHERE cell_id='pc:CELL1'").fetchone()
        assert conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 3
    finally:
        conn.close()
    assert tuple(cell) == (10, 1)


def test_unknown_censoring_does_not_reenable_life_comparison(tmp_path):
    from battery_insight.design_network import _collect

    project = _life_project(tmp_path, [100, 200, 300], [False, None, False])
    rdb.build_research_db(project)
    conn = rdb.connect_ro(project)
    try:
        raw = conn.execute("SELECT censored FROM observations WHERE run_id='A:ch2'").fetchone()
        cell = conn.execute("SELECT * FROM cell_metrics WHERE cell_id='pc:CELL1'").fetchone()
        design = conn.execute("SELECT * FROM design_metrics").fetchone()
        payload = _collect(conn)
    finally:
        conn.close()
    assert raw["censored"] is None
    assert cell["value"] is None and cell["censored"] is None
    assert cell["n_unknown_censor_runs"] == 1 and cell["n_runs"] == 2
    assert design["n_cells"] == 2 and design["n_observed_cells"] == 1
    assert design["n_censored_cells"] == 0 and design["n_unknown_censor_cells"] == 1
    assert all(design[k] is None for k in ("mean", "sd", "median", "min", "max"))
    metric = payload["design_metrics"][design["cohort_key"]][design["design_id"]]["cycles_to_80pct"]
    assert metric["comparison_exclusions"] == ["censoring_metadata_unknown"]
    assert not payload["percentiles"][design["cohort_key"]][design["design_id"]]


@pytest.mark.parametrize("temperature", ["25", ""])
def test_research_network_integration_retains_original_conditions(tmp_path, temperature):
    from battery_insight.design_network import _collect

    project = _base_project(tmp_path, with_metrics=True)
    tests_path = project / "dx" / "dx_tests.csv"
    tests = pd.read_csv(tests_path, dtype=str, keep_default_na=False)
    tests["temperature_c"] = temperature
    tests.to_csv(tests_path, index=False)
    rdb.build_research_db(project)
    conn = rdb.connect_ro(project)
    try:
        before = [tuple(r) for r in conn.execute("SELECT test_key,temperature_c,tags FROM tests")]
        payload = _collect(conn)
        assert [tuple(r) for r in conn.execute("SELECT test_key,temperature_c,tags FROM tests")] == before
    finally:
        conn.close()
    cohort = payload["cohorts"][0]
    assert cohort["comparison_eligible"] is bool(temperature)
    assert cohort["temperature_c"] == (temperature or None)
    if not temperature:
        assert cohort["comparison_exclusions"] == ["missing_condition:temperature_c"]
        assert all(not values for values in payload["percentiles"][cohort["cohort_key"]].values())