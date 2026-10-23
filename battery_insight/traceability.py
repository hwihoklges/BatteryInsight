"""Reviewed manufacturing genealogy joined to existing experiment/raw links.

No inference from names or document proximity. Every confirmed edge binds the
exact endpoint revisions and slide evidence. Treatment-to-test links are explicit
so a later WIP does not become an ancestor of a pre-WIP RPT on the same cell.
"""
from __future__ import annotations

from contextlib import contextmanager
from collections import deque
from datetime import date
import json
import math
from pathlib import Path
import sqlite3
import uuid

from .library import atomic_json, canonical, identity, now
from .knowledge_registry import KnowledgeRegistry

KINDS = {"material_lot": "소재 로트", "electrode_lot": "전극·전해질막 로트", "process_run": "공정 실행", "cell": "물리 셀"}
RELATIONS = {"consumed_in": "투입", "processed_in": "공정 대상", "produces": "산출", "tested_as": "셀 시험",
             "conditioned_for": "시험 전 공정 적용", "precedes": "공정 선후"}
STATUSES = {"draft", "confirmed", "retired"}


@contextmanager
def connect(project):
    path = Path(project).resolve() / "knowledge/traceability.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and "trace_meta" not in tables:
            raise ValueError("공정 추적 DB가 아닌 파일입니다.")
        if "trace_meta" in tables:
            row = db.execute("SELECT value FROM trace_meta WHERE key='schema'").fetchone()
            if row is None or row[0] != "traceability_v1":
                raise ValueError("지원하지 않는 공정 추적 DB 버전입니다.")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS trace_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO trace_meta VALUES('schema','traceability_v1');
            CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY,physical_cell_id TEXT UNIQUE,
                revision INTEGER NOT NULL,hash TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS edges(id TEXT PRIMARY KEY,revision INTEGER NOT NULL,
                hash TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions(kind TEXT NOT NULL,id TEXT NOT NULL,revision INTEGER NOT NULL,
                hash TEXT NOT NULL,payload TEXT NOT NULL,reviewer TEXT NOT NULL,reason TEXT NOT NULL,at TEXT NOT NULL,
                PRIMARY KEY(kind,id,revision));
            CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending');
        """)
        db.commit()
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def knowledge(project):
    path = Path(project).resolve() / "knowledge/knowledge.sqlite3"
    if not path.is_file():
        return {"documents": [], "evidence": [], "records": []}
    with KnowledgeRegistry(path, read_only=True) as registry:
        return registry.snapshot()


def _items(db, table):
    result = {}
    for row in db.execute(f"SELECT * FROM {table}"):
        item = json.loads(row["payload"])
        if identity(item) != row["hash"]:
            raise ValueError("공정 추적 데이터의 내용 해시가 다릅니다: " + row["id"])
        result[row["id"]] = {**item, "revision": row["revision"], "content_hash": row["hash"]}
    return result


def _text(value, label, required=False):
    if value is None:
        result = ""
    elif not isinstance(value, str):
        raise ValueError(label + ": 문자열을 사용하세요. ID의 선행 0을 유지합니다.")
    else:
        result = value.strip()
    if required and not result:
        raise ValueError(label + " 항목이 필요합니다.")
    return result


def _evidence(value, known):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("evidence_ids는 근거 ID 문자열 목록이어야 합니다.")
    ids = sorted(set(value))
    if set(ids) - known:
        raise ValueError("등록되지 않은 장표 근거가 있습니다: " + str(sorted(set(ids) - known)))
    return ids


def _entity(raw, old, known):
    allowed = {"entity_id", "kind", "name", "process_type", "performed_on", "attributes", "parameters",
               "evidence_ids", "field_evidence", "status", "notes", "revision", "content_hash"}
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise ValueError("지원하지 않는 공정·소재·셀 항목입니다.")
    item = {"entity_id": "ent_" + uuid.uuid4().hex, "kind": "process_run", "name": "", "process_type": "",
            "performed_on": "", "attributes": {}, "parameters": {}, "evidence_ids": [], "field_evidence": {}, "status": "draft", "notes": ""}
    if old:
        item.update({key: old[key] for key in item})
    item.update({key: val for key, val in raw.items() if key in item})
    for key in ("entity_id", "kind", "name", "process_type", "performed_on", "status", "notes"):
        item[key] = _text(item[key], key, key in {"entity_id", "kind", "name", "status"})
    if item["kind"] not in KINDS or item["status"] not in STATUSES:
        raise ValueError("공정 항목 종류 또는 확인 상태를 확인하세요.")
    if old and (old["entity_id"] != item["entity_id"] or old["kind"] != item["kind"]):
        raise ValueError("기존 공정 ID와 종류는 변경할 수 없습니다.")
    if item["performed_on"] and date.fromisoformat(item["performed_on"]).isoformat() != item["performed_on"]:
        raise ValueError("공정·제조일은 YYYY-MM-DD 형식으로 기록하세요.")
    if not isinstance(item["attributes"], dict):
        raise ValueError("attributes는 식별자·문자 정보 객체여야 합니다.")
    item["attributes"] = {_text(key, "속성 이름", True): _text(value, key) for key, value in item["attributes"].items()}
    parameters = {}
    if not isinstance(item["parameters"], dict):
        raise ValueError("parameters는 값·단위 객체여야 합니다.")
    for key, parameter in item["parameters"].items():
        _text(key, "공정 변수 이름", True)
        if not isinstance(parameter, dict) or set(parameter) - {"value", "unit"} or "value" not in parameter:
            raise ValueError(key + ": {value: 값, unit: 단위} 형식이 필요합니다.")
        value = parameter["value"]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)) or isinstance(value, (int, float)) and not math.isfinite(value):
            raise ValueError(key + ": 유효한 숫자 또는 원문 문자열이 필요합니다.")
        unit = _text(parameter.get("unit"), key + " 단위", isinstance(value, (int, float)))
        parameters[key] = {"value": value, "unit": unit}
    item["parameters"] = parameters
    item["evidence_ids"] = _evidence(item["evidence_ids"], known)
    fields = [key for key in ("name", "process_type", "performed_on") if item[key]]
    fields += ["attributes." + key for key, value in item["attributes"].items() if value]
    fields += ["parameters." + key for key in parameters]
    supplied = item["field_evidence"] if "field_evidence" in raw else {key: val for key, val in item["field_evidence"].items() if key in fields}
    if not isinstance(supplied, dict) or set(supplied) - set(fields):
        raise ValueError("값이 있는 항목에만 field_evidence를 지정하세요.")
    item["field_evidence"] = {key: _evidence(supplied.get(key, item["evidence_ids"]), known) for key in fields}
    if any(set(ids) - set(item["evidence_ids"]) for ids in item["field_evidence"].values()):
        raise ValueError("항목별 근거는 이 항목의 evidence_ids 안에서 선택하세요.")
    if item["status"] == "confirmed":
        if not item["evidence_ids"] or any(not ids for ids in item["field_evidence"].values()):
            raise ValueError("확정하려면 각 값의 장표 근거를 지정하세요.")
        if item["kind"] == "cell" and not item["attributes"].get("physical_cell_id"):
            raise ValueError("물리 셀을 확정하려면 physical_cell_id가 필요합니다.")
        if item["kind"] == "process_run" and not item["process_type"]:
            raise ValueError("공정 종류를 입력하세요.")
        if item["kind"].endswith("_lot") and not item["attributes"].get("lot_id"):
            raise ValueError("소재·전극 로트를 확정하려면 lot_id가 필요합니다.")
    return item


def _put(db, table, key, item, reviewer, reason):
    old = db.execute(f"SELECT * FROM {table} WHERE id=?", (key,)).fetchone()
    digest = identity(item)
    if old and old["hash"] == digest:
        return {**item, "revision": old["revision"], "content_hash": digest}
    revision = old["revision"] + 1 if old else 1
    if table == "entities":
        physical = item["attributes"].get("physical_cell_id") if item["kind"] == "cell" else None
        try:
            db.execute("INSERT INTO entities VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET physical_cell_id=excluded.physical_cell_id,revision=excluded.revision,hash=excluded.hash,payload=excluded.payload",
                       (key, physical or None, revision, digest, canonical(item)))
        except sqlite3.IntegrityError as exc:
            raise ValueError("이미 등록된 물리 셀 ID입니다. 기존 셀을 사용하세요.") from exc
    else:
        db.execute("INSERT OR REPLACE INTO edges VALUES(?,?,?,?)", (key, revision, digest, canonical(item)))
    db.execute("INSERT INTO revisions VALUES(?,?,?,?,?,?,?,?)", (table, key, revision, digest, canonical(item), reviewer, reason, now()))
    return {**item, "revision": revision, "content_hash": digest}


def _guard(raw, old):
    if old and (raw.get("content_hash", old["content_hash"]) != old["content_hash"] or raw.get("revision", old["revision"]) != old["revision"]):
        raise ValueError("다른 수정이 먼저 반영됐습니다. 최신 이력을 열고 다시 검토하세요.")


def _acyclic(edges):
    adjacency, indegree = {}, {}
    for edge in edges.values():
        if edge["target_kind"] == "entity" and edge["status"] != "retired":
            source, target = edge["source_id"], edge["target_id"]
            indegree.setdefault(source, 0)
            if target not in adjacency.setdefault(source, set()):
                adjacency[source].add(target)
                indegree[target] = indegree.get(target, 0) + 1
    ready, visited = deque(key for key, count in indegree.items() if not count), 0
    while ready:
        node = ready.popleft()
        visited += 1
        for target in adjacency.get(node, ()):
            indegree[target] -= 1
            if not indegree[target]:
                ready.append(target)
    if visited != len(indegree):
        raise ValueError("공정 연결에 순환이 있습니다. 동일 셀의 후처리는 processed_in → conditioned_for로 시험에 연결하세요.")


def save_bundle(project, payload, *, reviewer="local_user", reason="장표 근거 확인"):
    """Atomic reviewed JSON import. A failed edge rolls back all entity updates."""
    if not isinstance(payload, dict) or set(payload) - {"entities", "edges"}:
        raise ValueError("entities 및 edges 목록을 지정하세요.")
    if any(not isinstance(payload.get(key, []), list) for key in ("entities", "edges")):
        raise ValueError("entities/edges는 목록이어야 합니다.")
    reviewer, reason = _text(reviewer, "검토자", True), _text(reason, "검토 이유", True)
    snap = knowledge(project)
    known = {row["evidence_id"] for row in snap["evidence"]}
    records = {row["record_id"]: row for row in snap["records"]}
    evidence = {row["evidence_id"]: row for row in snap["evidence"]}
    documents = {row["document_id"]: row for row in snap["documents"]}
    proof_cache = {}
    def require_proof(ids):
        from .document_assets import document_proof
        for key in ids:
            document_id = evidence[key]["document_id"]
            if document_id not in proof_cache:
                proof_cache[document_id] = document_proof(project, documents[document_id])["valid"]
            if not proof_cache[document_id]:
                raise ValueError("장표 근거의 사본이 없거나 변경됐습니다. 장표를 다시 확인하세요.")
    saved_entities, saved_edges = [], []
    with connect(project) as db:
        db.execute("BEGIN IMMEDIATE")
        entities, edges = _items(db, "entities"), _items(db, "edges")
        seen = set()
        for raw in payload.get("entities", []):
            if not isinstance(raw, dict):
                raise ValueError("각 공정 항목은 객체여야 합니다.")
            key = raw.get("entity_id")
            if key and key in seen:
                raise ValueError("동일 entity_id를 한 번만 입력하세요.")
            seen.add(key)
            old = entities.get(key)
            _guard(raw, old)
            item = _entity(raw, old, known)
            if item["status"] == "confirmed":
                require_proof(item["evidence_ids"])
            saved = _put(db, "entities", item["entity_id"], item, reviewer, reason)
            entities[item["entity_id"]] = saved
            saved_entities.append(saved)
        seen = set()
        for raw in payload.get("edges", []):
            allowed = {"edge_id", "source_id", "target_id", "target_kind", "relation", "status", "evidence_ids", "role", "notes", "revision", "content_hash"}
            if not isinstance(raw, dict) or set(raw) - allowed:
                raise ValueError("지원하지 않는 공정 연결 항목입니다.")
            key = _text(raw.get("edge_id"), "edge_id") or "edge_" + uuid.uuid4().hex
            if key in seen:
                raise ValueError("동일 edge_id를 한 번만 입력하세요.")
            seen.add(key)
            old = edges.get(key)
            _guard(raw, old)
            item = {"edge_id": key, "target_kind": "entity", "relation": "consumed_in", "status": "draft", "evidence_ids": [], "role": "", "notes": ""}
            if old:
                item.update({name: old.get(name, "") for name in (*item, "source_id", "target_id")})
            item.update({name: value for name, value in raw.items() if name not in {"revision", "content_hash"}})
            for name in ("source_id", "target_id", "target_kind", "relation", "status", "role", "notes"):
                item[name] = _text(item.get(name), name, name not in {"notes", "role"})
            if item["relation"] not in RELATIONS or item["status"] not in STATUSES or item["target_kind"] not in {"entity", "record"}:
                raise ValueError("공정 연결 종류·상태를 확인하세요.")
            source = entities.get(item["source_id"])
            target = (entities if item["target_kind"] == "entity" else records).get(item["target_id"])
            if not source or not target:
                raise ValueError("연결 양 끝의 공정·셀·시험 기록을 먼저 등록하세요.")
            relation, sk, tk = item["relation"], source["kind"], target.get("kind", "record")
            valid = ((relation == "consumed_in" and sk in {"material_lot", "electrode_lot"} and tk == "process_run")
                or (relation == "processed_in" and sk in {"material_lot", "electrode_lot", "cell"} and tk == "process_run")
                or (relation == "produces" and sk == "process_run" and tk in {"material_lot", "electrode_lot", "cell"})
                or (relation == "precedes" and sk == tk == "process_run")
                or (relation == "tested_as" and sk == "cell" and tk == "record")
                or (relation == "conditioned_for" and sk == "process_run" and tk == "record"))
            if not valid:
                raise ValueError("투입·산출·셀 시험의 방향과 종류를 확인하세요.")
            item["evidence_ids"] = _evidence(item["evidence_ids"], known)
            if item["status"] == "confirmed":
                if source["status"] != "confirmed" or target["status"] != "confirmed" or not item["evidence_ids"]:
                    raise ValueError("양 끝의 확정 기록과 연결 근거가 필요합니다.")
                require_proof(item["evidence_ids"] + source["evidence_ids"] + target["evidence_ids"])
                if relation == "tested_as" and (not target.get("physical_cell_id") or source["attributes"].get("physical_cell_id") != target["physical_cell_id"]):
                    raise ValueError("시험 기록과 물리 셀 ID가 같아야 합니다. 시험 기록의 physical_cell_id를 먼저 확인하세요.")
                left_date = source.get("performed_on")
                right_date = target.get("performed_on") if tk != "record" else target.get("test_date") if target.get("date_kind") == "test_start" else None
                if left_date and right_date and left_date > right_date:
                    raise ValueError("공정·제조일이 후속 공정/시험 시작일보다 늦습니다. WIP 이후 이력을 이전 RPT에 연결할 수 없습니다.")
            item["bindings"] = {"source_hash": source["content_hash"], "target_hash": target["content_hash"]}
            saved = _put(db, "edges", key, item, reviewer, reason)
            edges[key] = saved
            saved_edges.append(saved)
        _acyclic(edges)
        unique = set()
        for edge in edges.values():
            if edge["status"] == "retired":
                continue
            signature = (edge["source_id"], edge["target_kind"], edge["target_id"], edge["relation"])
            if signature in unique:
                raise ValueError("같은 연결이 이미 등록되어 있습니다. 기존 edge_id를 수정하세요.")
            unique.add(signature)
        for edge in saved_edges:
            if edge["relation"] != "conditioned_for" or edge["status"] != "confirmed":
                continue
            record = records[edge["target_id"]]
            pending, visited, cells = [edge["source_id"]], set(), set()
            while pending:
                key = pending.pop()
                if key in visited:
                    continue
                visited.add(key)
                entity = entities[key]
                if entity["kind"] == "cell":
                    cells.add(entity["attributes"].get("physical_cell_id"))
                for parent in edges.values():
                    if (parent["target_kind"] == "entity" and parent["target_id"] == key and parent["status"] == "confirmed"
                        and parent["relation"] in {"processed_in", "precedes"}
                        and parent["bindings"] == {"source_hash": entities[parent["source_id"]]["content_hash"], "target_hash": entity["content_hash"]}):
                        require_proof(parent["evidence_ids"] + entities[parent["source_id"]]["evidence_ids"])
                        pending.append(parent["source_id"])
            if not record.get("physical_cell_id") or record["physical_cell_id"] not in cells:
                raise ValueError("후처리 공정의 대상 셀과 시험 셀이 같아야 합니다. processed_in 연결과 물리 셀 ID를 확인하세요.")
    return {"entities": saved_entities, "edges": saved_edges}


def snapshot(project, *, verify_dataset_id=None):
    from .document_assets import document_proof
    from . import library
    snap = knowledge(project)
    documents = {row["document_id"]: {**row, "proof": document_proof(project, row)} for row in snap["documents"]}
    evidence = {row["evidence_id"]: row for row in snap["evidence"]}
    def evidence_valid(ids):
        return bool(ids) and all(key in evidence and documents[evidence[key]["document_id"]]["proof"]["valid"] for key in ids)
    records = {row["record_id"]: row for row in snap["records"]}
    with connect(project) as db:
        entities, edges = _items(db, "entities"), _items(db, "edges")
        proposals = [{**json.loads(row["payload"]), "state": row["state"]} for row in db.execute("SELECT * FROM proposals ORDER BY id")]
    for item in entities.values():
        item["effective_status"] = item["status"] if item["status"] != "confirmed" or evidence_valid(item["evidence_ids"]) else "needs_review"
    for item in edges.values():
        source = entities.get(item["source_id"])
        target = (entities if item["target_kind"] == "entity" else records).get(item["target_id"])
        valid = bool(source and target and source.get("effective_status") == "confirmed" and target.get("effective_status", target["status"]) == "confirmed"
                     and evidence_valid(item["evidence_ids"]) and item["bindings"] == {"source_hash": source["content_hash"], "target_hash": target["content_hash"]})
        if item["target_kind"] == "record" and target:
            valid = valid and evidence_valid(target.get("evidence_ids", []))
        item["effective_status"] = item["status"] if item["status"] != "confirmed" or valid else "needs_review"
    # A conditioning-to-test claim also depends on the current reviewed subject
    # chain, not just its two direct endpoint records.
    for item in edges.values():
        if item["relation"] != "conditioned_for" or item["effective_status"] != "confirmed":
            continue
        pending, visited, owners = [item["source_id"]], set(), set()
        while pending:
            key = pending.pop()
            if key in visited:
                continue
            visited.add(key)
            entity = entities[key]
            if entity["kind"] == "cell":
                owners.add(entity["attributes"].get("physical_cell_id"))
            for parent in edges.values():
                if (parent["effective_status"] == "confirmed" and parent["target_kind"] == "entity" and parent["target_id"] == key
                    and parent["relation"] in {"processed_in", "precedes"}):
                    pending.append(parent["source_id"])
        if records[item["target_id"]].get("physical_cell_id") not in owners:
            item.update(effective_status="needs_review", review_reason="process_subject_binding_changed")
    datasets = library.library_rows(project)
    verified = None
    if verify_dataset_id:
        verified = library.get_dataset(project, verify_dataset_id)
    links = library.validate_links(project)
    for item in links:
        record = records.get(item["record_id"])
        item["valid"] = bool(item["valid"] and record and evidence_valid(record["evidence_ids"]))
        item["raw_integrity"] = "verified_now" if verified and item["dataset_id"] == verified["dataset_id"] else "catalog_only"
    return {"schema_version": "traceability_snapshot_v1", "generated_at": now(), "entities": list(entities.values()), "edges": list(edges.values()),
            "records": list(records.values()), "datasets": datasets, "raw_links": links, "documents": list(documents.values()),
            "evidence": list(evidence.values()), "proposals": proposals}


def revisions(project, kind, key):
    if kind not in {"entities", "edges"}:
        raise ValueError("이력 종류를 확인하세요.")
    with connect(project) as db:
        return [dict(row) | {"payload": json.loads(row["payload"])} for row in db.execute(
            "SELECT * FROM revisions WHERE kind=? AND id=? ORDER BY revision", (kind, key))]


def export_json(project, destination):
    destination = Path(destination).resolve()
    if destination.suffix.lower() != ".json" or destination.is_relative_to(Path(project).resolve() / "knowledge/documents"):
        raise ValueError("별도 .json 내보내기 경로를 선택하세요.")
    if destination.exists() and json.loads(destination.read_text(encoding="utf-8")).get("schema_version") != "traceability_snapshot_v1":
        raise ValueError("다른 파일을 덮어쓸 수 없습니다.")
    result = snapshot(project)
    atomic_json(destination, result)
    return Path(destination)
