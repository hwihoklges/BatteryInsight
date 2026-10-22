import importlib.util
import json
from pathlib import Path
import pandas as pd
import pytest
from battery_insight.pipeline import run_pipeline
from battery_insight.ingest import sha256_file


def demo_config(tmp_path):
    script = Path(__file__).parents[1] / "examples" / "make_demo.py"
    spec = importlib.util.spec_from_file_location("demo", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.make_demo(tmp_path / "input")


def test_end_to_end_chunk_boundaries_readonly_and_reuse(tmp_path):
    config = demo_config(tmp_path)
    source = config.parent / "SYNTHETIC_demo.csv"
    before = sha256_file(source)
    first = run_pipeline(config, tmp_path / "lake")
    cycles = pd.read_parquet(first / "gold" / "cycles.parquet")
    assert len(cycles) == 8
    assert cycles.charge_capacity_ah.tolist() == pytest.approx([1.] * 8)
    assert cycles.discharge_capacity_ah.tolist() == pytest.approx([1, 1, 1, .96, .92, .88, .84, .80])
    assert cycles.capacity_retention_pct.tolist() == pytest.approx([100, 100, 100, 96, 92, 88, 84, 80])
    assert sha256_file(source) == before
    assert run_pipeline(config, tmp_path / "lake") == first
    assert (first / "report.html").is_file()
    meta = json.loads((first / "run_manifest.json").read_text())
    assert meta["aim_status"] == "not_configured"
    assert meta["input_readonly"]
    assert list((first / "silver").rglob("*.parquet"))


def test_chunk_size_invariance(tmp_path):
    config = demo_config(tmp_path)
    first = run_pipeline(config, tmp_path / "lake")
    changed = json.loads(config.read_text())
    changed["chunk_rows"] = 10000
    config.write_text(json.dumps(changed))
    second = run_pipeline(config, tmp_path / "lake")
    assert first != second
    pd.testing.assert_frame_equal(pd.read_parquet(first / "gold" / "cycles.parquet"), pd.read_parquet(second / "gold" / "cycles.parquet"))


def test_artifact_tamper_is_not_silently_reused(tmp_path):
    config = demo_config(tmp_path)
    run = run_pipeline(config, tmp_path / "lake")
    (run / "report.html").write_text("modified")
    with pytest.raises(ValueError, match="changed or missing"):
        run_pipeline(config, tmp_path / "lake")
