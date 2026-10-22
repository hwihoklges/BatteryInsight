import json

import pandas as pd
import pytest

from battery_insight.cli import main
from battery_insight.dedup import prune_catalog, write_pruned_catalog


def source_catalog():
    return pd.DataFrame([
        {"namespace": "cyclediag", "metric_id": "cyclediag." + name,
         "feature_name": name, "role": role, "origin": origin,
         "unit_status": "needs_review", "group": "test", "is_metadata": role == "meta"}
        for name, role, origin in [
            ("CE", "indicator", "core_declared"),
            ("CI", "indicator", "core_declared"),
            ("Q_relax_source", "meta", "source_column_loop"),
            ("Q_relax_significant", "qc", "source_column_loop"),
        ]
    ])


def registry_file(tmp_path):
    p = tmp_path / "features" / "indicator_registry.py"
    p.parent.mkdir()
    p.write_text("FAMILIES={}\nEXACT_ALIASES={'CI':('CE','CI == 100 - CE')}\n", encoding="utf-8")
    return p


def test_pruning_outputs_preserve_full_inventory_and_show_decisions(tmp_path):
    full = source_catalog()
    registry = registry_file(tmp_path)
    out = tmp_path / "catalog"
    summary = write_pruned_catalog(full, out, registry)
    assert summary["pruning"]["decision_counts"] == {"support_only": 2, "keep": 1, "remove_duplicate": 1}
    pd.testing.assert_frame_equal(pd.read_parquet(out / "all_metrics" / "metrics_catalog.parquet"), full)
    kept = pd.read_parquet(out / "metrics_catalog.parquet")
    assert kept.metric_id.tolist() == ["cyclediag.CE"]
    assert kept.iloc[0].replaced_metric_ids == "cyclediag.CI"
    assert "동일 시험" in kept.iloc[0].selection_conditions
    removed = pd.read_csv(out / "removed_duplicates.csv", encoding="utf-8-sig")
    assert removed.iloc[0].representative_metric_id == "cyclediag.CE"
    support = pd.read_parquet(out / "support_and_review.parquet")
    assert set(support.decision) == {"support_only"}
    page = (out / "metrics_catalog.html").read_text(encoding="utf-8")
    assert "제외한 1개 항목" in page and "cyclediag.CI</td>" in page
    assert "all_metrics/metrics_catalog.html" in page
    policy = json.loads((out / "pruning_policy.json").read_text(encoding="utf-8"))
    assert len(policy["policy_code_sha256"]) == 64
    assert policy["legacy_baseline_choices"]["chgCapa_CCratio"][0] == "CCcapa"


def test_repeated_full_source_is_stable_but_reduced_input_cannot_replace_archive(tmp_path):
    full = source_catalog()
    registry = registry_file(tmp_path)
    out = tmp_path / "catalog"
    write_pruned_catalog(full, out, registry)
    reduced = pd.read_parquet(out / "metrics_catalog.parquet")
    ledger_before = (out / "pruning_decisions.csv").read_bytes()
    archive = out / "all_metrics" / "metrics_catalog.csv"
    archive_before = archive.read_bytes()
    with pytest.raises(ValueError, match="full source"):
        write_pruned_catalog(reduced, out, registry)
    assert archive.read_bytes() == archive_before
    write_pruned_catalog(pd.read_parquet(out / "all_metrics" / "metrics_catalog.parquet"), out, registry)
    assert (out / "pruning_decisions.csv").read_bytes() == ledger_before


def test_empty_catalog_has_actionable_error():
    with pytest.raises(ValueError, match="Empty catalog"):
        prune_catalog(source_catalog().iloc[:0], {"FAMILIES": {}, "EXACT_ALIASES": {}})


def test_catalog_cli_prunes_by_default_and_full_is_opt_in(tmp_path, monkeypatch, capsys):
    full = source_catalog()
    registry_file(tmp_path)
    monkeypatch.setattr("battery_insight.catalog.build_catalog", lambda *args: full)
    args = ["catalog", "--cyclediag-root", str(tmp_path), "--studio-root", str(tmp_path)]
    reduced_out = tmp_path / "selected"
    assert main(args + ["--out", str(reduced_out)]) == 0
    assert len(pd.read_parquet(reduced_out / "metrics_catalog.parquet")) == 1
    full_out = tmp_path / "all"
    assert main(args + ["--full", "--out", str(full_out)]) == 0
    assert len(pd.read_parquet(full_out / "metrics_catalog.parquet")) == len(full)
    assert not (full_out / "all_metrics").exists()
    capsys.readouterr()


def test_prune_cli_csv_and_failed_reduced_reinput(tmp_path, capsys):
    registry = registry_file(tmp_path)
    full_path = tmp_path / "full.csv"
    source_catalog().to_csv(full_path, index=False, encoding="utf-8-sig")
    out = tmp_path / "selected"
    assert main(["prune", "--catalog", str(full_path), "--registry", str(registry), "--out", str(out)]) == 0
    assert main(["prune", "--catalog", str(out / "metrics_catalog.parquet"), "--registry", str(registry), "--out", str(out)]) == 2
    assert "full source" in capsys.readouterr().err
