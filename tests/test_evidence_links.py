import csv
import json
import sqlite3
from contextlib import ExitStack, closing

import pandas as pd
import pytest

from battery_insight import evidence_links as el


@pytest.fixture(autouse=True)
def close_test_connections(monkeypatch):
    connect = sqlite3.connect
    with ExitStack() as stack:
        def managed_connect(*args, **kwargs):
            return stack.enter_context(closing(connect(*args, **kwargs)))
        monkeypatch.setattr(sqlite3, "connect", managed_connect)
        yield


def _database():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE tests(test_key TEXT PRIMARY KEY, design_id TEXT, candidate_id TEXT,
                           temperature_c REAL, pressure_mpa REAL, owner TEXT, sample_name TEXT);
        CREATE TABLE designs(design_id TEXT PRIMARY KEY, design_label TEXT, status TEXT,
                             design_fields_json TEXT, conflicts TEXT);
        CREATE TABLE slide_sets(set_id INTEGER, file_path TEXT, file_name TEXT, slide_no INTEGER,
                                set_no INTEGER, set_label TEXT, attrs TEXT, content_hash TEXT);
        CREATE TABLE slide_decks(file_path TEXT, family_id TEXT);
        CREATE TABLE slide_test_links(test_key TEXT, set_id INTEGER, file_path TEXT, file_name TEXT,
            slide_no INTEGER, set_label TEXT, confidence TEXT, review_status TEXT,
            review_confidence TEXT, reviewer TEXT, evidence TEXT, review_conditions TEXT,
            review_design TEXT, condition_conflict TEXT, n_disagree INTEGER);
        CREATE TABLE slide_link_review(test_key TEXT, decision TEXT);
        CREATE TABLE test_slide_facts(test_key TEXT, review_status TEXT, link_status TEXT);
    """)
    for key, design in (("a", "D1"), ("b", "D2"), ("c", "D3")):
        conn.execute("INSERT INTO tests VALUES (?,?,NULL,NULL,NULL,?,?)",
                     (key, design, "owner-" + key, "same generic sample ICE90"))
        conn.execute("INSERT INTO designs VALUES (?, 'same label', 'candidate', '{}', '')", (design,))
    _set(conn)
    return conn


def _set(conn, sid=1, path=r"D:\decks\report.pptx", page=2, number=1,
         label="PTFE1.5 P30", content="content-a", family="family-a"):
    conn.execute("INSERT INTO slide_sets VALUES (?,?,?,?,?,?,?,?)",
                 (sid, path, path.rsplit("\\", 1)[-1], page, number, label,
                  '{"temperature_c":60,"pressure_mpa":9,"ptfe_wt":99}', content))
    conn.execute("INSERT INTO slide_decks VALUES (?,?)", (path, family))


def _link(conn, test="a", sid=1, **kw):
    source = conn.execute("SELECT file_path,file_name,slide_no,set_no,set_label FROM slide_sets WHERE set_id=?",
                          (sid,)).fetchone()
    path, name, page, number, label = source
    row = dict(test_key=test, set_id=sid, file_path=path, file_name=name, slide_no=page,
               set_label=f"Set{number}: {label}", confidence="confirmed", review_status="link",
               review_confidence="high", reviewer="agent", evidence="Reviewed exact Set row and test",
               review_conditions="온도=45; 압력=1.5 MPa", review_design="",
               condition_conflict="", n_disagree=0)
    row.update(kw)
    conn.execute(f'INSERT INTO slide_test_links ({",".join(row)}) VALUES ({",".join("?" for _ in row)})',
                 tuple(row.values()))


def _build(conn):
    return el.build("does-not-need-to-exist", conn)


def test_common_set_not_physical_cell_and_no_source_mutation():
    conn = _database()
    _link(conn)
    _link(conn, "b")
    before = conn.execute("SELECT * FROM tests").fetchall()
    result = _build(conn)
    links = result["evidence_links"]
    assert {(r["entity_type"], r["left_id"], r["right_id"]) for r in links} == {
        ("test", "a", "b"), ("design", "D1", "D2")}
    assert all(r["relation"] == "common_set" and r["status"] == "candidate" for r in links)
    assert all(r["human_confirmed"] == r["same_physical_cell"] == 0 for r in links)
    assert all(r["confidence"] == "medium" for r in links)
    attrs = result["evidence_attributes"]
    assert {(r["field"], r["proposed_value"]) for r in attrs} == {
        ("temperature_c", 45), ("pressure_mpa", 1.5), ("ptfe_wt", 1.5), ("porosity_pct", 30)}
    assert all(r["scope"] == "reviewed_set_only" for r in attrs if r["entity_type"] == "design")
    assert conn.execute("SELECT * FROM tests").fetchall() == before
    assert conn.execute("SELECT design_fields_json FROM designs").fetchall() == [("{}",)] * 3
    assert all(json.loads(r["source_set_json"])["attrs"] for r in result["evidence_sources"])


@pytest.mark.parametrize("change,reason", [
    ({"review_status": ""}, "not_reviewed_link"),
    ({"review_status": "unsure"}, "not_reviewed_link"),
    ({"review_confidence": "low"}, "insufficient_review"),
    ({"reviewer": ""}, "insufficient_review"),
    ({"confidence": "candidate"}, "weak_source_link"),
    ({"evidence": ""}, "missing_review_evidence"),
    ({"set_id": -1}, "unresolved_set"),
    ({"set_id": 1.5}, "unresolved_set"),
    ({"file_path": r"D:\other\report.pptx"}, "source_identity_mismatch"),
    ({"slide_no": 3}, "source_identity_mismatch"),
    ({"set_label": "Set1-1"}, "ambiguous_or_inexact_set_scope"),
    ({"set_label": "Set1 different material"}, "ambiguous_or_inexact_set_scope"),
    ({"set_label": "Set1 / Set2"}, "ambiguous_or_inexact_set_scope"),
])
def test_fail_closed_sources(change, reason):
    conn = _database()
    _link(conn, **change)
    _link(conn, "b")
    result = _build(conn)
    rejected = next(r for r in result["evidence_sources"] if r["test_key"] == "a")
    assert reason in rejected["rejection_reason"]
    assert not result["evidence_links"]
    assert not any(r["test_key"] == "a" for r in result["evidence_attributes"])


@pytest.mark.parametrize("table,values", [
    ("slide_link_review", ("a", "no_slide")),
    ("slide_link_review", ("a", "unsure")),
    ("test_slide_facts", ("a", "no_slide", "confirmed")),
    ("test_slide_facts", ("a", "unresolved", "confirmed")),
])
def test_review_veto_wins_over_confirmed_link(table, values):
    conn = _database()
    _link(conn)
    _link(conn, "b")
    conn.execute(f'INSERT INTO {table} VALUES ({",".join("?" for _ in values)})', values)
    result = _build(conn)
    assert not result["evidence_links"]
    assert "review_veto" in result["evidence_sources"][0]["rejection_reason"]


def test_same_page_number_label_or_ice_never_connect_other_sources():
    conn = _database()
    _set(conn, 2, page=3)
    _set(conn, 3, path=r"D:\another-author\report.pptx")
    _link(conn)
    _link(conn, "b", 2)
    _link(conn, "c", 3)
    assert not _build(conn)["evidence_links"]
    # Even an exact content hash/family is used only for dependency counting.
    assert len({r["support_group"] for r in _build(conn)["evidence_sources"]}) == 1


def test_same_page_different_sets_and_ambiguous_set_rows():
    conn = _database()
    _set(conn, 2, number=2)
    _link(conn)
    _link(conn, "b", 2)
    assert not _build(conn)["evidence_links"]
    _set(conn, 3, number=1)
    assert "ambiguous_or_inexact_set_scope" in _build(conn)["evidence_sources"][0]["rejection_reason"]


def test_duplicate_basenames_across_authors_fail_closed():
    conn = _database()
    _set(conn, 2, path=r"D:\another-author\report.pptx")
    _link(conn)
    _link(conn, "b")
    result = _build(conn)
    assert not result["evidence_links"] and not result["evidence_attributes"]
    assert all("ambiguous_deck_reference" in r["rejection_reason"] for r in result["evidence_sources"])


def test_unresolved_aggregated_label_does_not_become_set_identity():
    conn = _database()
    conn.execute("UPDATE slide_sets SET set_no=NULL,set_label='60℃'")
    _link(conn, set_label="60℃ L5.6")
    _link(conn, "b", set_label="60℃")
    assert all(r["status"] == "rejected" for r in _build(conn)["evidence_sources"])


@pytest.mark.parametrize("source_label,review_label", [
    ("Set1 / Set2", "Set1 / Set2"), ("Set1-1", "Set1"),
    ("baseline", "baseline"), ("Set1,2", "Set1,2"), ("Set1/2", "Set1/2"),
    ("Set1-3", "Set1-3"), ("Set1~3", "Set1~3"), ("Set1 to 3", "Set1 to 3"),
])
def test_complete_single_set_identity_required(source_label, review_label):
    conn = _database()
    conn.execute("UPDATE slide_sets SET set_label=?", (source_label,))
    _link(conn, set_label=review_label)
    _link(conn, "b", set_label=review_label)
    assert not _build(conn)["evidence_links"]


@pytest.mark.parametrize("label", ["Set1/2 PTFE1.5", "Set1~3 PTFE1.5", "Set1 to 3 PTFE1.5",
                                 "Set1,2 PTFE1.5", "Set1-3 PTFE1.5"])
def test_extracted_multi_set_source_is_not_single_set_evidence(label):
    from battery_insight.slide_facts import extract_sets

    extracted = extract_sets(pd.DataFrame([{
        "file_path": r"D:\decks\report.pptx", "slide_no": 2, "text": "",
        "table_text": "Capacity (mAh/g) | Discharge @1st\n" + label + " | 180",
    }]))
    source = extracted.iloc[0]
    conn = _database()
    number = None if pd.isna(source.set_no) else int(source.set_no)
    conn.execute("UPDATE slide_sets SET set_no=?,set_label=?", (number, source.set_label))
    _link(conn, set_label="Set1")
    _link(conn, "b", set_label="Set1")
    rows = _build(conn)
    assert not rows["evidence_links"] and not rows["evidence_attributes"]
    assert all(r["status"] == "rejected" for r in rows["evidence_sources"])


def test_formatted_empty_review_evidence_is_not_evidence():
    conn = _database()
    _link(conn, evidence="검토: ")
    _link(conn, "b")
    rows = _build(conn)
    assert not rows["evidence_links"]
    assert "missing_review_evidence" in rows["evidence_sources"][0]["rejection_reason"]


@pytest.mark.parametrize("text", ["PTFE1.5; PTFE=2/3", "PTFE1.5 PTFE=2/3", "PTFE1.5; PTFE=999"])
def test_design_parser_does_not_discard_competing_ambiguous_values(text):
    conn = _database()
    conn.execute("UPDATE slide_sets SET set_label=?", (text,))
    _link(conn)
    assert not any(r["field"] == "ptfe_wt" for r in _build(conn)["evidence_attributes"])


def test_ambiguous_review_design_blocks_label_proposal():
    conn = _database()
    _link(conn, review_design="PTFE=2/3")
    attrs = [r for r in _build(conn)["evidence_attributes"] if r["field"] == "ptfe_wt"]
    assert len(attrs) == 1 and attrs[0]["status"] == "blocked_conflict"


def test_medium_review_and_named_reviewer_never_imply_human_confirmation():
    conn = _database()
    _link(conn, review_confidence="medium", reviewer="Named reviewer")
    _link(conn, "b")
    result = _build(conn)
    assert all(r["confidence"] == "low" for r in result["evidence_links"])
    assert all(r["human_confirmed"] == 0 for rows in result.values() for r in rows)
    assert not any(r["test_key"] == "a" for r in result["evidence_attributes"])


@pytest.mark.parametrize("conditions", [
    "", "45C; 1.5MPa", "온도=25/45; 압력=1.5/2 MPa",
    "활성화 60℃ -> 사이클 45℃; 체결압=3 MPa",
    "온도=45 (추정); 압력=~1.5 MPa", "온도=45; 온도 미상",
    "온도=-25~45; 압력=2 MPa (3 MPa 선체결)", "온도=450; 압력=999 MPa",
    "temperature_c=45; unknown", "temperature_c=45; uncertain", "온도=45; 추정",
])
def test_no_page_attribute_or_ambiguous_condition_inheritance(conditions):
    conn = _database()
    _link(conn, review_conditions=conditions)
    rows = _build(conn)["evidence_attributes"]
    assert not any(r["entity_type"] == "test" for r in rows)
    assert all(r["proposed_value"] != 99 for r in rows)


def test_explicit_condition_grammar_and_design_attribute_grammar():
    conn = _database()
    _link(conn, review_conditions="temperature_c=-5; pressure_mpa=0",
          review_design="loading_mah_cm2=4.5; cathode=A/B 80/20")
    attrs = _build(conn)["evidence_attributes"]
    assert {(a["field"], a["proposed_value"]) for a in attrs} >= {
        ("temperature_c", -5), ("pressure_mpa", 0), ("loading_mah_cm2", 4.5)}


def test_explicit_set_label_condition_cannot_be_overruled_by_review():
    conn = _database()
    conn.execute("UPDATE slide_sets SET set_label='material A; 온도=60'")
    _link(conn, review_conditions="온도=45")
    attrs = [r for r in _build(conn)["evidence_attributes"] if r["field"] == "temperature_c"]
    assert {r["proposed_value"] for r in attrs} == {45, 60}
    assert {r["basis"] for r in attrs} == {"explicit_set_label", "explicit_review_conditions"}
    assert all(r["status"] == "conflict" for r in attrs)


def test_original_and_competing_values_preserved_not_majority_voted():
    conn = _database()
    conn.execute("UPDATE tests SET temperature_c=25 WHERE test_key='a'")
    conn.execute("""UPDATE designs SET design_fields_json='{"ptfe_wt":2}' WHERE design_id='D1'""")
    _link(conn)
    _link(conn, "b", review_conditions="온도=60; 압력=1.5 MPa")
    _set(conn, 2, page=3)
    _link(conn, "a", 2, review_conditions="온도=60; 압력=1.5 MPa")
    result = _build(conn)
    temps = [r for r in result["evidence_attributes"] if r["entity_id"] == "a" and r["field"] == "temperature_c"]
    assert {r["proposed_value"] for r in temps} == {45, 60}
    assert all(r["status"] == "conflict" and json.loads(r["original_value_json"]) == 25 for r in temps)
    ptfe = [r for r in result["evidence_attributes"] if r["entity_id"] == "D1" and r["field"] == "ptfe_wt"]
    assert all(r["status"] == "conflict" for r in ptfe)
    assert all(r["status"] == "blocked_conflict" for r in result["evidence_links"])
    assert conn.execute("SELECT temperature_c FROM tests WHERE test_key='a'").fetchone() == (25,)


def test_medium_conflicting_observation_blocks_high_attribute():
    conn = _database()
    _link(conn)
    _set(conn, 2, page=3)
    _link(conn, "a", 2, review_confidence="medium", review_conditions="온도=60")
    temps = [r for r in _build(conn)["evidence_attributes"] if r["field"] == "temperature_c"]
    assert len(temps) == 1 and temps[0]["status"] == "conflict"


def test_competing_test_conditions_block_all_pair_observations():
    conn = _database()
    _link(conn)
    _link(conn, "b")
    _set(conn, 2, page=3)
    _link(conn, "a", 2, review_conditions="온도=60")
    _link(conn, "b", 2, review_conditions="온도=60")
    result = _build(conn)
    assert all(r["status"] == "blocked_conflict" for r in result["evidence_links"])
    assert all("competing_test_conditions" in r["guards_json"] for r in result["evidence_sources"])


@pytest.mark.parametrize("changes", [{"condition_conflict": "DB pressure mismatch"}, {"n_disagree": 1}])
def test_upstream_conflicts_retained_and_blocked(changes):
    conn = _database()
    _link(conn, **changes)
    _link(conn, "b")
    result = _build(conn)
    assert all(r["status"] == "blocked_conflict" for r in result["evidence_links"])
    assert all(r["status"] == "blocked_conflict" for r in result["evidence_attributes"] if r["test_key"] == "a")


def test_no_transitive_propagation_or_attribute_transfer():
    conn = _database()
    _set(conn, 2, page=3, number=2, label="different sample")
    _link(conn)
    _link(conn, "b")
    _link(conn, "b", 2, review_conditions="")
    _link(conn, "c", 2, review_conditions="")
    result = _build(conn)
    pairs = {(r["left_id"], r["right_id"]) for r in result["evidence_links"] if r["entity_type"] == "test"}
    assert pairs == {("a", "b"), ("b", "c")}
    assert not any(r["test_key"] == "c" for r in result["evidence_attributes"])


def test_copies_versions_count_once_without_cross_file_pairing():
    conn = _database()
    _set(conn, 2, path=r"D:\decks\report_v2.pptx", content="changed-content")
    _set(conn, 3, path=r"D:\copy\other.pptx", content="changed-content", family="other-family")
    for sid in (1, 2, 3):
        _link(conn, sid=sid)
        _link(conn, "b", sid)
    _link(conn)  # exact duplicate is not another observation
    el.attach_to_db("project", conn)
    assert conn.execute("SELECT n_observations,n_support_groups FROM v_evidence_link_candidates "
                        "WHERE entity_type='test'").fetchone() == (3, 1)
    assert conn.execute("SELECT COUNT(*) FROM evidence_sources").fetchone() == (6,)
    assert conn.execute("SELECT COUNT(*) FROM v_evidence_link_sources WHERE file_path IS NULL").fetchone() == (0,)


def test_attach_idempotent_transactional_and_missing_tables():
    conn = _database()
    _link(conn)
    _link(conn, "b")
    conn.commit()
    before = list(conn.iterdump())
    first = el.attach_to_db("project", conn)
    assert conn.in_transaction
    rows = conn.execute("SELECT * FROM evidence_links ORDER BY evidence_id").fetchall()
    assert el.attach_to_db("project", conn) == first
    assert conn.execute("SELECT * FROM evidence_links ORDER BY evidence_id").fetchall() == rows
    conn.rollback()
    assert list(conn.iterdump()) == before
    empty = sqlite3.connect(":memory:")
    assert el.attach_to_db("project", empty) == {name: 0 for name in first}
    assert empty.execute("SELECT COUNT(*) FROM v_evidence_design_attributes").fetchone() == (0,)


def test_reattach_removes_stale_proposals_and_preserves_pending_work():
    conn = _database()
    _link(conn)
    _link(conn, "b")
    el.attach_to_db("project", conn)
    conn.commit()
    conn.execute("INSERT INTO slide_link_review VALUES ('a','no_slide')")
    el.attach_to_db("project", conn)
    assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone() == (0,)
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone() == (2,)
    assert conn.execute("SELECT COUNT(*) FROM slide_link_review").fetchone() == (0,)


def test_failed_attach_restores_owned_tables_without_rolling_back_caller():
    conn = _database()
    _link(conn)
    _link(conn, "b")
    el.attach_to_db("project", conn)
    conn.commit()
    conn.execute("UPDATE tests SET owner='pending' WHERE test_key='a'")

    def deny_create(action, name, *unused):
        return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_CREATE_TABLE and name == "evidence_links" else sqlite3.SQLITE_OK

    conn.set_authorizer(deny_create)
    with pytest.raises(sqlite3.DatabaseError):
        el.attach_to_db("project", conn)
    conn.set_authorizer(None)
    assert conn.execute("SELECT COUNT(*) FROM evidence_links").fetchone() == (2,)
    assert conn.execute("SELECT owner FROM tests WHERE test_key='a'").fetchone() == ("pending",)
    assert conn.in_transaction


def test_build_works_with_readonly_query_mode_and_sqlite_row_factory():
    conn = _database()
    _link(conn)
    _link(conn, "b")
    conn.execute("PRAGMA query_only=ON")
    conn.row_factory = sqlite3.Row
    assert len(_build(conn)["evidence_links"]) == 2


def test_opt_in_csv_export_and_queryable_provenance(tmp_path):
    conn = _database()
    _link(conn)
    _link(conn, "b")
    el.attach_to_db("project", conn, export_dir=tmp_path)
    paths = list(tmp_path.glob("*.csv"))
    assert len(paths) == 4
    with (tmp_path / "evidence_sources.csv").open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 2 and all(r["file_path"] and r["slide_no"] and r["set_id"] for r in rows)
    assert conn.execute("SELECT COUNT(*) FROM v_evidence_design_attributes").fetchone() == (4,)
    assert conn.in_transaction
