import pandas as pd
import pytest
from battery_insight.external_screen import screen_external


def frames():
    cycles = pd.DataFrame([dict(cell_id="A", test_id="T", cycle=i, test_type="Cycle", capacity_complete=True,
                                source_path="raw.csv", source_hash="rawhash", completion_status="complete") for i in range(1, 6)])
    external = pd.DataFrame([dict(cell_id="A", test_id="T", cycle=i, metric_id="aim.reviewed_resistance", value=10 if i < 5 else 30,
                                  unit="mohm", definition_version="actual-v1", source_revision="a" * 64,
                                  source_path="export.csv", source_sha256="x", source_row=i, observed_until_cycle=i) for i in range(1, 6)])
    settings = {"baseline_window": 3, "min_baseline_cycles": 3, "metrics": {"aim.reviewed_resistance": {"unit": "mohm", "absolute_floor": 1, "direction": "high"}}}
    return cycles, external, settings


def test_external_screen_joins_and_keeps_metric_provenance():
    cycles, external, settings = frames()
    out, issues = screen_external(cycles, external, settings)
    assert len(out) == 5
    assert out.source_path.eq("export.csv").all()
    assert out.raw_source_path.eq("raw.csv").all()
    assert out.iloc[-1].robust_score > 4
    assert issues.empty


def test_future_cutoff_not_used_as_early_evidence():
    cycles, external, settings = frames()
    external["observed_until_cycle"] = 100
    out, issues = screen_external(cycles, external, settings)
    assert out.empty
    assert "external_future_information" in issues.code.tolist()


def test_external_wrong_unit_fails_explicitly():
    cycles, external, settings = frames()
    external["unit"] = "ohm"
    with pytest.raises(ValueError, match="Unit mismatch"):
        screen_external(cycles, external, settings)
