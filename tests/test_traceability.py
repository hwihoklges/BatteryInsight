import copy
import json
from pathlib import Path
import shutil

import pytest

from battery_insight import traceability as trace
from battery_insight import library
from battery_insight.document_assets import asset_path, document_proof
from battery_insight.knowledge_registry import KnowledgeRegistry
from battery_insight.slide_candidates import extract_proposals, _proposal
from battery_insight.traceability_demo import create_demo
from battery_insight.traceability_report import graph_data, build_report


@pytest.fixture(scope="module")
def demo_base(tmp_path_factory):
    root = tmp_path_factory.mktemp("trace_base") / "project"
    create_demo(root)
    return root


@pytest.fixture
def project(tmp_path, demo_base):
    return shutil.copytree(demo_base, tmp_path / "project")


def ancestors(graph, node_id):
    result, pending = set(), [node_id]
    while pending:
        current = pending.pop()
        for edge in graph["edges"]:
            if edge["target"] == current and edge["status"] == "confirmed" and edge["source"] not in result:
                result.add(edge["source"])
                pending.append(edge["source"])
    return result


def test_ppt_to_process_cell_test_and_parquet_complete_without_stage_split(project):
    snap = trace.snapshot(project)
    assert len(snap["entities"]) == 6 and len(snap["records"]) == 2 and len(snap["datasets"]) == 2
    assert all(edge["effective_status"] == "confirmed" for edge in snap["edges"])
    assert all(link["valid"] for link in snap["raw_links"])
    graph = graph_data(snap)
    assert {item["rpt_stage"] for item in snap["records"]} == {"BOL"}
    assert not any(edge["relation"] == "conditioned_for" for edge in snap["edges"])
    cells = {item["attributes"]["physical_cell_id"]: item for item in snap["entities"] if item["kind"] == "cell"}
    for record in snap["records"]:
        link = next(item for item in snap["raw_links"] if item["record_id"] == record["record_id"])
        history = ancestors(graph, "dataset:" + link["dataset_id"])
        assert "entity:" + cells[record["physical_cell_id"]]["entity_id"] in history
        assert sum("entity:" + cell["entity_id"] in history for cell in cells.values()) == 1
        assert record["set_no"] == "001"


def test_drafts_are_not_automatically_entities_or_raw_links(tmp_path):
    project = tmp_path / "draft"
    result = create_demo(project, reviewed=False)
    snap = trace.snapshot(project)
    assert result["pending"] == 8
    assert not snap["entities"] and not snap["records"] and not snap["raw_links"]
    extract_proposals(project)
    assert len(trace.snapshot(project)["proposals"]) == 8


def test_archive_makes_evidence_portable_and_detects_tampering(project):
    snap = trace.snapshot(project)
    doc = snap["documents"][0]
    missing_original = {**doc, "source_path": str(project / "no-longer-available.pptx")}
    assert document_proof(project, missing_original)["archived"]
    archive = asset_path(project, doc)
    archive.write_bytes(archive.read_bytes() + b"changed")
    changed = trace.snapshot(project)
    assert all(item["effective_status"] == "needs_review" for item in changed["entities"])
    assert all(not link["valid"] for link in changed["raw_links"])
    assert all(node["status"] == "needs_review" for node in graph_data(changed)["nodes"] if node["kind"] == "dataset")
    with pytest.raises(ValueError, match="장표"):
        trace.save_bundle(project, {"entities": [{"entity_id": snap["entities"][0]["entity_id"], "notes": "review"}]})


def test_process_revision_invalidates_pinned_edges_and_keeps_history(project):
    snap = trace.snapshot(project)
    process = next(item for item in snap["entities"] if item["process_type"] == "배합")
    saved = trace.save_bundle(project, {"entities": [{"entity_id": process["entity_id"], "name": "다시 확인한 배합명"}]})
    assert saved["entities"][0]["revision"] == process["revision"] + 1
    assert len(trace.revisions(project, "entities", process["entity_id"])) == 2
    updated = trace.snapshot(project)
    related = [edge for edge in updated["edges"] if process["entity_id"] in (edge["source_id"], edge["target_id"])]
    assert related and all(edge["effective_status"] == "needs_review" for edge in related)
    with pytest.raises(ValueError, match="최신"):
        trace.save_bundle(project, {"entities": [{"entity_id": process["entity_id"], "content_hash": process["content_hash"], "name": "old"}]})


def test_duplicate_physical_cell_does_not_replace_original_sqlite_row(project):
    before = trace.snapshot(project)
    cell = next(item for item in before["entities"] if item["kind"] == "cell")
    with pytest.raises(ValueError, match="물리 셀"):
        trace.save_bundle(project, {"entities": [{"entity_id": "duplicate-cell", "kind": "cell", "name": "duplicate",
            "attributes": {"physical_cell_id": cell["attributes"]["physical_cell_id"]}}]})
    after = trace.snapshot(project)
    assert {item["entity_id"] for item in after["entities"]} == {item["entity_id"] for item in before["entities"]}


def test_cycle_rejects_and_rolls_back_whole_bundle(project):
    snap = trace.snapshot(project)
    material = next(item for item in snap["entities"] if item["kind"] == "material_lot")
    assembly = next(item for item in snap["entities"] if item["process_type"] == "조립")
    with pytest.raises(ValueError, match="순환"):
        trace.save_bundle(project, {"entities": [{"entity_id": "must-roll-back", "kind": "process_run", "name": "new"}],
            "edges": [{"source_id": assembly["entity_id"], "target_id": material["entity_id"], "relation": "produces"}]})
    assert not any(item["entity_id"] == "must-roll-back" for item in trace.snapshot(project)["entities"])


def test_manufacture_date_cannot_be_after_test_start(project):
    snap = trace.snapshot(project)
    record = snap["records"][0]
    cell = next(item for item in snap["entities"] if item["kind"] == "cell" and item["attributes"]["physical_cell_id"] == record["physical_cell_id"])
    edge = next(item for item in snap["edges"] if item["relation"] == "tested_as" and item["target_id"] == record["record_id"])
    trace.save_bundle(project, {"entities": [{"entity_id": cell["entity_id"], "performed_on": "2026-02-01"}]})
    with pytest.raises(ValueError, match="늦습니다"):
        trace.save_bundle(project, {"edges": [{"edge_id": edge["edge_id"]}]})


def test_cell_test_link_rejects_a_different_physical_cell(project):
    snap = trace.snapshot(project)
    original = snap["records"][0]
    cell = next(item for item in snap["entities"] if item["kind"] == "cell" and item["attributes"]["physical_cell_id"] == original["physical_cell_id"])
    clean = {key: original[key] for key in ("sample_id", "sample_name", "experiment_name", "evidence_ids", "test_date", "date_kind")}
    with KnowledgeRegistry(project / "knowledge/knowledge.sqlite3") as registry:
        other = registry.upsert_records([{**clean, "physical_cell_id": "SYN-CELL-OTHER", "status": "confirmed"}], reviewer="test", reason="synthetic mismatch")[0]
    with pytest.raises(ValueError, match="물리 셀 ID"):
        trace.save_bundle(project, {"edges": [{"source_id": cell["entity_id"], "target_kind": "record", "target_id": other["record_id"],
            "relation": "tested_as", "status": "confirmed", "evidence_ids": other["evidence_ids"]}]})


def test_numeric_process_parameters_require_units(project):
    with pytest.raises(ValueError, match="단위"):
        trace.save_bundle(project, {"entities": [{"name": "drying", "parameters": {"temperature": {"value": 100}}}]})


def test_existing_optional_process_test_link_remains_readable_and_revision_bound(project):
    snap = trace.snapshot(project)
    record = snap["records"][0]
    cell = next(item for item in snap["entities"] if item["kind"] == "cell" and item["attributes"]["physical_cell_id"] == record["physical_cell_id"])
    refs = cell["evidence_ids"]
    trace.save_bundle(project, {"entities": [{"entity_id": "optional-process", "kind": "process_run", "name": "합성 보관",
        "process_type": "보관", "performed_on": "2026-01-04", "status": "confirmed", "evidence_ids": refs}],
        "edges": [{"edge_id": "optional-subject", "source_id": cell["entity_id"], "target_id": "optional-process", "relation": "processed_in",
                   "status": "confirmed", "evidence_ids": refs},
                  {"edge_id": "optional-test", "source_id": "optional-process", "target_id": record["record_id"], "target_kind": "record",
                   "relation": "conditioned_for", "status": "confirmed", "evidence_ids": record["evidence_ids"]}]})
    subject = next(item for item in trace.snapshot(project)["edges"] if item["edge_id"] == "optional-subject")
    trace.save_bundle(project, {"edges": [{"edge_id": subject["edge_id"], "status": "retired"}]})
    changed = trace.snapshot(project)
    conditioning = next(item for item in changed["edges"] if item["relation"] == "conditioned_for")
    assert conditioning["effective_status"] == "needs_review"
    assert conditioning["review_reason"] == "process_subject_binding_changed"


def test_raw_content_change_is_not_reported_as_currently_verified(project):
    dataset = library.library_rows(project)[0]
    path = Path(dataset["path"])
    path.write_bytes(path.read_bytes() + b"changed")
    normal = trace.snapshot(project)
    assert all(link["raw_integrity"] == "catalog_only" for link in normal["raw_links"])
    with pytest.raises(ValueError, match="Parquet"):
        trace.snapshot(project, verify_dataset_id=dataset["dataset_id"])


def test_unambiguous_headers_only_no_date_or_unit_guessing():
    doc = {"document_id": "doc", "sha256": "0" * 64}
    proposal = _proposal(doc, [
        ("시험명", "RPT", ["e1"]), ("세트번호", "001", ["e2"]), ("시험시작일", "260101", ["e3"]),
        ("구동압(MPa)", "800 kPa", ["e4"]), ("보고일", "2026-01-09", ["e5"]),
    ], "slide:1")
    assert proposal["payload"]["status"] == "draft"
    assert proposal["payload"]["set_no"] == "001"
    assert "test_date" not in proposal["payload"] and not proposal["payload"]["metadata"]
    assert proposal["payload"]["report_date"] == "2026-01-09"
    assert len(proposal["warnings"]) == 2


def test_conflicting_duplicate_numeric_headers_remain_unresolved():
    doc = {"document_id": "doc", "sha256": "0" * 64}
    proposal = _proposal(doc, [("공정명", "압착", ["e1"]), ("압착압(MPa)", "100", ["e2"]), ("압착압(MPa)", "200", ["e3"])], "slide:1")
    assert not proposal["payload"]["parameters"] and proposal["warnings"]


def test_report_escapes_document_text_and_protects_existing_output(project):
    entity = trace.snapshot(project)["entities"][0]
    trace.save_bundle(project, {"entities": [{"entity_id": entity["entity_id"], "name": "</script><script>alert(1)</script>"}]})
    page = build_report(project)
    html = page.read_text(encoding="utf-8")
    assert "</script><script>alert(1)</script>" not in html
    assert "\\u003c/script\\u003e" in html
    with pytest.raises(ValueError, match="덮어"):
        path = project / "existing.json"
        path.write_text('{"foreign": true}')
        trace.export_json(project, path)
    assert json.loads(path.read_text()) == {"foreign": True}
