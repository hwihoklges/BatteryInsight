"""db_build skips unchanged stages and reruns the ones whose inputs change."""
import json

from battery_insight import cell_qc, cycle_profiles, db_build, derived_cycles, derived_steps, design_network, metric_validation, research_db, run_metrics


def test_db_build_incremental(tmp_path, monkeypatch):
    calls = []
    (tmp_path / "dx").mkdir()
    (tmp_path / "dx" / "dx_files.csv").write_text("a\n1\n", encoding="utf-8")
    (tmp_path / "dx" / "dx_tests.csv").write_text("a\n1\n", encoding="utf-8")

    def touch(*paths):
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")

    db = tmp_path / "db"
    monkeypatch.setattr(derived_steps, "build_step_tables", lambda p, **k: calls.append("steps") or touch(tmp_path / "derived/steps/a.parquet"))
    monkeypatch.setattr(derived_cycles, "build_cycle_tables", lambda p, **k: calls.append("cycles") or touch(derived_cycles.cycles_summary_path(tmp_path)))
    monkeypatch.setattr(cycle_profiles, "build_cycle_profiles", lambda p, **k: calls.append("profiles") or touch(db / "cycle_profiles.parquet") or {"n_profiles": 1})
    monkeypatch.setattr(run_metrics, "build_run_metrics", lambda p, **k: calls.append("metrics") or touch(
        db / "run_profile.parquet", db / "run_metrics.parquet", db / "run_cycles.parquet"))
    monkeypatch.setattr(cell_qc, "build_cell_qc", lambda p: calls.append("qc") or touch(db / "run_qc.parquet") or {"runs": 1})
    monkeypatch.setattr(metric_validation, "validate_metrics", lambda p, **k: calls.append("validation") or touch(db / "metric_validation.json") or {"n": 1})
    monkeypatch.setattr(research_db, "build_research_db", lambda p: calls.append("research") or touch(db / "research.sqlite3") or {"designs": 1})
    monkeypatch.setattr(design_network, "build_network", lambda p: calls.append("network") or touch(db / "network.html") or db / "network.html")

    db_build.build_database(tmp_path, progress=None)
    assert calls == ["steps", "cycles", "profiles", "metrics", "qc", "validation", "research", "network"]
    calls.clear()
    result = db_build.build_database(tmp_path, progress=None)
    assert calls == [] and all(s["result"] == "unchanged" for s in result["stages"].values())

    (tmp_path / "dx" / "dx_tests.csv").write_text("a\n2\n22\n", encoding="utf-8")
    db_build.build_database(tmp_path, progress=None)
    assert calls == ["research", "network"]
    calls.clear()
    (tmp_path / "criteria.json").write_text("{}", encoding="utf-8")  # user edits a threshold
    db_build.build_database(tmp_path, progress=None)
    assert calls == ["qc", "research", "network"]
    calls.clear()
    db_build.build_database(tmp_path, force=True, progress=None)
    assert calls == ["steps", "cycles", "profiles", "metrics", "qc", "validation", "research", "network"]
    calls.clear()
    db_build.build_database(tmp_path, profile_every=0, progress=None)
    assert calls == []
    assert json.loads((db / db_build.STATE_FILE).read_text(encoding="utf-8")).keys() >= {"steps", "network"}
