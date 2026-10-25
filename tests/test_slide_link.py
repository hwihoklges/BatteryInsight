import pandas as pd
from pptx import Presentation

from battery_insight import slide_link as sl


def _deck(path, slides):
    prs = Presentation()
    for title, body in slides:
        s = prs.slides.add_slide(prs.slide_layouts[5])
        s.shapes.title.text = title
        tb = s.shapes.add_textbox(0, 0, 100, 100)
        tb.text_frame.text = body
    prs.save(str(path))


def test_slide_link(tmp_path):
    root = tmp_path / "slides" / "홍길동"
    root.mkdir(parents=True)
    _deck(root / "260720 SJ1300B 평가.pptx", [
        ("SJ1300B MT-1 cycle 결과", "260707 Set1 SJ1300B MT-1 45도 cycle 용량 retention 90% mAh"),
        ("개요", "목차"),
        ("신규 소재 XZ77Q 용량", "XZ77Q 사이클 retention 효율 mAh 결과"),
    ])
    (root / "~$tmp.pptx").write_bytes(b"x")
    (root / "broken.pptx").write_bytes(b"not a zip")

    proj = tmp_path / "proj"
    (proj / "dx").mkdir(parents=True)
    pd.DataFrame([
        dict(test_key="k1", owner_name="홍길동", test_date="2026-07-07", test_type="Cycle", sample_name="SJ1300B MT-1 45도",
             tags="material=SJ1300B; material=MT-1", test_name="07005399_260707_Set1_SJ1300B_MT-1", context_path="홍길동",
             temperature_c=45.0, n_channels=4),
        dict(test_key="k2", owner_name="홍길동", test_date="2026-03-01", test_type="RPT", sample_name="QQ9ZZ 샘플",
             tags="material=QQ9ZZ", test_name="07005399_260301_Set1_QQ9ZZ", context_path="홍길동", n_channels=2),
    ]).to_csv(proj / "dx" / "dx_tests.csv", index=False)

    idx = sl.index_slides(tmp_path / "slides", tmp_path / "c.parquet", workers=2, log=lambda *_: None)
    assert idx["file_path"].nunique() == 2 and (idx["error"] != "").sum() == 1
    assert (idx["slide_no"] > 0).sum() == 3
    again = sl.index_slides(tmp_path / "slides", tmp_path / "c.parquet", workers=2, log=lambda *_: None)
    assert len(again) == len(idx)

    pairs = sl.match_tests(proj, idx)
    assert set(pairs["test_key"]) == {"k1"}
    assert pairs.iloc[0]["slide_no"] == 1 and pairs.iloc[0]["confidence"] == "matched", pairs.iloc[0].to_dict()

    res = sl.build_slide_link(proj, tmp_path / "slides", workers=2, log=lambda *_: None)
    assert list(res["matched"]["test_key"]) == ["k1"]
    assert list(res["tests_without_slides"]["test_key"]) == ["k2"]
    sa = res["slides_without_data"]
    assert list(sa["page"]) == [3], (res["links"].to_dict("records"), idx[["slide_no","n_result_words"]].values.tolist())
    assert (proj / "slides" / "slide_link_report.xlsx").exists()

def _table_deck(path, rows, author="", title="결과"):
    from pptx.util import Inches
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = title
    tbl = s.shapes.add_table(len(rows), len(rows[0]), Inches(0.5), Inches(1.5), Inches(8), Inches(3)).table
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            tbl.cell(i, j).text = str(v)
    prs.core_properties.author = author
    prs.core_properties.last_modified_by = author
    prs.save(str(path))


def test_multi_root_sets_numbers_and_db(tmp_path):
    import sqlite3
    import numpy as np
    t2 = tmp_path / "2팀" / "홍길동"
    t4 = tmp_path / "4팀"
    t2.mkdir(parents=True)
    t4.mkdir(parents=True)
    rows = [["Capacity (mAh/g)", "1st Charge", "1st Discharge", "Eff (%)"],
            ["Set1: PTFE1.5_ZQ81 (7/7)", "225.0", "202.5", "90.0"],
            ["Set2: PTFE1.5_ZQ82 (7/7)", "225.0", "196.0", "87.1"]]
    _table_deck(t2 / "260720 ZQ 평가.pptx", rows, title="ZQ81 ZQ82 초기 용량 결과")
    _table_deck(t4 / "260801 ZQ 4팀.pptx", rows, author="임꺽정(KKJ)[kkj]/책임", title="ZQ81 ZQ82 초기 용량 결과")
    _table_deck(t2 / "260721 남의 평가.pptx", rows, title="ZQ81 ZQ82 초기 용량 결과")  # different-named owner, same folder

    proj = tmp_path / "proj"
    (proj / "dx").mkdir(parents=True)
    (proj / "db").mkdir()
    pd.DataFrame([
        dict(test_key="a1", owner_name="홍길동", test_date="2026-07-07", set_no="1", sample_name="PTFE1.5 ZQ81",
             test_name="x_260707_Set1_ZQ81", context_path="홍길동", n_channels=2),
        dict(test_key="b2", owner_name="임꺽정", test_date="2026-07-07", set_no="2", sample_name="PTFE1.5 ZQ82",
             test_name="x_260707_Set2_ZQ82", context_path="", n_channels=2),
        dict(test_key="c3", owner_name="김아무", test_date="2026-01-01", set_no="1", sample_name="QQ77",
             test_name="x_260101_Set1_QQ77", context_path="", n_channels=1),
    ]).to_csv(proj / "dx" / "dx_tests.csv", index=False)
    # DB cycles: a1 ICE 90.0 % (agrees with Set1), b2 ICE 87.1 % (agrees with Set2)
    cyc = []
    for key, ice in (("a1", 0.900), ("b2", 0.871)):
        for n in range(1, 4):
            cyc.append(dict(run_id=f"{key}:ch1", cycle_index=n, discharge_capacity_ah=0.0045 * (ice if n == 1 else 1),
                            charge_capacity_ah=0.0045 * (1 if n == 1 else 1.001)))
    pd.DataFrame(cyc).to_parquet(proj / "db" / "run_cycles.parquet")
    c = sqlite3.connect(proj / "db" / "research.sqlite3")
    c.execute("create table tests(test_key, owner, test_date, set_no, sample_name, test_type, design_id, candidate_id)")
    c.execute("create table designs(design_id, design_label, status)")
    c.commit(); c.close()

    res = sl.build_slide_link(proj, [tmp_path / "2팀", tmp_path / "4팀"], workers=2, log=lambda *_: None)
    idx = pd.read_parquet(proj / "slides" / "slide_index.parquet")
    assert set(idx["team"]) == {"2팀", "4팀"}
    assert "임꺽정" in set(idx["deck_people"])
    links = res["links"]
    best = res["matched"].set_index("test_key")
    assert best.loc["a1", "confidence"] == "verified" and best.loc["a1", "set_label"].startswith("Set1"), best.loc["a1"].to_dict()
    assert best.loc["a1", "file_name"] == "260720 ZQ 평가.pptx"
    assert best.loc["b2", "team"] == "4팀" and best.loc["b2", "set_label"].startswith("Set2"), best.loc["b2"].to_dict()
    assert "c3" in set(res["tests_without_slides"]["test_key"])
    # no aliasing: 임꺽정's test gets no owner credit from 홍길동's folder
    other = links[(links["test_key"] == "b2") & (links["owner_folder"] == "홍길동")]
    assert not len(other) or (other["confidence"] != "verified").all()
    c = sqlite3.connect(proj / "db" / "research.sqlite3")
    n = c.execute("select count(*) from v_test_slides where test_key='a1'").fetchone()[0]
    teams = {r[0] for r in c.execute("select team from slide_decks")}
    c.close()
    assert n >= 1 and teams == {"2팀", "4팀"}


def test_check_metrics_tolerances():
    import numpy as np
    d = np.array([0.0045 * 0.9, 0.0045, 0.0045, 0.00445] + [0.0044] * 96)
    ch = np.array([0.0045] * 100)
    ok, bad, det = sl.check_metrics({"ice_pct": 90.2, "retention_100cyc_pct": 97.8, "ce_50cyc_pct": 97.7}, (d, ch))
    assert (ok, bad) == (2, 0), det          # CE is shown but not counted
    ok, bad, _ = sl.check_metrics({"ice_pct": 88.0}, (d, ch))
    assert (ok, bad) == (0, 1)
    ok, bad, det = sl.check_metrics({"ice_pct": 89.5}, (d, ch))   # 0.5 %p: neutral, neither agree nor disagree
    assert (ok, bad) == (0, 0) and "≈" in det
    assert sl.check_metrics({"retention_1cyc_pct": 50}, (d, ch))[:2] == (0, 0)  # too-short windows ignored