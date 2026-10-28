import json
from io import BytesIO
from pathlib import Path

import pandas as pd
import pytest

from battery_insight import cell_qc


def _cycles(spec):
    """spec: run_id -> list of (charge_ah, discharge_ah) per cycle."""
    rows = []
    for rid, cyc in spec.items():
        for i, (chg, dchg) in enumerate(cyc, 1):
            rows.append({"run_id": rid, "cycle_index": i, "charge_capacity_ah": chg, "discharge_capacity_ah": dchg})
    return pd.DataFrame(rows)


def _status(q):
    return dict(zip(q["run_id"], q["qc_status"]))


def _profile(cycles):
    return pd.DataFrame({"run_id": cycles["run_id"].unique(), "protocol_signature": "formation-A"})


def _screen(spec, **kwargs):
    cycles = _cycles(spec)
    return cell_qc.screen(cycles, _profile(cycles), None, **kwargs)


def test_low_ice_is_relative_outlier_but_dead_channel_is_unconfirmed():
    good = [(0.10, 0.085), (0.085, 0.084)]
    q = _screen({
        "t1:ch1": good, "t1:ch2": good, "t1:ch3": [(0.10, 0.086), (0.086, 0.085)],
        "t1:ch4": [(0.10, 0.060), (0.06, 0.059)],           # ICE 60 % vs 85 %
        "t1:ch5": [(0.10, 0.0)],                            # charged, never discharged
    })
    s = _status(q)
    assert s["t1:ch4"] == "defect" and "low_ice" in q.set_index("run_id").at["t1:ch4", "qc_reasons"]
    assert s["t1:ch5"] == "suspect" and "dead_unconfirmed" in q.set_index("run_id").at["t1:ch5", "qc_not_assessed"]
    assert s["t1:ch1"] == s["t1:ch2"] == s["t1:ch3"] == "ok"   # dead channel is not in the reference


def test_two_channel_test_needs_a_larger_gap_and_charge_only_is_unconfirmed():
    q = _screen({
        "t2:a": [(0.10, 0.085)] * 2, "t2:b": [(0.10, 0.078)] * 2,
        "t3:a": [(0.10, 0.085)] * 2, "t3:b": [(0.10, 0.060)] * 2,
        "t4:a": [(0.10, 0.0)], "t4:b": [(0.10, 0.0)],         # every channel charge-only
    })
    s = _status(q)
    assert s["t2:b"] == "suspect" and s["t2:a"] == "ok"
    assert s["t3:b"] == "defect"
    assert s["t4:a"] == s["t4:b"] == "suspect"
    assert not q.set_index("run_id").at["t4:a", "qc_assessed_rules"]


def test_majority_rule_holds_verdict_and_capacity_alone_is_never_defect():
    good, low = [(0.10, 0.085)] * 2, [(0.10, 0.065)] * 2
    q = _screen({
        "t5:a": good, "t5:b": low, "t5:c": low,               # 2 of 3 low -> which side is normal?
        "t6:a": good, "t6:b": good, "t6:c": [(0.07, 0.0595)],  # same ICE, 0.7x capacity (mass)
    })
    s = _status(q)
    assert s["t5:b"] == s["t5:c"] == "suspect"
    assert s["t6:c"] == "suspect" and "low_capacity" in q.set_index("run_id").at["t6:c", "qc_reasons"]


def test_cycling_metrics_compare_same_protocol_and_overrides_win(monkeypatch):
    cyc = _cycles({f"t7:{c}": [(0.10, 0.085)] * 2 for c in "abcd"})
    prof = pd.DataFrame({"run_id": [f"t7:{c}" for c in "abcd"],
                         "protocol_signature": ["0.5C", "0.5C", "0.5C", "1C"]})
    met = pd.DataFrame([
        *({"run_id": f"t7:{c}", "metric_id": "ce_median_pct", "value": v, "censored": False}
          for c, v in zip("abcd", [99.9, 99.8, 96.0, 95.0])),
        *({"run_id": f"t7:{c}", "metric_id": "cycles_to_80pct", "value": v, "censored": cen}
          for c, v, cen in zip("abcd", [200, 210, 15, 30], [True, True, False, False])),
    ])
    q = cell_qc.screen(cyc, prof, met)
    s, why = _status(q), q.set_index("run_id")["qc_reasons"]
    assert s["t7:c"] == "defect" and "low_ce" in why["t7:c"] and "early_fade" in why["t7:c"]
    assert s["t7:d"] == "not_assessed"

    project, files = _memory_project(monkeypatch, cyc, prof, met)
    first = cell_qc.build_cell_qc(project)
    assert first["defect"] == 1
    assert not (project / cell_qc.CRITERIA_FILE).exists()
    assert not (project / cell_qc.OVERRIDE_FILE).exists()
    monkeypatch.setattr(cell_qc, "load_overrides", lambda _: {
        "t7:c": ("keep", "육안 확인 정상"), "t7:a": ("exclude", "조립 불량")})
    monkeypatch.setattr(cell_qc, "load_criteria", lambda _: {"ce_drop_pp": 10.0})
    cell_qc.build_cell_qc(project)
    status = cell_qc.load_status(project)
    assert status["t7:c"][0] == "ok" and status["t7:a"][0] == "defect"
    q2 = files[cell_qc.output_path(project)].set_index("run_id")
    assert q2.at["t7:c", "qc_source"] == "user" and "low_ce" not in q2.at["t7:c", "qc_reasons"]
    assert q2.at["t7:c", "qc_auto"] == "defect" and q2.at["t7:c", "qc_status"] == "ok"
    assert "early_fade" in q2.at["t7:c", "qc_reasons"]


def _memory_project(monkeypatch, cycles=None, profile=None, metrics=None):
    """Exercise build/load contracts without writing any project or temporary files."""
    project = Path("qc_test_project")
    files = {project / "db" / name: frame.copy() for name, frame in (
        ("run_cycles.parquet", cycles), ("run_profile.parquet", profile),
        ("run_metrics.parquet", metrics)) if frame is not None}
    monkeypatch.setattr(Path, "exists", lambda path: path in files)
    monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: None)
    monkeypatch.setattr(pd, "read_parquet",
                        lambda path, columns=None: files[path].copy() if columns is None else files[path][columns].copy())
    monkeypatch.setattr(pd.DataFrame, "to_parquet",
                        lambda frame, path, **kw: files.__setitem__(path, frame.copy()))
    monkeypatch.setattr(Path, "write_text", lambda *a, **kw: pytest.fail("unexpected configuration write"))
    monkeypatch.setattr(pd.DataFrame, "to_csv", lambda *a, **kw: pytest.fail("unexpected configuration write"))
    return project, files


@pytest.mark.parametrize("initial", [(0.1, 0), (0.1, 0.03), (0.1, 0.06), (0, 0)])
def test_recovery_is_incomplete_start_not_defect(initial):
    q = _screen({"t:a": [(0.1, 0.085)] * 3, "t:b": [(0.1, 0.085)] * 3,
                 "t:c": [initial, (0.085, 0.084), (0.084, 0.083)]}).set_index("run_id")
    assert q.at["t:c", "incomplete_start"]
    assert q.at["t:c", "qc_status"] == "suspect"
    assert "incomplete_start" in q.at["t:c", "qc_reasons"]
    assert "low_ice:" not in q.at["t:c", "qc_reasons"]
    assert q.at["t:a", "ice_others"] == pytest.approx(85)


def test_censored_survivor_counts_toward_majority_safeguard():
    cycles = _cycles({f"t:{ch}": [(0.1, 0.085)] * 3 for ch in "abc"})
    metrics = pd.DataFrame([
        dict(run_id=f"t:{ch}", metric_id="cycles_to_80pct", value=value, censored=censored)
        for ch, value, censored in [("a", 10, False), ("b", 11, False), ("c", 100, True)]
    ])
    q = cell_qc.screen(cycles, _profile(cycles), metrics).set_index("run_id")
    assert q.at["t:a", "qc_status"] == q.at["t:b", "qc_status"] == "suspect"
    assert "majority ambiguous" in q.at["t:a", "qc_reasons"]


@pytest.mark.parametrize("initial", [(None, None), (0.1, None), (None, 0.08)])
def test_missing_capacity_is_not_zero_or_a_dead_channel(initial):
    q = _screen({"t:a": [(0.1, 0.085)] * 2, "t:b": [initial]}).set_index("run_id")
    assert q.at["t:b", "qc_auto"] == "not_assessed"
    assert "dead" not in q.at["t:b", "qc_reasons"]
    assert pd.isna(q.at["t:b", "ice_pct"])
    assert pd.isna(q.at["t:a", "ice_others"])


@pytest.mark.parametrize("dtype", ["Float64", "Float32"])
def test_nullable_missing_capacities_are_not_assessed(dtype):
    cycles = _cycles({"t:a": [(0.1, 0.085)] * 2, "t:b": [(None, None)]})
    for col in ("charge_capacity_ah", "discharge_capacity_ah"):
        cycles[col] = cycles[col].astype(dtype)
    cycles["cycle_index"] = cycles["cycle_index"].astype("Int64")
    before = cycles.copy(deep=True)
    q = cell_qc.screen(cycles, _profile(cycles), None).set_index("run_id")
    assert q.at["t:b", "qc_status"] == "not_assessed"
    assert pd.isna(q.at["t:b", "ice_pct"])
    pd.testing.assert_frame_equal(cycles, before)


def test_first_row_not_reassembled_from_different_cycles_and_inputs_unchanged():
    c = _cycles({"t:a": [(0.1, None), (0.08, 0.079)], "t:b": [(0.1, 0.085)] * 2})
    p = _profile(c)
    before_c, before_p = c.copy(deep=True), p.copy(deep=True)
    q = cell_qc.screen(c, p, None).set_index("run_id")
    assert pd.isna(q.at["t:a", "ice_pct"])
    assert pd.isna(q.at["t:a", "dchg1_ah"])
    assert q.at["t:a", "incomplete_start"]
    pd.testing.assert_frame_equal(c, before_c)
    pd.testing.assert_frame_equal(p, before_p)


@pytest.mark.parametrize("kind", ["is_full", "index", "single"])
def test_incomplete_or_lone_cycles_cannot_give_automatic_defect(kind):
    c = _cycles({"t:a": [(0.1, 0.085)] * 2, "t:b": [(0.1, 0.06)] * (1 if kind == "single" else 2)})
    if kind == "is_full":
        c["is_full"] = c["run_id"].eq("t:a")
    elif kind == "index":
        c.loc[c["run_id"].eq("t:b"), "cycle_index"] += 4
    q = cell_qc.screen(c, _profile(c), None).set_index("run_id")
    assert q.at["t:b", "qc_status"] in ("not_assessed", "suspect")


@pytest.mark.parametrize("signature", [None, "", " ", "unknown", "?C/?C|None-4.2V|cont"])
def test_unknown_protocols_are_not_shared_reference_groups(signature):
    c = _cycles({"t:a": [(0.1, 0.085)] * 2, "t:b": [(0.1, 0.06)] * 2})
    p = _profile(c).assign(protocol_signature=signature)
    m = pd.DataFrame({"run_id": ["t:a", "t:b"], "metric_id": "ce_median_pct", "value": [100, 90]})
    q = cell_qc.screen(c, p, m)
    assert set(q["qc_status"]) == {"not_assessed"}
    assert q[["ice_others", "ce_others", "cap_rel"]].isna().all().all()


def test_formation_context_is_not_pooled_even_with_minimum_siblings_one():
    q = _screen({"t:a": [(0.1, 0.99)] * 2, "t:b": [(0.1, 0.099)] * 2,
                 "t:c": [(0.1, 0.07)] * 2}, criteria={"min_siblings_defect": 1}).set_index("run_id")
    assert q.at["t:c", "qc_status"] == "not_assessed"
    assert pd.isna(q.at["t:c", "ice_others"])
    assert q.at["t:a", "qc_status"] == "not_assessed"  # invalid ICE, not reference evidence


def test_different_protocols_also_do_not_share_formation_references():
    c = _cycles({"t:a": [(0.1, 0.085)] * 2, "t:b": [(0.1, 0.06)] * 2})
    p = _profile(c)
    p["protocol_signature"] = ["formation-A", "formation-B"]
    assert set(cell_qc.screen(c, p, None)["qc_status"]) == {"not_assessed"}


@pytest.mark.parametrize("unavailable", ["dead", "missing", "quality", "excluded"])
def test_unavailable_peers_cannot_supply_cycling_references(unavailable):
    c = _cycles({"t:a": [(0.1, 0.085)] * 2, "t:b": [(0.1, 0.085)] * 2})
    p = _profile(c)
    overrides = None
    if unavailable == "dead":
        c.loc[c["run_id"].eq("t:a"), "discharge_capacity_ah"] = 0
    elif unavailable == "missing":
        c = c[c["run_id"].eq("t:b")]
    elif unavailable == "quality":
        p["quality"] = ["insufficient", "ok"]
    else:
        overrides = {"t:a": ("exclude", "reviewed")}
    m = pd.DataFrame([
        {"run_id": rid, "metric_id": mid, "value": value, "censored": False}
        for rid, ce, life in [("t:a", 100, 200), ("t:b", 90, 5)]
        for mid, value in [("ce_median_pct", ce), ("cycles_to_80pct", life)]])
    q = cell_qc.screen(c, p, m, overrides=overrides).set_index("run_id")
    assert q.at["t:b", "qc_status"] == "not_assessed"
    assert pd.isna(q.at["t:b", "ce_others"])
    assert pd.isna(q.at["t:b", "c80_others"])


@pytest.mark.parametrize("censored,expected", [("false", "defect"), ("true", "ok"),
                                             (None, "ok"), ("garbage", "ok")])
def test_censor_flags_are_parsed_not_truth_cast(censored, expected):
    c = _cycles({"t:a": [(0.1, 0.085)] * 2, "t:b": [(0.1, 0.085)] * 2})
    m = pd.DataFrame({"run_id": ["t:a", "t:b"], "metric_id": "cycles_to_80pct",
                      "value": [200, 5], "censored": [True, censored]})
    q = cell_qc.screen(c, _profile(c), m).set_index("run_id")
    assert q.at["t:b", "qc_status"] == expected
    if expected == "ok":
        assert "early_fade" not in q.at["t:b", "qc_assessed_rules"]


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), "bad"])
def test_invalid_metric_values_cannot_create_or_supply_outliers(value):
    c = _cycles({"t:a": [(0.1, 0.085)] * 2, "t:b": [(0.1, 0.085)] * 2})
    m = pd.DataFrame({"run_id": ["t:a", "t:b"], "metric_id": "ce_median_pct", "value": [100, value]})
    q = cell_qc.screen(c, _profile(c), m)
    assert set(q["qc_status"]) == {"ok"}
    assert q["ce_dev"].isna().all()


def test_profile_only_and_dataset_runs_preserved_and_human_overrides_win(monkeypatch):
    p = pd.DataFrame({"run_id": ["t:a", "t:b", "other:a", "ds:unlinked"]})
    project, files = _memory_project(monkeypatch, profile=p)
    monkeypatch.setattr(cell_qc, "load_overrides", lambda _: {
        "t": ("exclude", "test review"), "t:a": ("keep", "run review"), "ds:unlinked": ("exclude", "dataset review")})
    result = cell_qc.build_cell_qc(project)
    assert result["runs"] == 4 and result["user_overrides"] == 3 and result["not_assessed"] == 1
    q = files[cell_qc.output_path(project)].set_index("run_id")
    assert set(q["qc_auto"]) == {"not_assessed"}
    assert q.at["t:a", "qc_status"] == "ok" and q.at["t:b", "qc_status"] == "defect"
    assert q.at["ds:unlinked", "qc_status"] == "defect"
    assert q.at["t:a", "qc_note"] == "run review"
    assert cell_qc.load_status(project)["other:a"][0] == "not_assessed"
    pd.testing.assert_frame_equal(files[project / "db" / "run_profile.parquet"], p)


def test_all_1537_profiles_are_retained_with_1342_cycle_runs():
    c = _cycles({f"t{i}:a": [(0.1, 0.085)] for i in range(1342)})
    p = pd.DataFrame({"run_id": [f"t{i}:a" for i in range(1537)]})
    q = cell_qc.screen(c, p, None, overrides={"t1536:a": ("exclude", "reviewed")})
    assert len(q) == 1537 and q["run_id"].is_unique
    assert q["n_cycles"].eq(0).sum() == 195
    assert _status(q)["t1536:a"] == "defect"


@pytest.mark.parametrize("c", [pd.DataFrame(), pd.DataFrame(columns=["run_id"]),
                             _cycles({"ds:x": [(0.1, 0.08)]})])
def test_empty_and_dataset_only_inputs_safe(c):
    q = cell_qc.screen(c, None, None)
    assert list(q.columns) == cell_qc.OUTPUT_COLUMNS
    assert q.empty or set(q["qc_status"]) == {"not_assessed"}


def test_empty_build_replaces_stale_qc_in_memory(monkeypatch):
    project, files = _memory_project(monkeypatch)
    files[cell_qc.output_path(project)] = pd.DataFrame({"run_id": ["old:a"], "qc_status": ["defect"]})
    assert cell_qc.build_cell_qc(project)["runs"] == 0
    assert cell_qc.load_status(project) == {}


@pytest.mark.parametrize("criteria", [[], False, {"typo": 1}, {"dead_ah": 0}, {"ice_drop_pp": "5"},
    {"ce_drop_pp": True}, {"cap_low": 1}, {"cap_high": 0.9}, {"fade_ratio": 1},
    {"majority_share": 0.4}, {"min_siblings_defect": 1.5}, {"fade_min_cycles": 2.5},
    {"single_sibling_factor": 0.5}, {"dead_ice_pct": 60}, {"ice_drop_pp": float("nan")},
    {"ce_max_valid": float("inf")}])
def test_bad_criteria_rejected_even_on_empty_input(criteria):
    with pytest.raises(ValueError):
        cell_qc.screen(pd.DataFrame(), None, None, criteria=criteria)


@pytest.mark.parametrize("text", ["{", "[]", "null", '{"cell_qc": null}', '{"cell_qc": []}',
    '{"cell_qc":{"ice_drop_pp":NaN}}', '{"cell_qc":{"ice_drop_pp":5,"ice_drop_pp":6}}'])
def test_malformed_criteria_file_errors_without_writes(monkeypatch, text):
    monkeypatch.setattr(Path, "exists", lambda _: True)
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: text)
    monkeypatch.setattr(Path, "write_text", lambda *a, **kw: pytest.fail("must not overwrite input"))
    with pytest.raises(ValueError, match="criteria.json"):
        cell_qc.load_criteria(Path("qc_test_project"))


def test_missing_optional_config_and_unrelated_criteria_are_read_only(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda _: False)
    monkeypatch.setattr(Path, "write_text", lambda *a, **kw: pytest.fail("must not write"))
    monkeypatch.setattr(Path, "mkdir", lambda *a, **kw: pytest.fail("must not mkdir"))
    assert cell_qc.load_overrides("qc_test_project") == {}
    assert cell_qc.load_criteria("qc_test_project") == cell_qc.DEFAULTS
    monkeypatch.setattr(Path, "exists", lambda _: True)
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: json.dumps({"unrelated": 123}))
    assert cell_qc.load_criteria("qc_test_project") == cell_qc.DEFAULTS


@pytest.mark.parametrize("text", ["", "run_id,note\nt:a,x\n",
    "run_id,decision\nt:a,kep\n", "run_id,decision\n,exclude\n",
    "run_id,test_key,decision\nt:a,t,keep\n", "test_key,decision\nt:a,exclude\n",
    "run_id,decision\nt:a,keep\nt:a,exclude\n", "run_id,decision\nNA,keep\n",
    "run_id,decision,note\nwrong-id,t:a,exclude,review\n",
    "run_id,decision,note\nt:a,keep\n", "run_id,decision,decision\nt:a,keep,exclude\n",
    'run_id,decision,note\nt:a,keep,"unterminated\n'])
def test_bad_override_csv_is_not_silently_ignored(monkeypatch, text):
    monkeypatch.setattr(Path, "exists", lambda _: True)
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: text)
    with pytest.raises(ValueError):
        overrides = cell_qc.load_overrides("qc_test_project")
        cell_qc.screen(_cycles({"t:a": [(0.1, 0.08)]}), None, None, overrides=overrides)


def test_explicit_test_key_override_csv_and_header_only_template(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda _: True)
    text = "run_id,test_key,decision,note\n,t,EXCLUDE,test\nt:a,,keep,run\n"
    monkeypatch.setattr(Path, "read_text", lambda *a, **kw: text)
    assert cell_qc.load_overrides("qc_test_project") == {"t": ("exclude", "test"), "t:a": ("keep", "run")}
    text = "run_id,decision,note\n"
    assert cell_qc.load_overrides("qc_test_project") == {}


@pytest.mark.parametrize("overrides", [[], False, {"typo:a": ("keep", "")}, {"t:a": ("typo", "")},
                                     {"t:a": "keep"}, {"t:a": ("keep", None)}, {" t:a": ("keep", "")}])
def test_bad_override_mappings_rejected(overrides):
    with pytest.raises(ValueError):
        cell_qc.screen(_cycles({"t:a": [(0.1, 0.08)]}), None, None, overrides=overrides)


def test_duplicate_profiles_cycles_and_metric_identities_fail_explicitly():
    c = _cycles({"t:a": [(0.1, 0.08)]})
    p = _profile(c)
    m = pd.DataFrame({"run_id": ["t:a"] * 2, "metric_id": ["ce_median_pct"] * 2, "value": [99, 98]})
    for cycles, profile, metrics in [(pd.concat([c, c]), p, None),
                                     (c, pd.concat([p, p]), None), (c, p, m)]:
        with pytest.raises(ValueError, match="duplicate"):
            cell_qc.screen(cycles, profile, metrics)


def test_excluded_run_cannot_change_majority_verdict_for_other_runs():
    spec = {"t:a": [(0.1, 0.085)] * 2, "t:b": [(0.1, 0.065)] * 2, "t:c": [(0.1, 0.065)] * 2}
    excluded = _screen(spec, overrides={"t:c": ("exclude", "reviewed")}).set_index("run_id")
    removed = _screen({rid: rows for rid, rows in spec.items() if rid != "t:c"}).set_index("run_id")
    assert excluded.at["t:b", "qc_auto"] == removed.at["t:b", "qc_auto"] == "defect"
    assert "majority ambiguous" not in excluded.at["t:b", "qc_reasons"]


def test_unrelated_profiles_do_not_dilute_majority_guard():
    c = _cycles({"t:a": [(0.1, 0.085)] * 2, "t:b": [(0.1, 0.065)] * 2, "t:c": [(0.1, 0.065)] * 2})
    p = pd.concat([_profile(c), pd.DataFrame({"run_id": [f"t:{i}" for i in range(20)]})], ignore_index=True)
    q = cell_qc.screen(c, p, None).set_index("run_id")
    assert q.at["t:b", "qc_auto"] == q.at["t:c", "qc_auto"] == "suspect"
    assert "majority ambiguous" in q.at["t:b", "qc_reasons"]


def test_schema_roundtrips_to_parquet_without_files():
    for cycles, profile in [(pd.DataFrame(), None),
                            (_cycles({"ds:a": [(0.1, 0.08)], "ds:b": [(0.1, 0.07)]}), None),
                            (pd.DataFrame(), pd.DataFrame({"run_id": ["t:a", "t:b"]}))]:
        q = cell_qc.screen(cycles, profile, None)
        restored = pd.read_parquet(BytesIO(q.to_parquet(index=False)))
        pd.testing.assert_frame_equal(q, restored)
        if len(q) and q["run_id"].str.startswith("ds:").all():
            assert q["n_channels"].eq(1).all()
