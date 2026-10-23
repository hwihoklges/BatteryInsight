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


def test_ppt_to_process_cell_test_and_parquet_complete_and_stage_specific(project):
    snap = trace.snapshot(project)
    assert len(snap["entities"]) == 6 and len(snap["records"]) == 2 and len(snap["datasets"]) == 2
    assert all(edge["effective_status"] == "confirmed" for edge in snap["edges"])
    assert all(link["valid"] for link in snap["raw_links"])
    graph = graph_data(snap)
    wip = next(item for item in snap["entities"] if item["process_type"] == "WIP")
    records = {item["rpt_stage"]: item for item in snap["records"]}
    for stage in ("BOL", "after_WIP"):
        link = next(item for item in snap["raw_links"] if item["record_id"] == records[stage]["record_id"])
        history = ancestors(graph, "dataset:" + link["dataset_id"])
        assert ("entity:" + wip["entity_id"] in history) == (stage == "after_WIP")
        assert any(item.startswith("entity:") for item in history)
    assert records["BOL"]["set_no"] == "001"


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
    wip = next(item for item in snap["entities"] if item["process_type"] == "WIP")
    with pytest.raises(ValueError, match="순환"):
        trace.save_bundle(project, {"entities": [{"entity_id": "must-roll-back", "kind": "process_run", "name": "new"}],
            "edges": [{"source_id": wip["entity_id"], "target_id": material["entity_id"], "relation": "produces"}]})
    assert not any(item["entity_id"] == "must-roll-back" for item in trace.snapshot(project)["entities"])


def test_future_wip_cannot_be_bound_to_pre_wip_rpt(project):
    snap = trace.snapshot(project)
    wip = next(item for item in snap["entities"] if item["process_type"] == "WIP")
    bol = next(item for item in snap["records"] if item["rpt_stage"] == "BOL")
    with pytest.raises(ValueError, match="늦습니다"):
        trace.save_bundle(project, {"edges": [{"source_id": wip["entity_id"], "target_kind": "record", "target_id": bol["record_id"],
            "relation": "conditioned_for", "status": "confirmed", "evidence_ids": bol["evidence_ids"]}]})


def test_conditioning_rejects_a_different_physical_cell(project):
    snap = trace.snapshot(project)
    wip = next(item for item in snap["entities"] if item["process_type"] == "WIP")
    original = next(item for item in snap["records"] if item["rpt_stage"] == "after_WIP")
    clean = {key: original[key] for key in ("sample_id", "sample_name", "experiment_name", "evidence_ids", "test_date", "date_kind")}
    with KnowledgeRegistry(project / "knowledge/knowledge.sqlite3") as registry:
        other = registry.upsert_records([{**clean, "physical_cell_id": "SYN-CELL-OTHER", "status": "confirmed"}], reviewer="test", reason="synthetic mismatch")[0]
    with pytest.raises(ValueError, match="대상 셀"):
        trace.save_bundle(project, {"edges": [{"source_id": wip["entity_id"], "target_kind": "record", "target_id": other["record_id"],
            "relation": "conditioned_for", "status": "confirmed", "evidence_ids": other["evidence_ids"]}]})


def test_numeric_process_parameters_require_units(project):
    with pytest.raises(ValueError, match="단위"):
        trace.save_bundle(project, {"entities": [{"name": "drying", "parameters": {"temperature": {"value": 100}}}]})


def test_retiring_process_subject_invalidates_conditioning_claim(project):
    snap = trace.snapshot(project)
    subject = next(item for item in snap["edges"] if item["relation"] == "processed_in")
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
