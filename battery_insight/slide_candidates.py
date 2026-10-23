"""Drafts from explicit PPT table headers and key:value text.

No OCR, fuzzy sample merge, filename dates, or automatic confirmation. Each
proposed value cites its header and value cell. Unparsed values stay visible.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path
import json
import math
import re

from .library import canonical, identity
from . import traceability as trace
from .knowledge_registry import KnowledgeRegistry


def label(value):
    return re.sub(r"[\s_]", "", value.casefold()).strip()


ALIASES = {}
def aliases(field, *names):
    for name in (field, *names):
        ALIASES[label(name)] = field


for field, names in {
    "sample_id": ("샘플ID", "시료ID"), "sample_name": ("샘플명", "시료명"),
    "physical_cell_id": ("물리셀ID", "셀ID"), "experiment_name": ("시험명", "실험명"),
    "test_date": ("시험시작일", "평가시작일"), "manufacture_date": ("제조일",), "report_date": ("보고일",),
    "set_no": ("세트", "세트번호", "Set"), "namespace": ("프로젝트", "번호사용범위"),
    "test_type": ("시험종류", "평가종류"), "rpt_stage": ("RPT시점",), "channel_id": ("채널", "채널ID"),
    "design_id": ("설계ID",), "batch_id": ("제조배치", "배치ID"),
    "kind": ("항목종류",), "name": ("항목명", "명칭"), "process_id": ("공정ID",),
    "process_type": ("공정종류", "공정명"), "performed_on": ("공정일",),
    "material_id": ("소재로트ID",), "electrode_id": ("전극로트ID", "전해질막로트ID",),
    "lot_id": ("로트번호", "LotID"), "supplier": ("공급사",), "material": ("소재명",),
    "electrode_side": ("전극극성",), "fabrication_method": ("습식건식", "제조방식"),
    "current_collector": ("집전체",), "equipment_id": ("설비ID",), "operator": ("작업자",),
    "composition_basis": ("조성기준",), "input_ids": ("투입ID",), "output_ids": ("산출ID",),
}.items():
    aliases(field, *names)

# Units are declared by the header, never inferred from a plausible magnitude.
NUMERIC = {
    "구동압(MPa)": ("pressure_mpa", "MPa", "record"), "온도(°C)": ("temperature_c", "°C", "record"),
    "압착압(MPa)": ("pressing_pressure", "MPa", "entity"), "건조온도(°C)": ("drying_temperature", "°C", "entity"),
    "건조시간(h)": ("drying_time", "h", "entity"), "배합시간(min)": ("mixing_time", "min", "entity"),
    "배합속도(rpm)": ("mixing_speed", "rpm", "entity"), "로딩(mg/cm²)": ("mass_loading", "mg/cm²", "entity"),
    "로딩(mg/cm2)": ("mass_loading", "mg/cm²", "entity"), "활물질비율(%)": ("active_fraction", "%", "entity"),
    "막두께(μm)": ("film_thickness", "μm", "entity"), "NP비": ("np_ratio", "1", "entity"),
}
NUMERIC = {label(key): value for key, value in NUMERIC.items()}
KIND_NAMES = {**{key: key for key in trace.KINDS}, **{value: key for key, value in trace.KINDS.items()}}
ATTRIBUTES = {"sample_id", "physical_cell_id", "design_id", "batch_id", "lot_id", "supplier", "material",
              "electrode_side", "fabrication_method", "current_collector", "equipment_id", "operator", "composition_basis"}


def _recognized(text):
    return label(text) in ALIASES or label(text) in NUMERIC


def _proposal(document, entries, location):
    values, refs, numeric, warnings, raw_values, unknown = {}, {}, [], [], [], []
    conflicts = set()
    for header, text, evidence_ids in entries:
        key = label(header)
        raw_values.append({"header": header, "value": text, "evidence_ids": evidence_ids})
        if key in NUMERIC:
            field, unit, destination = NUMERIC[key]
            match = re.fullmatch(r"\s*([-+]?\d+(?:\.\d+)?)\s*(.*?)\s*", text)
            allowed_units = {"", unit, "mg/cm2" if unit == "mg/cm²" else unit}
            if not match or not math.isfinite(float(match[1])) or match[2] not in allowed_units:
                warnings.append(header + ": 수치·단위 직접 확인 필요 (원문 보존)")
            else:
                numeric.append((field, float(match[1]), unit, destination, evidence_ids))
            continue
        field = ALIASES.get(key)
        if not field:
            unknown.append(header)
            continue
        if field in values and values[field] != text.strip():
            conflicts.add(field)
            warnings.append(field + ": 서로 다른 값이 있어 자동 입력하지 않음")
        values[field] = text.strip()
        refs.setdefault(field, []).extend(evidence_ids)
    for field in conflicts:
        values.pop(field, None)
    if unknown:
        warnings.append("자동 입력하지 않은 열·항목: " + ", ".join(sorted(set(unknown))) + " (원문에서 직접 확인)")
    numeric_groups = defaultdict(list)
    for item in numeric:
        numeric_groups[(item[0], item[3])].append(item)
    numeric = []
    for (field, destination), items in numeric_groups.items():
        if len({(item[1], item[2]) for item in items}) > 1:
            warnings.append(field + ": 서로 다른 수치가 있어 자동 입력하지 않음")
        else:
            first = items[0]
            numeric.append((*first[:4], sorted({key for item in items for key in item[4]})))
    if not values and not numeric:
        return None
    record = not KIND_NAMES.get(values.get("kind")) and (any(values.get(key) for key in ("experiment_name", "test_type", "set_no"))
        or any(values.get(key) for key in ("sample_id", "sample_name")) and not any(values.get(key) for key in ("process_id", "process_type", "material_id", "electrode_id", "physical_cell_id")))
    evidence_ids = sorted({key for entry in entries for key in entry[2]})
    proposal_id = "prop_" + identity(["ppt_explicit_v1", document["sha256"], location, raw_values])[:24]
    if record:
        payload = {"record_id": "rec_" + proposal_id[5:], "status": "draft", "date_kind": "unknown",
                   "metadata": {}, "evidence_ids": evidence_ids, "field_evidence": {}}
        from .knowledge_registry import TEXT_FIELDS
        for field in TEXT_FIELDS:
            if not values.get(field):
                continue
            value = values[field]
            if field.endswith("_date"):
                try:
                    if date.fromisoformat(value).isoformat() != value:
                        raise ValueError
                except ValueError:
                    warnings.append(field + ": YYYY-MM-DD와 날짜 의미 확인 필요")
                    continue
            payload[field] = value
            payload["field_evidence"][field] = sorted(set(refs[field]))
        if payload.get("test_date"):
            payload["date_kind"] = "test_start"
            payload["field_evidence"]["date_kind"] = payload["field_evidence"]["test_date"]
        for field, value, unit, destination, ids in numeric:
            if destination == "record":
                payload["metadata"][field] = value
                payload["field_evidence"][field] = sorted(set(ids))
            else:
                warnings.append(field + ": 공정·전극 값입니다. 원문을 확인해 별도 이력에 등록하세요.")
        target = "record"
        title = values.get("experiment_name") or values.get("sample_name") or values.get("sample_id") or "시험 정보 확인"
    else:
        kind = KIND_NAMES.get(values.get("kind"))
        if not kind:
            kind = ("process_run" if values.get("process_id") or values.get("process_type") else
                    "electrode_lot" if values.get("electrode_id") else "cell" if values.get("physical_cell_id") else "material_lot")
            warnings.append("항목 종류는 명시된 식별자 기준의 후보입니다. 등록 전에 확인하세요.")
        attributes = {key: value for key, value in values.items() if key in ATTRIBUTES and value}
        if values.get("process_id"):
            attributes["process_run_id"] = values["process_id"]
            refs["process_run_id"] = refs["process_id"]
        for key in ("material_id", "electrode_id"):
            if values.get(key) and not attributes.get("lot_id"):
                attributes["lot_id"] = values[key]
                refs["lot_id"] = refs[key]
        title = (values.get("name") or values.get("process_type") or values.get("material") or
                 values.get("physical_cell_id") or attributes.get("lot_id") or "공정 정보 확인")
        payload = {"entity_id": "ent_" + proposal_id[5:], "kind": kind, "name": title, "status": "draft",
                   "attributes": attributes, "parameters": {}, "process_type": values.get("process_type", ""),
                   "performed_on": values.get("performed_on") or values.get("manufacture_date", ""),
                   "evidence_ids": evidence_ids, "field_evidence": {}}
        for field, source in (("name", "name"), ("process_type", "process_type"), ("performed_on", "performed_on" if values.get("performed_on") else "manufacture_date")):
            if payload.get(field):
                payload["field_evidence"][field] = sorted(set(refs.get(source, evidence_ids)))
        if payload["performed_on"]:
            try:
                if date.fromisoformat(payload["performed_on"]).isoformat() != payload["performed_on"]:
                    raise ValueError
            except ValueError:
                warnings.append("공정·제조일의 연도와 날짜 형식 확인 필요")
                payload["performed_on"] = ""
        for key in attributes:
            payload["field_evidence"]["attributes." + key] = sorted(set(refs.get(key, evidence_ids)))
        for field, value, unit, destination, ids in numeric:
            if destination == "entity":
                payload["parameters"][field] = {"value": value, "unit": unit}
                payload["field_evidence"]["parameters." + field] = sorted(set(ids))
            else:
                warnings.append(field + ": 시험 조건입니다. 원문을 확인해 시험 기록에 등록하세요.")
        target = "entity"
    suggested = {key: values[key] for key in ("input_ids", "output_ids") if values.get(key)}
    if suggested:
        warnings.append("투입·산출 ID는 원문 후보입니다. 실제 항목을 선택하여 연결을 따로 확인하세요.")
    return {"proposal_id": proposal_id, "document_id": document["document_id"], "location": location,
            "title": title, "target": target, "payload": payload, "raw_values": raw_values, "warnings": warnings,
            "suggested_relations": suggested, "policy": "draft_only_explicit_headers_no_automatic_links"}


def extract_proposals(project, document_ids=None):
    snap = trace.knowledge(project)
    documents = {item["document_id"]: item for item in snap["documents"] if item.get("format") == "pptx" and (document_ids is None or item["document_id"] in document_ids)}
    tables, candidates = defaultdict(dict), []
    for block in snap["evidence"]:
        doc = documents.get(block["document_id"])
        if not doc:
            continue
        match = re.fullmatch(r"(.+)/table:r(\d+):c(\d+)", block["locator"])
        if match:
            tables[(doc["document_id"], match[1])][(int(match[2]), int(match[3]))] = block
        elif block["kind"] in {"text", "speaker_notes"}:
            entries = []
            for line in block["text"].splitlines():
                parts = re.fullmatch(r"\s*[•*\-]?\s*([^:=：]{1,70})\s*[:=：]\s*(.+)", line)
                if parts and _recognized(parts[1]):
                    entries.append((parts[1], parts[2], [block["evidence_id"]]))
            if len(entries) >= 2:
                candidates.append(_proposal(doc, entries, block["locator"]))
    for (document_id, location), cells in tables.items():
        rows = sorted({row for row, col in cells})
        first = rows[0]
        headers = {col: block for (row, col), block in cells.items() if row == first and _recognized(block["text"])}
        if len(headers) >= 2:
            all_headers = {col: block for (row, col), block in cells.items() if row == first}
            for row in rows[1:]:
                entries = [(header["text"], cells[(row, col)]["text"], [header["evidence_id"], cells[(row, col)]["evidence_id"]])
                           for col, header in all_headers.items() if (row, col) in cells]
                if entries:
                    candidates.append(_proposal(documents[document_id], entries, location + "/row:" + str(row)))
        else:
            entries = []
            for row in rows:
                if (row, 1) in cells and (row, 2) in cells and _recognized(cells[(row, 1)]["text"]):
                    entries.append((cells[(row, 1)]["text"], cells[(row, 2)]["text"], [cells[(row, col)]["evidence_id"] for col in (1, 2)]))
            if len(entries) >= 2:
                candidates.append(_proposal(documents[document_id], entries, location))
    candidates = [item for item in candidates if item]
    with trace.connect(project) as db:
        for item in candidates:
            db.execute("INSERT OR IGNORE INTO proposals(id,payload) VALUES(?,?)", (item["proposal_id"], canonical(item)))
        pending = db.execute("SELECT COUNT(*) FROM proposals WHERE state='pending'").fetchone()[0]
    return {"candidates": len(candidates), "pending": pending, "policy": "검토 대기만 생성; 실험/공정 확정 및 raw 연결 없음"}


def review_proposal(project, proposal_id, payload=None, *, action="accepted", reviewer="local_user", reason="장표 항목 확인"):
    if action not in {"accepted", "dismissed"}:
        raise ValueError("후보 검토 상태를 확인하세요.")
    with trace.connect(project) as db:
        row = db.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not row:
        raise ValueError("후보를 찾을 수 없습니다.")
    proposal = json.loads(row["payload"])
    if action == "accepted":
        payload = payload or proposal["payload"]
        if proposal["target"] == "entity":
            saved = trace.save_bundle(project, {"entities": [payload]}, reviewer=reviewer, reason=reason)
        else:
            with KnowledgeRegistry(Path(project) / "knowledge/knowledge.sqlite3") as registry:
                saved = registry.upsert_records([payload], reviewer=reviewer, reason=reason)
    else:
        saved = None
    with trace.connect(project) as db:
        db.execute("UPDATE proposals SET state=? WHERE id=?", (action, proposal_id))
    return saved
