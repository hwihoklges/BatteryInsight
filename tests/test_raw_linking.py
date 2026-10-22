"""Explicit document/raw joins: ambiguous keys, immutable sources and revisions."""
import copy
import csv
import hashlib
import json
from pathlib import Path

import pytest

from battery_insight.knowledge_registry import KnowledgeRegistry
from battery_insight.raw_linking import (SOURCE_FIELDS, apply_confirmed_links, propose_links,
                                         raw_inventory, review_links, write_raw_key_template)


def write_database(root, rows):
    root.mkdir(exist_ok=True)
    (root / "database_manifest.json").write_text(json.dumps({
        "schema_version": "raw_database_v1", "experiments": rows}), encoding="utf-8")
    fields = list(dict.fromkeys(key for row in rows for key in row))
    with (root / "experiments.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "raw_database"
    rows = [{"experiment_id": f"raw-{i}", "source_id": f"source-{i}",
             "source_sha256": hashlib.sha256(f"source{i}".encode()).hexdigest(),
             "cell_id": f"channel-cell-{i}", "test_id": f"test-{i}",
             "source_container": str(tmp_path / "260129_set007_RPT.zip"),
             "source_channel": f"CH{i:02d}", "test_type": "RPT", "rpt_stage": "BOL",
             "label": f"default {i}", "design_id": "", "batch_id": "", "physical_cell_id": ""}
            for i in range(2)]
    write_database(root, rows)
    registry = KnowledgeRegistry(tmp_path / "knowledge.sqlite")
    registry.import_document({"sha256": "a" * 64, "status": "complete", "blocks": [
        {"block_id": "ev-slide1", "locator": "slide:1:shape:2", "text": "2026-01-29 SET007 sample A"}]})
    yield root, rows, registry
    registry.close()


def record(registry, record_id="r1", **changes):
    return registry.upsert_records([{
        "record_id": record_id, "status": "confirmed", "sample_id": "sample-a", "sample_name": "A",
        "experiment_name": "pressure RPT", "test_date": "2026-01-29", "date_kind": "test_start",
        "namespace": "project-a", "set_no": "007", "test_type": "RPT", "rpt_stage": "BOL",
        "design_id": "D-A", "batch_id": "B-A", "evidence_ids": ["ev-slide1"], **changes,
    }], reviewer="reviewer", reason="Checked slide and sample register")[0]


def keys(identity="raw-0", **changes):
    return {"experiment_id": identity, "test_date": "2026-01-29", "date_kind": "test_start",
            "namespace": "project-a", "set_no": "007", **changes}


def decision(rows, rec, action="confirm"):
    return {"experiment_id": rows[0]["experiment_id"], "record_id": rec["record_id"],
            "expected_record_revision": rec["revision"], "source_sha256": rows[0]["source_sha256"], "decision": action}


def linked(setup, **record_changes):
    root, rows, registry = setup
    rec = record(registry, **record_changes)
    proposals = propose_links(root, registry.list_records(), [keys()])
    overlay = review_links(root, registry, proposals, [decision(rows, rec)], "reviewer", "Confirmed raw channel map")
    return root, rows, registry, rec, proposals, overlay


def test_inventory_hints_never_become_confirmed_keys_and_originals_are_unchanged(setup, tmp_path):
    root, rows, registry = setup
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    inventory = raw_inventory(root)
    assert inventory["rows"][0]["candidate_dates"] == ["2026-01-29"]
    assert inventory["rows"][0]["candidate_set_numbers"] == ["007"]
    for field in SOURCE_FIELDS:
        assert inventory["rows"][0][field] == rows[0][field]
    output = tmp_path / "raw_keys.csv"
    write_raw_key_template(root, output)
    with output.open(encoding="utf-8-sig", newline="") as handle:
        template = list(csv.DictReader(handle))
    assert template[0]["test_date"] == template[0]["date_kind"] == template[0]["set_no"] == ""
    record(registry)
    result = propose_links(root, registry, output)
    assert {row["status"] for row in result["rows"]} == {"missing_keys"}
    assert result["automatic_links_applied"] == 0
    assert before == {path.name: path.read_bytes() for path in root.iterdir()}
    with pytest.raises(ValueError, match="outside"):
        write_raw_key_template(root, root / "experiments.csv")


@pytest.mark.parametrize("changes,expected", [
    ({"namespace": "project-b"}, "unmatched"),
    ({"set_no": "7"}, "unmatched"),
    ({"test_date": "2026-01-30"}, "unmatched"),
    ({"rpt_stage": "100cyc"}, "conflict"),
    ({"test_type": "Cycle"}, "conflict"),
    ({"channel_id": "CH01"}, "conflict"),
    ({"status": "draft"}, "unmatched"),
    ({"date_kind": "report"}, "unmatched"),
])
def test_collision_guards_and_unconfirmed_records(setup, changes, expected):
    root, _, registry = setup
    record(registry, **changes)
    proposals = propose_links(root, registry, [keys()])
    assert proposals["rows"][0]["status"] == expected
    assert proposals["rows"][0]["candidates"] == []


@pytest.mark.parametrize("change", [{"date_kind": "report"}, {"date_kind": "manufacture"},
                                     {"date_kind": "unknown"}, {"date_kind": ""},
                                     {"test_date": "260129"}, {"test_date": "2026-02-30"},
                                     {"namespace": ""}, {"set_no": ""}])
def test_raw_keys_require_explicit_start_date_and_scope(setup, change):
    root, _, registry = setup
    record(registry)
    result = propose_links(root, registry, [keys(**change)])
    assert result["rows"][0]["status"] == "missing_keys"


def test_sample_name_exact_or_explicit_alias_only(setup):
    root, _, registry = setup
    record(registry, sample_aliases=["Sample-A"])
    for sample, expected in [("A", "unique_candidate"), ("Sample-A", "unique_candidate"), ("a", "conflict")]:
        result = propose_links(root, registry, [keys(sample_name=sample)])
        assert result["rows"][0]["status"] == expected


def test_numeric_set_and_immutable_raw_key_changes_rejected(setup):
    root, rows, registry = setup
    record(registry)
    with pytest.raises(ValueError, match="leading zeros"):
        propose_links(root, registry, [keys(set_no=7)])
    for field, value in [("source_sha256", "c" * 64), ("source_id", "different"),
                         ("test_type", "Cycle"), ("channel_id", "CH99"), ("rpt_stage", "100cyc")]:
        with pytest.raises(ValueError):
            propose_links(root, registry, [keys(**{field: value})])


def test_unique_candidates_are_not_applied_until_review_and_do_not_infer_physical_cells(setup):
    root, rows, registry = setup
    rec = record(registry, metadata={"pressure_mpa": 5, "cathode_active_mass_loading_mg_cm2": 8.5})
    before = copy.deepcopy(rows)
    proposals = propose_links(root, registry, [keys("raw-0"), keys("raw-1")])
    assert [row["status"] for row in proposals["rows"]] == ["unique_candidate", "unique_candidate"]
    assert proposals["automatic_links_applied"] == 0
    overlay = review_links(root, registry, proposals, [decision(rows, rec)], "reviewer", "Channel map checked")
    applied, issues = apply_confirmed_links(rows, overlay, root)
    assert issues == []
    assert applied[0]["label"] == "A" and applied[0]["design_id"] == "D-A"
    assert applied[0]["pressure_mpa"] == 5 and applied[0]["cathode_active_mass_loading_mg_cm2"] == 8.5
    assert applied[0]["physical_cell_id"] == ""
    assert applied[0]["knowledge_link"]["evidence_ids"] == ["ev-slide1"]
    assert applied[0]["knowledge_link"]["set_no"] == "007"
    assert applied[0]["knowledge_link"]["date_kind"] == "test_start"
    assert applied[0]["knowledge_link"]["evidence_locations"][0]["locator"] == "slide:1:shape:2"
    assert applied[1] == rows[1]
    assert rows == before
    for field in (*SOURCE_FIELDS, "cell_id", "test_id"):
        assert applied[0][field] == before[0][field]


def test_explicit_physical_cell_alias_only_exposed_as_metadata(setup):
    root, rows, _, _, _, overlay = linked(setup, physical_cell_id="physical-0001")
    applied, issues = apply_confirmed_links(rows, overlay, root)
    assert issues == [] and applied[0]["physical_cell_id"] == "physical-0001"
    assert applied[0]["cell_id"] == rows[0]["cell_id"]


def test_ambiguous_candidates_need_one_explicit_record_selection(setup):
    root, rows, registry = setup
    record(registry)
    second = record(registry, "r2", sample_id="sample-b", sample_name="B")
    proposals = propose_links(root, registry, [keys()])
    assert proposals["rows"][0]["status"] == "ambiguous"
    overlay = review_links(root, registry, proposals, [decision(rows, second)], "reviewer", "Checked individual channel map")
    applied, issues = apply_confirmed_links(rows, overlay, root)
    assert issues == [] and applied[0]["label"] == "B"
    with pytest.raises(ValueError, match="exactly one"):
        review_links(root, registry, proposals, [{**decision(rows, second), "record_id": "absent"}], "reviewer", "Checked")


def test_review_guards_stale_source_and_record_revision(setup):
    root, rows, registry, rec, proposals, _ = linked(setup)
    with pytest.raises(ValueError, match="source_sha256"):
        review_links(root, registry, proposals, [{**decision(rows, rec), "source_sha256": "c" * 64}], "reviewer", "Checked")
    registry.upsert_records([{"record_id": rec["record_id"], "experiment_name": "Updated experiment"}],
                            reviewer="reviewer", reason="Corrected slide interpretation")
    with pytest.raises(ValueError, match="stale"):
        review_links(root, registry, proposals, [decision(rows, rec)], "reviewer", "Checked")
    rows[0]["source_sha256"] = "d" * 64
    write_database(root, rows)
    with pytest.raises(ValueError, match="source identity"):
        review_links(root, registry, proposals, [decision(rows, rec)], "reviewer", "Checked")


def test_raw_key_csv_changes_after_proposal_are_detected(setup, tmp_path):
    root, rows, registry = setup
    rec = record(registry)
    path = tmp_path / "keys.csv"
    original = keys()
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(original))
        writer.writeheader()
        writer.writerow(original)
    proposals = propose_links(root, registry, path)
    path.write_text(path.read_text().replace("2026-01-29", "2026-01-30"))
    with pytest.raises(ValueError, match="source changed"):
        review_links(root, registry, proposals, [decision(rows, rec)], "reviewer", "Checked")


def test_proposal_key_tampering_and_missing_review_guards(setup):
    root, rows, registry, rec, proposals, overlay = linked(setup)
    proposals["rows"][0]["raw_keys"]["test_date"] = "2026-01-30"
    with pytest.raises(ValueError, match="keys have changed"):
        review_links(root, registry, proposals, [decision(rows, rec)], "reviewer", "Checked")
    for field, value in [("reviewer", ""), ("record_id", "other")]:
        changed = copy.deepcopy(overlay)
        changed["links"][0]["review"][field] = value
        applied, issues = apply_confirmed_links(rows, changed, root)
        assert applied == rows and issues[0]["reason"] == "knowledge_link_missing_review"


def test_source_or_record_changes_skip_stale_links_while_append_is_safe(setup):
    root, rows, registry, rec, _, overlay = linked(setup)
    record(registry, "unrelated", namespace="other-project")
    more = copy.deepcopy(rows[1])
    more.update(experiment_id="raw-2", source_id="source-2", source_sha256="e" * 64)
    write_database(root, rows + [more])
    applied, issues = apply_confirmed_links(rows, overlay, root)
    assert issues == [] and applied[0]["label"] == "A"
    registry.upsert_records([{"record_id": rec["record_id"], "status": "draft"}], reason="Recheck")
    applied, issues = apply_confirmed_links(rows, overlay, root)
    assert applied == rows and issues[0]["reason"] == "knowledge_link_stale_record"
    rows[0]["source_sha256"] = "d" * 64
    write_database(root, rows)
    applied, issues = apply_confirmed_links(rows, overlay, root)
    assert applied == rows and issues[0]["reason"] == "knowledge_link_stale_source"


def test_existing_metadata_cannot_be_silently_overwritten(setup):
    root, rows, registry, rec, proposals, overlay = linked(setup)
    rows[0]["design_id"] = "D-manually-reviewed"
    write_database(root, rows)
    with pytest.raises(ValueError, match="metadata conflicts"):
        review_links(root, registry, proposals, [decision(rows, rec)], "reviewer", "Checked")
    applied, issues = apply_confirmed_links(rows, overlay, root)
    assert applied == rows and issues[0]["reason"] == "knowledge_link_existing_metadata_conflict"


def test_reject_and_clear_retain_review_history(setup):
    root, rows, registry, rec, proposals, overlay = linked(setup)
    rejected = review_links(root, registry, proposals, [decision(rows, rec, "reject")],
                            "reviewer", "Incorrect channel assignment", existing_overlay=overlay)
    assert rejected["links"] == [] and len(rejected["history"]) == 2
    assert overlay["links"]  # no caller object mutation
    cleared = review_links(root, registry, proposals,
                           [{"experiment_id": "raw-0", "decision": "clear", "source_sha256": rows[0]["source_sha256"]}],
                           "reviewer", "Clear obsolete link", existing_overlay=overlay)
    assert cleared["links"] == [] and len(cleared["history"]) == 2


def test_registry_must_exist_and_source_ids_cannot_be_modified_by_overlay(setup, tmp_path):
    root, rows, _, _, _, overlay = linked(setup)
    overlay["links"][0]["source_id"] = "other-source"
    applied, issues = apply_confirmed_links(rows, overlay, root)
    assert applied == rows and issues[0]["reason"] == "knowledge_link_stale_source"
    overlay["registry_path"] = str(tmp_path / "does-not-exist.sqlite")
    with pytest.raises(ValueError, match="does not exist"):
        apply_confirmed_links(rows, overlay, root)
    assert not Path(overlay["registry_path"]).exists()


def test_link_review_and_application_are_read_only_for_both_databases(setup):
    root, rows, registry, rec, proposals, overlay = linked(setup)
    before = {str(path): path.read_bytes() for path in root.iterdir()}
    before[str(registry.path)] = registry.path.read_bytes()
    reviewed = review_links(root, registry.path, proposals, [decision(rows, rec)], "reviewer", "Checked again")
    applied, issues = apply_confirmed_links(rows, reviewed, root)
    assert issues == [] and applied[0]["label"] == "A"
    assert all(Path(path).read_bytes() == encoded for path, encoded in before.items())
