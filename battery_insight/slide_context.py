"""Circumstantial context for tests that no slide shows (데이터O·장표X).

Each data-only test gets a primary reason (``ctx_reason``) plus the evidence behind it:

* role from the DX fields / sample name: equipment check, analysis sample (XRD/DSC), preparatory step
  (formation, capacity check, SOC setting, dOCV check), storage, main test
* report timing against the owner's decks: before the owner's first deck, after the latest one,
  owner has no decks at all
* experiment group: tests sharing the series / batch / 도급 batch / design / input date with the same owner,
  and the slide of the nearest group member that *is* reported (most shared sample words first). An input day or
  batch often holds several sub-experiments, so those loose groups keep only tests sharing sample words; if just
  the other sub-experiments were reported the test gets "같은 투입분 다른 실험만 보고됨"
* data signals: no cycle data, retest, anomaly flags, ICE far from the same design's other tests

Everything is inferred (suggestions); nothing here changes test or slide data.
"""
from __future__ import annotations

import re
from collections import Counter

import numpy as np
import pandas as pd

STRONG = ("confirmed", "probable", "verified", "matched")
CONF_ORDER = ["confirmed", "probable", "verified", "matched", "candidate"]

EQUIP = re.compile(r"채널\s*이상|악어집게|충방전기\s*비교|장비\s*(?:점검|비교|이상)|교정|calibration|"
                   r"\d+\s*m?a\s+vs\.?\s+\d+", re.I)
ANALYSIS = re.compile(r"dsc|xrd|in\s*-?\s*situ|\bsem\b|\bxps\b|\btem\b|분석|analys", re.I)
STEP = re.compile(r"(?:^|[\s_(])(?:fm|form(?:ation)?)(?=$|[\s_)])|활성화|capa\s*-?\s*check|용량\s*(?:체크|확인)|세팅|"
                  r"setting|사전|pre[\s_-]?(?:test|check)|before|(?:측정|qpeed|저장|qc|평가|시험|dcir|rpt)\s*전(?=[\s_)]|$)",
                  re.I)
RETEST = re.compile(r"[\s_](?:r+e|재측정|재시험|재평가)$|[\s_]2차(?:$|[\s_])", re.I)
TOKEN = re.compile(r"[0-9a-z가-힣.]+")
TOKEN_STOP = re.compile(r"^(?:set\d*|no\d*|ch\d*|cyc|cycle|r+e|test|\d+)$")
STEP_MAX_CYCLES = 20          # a "formation"/"check" that ran this many cycles is a real test
REPORT_LAG = pd.Timedelta(days=7)
ARCHIVE_GAP = pd.Timedelta(days=60)
ICE_DEV = 5.0                 # %p from the median of the same design's other tests
ICE_LOW = 70.0
ICE_CONT = 95.0               # first-cycle efficiency this high means formation ran in an earlier test

GROUP_BASES = (("series_key", "같은 시리즈"), ("batch_candidate", "같은 배치"), ("contract", "같은 도급 배치"),
               ("design", "같은 설계"), ("date_key", "같은 투입일"))
LOOSE_BASES = {"batch_candidate", "date_key"}    # may hold unrelated sub-experiments: split by sample words
GENERIC_MIN_TESTS = 4
GENERIC_SHARE = 0.5
COLS = ["test_key", "ctx_reason", "ctx_detail", "role", "report_timing", "owner_eff", "owner_basis", "owner_first_deck",
        "owner_last_deck", "n_cycles", "group_basis", "n_group", "n_group_reported", "sibling_tests", "sibling_slide",
        "sibling_common", "sibling_design", "design_n_reported", "db_ice_pct", "ice_dev_pct", "ctx_flags", "review_status",
        "review_note"]


def _s(x) -> str:
    return x if isinstance(x, str) else ""


def _tokens(sample, name) -> set:
    """Content words of the sample name (test name if empty) without set/channel numbers and dates."""
    text = re.sub(r"(?<!\d)\.|\.(?!\d)", " ", (_s(sample) or _s(name)).lower())     # keep 1.5, split Ref.전도
    return {t for t in TOKEN.findall(text) if len(t) >= 2 and not TOKEN_STOP.match(t)}


def _role(r, n_cyc: int) -> str:
    text = f"{_s(r.sample_name)} {_s(r.test_name)}"
    proto, stage, ttype = _s(r.protocol), _s(r.rpt_stage), _s(r.test_type_final)
    if proto == "cycler_comparison" or EQUIP.search(text):
        return "장비·채널 점검"
    if proto in ("DSC_sample", "XRD_sample") or ttype == "Insitu" or ANALYSIS.search(text):
        return "분석용 셀"
    if n_cyc < STEP_MAX_CYCLES and (proto == "dOCV_check" or stage.startswith("before") or stage == "BOL"
                                    or ttype == "Formation" or STEP.search(text)):
        return "선행·점검 단계"
    if ttype == "Storage":
        return "저장 시험"
    return "본시험"


def data_only_context(tests: pd.DataFrame, test_facts: pd.DataFrame, decks: pd.DataFrame, owners: pd.DataFrame,
                      design_of: dict, n_cycles: dict) -> pd.DataFrame:
    """One row per test without a trusted slide link (see module docstring)."""
    need = ["owner_base", "requester_code", "context_path", "test_date", "sample_name", "test_name", "test_type_final",
            "protocol", "rpt_stage", "retest", "series_key", "batch_candidate", "tags", "anomaly", "quality_flags"]
    T = tests.reindex(columns=["test_key"] + need).copy()
    T["test_date"] = pd.to_datetime(T["test_date"], errors="coerce")
    f = test_facts.set_index("test_key")
    reported = set(f.index[f["link_status"].isin(STRONG)])
    lead = (owners[owners["suggestion"] == "유력"].drop_duplicates("requester_code")
            .set_index("requester_code")["candidate_name"] if len(owners) else pd.Series(dtype=str))
    okey, oeff, obasis = [], [], []
    for o, c, p in zip(T["owner_base"], T["requester_code"], T["context_path"]):
        o, c = _s(o), _s(c)
        if o:
            okey.append(o), oeff.append(o), obasis.append("이름")
        elif c:
            okey.append("#" + c), oeff.append(lead.get(c, "")), obasis.append("사번 후보" if c in lead else "")
        else:
            okey.append("@" + _s(p)), oeff.append(""), obasis.append("")
    T["okey"], T["owner_eff"], T["owner_basis"] = okey, oeff, obasis
    T["design"] = T["test_key"].map(design_of)
    T["contract"] = T["tags"].map(_s).str.extract(r"contract_batch:(\w+)")[0]
    T["date_key"] = T["test_date"].dt.strftime("%Y-%m-%d")
    T["n_cycles"] = [int(n_cycles.get(k, 0)) for k in T["test_key"]]

    groups: dict[tuple, list] = {}
    for col, _ in GROUP_BASES:
        scope = T if col == "series_key" else T.assign(_k=T["okey"])
        for k, g in scope.groupby([col] if col == "series_key" else ["_k", col], sort=False):
            if len(g) > 1:
                groups[(col,) + (k if isinstance(k, tuple) else (k,))] = list(g["test_key"])

    def gkey(r, col):
        v = getattr(r, col)
        if not isinstance(v, str) or not v:
            return None
        return (col, v) if col == "series_key" else (col, r.okey, v)

    dt = pd.to_datetime(decks["file_date"], errors="coerce") if len(decks) else pd.Series(dtype="datetime64[ns]")
    span = (pd.DataFrame({"o": decks["slide_owner"], "d": dt}).dropna().groupby("o")["d"].agg(["min", "max"])
            if len(decks) else pd.DataFrame(columns=["min", "max"]))
    date_of = dict(zip(T["test_key"], T["test_date"]))
    conf_of = f["link_status"].to_dict()
    ice = f["db_ice_pct"] if "db_ice_pct" in f else pd.Series(dtype=float)
    by_design: dict[str, list] = {}
    for k, d in zip(T["test_key"], T["design"]):
        if isinstance(d, str) and pd.notna(ice.get(k)):
            by_design.setdefault(d, []).append(k)
    design_rep = T[T["test_key"].isin(reported)].groupby("design").size()
    names = {n.lower() for n in pd.concat([T["owner_base"], owners.get("candidate_name", pd.Series(dtype=str))])
             if isinstance(n, str)}
    toks = {k: _tokens(s, n) - names for k, s, n in zip(T["test_key"], T["sample_name"], T["test_name"])}
    generic: dict[str, set] = {}                  # words in half of an owner's tests (folder names etc.) say nothing
    for ok, keys in T.groupby("okey")["test_key"]:
        if len(keys) >= GENERIC_MIN_TESTS:
            cnt = Counter(w for k in keys for w in toks[k])
            generic[ok] = {w for w, c in cnt.items() if c >= GENERIC_SHARE * len(keys)}
    words = {k: toks[k] - generic.get(ok, set()) for k, ok in zip(T["test_key"], T["okey"])}

    rows = []
    for r in T[~T["test_key"].isin(reported)].itertuples(index=False):
        role = _role(r, r.n_cycles)
        o = r.owner_eff
        first = last = None
        if not o:
            timing = "담당자 미상"
        elif o not in span.index:
            timing = "담당자 장표 없음"
        else:
            first, last = span.at[o, "min"], span.at[o, "max"]
            if pd.isna(r.test_date):
                timing = "시험일 미상"
            elif r.test_date < first - ARCHIVE_GAP:
                timing = "장표 보관 이전"
            elif last < r.test_date + REPORT_LAG:
                timing = "최신 장표 이후"
            else:
                timing = "장표 기간 내"

        mine = words[r.test_key]
        basis, members, reps, weak, other = "", [], [], False, None
        for col, label in GROUP_BASES:
            k = gkey(r, col)
            mem = [x for x in groups.get(k, []) if x != r.test_key] if k else []
            if not mem:
                continue
            loose = col in LOOSE_BASES
            if loose and mine:
                # one input day / batch often holds several sub-experiments: if only other ones were reported,
                # keep the tests sharing sample words with this one
                sub = [x for x in mem if mine & words[x]]
                rest = [x for x in mem if x in reported and x not in sub]
                if rest:
                    if other is None:
                        other = (label, len(mem) + 1, rest)
                    mem = sub
                    if not mem:
                        continue
            rp = [x for x in mem if x in reported]
            if not basis or rp:
                basis, members, reps, weak = label, mem, rp, loose and not mine
            if rp:
                break

        def shared(x):
            return mine & words[x]

        def near(x):
            d0, d1 = date_of.get(x), r.test_date
            gap = abs((d0 - d1).days) if pd.notna(d0) and pd.notna(d1) else 9999
            c = conf_of.get(x, "")
            return -len(shared(x)), gap, CONF_ORDER.index(c) if c in CONF_ORDER else 9

        def ref(x):
            p = f.loc[x]
            s = f"{_s(p.get('slide_file_name'))} p{int(p['slide_no'])}" if pd.notna(p.get("slide_no")) else ""
            return f"{s} {p['slide_set_label']}" if _s(p.get("slide_set_label")) else s
        reps = sorted(reps, key=near)
        sib_slide = sib_design = sib_common = ""
        if reps:
            sib_slide = ref(reps[0])
            sib_design = _s(f.at[reps[0], "slide_design"]) if "slide_design" in f else ""
            sib_common = ",".join(sorted(shared(reps[0])))
        oth_txt = ""
        if other:
            olabel, on, orep = other
            orep = sorted(orep, key=near)
            oth_txt = f"{olabel} {on}건 중 다른 실험 {len(orep)}건만 보고: {ref(orep[0])}"

        flags = []
        if r.n_cycles == 0:
            flags.append("사이클 데이터 없음")
        if _s(r.anomaly):
            flags.append(f"데이터 이상({r.anomaly})")
        if "no_valid_voltage" in _s(r.quality_flags):
            flags.append("전압 기록 없음")
        if _s(r.retest) == "retest" or RETEST.search(_s(r.sample_name)):
            flags.append("재시험")
        v = ice.get(r.test_key)
        dev = None
        if pd.notna(v):
            peers = [ice[x] for x in by_design.get(r.design, []) if x != r.test_key]
            if len(peers) >= 2:
                dev = float(v - np.median(peers))
                if abs(dev) > ICE_DEV and v < ICE_CONT:
                    flags.append(f"ICE 이상({dev:+.1f}%p, 같은 설계 대비)")
            if v < ICE_LOW:
                flags.append(f"ICE 낮음({v:.0f}%)")
            elif v >= ICE_CONT:
                flags.append(f"ICE {v:.0f}%(화성 후 이어서 측정 추정)")

        n_grp = len(members) + 1 if members else 0
        fmt = lambda d: d.strftime("%Y-%m-%d") if d is not None and pd.notna(d) else "?"
        grp_txt = f"{basis} {n_grp}건 중 {len(reps)}건 보고: {sib_slide}" if reps else ""
        if reps and weak:
            grp_txt += " (샘플명 정보 없음, 약한 근거)"
        if role == "장비·채널 점검":
            reason, detail = "장비·채널 점검", "채널·충방전기 점검용 시험"
        elif role == "분석용 셀":
            reason, detail = "분석용 셀(XRD/DSC 등)", "분석 시료용 셀(사이클 결과 장표 대상 아님)"
        elif timing == "장표 보관 이전":
            reason, detail = "장표 보관 이전 시험", f"시험 {fmt(r.test_date)}, {o} 첫 장표 {fmt(first)}"
        elif timing == "최신 장표 이후":
            reason, detail = "미보고(최신 장표 이후)", f"시험 {fmt(r.test_date)}, {o} 최신 장표 {fmt(last)}"
        elif timing == "담당자 장표 없음":
            reason, detail = "담당자 장표 없음", f"{o} 이름의 장표(폴더·작성자) 없음"
        elif role == "선행·점검 단계":
            reason = "선행·점검 단계(본시험 장표 있음)" if reps else "선행·점검 단계(본시험도 미보고)"
            detail = grp_txt or oth_txt or "활성화·용량확인·세팅 등 본시험 전 단계"
        elif r.n_cycles == 0:
            reason, detail = "사이클 데이터 없음", "DB에 사이클 기록 없음(저장·분석 시험 또는 변환 누락)"
        elif reps:
            reason, detail = "같은 실험군은 보고됨(이 시험 미표시)", grp_txt
        elif other:
            reason = "같은 투입분 다른 실험만 보고됨"
            detail = oth_txt + (f"; 이 실험 {n_grp}건은 모두 장표 없음" if n_grp else "; 이 실험은 1건뿐")
        elif n_grp >= 3:
            reason, detail = "실험군 전체 미보고", f"{basis} {n_grp}건 모두 장표 없음"
        else:
            reason, detail = "미상", ""
        if reps and v is not None and pd.notna(v) and v >= ICE_CONT:
            detail += " · 첫 사이클 효율 ~100% → 활성화 뒤 이어서 돌린 후속 시험"
        elif reps and dev is not None and dev < -ICE_DEV:
            detail += " · ICE가 같은 설계보다 낮아 제외됐을 가능성"
        rows.append(dict(
            test_key=r.test_key, ctx_reason=reason, ctx_detail=detail, role=role, report_timing=timing, owner_eff=o,
            owner_basis=r.owner_basis, owner_first_deck=fmt(first) if first is not None else "",
            owner_last_deck=fmt(last) if last is not None else "", n_cycles=r.n_cycles, group_basis=basis,
            n_group=n_grp, n_group_reported=len(reps), sibling_tests=",".join(reps[:5]), sibling_slide=sib_slide,
            sibling_common=sib_common,
            sibling_design=sib_design, design_n_reported=int(design_rep.get(r.design, 0)) if isinstance(r.design, str) else 0,
            db_ice_pct=round(float(v), 2) if pd.notna(v) else None, ice_dev_pct=round(dev, 2) if dev is not None else None,
            ctx_flags="; ".join(flags),
            review_status=_s(f.at[r.test_key, "review_status"]) if r.test_key in f.index else "",
            review_note=_s(f.at[r.test_key, "review_note"]) if r.test_key in f.index else ""))
    return pd.DataFrame(rows, columns=COLS)
