"""Integrated Parquet catalogue, reviewed metadata, rules and document links.

The catalogue is independent of measurements. Filename values are hints, never
confirmed experiment facts. No LLM, network client, or measurement rewrite.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import fnmatch
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import uuid

from .knowledge_registry import METADATA_FIELDS, NUMERIC_METADATA_FIELDS, TEXT_FIELDS, DATE_KINDS

UNIT_FIELDS = {"voltage_unit", "capacity_unit"}
FIELDS = (set(TEXT_FIELDS) | set(METADATA_FIELDS) | UNIT_FIELDS | {"cohort", "include", "notes"}) - {"status"}
TEST_TYPES = {"Unknown", "RPT", "Cycle", "Insitu", "DCIR", "HPPC", "Formation", "GITT", "Storage"}
RULE_FIELDS = FIELDS - {"sample_id", "sample_name", "physical_cell_id", "test_date", "test_end_date",
                        "manufacture_date", "report_date", "channel_id", "set_no"}


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def identity(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


_HASHES = {}


def file_stamp(path):
    info = Path(path).stat()
    return [info.st_size, info.st_mtime_ns]


def file_hash(path):
    # Memoised per process by (path, size, mtime_ns): repeated verification of
    # the same unchanged multi-GB library no longer re-reads every byte.
    path = Path(path)
    key = (str(path.resolve()), *file_stamp(path))
    if key in _HASHES:
        return _HASHES[key]
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(data)
    if list(key[1:]) == file_stamp(path):
        _HASHES[key] = result.hexdigest()
    return result.hexdigest()


def source_unchanged(row, *, strict=False):
    """True when the registered Parquet still matches its catalogued SHA-256.

    Fast path: the size/mtime stamp recorded when the hash was computed still
    matches (``strict=True`` forces a full re-hash).
    """
    try:
        stamp = file_stamp(row["path"])
    except OSError:
        return False
    if not strict and row.get("stamp") == stamp:
        return True
    return file_hash(row["path"]) == row["sha256"]


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def filename_hints(path):
    text = str(path).replace("\\", "/")
    dates = set()
    for raw in re.findall(r"(?<!\d)(?:20\d{6}|\d{6})(?!\d)", text):
        try:
            parsed = datetime.strptime(raw, "%Y%m%d" if len(raw) == 8 else "%y%m%d").date()
            dates.add(parsed.isoformat())
        except ValueError:
            pass
    sets = sorted(set(re.findall(r"(?i)(?:set|세트)[ _-]*([0-9]+)", text)))
    channels = sorted(set(re.findall(r"(?i)M\d+Ch\d+\[\d+\]", text)))
    return {"date_candidates": sorted(dates), "set_candidates": sets, "channel_candidates": channels,
            "test_type_candidates": sorted({kind for kind in ("RPT", "Cycle", "HPPC", "DCIR")
                if re.search(r"(?i)(?:^|[^a-z])" + kind + r"(?:$|[^a-z])", text)}),
            "status": "unreviewed_filename_hints"}


def validate_metadata(values):
    if not isinstance(values, dict) or set(values) - FIELDS:
        raise ValueError("지원하지 않는 실험 정보: " + str(sorted(set(values) - FIELDS) if isinstance(values, dict) else values))
    clean = {}
    for key, value in values.items():
        if value in (None, ""):
            clean[key] = None
        elif key == "include":
            if type(value) is not bool:
                raise ValueError("include는 true/false여야 합니다.")
            clean[key] = value
        elif key in NUMERIC_METADATA_FIELDS:
            if isinstance(value, bool):
                raise ValueError(f"{key}: 숫자를 입력하세요.")
            number = float(value)
            if not math.isfinite(number) or key != "temperature_c" and number < 0:
                raise ValueError(f"{key}: 유효한 숫자를 입력하세요.")
            if "wt_pct" in key and number > 100:
                raise ValueError(f"{key}: 0~100 범위여야 합니다.")
            clean[key] = number
        else:
            if not isinstance(value, str):
                raise ValueError(f"{key}: 문자열을 입력하세요. 세트 번호의 선행 0은 유지합니다.")
            clean[key] = value.strip() or None
    if clean.get("test_type") is not None and clean["test_type"] not in TEST_TYPES:
        raise ValueError("시험 종류를 확인하세요.")
    if clean.get("date_kind") is not None and clean["date_kind"] not in DATE_KINDS:
        raise ValueError("날짜 의미를 확인하세요.")
    for key in ("test_date", "test_end_date", "manufacture_date", "report_date"):
        if clean.get(key):
            if date.fromisoformat(clean[key]).isoformat() != clean[key]:
                raise ValueError(f"{key}: YYYY-MM-DD를 입력하세요.")
    if clean.get("voltage_unit") not in {None, "V", "mV"} or clean.get("capacity_unit") not in {None, "Ah", "mAh", "uAh"}:
        raise ValueError("전압 단위 V/mV, 용량 단위 Ah/mAh/uAh를 확인하세요.")
    return clean


@contextmanager
def connect(project):
    project = Path(project).resolve()
    project.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(project / "library.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and "library_meta" not in tables:
            raise ValueError("이 SQLite 파일은 BatteryInsight Parquet 등록 DB가 아닙니다.")
        if "library_meta" in tables:
            version = db.execute("SELECT value FROM library_meta WHERE key='schema_version'").fetchone()
            if not version or version[0] != "1":
                raise ValueError("지원하지 않는 Parquet 등록 DB 버전입니다.")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript('''CREATE TABLE IF NOT EXISTS library_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS roots(root_id TEXT PRIMARY KEY,path TEXT UNIQUE NOT NULL);
            CREATE TABLE IF NOT EXISTS files(dataset_id TEXT PRIMARY KEY,root_id TEXT NOT NULL REFERENCES roots(root_id),
                relative_path TEXT NOT NULL,sha256 TEXT NOT NULL,payload TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,
                UNIQUE(root_id,relative_path));
            CREATE TABLE IF NOT EXISTS assignments(dataset_id TEXT PRIMARY KEY REFERENCES files(dataset_id),
                source_sha256 TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS library_events(id INTEGER PRIMARY KEY,at TEXT NOT NULL,action TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS library_links(dataset_id TEXT PRIMARY KEY REFERENCES files(dataset_id),
                source_sha256 TEXT NOT NULL,record_id TEXT NOT NULL,record_hash TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS classification_rules(rule_id TEXT PRIMARY KEY,payload TEXT NOT NULL);''')
        version = db.execute("SELECT value FROM library_meta WHERE key='schema_version'").fetchone()
        if version and version[0] != "1":
            raise ValueError("지원하지 않는 Parquet 등록 DB 버전입니다.")
        if not version:
            db.execute("INSERT INTO library_meta VALUES('schema_version','1')")
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def event(db, action, payload):
    db.execute("INSERT INTO library_events(at,action,payload) VALUES(?,?,?)", (now(), action, canonical(payload)))


@functools.lru_cache(maxsize=1024)
def _resolved(path):
    # realpath on the NAS costs ~9 ms per call; roots are few and stable.
    return Path(path).resolve()


def project_path(project, path):
    root, path = _resolved(str(project)), Path(path).resolve()
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)


def resolve_path(project, stored):
    return _resolved(str(_resolved(str(project)) / stored))


def parquet_info(path):
    import pyarrow.parquet as pq
    file = pq.ParquetFile(path)
    names = set(file.schema_arrow.names)
    if {"step_time_s", "voltage_native", "capacity_native"} <= names:
        kind = "native_cyc"
    elif {"time_s", "voltage_v", "capacity_ah"} <= names:
        kind = "normalized_curve"
    else:
        raise ValueError("시간·용량·전압 곡선 Parquet이 아닙니다.")
    stats = {}
    for name in ("cycle", "step_time_s", "time_s", "total_time_s", "voltage_native", "capacity_native", "voltage_v", "capacity_ah"):
        if name not in names:
            continue
        index = file.schema_arrow.get_field_index(name)
        bounds = []
        for group in range(file.metadata.num_row_groups):
            value = file.metadata.row_group(group).column(index).statistics
            if value and value.has_min_max:
                bounds.append((value.min, value.max))
        if bounds:
            numbers = [float(value) for pair in bounds for value in pair if math.isfinite(float(value))]
            if numbers:
                stats[name] = [min(numbers), max(numbers)]
    metadata = file.schema_arrow.metadata or {}
    source = {}
    for key in (b"source_relative_path", b"source_sha256", b"source_cyc", b"source_cts"):
        if key in metadata:
            source[key.decode()] = metadata[key].decode("utf-8", errors="replace")
    return {"format": kind, "rows": file.metadata.num_rows, "columns": file.schema_arrow.names,
            "stats": stats, "source": source, "bytes": Path(path).stat().st_size}


def scan_folder(project, folder, *, progress=None):
    project, folder = Path(project).resolve(), Path(folder).resolve()
    if not folder.is_dir() or folder == project or folder in project.parents:
        raise ValueError("Parquet 데이터 폴더를 선택하세요. 프로젝트 전체를 입력으로 선택할 수 없습니다.")
    root_path = project_path(project, folder)
    paths = sorted(path for path in folder.rglob("*.parquet") if path.is_file() and not path.is_symlink())
    results, seen = [], set()
    with connect(project) as db:
        found = db.execute("SELECT root_id FROM roots WHERE path=?", (root_path,)).fetchone()
        root_id = found[0] if found else identity(root_path)[:20]
        db.execute("INSERT OR IGNORE INTO roots VALUES(?,?)", (root_id, root_path))
        for index, path in enumerate(paths, 1):
            relative = path.relative_to(folder).as_posix()
            dataset = identity([root_id, relative])[:24]
            try:
                stamp = file_stamp(path)
                old = db.execute("SELECT sha256,payload FROM files WHERE dataset_id=?", (dataset,)).fetchone()
                previous = json.loads(old["payload"]) if old else {}
                # Unchanged size+mtime since the last verified hash: reuse it
                # instead of re-reading the whole file on every rescan.
                sha = old["sha256"] if old and previous.get("stamp") == stamp else file_hash(path)
                payload = previous if old and old["sha256"] == sha else parquet_info(path)
                if stamp != file_stamp(path):
                    raise ValueError("등록 중 파일이 변경됐습니다.")
                payload.update(dataset_id=dataset, relative_path=relative, root_id=root_id, stamp=stamp,
                               hints=filename_hints(payload.get("source", {}).get("source_relative_path", relative)))
                db.execute("INSERT INTO files VALUES(?,?,?,?,?,1) ON CONFLICT(dataset_id) DO UPDATE SET sha256=excluded.sha256,payload=excluded.payload,active=1",
                           (dataset, root_id, relative, sha, canonical(payload)))
                seen.add(dataset)
                results.append({"dataset_id": dataset, "path": relative, "status": "complete", "reused": bool(old and old["sha256"] == sha), "rows": payload["rows"]})
            except Exception as exc:
                # Invalid/unsupported Parquet stays visible as a scan issue. It
                # never replaces the last valid catalogue entry for that path.
                results.append({"path": relative, "status": "failed", "error": str(exc)})
            if progress:
                progress({"index": index, "total": len(paths), "message": relative, "status": results[-1]["status"]})
        existing = {row[0] for row in db.execute("SELECT dataset_id FROM files WHERE root_id=?", (root_id,))}
        for dataset in existing - seen:
            db.execute("UPDATE files SET active=0 WHERE dataset_id=?", (dataset,))
        event(db, "scan", {"root_id": root_id, "files": len(results)})
    export_library(project)
    return {"status": "success" if all(item["status"] == "complete" for item in results) else "partial_failure",
            "root_id": root_id, "files": results, "summary": {"registered": len(seen), "failed": sum(item["status"] == "failed" for item in results)}}


def library_rows(project, *, include_inactive=False, dataset_id=None):
    rows = []
    with connect(project) as db:
        condition = " WHERE dataset_id=?" if dataset_id is not None else ""
        params = (dataset_id,) if dataset_id is not None else ()
        links = {item["dataset_id"]: dict(item) for item in db.execute("SELECT * FROM library_links" + condition, params)}
        condition = " WHERE f.dataset_id=?" if dataset_id is not None else ""
        for item in db.execute("SELECT f.*,r.path root_path,a.source_sha256 assignment_sha,a.payload assignment FROM files f JOIN roots r USING(root_id) LEFT JOIN assignments a USING(dataset_id)" + condition + " ORDER BY r.path,f.relative_path", params):
            if not item["active"] and not include_inactive:
                continue
            payload = json.loads(item["payload"])
            assignment = json.loads(item["assignment"]) if item["assignment"] else {}
            current = bool(assignment and item["assignment_sha"] == item["sha256"])
            payload.update(sha256=item["sha256"], active=bool(item["active"]),
                path=str(resolve_path(project, item["root_path"]) / item["relative_path"]),
                metadata=assignment.get("metadata", {}), metadata_current=current,
                assignment_source_current=current,
                review_status="confirmed" if current else "needs_review" if assignment else "unclassified",
                assignment=assignment)
            rows.append(payload)
    if links:
        from .knowledge_registry import KnowledgeRegistry
        path = Path(project) / "knowledge/knowledge.sqlite3"
        records = {}
        if path.exists():
            with KnowledgeRegistry(path, read_only=True) as registry:
                records = {link["record_id"]: registry.get_record(link["record_id"]) for link in links.values()}
        for row in rows:
            link = links.get(row["dataset_id"])
            if link:
                record = records.get(link["record_id"])
                valid = bool(link["source_sha256"] == row["sha256"] and record and record["status"] == "confirmed" and record["content_hash"] == link["record_hash"])
                row["document_link_current"] = valid
                if not valid:
                    row.update(metadata_current=False, review_status="needs_review", review_reason="document_record_or_source_changed")
    return rows


def get_dataset(project, dataset_id, *, verify=True):
    rows = library_rows(project, dataset_id=dataset_id)
    if len(rows) != 1:
        raise ValueError("활성 데이터 항목을 찾을 수 없습니다.")
    row = rows[0]
    if verify and not source_unchanged(row, strict=verify == "strict"):
        raise ValueError("Parquet 파일이 변경됐습니다. 먼저 폴더를 다시 등록하세요.")
    return row


def _store_assignment(db, row, clean, reviewer, reason):
    dataset_id = row["dataset_id"]
    merged = {**(row["metadata"] if row["metadata_current"] else {}), **clean}
    payload = {"metadata": merged, "reviewer": reviewer, "reason": reason, "at": now()}
    changed = any(key in row["metadata"] and row["metadata"][key] != value for key, value in clean.items())
    if changed:
        old_link = db.execute("SELECT record_id FROM library_links WHERE dataset_id=?", (dataset_id,)).fetchone()
        if old_link:
            db.execute("DELETE FROM library_links WHERE dataset_id=?", (dataset_id,))
            event(db, "metadata_edit_detached_document_link", {"dataset_id": dataset_id, "previous_record_id": old_link[0]})
    db.execute("INSERT OR REPLACE INTO assignments VALUES(?,?,?)", (dataset_id, row["sha256"], canonical(payload)))
    event(db, "assign_metadata", {"dataset_id": dataset_id, "sha256": row["sha256"], **payload})
    return payload


def assign_metadata(project, dataset_id, metadata, *, reviewer="local_user", reason="직접 확인", expected_sha256=None):
    clean = validate_metadata(metadata)
    if not str(reviewer).strip() or not str(reason).strip():
        raise ValueError("확인자와 변경 이유가 필요합니다.")
    row = get_dataset(project, dataset_id)
    if expected_sha256 and expected_sha256 != row["sha256"]:
        raise ValueError("등록 후 원본이 바뀌었습니다. 내용을 다시 확인하세요.")
    with connect(project) as db:
        payload = _store_assignment(db, row, clean, reviewer, reason)
    export_library(project)
    return payload


def export_library(project):
    import pandas as pd
    rows = library_rows(project, include_inactive=True)
    flat = [{"dataset_id": row["dataset_id"], "relative_path": row["relative_path"], "path": row["path"],
             "sha256": row["sha256"], "rows": row["rows"], "format": row["format"], "active": row["active"],
             "review_status": row["review_status"], "hints_json": canonical(row["hints"]), **row["metadata"]} for row in rows]
    frame = pd.DataFrame(flat) if flat else pd.DataFrame({"dataset_id": pd.Series(dtype=str)})
    path = Path(project) / "library.parquet"
    temp = path.with_name("library." + uuid.uuid4().hex + ".tmp")
    frame.to_parquet(temp, index=False, compression="zstd", compression_level=19)
    temp.replace(path)
    return path


def export_registration(project, path):
    import pandas as pd
    rows = library_rows(project)
    fields = ["dataset_id", "source_sha256", "confirmed", "test_date", "date_kind", "namespace", "set_no", "sample_name", "physical_cell_id", "test_type", "rpt_stage", "design_id", "batch_id", "voltage_unit", "capacity_unit"]
    records = [{"dataset_id": row["dataset_id"], "source_sha256": row["sha256"], "confirmed": row["metadata_current"], **row["metadata"]} for row in rows]
    frame = pd.DataFrame(records).reindex(columns=list(dict.fromkeys(fields + [key for record in records for key in record])))
    path = Path(path).resolve()
    protected = {Path(row["path"]).resolve() for row in rows} | {Path(project).resolve() / "library.sqlite3", Path(project).resolve() / "library.parquet"}
    if path in protected:
        raise ValueError("등록표는 원본/DB와 다른 파일에 저장하세요.")
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")
    return path


def import_registration(project, path, *, reviewer="local_user"):
    import pandas as pd
    path = Path(path)
    frame = pd.read_excel(path, dtype=str, keep_default_na=False) if path.suffix.lower() in {".xlsx", ".xlsm"} else pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    if not {"dataset_id", "source_sha256", "confirmed"} <= set(frame) or frame.dataset_id.duplicated().any():
        raise ValueError("dataset_id/source_sha256/confirmed 열과 중복 없는 등록표가 필요합니다.")
    pending, checked = [], []
    for item in frame.to_dict("records"):
        if str(item["confirmed"]).strip().lower() not in {"true", "1", "yes", "확인"}:
            pending.append(item["dataset_id"])
            continue
        metadata = {key: value for key, value in item.items() if key in FIELDS and value != ""}
        if "include" in metadata:
            metadata["include"] = str(metadata["include"]).lower() in {"true", "1"}
        clean = validate_metadata(metadata)
        row = get_dataset(project, item["dataset_id"])
        if item["source_sha256"] != row["sha256"]:
            raise ValueError("등록표 원본 해시가 현재 파일과 다릅니다: " + item["dataset_id"])
        checked.append((row, clean))
    # Validate the complete sheet before committing any assignment.
    with connect(project) as db:
        for row, clean in checked:
            if not source_unchanged(row):
                raise ValueError("등록표 적용 중 원본이 바뀌었습니다.")
            _store_assignment(db, row, clean, reviewer, "확인된 실험 등록표 가져오기")
    export_library(project)
    return {"confirmed": len(checked), "pending": pending}


def set_rules(project, rules, *, reviewer="local_user"):
    if not isinstance(rules, list):
        raise ValueError("rules는 규칙 목록이어야 합니다.")
    checked = []
    for rule in rules:
        if not isinstance(rule, dict) or not rule.get("rule_id") or not rule.get("path_glob") or not isinstance(rule["path_glob"], str):
            raise ValueError("규칙에는 rule_id/path_glob/metadata가 필요합니다.")
        clean = validate_metadata(rule.get("metadata", {}))
        if set(clean) - RULE_FIELDS:
            raise ValueError("셀·샘플·날짜 등 개별 식별 정보는 폴더 규칙으로 일괄 복제할 수 없습니다.")
        checked.append({"rule_id": str(rule["rule_id"]), "path_glob": rule["path_glob"], "metadata": clean, "reviewer": reviewer})
    if len({rule["rule_id"] for rule in checked}) != len(checked):
        raise ValueError("규칙 ID가 중복됩니다.")
    with connect(project) as db:
        for rule in checked:
            db.execute("INSERT OR REPLACE INTO classification_rules VALUES(?,?)", (rule["rule_id"], canonical(rule)))
        event(db, "set_rules", checked)
    return checked


def apply_rules(project):
    with connect(project) as db:
        rules = [json.loads(row[0]) for row in db.execute("SELECT payload FROM classification_rules")]
    applied, conflicts, changes = [], [], []
    for row in library_rows(project):
        candidates = [rule for rule in rules if fnmatch.fnmatchcase(row["relative_path"], rule["path_glob"])]
        if candidates and row.get("document_link_current") is False:
            conflicts.append({"dataset_id": row["dataset_id"], "fields": ["stale_document_link"]})
            continue
        updates, collision = {}, []
        for rule in candidates:
            for key, value in rule["metadata"].items():
                if key in updates and updates[key] != value or row["metadata_current"] and key in row["metadata"] and row["metadata"][key] not in (None, value):
                    collision.append(key)
                updates[key] = value
        if collision:
            conflicts.append({"dataset_id": row["dataset_id"], "fields": sorted(set(collision))})
        elif updates and (not row["metadata_current"] or any(row["metadata"].get(key) != value for key, value in updates.items())):
            changes.append((row, updates, "규칙: " + ",".join(rule["rule_id"] for rule in candidates)))
            applied.append(row["dataset_id"])
    if changes:
        with connect(project) as db:
            for row, updates, reason in changes:
                if not source_unchanged(row):
                    raise ValueError("규칙 적용 중 원본이 바뀌었습니다.")
                _store_assignment(db, row, updates, "approved_rules", reason)
        export_library(project)
    return {"applied": applied, "conflicts": conflicts}


def import_documents(project, documents):
    from .document_intake import extract_document
    from .knowledge_registry import KnowledgeRegistry
    from .knowledge_report import write_review, write_templates
    root = Path(project) / "knowledge"
    root.mkdir(parents=True, exist_ok=True)
    write_templates(root)
    results = []
    imported_ids = []
    with KnowledgeRegistry(root / "knowledge.sqlite3") as registry:
        for path in documents:
            extraction = extract_document(path)
            if extraction.get("sha256"):
                if extraction.get("status") != "failed":
                    from .document_assets import save_document_copy
                    save_document_copy(project, path, extraction)
                registry.import_document(extraction)
                imported_ids.append(extraction["document_id"])
            results.append({"path": str(path), "status": extraction.get("status"), "issues": extraction.get("issues", [])})
        write_review(registry.snapshot(), root / "review")
    if imported_ids:
        from .slide_candidates import extract_proposals
        extract_proposals(project, document_ids=set(imported_ids))
    return results


def knowledge_candidates(project, dataset_id):
    from .knowledge_registry import KnowledgeRegistry
    row = get_dataset(project, dataset_id)
    keys = row["metadata"] if row["metadata_current"] else {}
    required = ("test_date", "namespace", "set_no", "test_type")
    path = Path(project) / "knowledge/knowledge.sqlite3"
    if not path.exists() or any(not keys.get(key) for key in required) or keys.get("date_kind") != "test_start":
        return []
    with KnowledgeRegistry(path, read_only=True) as registry:
        records = registry.list_records("confirmed")
    return [record for record in records if record.get("matching_eligible") and all(record.get(key) == keys[key] for key in required)
            and all(not keys.get(key) or not record.get(key) or keys[key] == record[key] for key in ("channel_id", "sample_name"))]


def link_record(project, dataset_id, record_id, *, reviewer="local_user", reason="장표 기록과 직접 연결"):
    from .knowledge_registry import KnowledgeRegistry
    row = get_dataset(project, dataset_id)
    with KnowledgeRegistry(Path(project) / "knowledge/knowledge.sqlite3", read_only=True) as registry:
        record = registry.get_record(record_id)
    if not record or record["status"] != "confirmed":
        raise ValueError("근거가 확인된 장표 실험 기록만 연결할 수 있습니다.")
    metadata = {**record.get("metadata", {}), **{key: record[key] for key in TEXT_FIELDS if record.get(key) is not None}}
    if row.get("assignment_source_current"):
        metadata.update({key: row["metadata"][key] for key in UNIT_FIELDS if key in row["metadata"]})
    # A human may resolve a candidate explicitly, but conflicting current keys
    # are not silently replaced by a document record.
    if row["metadata_current"]:
        clashes = [key for key, value in metadata.items() if key in row["metadata"] and row["metadata"][key] not in (None, value)]
        if clashes:
            raise ValueError("등록 정보와 장표 값이 다릅니다. 먼저 검토하세요: " + ", ".join(clashes))
    assign_metadata(project, dataset_id, metadata, reviewer=reviewer, reason=reason)
    payload = {"reviewer": reviewer, "reason": reason, "at": now()}
    with connect(project) as db:
        db.execute("INSERT OR REPLACE INTO library_links VALUES(?,?,?,?,?)", (dataset_id, row["sha256"], record_id, record["content_hash"], canonical(payload)))
        event(db, "link_record", {"dataset_id": dataset_id, "record_id": record_id, "record_hash": record["content_hash"], **payload})
    return record


def validate_links(project):
    from .knowledge_registry import KnowledgeRegistry
    with connect(project) as db:
        links = [dict(row) for row in db.execute("SELECT * FROM library_links")]
    path = Path(project) / "knowledge/knowledge.sqlite3"
    records = {}
    if path.exists():
        with KnowledgeRegistry(path, read_only=True) as registry:
            records = {row["record_id"]: row for row in registry.list_records()}
    sources = {row["dataset_id"]: row for row in library_rows(project)}
    return [{**link, "valid": bool(link["dataset_id"] in sources and sources[link["dataset_id"]]["sha256"] == link["source_sha256"]
             and link["record_id"] in records and records[link["record_id"]]["status"] == "confirmed"
             and records[link["record_id"]]["content_hash"] == link["record_hash"])} for link in links]


def unlink_record(project, dataset_id, *, reviewer="local_user", reason="연결 해제 후 실험 정보 재확인"):
    get_dataset(project, dataset_id)
    with connect(project) as db:
        db.execute("DELETE FROM library_links WHERE dataset_id=?", (dataset_id,))
        db.execute("UPDATE assignments SET source_sha256='' WHERE dataset_id=?", (dataset_id,))
        event(db, "unlink_record", {"dataset_id": dataset_id, "reviewer": reviewer, "reason": reason})
    export_library(project)
    return {"dataset_id": dataset_id, "status": "needs_review"}


def conversion_record_roots(project):
    """Folders whose conversion_manifest.json files count as existing conversion records."""
    project = Path(project).resolve()
    return [path for path in (project / "sources", project / "converted") if path.is_dir()]


def _registered_roots(project):
    with connect(project) as db:
        return [resolve_path(project, row[0]) for row in db.execute("SELECT path FROM roots")]


def convert_and_register(project, source, *, progress=None, reuse="stamp", reconvert_legacy=False):
    """Convert one CYC folder; files already recorded in this project are not converted again."""
    from cyc2parquet.service import convert_folder
    project, source = Path(project).resolve(), Path(source).resolve()
    target = project / "converted" / (source.name + "_" + identity(str(source))[:10])
    converted = convert_folder(source, target, progress=progress, known=conversion_record_roots(project),
                               reuse=reuse, reconvert_legacy=reconvert_legacy)
    registered = scan_folder(project, target / "parquet", progress=progress) if (target / "parquet").is_dir() else {"summary": {"registered": 0}}
    # Existing records normally live under an already registered root (sources/, converted/).
    roots = _registered_roots(project)
    folders = {str(Path(item["existing_manifest"]).parent / "parquet") for item in converted["files"] if item["status"] == "existing"}
    extra = sorted(folder for folder in folders
                   if not any(_resolved(folder).is_relative_to(root) for root in roots))
    extra_scans = [scan_folder(project, folder, progress=progress) for folder in extra if Path(folder).is_dir()]
    stored = project / "native_sources.json"
    known = json.loads(stored.read_text(encoding="utf-8")) if stored.exists() else []
    atomic_json(stored, sorted(set(known + [str(source)])))
    return {"conversion": converted, "library": registered, "existing_registered": len(extra_scans),
            "output": str(target), "status": converted["status"]}


def diagnose_conversions(project, *, extra_roots=(), out=None, probe_sources=False):
    """Diagnose every conversion record in the project (failures, CTS gaps, stale failures)."""
    from cyc2parquet.diagnose import diagnose_manifests, write_report
    project = Path(project).resolve()
    report = diagnose_manifests(conversion_record_roots(project) + [Path(root) for root in extra_roots], probe_sources=probe_sources)
    report["outputs"] = write_report(report, Path(out) if out else project / "reports" / "conversion_diagnosis")
    return {"status": "success", "summary": report["summary"], "recommendations": report["recommendations"],
            "outputs": report["outputs"]}


def refresh_project(project, *, progress=None, convert_sources=True):
    project = Path(project).resolve()
    source_file = project / "native_sources.json"
    conversions = []
    if convert_sources and source_file.exists():
        for source in json.loads(source_file.read_text(encoding="utf-8")):
            if Path(source).is_dir():
                conversions.append(convert_and_register(project, source, progress=progress))
    with connect(project) as db:
        roots = [row[0] for row in db.execute("SELECT path FROM roots")]
    scans = [scan_folder(project, resolve_path(project, root), progress=progress) for root in roots if resolve_path(project, root).is_dir()]
    return {"conversions": len(conversions), "scanned_roots": len(scans), "classification": apply_rules(project)}


def import_feature_export(project, dataset_id, spec_path):
    """Import a reviewed single-test export through the existing metric adapter."""
    from .adapters import import_external_features
    import pandas as pd
    row = get_dataset(project, dataset_id)
    spec_path = Path(spec_path).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    if not all(spec.get(key) for key in ("path", "mapping", "expected_cell_id", "expected_test_id")):
        raise ValueError("외부 지표 설정에는 path/mapping/expected_cell_id/expected_test_id가 필요합니다.")
    frame = import_external_features((spec_path.parent / spec["path"]).resolve(), spec["mapping"])
    if frame.empty or not {"cell_id", "test_id", "metric_id", "cycle", "value", "unit", "observed_until_cycle"} <= set(frame):
        raise ValueError("연결 가능한 외부 지표 행이 없습니다.")
    if not frame.cell_id.astype(str).eq(str(spec["expected_cell_id"])).all() or not frame.test_id.astype(str).eq(str(spec["expected_test_id"])).all():
        raise ValueError("외부 지표가 지정한 셀·시험 이외의 행을 포함합니다.")
    if frame.duplicated(["metric_id", "cycle"]).any():
        raise ValueError("외부 지표의 metric_id/cycle이 중복됩니다.")
    frame["dataset_id"] = dataset_id
    frame["dataset_sha256"] = row["sha256"]
    root = Path(project) / "external_metrics"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (dataset_id + ".parquet")
    temporary = path.with_suffix(".tmp")
    frame.to_parquet(temporary, index=False, compression="zstd", compression_level=19)
    temporary.replace(path)
    atomic_json(path.with_suffix(".json"), {"sha256": file_hash(path), "dataset_sha256": row["sha256"], "spec_sha256": file_hash(spec_path), "at": now()})
    with connect(project) as db:
        event(db, "import_feature_export", {"dataset_id": dataset_id, "rows": len(frame), "path": project_path(project, path)})
    return {"rows": len(frame), "metrics": sorted(frame.metric_id.unique().tolist())}
