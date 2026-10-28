"""Conservative, non-transitive proposals from the attached research DB snapshot.

Call ``attach_to_db(project, conn)`` after ``slide_link.attach_to_db`` and before
the caller's commit/export. ``build(project, conn)`` is read-only, including on
the project filesystem. No other battery_insight modules are imported.

Only an exact, unique reviewed Set reference on the same file/page can connect
tests. A common_set is NOT a common design or physical cell. Agent "confirmed"
links remain candidates; even named reviewers are not assumed human-confirmed.
Medium review confidence permits relationships, but only high permits attribute
proposals. Review decisions in the attached DB are authoritative; reattach
slides after editing the review CSV. Any no_slide/unsure decision vetoes a test.

Attributes deliberately support a small grammar: standalone temperature/operating
pressure clauses in review_conditions or the exact Set label, and numeric
PTFE/porosity/loading clauses in review_design or the Set label. Parsed attrs are retained but never
trusted: extract_sets can inherit page-wide conditions or collapse multiple rows.
Design proposals describe the observed Set, not every test assigned that design.
No values, owners, IDs or cells are merged or overwritten. Copied slides and deck
families share support groups; counts never increase confidence.
"""
from __future__ import annotations

import csv
import hashlib
import itertools
import json
import math
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

__all__ = ["build", "attach_to_db", "export_csv"]

_CONDITIONS = ("temperature_c", "pressure_mpa")
_DESIGN = ("ptfe_wt", "porosity_pct", "loading_mah_cm2")
_NUMBER = r"[-+]?\d+(?:\.\d+)?"
_SET = re.compile(r"^\[?\s*set\s*#?\s*(\d+)\s*\]?(.*)$", re.I)
_UNCERTAIN = re.compile(r"[?~≈<>]|추정|불명|미상|미확인|확인\s*필요|아님|없음|미표시|"
                        r"\b(?:unknown|uncertain|maybe|possibly|estimated|not|or|versus|vs)\b", re.I)
_TEMP = re.compile(
    rf"(?:(?:temperature_c|temperature|temp|온도)\s*[:=]\s*({_NUMBER})"
    rf"\s*(?:℃|°\s*C|도(?:씨)?)?|({_NUMBER})\s*(?:℃|°\s*C|도(?:씨)?))", re.I)
_PRESSURE = re.compile(
    rf"(?:(?:pressure|압력|구동압)\s*[:=]\s*({_NUMBER})\s*MPa|"
    rf"pressure_mpa\s*[:=]\s*({_NUMBER})\s*(?:MPa)?)", re.I)
_DESIGN_PATTERNS = {
    "ptfe_wt": re.compile(rf"(?:ptfe_wt\s*[:=]\s*|PTFE\s*[:=]?\s*)({_NUMBER})\s*(?:wt%|%)?", re.I),
    "porosity_pct": re.compile(rf"(?:(?:porosity_pct|porosity|공극률)\s*[:=]\s*|P)({_NUMBER})\s*%?", re.I),
    "loading_mah_cm2": re.compile(rf"(?:loading_mah_cm2|loading)\s*[:=]\s*({_NUMBER})\s*(?:mAh/cm[²2])?", re.I),
}


def _text(value):
    return value.strip() if isinstance(value, str) else ""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value):
    try:
        obj = json.loads(value or "{}")
        return obj if isinstance(obj, dict) else {}
    except (ValueError, TypeError):
        return {}


def _id(*values):
    return hashlib.sha256(_json(values).encode("utf-8")).hexdigest()[:24]


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (ValueError, TypeError):
        return None


def _integer(value):
    number = _number(value)
    return int(number) if number is not None and number.is_integer() else None


def _norm(value):
    # Deliberately retain punctuation, dates, sub-Set suffixes and author labels.
    return re.sub(r"\s+", "", _text(value)).casefold()


def _rows(conn, table):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
                        (table,)).fetchone():
        return []
    cur = conn.execute(f'SELECT * FROM "{table}"')
    columns = [col[0] for col in cur.description]
    return [dict(zip(columns, row)) for row in cur]


def _unique(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[row.get(key)].append(row)
    return {key: values[0] for key, values in groups.items() if key is not None and len(values) == 1}


def _matches(label, source):
    label = _text(label)
    parsed = _text(source.get("set_label"))
    match = _SET.fullmatch(label)
    if not match:
        return False
    if any(len(re.findall(r"\bset\s*#?\s*\d", text, re.I)) > 1 for text in (label, parsed)):
        return False
    if re.match(r"\s*(?:[-~–—/.,&+]|to\b)\s*(?:#?\d|set)", match[2], re.I):
        return False
    parsed_match = _SET.fullmatch(parsed)
    source_suffix = parsed_match[2] if parsed_match else parsed
    # extract_sets removes the leading Set number but retains range/list suffixes.
    if re.match(r"\s*(?:[-~–—/.,&+]|to\b)\s*(?:#?\d|set)", source_suffix, re.I):
        return False
    if _norm(label) == _norm(parsed):
        return source.get("set_no") is None or int(match[1]) == _integer(source.get("set_no"))
    if int(match[1]) != _integer(source.get("set_no")):
        return False
    suffix = match[2]
    if any(re.match(r"\s*[-/.,&+]\s*\d", s)
           for s in (suffix, parsed_match[2] if parsed_match else "")):
        return False  # Set1-1 is not Set1.
    suffix = suffix.lstrip(" :：.)_")
    if parsed_match:
        if int(parsed_match[1]) != int(match[1]):
            return False
        parsed = parsed_match[2].lstrip(" :：.)_")
    return not suffix or _norm(suffix) == _norm(parsed)


def _parse_conditions(text):
    values = defaultdict(set)
    warnings = set()
    for clause in re.split(r"[;\n]", _text(text)):
        clause = clause.strip()
        if (_UNCERTAIN.search(clause)
                and not re.search(r"temperature|temp|온도|℃|°\s*C|\d\s*도|pressure|압력|구동압|MPa|체결압",
                                  clause, re.I)):
            warnings.update("unscoped_or_ambiguous:" + field for field in _CONDITIONS)
        for field, pattern, hint in (
            ("temperature_c", _TEMP, r"temperature|temp|온도|℃|°\s*C|\d\s*도"),
            ("pressure_mpa", _PRESSURE, r"pressure|압력|구동압|MPa|체결압"),
        ):
            if not re.search(hint, clause, re.I):
                continue
            match = pattern.fullmatch(clause)
            if match and not _UNCERTAIN.search(clause):
                value = next(_number(x) for x in match.groups() if x is not None)
                if value is not None and (-100 <= value <= 200 if field == "temperature_c" else 0 <= value <= 100):
                    values[field].add(value)
                    continue
            warnings.add("unscoped_or_ambiguous:" + field)
    # Never pick one convenient clause from a multi-stage/ambiguous description.
    for warning in warnings:
        values.pop(warning.split(":", 1)[1], None)
    return {k: sorted(v) for k, v in values.items()}, sorted(warnings)


def _parse_design(text, *, label=False):
    values = defaultdict(set)
    warnings = set()
    if _UNCERTAIN.search(_text(text)):
        return {}, ["unscoped_or_ambiguous:" + field for field in _DESIGN]
    clauses = re.split(r"[;\n]", _text(text))
    hints = {
        "ptfe_wt": r"(?<![a-z])ptfe",
        "porosity_pct": r"porosity|공극률|(?<![a-z])P\d",
        "loading_mah_cm2": r"loading",
    }
    for clause in clauses:
        for field, pattern in _DESIGN_PATTERNS.items():
            if not re.search(hints[field], clause, re.I):
                continue
            match = pattern.fullmatch(clause.strip())
            matches = [match] if match else []
            if not matches and label:
                tokens = re.split(r"[\s_]+", clause)
                matches = [m for token in tokens if (m := pattern.fullmatch(token))]
                if len(matches) != len(re.findall(hints[field], clause, re.I)):
                    matches = []
            if not matches:
                warnings.add("unscoped_or_ambiguous:" + field)
            for match in matches:
                value = _number(match[1])
                upper = 20 if field == "loading_mah_cm2" else 100
                if value is not None and 0 <= value <= upper:
                    values[field].add(value)
                else:
                    warnings.add("unscoped_or_ambiguous:" + field)
    for warning in warnings:
        values.pop(warning.split(":", 1)[1], None)
    return {k: sorted(v) for k, v in values.items()}, sorted(warnings)


def _support_groups(sets, decks):
    """Union source dependencies only, never test/design relationships."""
    parent = {}

    def root(key):
        parent.setdefault(key, key)
        if parent[key] != key:
            parent[key] = root(parent[key])
        return parent[key]

    families = {d.get("file_path"): _text(d.get("family_id")) for d in decks}
    tokens = {}
    for row in sets:
        path = _text(row.get("file_path"))
        keys = ["file:" + path]
        if families.get(path):
            keys.append("family:" + families[path])
        content = _text(row.get("content_hash"))
        if content and not content.startswith("u:"):
            keys.append("content:" + content)
        for key in keys:
            a, b = root(keys[0]), root(key)
            parent[max(a, b)] = min(a, b)
        tokens[path] = keys[0]
    return {path: _id(root(key)) for path, key in tokens.items()}


_SCHEMAS = {
    "evidence_sources": """
        evidence_id TEXT PRIMARY KEY, project TEXT, test_key TEXT, design_id TEXT,
        file_path TEXT, file_name TEXT, slide_no INTEGER, set_id INTEGER, set_label TEXT,
        parsed_set_label TEXT, support_group TEXT, source_confidence TEXT,
        review_confidence TEXT, reviewer TEXT, reviewer_kind TEXT, human_confirmed INTEGER,
        confidence TEXT, status TEXT, rejection_reason TEXT, guards_json TEXT,
        conditions_json TEXT, parse_warnings_json TEXT, evidence TEXT,
        source_link_json TEXT, source_set_json TEXT, original_test_json TEXT,
        original_design_json TEXT""",
    "evidence_links": """
        evidence_id TEXT PRIMARY KEY, candidate_id TEXT, entity_type TEXT,
        left_id TEXT, right_id TEXT, relation TEXT, status TEXT, confidence TEXT,
        human_confirmed INTEGER, same_physical_cell INTEGER, support_group TEXT,
        left_source_id TEXT, right_source_id TEXT, guards_json TEXT""",
    "evidence_attributes": """
        evidence_id TEXT PRIMARY KEY, entity_type TEXT, entity_id TEXT, test_key TEXT,
        field TEXT, proposed_value REAL, original_value_json TEXT, original_record_json TEXT,
        status TEXT, confidence TEXT, human_confirmed INTEGER, scope TEXT,
        source_id TEXT, support_group TEXT, basis TEXT, guards_json TEXT""",
}
_COLUMNS = {
    name: [part.strip().split()[0] for part in schema.split(",")]
    for name, schema in _SCHEMAS.items()
}


def build(project, conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Read an attached snapshot; return rows for the three evidence_* tables.

    Missing tables produce empty results; missing review/identity fields fail
    closed. Rejected link rows remain queryable in evidence_sources. Source IDs
    are deterministic for identical input, and exact duplicate links collapse.
    No raw files, parquet files, source DB records, or slide decks are written.
    """
    tests = _unique(_rows(conn, "tests"), "test_key")
    designs = _unique(_rows(conn, "designs"), "design_id")
    sets = _rows(conn, "slide_sets")
    by_set = _unique(sets, "set_id")
    by_page = defaultdict(list)
    paths_by_name_page = defaultdict(set)
    for row in sets:
        by_page[(row.get("file_path"), _integer(row.get("slide_no")))].append(row)
        if _text(row.get("file_name")):
            paths_by_name_page[(row["file_name"], _integer(row.get("slide_no")))].add(row.get("file_path"))
    support = _support_groups(sets, _rows(conn, "slide_decks"))
    veto = set()
    for row in _rows(conn, "slide_link_review"):
        if _text(row.get("decision")).lower() in ("no_slide", "unsure"):
            veto.add(row.get("test_key"))
    for row in _rows(conn, "test_slide_facts"):
        if any(_text(row.get(k)).lower() in ("no_slide", "unsure", "unresolved")
               for k in ("review_status", "link_status")):
            veto.add(row.get("test_key"))

    result = {name: [] for name in _SCHEMAS}
    accepted = defaultdict(list)
    observed_values = defaultdict(set)
    observed_ambiguities = set()
    source_ids = set()
    for link in _rows(conn, "slide_test_links"):
        key = link.get("test_key")
        test = tests.get(key, {})
        sid = _integer(link.get("set_id"))
        source = by_set.get(sid, {})
        page = (link.get("file_path"), _integer(link.get("slide_no")))
        review_conf = _text(link.get("review_confidence")).lower()
        reviewer = _text(link.get("reviewer"))
        review_evidence = _text(link.get("review_evidence", link.get("evidence")))
        review_evidence = re.sub(r"^\s*검토\s*:\s*", "", review_evidence)
        conditions, warnings = _parse_conditions(link.get("review_conditions"))
        condition_observations = [(field, value, "explicit_review_conditions")
                                  for field, values in conditions.items() for value in values]
        label_conditions, label_warnings = _parse_conditions(source.get("set_label"))
        warnings += label_warnings
        for field, values in label_conditions.items():
            condition_observations += [(field, value, "explicit_set_label") for value in values]
            conditions[field] = sorted(set(conditions.get(field, [])) | set(values))
        design_observations = []
        for text, label, basis in ((source.get("set_label"), True, "explicit_set_label"),
                                   (link.get("review_design"), False, "explicit_review_design")):
            values, design_warnings = _parse_design(text, label=label)
            warnings += design_warnings
            design_observations += [(field, value, basis) for field, items in values.items() for value in items]
        reasons = []
        if key not in tests:
            reasons.append("unknown_or_duplicate_test")
        if key in veto:
            reasons.append("review_veto")
        if _text(link.get("review_status")) != "link":
            reasons.append("not_reviewed_link")
        if review_conf not in ("high", "medium") or not reviewer:
            reasons.append("insufficient_review")
        if link.get("confidence") not in ("confirmed", "probable", "verified", "matched"):
            reasons.append("weak_source_link")
        if not review_evidence:
            reasons.append("missing_review_evidence")
        explicit_path = _text(link.get("review_file_path"))
        if (len(paths_by_name_page[(link.get("file_name"), page[1])]) > 1
                and (not explicit_path or Path(explicit_path) != Path(_text(page[0])))):
            # Only an explicit review path can disambiguate repeated basenames.
            reasons.append("ambiguous_deck_reference")
        if sid is None or sid < 0 or not source:
            reasons.append("unresolved_set")
        elif (not page[0] or page[1] is None or page[1] < 1
              or page != (source.get("file_path"), _integer(source.get("slide_no")))):
            reasons.append("source_identity_mismatch")
        else:
            matches = [s for s in by_page[page] if _matches(link.get("set_label"), s)]
            if len(matches) != 1 or matches[0].get("set_id") != sid:
                reasons.append("ambiguous_or_inexact_set_scope")
        evidence_id = _id({k: v for k, v in link.items() if k != "rank"})
        if evidence_id in source_ids:
            continue
        source_ids.add(evidence_id)
        guards = []
        if _text(link.get("condition_conflict")):
            guards.append("upstream_condition_conflict")
        if (_number(link.get("n_disagree")) or 0) > 0:
            guards.append("metric_disagreement")
        for field, values in conditions.items():
            if len(values) > 1:
                guards.append("multiple_review_values:" + field)
            original = test.get(field)
            if original is not None and original != "":
                if any(_number(original) != v for v in values):
                    guards.append("original_condition_conflict:" + field)
        design = _text(test.get("design_id")) or _text(test.get("candidate_id"))
        row = dict(
            evidence_id=evidence_id, project=str(Path(project)), test_key=key, design_id=design,
            file_path=link.get("file_path"), file_name=link.get("file_name"), slide_no=page[1],
            set_id=sid, set_label=link.get("set_label"), parsed_set_label=source.get("set_label"),
            support_group=support.get(page[0], _id(page[0])), source_confidence=link.get("confidence"),
            review_confidence=review_conf, reviewer=reviewer,
            reviewer_kind="agent" if re.search(r"agent|copilot|gpt|claude", reviewer, re.I) else "unspecified",
            human_confirmed=0, confidence="medium" if review_conf == "high" else "low",
            status="rejected" if reasons else "eligible", rejection_reason=";".join(reasons),
            guards_json=_json(sorted(guards)), conditions_json=_json(conditions),
            parse_warnings_json=_json(warnings), evidence=link.get("evidence"),
            source_link_json=_json(link), source_set_json=_json(source),
            original_test_json=_json(test), original_design_json=_json(designs.get(design, {})))
        result["evidence_sources"].append(row)
        if reasons:
            continue
        accepted[(page[0], page[1], sid)].append(row)
        for warning in warnings:
            field = warning.split(":", 1)[1]
            observed_ambiguities.add(("test", key, field) if field in _CONDITIONS else ("design", design, field))
        observations = condition_observations + design_observations
        for field, value, basis in observations:
            entity_type = "test" if field in _CONDITIONS else "design"
            observed_values[(entity_type, key if entity_type == "test" else design, field)].add(value)
            if review_conf != "high":
                continue
            original_record = test if entity_type == "test" else designs.get(design)
            if original_record is None:
                continue
            original_fields = test if entity_type == "test" else _object(original_record.get("design_fields_json"))
            original = original_fields.get(field)
            attr_guards = list(guards)
            if entity_type == "design" and _text(original_record.get("conflicts")):
                attr_guards.append("existing_design_conflicts")
            if original is None or original == "":
                status = "candidate"
            elif _number(original) == value:
                status = "agrees_existing"
            else:
                status = "conflict"
                attr_guards.append("original_value_conflict")
            if attr_guards and status != "conflict":
                status = "blocked_conflict"
            result["evidence_attributes"].append(dict(
                evidence_id=_id(evidence_id, entity_type, field, value, basis),
                entity_type=entity_type, entity_id=key if entity_type == "test" else design,
                test_key=key, field=field, proposed_value=value, original_value_json=_json(original),
                original_record_json=_json(original_record), status=status, confidence=row["confidence"],
                human_confirmed=0, scope="reviewed_test" if entity_type == "test" else "reviewed_set_only",
                source_id=evidence_id, support_group=row["support_group"], basis=basis,
                guards_json=_json(sorted(attr_guards))))

    # Disagreement is preserved, never resolved by majority/copy count.
    attributes = defaultdict(list)
    for row in result["evidence_attributes"]:
        attributes[(row["entity_type"], row["entity_id"], row["field"])].append(row)
    for identity, rows in attributes.items():
        if identity in observed_ambiguities:
            for row in rows:
                if row["status"] != "conflict":
                    row["status"] = "blocked_conflict"
                row["guards_json"] = _json(sorted(set(json.loads(row["guards_json"])) | {"ambiguous_source_value"}))
        if len(observed_values[identity]) > 1:
            for row in rows:
                row["status"] = "conflict"
                row["guards_json"] = _json(sorted(set(json.loads(row["guards_json"])) | {"competing_source_values"}))

    for rows in accepted.values():
        for row in rows:
            guards = set(json.loads(row["guards_json"]))
            for field in _CONDITIONS:
                if len(observed_values[("test", row["test_key"], field)]) > 1:
                    guards.add("competing_test_conditions:" + field)
            row["guards_json"] = _json(sorted(guards))
        for left, right in itertools.combinations(sorted(rows, key=lambda r: r["evidence_id"]), 2):
            if left["test_key"] == right["test_key"]:
                continue
            guards = set(json.loads(left["guards_json"]) + json.loads(right["guards_json"]))
            for field in _CONDITIONS:
                lv = set(json.loads(left["conditions_json"]).get(field, []))
                rv = set(json.loads(right["conditions_json"]).get(field, []))
                for row, values in ((left, lv), (right, rv)):
                    original = _number(tests[row["test_key"]].get(field))
                    if original is not None:
                        values.add(original)
                if len(lv | rv) > 1:
                    guards.add("pair_condition_conflict:" + field)
            for entity_type, field in (("test", "test_key"), ("design", "design_id")):
                a, b = left[field], right[field]
                if not a or not b or a == b or entity_type == "design" and (a not in designs or b not in designs):
                    continue
                first, second = (left, right) if a < b else (right, left)
                result["evidence_links"].append(dict(
                    evidence_id=_id(entity_type, first["evidence_id"], second["evidence_id"]),
                    candidate_id=_id(entity_type, first[field], second[field], "common_set"),
                    entity_type=entity_type, left_id=first[field], right_id=second[field],
                    relation="common_set", status="blocked_conflict" if guards else "candidate",
                    confidence="low" if "low" in (left["confidence"], right["confidence"]) else "medium",
                    human_confirmed=0, same_physical_cell=0, support_group=left["support_group"],
                    left_source_id=first["evidence_id"], right_source_id=second["evidence_id"],
                    guards_json=_json(sorted(guards))))
    return result


_VIEWS = {
    "v_evidence_link_candidates": """
        SELECT candidate_id, entity_type, left_id, right_id, relation,
               CASE WHEN SUM(status='blocked_conflict')>0 THEN 'blocked_conflict' ELSE 'candidate' END status,
               CASE WHEN SUM(confidence='low')>0 THEN 'low' ELSE 'medium' END confidence,
               0 human_confirmed, 0 same_physical_cell, COUNT(*) n_observations,
               COUNT(DISTINCT support_group) n_support_groups
        FROM evidence_links GROUP BY candidate_id, entity_type, left_id, right_id, relation""",
    "v_evidence_link_sources": """
        SELECT l.*, a.file_path, a.file_name, a.slide_no, a.set_id, a.set_label,
               a.test_key left_test_key, b.test_key right_test_key,
               a.evidence left_evidence, b.evidence right_evidence,
               a.reviewer left_reviewer, b.reviewer right_reviewer,
               a.review_confidence left_review_confidence, b.review_confidence right_review_confidence
        FROM evidence_links l JOIN evidence_sources a ON l.left_source_id=a.evidence_id
        JOIN evidence_sources b ON l.right_source_id=b.evidence_id""",
    "v_evidence_attribute_sources": """
        SELECT a.*, s.file_path, s.file_name, s.slide_no, s.set_id, s.set_label,
               s.evidence, s.reviewer, s.review_confidence
        FROM evidence_attributes a JOIN evidence_sources s ON a.source_id=s.evidence_id""",
    "v_evidence_design_attributes": """
        SELECT * FROM v_evidence_attribute_sources WHERE entity_type='design'""",
}


def attach_to_db(project, conn: sqlite3.Connection, *, export_dir=None) -> dict[str, int]:
    """Replace only owned tables/views atomically; NEVER commit the caller's work.

    Returns row counts. The caller must commit (or rollback). CSV export is
    opt-in; alternatively call export_csv after committing. An export error
    leaves the transaction pending for the caller to roll back.
    """
    rows = build(project, conn)
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT evidence_links_attach")
    try:
        for view in reversed(_VIEWS):
            conn.execute(f'DROP VIEW IF EXISTS "{view}"')
        for table, schema in _SCHEMAS.items():
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            conn.execute(f'CREATE TABLE "{table}" ({schema})')
            cols = _COLUMNS[table]
            conn.executemany(f'INSERT INTO "{table}" VALUES ({",".join("?" for _ in cols)})',
                             [tuple(row[c] for c in cols) for row in rows[table]])
        conn.execute("CREATE INDEX ix_evidence_sources_set ON evidence_sources(file_path,slide_no,set_id)")
        conn.execute("CREATE INDEX ix_evidence_links_candidate ON evidence_links(candidate_id)")
        conn.execute("CREATE INDEX ix_evidence_attributes_entity ON evidence_attributes(entity_type,entity_id,field)")
        for view, query in _VIEWS.items():
            conn.execute(f'CREATE VIEW "{view}" AS {query}')
        conn.execute("RELEASE SAVEPOINT evidence_links_attach")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT evidence_links_attach")
        conn.execute("RELEASE SAVEPOINT evidence_links_attach")
        raise
    if export_dir is not None:
        export_csv(conn, export_dir)
    return {table: len(data) for table, data in rows.items()}


def export_csv(conn: sqlite3.Connection, directory) -> list[Path]:
    """Explicit opt-in export of owned tables and summary view (UTF-8 BOM)."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for table in (*_SCHEMAS, "v_evidence_link_candidates"):
        cur = conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')
        path = directory / f"{table}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as stream:
            writer = csv.writer(stream)
            writer.writerow(col[0] for col in cur.description)
            writer.writerows(cur)
        paths.append(path)
    return paths
