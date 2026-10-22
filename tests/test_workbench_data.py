"""Readonly evidence integrity, upload preservation and dynamic-window data."""
import hashlib
import io
import json

import numpy as np
import pandas as pd
import pytest

from battery_insight import workbench_data as workbench
from battery_insight.ingest import sha256_file
from test_experiment_updates import registration, metric_specs, write_json, write_registrations, write_run_tables


def seal_run(run):
    artifacts = {p.relative_to(run).as_posix(): sha256_file(p) for p in run.rglob("*") if p.is_file() and p.name != "run_manifest.json"}
    write_json(run / "run_manifest.json", {"status": "complete", "artifact_sha256": artifacts})


def measured_run(tmp_path):
    output = tmp_path / "output"
    run = output / "lake" / "runs" / "measured"
    row = registration()
    raw = tmp_path / "source.csv"
    raw.write_bytes(b"original raw must remain unchanged\n")
    source_hash = sha256_file(raw)
    cycles = pd.DataFrame([dict(cell_id=row["cell_id"], test_id=row["test_id"], test_type="Cycle", include=True,
                               cycle=c, life_index=i, charge_cc_capacity_ah=0.8, charge_cv_capacity_ah=0.2,
                               source_hash=source_hash, source_row_start=1, source_row_end=43)
                           for i, c in enumerate([10., 20.], 1)])
    write_run_tables(run, {"cycles": cycles})
    silver = []
    for cycle in (10., 20.):
        for i in range(43):
            silver.append(dict(cell_id=row["cell_id"], test_id=row["test_id"], test_type="Cycle", include=True,
                               cycle=cycle, segment_id="cc" if i < 40 else "cv", step_id="cc" if i < 40 else "cv",
                               time_s=float(i + (cycle - 10) * 100), step_time_s=float(i if i < 40 else i - 40),
                               voltage_v=3. + i * .01 if i < 40 else 3.4, current_a=1., mode="CC" if i < 40 else "CV",
                               capacity_ah=i / 3600, soc_pct=float(i), source_path=str(raw), source_hash=source_hash,
                               source_row=i + 1 + (43 if cycle == 20 else 0)))
    partition = run / "silver" / hashlib.sha256(row["test_id"].encode()).hexdigest()[:16]
    partition.mkdir(parents=True)
    silver_path = partition / "part.parquet"
    pd.DataFrame(silver).to_parquet(silver_path, index=False, row_group_size=43)
    write_json(run / "config_snapshot.json", {"sources": [dict(path=str(raw), cell_id=row["cell_id"], test_id=row["test_id"], test_type="Cycle")], "features": {}})
    write_json(run / "input_snapshot.json", [dict(kind="raw", path=str(raw), sha256=source_hash, cell_id=row["cell_id"], test_id=row["test_id"])])
    seal_run(run)
    write_registrations(output / "experiments.csv", [row])
    write_json(output / "update_state.json", {"records": {row["experiment_id"]: {"run_path": str(run), "raw_sha256": source_hash}}})
    return output, run, row, raw, silver_path


def test_csv_preserves_leading_zero_ids_korean_and_extra_metadata():
    result = workbench.parse_registration_upload("experiment_id,cell_id,custom_note\n0001,0012,한글\n".encode("utf-8-sig"), "registration.csv")
    assert result["rows"] == [{"experiment_id": "0001", "cell_id": "0012", "custom_note": "한글"}]
    assert result["requires_sheet_selection"] is False
    assert workbench.parse_registration_upload("id,메모\n01,검토\n".encode("cp949"), "legacy.csv")["encoding"] == "cp949"


@pytest.mark.parametrize("data,match", [("id,id\n1,2\n", "unique"), ("id,x\n1\n", "column count"),
                                         ("id\n=1+1\n", "Formulas"), ("id\nInfinity\n", "finite")])
def test_bad_csv_is_rejected(data, match):
    with pytest.raises(ValueError, match=match):
        workbench.parse_registration_upload(data.encode(), "input.csv")


def workbook_bytes(book):
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_xlsx_requires_sheet_choice_and_preserves_formatted_ids():
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = "등록"
    sheet.append(["experiment_id", "cell_id", "extra"])
    sheet.append(["0007", 12, "memo"])
    sheet["B2"].number_format = "00000"
    book.create_sheet("설명")
    content = workbook_bytes(book)
    choices = workbench.parse_registration_upload(content, "input.xlsx")
    assert choices["sheets"] == ["등록", "설명"] and choices["requires_sheet_selection"]
    parsed = workbench.parse_registration_upload(content, "input.xlsx", sheet="등록")
    assert parsed["rows"][0] == {"experiment_id": "0007", "cell_id": "00012", "extra": "memo"}
    sheet["C2"] = "=1+1"
    with pytest.raises(ValueError, match="Formulas"):
        workbench.parse_registration_upload(workbook_bytes(book), "input.xlsx", sheet="등록")


def test_upload_has_hard_row_and_byte_limits(monkeypatch):
    monkeypatch.setattr(workbench, "MAX_UPLOAD_ROWS", 1)
    with pytest.raises(ValueError, match="row limit"):
        workbench.parse_registration_upload(b"id\n1\n2\n", "input.csv")
    monkeypatch.setattr(workbench, "MAX_UPLOAD_BYTES", 2)
    with pytest.raises(ValueError, match="10 MiB"):
        workbench.parse_registration_upload(b"id\n", "input.csv")


def test_evidence_selected_cycle_derivatives_actual_rows_and_raw_unchanged(tmp_path):
    output, run, row, raw, _ = measured_run(tmp_path)
    original = raw.read_bytes()
    quality = pd.DataFrame([dict(cell_id=row["cell_id"], test_id=row["test_id"], cycle=20., code="actual_flag", source_row=44)])
    quality.to_parquet(run / "gold" / "quality.parquet", index=False)
    seal_run(run)
    result = workbench.read_evidence(output, row["experiment_id"], 20.)
    assert result["cycles"] == [{"cycle": 10., "life_index": 1.}, {"cycle": 20., "life_index": 2.}]
    assert {p["source_row"] for curve in result["curves"] for p in curve["points"]} == set(range(44, 87))
    assert result["quality"][0]["code"] == "actual_flag"
    cc, cv = result["curves"]
    assert cc["derivative"]["status"] == "valid" and cv["dqdv"] == []
    assert cc["dqdv"][0]["value"] == pytest.approx(1 / 36)
    assert cc["dvdq"][0]["value"] == pytest.approx(36)
    assert cc["source"]["sha256"] == sha256_file(raw)
    assert raw.read_bytes() == original
    json.dumps(result, allow_nan=False)


def test_evidence_reads_actual_synthetic_pipeline_outputs(tmp_path):
    from battery_insight.experiment_updates import update_experiments
    from test_experiment_updates import workspace
    config, inbox, _, row, output = workspace(tmp_path, synthetic=True)
    raw = inbox / row["raw_path"]
    original_hash = sha256_file(raw)
    payload = update_experiments(config, output)
    assert payload["ingestion"]["processed"] == 1
    evidence = workbench.read_evidence(output, row["experiment_id"])
    assert len(evidence["cycles"]) == 8 and evidence["curves"]
    assert evidence["source_hash"] == original_hash == sha256_file(raw)
    assert {curve["source"]["sha256"] for curve in evidence["curves"]} == {original_hash}
    assert any(curve["dqdv"] for curve in evidence["curves"])
    json.dumps(evidence, allow_nan=False)


def test_evidence_downsampling_reports_counts_and_keeps_endpoints(tmp_path, monkeypatch):
    output, _, row, _, _ = measured_run(tmp_path)
    monkeypatch.setattr(workbench, "MAX_CURVE_POINTS", 7)
    curve = workbench.read_evidence(output, row["experiment_id"])["curves"][0]
    assert curve["sampling"] == {"original_points": 40, "returned_points": 7, "downsampled": True, "method": "evenly spaced source rows including endpoints"}
    assert curve["points"][0]["source_row"] == 1 and curve["points"][-1]["source_row"] == 40


def test_evidence_rejects_modified_artifact_even_outside_selected_row_group(tmp_path):
    output, _, row, _, silver = measured_run(tmp_path)
    frame = pd.read_parquet(silver)
    frame.loc[frame.cycle.eq(20), "voltage_v"] = 99.
    frame.to_parquet(silver, index=False)
    with pytest.raises(ValueError, match="modified"):
        workbench.read_evidence(output, row["experiment_id"], 10.)


def test_evidence_rejects_unavailable_cycle_and_mismatched_source_hash(tmp_path):
    output, run, row, _, silver = measured_run(tmp_path)
    with pytest.raises(ValueError, match="not available"):
        workbench.read_evidence(output, row["experiment_id"], 99.)
    frame = pd.read_parquet(silver)
    frame["source_hash"] = "different-source"
    frame.to_parquet(silver, index=False)
    seal_run(run)
    with pytest.raises(ValueError, match="source hash"):
        workbench.read_evidence(output, row["experiment_id"], 10.)


def test_evidence_cycle_read_limit_and_invalid_clock_do_not_fabricate_derivatives(tmp_path, monkeypatch):
    output, run, row, _, silver = measured_run(tmp_path)
    monkeypatch.setattr(workbench, "MAX_CYCLE_POINTS", 20)
    with pytest.raises(ValueError, match="point limit"):
        workbench.read_evidence(output, row["experiment_id"], 10.)
    monkeypatch.setattr(workbench, "MAX_CYCLE_POINTS", 200000)
    frame = pd.read_parquet(silver)
    frame.loc[1, "time_s"] = frame.loc[0, "time_s"]
    frame.to_parquet(silver, index=False)
    seal_run(run)
    curve = workbench.read_evidence(output, row["experiment_id"], 10.)["curves"][0]
    assert curve["dqdv"] == curve["dvdq"] == []
    assert curve["derivative"]["status"] == "not_supported"


def test_run_extras_cache_reuses_and_detects_changed_content(tmp_path, monkeypatch):
    from battery_insight import metric_packs
    output, run, row, _, _ = measured_run(tmp_path)
    actual = metric_packs.extract_metric_packs
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return actual(*args, **kwargs)
    monkeypatch.setattr(metric_packs, "extract_metric_packs", counted)
    first = workbench._cached_run_extras(output, run, row, [])
    assert workbench._cached_run_extras(output, run, row, []) == first
    assert len(calls) == 1
    cache = next((output / "workbench_cache").glob("*.json"))
    cache.write_text('{"data":"tampered"}', encoding="utf-8")
    assert workbench._cached_run_extras(output, run, row, []) == first
    assert len(calls) == 2
    cycles_path = run / "gold" / "cycles.parquet"
    cycles = pd.read_parquet(cycles_path)
    cycles.loc[0, "charge_cc_capacity_ah"] = .4
    cycles.to_parquet(cycles_path, index=False)
    with pytest.raises(ValueError, match="modified"):
        workbench._cached_run_extras(output, run, row, [])
    seal_run(run)
    changed = workbench._cached_run_extras(output, run, row, [])
    assert changed != first


def test_external_bundle_rebuilds_full_life_and_preserves_observation_boundary(tmp_path):
    output, run, row, _, _ = measured_run(tmp_path)
    external_spec = {"metric_id": "external.a", "column": "a", "table": "external_features", "role": "indicator",
                     "unit": "V", "definition_version": "v1", "source_revision": "rev1", "namespace": "verified", "family": "external_voltage"}
    external = pd.DataFrame([dict(cell_id=row["cell_id"], test_id=row["test_id"], cycle=c, metric_id="a", value=v,
                                  observed_until_cycle=until, unit="V", definition_version="v1", source_revision="rev1", namespace="verified", role="indicator")
                             for c, v, until in [(10., 3., 20.), (20., 4., 20.)]])
    external.to_parquet(run / "gold" / "external_features.parquet", index=False)
    seal_run(run)
    pd.DataFrame([dict(experiment_id=row["experiment_id"], metric_id="external.a", life_index=1., value=999., unit="V"),
                  dict(experiment_id=row["experiment_id"], metric_id="local.ce", life_index=1., value=99., unit="%")]).to_parquet(output / "observations.parquet", index=False)
    config = {"metrics": metric_specs() + [external_spec], "comparison": {"life_min": 1, "life_max": 1}, "is_demo": True}
    bundle = workbench.build_workbench_bundle(output, config, [row, registration("pending"), registration("disabled", include=False)],
                                             {"ingestion": {"records": [{"experiment_id": row["experiment_id"], "run_path": str(run)}]}})
    values = [obs for obs in bundle["observations"] if obs["metric_id"] == "external.a"]
    assert [(v["life_index"], v["value"], v["observed_until_life"]) for v in values] == [(1., 3., 2.), (2., 4., 2.)]
    assert {r["experiment_id"] for r in bundle["experiments"]} == {"registered", "pending"}
    assert bundle["is_demo"]
    assert all(s["selected_by_default"] == (not s["metric_id"].startswith("pack.")) for s in bundle["metrics"])
    assert all(not p["selected_by_default"] for p in bundle["available_packs"])
    assert "temperature_c" in {s["field_id"] for s in bundle["control_fields"]}
    assert bundle["design_fields"] == []
    json.dumps(bundle, allow_nan=False)
    external.loc[0, "observed_until_cycle"] = 15.
    external.to_parquet(run / "gold" / "external_features.parquet", index=False)
    seal_run(run)
    observations, issues = workbench._full_external_observations(run, row, [external_spec])
    assert len(observations) == 1 and issues[0]["reason"] == "external_observation_boundary_unmapped"


def test_control_fields_customization_is_unweighted_and_keeps_missing_metadata(tmp_path):
    fields = [{"field_id": "fixture_id", "label": "Fixture", "kind": "categorical", "unit": ""}]
    config = {"metrics": metric_specs(), "comparison": {}, "workbench": {"control_fields": fields}}
    bundle = workbench.build_workbench_bundle(tmp_path, config, [registration()], {})
    assert bundle["control_fields"] == fields
    assert "fixture_id" not in bundle["experiments"][0]
    assert bundle["design_fields"] == []


def test_pending_pack_definitions_preserve_user_selection_without_stale_observations(tmp_path):
    from battery_insight.workbench_server import Workbench, defaults
    output, run, row, _, _ = measured_run(tmp_path)
    config = {"metrics": metric_specs(), "comparison": {"life_min": 1, "life_max": 2, "min_points": 2}}
    ready = {"ingestion": {"records": [{"experiment_id": row["experiment_id"], "run_path": str(run)}]}}
    available = workbench.build_workbench_bundle(output, config, [row], ready)
    selected = "pack.cc_cv.cc_fraction_pct"
    options = defaults(available)
    options.update(metric_ids=[selected], min_overlap=1)
    controller = object.__new__(Workbench)
    controller.bundle = available
    assert controller._reconcile_options(options)["metric_ids"] == [selected]
    dashboard = output / "dashboard"
    dashboard.mkdir()
    # This stale value must never migrate from the previously published bundle.
    available["observations"][0]["value"] = 99999.
    write_json(dashboard / "data.json", available)
    pending = workbench.build_workbench_bundle(output, config, [row], {"ingestion": {"records": []}})
    controller.bundle = pending
    pending_options = controller._reconcile_options(options)
    assert pending_options["metric_ids"] == [selected]
    assert pending["observations"] == []
    retained = next(m for m in pending["metrics"] if m["metric_id"] == selected)
    assert retained["available"] is False and retained["selected_by_default"] is False
    assert retained["availability_reason"] == "current_data_unavailable"
    cc_cv = next(p for p in pending["available_packs"] if p["pack_id"] == "cc_cv")
    assert cc_cv["available"] is False and cc_cv["available_metric_ids"] == []
    assert selected in cc_cv["unavailable_metric_ids"]
    retained["label"] = "Old definition label"
    write_json(dashboard / "data.json", pending)
    restored = workbench.build_workbench_bundle(output, config, [row], ready)
    controller.bundle = restored
    assert controller._reconcile_options(pending_options)["metric_ids"] == [selected]
    current = next(m for m in restored["metrics"] if m["metric_id"] == selected)
    assert current["available"] and current["label"] != "Old definition label"
    assert "availability_reason" not in current
    assert next(o["value"] for o in restored["observations"] if o["metric_id"] == selected) == 80.


def test_previous_bundle_does_not_restore_removed_basic_metrics_or_target_specs(tmp_path):
    output = tmp_path
    dashboard = output / "dashboard"
    dashboard.mkdir()
    write_json(dashboard / "data.json", {"metrics": [dict(metric_specs()[0], metric_id="removed.basic"),
               {"metric_id": "pack.invalid_target", "table": "workbench_pack", "pack_id": "cc_cv", "role": "target"}],
               "observations": [{"metric_id": "removed.basic", "value": 99.}]})
    bundle = workbench.build_workbench_bundle(output, {"metrics": metric_specs(), "comparison": {}}, [registration()], {})
    assert {m["metric_id"] for m in bundle["metrics"]} == {m["metric_id"] for m in metric_specs()}
    assert bundle["observations"] == []


@pytest.mark.parametrize("field,bad", [("unit", "mV"), ("source_revision", "other"), ("definition_version", "v2"), ("namespace", "other"), ("role", "target")])
def test_external_metadata_must_match_verified_definition(tmp_path, field, bad):
    _, run, row, _, _ = measured_run(tmp_path)
    spec = dict(metric_id="external.a", column="a", unit="V", definition_version="v1", source_revision="rev", namespace="approved")
    point = dict(cell_id=row["cell_id"], test_id=row["test_id"], cycle=10., metric_id="a", value=3., observed_until_cycle=10., unit="V", definition_version="v1", source_revision="rev", namespace="approved", role="indicator")
    point[field] = bad
    pd.DataFrame([point]).to_parquet(run / "gold" / "external_features.parquet", index=False)
    seal_run(run)
    with pytest.raises(ValueError):
        workbench._full_external_observations(run, row, [spec])
