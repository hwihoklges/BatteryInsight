import json

import pandas as pd

from battery_insight.slide_facts import extract_sets, metric_family, parse_dates


def run(table="", text=""):
    df = pd.DataFrame([{"file_path": "a.pptx", "slide_no": 1, "text": text, "table_text": table}])
    return extract_sets(df)


def row(df, n):
    r = df[df.set_no == n].iloc[0]
    return r, json.loads(r["attrs"]), json.loads(r.metrics)


T1 = (
    "Capacity (mAh/g) | Discharge @1st | Discharge @100cyc | Eff (%) @100cyc\n"
    "Set1:건식_PTFE1.5_6:4 _가원가기 | 177.1 | 162.6 | 100\n"
    "Set2: 건식 PTFE1.5_7:3 _가원가기 | 172.5 | 143.1 | 99.4\n"
    "Cycle | Retention @100cyc\n"
    "Set1:건식_PTFE1.5_6:4_가원가기 | 91.8\n"
)


def test_rows_and_stacked_blocks():
    df = run(T1)
    assert len(df) == 2
    r, a, m = row(df, 1)
    assert m == {"discharge_1st_mah_g": 177.1, "discharge_100cyc_mah_g": 162.6,
                 "ce_100cyc_pct": 100.0, "retention_100cyc_pct": 91.8}
    assert a["ptfe_wt"] == 1.5 and a["ratio"] == "6:4" and a["wet_dry"] == "dry"
    assert "ptfe1.5" in r.label_tokens.split() and "6:4" in r.label_tokens.split()
    assert r.source == "table_row"
    _, a2, m2 = row(df, 2)
    assert a2["ratio"] == "7:3" and "retention_100cyc_pct" not in m2


def test_first_cycle_table_and_porosity():
    df = run("Capacity (mAh/g) | 1st Charge | 1st Discharge | Eff (%)\nSet1:건식_PTFE1.5_P30% | 225.2 | 202.6 | 90.0")
    _, a, m = row(df, 1)
    assert m == {"charge_1st_mah_g": 225.2, "discharge_1st_mah_g": 202.6, "ice_pct": 90.0}
    assert a["porosity_pct"] == 30 and a["ptfe_wt"] == 1.5


def test_rate_and_date():
    df = run("Rate | 1.0C (%) | 0.1C (%)\nSet1:SJ1300A_65_오창제조(7/8) | 88.6 | 99.0")
    r, a, m = row(df, 1)
    assert m == {"rate_1c_pct": 88.6, "rate_0.1c_pct": 99.0}
    assert r.dates == "0708"
    assert a["materials"] == ["SJ1300A"] and a["site"] == "오창"
    assert "sj1300a" in r.label_tokens.split() and "오창제조" in r.label_tokens.split()


def test_transposed_columns():
    t = ("구분 | Set1_Wet_B2.5 | Set2_Dry_PTFE1.0 | Set3_Dry_PTFE2.0\n"
         "Porosity (%) | - | 42.6 | 42.9\nLoading (mAh/cm2) | - | 4.85 | 4.78")
    df = run(t)
    assert len(df) == 3
    r, a, _ = row(df, 2)
    assert a["porosity_pct"] == 42.6 and a["loading_mah_cm2"] == 4.85 and a["wet_dry"] == "dry"
    assert a["ptfe_wt"] == 1.0 and r.source == "table_col"
    assert "porosity_pct" not in row(df, 1)[1]


def test_set_number_header():
    df = run("Set# | 1 | 2 | 3 | 4\n전해질 | X | O | O | X\nLoading (mAh/cm2) | 4.1 | 4.2 | - | 4.4")
    assert sorted(df.set_no) == [1, 2, 3, 4]
    assert json.loads(df[df.set_no == 2].iloc[0]["attrs"])["x:전해질"] == "O"
    assert json.loads(df[df.set_no == 4].iloc[0]["attrs"])["loading_mah_cm2"] == 4.4


def test_unnumbered_label_row():
    df = run("Cycle | Retention @100cyc\nREF | 95.0\n동시투입 저섬유화 | 96.5")
    assert df.set_no.isna().all() and len(df) == 2
    assert json.loads(df[df.set_label == "REF"].iloc[0].metrics)["retention_100cyc_pct"] == 95.0


def test_replicates_mean():
    df = run("Capacity (mAh/g) | Discharge @1st\nSet1:A | 100\n | 110\n | 120\nSet2:B | 90")
    r, _, m = row(df, 1)
    assert m["discharge_1st_mah_g"] == 110.0 and r.n_values == 3
    assert row(df, 2)[0].n_values == 1


def test_text_lines_and_merge():
    text = ("Set1: SJ1300A 65% 오창제조 (7/8 투입) ¶ Set2: SJ1300A 70% ¶ "
            "Chamber 45℃, 전압범위 4.2-2.5V에서 0.1C/0.1C로 Mono cell 초기평가 ¶ #1~#3 셀")
    t = "Rate | 1.0C (%)\nSet1:SJ1300A_65 | 88.6"
    df = run(t, text)
    assert len(df) == 2
    r, a, m = row(df, 1)
    assert r.source == "table_row" and m["rate_1c_pct"] == 88.6 and r.dates == "0708"
    assert a["temperature_c"] == 45 and a["voltage_range"] == "2.5-4.2"
    assert row(df, 2)[0].source == "text"


def test_dates_and_family():
    assert parse_dates("260708 2026.07.08 25.10.20. 7월8일 7/8") == ["20260708", "20251020", "0708"]
    assert metric_family("retention_100cyc_pct") == ("retention_pct", 100)
    assert metric_family("ice_pct") == ("ice_pct", 1)
    assert metric_family("rate_0.1c_pct") == ("rate_pct", 0.1)
    assert metric_family("discharge_1st_mah_g") == ("discharge_mah_g", 1)

def test_multi_value_cell_and_int_set_rows():
    df = run("Set No. | Loading (mAh/cm2) | Discharge @1st\nSet1:A | 4.86 4.88 4.88 | 100 110\nSet#2-4 | 1 | 2")
    _, a, m = row(df, 1)
    assert abs(a["loading_mah_cm2"] - 4.8733) < 1e-3 and m["discharge_1st_mah_g"] == 105.0
    df = run("Set | Retention @100cyc\n1 | 98.5\n | 98.7\nAvg | 98.6\n2 | 97.0")
    r, _, m = row(df, 1)
    assert m["retention_100cyc_pct"] == 98.6 and r.n_values == 2
    assert sorted(df.set_no) == [1, 2]


def test_grouped_transposed_dcir():
    t = "Resistance (Ω) | Set1:A |  | Set2:B | \nSOC | 30s | 0.1s | 30s | 0.1s\n80 | 3.0 | 1.4 | 2.6 | 1.3"
    df = run(t)
    _, _, m1 = row(df, 1)
    _, _, m2 = row(df, 2)
    assert m1 == {"dcir_soc_80_30s": 3.0, "dcir_soc_80_0.1s": 1.4}
    assert m2["dcir_soc_80_30s"] == 2.6


