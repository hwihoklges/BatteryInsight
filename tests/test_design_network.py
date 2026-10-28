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
    _collect,
    _embed_json,
    _percentiles,
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
    assert cohort["comparison_eligible"] is True
    assert cohort["comparison_exclusions"] == []


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


def test_unknown_censor_status_blocks_even_stale_nonnull_means():
    from battery_insight.design_network import _metric_comparison_exclusions, _percentiles

    metric = {
        "metric_id": "cycles_to_80pct", "mean": 200, "n_cells": 2,
        "n_observed_cells": 1, "n_censored_cells": 0, "n_unknown_censor_cells": 1,
    }
    assert _metric_comparison_exclusions(metric) == ["censoring_metadata_unknown"]
    assert _percentiles({"D1": {"cycles_to_80pct": metric}}, {"cycles_to_80pct": True}) == {"D1": {}}


@pytest.fixture
def synthetic_conn(tmp_path):
    path = tmp_path / "synthetic.sqlite3"
    _make_db(path)
    conn = sqlite3.connect(path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.mark.parametrize(("field", "value"), [
    ("temperature_c", None), ("temperature_c", ""), ("temperature_c", "unknown"),
    ("temperature_c", "T?"), ("temperature_c", "NaN"), ("temperature_c", "inf"),
    ("test_type", None), ("test_type", " "), ("test_type", "UNKNOWN"),
    ("test_type", "N/A"), ("test_type", "unspecified"), ("test_type", "미정"),
    ("c_rate_class", None), ("c_rate_class", "unknown"), ("c_rate_class", "C?"),
    ("c_rate_class", "0.5C/C?"), ("c_rate_class", "C?/0.5C"),
    ("c_rate_class", "0.5C/unknown"), ("c_rate_class", "--"),
    ("c_rate_class", "0.5C/N/A"), ("c_rate_class", "C_unknown"),
    ("v_window", None), ("v_window", "?-4.2"), ("v_window", "3.0-?"),
    ("v_window", "null"), ("v_window", "—"),
])
def test_unknown_original_conditions_block_comparisons(synthetic_conn, field, value):
    conn = synthetic_conn
    original = _collect(conn)
    conn.execute(f"UPDATE cohorts SET {field}=?", (value,))
    before = conn.execute("SELECT * FROM cohorts").fetchall()
    metric_rows = conn.execute("SELECT * FROM design_metrics").fetchall()
    payload = _collect(conn)
    cohort = payload["cohorts"][0]
    ck = cohort["cohort_key"]
    # The key still spells out known conditions; it must not fill missing originals.
    assert cohort[field] == value
    assert cohort["comparison_eligible"] is False
    assert cohort["comparison_exclusions"] == [f"missing_condition:{field}"]
    assert cohort["useful"] is False
    assert payload["electrochem_edges"][ck] == []
    assert payload["percentiles"][ck] == {"D1": {}, "D2": {}, "D3": {}}
    assert payload["design_metrics"] == original["design_metrics"]
    assert payload["design_edges"] == original["design_edges"]
    assert conn.execute("SELECT * FROM cohorts").fetchall() == before
    assert conn.execute("SELECT * FROM design_metrics").fetchall() == metric_rows


def test_absent_cohort_does_not_infer_conditions_from_key(synthetic_conn):
    synthetic_conn.execute("DELETE FROM cohorts")
    payload = _collect(synthetic_conn)
    cohort = payload["cohorts"][0]
    assert cohort["comparison_eligible"] is False
    assert cohort["useful"] is False
    assert cohort["comparison_exclusions"] == [
        "missing_condition:test_type", "missing_condition:temperature_c",
        "missing_condition:c_rate_class", "missing_condition:v_window",
    ]
    assert payload["electrochem_edges"][cohort["cohort_key"]] == []
    assert all(not p for p in payload["percentiles"][cohort["cohort_key"]].values())


def test_inferred_condition_columns_do_not_replace_originals(synthetic_conn):
    conn = synthetic_conn
    conn.execute("ALTER TABLE cohorts ADD COLUMN inferred_temperature_c REAL")
    conn.execute("UPDATE cohorts SET temperature_c=NULL, inferred_temperature_c=25")
    payload = _collect(conn)
    cohort = payload["cohorts"][0]
    assert cohort["temperature_c"] is None
    assert "inferred_temperature_c" not in cohort
    assert cohort["comparison_exclusions"] == ["missing_condition:temperature_c"]
    assert not payload["electrochem_edges"][cohort["cohort_key"]]


def test_condition_gating_is_per_cohort_and_respects_filter(synthetic_conn):
    conn = synthetic_conn
    ck = conn.execute("SELECT cohort_key FROM cohorts").fetchone()[0]
    conn.execute("INSERT INTO cohorts SELECT 'missing', test_type, NULL, c_rate_class, "
                 "v_window, n_designs, n_cells FROM cohorts")
    conn.execute("INSERT INTO design_metrics SELECT design_id, 'missing', metric_id, "
                 "window, n_cells, mean, sd, median, min, max FROM design_metrics")
    # Zero Celsius is known, not a falsey placeholder.
    conn.execute("UPDATE cohorts SET temperature_c=0 WHERE cohort_key=?", (ck,))
    payload = _collect(conn)
    assert payload["cohorts"][0]["cohort_key"] == ck
    assert payload["cohorts"][0]["comparison_eligible"] is True
    assert payload["electrochem_edges"][ck]
    assert payload["percentiles"][ck]["D1"]
    assert not payload["electrochem_edges"]["missing"]
    filtered = _collect(conn, cohort_filter="missing")
    assert len(filtered["cohorts"]) == 1
    assert set(filtered["design_metrics"]) == {"missing"}
    assert filtered["electrochem_edges"] == {"missing": []}


def _add_censor_metadata(conn):
    for field, dtype in (
        ("n_observed_cells", "INTEGER"), ("n_censored_cells", "INTEGER"),
        ("censor_lower_bound_min", "REAL"), ("censor_lower_bound_max", "REAL"),
    ):
        conn.execute(f"ALTER TABLE design_metrics ADD COLUMN {field} {dtype}")
    conn.execute("UPDATE design_metrics SET n_observed_cells=n_cells, n_censored_cells=0")


def _keep_capacity_and_cycles(conn):
    conn.execute("DELETE FROM design_metrics WHERE metric_id NOT IN ('cap_initial_ah', 'retention_pct')")
    conn.execute("UPDATE design_metrics SET metric_id='cycles_to_80pct', window='' "
                 "WHERE metric_id='retention_pct'")
    conn.execute("INSERT INTO metric_defs VALUES ('cycles_to_80pct','80% 수명','cycle',1,'first crossing')")


@pytest.mark.parametrize("stale_mean", [None, 999.0])
@pytest.mark.parametrize("metric_id", ["cycles_to_80pct", "cap_initial_ah"])
def test_censored_metrics_excluded_even_with_stale_means(synthetic_conn, stale_mean, metric_id):
    conn = synthetic_conn
    _keep_capacity_and_cycles(conn)
    _add_censor_metadata(conn)
    original = _collect(conn)
    ck = original["cohorts"][0]["cohort_key"]
    assert len(original["electrochem_edges"][ck]) == 3
    conn.execute(
        "UPDATE design_metrics SET n_observed_cells=3, n_censored_cells=1, "
        "censor_lower_bound_min=120, censor_lower_bound_max=150, mean=? "
        "WHERE design_id='D1' AND metric_id=?", (stale_mean, metric_id),
    )
    before = conn.execute("SELECT * FROM design_metrics").fetchall()
    payload = _collect(conn)
    metric = payload["design_metrics"][ck]["D1"][metric_id]
    assert metric["mean"] == stale_mean
    assert metric["n_cells"] == 4
    assert metric["n_observed_cells"] == 3
    assert metric["n_censored_cells"] == 1
    assert metric["censor_lower_bound_min"] == 120
    assert metric["censor_lower_bound_max"] == 150
    assert metric["comparison_exclusions"] == ["censored_cells"]
    assert metric_id not in payload["percentiles"][ck]["D1"]
    assert metric_id in payload["percentiles"][ck]["D2"]
    # D1 now shares only one safe metric; D2-D3 still share two.
    assert len(payload["electrochem_edges"][ck]) == 1
    edge = payload["electrochem_edges"][ck][0]
    assert {edge["source"], edge["target"]} == {"D2", "D3"}
    assert edge["shared"] == 2
    assert conn.execute("SELECT * FROM design_metrics").fetchall() == before


def test_legacy_cycles_excluded_but_ordinary_metrics_still_rank(synthetic_conn):
    _keep_capacity_and_cycles(synthetic_conn)
    payload = _collect(synthetic_conn)
    ck = payload["cohorts"][0]["cohort_key"]
    assert payload["electrochem_edges"][ck] == []
    for did, metrics in payload["design_metrics"][ck].items():
        cycles = metrics["cycles_to_80pct"]
        assert cycles["mean"] is not None
        assert "n_censored_cells" not in cycles
        assert cycles["comparison_exclusions"] == ["censoring_metadata_missing"]
        assert set(payload["percentiles"][ck][did]) == {"cap_initial_ah"}
        assert metrics["cap_initial_ah"]["comparison_exclusions"] == []


def test_uncensored_cycles_with_metadata_remain_comparable(synthetic_conn):
    _keep_capacity_and_cycles(synthetic_conn)
    _add_censor_metadata(synthetic_conn)
    payload = _collect(synthetic_conn)
    ck = payload["cohorts"][0]["cohort_key"]
    assert len(payload["electrochem_edges"][ck]) == 3
    for did, metrics in payload["design_metrics"][ck].items():
        assert metrics["cycles_to_80pct"]["comparison_exclusions"] == []
        assert metrics["cycles_to_80pct"]["censor_lower_bound_min"] is None
        assert set(payload["percentiles"][ck][did]) == {"cap_initial_ah", "cycles_to_80pct"}


@pytest.mark.parametrize(("observed", "censored", "reason"), [
    (4, None, "censoring_metadata_missing"),
    (None, 0, "censoring_metadata_missing"),
    (3, 0, "censoring_metadata_invalid"),
    (4, -1, "censoring_metadata_invalid"),
])
def test_unverified_censor_metadata_does_not_enable_cycles(synthetic_conn, observed, censored, reason):
    _keep_capacity_and_cycles(synthetic_conn)
    _add_censor_metadata(synthetic_conn)
    synthetic_conn.execute(
        "UPDATE design_metrics SET n_observed_cells=?, n_censored_cells=? "
        "WHERE metric_id='cycles_to_80pct' AND design_id='D1'", (observed, censored),
    )
    payload = _collect(synthetic_conn)
    ck = payload["cohorts"][0]["cohort_key"]
    assert payload["design_metrics"][ck]["D1"]["cycles_to_80pct"]["comparison_exclusions"] == [reason]
    assert "cycles_to_80pct" not in payload["percentiles"][ck]["D1"]
    assert all("D1" not in (e["source"], e["target"]) for e in payload["electrochem_edges"][ck])


def test_all_censored_cohort_not_useful(synthetic_conn):
    _add_censor_metadata(synthetic_conn)
    synthetic_conn.execute("UPDATE design_metrics SET n_censored_cells=n_cells, n_observed_cells=0")
    payload = _collect(synthetic_conn)
    cohort = payload["cohorts"][0]
    ck = cohort["cohort_key"]
    assert cohort["comparison_eligible"] is True  # Conditions are known.
    assert cohort["useful"] is False
    assert payload["electrochem_edges"][ck] == []
    assert all(not p for p in payload["percentiles"][ck].values())


@pytest.mark.parametrize(("values", "expected"), [
    ([7], [50]), ([7, 7, 7], [50, 50, 50]),
    ([1, 2, 3], [0, 50, 100]), ([1, 1, 3], [25, 25, 100]),
    ([1, 3, 3], [0, 75, 75]), ([1, 2, 2, 3], [0, 50, 50, 100]),
])
@pytest.mark.parametrize("direction", [True, False, None])
def test_percentiles_use_direction_corrected_midranks(values, expected, direction):
    dmap = {str(i): {"metric|100": {"mean": value}} for i, value in enumerate(values)}
    percentiles = _percentiles(dmap, {"metric": direction})
    for i, pct in enumerate(expected):
        assert percentiles[str(i)]["metric|100"] == {
            "pct": 100 - pct if direction is False else pct,
            "flipped": None if direction is None else not direction,
        }


def test_percentiles_directly_exclude_censored_and_legacy_cycles():
    dmap = {
        "A": {"ordinary": {"mean": 99, "n_censored_cells": 1},
              "cycles_to_80pct": {"mean": 900}},
        "B": {"ordinary": {"mean": 2}},
        "C": {"ordinary": {"mean": 4}},
    }
    pc = _percentiles(dmap, {"ordinary": True, "cycles_to_80pct": True})
    assert pc["A"] == {}
    assert pc["B"]["ordinary"]["pct"] == 0
    assert pc["C"]["ordinary"]["pct"] == 100


def test_html_wires_visible_condition_and_censoring_warnings(synthetic_conn):
    synthetic_conn.execute("UPDATE cohorts SET c_rate_class='0.5C/C?'")
    _keep_capacity_and_cycles(synthetic_conn)
    html = render_html(_collect(synthetic_conn))
    # Check the executable UI, not just the embedded JSON payload.
    ui = html[html.index('<script>\n"use strict";'):]
    assert 'id="comparisonWarning"' in html
    assert 'document.getElementById("comparisonWarning").textContent' in ui
    assert 'cohort.comparison_eligible === false' in ui
    assert 'cohort.comparison_exclusions||[]).map(comparisonReason)' in ui
    assert 'temperature_c:"온도"' in ui and 'c_rate_class:"C-rate"' in ui
    assert '중도절단 정보 없음 — 비교 제외' in ui
    assert '중도절단 포함 — 비교 제외' in ui
    assert 'c.censor_lower_bound_min' in ui and 'c.n_censored_cells' in ui
    assert 'const fa=fmtCell(ca), fb=fmtCell(cb)' in ui
    assert 'if(!cohortComparable()) note="조건 미확인 — 비교 제외"' in ui
    # Excluded ranks must not become zero-valued radar vertices.
    assert 'sel.every(id=>pc[id]&&pc[id][k])' in ui
    assert '공통 지표(≥3)가 부족합니다.</p>\'; return;' in ui
