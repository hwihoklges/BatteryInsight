"""Reviewed slide <-> test links and the slide-derived facts written to the research DB.

``slides/slide_link_review.csv`` is owned by the reviewer (person or review agent) and survives re-runs:

    test_key, decision (link | no_slide | unsure), file_name, slide_no, set_label,
    confidence (high | medium | low), evidence, slide_design, slide_conditions, metric_check,
    owner_guess, note, reviewer

Decisions override the automatic matcher of :mod:`slide_link`:

* ``link``     the listed (deck, page, set) rows become the test's links (``confirmed`` / ``probable``)
* ``no_slide`` every automatic link of the test is dropped (reviewed: no slide shows it)
* ``unsure``   automatic links are kept and flagged

Slide-stated values never overwrite DB fields: they live in separate ``slide_*`` columns of
``test_slide_facts`` / ``design_slide_facts``.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

REVIEW_FILE = "slide_link_review.csv"
REVIEW_COLS = ["test_key", "decision", "file_name", "slide_no", "set_label", "confidence", "evidence",
               "slide_design", "slide_conditions", "metric_check", "owner_guess", "note", "reviewer", "file_path"]
DECISIONS = ("link", "no_slide", "unsure")
REVIEW_SCORE = {"high": 30.0, "medium": 25.0, "low": 20.0}
CONF_ORDER = ["confirmed", "probable", "verified", "matched", "candidate"]
STRONG = ("confirmed", "probable", "verified", "matched")     # links trusted enough for slide facts
REVIEW_EXTRA = ["review_status", "review_confidence", "reviewer", "review_note", "review_design",
                "review_conditions", "owner_guess", "review_file_path", "review_evidence"]


def _norm(s) -> str:
    return re.sub(r"[\s_\-·ㆍ]+", "", str(s or "").lower())


def _str(x) -> str:
    return x if isinstance(x, str) else ""


def _loads(s) -> dict:
    try:
        d = json.loads(s) if isinstance(s, str) and s else {}
        return d if isinstance(d, dict) else {}
    except ValueError:
        return {}


# ----------------------------------------------------------------------------- review file
def load_review(project) -> pd.DataFrame:
    p = Path(project) / "slides" / REVIEW_FILE
    if not p.exists():
        return pd.DataFrame(columns=REVIEW_COLS)
    r = pd.read_csv(p, dtype=str, encoding="utf-8-sig", keep_default_na=False)
    for c in REVIEW_COLS:
        if c not in r:
            r[c] = ""
    r = r[REVIEW_COLS].apply(lambda s: s.astype(str).str.strip())
    r["decision"] = r["decision"].str.lower()
    r["confidence"] = r["confidence"].str.lower()
    r = r[(r["test_key"] != "") & r["decision"].isin(DECISIONS)]
    return _effective_review(r).reset_index(drop=True)


def _effective_review(review: pd.DataFrame) -> pd.DataFrame:
    """An explicit human decision supersedes all agent decisions for that test."""
    if review is None or review.empty or "reviewer" not in review:
        return review
    who = review["reviewer"].map(_str).str.strip().str.lower()
    human = (who != "") & ~who.str.startswith("agent")
    keys = set(review.loc[human, "test_key"])
    return review[human | ~review["test_key"].isin(keys)]


def review_status(review: pd.DataFrame) -> pd.Series:
    """test_key -> link | no_slide | unsure (a single 'link' row wins)."""
    if review is None or not len(review):
        return pd.Series(dtype=str)
    review = _effective_review(review)
    rank = review["decision"].map({"link": 0, "no_slide": 1, "unsure": 2})
    return review.assign(_r=rank).sort_values("_r").drop_duplicates("test_key").set_index("test_key")["decision"]


def _sets_for_label(label: str, ids: list, sets: pd.DataFrame) -> list:
    """Parsed Set mentions on the page that correspond to the reviewer's set label."""
    if not ids:
        return []
    if (re.search(r"set\s*#?\s*\d+\s*[-~,/&]\s*(?:set\s*#?\s*)?\d", str(label), re.I)
            or len(re.findall(r"set\s*#?\s*\d+", str(label), re.I)) > 1):
        return []
    lab = _norm(label)
    m = re.search(r"set#?0*(\d+)", lab)
    if m:
        no = int(m.group(1))
        hit = [i for i in ids if pd.notna(sets.at[i, "set_no"]) and int(sets.at[i, "set_no"]) == no]
        if hit:
            return hit
    if len(lab) >= 2:
        hit = [i for i in ids if (sl := _norm(sets.at[i, "set_label"])) and
               (sl == lab or (len(lab) >= 3 and len(sl) >= 3 and (lab in sl or sl in lab)))]
        if hit:
            return hit
    return []


def apply_review(pairs: pd.DataFrame, review: pd.DataFrame, S: pd.DataFrame, sets: pd.DataFrame,
                 facts: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Apply reviewed decisions to the automatic pairs. Returns (pairs, unresolved review rows)."""
    from .slide_link import check_metrics
    pairs = pairs.copy()
    for c in REVIEW_EXTRA:
        pairs[c] = ""
    if review is None or not len(review):
        return pairs, pd.DataFrame(columns=REVIEW_COLS)
    review = _effective_review(review)
    status = review_status(review)
    linkers = set(status[status == "link"].index)

    by_name: dict[str, list] = {}
    for fp, fn in S[["file_path", "file_name"]].drop_duplicates().itertuples(index=False):
        by_name.setdefault(fn, []).append(fp)
    srow = S.drop_duplicates(["file_path", "slide_no"]).set_index(["file_path", "slide_no"])
    by_slide = {k: list(g.index) for k, g in sets.groupby(["file_path", "slide_no"], sort=False)}
    rows, bad = [], []
    for r in review[review["decision"] == "link"].itertuples():
        try:
            n = int(float(r.slide_no))
        except ValueError:
            bad.append(r.Index)
            continue
        fps = [f for f in by_name.get(r.file_name, []) if (f, n) in srow.index]
        if not fps:
            bad.append(r.Index)
            continue
        explicit_path = _str(getattr(r, "file_path", "")).strip()
        if explicit_path:
            fps = [f for f in fps if Path(f) == Path(explicit_path)]
        if len(fps) != 1:
            bad.append(r.Index)
            continue
        fp = fps[0]
        s = srow.loc[(fp, n)]
        ids = _sets_for_label(r.set_label, by_slide.get((fp, n), []), sets)
        attrs, mets = {}, {}
        # A reviewed page can mention several Sets; do not pool their values into one Set.
        for i in ids if len(ids) == 1 else []:
            attrs.update(_loads(sets.at[i, "attrs"]))
            mets.update(_loads(sets.at[i, "metrics"]))
        n_ok = n_bad = 0
        det = ""
        if mets and r.test_key in facts:
            n_ok, n_bad, det = check_metrics(mets, facts[r.test_key])
        rows.append({
            "test_key": r.test_key, "confidence": "confirmed" if r.confidence in ("high", "medium") else "probable",
            "score": REVIEW_SCORE.get(r.confidence, 20.0), "slide_score": np.nan, "set_bonus": 0.0,
            "team": s["team"], "owner_folder": s["owner_folder"], "file_name": r.file_name, "slide_no": n,
            "title": s["title"], "file_date": s["file_date"], "set_label": r.set_label,
            "set_metrics": json.dumps(mets, ensure_ascii=False) if mets else "",
            "metric_check": "; ".join(x for x in (r.metric_check, f"자동대조: {det}" if det else "") if x),
            "n_agree": n_ok, "n_disagree": n_bad, "evidence": f"검토: {r.evidence}",
            "set_attrs": json.dumps(attrs, ensure_ascii=False) if attrs else "", "set_row": int(ids[0]) if len(ids) == 1 else -1,
            "file_path": fp, "set_evidence": "", "owner_score": 0.0,
            "review_status": "link", "review_confidence": r.confidence, "reviewer": r.reviewer,
            "review_note": r.note, "review_design": r.slide_design, "review_conditions": r.slide_conditions,
            "owner_guess": r.owner_guess, "review_file_path": explicit_path, "review_evidence": r.evidence})
    # tests whose link rows all failed to resolve keep their automatic links (flagged), so nothing is lost silently
    resolved = {r["test_key"] for r in rows}
    decided = resolved | set(status[status == "no_slide"].index)
    pairs.loc[pairs["test_key"].isin(set(status[status == "unsure"].index)), "review_status"] = "unsure"
    pairs.loc[pairs["test_key"].isin(linkers - resolved), "review_status"] = "unresolved"
    pairs = pairs[~pairs["test_key"].isin(decided)]
    if rows:
        new = pd.DataFrame(rows).drop_duplicates(["test_key", "file_path", "slide_no", "set_label"])
        pairs = pd.concat([pairs, new], ignore_index=True)
    pairs = pairs.sort_values(["test_key", "score"], ascending=[True, False]).reset_index(drop=True)
    return pairs, review.loc[bad]


# ----------------------------------------------------------------------------- deck versions / copies
_VER = re.compile(r"(?:[_\s\-]*(?:v|ver\.?|rev\.?)\s?\d+(?:\.\d+)?(?![\d])|\(.*?\)|\[.*?\]|"
                  r"[_\s\-]*(?:final|최종|수정본?|updated?|copy|사본)(?=$|[_\s\-]))", re.I)


def family_key(file_name: str) -> str:
    """Deck title without date prefix, version / update marks and a trailing owner name."""
    stem = Path(str(file_name)).stem
    stem = re.sub(r"^\s*\d{6}(?:[_\s\-]+|(?=[^\d]))", "", stem)
    stem = _VER.sub(" ", stem)
    stem = re.sub(r"_[가-힣]{3}(?=$|[_\s])", " ", stem)
    return re.sub(r"[\s_\-]+", " ", stem).strip().lower()


def deck_families(decks: pd.DataFrame) -> pd.DataFrame:
    """family_id / family_rank (1 = newest) / family_size for decks that are versions of the same report."""
    d = decks.copy()
    key = d["team"].astype(str) + "|" + d["file_name"].map(family_key)
    d["family_key"] = key
    d["family_id"] = key.map(lambda k: hashlib.sha1(k.encode("utf-8")).hexdigest()[:10])
    d = d.sort_values(["family_id", "file_date", "file_name"], ascending=[True, False, False])
    d["family_rank"] = d.groupby("family_id").cumcount() + 1
    d["family_size"] = d.groupby("family_id")["file_path"].transform("size")
    return d


def slide_copies(S: pd.DataFrame, min_chars: int = 40) -> pd.DataFrame:
    """content_hash / is_canonical / n_copies / canonical_ref for slides repeated across versions or folders.

    Identical (normalised) title + text + table = same slide; the newest deck's copy is canonical.
    """
    txt = (S["title"].fillna("") + "¶" + S["text"].fillna("") + "¶" + S["table_text"].fillna("")).map(_norm)
    h = [hashlib.sha1(t.encode("utf-8")).hexdigest()[:16] if len(t) >= min_chars else f"u:{fp}:{n}"
         for t, fp, n in zip(txt, S["file_path"], S["slide_no"])]
    d = pd.DataFrame({"file_path": S["file_path"].to_numpy(), "slide_no": S["slide_no"].to_numpy(),
                      "file_name": S["file_name"].to_numpy(), "file_date": S["file_date"].to_numpy(),
                      "content_hash": h}, index=S.index)
    first = d.sort_values(["file_date", "file_name"], ascending=[False, False]).drop_duplicates("content_hash")
    ref = dict(zip(first["content_hash"], zip(first["file_path"], first["slide_no"], first["file_name"])))
    d["is_canonical"] = [(fp, n) == ref[x][:2] for x, fp, n in zip(d["content_hash"], d["file_path"], d["slide_no"])]
    d["n_copies"] = d.groupby("content_hash")["file_path"].transform("size")
    d["canonical_ref"] = [f"{ref[x][2]} p{ref[x][1]}" for x in d["content_hash"]]
    return d[["content_hash", "is_canonical", "n_copies", "canonical_ref"]]


# ----------------------------------------------------------------------------- slide facts
def _fmt_attrs(attrs: dict, keys=None) -> str:
    out = []
    for k, v in attrs.items():
        if keys and k not in keys:
            continue
        if isinstance(v, list):
            v = "/".join(map(str, v))
        out.append(f"{k[2:] if k.startswith('x:') else k}={v}")
    return "; ".join(out)


COND_KEYS = ("temperature_c", "pressure_mpa", "voltage_range", "x:구동압", "x:c-rate", "x:온도")


def _num(x):
    try:
        v = float(x)
        return v if np.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def build_test_facts(links: pd.DataFrame, review: pd.DataFrame, tests: pd.DataFrame, facts: dict) -> pd.DataFrame:
    """One row per test: review status, best slide, slide-stated design / conditions / results (``slide_*``)."""
    review = _effective_review(review)
    status = review_status(review)
    guess = (review[review["owner_guess"] != ""].groupby("test_key")["owner_guess"].agg(lambda x: x.mode().iloc[0])
             if len(review) else pd.Series(dtype=str))
    notes = (review[review["decision"] != "link"].groupby("test_key")["note"].agg(lambda x: " | ".join(v for v in x if v))
             if len(review) else pd.Series(dtype=str))
    L = links[links["confidence"].isin(STRONG)] if len(links) else links
    groups = {k: g for k, g in L.groupby("test_key", sort=False)} if len(L) else {}
    allconf = links.groupby("test_key")["confidence"].agg(
        lambda x: min(x, key=lambda c: CONF_ORDER.index(c) if c in CONF_ORDER else 99)) if len(links) else {}
    reviewed_links = set(links.loc[links["confidence"].isin(["confirmed", "probable"]), "test_key"]) if len(links) else set()
    rows = []
    for t in tests.itertuples(index=False):
        key = t.test_key
        st = status.get(key, "")
        if st == "link" and key not in reviewed_links:
            st = "unresolved"
        g = groups.get(key)
        rec = {"test_key": key, "review_status": st,
               "link_status": "no_slide" if st == "no_slide" else (allconf.get(key, "") or "none")}
        f = facts.get(key)
        d1 = c1 = None
        if f is not None and len(f[0]):
            d1 = _num(f[0][0] * 1000)
            c1 = _num(f[1][0] * 1000) if len(f[1]) else None
        ice = 100 * d1 / c1 if d1 is not None and c1 else None
        rec.update(db_chg1_mah=c1, db_dchg1_mah=d1,
                   db_ice_pct=ice if ice is not None and 0 < ice <= 105 else None,   # >105 %: partial 1st charge
                   slide_owner_guess=guess.get(key, ""), review_note=notes.get(key, ""))
        if g is not None and len(g):
            g = g.sort_values("score", ascending=False, kind="stable")
            p = g.iloc[0]
            # Summary values must originate on the cited page/Set, not another linked experiment.
            attrs, mets = _loads(p["set_attrs"]), _loads(p["set_metrics"])
            rd = _str(p.get("review_design"))
            rc = _str(p.get("review_conditions"))
            sd = _num(mets.get("discharge_1st_mah_g"))
            mass = 1000 * d1 / sd if d1 and sd and 30 <= sd <= 400 else None
            rec.update(n_slide_links=len(g), n_decks=g["file_path"].nunique(), slide_file_name=p["file_name"],
                       slide_no=int(p["slide_no"]), slide_set_label=p["set_label"], slide_team=p["team"],
                       slide_date=str(p["file_date"])[:10], slide_design=rd or _fmt_attrs(attrs),
                       slide_conditions=rc or _fmt_attrs(attrs, COND_KEYS),
                       slide_attrs_json=json.dumps(attrs, ensure_ascii=False) if attrs else "",
                       slide_metrics_json=json.dumps(mets, ensure_ascii=False) if mets else "",
                       slide_chg1_mah_g=_num(mets.get("charge_1st_mah_g")), slide_dchg1_mah_g=sd,
                       slide_ice_pct=_num(mets.get("ice_pct")), implied_active_mass_mg=mass,
                       metric_check=p["metric_check"], slide_evidence=p["evidence"], slide_file_path=p["file_path"])
        rows.append(rec)
    cols = ["test_key", "review_status", "link_status", "n_slide_links", "n_decks", "slide_file_name", "slide_no",
            "slide_set_label", "slide_team", "slide_date", "slide_design", "slide_conditions", "slide_attrs_json",
            "slide_metrics_json", "slide_chg1_mah_g", "slide_dchg1_mah_g", "slide_ice_pct", "db_chg1_mah", "db_dchg1_mah",
            "db_ice_pct", "implied_active_mass_mg", "metric_check", "slide_owner_guess", "review_note", "slide_evidence",
            "slide_file_path"]
    out = pd.DataFrame(rows).reindex(columns=cols)
    out["n_slide_links"] = out["n_slide_links"].fillna(0).astype(int)
    out["n_decks"] = out["n_decks"].fillna(0).astype(int)
    out["slide_no"] = out["slide_no"].astype("Int64")
    return out


def owner_candidates(review: pd.DataFrame, links: pd.DataFrame, tests: pd.DataFrame, deck_owner: dict) -> pd.DataFrame:
    """Who is behind a requester code without a name? Suggestions only (dx/owners.csv stays user-owned)."""
    t = tests[tests["owner_name"].isna() & tests["requester_code"].notna()]
    code = dict(zip(t["test_key"], t["requester_code"].astype(str)))
    n_code = t["requester_code"].astype(str).value_counts()
    ev = []
    if len(review):
        for r in review[(review["owner_guess"] != "") & review["test_key"].isin(code)].itertuples(index=False):
            ev.append((code[r.test_key], r.owner_guess, r.test_key, "review"))
    if len(links):
        L = links[links["confidence"].isin(STRONG) & links["test_key"].isin(code)]
        for k, fp in zip(L["test_key"], L["file_path"]):
            who = deck_owner.get(fp, "")
            if who:
                ev.append((code[k], who, k, "deck"))
    cols = ["requester_code", "candidate_name", "n_tests_supporting", "n_code_tests", "share", "from_review",
            "from_decks", "suggestion", "example_tests"]
    if not ev:
        return pd.DataFrame(columns=cols)
    e = pd.DataFrame(ev, columns=["requester_code", "candidate_name", "test_key", "src"])
    g = e.groupby(["requester_code", "candidate_name"]).agg(
        n_tests_supporting=("test_key", "nunique"),
        from_review=("src", lambda x: int((x == "review").sum())), from_decks=("src", lambda x: int((x == "deck").sum())),
        example_tests=("test_key", lambda x: ",".join(list(dict.fromkeys(x))[:5]))).reset_index()
    g["n_code_tests"] = g["requester_code"].map(n_code).astype(int)
    g["share"] = (g["n_tests_supporting"] / g["n_code_tests"]).round(2)
    top = g.groupby("requester_code")["n_tests_supporting"].transform("max")
    # 유력 = the top candidate backed by at least 3 tests and a majority of the code's tests
    g["suggestion"] = np.where((g["n_tests_supporting"] == top) & (g["n_tests_supporting"] >= 3) & (g["share"] >= 0.5),
                               "유력", "약함")
    return g.sort_values(["requester_code", "n_tests_supporting"], ascending=[True, False])[cols].reset_index(drop=True)


# ----------------------------------------------------------------------------- matcher evaluation
_SETNO = re.compile(r"set\s*#?\s*0*(\d+)", re.I)


def _set_no(label) -> int | None:
    m = _SETNO.search(str(label or ""))
    return int(m.group(1)) if m else None


def evaluate_auto(auto: pd.DataFrame, final: pd.DataFrame, review: pd.DataFrame, S: pd.DataFrame) -> pd.DataFrame:
    """Accuracy of the automatic matcher measured against the reviewed decisions.

    A hit = the automatic slide is the reviewed slide or an identical copy of it (``content_hash``); a set hit also
    needs the same Set number when both labels carry one.
    """
    cols = ["check", "n_tests", "n_hit", "n_set_hit", "n_family_hit", "rate"]
    st = review_status(review)
    if not len(st) or not len(auto):
        return pd.DataFrame(columns=cols)
    h = dict(zip(zip(S["file_path"], S["slide_no"]), S["content_hash"]))
    fam = {fp: f"{t}|{family_key(fn)}" for fp, t, fn in S[["file_path", "team", "file_name"]].drop_duplicates(
        "file_path").itertuples(index=False)}
    rv = final[final["confidence"].isin(["confirmed", "probable"])]
    truth: dict[str, dict] = {}
    tfam: dict[str, set] = {}
    for k, fp, n, lab in zip(rv["test_key"], rv["file_path"], rv["slide_no"], rv["set_label"]):
        truth.setdefault(k, {}).setdefault(h.get((fp, int(n))), set()).add(_set_no(lab))
        tfam.setdefault(k, set()).add((fam.get(fp), _set_no(lab)))
    no_slide = set(st[st == "no_slide"].index)
    reviewed = set(truth) | no_slide
    a = auto[auto["test_key"].isin(reviewed)]
    top = a.sort_values("score", ascending=False).drop_duplicates("test_key")

    def hit(k, fp, n, lab):
        no = _set_no(lab)
        f = fam.get(fp)
        fhit = any(ff == f and (no is None or sn is None or no == sn) for ff, sn in tfam.get(k, ()))
        sets_ = truth.get(k, {}).get(h.get((fp, int(n))))
        if sets_ is None:
            return False, False, fhit
        return True, (no is None or None in sets_ or no in sets_), fhit

    rows = []
    for conf in ("verified", "matched", "candidate"):
        t = top[top["confidence"] == conf]
        hs = [hit(*x) for x in zip(t["test_key"], t["file_path"], t["slide_no"], t["set_label"])]
        n_hit, n_set, n_fam = (sum(x[i] for x in hs) for i in range(3))
        rows.append({"check": f"자동 1순위 = {conf}", "n_tests": len(t), "n_hit": n_hit, "n_set_hit": n_set,
                     "n_family_hit": n_fam, "rate": round(n_set / len(t), 3) if len(t) else None})
    linked = [k for k in truth]
    any_hit = any_fam = 0
    for k, g in a[a["test_key"].isin(linked)].groupby("test_key"):
        hs = [hit(*x) for x in zip(g["test_key"], g["file_path"], g["slide_no"], g["set_label"])]
        any_hit += any(x[0] for x in hs)
        any_fam += any(x[0] or x[2] for x in hs)
    rows.append({"check": "검토 link test 중 자동 후보에 정답 슬라이드 포함", "n_tests": len(linked), "n_hit": any_hit,
                 "n_set_hit": None, "n_family_hit": any_fam, "rate": round(any_hit / len(linked), 3) if linked else None})
    fp_ = top[top["test_key"].isin(no_slide) & top["confidence"].isin(["verified", "matched"])]
    rows.append({"check": "검토 no_slide test에 자동 verified/matched (오탐)", "n_tests": len(no_slide),
                 "n_hit": len(fp_), "n_set_hit": None, "rate": round(len(fp_) / len(no_slide), 3) if no_slide else None})
    return pd.DataFrame(rows, columns=cols)


def design_slide_facts(conn) -> int:
    """Aggregate test_slide_facts per design (consensus of slide-stated attributes) -> table design_slide_facts."""
    try:
        f = pd.read_sql(
            "SELECT f.*, coalesce(t.design_id, t.candidate_id) AS design_id FROM test_slide_facts f "
            "JOIN tests t ON t.test_key = f.test_key WHERE f.link_status IN ('confirmed','probable','verified','matched')",
            conn)
    except Exception:
        return 0
    rows = []
    for did, g in f.groupby("design_id", sort=False):
        cons: dict[str, dict] = {}
        for a in g["slide_attrs_json"]:
            for k, v in _loads(a).items():
                v = "/".join(map(str, v)) if isinstance(v, list) else str(v)
                cons.setdefault(k, {}).setdefault(v, 0)
                cons[k][v] += 1
        uniq = lambda s: " | ".join(list(dict.fromkeys(x for x in s if isinstance(x, str) and x))[:5])
        num = lambda c: g[c].dropna()
        rows.append({
            "design_id": did, "n_tests_with_slides": len(g),
            "n_confirmed": int(g["link_status"].isin(["confirmed", "probable"]).sum()),
            "n_slide_designs": int(g["slide_design"].map(lambda x: _norm(x) if isinstance(x, str) and x else None)
                                   .dropna().nunique()),
            "decks": uniq(g["slide_file_name"]), "set_labels": uniq(g["slide_set_label"]),
            "slide_designs": uniq(g["slide_design"]), "slide_conditions": uniq(g["slide_conditions"]),
            "attr_consensus_json": json.dumps(cons, ensure_ascii=False) if cons else "",
            "attr_conflicts": ", ".join(sorted(k for k, v in cons.items() if len(v) > 1)),
            "slide_ice_pct_mean": num("slide_ice_pct").mean() if len(num("slide_ice_pct")) else None,
            "slide_dchg1_mah_g_mean": num("slide_dchg1_mah_g").mean() if len(num("slide_dchg1_mah_g")) else None,
            "implied_active_mass_mg_mean": (num("implied_active_mass_mg").mean()
                                            if len(num("implied_active_mass_mg")) else None)})
    cols = ["design_id", "n_tests_with_slides", "n_confirmed", "n_slide_designs", "decks", "set_labels", "slide_designs",
            "slide_conditions", "attr_consensus_json", "attr_conflicts", "slide_ice_pct_mean", "slide_dchg1_mah_g_mean",
            "implied_active_mass_mg_mean"]
    d = pd.DataFrame(rows, columns=cols)
    d.to_sql("design_slide_facts", conn, if_exists="replace", index=False)
    return len(d)
