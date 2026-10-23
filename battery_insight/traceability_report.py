"""Standalone offline genealogy graph; document strings are data, never HTML."""
from pathlib import Path
import json
import uuid

from . import traceability as trace
from .library import atomic_json


def graph_data(snap):
    nodes, edges = [], []
    documents = {item["document_id"]: item for item in snap["documents"]}
    evidence = {item["evidence_id"]: item for item in snap["evidence"]}
    def proof_ok(ids):
        return bool(ids) and all(key in evidence and documents[evidence[key]["document_id"]]["proof"]["valid"] for key in ids)
    for item in snap["entities"]:
        nodes.append({"id": "entity:" + item["entity_id"], "kind": item["kind"], "label": item["name"], "status": item["effective_status"],
                      "date": item["performed_on"], "data": item, "evidence_ids": item["evidence_ids"]})
    for item in snap["records"]:
        status = item["status"] if item["status"] != "confirmed" or proof_ok(item["evidence_ids"]) else "needs_review"
        nodes.append({"id": "record:" + item["record_id"], "kind": "record", "label": item.get("experiment_name") or item["record_id"],
                      "date": item.get("test_date", ""), "status": status, "data": item, "evidence_ids": item["evidence_ids"]})
    raw_links = {item["dataset_id"]: item for item in snap["raw_links"]}
    for item in snap["datasets"]:
        link = raw_links.get(item["dataset_id"])
        status = "confirmed" if link and link["valid"] else "needs_review" if link else "unlinked"
        nodes.append({"id": "dataset:" + item["dataset_id"], "kind": "dataset", "label": item["relative_path"],
                      "status": status, "date": item["metadata"].get("test_date", ""),
                      "data": item, "evidence_ids": []})
    for item in snap["edges"]:
        edges.append({"id": item["edge_id"], "source": "entity:" + item["source_id"],
                      "target": ("entity:" if item["target_kind"] == "entity" else "record:") + item["target_id"],
                      "label": trace.RELATIONS[item["relation"]], "status": item["effective_status"], "data": item, "evidence_ids": item["evidence_ids"]})
    for item in snap["raw_links"]:
        edges.append({"id": "raw:" + item["dataset_id"], "source": "record:" + item["record_id"], "target": "dataset:" + item["dataset_id"],
                      "label": "raw/Parquet 연결", "status": "confirmed" if item["valid"] else "needs_review", "data": item, "evidence_ids": []})
    return {"schema_version": "traceability_graph_v1", "nodes": nodes, "edges": edges, "documents": snap["documents"], "evidence": snap["evidence"],
            "generated_at": snap["generated_at"], "pending": sum(item["state"] == "pending" for item in snap["proposals"])}


def build_report(project, *, focus=None, verify=False):
    project = Path(project).resolve()
    snap = trace.snapshot(project, verify_dataset_id=focus if verify else None)
    data = graph_data(snap)
    data["initial_focus"] = "dataset:" + focus if focus else ""
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    assets = Path(__file__).parent / "assets"
    css = (assets / "traceability.css").read_text(encoding="utf-8")
    js = (assets / "traceability.js").read_text(encoding="utf-8")
    html = """<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>BatteryInsight 공정·실험 추적</title><style>__CSS__</style>
<header><small>BatteryInsight · 로컬 공정·실험 이력</small><h1>이 데이터는 어떤 셀과 공정에서 왔을까?</h1>
<p>소재·공정·전극·물리 셀·시험·Parquet을 확인한 근거로 연결합니다. 노드나 선을 누르면 장표의 원문과 슬라이드 위치를 볼 수 있습니다.</p><div id="summary"></div></header>
<nav><input id="search" placeholder="셀 ID · 로트 · 공정 · 시험 · raw 검색"><select id="focus"><option value="">전체 연결</option></select>
<select id="direction"><option value="up">선택 항목의 이전 이력</option><option value="down">이후 시험·데이터</option><option value="both">이전 + 이후</option></select>
<label><input type="checkbox" id="review">초안·재검토 표시</label><label><input type="checkbox" id="unlinked">미연결 raw 표시</label></nav>
<div id="types"></div><main><section><div id="notice"></div><div id="canvas"><svg id="graph" xmlns="http://www.w3.org/2000/svg"></svg></div>
<details><summary>이력 시간순 목록</summary><div id="timeline"></div></details></section><aside id="detail"><h2>연결 근거</h2><p>항목을 선택하세요. 같은 셀의 후처리 공정은 해당 시험에 명시적으로 연결할 때만 그 시험의 이전 이력에 포함됩니다.</p></aside></main>
<footer>확정은 기록·연결의 검토 상태입니다. 생략된 공정이나 소재가 없다는 보장은 아닙니다. 원본 시계열 전체를 이 페이지에 싣지 않습니다. raw 무결성 ‘catalog_only’는 등록 시점 해시의 대응만 확인한 상태이며 현재 파일을 다시 읽은 검사는 별도입니다.</footer>
<script type="application/json" id="trace-data">__DATA__</script><script>__JS__</script></html>"""
    html = html.replace("__CSS__", css).replace("__JS__", js).replace("__DATA__", encoded)
    output = project / "traceability"
    output.mkdir(parents=True, exist_ok=True)
    target = output / "index.html"
    if target.exists() and "BatteryInsight 공정·실험 추적" not in target.read_text(encoding="utf-8"):
        raise ValueError("다른 파일을 덮어쓸 수 없습니다: " + str(target))
    graph_file = output / "graph.json"
    if graph_file.exists() and json.loads(graph_file.read_text(encoding="utf-8")).get("schema_version") != "traceability_graph_v1":
        raise ValueError("다른 graph.json 파일을 덮어쓸 수 없습니다.")
    temporary = output / ("index." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(html, encoding="utf-8")
    temporary.replace(target)
    atomic_json(graph_file, data)
    return target
