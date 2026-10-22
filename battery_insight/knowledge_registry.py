"""Reviewed experiment/sample identities with immutable document evidence.

This SQLite catalogue is independent of the raw-data database.  Document text is
untrusted evidence, never instructions; importing it creates no experiment or raw
link.  Only explicit record review can confirm a sample assignment.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Iterator
from uuid import uuid4


SCHEMA_VERSION = "knowledge_registry_v1"
DATE_KINDS = frozenset({"test_start", "test_end", "manufacture", "report", "unknown"})
STATUSES = frozenset({"draft", "confirmed"})
TEXT_FIELDS = (
    "sample_id", "sample_name", "experiment_name", "test_date", "date_kind",
    "manufacture_date", "report_date", "test_end_date",
    "set_no", "namespace", "test_type", "rpt_stage", "channel_id",
    "physical_cell_id", "design_id", "batch_id",
)
NUMERIC_METADATA_FIELDS = frozenset({
    "temperature_c", "pressure_mpa", "charge_rate_c", "discharge_rate_c",
    "voltage_min_v", "voltage_max_v", "cathode_areal_capacity_mAh_cm2",
    "anode_areal_capacity_mAh_cm2", "cathode_active_mass_loading_mg_cm2",
    "anode_active_mass_loading_mg_cm2", "cathode_pressed_density_g_cm3",
    "anode_pressed_density_g_cm3", "electrolyte_layer_areal_mass_mg_cm2",
    "electrolyte_layer_pressed_thickness_um", "anode_silicon_wt_pct_of_active",
    "anode_graphite_wt_pct_of_active", "anode_other_wt_pct_of_active",
    *(f"{side}_{component}_wt_pct" for side in ("cathode", "anode")
      for component in ("active", "solid_electrolyte", "conductive", "binder", "other")),
})
TEXT_METADATA_FIELDS = frozenset({
    "protocol", "chemistry", "formation_id", "design_group", "electrolyte_layer_material",
    "cathode_composition_basis", "anode_composition_basis",
    *(f"{side}_{component}" for side in ("cathode", "anode")
      for component in ("active_material", "solid_electrolyte", "binder", "conductive_additive")),
})
METADATA_FIELDS = NUMERIC_METADATA_FIELDS | TEXT_METADATA_FIELDS
_CONTROL_FIELDS = frozenset({"record_id", "status", "metadata", "evidence_ids", "field_evidence",
                             "sample_aliases", "notes", "revision", "content_hash", "matching_eligible",
                             "reviewer", "reason", "created_at", "updated_at"})


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: object, label: str, *, required: bool = False) -> str | None:
    if value is None:
        result = None
    elif not isinstance(value, str):
        raise ValueError(f"{label} must be a string; identifiers such as set_no must preserve leading zeros")
    else:
        result = value.strip() or None
    if required and result is None:
        raise ValueError(f"{label} is required")
    return result


def _strings(value: object, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list of strings")
    return sorted({_text(item, label, required=True) for item in value})


def _metadata(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("metadata must be an object")
    unknown = set(value) - METADATA_FIELDS
    if unknown:
        raise ValueError(f"Unknown or protected metadata fields: {sorted(unknown)}")
    result = {}
    for key, raw in value.items():
        if raw is None:
            result[key] = None
        elif key in TEXT_METADATA_FIELDS:
            result[key] = _text(raw, f"metadata.{key}")
        else:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw):
                raise ValueError(f"metadata.{key} must be a finite number in the named unit or null")
            if "wt_pct" in key and not 0 <= raw <= 100:
                raise ValueError(f"metadata.{key} must be within 0..100; no composition normalization is applied")
            if key != "temperature_c" and raw < 0:
                raise ValueError(f"metadata.{key} must be non-negative")
            result[key] = raw
    lower, upper = result.get("voltage_min_v"), result.get("voltage_max_v")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("voltage_min_v must not exceed voltage_max_v")
    for side in ("cathode", "anode"):
        key = f"{side}_composition_basis"
        if result.get(key) not in (None, "electrode_total", "active_material_total"):
            raise ValueError(f"metadata.{key} requires electrode_total or active_material_total")
    return result


class KnowledgeRegistry:
    """Separate local evidence catalogue; writes are atomic and revisioned.

    ``upsert_records`` accepts partial updates when ``record_id`` is supplied.
    Exported ``revision``/``content_hash`` are optional optimistic-lock guards.
    New records without an ID receive a UUID; retain the returned ID for future
    updates.  Names are never used as an identity or a fuzzy merge key.
    """

    def __init__(self, path: str | Path, *, read_only: bool = False):
        self.path = Path(path).resolve()
        self.read_only = read_only
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path.as_uri() + "?mode=ro" if read_only else self.path,
                                   timeout=30, isolation_level=None, uri=read_only)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=30000")
        try:
            tables = {row[0] for row in self._db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "registry_meta" in tables:
                version = self._db.execute("SELECT value FROM registry_meta WHERE key='schema_version'").fetchone()
                if not version or version[0] != SCHEMA_VERSION:
                    raise ValueError(f"Unsupported knowledge registry schema: {version[0] if version else None}")
            elif tables or read_only:
                raise ValueError("The selected file is not a knowledge registry; no schema changes were made")
        except BaseException:
            self._db.close()
            raise
        if read_only:
            return
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS registry_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL,
                payload_json TEXT NOT NULL, imported_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                locator TEXT NOT NULL, payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS samples (
                sample_id TEXT PRIMARY KEY, sample_name TEXT NOT NULL,
                aliases_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS experiment_records (
                record_id TEXT PRIMARY KEY, sample_id TEXT REFERENCES samples(sample_id),
                status TEXT NOT NULL CHECK(status IN ('draft','confirmed')),
                revision INTEGER NOT NULL, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS record_evidence (
                record_id TEXT NOT NULL REFERENCES experiment_records(record_id),
                evidence_id TEXT NOT NULL REFERENCES evidence(evidence_id),
                PRIMARY KEY(record_id,evidence_id));
            CREATE TABLE IF NOT EXISTS record_revisions (
                record_id TEXT NOT NULL REFERENCES experiment_records(record_id),
                revision INTEGER NOT NULL, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
                reviewer TEXT, reason TEXT, created_at TEXT NOT NULL,
                PRIMARY KEY(record_id,revision));
            CREATE TABLE IF NOT EXISTS audit_log (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                entity_id TEXT NOT NULL, reviewer TEXT, reason TEXT,
                payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS records_status_idx ON experiment_records(status);
            CREATE INDEX IF NOT EXISTS evidence_document_idx ON evidence(document_id);
        """)
        with self._transaction():
            self._db.execute("INSERT OR IGNORE INTO registry_meta VALUES ('schema_version',?)", (SCHEMA_VERSION,))
            self._db.execute("INSERT OR IGNORE INTO registry_meta VALUES ('revision','0')")
            version = self._db.execute("SELECT value FROM registry_meta WHERE key='schema_version'").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise ValueError(f"Unsupported knowledge registry schema: {version}")

    def __enter__(self) -> "KnowledgeRegistry":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _transaction(self, *, write: bool = True) -> Iterator[None]:
        if write and self.read_only:
            raise ValueError("This knowledge registry is read-only")
        self._db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    def _bump(self) -> None:
        self._db.execute("UPDATE registry_meta SET value=CAST(value AS INTEGER)+1 WHERE key='revision'")

    def _audit(self, action: str, entity_id: str, payload: dict,
               reviewer: str | None = None, reason: str | None = None) -> None:
        self._db.execute("INSERT INTO audit_log(action,entity_id,reviewer,reason,payload_json,created_at) VALUES (?,?,?,?,?,?)",
                         (action, entity_id, reviewer, reason, _json(payload), _now()))

    def import_document(self, extraction: dict) -> dict:
        """Store an extractor result without interpreting its text or creating links.

        Failed extraction is retained for the intake queue, with no evidence.
        Reimport of the same SHA is idempotent; changing its evidence is rejected.
        """
        if not isinstance(extraction, dict):
            raise ValueError("extraction must be an object")
        payload = json.loads(_json(extraction))
        sha = _text(payload.get("sha256"), "sha256", required=True)
        if re.fullmatch(r"[0-9a-fA-F]{64}", sha) is None:
            raise ValueError("sha256 must contain 64 hexadecimal characters")
        payload["sha256"] = sha = sha.lower()
        document_id = _text(payload.get("document_id"), "document_id") or "doc_" + sha[:24]
        payload["document_id"] = document_id
        status = payload.get("status", "partial")
        if status not in {"complete", "partial", "failed"}:
            raise ValueError("document status must be complete, partial or failed")
        payload["status"] = status
        blocks = payload.get("blocks", [])
        if not isinstance(blocks, list):
            raise ValueError("document blocks must be a list")
        if status == "failed" and blocks:
            raise ValueError("failed extraction must not supply usable evidence blocks")
        normalized, seen = [], set()
        for block in blocks:
            if not isinstance(block, dict):
                raise ValueError("each evidence block must be an object")
            item = dict(block)
            locator = _text(item.get("locator"), "evidence locator", required=True)
            if not isinstance(item.get("text", ""), str):
                raise ValueError("evidence text must be a string")
            item["locator"] = locator
            evidence_id = _text(item.get("block_id", item.get("evidence_id")), "block_id")
            evidence_id = evidence_id or "ev_" + _digest([sha, locator, item.get("text", "")])[:24]
            if evidence_id in seen:
                raise ValueError(f"Duplicate evidence block: {evidence_id}")
            seen.add(evidence_id)
            item["block_id"], item["needs_review"] = evidence_id, True
            normalized.append(item)
        payload["blocks"] = normalized
        with self._transaction():
            prior = self._db.execute("SELECT * FROM documents WHERE sha256=?", (sha,)).fetchone()
            if prior:
                old = json.loads(prior["payload_json"])
                if old["document_id"] != document_id or old.get("blocks", []) != normalized:
                    raise ValueError("Same document SHA has conflicting evidence; existing provenance is immutable")
                return old | {"imported_at": prior["imported_at"]}
            if self._db.execute("SELECT 1 FROM documents WHERE document_id=?", (document_id,)).fetchone():
                raise ValueError(f"Document ID already belongs to different content: {document_id}")
            imported_at = _now()
            self._db.execute("INSERT INTO documents VALUES (?,?,?,?)", (document_id, sha, _json(payload), imported_at))
            for block in normalized:
                if self._db.execute("SELECT 1 FROM evidence WHERE evidence_id=?", (block["block_id"],)).fetchone():
                    raise ValueError(f"Evidence ID already belongs to a different document: {block['block_id']}")
                self._db.execute("INSERT INTO evidence VALUES (?,?,?,?)",
                                 (block["block_id"], document_id, block["locator"], _json(block)))
            self._bump()
            self._audit("document_import", document_id, {"sha256": sha, "block_count": len(normalized), "status": status})
            return payload | {"imported_at": imported_at}

    def _normalize_record(self, supplied: dict, old: dict | None, reviewer: str | None, reason: str | None) -> dict:
        if not isinstance(supplied, dict):
            raise ValueError("each experiment record must be an object")
        unknown = set(supplied) - set(TEXT_FIELDS) - _CONTROL_FIELDS
        if unknown:
            raise ValueError(f"Unknown or protected record fields: {sorted(unknown)}")
        record = {key: None for key in TEXT_FIELDS}
        record.update({"date_kind": "unknown", "status": "draft", "metadata": {}, "evidence_ids": [],
                       "field_evidence": {}, "sample_aliases": [], "notes": None})
        if old:
            record.update({key: old[key] for key in record if key in old})
        record.update({key: val for key, val in supplied.items() if key in record})
        record["record_id"] = old["record_id"] if old else _text(supplied.get("record_id"), "record_id") or "rec_" + uuid4().hex
        for key in TEXT_FIELDS:
            record[key] = _text(record[key], key)
        record["date_kind"] = record["date_kind"] or "unknown"
        if record["date_kind"] not in DATE_KINDS:
            raise ValueError(f"Unknown date_kind: {record['date_kind']}")
        for date_field in ("test_date", "manufacture_date", "report_date", "test_end_date"):
            if not record[date_field]:
                continue
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", record[date_field]):
                raise ValueError(f"{date_field} requires unambiguous YYYY-MM-DD; document/report dates must not be inferred as test dates")
            try:
                date.fromisoformat(record[date_field])
            except ValueError as exc:
                raise ValueError(f"{date_field} is not a valid calendar date") from exc
        if record["status"] not in STATUSES:
            raise ValueError("record status must be draft or confirmed")
        record["notes"] = _text(record["notes"], "notes")
        record["metadata"] = _metadata(record["metadata"])
        record["evidence_ids"] = _strings(record["evidence_ids"], "evidence_ids")
        record["sample_aliases"] = _strings(record["sample_aliases"], "sample_aliases")
        if not isinstance(record["field_evidence"], dict):
            raise ValueError("field_evidence must be an object of field names to evidence ID lists")
        field_evidence = {}
        for field, ids in record["field_evidence"].items():
            if field not in set(TEXT_FIELDS) | METADATA_FIELDS | {"notes", "sample_aliases"}:
                raise ValueError(f"Unknown evidence field: {field}")
            field_evidence[field] = _strings(ids, f"field_evidence.{field}")
            if not set(field_evidence[field]) <= set(record["evidence_ids"]):
                raise ValueError(f"field_evidence.{field} must reference this record's evidence_ids")
        record["field_evidence"] = field_evidence
        for evidence_id in record["evidence_ids"]:
            if not self._db.execute("SELECT 1 FROM evidence WHERE evidence_id=?", (evidence_id,)).fetchone():
                raise ValueError(f"Unknown evidence_id: {evidence_id}")
        if record["status"] == "confirmed":
            for key in ("sample_id", "sample_name", "experiment_name"):
                _text(record[key], key, required=True)
            if not record["evidence_ids"]:
                raise ValueError("Confirmed records require document evidence_ids")
            _text(reviewer, "reviewer", required=True)
            _text(reason, "reason", required=True)
        # A confirmed record may still lack a genuine test date. Keep the known
        # sample facts but exclude it from automatic raw-key candidate matching.
        record["matching_eligible"] = bool(record["status"] == "confirmed" and record["date_kind"] == "test_start"
                                           and all(record[key] for key in ("test_date", "set_no", "namespace", "test_type")))
        return record

    def upsert_records(self, records: list[dict], reviewer: str | None = None, reason: str | None = None) -> list[dict]:
        """Atomically insert/revise explicitly reviewed records; never infer names.

        Reviewer/reason may be set per input row, overriding batch defaults.
        Complete no-op reimports do not create an extra revision or audit event.
        """
        if not isinstance(records, list):
            raise ValueError("records must be a list")
        result, seen = [], set()
        with self._transaction():
            for supplied in records:
                if not isinstance(supplied, dict):
                    raise ValueError("each experiment record must be an object")
                record_id = _text(supplied.get("record_id"), "record_id")
                old = self.get_record(record_id) if record_id else None
                if record_id and record_id in seen:
                    raise ValueError(f"Duplicate record_id in batch: {record_id}")
                if record_id:
                    seen.add(record_id)
                if "revision" in supplied and (old is None or supplied["revision"] != old["revision"]):
                    raise ValueError(f"Stale or nonexistent record revision: {record_id}")
                if "content_hash" in supplied and (old is None or supplied["content_hash"] != old["content_hash"]):
                    raise ValueError(f"Stale or nonexistent record content hash: {record_id}")
                row_reviewer = _text(supplied.get("reviewer", reviewer), "reviewer")
                row_reason = _text(supplied.get("reason", reason), "reason")
                record = self._normalize_record(supplied, old, row_reviewer, row_reason)
                record_id, now = record["record_id"], _now()
                content_hash = _digest(record)
                if old and content_hash == old["content_hash"]:
                    result.append(old)
                    continue
                sample_id, sample_name = record["sample_id"], record["sample_name"]
                if sample_id and sample_name:
                    sample = self._db.execute("SELECT * FROM samples WHERE sample_id=?", (sample_id,)).fetchone()
                    if sample:
                        if sample["sample_name"] != sample_name:
                            raise ValueError(f"sample_id {sample_id!r} already has canonical name {sample['sample_name']!r}; use explicit sample_aliases, or a different sample_id")
                        aliases = sorted(set(json.loads(sample["aliases_json"])) | set(record["sample_aliases"]))
                        self._db.execute("UPDATE samples SET aliases_json=? WHERE sample_id=?", (_json(aliases), sample_id))
                    else:
                        self._db.execute("INSERT INTO samples VALUES (?,?,?,?)", (sample_id, sample_name, _json(record["sample_aliases"]), now))
                elif sample_id:
                    # Drafts may hold an unresolved ID; don't create shared sample
                    # facts until a name is explicitly supplied.
                    sample_id = None
                version = old["revision"] + 1 if old else 1
                created_at = old["created_at"] if old else now
                self._db.execute("""INSERT INTO experiment_records VALUES (?,?,?,?,?,?,?,?)
                    ON CONFLICT(record_id) DO UPDATE SET sample_id=excluded.sample_id,status=excluded.status,
                        revision=excluded.revision,content_hash=excluded.content_hash,payload_json=excluded.payload_json,
                        updated_at=excluded.updated_at""",
                    (record_id, sample_id, record["status"], version, content_hash, _json(record), created_at, now))
                self._db.execute("DELETE FROM record_evidence WHERE record_id=?", (record_id,))
                self._db.executemany("INSERT INTO record_evidence VALUES (?,?)", [(record_id, eid) for eid in record["evidence_ids"]])
                self._db.execute("INSERT INTO record_revisions VALUES (?,?,?,?,?,?,?)",
                                 (record_id, version, content_hash, _json(record), row_reviewer, row_reason, now))
                self._bump()
                self._audit("record_review" if record["status"] == "confirmed" else "record_draft", record_id,
                            {"revision": version, "content_hash": content_hash, "previous_hash": old["content_hash"] if old else None}, row_reviewer, row_reason)
                result.append(record | {"revision": version, "content_hash": content_hash, "created_at": created_at, "updated_at": now})
        return result

    @staticmethod
    def _record(row: sqlite3.Row) -> dict:
        record = json.loads(row["payload_json"])
        if _digest(record) != row["content_hash"]:
            raise ValueError(f"Knowledge record content hash mismatch: {row['record_id']}")
        return record | {key: row[key] for key in ("revision", "content_hash", "created_at", "updated_at")}

    def get_record(self, record_id: str) -> dict | None:
        row = self._db.execute("SELECT * FROM experiment_records WHERE record_id=?", (record_id,)).fetchone()
        return self._record(row) if row else None

    def list_records(self, status: str | None = None) -> list[dict]:
        if status is not None and status not in STATUSES:
            raise ValueError("status must be draft or confirmed")
        rows = self._db.execute("SELECT * FROM experiment_records" + (" WHERE status=?" if status else "") + " ORDER BY record_id",
                                (status,) if status else ()).fetchall()
        return [self._record(row) for row in rows]

    def list_documents(self) -> list[dict]:
        return [json.loads(row["payload_json"]) | {"imported_at": row["imported_at"]}
                for row in self._db.execute("SELECT * FROM documents ORDER BY document_id")]

    def list_evidence(self, document_id: str | None = None) -> list[dict]:
        rows = self._db.execute("SELECT * FROM evidence" + (" WHERE document_id=?" if document_id else "") + " ORDER BY evidence_id",
                                (document_id,) if document_id else ()).fetchall()
        return [json.loads(row["payload_json"]) | {"evidence_id": row["evidence_id"], "document_id": row["document_id"]} for row in rows]

    def list_samples(self) -> list[dict]:
        return [{"sample_id": row["sample_id"], "sample_name": row["sample_name"], "sample_aliases": json.loads(row["aliases_json"]),
                 "created_at": row["created_at"]} for row in self._db.execute("SELECT * FROM samples ORDER BY sample_id")]

    def list_revisions(self, record_id: str) -> list[dict]:
        return [json.loads(row["payload_json"]) | {key: row[key] for key in ("revision", "content_hash", "reviewer", "reason", "created_at")}
                for row in self._db.execute("SELECT * FROM record_revisions WHERE record_id=? ORDER BY revision", (record_id,))]

    def audit_log(self) -> list[dict]:
        return [{key: row[key] for key in ("event_id", "action", "entity_id", "reviewer", "reason", "created_at")}
                | {"payload": json.loads(row["payload_json"])} for row in self._db.execute("SELECT * FROM audit_log ORDER BY event_id")]

    def snapshot(self) -> dict:
        """Return one consistent, reviewable catalogue snapshot including evidence."""
        with self._transaction(write=False):
            revision = int(self._db.execute("SELECT value FROM registry_meta WHERE key='revision'").fetchone()[0])
            return {"schema_version": SCHEMA_VERSION, "revision": revision, "documents": self.list_documents(),
                    "evidence": self.list_evidence(), "samples": self.list_samples(), "records": self.list_records()}
