"""Reviewable links between document records and immutable raw experiments.

Filename hints are deliberately excluded from matching.  A link is only a
proposal until a reviewer confirms a specific record revision and source hash.
The original manifest, registration CSV, raw files and derived tables are never
modified by this module.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import io
import json
import re
from contextlib import nullcontext
from datetime import date, datetime, timezone
from pathlib import Path

from .knowledge_registry import METADATA_FIELDS


SOURCE_FIELDS = ("experiment_id", "source_id", "source_sha256", "test_type",
                 "source_channel", "source_container")
RAW_KEY_FIELDS = ("test_date", "date_kind", "namespace", "set_no", "sample_name", "channel_id")
# These are comparison metadata, never source identities or calculated metrics.
LINK_METADATA_FIELDS = METADATA_FIELDS


def _text(value):
    return "" if value is None else str(value).strip()


def _known(value):
    return _text(value).lower() not in {"", "unknown", "unverified", "needs_review", "?", "none", "null", "nan"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _read_json(value):
    return json.loads(Path(value).read_text(encoding="utf-8-sig")) if isinstance(value, (str, Path)) else copy.deepcopy(value)


def _database(database):
    root = Path(database).resolve()
    path = root / "database_manifest.json"
    encoded = path.read_bytes()
    manifest = json.loads(encoded.decode("utf-8-sig"))
    if manifest.get("schema_version") != "raw_database_v1":
        raise ValueError("Unsupported raw database manifest schema")
    entries = manifest.get("experiments")
    if not isinstance(entries, list) or any(not isinstance(row, dict) or not row.get("experiment_id") for row in entries):
        raise ValueError("Database manifest requires experiment identities")
    by_id = {row["experiment_id"]: row for row in entries}
    if len(by_id) != len(entries):
        raise ValueError("Duplicate raw experiment identity")
    for row in entries:
        if not row.get("source_id") or not re.fullmatch(r"[0-9a-fA-F]{64}", _text(row.get("source_sha256"))):
            raise ValueError("Raw experiment requires source_id and source_sha256")
    registry = root / "experiments.csv"
    csv_bytes = registry.read_bytes()
    registered = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    if any(not row.get("experiment_id") for row in registered):
        raise ValueError("experiments.csv requires experiment_id")
    registrations = {row["experiment_id"]: row for row in registered}
    if len(registrations) != len(registered) or set(registrations) - set(by_id):
        raise ValueError("experiments.csv contains duplicate or unknown experiment IDs")
    # Manifest source values are authoritative; the existing adapter also supports
    # stale source hashes in a CSV left over after a successful raw re-import.
    for identity, row in registrations.items():
        for field in ("source_id", "test_type", "source_container", "source_channel"):
            if field in row and _text(row[field]) != _text(by_id[identity].get(field)):
                raise ValueError(f"experiments.csv cannot change immutable {field}: {identity}")
    if encoded != path.read_bytes() or csv_bytes != registry.read_bytes():
        raise ValueError("Raw registry changed while reading; retry")
    return root, by_id, registrations, {
        "database_path": str(root),
        "database_manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "experiments_csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
    }


def _binding(row):
    return {field: copy.deepcopy(row.get(field, "")) for field in SOURCE_FIELDS}


def _check_binding(expected, current):
    return all(_text(expected.get(field)) == _text(current.get(field)) for field in SOURCE_FIELDS)


def _hints(row):
    name = _text(row.get("source_container")).replace("\\", "/").rsplit("/", 1)[-1]
    dates = []
    for token in re.findall(r"(?<!\d)(?:20\d{6}|\d{6})(?!\d)", name):
        expanded = token if len(token) == 8 else "20" + token
        try:
            candidate = date(int(expanded[:4]), int(expanded[4:6]), int(expanded[6:])).isoformat()
        except ValueError:
            continue
        if candidate not in dates:
            dates.append(candidate)
    sets = list(dict.fromkeys(re.findall(r"(?:set|세트)\s*[-_#]?\s*(\d+(?:-\d+)?)", name, flags=re.I)))
    return {"candidate_dates": dates, "candidate_set_numbers": sets,
            "hint_status": "unconfirmed_filename_hints_only"}


def raw_inventory(database):
    """Return exact manifest identities and separately labelled filename hints."""
    _, rows, _, source = _database(database)
    return {"schema_version": "raw_inventory_v1", "generated_at": _now(), **source,
            "rows": [{**_binding(row), "rpt_stage": row.get("rpt_stage", ""),
                      **_hints(row)} for row in rows.values()]}


def write_raw_key_template(database, path):
    """Write a separate, deliberately unfilled key-confirmation CSV."""
    destination = Path(path).resolve()
    root = Path(database).resolve()
    if destination.is_relative_to(root):
        raise ValueError("Write the raw key template outside the immutable raw database")
    inventory = raw_inventory(database)
    for row in inventory["rows"]:
        source = Path(row["source_container"]).resolve()
        if destination == source or destination.is_relative_to(source):
            raise ValueError("Raw key template cannot overwrite a raw source")
    fields = [*SOURCE_FIELDS, "rpt_stage", *RAW_KEY_FIELDS,
              "candidate_dates", "candidate_set_numbers", "hint_status"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for row in inventory["rows"]:
        item = {**row, **dict.fromkeys(RAW_KEY_FIELDS, "")}
        for key in ("candidate_dates", "candidate_set_numbers"):
            item[key] = json.dumps(item[key], ensure_ascii=False)
        writer.writerow(item)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(output.getvalue(), encoding="utf-8-sig", newline="")
    return inventory


def _keys(raw_keys, by_id):
    if raw_keys is None:
        entries = []
    elif isinstance(raw_keys, (str, Path)):
        path = Path(raw_keys)
        if path.suffix.lower() == ".csv":
            entries = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
        else:
            entries = _read_json(path)
    else:
        entries = copy.deepcopy(raw_keys)
    if isinstance(entries, dict):
        entries = entries.get("rows", entries)
        if isinstance(entries, dict):
            entries = [{"experiment_id": key, **value} for key, value in entries.items()]
    if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
        raise ValueError("Raw keys must be a CSV, a row list or an experiment_id mapping")
    result = {}
    for row in entries:
        identity = row.get("experiment_id")
        if identity not in by_id or identity in result:
            raise ValueError("Raw keys contain duplicate or unknown experiment_id")
        original = by_id[identity]
        for field in SOURCE_FIELDS:
            if field in row and _text(row[field]) != _text(original.get(field)):
                raise ValueError(f"Raw keys cannot change immutable {field}: {identity}")
        if row.get("set_no") is not None and not isinstance(row.get("set_no"), str):
            raise ValueError("set_no must be text so leading zeros are preserved")
        if _known(row.get("rpt_stage")) and _known(original.get("rpt_stage")) and _text(row["rpt_stage"]) != _text(original["rpt_stage"]):
            raise ValueError("Raw key RPT stage conflicts with database manifest")
        if _known(row.get("channel_id")) and _known(original.get("source_channel")) and _text(row["channel_id"]) != _text(original["source_channel"]):
            raise ValueError("Raw key channel_id conflicts with database source_channel")
        result[identity] = {field: _text(row.get(field)) for field in RAW_KEY_FIELDS}
        result[identity]["test_type"] = _text(original.get("test_type"))
        result[identity]["rpt_stage"] = _text(original.get("rpt_stage")) or _text(row.get("rpt_stage"))
        result[identity]["channel_id"] = result[identity]["channel_id"] or _text(original.get("source_channel"))
    return result


def _required_keys(keys):
    missing = [field for field in ("test_date", "namespace", "set_no", "test_type") if not _known(keys.get(field))]
    if keys.get("date_kind") != "test_start":
        missing.append("date_kind=test_start")
    if _known(keys.get("test_date")):
        try:
            parsed = date.fromisoformat(keys["test_date"])
            if parsed.isoformat() != keys["test_date"]:
                raise ValueError()
        except (ValueError, TypeError):
            missing.append("test_date=YYYY-MM-DD")
    return missing


def _eligible(record):
    return (record.get("status") == "confirmed" and record.get("matching_eligible") is True
            and not _required_keys(record) and isinstance(record.get("set_no"), str))


def _match(keys, record):
    """Return no base match, a conflict list, or an exact candidate match."""
    if any(_text(keys.get(field)) != _text(record.get(field)) for field in ("test_date", "namespace", "set_no")):
        return None
    conflicts = []
    if _text(keys.get("test_type")).casefold() != _text(record.get("test_type")).casefold():
        conflicts.append("test_type")
    if _known(keys.get("rpt_stage")) and _known(record.get("rpt_stage")) and _text(keys["rpt_stage"]) != _text(record["rpt_stage"]):
        conflicts.append("rpt_stage")
    if _known(record.get("channel_id")) and _text(keys.get("channel_id")) != _text(record["channel_id"]):
        conflicts.append("channel_id")
    aliases = record.get("sample_aliases") or []
    if not isinstance(aliases, list):
        aliases = []
    names = {_text(record.get("sample_name")), *(_text(alias) for alias in aliases)}
    if _known(keys.get("sample_name")) and _text(keys["sample_name"]) not in names:
        conflicts.append("sample_name")
    return conflicts


def propose_links(database, records, raw_keys=None):
    """Propose matches; even a unique candidate requires an explicit review."""
    _, rows, _, source = _database(database)
    if hasattr(records, "list_records"):
        records = records.list_records()
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError("records must be a list of knowledge registry records")
    identities = [record.get("record_id") for record in records]
    if any(not identity for identity in identities) or len(set(identities)) != len(identities):
        raise ValueError("Knowledge records require unique record_id values")
    key_source = None
    if isinstance(raw_keys, (str, Path)):
        key_path = Path(raw_keys).resolve()
        key_source = {"path": str(key_path), "sha256": hashlib.sha256(key_path.read_bytes()).hexdigest()}
    keys_by_id = _keys(raw_keys, rows)
    if key_source and hashlib.sha256(Path(key_source["path"]).read_bytes()).hexdigest() != key_source["sha256"]:
        raise ValueError("Raw key source changed while reading; retry")
    eligible = [record for record in records if _eligible(record)]
    result = []
    for identity, raw in rows.items():
        keys = keys_by_id.get(identity, {})
        missing = _required_keys(keys)
        item = {**_binding(raw), "raw_keys": keys, "candidates": [], "conflicts": [], "missing_keys": missing}
        if missing:
            item["status"] = "missing_keys"
        else:
            for record in eligible:
                conflicts = _match(keys, record)
                if conflicts is None:
                    continue
                if conflicts:
                    item["conflicts"].append({"record_id": record["record_id"], "fields": conflicts})
                    continue
                item["candidates"].append({"record_id": record["record_id"], "expected_record_revision": record["revision"],
                                           "expected_record_content_hash": record["content_hash"],
                                           "sample_id": record.get("sample_id", ""), "sample_name": record.get("sample_name", ""),
                                           "experiment_name": record.get("experiment_name", "")})
            count = len(item["candidates"])
            item["status"] = "unique_candidate" if count == 1 else "ambiguous" if count > 1 else "conflict" if item["conflicts"] else "unmatched"
        result.append(item)
    return {"schema_version": "raw_link_proposals_v1", "generated_at": _now(), **source,
            "raw_keys_sha256": _digest(keys_by_id), "records_snapshot_sha256": _digest(records),
            "raw_keys_source": key_source, "automatic_links_applied": 0, "rows": result}


def _registry_context(registry):
    if isinstance(registry, (str, Path)):
        path = Path(registry).resolve()
        if not path.is_file():
            raise ValueError("Knowledge registry does not exist; refusing to create it while linking")
        from .knowledge_registry import KnowledgeRegistry
        return KnowledgeRegistry(path, read_only=True)
    if not hasattr(registry, "get_record") or not hasattr(registry, "list_records"):
        raise ValueError("registry must be an existing KnowledgeRegistry or its path")
    return nullcontext(registry)


def _metadata(record):
    result = {key: copy.deepcopy(value) for key, value in (record.get("metadata") or {}).items()
              if key in LINK_METADATA_FIELDS and _known(value)}
    for key in ("design_id", "batch_id", "physical_cell_id"):
        if _known(record.get(key)):
            result[key] = record[key]
    if _known(record.get("sample_name")):
        result["label"] = record["sample_name"]
    return result


def _same_value(left, right):
    if _text(left) == _text(right):
        return True
    if isinstance(right, (int, float)) and not isinstance(right, bool):
        try:
            return float(left) == right
        except (TypeError, ValueError):
            pass
    return False


def _metadata_conflicts(row, original, metadata):
    conflicts = []
    for key, value in metadata.items():
        old = row.get(key)
        if key == "label" and _text(old) in {_text(original.get("label")), _text(original.get("experiment_id"))}:
            continue
        if _known(old) and not _same_value(old, value):
            conflicts.append(key)
    return conflicts


def review_links(database, registry, proposals, decisions, reviewer, reason, existing_overlay=None):
    """Return a new overlay after validating every explicit review decision.

    Confirm/reject decisions bind ``expected_record_revision`` and
    ``source_sha256``.  ``clear`` unlinks an experiment without deleting history.
    Existing nonempty CSV metadata is never silently overwritten.
    """
    if not _text(reviewer) or not _text(reason):
        raise ValueError("Link review requires reviewer and reason")
    root, rows, registrations, source = _database(database)
    proposals = _read_json(proposals)
    if proposals.get("schema_version") != "raw_link_proposals_v1" or Path(proposals.get("database_path", "")).resolve() != root:
        raise ValueError("Proposal does not belong to this raw database")
    proposed_rows = proposals.get("rows", [])
    by_id = {row.get("experiment_id"): row for row in proposed_rows}
    if len(by_id) != len(proposed_rows):
        raise ValueError("Proposal has duplicate experiment IDs")
    raw_keys_snapshot = {identity: row["raw_keys"] for identity, row in by_id.items() if row.get("raw_keys")}
    if _digest(raw_keys_snapshot) != proposals.get("raw_keys_sha256"):
        raise ValueError("Proposal raw keys have changed; regenerate proposals")
    key_source = proposals.get("raw_keys_source")
    if key_source and (not Path(key_source["path"]).is_file()
                       or hashlib.sha256(Path(key_source["path"]).read_bytes()).hexdigest() != key_source["sha256"]):
        raise ValueError("Raw key source changed after proposal; regenerate proposals")
    decisions = _read_json(decisions)
    if isinstance(decisions, dict):
        decisions = decisions.get("decisions")
    if not isinstance(decisions, list) or any(not isinstance(row, dict) for row in decisions):
        raise ValueError("decisions must be a row list")
    if len({row.get("experiment_id") for row in decisions}) != len(decisions):
        raise ValueError("Review batch contains duplicate experiment IDs")
    overlay = _read_json(existing_overlay) if existing_overlay else {
        "schema_version": "raw_knowledge_links_v1", "links": [], "history": []}
    if overlay.get("schema_version") != "raw_knowledge_links_v1":
        raise ValueError("Unsupported knowledge overlay schema")
    if overlay.get("database_path") and Path(overlay["database_path"]).resolve() != root:
        raise ValueError("Existing overlay belongs to another raw database")
    links = {link["experiment_id"]: link for link in overlay.get("links", [])}
    if len(links) != len(overlay.get("links", [])):
        raise ValueError("Overlay contains duplicate experiment links")
    history = copy.deepcopy(overlay.get("history", []))
    with _registry_context(registry) as current:
        registry_path = str(Path(current.path).resolve())
        if overlay.get("registry_path") and Path(overlay["registry_path"]).resolve() != Path(registry_path):
            raise ValueError("Existing overlay belongs to another knowledge registry")
        for decision in decisions:
            identity = decision.get("experiment_id")
            proposed = by_id.get(identity)
            raw = rows.get(identity)
            action = decision.get("decision")
            if action not in {"confirm", "reject", "clear"}:
                raise ValueError("Link decision must be confirm, reject or clear")
            if raw is None or proposed is None or not _check_binding(proposed, raw):
                raise ValueError("Proposal raw source identity is stale or invalid")
            if decision.get("source_sha256") != raw["source_sha256"]:
                raise ValueError("Decision source_sha256 is required and must match current raw source")
            audit = {"experiment_id": identity, "record_id": decision.get("record_id"), "decision": action,
                     "reviewer": _text(reviewer), "reason": _text(decision.get("reason")) or _text(reason),
                     "reviewed_at": _now(), "source_sha256": raw["source_sha256"]}
            if action == "clear":
                links.pop(identity, None)
                history.append(audit)
                continue
            candidates = [item for item in proposed.get("candidates", []) if item.get("record_id") == decision.get("record_id")]
            if len(candidates) != 1:
                raise ValueError("Decision must identify exactly one proposed candidate")
            candidate = candidates[0]
            record = current.get_record(candidate["record_id"])
            if not record or not _eligible(record):
                raise ValueError("Knowledge record is missing or no longer confirmed and matchable")
            if (decision.get("expected_record_revision") != record["revision"]
                    or type(decision.get("expected_record_revision")) is not int
                    or candidate["expected_record_revision"] != record["revision"]
                    or candidate["expected_record_content_hash"] != record["content_hash"]):
                raise ValueError("Knowledge record revision is stale; regenerate proposals")
            keys = proposed.get("raw_keys", {})
            if _required_keys(keys) or _match(keys, record) != []:
                raise ValueError("Proposal keys do not match the current knowledge record")
            # Validate channel/type/stage again against the current manifest.
            validated = _keys([{**keys, "experiment_id": identity}], rows)[identity]
            if validated != keys:
                raise ValueError("Proposal raw keys changed; regenerate proposals")
            audit.update(expected_record_revision=record["revision"], record_content_hash=record["content_hash"])
            if action == "reject":
                if links.get(identity, {}).get("record_id") == record["record_id"]:
                    links.pop(identity)
            else:
                metadata = _metadata(record)
                existing = {**raw, **registrations.get(identity, {})}
                conflicts = _metadata_conflicts(existing, raw, metadata)
                if conflicts:
                    raise ValueError(f"Existing raw registration metadata conflicts with knowledge record: {identity}: {', '.join(conflicts)}")
                links[identity] = {**_binding(raw), "record_id": record["record_id"], "record_revision": record["revision"],
                                   "record_content_hash": record["content_hash"], "record_snapshot": copy.deepcopy(record),
                                   "raw_keys": copy.deepcopy(keys), "metadata": metadata, "review": audit}
            history.append(audit)
        registry_snapshot_hash = _digest(current.list_records())
    # This is an audit snapshot, not a global invalidation key. Appending a new
    # experiment or unrelated registry record does not invalidate existing links.
    overlay.update(source)
    overlay.update(registry_path=registry_path, registry_snapshot_sha256=registry_snapshot_hash,
                   generated_at=_now(), links=list(links.values()), history=history)
    return overlay


def apply_confirmed_links(rows, overlay, database_path, knowledge_registry_path=None):
    """Apply valid reviewed comparison metadata to copies; return rows and issues.

    Source identities remain unchanged.  A physical-cell alias is only returned
    as ``physical_cell_id``; the database adapter owns its comparison-cell view.
    Stale/revoked/conflicting links are skipped with an explicit issue.
    """
    root, originals, _, _ = _database(database_path)
    overlay = _read_json(overlay)
    if overlay.get("schema_version") != "raw_knowledge_links_v1" or Path(overlay.get("database_path", "")).resolve() != root:
        raise ValueError("Knowledge overlay does not belong to this raw database")
    links = overlay.get("links", [])
    if not isinstance(links, list) or any(not isinstance(item, dict) for item in links):
        raise ValueError("Knowledge overlay links must be a list")
    by_id = {link.get("experiment_id"): link for link in links}
    if len(by_id) != len(links):
        raise ValueError("Knowledge overlay contains duplicate raw experiment links")
    registry_path = knowledge_registry_path or overlay.get("registry_path")
    if not registry_path:
        raise ValueError("Knowledge overlay requires an existing registry path")
    if knowledge_registry_path and Path(knowledge_registry_path).resolve() != Path(overlay.get("registry_path", "")).resolve():
        raise ValueError("Knowledge registry path differs from reviewed overlay")
    result, issues = [], []
    with _registry_context(registry_path) as current:
        evidence_by_id = {item["evidence_id"]: item for item in current.list_evidence()}
        for item in rows:
            row = copy.deepcopy(item)
            identity = row.get("experiment_id")
            link = by_id.get(identity)
            if not link:
                result.append(row)
                continue
            issue = None
            original = originals.get(identity)
            record = None
            if original is None or not _check_binding(link, original) or not _check_binding(row, original):
                issue = "knowledge_link_stale_source"
            else:
                try:
                    record = current.get_record(link.get("record_id"))
                except (KeyError, ValueError):
                    pass
                if (not record or not _eligible(record) or record.get("revision") != link.get("record_revision")
                        or record.get("content_hash") != link.get("record_content_hash")
                        or record != link.get("record_snapshot")):
                    issue = "knowledge_link_stale_record"
            if issue is None:
                review = link.get("review", {})
                if (review.get("decision") != "confirm" or not _text(review.get("reviewer"))
                        or not _text(review.get("reason")) or not review.get("reviewed_at")
                        or review.get("experiment_id") != identity or review.get("record_id") != record["record_id"]
                        or review.get("source_sha256") != original["source_sha256"]
                        or review.get("expected_record_revision") != record["revision"]
                        or review.get("record_content_hash") != record["content_hash"]):
                    issue = "knowledge_link_missing_review"
                elif _required_keys(link.get("raw_keys", {})) or _match(link["raw_keys"], record) != []:
                    issue = "knowledge_link_key_conflict"
                else:
                    try:
                        keys = _keys([{**link["raw_keys"], "experiment_id": identity}], originals)[identity]
                        if keys != link["raw_keys"]:
                            issue = "knowledge_link_key_conflict"
                    except ValueError:
                        issue = "knowledge_link_key_conflict"
            if issue is None:
                metadata = _metadata(record)
                if metadata != link.get("metadata"):
                    issue = "knowledge_link_metadata_changed"
                elif _metadata_conflicts(row, original, metadata):
                    issue = "knowledge_link_existing_metadata_conflict"
            if issue:
                issues.append({"experiment_id": identity, "record_id": link.get("record_id"), "reason": issue})
            else:
                row.update(copy.deepcopy(metadata))
                row["knowledge_link"] = {"record_id": record["record_id"], "revision": record["revision"],
                                         "content_hash": record["content_hash"], "sample_id": record.get("sample_id", ""),
                                         "sample_name": record.get("sample_name", ""), "experiment_name": record.get("experiment_name", ""),
                                         **{key: record.get(key) for key in ("test_date", "date_kind", "namespace", "set_no", "test_type", "rpt_stage", "channel_id")},
                                         "evidence_ids": copy.deepcopy(record.get("evidence_ids", [])),
                                         "field_evidence": copy.deepcopy(record.get("field_evidence", {})),
                                         "evidence_locations": [{"evidence_id": evidence_id,
                                                                  "document_id": evidence_by_id[evidence_id]["document_id"],
                                                                  "locator": evidence_by_id[evidence_id]["locator"]}
                                                                 for evidence_id in record.get("evidence_ids", []) if evidence_id in evidence_by_id],
                                         "reviewed_by": link["review"]["reviewer"], "reviewed_at": link["review"]["reviewed_at"]}
            result.append(row)
    for identity in set(by_id) - set(originals):
        issues.append({"experiment_id": identity, "record_id": by_id[identity].get("record_id"), "reason": "knowledge_link_missing_experiment"})
    return result, issues
