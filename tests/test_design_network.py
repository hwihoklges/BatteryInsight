"""Tests for Module C (design_network) — synthetic contract-schema sqlite."""
from __future__ import annotations

import json
import sqlite3

import pytest

from battery_insight.design_network import (
    build_network,
    design_similarity,
    electrochem_edges,
    render_html,
    _embed_json,
)


# --------------------------------------------------------------------------- #
# Synthetic research.sqlite3 with the contract schema
# --------------------------------------------------------------------------- #
def _make_db(path, *, extra_metrics=True):
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE designs(design_id TEXT PRIMARY KEY, design_label TEXT, status TEXT,
            tags_json TEXT, design_fields_json TEXT, conflicts TEXT, source TEXT,
            n_cells INTEGER, n_runs INTEGER);
        CREATE TABLE cells(cell_id TEXT PRIMARY KEY, status TEXT, design_id TEXT,
            physical_cell_id TEXT, sample_name TEXT);
        CREATE TABLE runs(run_id TEXT PRIMARY KEY, test_key TEXT, channel TEXT,
            cell_id TEXT, design_id TEXT, cohort_key TEXT, n_datasets INTEGER, quality TEXT);
        CREATE TABLE design_metrics(design_id TEXT, cohort_key TEXT, metric_id TEXT,
            window TEXT, n_cells INTEGER, mean REAL, sd REAL, median REAL, min REAL, max REAL);
        CREATE TABLE metric_defs(metric_id TEXT PRIMARY KEY, name_ko TEXT, unit TEXT,
            higher_is_better INTEGER, definition TEXT);
        CREATE TABLE cohorts(cohort_key TEXT PRIMARY KEY, test_type TEXT, temperature_c REAL,
            c_rate_class TEXT, v_window TEXT, n_designs INTEGER, n_cells INTEGER);
        """
    )
    designs = [
        ("D1", "설계 A", "confirmed",
         json.dumps({"material": "NMC811", "process": "dry", "cell_format": "pouch",
                     "supplier": "Acme", "geometry": "stack", "anode": "graphite"}), "{}", "", "map", 4, 6),
        # D2 identical tags to D1 -> design sim 1.0
        ("D2", "설계 B", "candidate",
         json.dumps({"material": "NMC811", "process": "dry", "cell_format": "pouch",
                     "supplier": "Acme", "geometry": "stack", "anode": "graphite"}), "{}", "", "cand", 3, 4),
        # D3 totally different tags
        ("D3", "설계 C", "confirmed",
         json.dumps({"material": "LFP", "process": "wet", "cell_format": "cylindrical",
                     "supplier": "Beta", "geometry": "jelly", "anode": "silicon"}), "{}", "", "map", 5, 7),
    ]
    cur.executemany("INSERT INTO designs VALUES (?,?,?,?,?,?,?,?,?)", designs)

    cur.execute("INSERT INTO cohorts VALUES (?,?,?,?,?,?,?)",
                ("cycle|25|0.5C/0.5C|3.0-4.2", "cycle", 25.0, "0.5C/0.5C", "3.0-4.2", 3, 12))

    cur.executemany(
        "INSERT INTO metric_defs VALUES (?,?,?,?,?)",
        [
            ("cap_initial_ah", "초기 용량", "Ah", 1, "first full discharge"),
            ("retention_pct", "용량 유지율", "%", 1, "cap_at/cap_initial"),
            ("fade_pct_per_100", "열화율", "%/100cyc", 0, "linear fade"),
            ("ce_median_pct", "쿨롱 효율", "%", None, "median CE"),
        ],
    )

    ck = "cycle|25|0.5C/0.5C|3.0-4.2"
    # D1 and D2 get DIFFERENT electrochem values; D3 different again.
    # ce_median_pct has ZERO MAD across designs (all equal) -> must be skipped.
    rows = [
        # design, cohort, metric, window, n_cells, mean, sd, median, min, max
        ("D1", ck, "cap_initial_ah", "", 4, 2.00, 0.05, 2.00, 1.9, 2.1),
        ("D2", ck, "cap_initial_ah", "", 3, 3.00, 0.04, 3.00, 2.9, 3.1),
        ("D3", ck, "cap_initial_ah", "", 5, 2.50, 0.06, 2.50, 2.4, 2.6),
        ("D1", ck, "retention_pct", "100", 4, 95.0, 1.0, 95.0, 93, 97),
        ("D2", ck, "retention_pct", "100", 3, 70.0, 1.2, 70.0, 68, 72),
        ("D3", ck, "retention_pct", "100", 5, 85.0, 1.1, 85.0, 83, 87),
        # ce_median_pct identical across designs -> MAD = 0 -> skipped metric
        ("D1", ck, "ce_median_pct", "", 4, 99.5, 0.1, 99.5, 99, 100),
        ("D2", ck, "ce_median_pct", "", 3, 99.5, 0.1, 99.5, 99, 100),
        ("D3", ck, "ce_median_pct", "", 5, 99.5, 0.1, 99.5, 99, 100),
    ]
    if extra_metrics:
        rows += [
            ("D1", ck, "fade_pct_per_100", "", 4, 1.0, 0.2, 1.0, 0.8, 1.2),
            ("D2", ck, "fade_pct_per_100", "", 3, 6.0, 0.3, 6.0, 5.7, 6.3),
            ("D3", ck, "fade_pct_per_100", "", 5, 3.0, 0.25, 3.0, 2.8, 3.2),
        ]
    cur.executemany("INSERT INTO design_metrics VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Similarity maths
# --------------------------------------------------------------------------- #
def test_design_similarity_identical_is_one():
    tags = {"material": "NMC811", "process": "dry", "cell_format": "pouch"}
    assert design_similarity(tags, dict(tags)) == pytest.approx(1.0)


def test_design_similarity_disjoint_is_zero():
    a = {"material": "NMC811", "process": "dry"}
    b = {"material": "LFP", "process": "wet"}
    assert design_similarity(a, b) == pytest.approx(0.0)


def test_design_similarity_no_keys_is_none():
    assert design_similarity({}, {}) is None
    assert design_similarity({"storage": "dry"}, {"condition": "x"}) is None


def test_electrochem_identical_designs_similarity_one():
    metrics = {
        "A": {"m1": 1.0, "m2": 2.0, "m3": 3.0},
        "B": {"m1": 1.0, "m2": 2.0, "m3": 3.0},
        # C and D provide spread so MAD>0 even though A==B
        "C": {"m1": 5.0, "m2": 9.0, "m3": -1.0},
        "D": {"m1": 8.0, "m2": -4.0, "m3": 11.0},
    }
    edges = electrochem_edges(metrics)
    ab = next(e for e in edges if {e["source"], e["target"]} == {"A", "B"})
    assert ab["weight"] == pytest.approx(1.0)


def test_electrochem_mad_zero_metric_skipped():
    # m_const is identical across all designs -> MAD 0 -> skipped. Only m1,m2 remain.
    metrics = {
        "A": {"m1": 1.0, "m2": 1.0, "m_const": 5.0},
        "B": {"m1": 2.0, "m2": 2.0, "m_const": 5.0},
        "C": {"m1": 3.0, "m2": 4.0, "m_const": 5.0},
    }
    from battery_insight.design_network import electrochem_z
    z = electrochem_z(metrics)
    assert all("m_const" not in row for row in z.values())


def test_electrochem_fewer_than_two_shared_no_edge():
    # A and B share only one non-skipped metric -> no edge.
    metrics = {
        "A": {"m1": 1.0, "m2": 1.0},
        "B": {"m1": 2.0, "m3": 2.0},
        "C": {"m1": 3.0, "m2": 5.0, "m3": 9.0},
    }
    edges = electrochem_edges(metrics)
    assert not any({e["source"], e["target"]} == {"A", "B"} for e in edges)


# --------------------------------------------------------------------------- #
# End-to-end build + discordance + self-containment
# --------------------------------------------------------------------------- #
def test_build_network_end_to_end(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    _make_db(db / "research.sqlite3")

    html_path = build_network(tmp_path)
    assert html_path.exists()
    payload = json.loads((db / "network.json").read_text(encoding="utf-8"))

    assert len(payload["designs"]) == 3
    # D1==D2 tags -> structural edge at default threshold 0.3
    pairs = {frozenset((e["source"], e["target"])) for e in payload["design_edges"]}
    assert frozenset(("D1", "D2")) in pairs
    # D1-D3 disjoint tags -> no structural edge
    assert frozenset(("D1", "D3")) not in pairs

    ck = "cycle|25|0.5C/0.5C|3.0-4.2"
    assert ck in payload["electrochem_edges"]
    assert len(payload["electrochem_edges"][ck]) >= 1

    cohort = next(c for c in payload["cohorts"] if c["cohort_key"] == ck)
    assert cohort["useful"] is True


def test_discordance_present(tmp_path):
    # D1 & D2 identical design (sim 1) but divergent electrochem -> "유사설계·상이응답".
    db = tmp_path / "db"
    db.mkdir()
    _make_db(db / "research.sqlite3")
    build_network(tmp_path)
    payload = json.loads((db / "network.json").read_text(encoding="utf-8"))

    ck = "cycle|25|0.5C/0.5C|3.0-4.2"
    d_edges = {frozenset((e["source"], e["target"])): e["weight"] for e in payload["design_edges"]}
    e_edges = {frozenset((e["source"], e["target"])): e["weight"]
               for e in payload["electrochem_edges"][ck]}
    key = frozenset(("D1", "D2"))
    assert d_edges.get(key) == pytest.approx(1.0)
    # electrochem similarity should be clearly lower (divergent metrics)
    assert e_edges.get(key, 1.0) < d_edges[key]
    # discordance condition with defaults (hi=0.6, lo=0.4): design>=hi and ec<=lo
    assert d_edges[key] >= 0.6 and e_edges.get(key, 1.0) <= 0.4


def test_html_is_self_contained(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    _make_db(db / "research.sqlite3")
    build_network(tmp_path)
    html = (db / "network.html").read_text(encoding="utf-8")

    # No remote script/link sources.
    import re
    for m in re.finditer(r"<(script|link)\b[^>]*>", html, re.IGNORECASE):
        tag = m.group(0)
        assert "http://" not in tag and "https://" not in tag, tag
    assert html.startswith("<!DOCTYPE html>")
    assert len(html.encode("utf-8")) < 10 * 1024 * 1024


def test_json_escapes_script_close():
    payload = {"designs": [{"design_id": "x", "label": "</script><b>pwn</b>"}]}
    embedded = _embed_json(payload)
    assert "</script>" not in embedded
    assert "<\\/script>" in embedded


def test_embedded_json_parses_back(tmp_path):
    db = tmp_path / "db"
    db.mkdir()
    _make_db(db / "research.sqlite3")
    build_network(tmp_path)
    html = (db / "network.html").read_text(encoding="utf-8")
    # extract the JSON payload block and confirm it parses after un-escaping
    start = html.index('type="application/json">') + len('type="application/json">')
    end = html.index("</script>", start)
    block = html[start:end].replace("<\\/", "</")
    data = json.loads(block)
    assert "designs" in data and len(data["designs"]) == 3
