import json

import pandas as pd

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


def test_low_ice_and_dead_channels_are_defects_with_three_or_more_channels():
    good = [(0.10, 0.085), (0.085, 0.084)]
    q = cell_qc.screen(_cycles({
        "t1:ch1": good, "t1:ch2": good, "t1:ch3": [(0.10, 0.086), (0.086, 0.085)],
        "t1:ch4": [(0.10, 0.060), (0.06, 0.059)],           # ICE 60 % vs 85 %
        "t1:ch5": [(0.10, 0.0)],                            # charged, never discharged
    }), None, None)
    s = _status(q)
    assert s["t1:ch4"] == "defect" and "low_ice" in q.set_index("run_id").at["t1:ch4", "qc_reasons"]
    assert s["t1:ch5"] == "defect" and "dead" in q.set_index("run_id").at["t1:ch5", "qc_reasons"]
    assert s["t1:ch1"] == s["t1:ch2"] == s["t1:ch3"] == "ok"   # dead channel is not in the reference


def test_two_channel_test_needs_a_larger_gap_and_charge_only_protocols_are_not_dead():
    q = cell_qc.screen(_cycles({
        "t2:a": [(0.10, 0.085)], "t2:b": [(0.10, 0.078)],     # 7 %p lower -> only suspect
        "t3:a": [(0.10, 0.085)], "t3:b": [(0.10, 0.060)],     # 25 %p lower -> defect
        "t4:a": [(0.10, 0.0)], "t4:b": [(0.10, 0.0)],         # every channel charge-only
    }), None, None)
    s = _status(q)
    assert s["t2:b"] == "suspect" and s["t2:a"] == "ok"
    assert s["t3:b"] == "defect"
    assert s["t4:a"] == s["t4:b"] == "ok"


def test_majority_rule_holds_verdict_and_capacity_alone_is_never_defect():
    good, low = [(0.10, 0.085)], [(0.10, 0.065)]
    q = cell_qc.screen(_cycles({
        "t5:a": good, "t5:b": low, "t5:c": low,               # 2 of 3 low -> which side is normal?
        "t6:a": good, "t6:b": good, "t6:c": [(0.07, 0.0595)],  # same ICE, 0.7x capacity (mass)
    }), None, None)
    s = _status(q)
    assert s["t5:b"] == s["t5:c"] == "suspect"
    assert s["t6:c"] == "suspect" and "low_capacity" in q.set_index("run_id").at["t6:c", "qc_reasons"]


def test_cycling_metrics_compare_same_protocol_and_overrides_win(tmp_path):
    cyc = _cycles({f"t7:{c}": [(0.10, 0.085)] for c in "abcd"})
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
    assert s["t7:d"] == "ok"   # other protocol: nothing to compare with

    project = tmp_path / "p"
    (project / "db").mkdir(parents=True)
    cyc.to_parquet(project / "db" / "run_cycles.parquet")
    prof.to_parquet(project / "db" / "run_profile.parquet")
    met.to_parquet(project / "db" / "run_metrics.parquet")
    first = cell_qc.build_cell_qc(project)
    assert first["defect"] == 1
    crit = json.loads((project / "criteria.json").read_text(encoding="utf-8"))
    assert crit["cell_qc"]["ice_drop_pp"] == 5.0                  # template written for the user
    override = project / cell_qc.OVERRIDE_FILE
    assert override.exists()
    pd.DataFrame([{"run_id": "t7:c", "decision": "keep", "note": "육안 확인 정상"},
                  {"run_id": "t7:a", "decision": "exclude", "note": "조립 불량"}]).to_csv(override, index=False)
    crit["cell_qc"]["ce_drop_pp"] = 10.0
    (project / "criteria.json").write_text(json.dumps(crit), encoding="utf-8")
    cell_qc.build_cell_qc(project)
    status = cell_qc.load_status(project)
    assert status["t7:c"][0] == "ok" and status["t7:a"][0] == "defect"
    q2 = pd.read_parquet(cell_qc.output_path(project)).set_index("run_id")
    assert q2.at["t7:c", "qc_source"] == "user" and "low_ce" not in q2.at["t7:c", "qc_reasons"]
