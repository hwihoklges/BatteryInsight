import sqlite3

import pandas as pd
from pptx import Presentation
from pptx.util import Inches

from battery_insight import slide_link as sl
from battery_insight import slide_review as sr


def _table_deck(path, rows, title="결과"):
    prs = Presentation()
    s = prs.slides.add_slide(prs.slide_layouts[5])
    s.shapes.title.text = title
    tbl = s.shapes.add_table(len(rows), len(rows[0]), Inches(0.5), Inches(1.5), Inches(8), Inches(3)).table
    for i, row in enumerate(rows):
        for j, v in enumerate(row):
            tbl.cell(i, j).text = str(v)
    prs.save(str(path))


def _project(tmp_path):
    root = tmp_path / "2팀" / "홍길동"
    root.mkdir(parents=True)
    rows = [["Capacity (mAh/g)", "1st Charge", "1st Discharge", "Eff (%)"],
            ["Set1: PTFE1.5_ZQ81 (7/7)", "225.0", "202.5", "90.0"],
            ["Set2: PTFE1.5_ZQ82 (7/7)", "225.0", "196.0", "87.1"]]
    _table_deck(root / "260720 ZQ 평가_v1.pptx", rows, title="ZQ81 ZQ82 초기 용량 결과")
    _table_deck(root / "260725 ZQ 평가_v2.pptx", rows, title="ZQ81 ZQ82 초기 용량 결과")   # same slide, newer version
    proj = tmp_path / "proj"
    (proj / "dx").mkdir(parents=True)
    (proj / "db").mkdir()
    (proj / "slides").mkdir()
    pd.DataFrame([
        dict(test_key="a1", owner_name="홍길동", requester_code="00123456", test_date="2026-07-07", set_no="1",
             sample_name="PTFE1.5 ZQ81", test_name="x_260707_Set1_ZQ81", n_channels=2),
        dict(test_key="b2", owner_name=None, requester_code="00999999", test_date="2026-07-07", set_no="2",
             sample_name="PTFE1.5 ZQ82", test_name="x_260707_Set2_ZQ82", n_channels=2),
        dict(test_key="c3", owner_name="홍길동", requester_code="00123456", test_date="2026-07-08", set_no="3",
             sample_name="QQ77", test_name="x_260708_Set3_QQ77", n_channels=1),
        dict(test_key="d4", owner_name="홍길동", requester_code="00123456", test_date="2026-07-07", set_no="1",
             sample_name="PTFE1.5 ZQ81", test_name="y_260707_Set1_ZQ81", n_channels=1),
    ]).to_csv(proj / "dx" / "dx_tests.csv", index=False)
    cyc = []
    for key, ice in (("a1", 0.900), ("b2", 0.871), ("d4", 0.900)):
        for n in range(1, 4):
            cyc.append(dict(run_id=f"{key}:ch1", cycle_index=n, discharge_capacity_ah=0.0045 * (ice if n == 1 else 1),
                            charge_capacity_ah=0.0045 * (1 if n == 1 else 1.001)))
    pd.DataFrame(cyc).to_parquet(proj / "db" / "run_cycles.parquet")
    c = sqlite3.connect(proj / "db" / "research.sqlite3")
    c.execute("create table tests(test_key, owner, test_date, set_no, sample_name, test_type, design_id, candidate_id)")
    c.execute("create table designs(design_id, design_label, status)")
    c.executemany("insert into tests(test_key, design_id) values (?, ?)", [("a1", "D1"), ("b2", "D2"), ("c3", "D3")])
    c.executemany("insert into designs values (?, ?, 'confirmed')", [("D1", "ZQ81"), ("D2", "ZQ82"), ("D3", "QQ77")])
    c.commit()
    c.close()
    return tmp_path / "2팀", proj


def test_review_overrides_links_and_writes_slide_facts(tmp_path):
    root, proj = _project(tmp_path)
    pd.DataFrame([
        dict(test_key="b2", decision="link", file_name="260720 ZQ 평가_v1.pptx", slide_no="1", set_label="Set2",
             confidence="high", evidence="ICE 87.1", slide_design="PTFE1.5 ZQ82", slide_conditions="25℃",
             owner_guess="임꺽정", reviewer="agent"),
        dict(test_key="c3", decision="no_slide", note="보고 전", reviewer="agent"),
        dict(test_key="d4", decision="link", file_name="없는 덱.pptx", slide_no="3", set_label="Set1",
             confidence="high", reviewer="agent"),
    ]).to_csv(proj / "slides" / sr.REVIEW_FILE, index=False, encoding="utf-8-sig")

    res = sl.build_slide_link(proj, [root], workers=1, log=lambda *_: None)
    links = res["links"]
    b2 = links[links["test_key"] == "b2"]
    assert list(b2["confidence"]) == ["confirmed"], b2.to_dict("records")
    assert b2.iloc[0]["file_name"] == "260720 ZQ 평가_v1.pptx" and b2.iloc[0]["review_design"] == "PTFE1.5 ZQ82"
    assert "Set2" in b2.iloc[0]["set_metrics"] or "87.1" in b2.iloc[0]["set_metrics"]
    assert "c3" not in set(links["test_key"])
    tb = res["tests_without_slides"].set_index("test_key")
    assert tb.loc["c3", "review_status"] == "no_slide"
    # unresolved review row: reported, and the test keeps its automatic links
    assert list(res["review_unresolved"]["test_key"]) == ["d4"]
    assert not res["review"].set_index("test_key").loc["d4", "resolved"]
    assert (links.loc[links["test_key"] == "d4", "review_status"] == "unresolved").all()
    assert len(links[links["test_key"] == "d4"])

    f = res["test_facts"].set_index("test_key")
    assert f.loc["b2", "link_status"] == "confirmed" and f.loc["b2", "review_status"] == "link"
    assert f.loc["b2", "slide_ice_pct"] == 87.1
    assert abs(f.loc["b2", "db_ice_pct"] - 87.1) < 0.01
    assert abs(f.loc["b2", "implied_active_mass_mg"] - 1000 * 4.5 * 0.871 / 196.0) < 0.01
    assert f.loc["b2", "slide_design"] == "PTFE1.5 ZQ82"
    assert f.loc["c3", "link_status"] == "no_slide"
    assert f.loc["d4", "review_status"] == "unresolved"

    own = res["owner_candidates"].set_index("candidate_name")
    assert set(own["requester_code"]) == {"00999999"}               # code kept as text (leading zeros)
    assert own.loc["임꺽정", "from_review"] == 1 and own.loc["홍길동", "from_decks"] == 1

    c = sqlite3.connect(proj / "db" / "research.sqlite3")
    try:
        row = c.execute("select link_status, slide_file_name, design_label from v_test_slide_facts "
                        "where test_key='b2'").fetchone()
        assert row == ("confirmed", "260720 ZQ 평가_v1.pptx", "ZQ82")
        d2 = c.execute("select n_tests_with_slides, n_confirmed, slide_designs from design_slide_facts "
                       "where design_id='D2'").fetchone()
        assert d2[0] == 1 and d2[1] == 1 and "ZQ82" in d2[2]
        assert "D2" in set(res["design_facts"]["design_id"])
        assert c.execute("select count(*) from slide_link_review").fetchone()[0] == 3
        assert c.execute("select review_status from v_test_slides where test_key='b2'").fetchone()[0] == "link"
        fam = c.execute("select family_size, family_rank, file_name from slide_decks order by family_rank").fetchall()
        assert [r[:2] for r in fam] == [(2, 1), (2, 2)] and fam[0][2] == "260725 ZQ 평가_v2.pptx"
    finally:
        c.close()

    # the two decks repeat one slide: only the newest copy is canonical
    sets = pd.read_parquet(proj / "slides" / "slide_sets.parquet")
    assert sets["content_hash"].nunique() == 1
    assert set(sets.loc[sets["is_canonical"], "file_name"]) == {"260725 ZQ 평가_v2.pptx"}


def test_family_key_groups_versions():
    k = sr.family_key
    assert k("260911 Si계 건식음극 전해질함량 DOE_v4.2.pptx") == k("260923 Si계 건식음극 전해질함량 DOE_v5.pptx")
    assert k("260902 (update) 건조 task 정리.pptx") == k("260830 건조 task 정리.pptx")
    assert k("261007 반제품 드라이룸 노출 평가_v4.1.pptx") == k("260930 반제품 드라이룸 노출 평가_v3_이수현.pptx")
    assert k("260519 SJ1300B 평가.pptx") != k("260519 SJ1300A 평가.pptx")


def test_review_status_priority_and_load(tmp_path):
    (tmp_path / "slides").mkdir()
    pd.DataFrame([dict(test_key="t", decision="unsure"), dict(test_key="t", decision="LINK", file_name="x", slide_no="1"),
                  dict(test_key="u", decision="no_slide"), dict(test_key="v", decision="maybe")]
                 ).to_csv(tmp_path / "slides" / sr.REVIEW_FILE, index=False)
    r = sr.load_review(tmp_path)
    assert set(r["test_key"]) == {"t", "u"}             # unknown decisions are dropped
    st = sr.review_status(r)
    assert st["t"] == "link" and st["u"] == "no_slide"
