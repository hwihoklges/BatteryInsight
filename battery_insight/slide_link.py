"""Link report slides (pptx) to cycler-test records (dx_tests.csv).

index_slides   : zip-level read of ppt/slides/*.xml -> one row per slide (parquet cache)
match_tests    : vectorised scoring (owner + date + token IDF overlap + temp/pressure/type)
build_slide_link: writes slide_index.parquet, slide_links.csv, slides_without_data.csv,
                  tests_without_slides.csv, matched_designs.csv, slide_link_report.xlsx
"""
from __future__ import annotations

import argparse
import math
import os
import re
import sqlite3
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
import pandas as pd

A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PR = "{http://schemas.openxmlformats.org/package/2006/relationships}"

MATCHED_THR = 7.0
CANDIDATE_THR = 4.5
SPECIFIC_DF = 30          # token is "specific" if it appears in <= this many tests
# Owners are matched by exact name only: different names are different people (no aliasing).
MAX_DF = 60                # tokens in more tests than this are ignored (generic)

# ----------------------------------------------------------------------------- slide indexing
_DATE_STRONG = [
    re.compile(r"(?<!\d)(20)?(2[4-7])[.\-/_ ]?(0[1-9]|1[0-2])[.\-/_ ]?(0[1-9]|[12]\d|3[01])(?!\d)"),
]
_W_SLASH = re.compile(r"(?<![\d.])(1[0-2]|0?[1-9])\s?/\s?(3[01]|[12]\d|0?[1-9])(?![\d/])")
_W_KOR = re.compile(r"(1[0-2]|0?[1-9])\s?월\s?(3[01]|[12]\d|0?[1-9])\s?일")
_W_DOT = re.compile(r"(?<![\d.])(0[1-9]|1[0-2])\.(0[1-9]|[12]\d|3[01])(?![\d.]|\s?(?:mpa|c\b|%|mm|um|μm|g|wt|v\b))", re.I)
_TEMP = re.compile(r"(?<![\d.])(\d{2})\s?(?:도|℃|°\s?c|deg)", re.I)
_PRESS = re.compile(r"(\d(?:\.\d+)?)\s?mpa", re.I)
TYPE_WORDS = {
    "RPT": r"rpt|capacheck|cap\s?check", "Formation": r"formation|\bfm\b|화성|활성화",
    "Cycle": r"cycle|사이클|수명|retention", "Storage": r"storage|저장|swelling",
    "DCIR": r"dcir|저항|\bir\b", "Rate": r"율특성|rate|c-rate",
}
_TYPE_RE = {k: re.compile(v, re.I) for k, v in TYPE_WORDS.items()}
RESULT_WORDS = re.compile(
    r"용량|사이클|cycle|retention|효율|dcir|저항|\bfm\b|formation|화성|율특성|c-rate|mah|\bah\b|capacity|rpt|저장|수명|"
    r"cell|셀|충방전|전압|\bsoc\b|swelling|ocv", re.I)
RESULT_STRONG = re.compile(r"cycle|사이클|retention|dcir|저항|formation|화성|율특성|c-rate|mah|capacity|용량|rpt|수명|충방전|저장|효율", re.I)


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-·ㆍ]+", "", str(s).lower())


def _dates_from(text: str, year_hint: int):
    strong, weak = set(), set()
    for m in _DATE_STRONG[0].finditer(text):
        strong.add(f"20{m.group(2)}{m.group(3)}{m.group(4)}")
    for m in _W_SLASH.finditer(text):
        weak.add(f"{int(m.group(1)):02d}{int(m.group(2)):02d}")
    for m in _W_KOR.finditer(text):
        weak.add(f"{int(m.group(1)):02d}{int(m.group(2)):02d}")
    for m in _W_DOT.finditer(text):
        weak.add(f"{m.group(1)}{m.group(2)}")
    return strong, weak


def _file_date(stem: str):
    m = re.search(r"(?<!\d)(20)?(2[4-7])(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)", stem)
    if m:
        return pd.Timestamp(f"20{m.group(2)}-{m.group(3)}-{m.group(4)}")
    m = re.search(r"(?<!\d)(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)", stem)
    if m:
        return pd.Timestamp(f"2026-{m.group(1)}-{m.group(2)}")
    return pd.NaT


def _parse_xml_slide(xml: bytes):
    root = ET.fromstring(xml)
    title, paras, tables = "", [], []
    for sp in root.iter(P + "sp"):
        ph = sp.find(f"{P}nvSpPr/{P}nvPr/{P}ph")
        if ph is not None and ph.get("type") in ("title", "ctrTitle") and not title:
            title = " ".join("".join(t.text or "" for t in p.iter(A + "t")) for p in sp.iter(A + "p")).strip()
    for tbl in root.iter(A + "tbl"):
        rows = []
        for tr in tbl.iter(A + "tr"):
            rows.append(" | ".join(" ".join("".join(t.text or "" for t in p.iter(A + "t")) for p in tc.iter(A + "p")).strip()
                                   for tc in tr.iter(A + "tc")))
        tables.append("\n".join(rows))
    for p in root.iter(A + "p"):
        s = "".join(t.text or "" for t in p.iter(A + "t")).strip()
        if s:
            paras.append(s)
    if not title and paras:
        title = paras[0][:120]
    return title, " ¶ ".join(paras), "\n".join(tables)


def _slide_order(z: zipfile.ZipFile):
    names = set(z.namelist())
    try:
        pres = ET.fromstring(z.read("ppt/presentation.xml"))
        rels = ET.fromstring(z.read("ppt/_rels/presentation.xml.rels"))
        tgt = {r.get("Id"): r.get("Target") for r in rels.iter(PR + "Relationship")}
        out = []
        for s in pres.iter(P + "sldId"):
            t = tgt.get(s.get(R + "id"), "")
            t = t.lstrip("/")
            t = t if t.startswith("ppt/") else "ppt/" + t
            if t in names:
                out.append(t)
        if out:
            return out
    except Exception:
        pass
    sl = [n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)]
    return sorted(sl, key=lambda n: int(re.findall(r"\d+", n)[-1]))


def _person(s: str) -> str:
    """'김제덕(JEADUK KIM)[jeduk123]/선임/...' -> '김제덕'; '최 유이' -> '최유이'; non-person -> ''."""
    head = re.split(r"[(\[/<]", str(s or ""), maxsplit=1)[0].replace(" ", "")
    return head if re.fullmatch(r"[가-힣]{2,4}", head) else ""


def _parse_deck(path: str, root: str, team: str = ""):
    p = Path(path)
    rel = p.relative_to(root)
    parts = rel.parts
    owner = parts[0] if len(parts) > 1 else ""
    st = p.stat()
    base = dict(file_path=str(p), team=team, owner_folder=owner, file_name=p.name, rel_dir="/".join(parts[:-1]),
                size=st.st_size, mtime=st.st_mtime, author="", last_modified_by="", deck_people="", modified="", error="")
    fdate = _file_date(p.stem)
    try:
        with zipfile.ZipFile(path) as z:
            try:
                core = z.read("docProps/core.xml").decode("utf8", "ignore")
                m = re.search(r"<dc:creator>(.*?)</dc:creator>", core, re.S)
                base["author"] = m.group(1) if m else ""
                m = re.search(r"<cp:lastModifiedBy>(.*?)</cp:lastModifiedBy>", core, re.S)
                base["last_modified_by"] = m.group(1) if m else ""
                m = re.search(r"<dcterms:modified[^>]*>(.*?)</dcterms:modified>", core, re.S)
                base["modified"] = m.group(1)[:10] if m else ""
            except Exception:
                pass
            people = [x for x in (_person(base["author"]), _person(base["last_modified_by"])) if x]
            base["deck_people"] = " ".join(dict.fromkeys(people))
            if pd.isna(fdate):
                fdate = pd.Timestamp(base["modified"]) if base["modified"] else pd.Timestamp(st.st_mtime, unit="s")
            base["file_date"] = fdate
            rows = []
            for i, name in enumerate(_slide_order(z), 1):
                try:
                    title, text, table = _parse_xml_slide(z.read(name))
                except Exception as e:  # one bad slide shouldn't kill the deck
                    title, text, table = "", "", ""
                    base = {**base, "error": f"slide{i}:{e}"[:200]}
                full = text + " " + table
                strong, weak = _dates_from(full, fdate.year)
                low = full.lower()
                rows.append({**base, "slide_no": i, "title": title, "text": text, "table_text": table,
                             "dates_strong": " ".join(sorted(strong)), "dates_weak": " ".join(sorted(weak)),
                             "temps": " ".join(sorted(set(_TEMP.findall(full)))),
                             "press": " ".join(sorted(set(f"{float(x):g}" for x in _PRESS.findall(full)))),
                             "types": " ".join(k for k, r in _TYPE_RE.items() if r.search(full)),
                             "n_result_words": len(set(w.lower() for w in RESULT_STRONG.findall(full))),
                             "has_table": bool(table)})
            return rows
    except Exception as e:
        base["file_date"] = fdate
        return [{**base, "slide_no": 0, "title": "", "text": "", "table_text": "", "dates_strong": "", "dates_weak": "",
                 "temps": "", "press": "", "types": "", "n_result_words": 0, "has_table": False,
                 "error": f"{type(e).__name__}:{e}"[:200]}]


INDEX_COLS_REQUIRED = ("team", "deck_people", "last_modified_by")


def _roots(slide_roots) -> list[str]:
    if isinstance(slide_roots, (str, Path)):
        slide_roots = [slide_roots]
    return [str(r) for r in slide_roots]


def index_slides(slide_roots, cache_path=None, workers: int = 8, log=print) -> pd.DataFrame:
    """Index every pptx/pptm under one or more roots; ``team`` = root folder name (e.g. 2팀, 4팀)."""
    files = []
    for root in _roots(slide_roots):
        team = Path(root).name
        for d, _, fs in os.walk(root):
            for f in fs:
                if f.startswith("~$") or not f.lower().endswith((".pptx", ".pptm")):
                    continue
                fp = os.path.join(d, f)
                try:
                    st = os.stat(fp)
                    files.append((fp, st.st_size, st.st_mtime, root, team))
                except OSError:
                    pass
    cache = pd.DataFrame()
    if cache_path and Path(cache_path).exists():
        try:
            cache = pd.read_parquet(cache_path)
        except Exception:
            cache = pd.DataFrame()
        if len(cache) and not all(c in cache for c in INDEX_COLS_REQUIRED):
            cache = pd.DataFrame()  # older schema -> reparse
    keep, todo = [], []
    if len(cache):
        sig = cache.groupby("file_path")[["size", "mtime", "team"]].first()
        known = {k: (int(r["size"]), float(r["mtime"]), r["team"]) for k, r in sig.iterrows()}
    else:
        known = {}
    for fp, sz, mt, root, team in files:
        (keep if known.get(fp) == (sz, mt, team) else todo).append((fp, root, team))
    log(f"decks: {len(files)} (cached {len(keep)}, to parse {len(todo)})")
    new = []
    if todo:
        with ThreadPoolExecutor(workers) as ex:
            for n, rows in enumerate(ex.map(lambda a: _parse_deck(*a), todo), 1):
                new.extend(rows)
                if n % 50 == 0:
                    log(f"  parsed {n}/{len(todo)}")
    parts = []
    if len(cache):
        parts.append(cache[cache["file_path"].isin({k[0] for k in keep})])
    if new:
        parts.append(pd.DataFrame(new))
    df = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    if len(df):
        df["file_date"] = pd.to_datetime(df["file_date"])
        if cache_path:
            Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
            df.to_parquet(cache_path, index=False)
    return df


# ----------------------------------------------------------------------------- test side
STOP = re.compile(
    r"^(set\d*|no\d[\d\-no~]*|\d+(\.\d+)?(c|mpa|도|℃|v|ma|mah|a)?|\d+-\d+|bol|eol|capacheck|rpt|cycle|cycles|fm|formation|"
    r"dcir|storage|test|cell|cells|data|new|old|v\d+|ch\d+|retest|re|저장|사이클|구동|정리필요|평가|시험|"
    r"\d+(\.\d+)?(c|mpa)|0\.\d+c|\d+도|dry|wet|reference|mono|bi|oc|rt|mpa|cc|cv)$")
PERSON = re.compile(r"^[가-힣]{3}$")


def _split_tokens(s: str):
    for t in re.split(r"[\s_/,;+()\[\]=:&]+", str(s)):
        n = _norm(t).strip(".")
        if len(n) < 3 or STOP.match(n) or re.fullmatch(r"\d{6,8}(set\d+)?", n):
            continue
        yield n


def _owner_base(x):
    return re.sub(r"\(.*?\)", "", str(x)).strip() if isinstance(x, str) else ""


def _load_tests(project: Path) -> pd.DataFrame:
    t = pd.read_csv(project / "dx" / "dx_tests.csv", dtype={"test_key": str, "requester_code": str})
    for c in ("owner_name", "requester_code", "set_no", "test_type", "test_type_inferred", "sample_name", "tags", "test_name",
              "series_key", "context_path", "temperature_c", "pressure_mpa", "loading_raw", "anomaly", "n_channels"):
        if c not in t:
            t[c] = np.nan
    t["test_date"] = pd.to_datetime(t["test_date"], errors="coerce")
    t["owner_base"] = t["owner_name"].map(_owner_base)
    t["test_type_final"] = t["test_type"].fillna(t["test_type_inferred"])
    return t


def _tags_dict(tags):
    out = {}
    for kv in str(tags if isinstance(tags, str) else "").split(";"):
        if "=" in kv:
            k, v = kv.strip().split("=", 1)
            out.setdefault(k.strip(), []).append(v.strip())
    return out


def _test_tokens(r) -> set:
    toks = set()
    tn = str(r.get("test_name") or "")
    tn = re.sub(r"^\d{5,9}_\d{6}_", "", tn)
    for src in (r.get("sample_name"), tn, r.get("series_key")):
        if isinstance(src, str):
            toks.update(_split_tokens(re.sub(r"^\d{5,9}_\d{6}_", "", src)))
    for k, vs in _tags_dict(r.get("tags")).items():
        for v in vs:
            if ":" in v and re.search(r":\s*[\d.\-]+\s*\w*$", v):
                continue  # numeric ratios / thickness are not identifiers
            toks.update(_split_tokens(v))
    for seg in str(r.get("context_path") or "").split("/"):
        if not PERSON.match(seg.strip()):
            toks.update(_split_tokens(seg))
    return toks


def _design_info(project: Path) -> dict:
    out = {}
    db = project / "db" / "research.sqlite3"
    if not db.exists():
        return out
    c = sqlite3.connect(str(db))
    try:
        d = pd.read_sql("select t.test_key, t.design_id, t.candidate_id, g.design_label, g.status "
                        "from tests t left join designs g on g.design_id=coalesce(t.design_id,t.candidate_id)", c)
    except Exception:
        d = pd.DataFrame()
    c.close()
    for r in d.itertuples():
        out[r.test_key] = (r.design_id if isinstance(r.design_id, str) else r.candidate_id, r.design_label, r.status)
    return out


# ----------------------------------------------------------------------------- matching
def match_tests(project, slide_index: pd.DataFrame, matched_thr=MATCHED_THR, cand_thr=CANDIDATE_THR) -> pd.DataFrame:
    project = Path(project)
    tests = _load_tests(project).reset_index(drop=True)
    S = slide_index[slide_index["slide_no"] > 0].reset_index(drop=True)
    nT, nS = len(tests), len(S)
    if nS == 0:
        return pd.DataFrame()
    ttoks = [_test_tokens(r) for r in tests.to_dict("records")]
    df = pd.Series([t for s in ttoks for t in s]).value_counts()
    vocab = [t for t, c in df.items() if c <= MAX_DF]
    vi = {t: i for i, t in enumerate(vocab)}
    idf = np.array([math.log(nT / df[t]) for t in vocab])
    spec = np.array([df[t] <= SPECIFIC_DF for t in vocab])
    W = np.zeros((nT, len(vocab)), np.float32)
    Wsp = np.zeros_like(W)
    for i, ts in enumerate(ttoks):
        for t in ts:
            if t in vi:
                W[i, vi[t]] = idf[vi[t]]
                Wsp[i, vi[t]] = spec[vi[t]]
    denom = W.sum(1)
    # slide body hits count fully; hits only in the deck file name count half and are never 'specific' evidence
    mtxt = [_norm(f"{a} {b} {c}") for a, b, c in zip(S["title"], S["text"], S["table_text"])]
    ftxt = S["file_name"].str.replace(r"\.pptm?$", "", regex=True).map(_norm)

    def _hits(texts):
        out = np.zeros((len(vocab), len(texts)), bool)
        for ti, tok in enumerate(vocab):
            # separators are stripped from slide text, so only guard the left side against same-class chars
            left = r"(?<![a-z])" if tok[0].isalpha() else r"(?<!\d)"
            pat = re.compile(rf"{left}{re.escape(tok)}(?![a-z0-9])") if tok.isascii() else None
            for j, txt in enumerate(texts):
                if tok in txt and (pat is None or pat.search(txt)):
                    out[ti, j] = True
        return out

    M = _hits(mtxt)
    uf = ftxt.drop_duplicates()
    Mfile = _hits(list(uf))[:, pd.Index(uf).get_indexer(ftxt)] & ~M
    Mf = M.astype(np.float32) + 0.5 * Mfile
    cov = (W @ Mf) / np.maximum(denom, 1e-6)[:, None]
    nspec = Wsp @ M.astype(np.float32)

    # owner: exact names only. Person folder (2팀/<이름>) is strong; deck author/lastModifiedBy is weak evidence.
    folders = S["owner_folder"].fillna("").to_numpy()
    person_f = set(f for f in set(folders) if PERSON.match(f))
    deck_people = [set(str(x).split()) for x in S.get("deck_people", pd.Series([""] * nS)).fillna("")]
    # owner names written in the deck file name (strong) or on the slide itself (weak), e.g. "..._김선준 및 이준호.pptx"
    named_file = [set() for _ in range(nS)]
    body = (S["title"].fillna("") + " " + S["text"].fillna("")).to_numpy()
    fnames = S["file_name"].fillna("").to_numpy()
    for name in set(tests["owner_base"]) - {""}:
        for j in np.flatnonzero(np.char.find(fnames.astype(str), name) >= 0):
            named_file[j].add(name)
        for j in np.flatnonzero(np.char.find(body.astype(str), name) >= 0):
            deck_people[j].add(name)
    all_people = person_f | set().union(*deck_people) | set().union(*named_file)
    own = np.zeros((nT, nS), np.float32)
    for i, r in enumerate(tests.to_dict("records")):
        people = set()
        if r["owner_base"]:
            people.add(r["owner_base"])
        for seg in str(r.get("context_path") or "").split("/"):
            seg = _owner_base(seg.strip())
            if seg in all_people:
                people.add(seg)
        if not people:
            continue
        same = np.isin(folders, list(people)) | np.array([bool(people & nf) for nf in named_file])
        weak = np.array([bool(people & dp) for dp in deck_people]) & ~same
        other = np.isin(folders, list(person_f)) & ~same & ~weak
        own[i] = np.where(same, 2.0, np.where(weak, 1.0, np.where(other, -1.5, 0.0)))
        if not (people & all_people):
            own[i] = 0.0  # owner has no slides anywhere: neither bonus nor penalty

    # date
    sd = S["file_date"].to_numpy("datetime64[D]")
    td = tests["test_date"].to_numpy("datetime64[D]")
    delta = (sd[None, :] - td[:, None]).astype("timedelta64[D]").astype(float)  # NaT -> nan
    prox = np.where((delta >= -3) & (delta <= 120), 1.0, np.where((delta > 120) & (delta <= 240), 0.3,
                    np.where(delta < -3, -1.0, 0.0)))
    inwin = (delta >= -3) & (delta <= 240)
    inv_s, inv_w = {}, {}
    for j, (a, b) in enumerate(zip(S["dates_strong"], S["dates_weak"])):
        for k in a.split():
            inv_s.setdefault(k, []).append(j)
        for k in b.split():
            inv_w.setdefault(k, []).append(j)
    dscore = np.zeros((nT, nS), np.float32)
    for i, d in enumerate(tests["test_date"]):
        if pd.isna(d):
            continue
        for j in inv_s.get(d.strftime("%Y%m%d"), []):
            dscore[i, j] = 3.0
        for j in inv_w.get(d.strftime("%m%d"), []):
            if inwin[i, j] and dscore[i, j] < 1.5:
                dscore[i, j] = 1.5
    # temperature / pressure / type bonus
    extra = np.zeros((nT, nS), np.float32)
    temps = [set(x.split()) for x in S["temps"]]
    press = [set(x.split()) for x in S["press"]]
    types = [set(x.split()) for x in S["types"]]
    tcols = {}
    for key, sets in (("t", temps), ("p", press), ("y", types)):
        vals = set().union(*sets)
        for v in vals:
            tcols[(key, v)] = np.array([v in s for s in sets])
    for i, r in enumerate(tests.to_dict("records")):
        if pd.notna(r["temperature_c"]):
            extra[i] += 0.3 * tcols.get(("t", f"{int(r['temperature_c'])}"), 0)
        if pd.notna(r["pressure_mpa"]):
            extra[i] += 0.3 * tcols.get(("p", f"{float(r['pressure_mpa']):g}"), 0)
        if isinstance(r["test_type_final"], str):
            extra[i] += 0.3 * tcols.get(("y", r["test_type_final"]), 0)

    score = own + prox + dscore + 5.0 * cov + extra
    valid = (dscore >= 3.0) | (nspec > 0) | ((dscore > 0) & (cov > 0))
    score = np.where(valid, score, np.minimum(score, cand_thr - 0.5))
    ii, jj = np.nonzero(score >= cand_thr)
    recs = []
    for i, j in zip(ii, jj):
        hit = [vocab[k] for k in np.nonzero(M[:, j] & (W[i] > 0))[0]]
        ev = []
        if dscore[i, j] >= 3:
            ev.append("date:exact")
        elif dscore[i, j] > 0:
            ev.append("date:md")
        if own[i, j] >= 2:
            ev.append("owner")
        elif own[i, j] > 0:
            ev.append("owner(author)")
        if prox[i, j] > 0.5:
            ev.append("near")
        if hit:
            ev.append("tok:" + ",".join(hit[:8]))
        if extra[i, j] > 0:
            ev.append("cond")
        recs.append((i, j, float(score[i, j]), "; ".join(ev), float(cov[i, j]), float(dscore[i, j]), float(own[i, j])))
    if not recs:
        return pd.DataFrame()
    pr = pd.DataFrame(recs, columns=["ti", "sj", "score", "evidence", "token_cov", "date_score", "owner_score"])
    pr["confidence"] = np.where(pr["score"] >= matched_thr, "matched", "candidate")
    pr["test_key"] = tests["test_key"].to_numpy()[pr["ti"]]
    for c in ["file_path", "file_name", "team", "owner_folder", "slide_no", "title", "file_date"]:
        pr[c] = S[c].to_numpy()[pr["sj"]] if c in S else ""
    pr["slide_row"] = pr["sj"]
    return pr.sort_values(["test_key", "score"], ascending=[True, False]).reset_index(drop=True)


# ----------------------------------------------------------------------------- set-level evidence
RET_TOL, ICE_TOL, CE_TOL = 2.0, 0.3, 0.3   # %p tolerance when a slide number is compared with the DB
# ICE 0.3-0.7 %p apart is shown as "≈" and counted neither way: in the 513 reviewed tests only 15 % (0.3-0.5) and
# 4 % (0.5-0.7) of such "agreements" were the reviewed slide, against 41 % at <= 0.1 %p.
ICE_LOOSE = 0.7


def test_cycle_facts(project) -> dict:
    """test_key -> (discharge_ah[cycle], charge_ah[cycle]) median over the test's runs (cycle 1 = first).

    Runs judged ``defect`` by cell_qc are left out unless every run of the test is.
    """
    p = Path(project) / "db" / "run_cycles.parquet"
    if not p.exists():
        return {}
    c = pd.read_parquet(p, columns=["run_id", "cycle_index", "discharge_capacity_ah", "charge_capacity_ah"])
    c = c[~c["run_id"].str.startswith("ds:")]
    c["test_key"] = c["run_id"].str.split(":", n=1).str[0]
    from .cell_qc import load_status
    bad = {rid for rid, (status, _) in load_status(project).items() if status == "defect"}
    if bad:
        is_bad = c["run_id"].isin(bad)
        all_bad = is_bad.groupby(c["test_key"]).transform("all")
        c = c[~is_bad | all_bad]
    g = c.groupby(["test_key", "cycle_index"], sort=True)[["discharge_capacity_ah", "charge_capacity_ah"]].median()
    out = {}
    for k, d in g.groupby(level=0, sort=False):
        out[k] = (d["discharge_capacity_ah"].to_numpy(float), d["charge_capacity_ah"].to_numpy(float))
    return out


def _metric_n(key: str):
    m = re.fullmatch(r"(retention|ce|discharge|charge)_(\d+)cyc_(pct|mah_g)", key)
    return (m.group(1), int(m.group(2))) if m else (None, None)


def check_metrics(metrics: dict, facts) -> tuple[int, int, str]:
    """Compare slide-reported numbers with the DB cycle series. Returns (n_agree, n_disagree, detail)."""
    if not metrics or facts is None:
        return 0, 0, ""
    d, ch = facts
    if not len(d) or not np.isfinite(d[0]) or d[0] <= 0:
        return 0, 0, ""
    ok = bad = 0
    det = []

    def judge(name, slide_v, db_cands, tol, counts=True, loose=None):
        nonlocal ok, bad
        db_cands = [x for x in db_cands if x is not None and np.isfinite(x)]
        if not db_cands:
            return
        best = min(db_cands, key=lambda x: abs(x - slide_v))
        good = abs(best - slide_v) <= tol
        near = not good and loose is not None and abs(best - slide_v) <= loose
        if counts and not near:  # CE (~99.x% everywhere) is shown but never counted as identifying evidence
            ok, bad = ok + good, bad + (not good)
        det.append(f"{name}: 장표 {slide_v:g} / DB {best:.1f} {'✓' if good else '≈' if near else '✗'}")

    def at(arr, n):
        return float(arr[n - 1]) if 0 < n <= len(arr) else None

    def ratio(a, b):
        return 100.0 * a / b if a is not None and b not in (None, 0) and np.isfinite(b) else None

    refs = [x for x in (at(d, 1), at(d, 2), at(d, 3), np.nanmax(d[:3]) if len(d) else None) if x]
    for key, v in metrics.items():
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if key == "ice_pct":
            judge("ICE", v, [ratio(at(d, 1), at(ch, 1))], ICE_TOL, loose=ICE_LOOSE)
            continue
        fam, n = _metric_n(key)
        if fam == "retention" and n and 5 <= n <= len(d) + 1:
            dn = at(d, min(n, len(d)))
            judge(f"유지율@{n}", v, [ratio(dn, r) for r in refs], RET_TOL)
        elif fam == "ce" and n and n <= len(d):
            judge(f"CE@{n}", v, [ratio(at(d, n), at(ch, n))], CE_TOL, counts=False)
    # discharge@N / discharge@1st reported in mAh/g -> unit-free retention
    d1 = metrics.get("discharge_1st_mah_g")
    for key, v in metrics.items():
        fam, n = _metric_n(key)
        if fam == "discharge" and d1 and n and 5 <= n <= len(d) + 1 and "retention_%dcyc_pct" % n not in metrics:
            try:
                judge(f"방전비@{n}", 100 * float(v) / float(d1), [ratio(at(d, min(n, len(d))), r) for r in refs], RET_TOL)
            except (TypeError, ValueError, ZeroDivisionError):
                pass
    return ok, bad, "; ".join(det)


def _set_no(x):
    m = re.search(r"\d+", str(x)) if x is not None and x == x else None
    return int(m.group(0)) if m else None


def match_sets(project, pairs: pd.DataFrame, sets: pd.DataFrame, tests: pd.DataFrame, facts: dict) -> pd.DataFrame:
    """Refine slide-level pairs with set mentions on the slide (set number, label tokens, dates, numbers).

    Adds set_label / set_attrs / set_metrics / metric_check / set_bonus and recomputes score + confidence
    (``verified`` when slide-reported numbers agree with the DB cycle data).
    """
    import json
    if not len(pairs):
        return pairs
    pairs = pairs.copy()
    for c, v in (("set_label", ""), ("set_attrs", ""), ("set_metrics", ""), ("metric_check", ""),
                 ("set_evidence", ""), ("set_bonus", 0.0), ("n_agree", 0), ("n_disagree", 0), ("set_row", -1)):
        pairs[c] = v
    if sets is None or not len(sets):
        return pairs
    tinfo = {}
    for r in tests.to_dict("records"):
        d = r.get("test_date")
        ymd = d.strftime("%Y%m%d") if pd.notna(d) else ""
        tinfo[r["test_key"]] = (_set_no(r.get("set_no")), ymd, ymd[4:], _test_tokens(r))
    srec = {}  # set_id -> (set_no, label tokens, dates, metrics dict)
    for s in sets.itertuples():
        try:
            met = json.loads(s.metrics) if isinstance(s.metrics, str) and s.metrics else {}
        except ValueError:
            met = {}
        srec[s.Index] = (None if pd.isna(s.set_no) else int(s.set_no),
                         set(_norm(x).strip(".") for x in str(s.label_tokens).split()), set(str(s.dates).split()), met)
    by_slide = {k: list(g.index) for k, g in sets.groupby(["file_path", "slide_no"], sort=False)}
    rows = []
    for idx, key, fp, sn in zip(pairs.index, pairs["test_key"], pairs["file_path"], pairs["slide_no"]):
        ids = by_slide.get((fp, sn))
        if not ids:
            continue
        tset, ymd, md, toks = tinfo[key]
        tf = facts.get(key)
        best = None
        for sid in ids:
            sno, ltok, sd, met = srec[sid]
            bonus, ev = 0.0, []
            if tset is not None and sno is not None:
                if sno == tset:
                    bonus += 1.5
                    ev.append(f"set{sno}")
                else:
                    bonus -= 1.0
            shared = sorted(t_ for t_ in toks if t_ in ltok or (len(t_) >= 4 and any(t_ in x for x in ltok)))
            if shared:
                bonus += min(2.0, 0.8 * len(shared))
                ev.append("label:" + ",".join(shared[:5]))
            if ymd and ymd in sd:
                bonus += 2.0
                ev.append("setdate")
            elif md and md in sd:
                bonus += 1.5
                ev.append("setdate:md")
            n_ok = n_bad = 0
            det = ""
            if ev and met and tf is not None:  # numbers only confirm a set that is already identified
                n_ok, n_bad, det = check_metrics(met, tf)
                if n_ok and not n_bad:
                    bonus += 2.5
                    ev.append(f"수치일치{n_ok}")
                elif n_ok > n_bad:
                    bonus += 0.5
                    ev.append(f"수치일부일치{n_ok}/{n_ok + n_bad}")
                elif n_bad:
                    bonus -= 1.0
                    ev.append(f"수치불일치{n_bad}")
            if best is None or bonus > best[0]:
                best = (bonus, sid, ev, n_ok, n_bad, det)
        if best is None or best[0] <= 0:
            continue
        bonus, sid, ev, n_ok, n_bad, det = best
        s = sets.loc[sid]
        lbl = s["set_label"] if isinstance(s["set_label"], str) else ""
        rows.append((idx, (f"Set{int(s['set_no'])} " if pd.notna(s["set_no"]) else "") + lbl, s["attrs"], s["metrics"],
                     det, "; ".join(ev), bonus, n_ok, n_bad, int(sid)))
    if rows:
        u = pd.DataFrame(rows, columns=["i", "set_label", "set_attrs", "set_metrics", "metric_check", "set_evidence",
                                        "set_bonus", "n_agree", "n_disagree", "set_row"]).set_index("i")
        pairs.loc[u.index, u.columns] = u
    return pairs


def _finalise(pairs: pd.DataFrame, matched_thr=MATCHED_THR, cand_thr=CANDIDATE_THR) -> pd.DataFrame:
    if not len(pairs):
        return pairs
    p = pairs.copy()
    p["slide_score"] = p["score"]
    p["score"] = p["score"] + p["set_bonus"].astype(float)
    p["evidence"] = np.where(p["set_evidence"] != "", p["evidence"] + " | " + p["set_evidence"], p["evidence"])
    ev = p["set_evidence"].astype(str)
    num_ok = ev.str.contains(r"수치일치\d+(?:;|$)")
    # a different person's folder means "someone else's experiment": never auto-verify those
    verified = num_ok & (p["score"] >= cand_thr) & (p["owner_score"] >= 0)
    p["evidence"] = np.where(p["owner_score"] < 0, p["evidence"] + " | 타인폴더", p["evidence"])
    p["confidence"] = np.where(verified, "verified", np.where(p["score"] >= matched_thr, "matched", "candidate"))
    keep = p["score"] >= cand_thr
    # pairs pulled in from below the slide-level threshold need strong set evidence, not just shared tokens
    strong = ev.str.contains("setdate") | num_ok | (ev.str.contains(r"(?:^|; )set\d") & ev.str.contains("label:"))
    p = p[keep & ((p["slide_score"] >= cand_thr) | strong)]
    return p.sort_values(["test_key", "score"], ascending=[True, False]).reset_index(drop=True)


def _prune(pairs: pd.DataFrame, S: pd.DataFrame) -> pd.DataFrame:
    """Drop pair types that never held a reviewed link (none of ~1,660 such pairs for the 513 reviewed tests): pages
    without result words, a cover-like first page of a multi-page deck and decks in another named person's folder."""
    if not len(pairs):
        return pairs
    nrw = S.drop_duplicates(["file_path", "slide_no"]).set_index(["file_path", "slide_no"])["n_result_words"]
    words = nrw.reindex(pd.MultiIndex.from_arrays([pairs["file_path"], pairs["slide_no"]])).fillna(0).to_numpy()
    n_pages = pairs["file_path"].map(S.groupby("file_path")["slide_no"].max()).to_numpy()
    cover = (pairs["slide_no"].to_numpy() == 1) & (n_pages > 1) & (words < 3)
    keep = (words > 0) & ~cover & (pairs["owner_score"].to_numpy() >= 0)
    return pairs[keep].reset_index(drop=True)


# ----------------------------------------------------------------------------- design keywords
_KW = re.compile(
    r"(SJ\s?\d+[A-Za-z]?|MT-?\d+(?:\.\d+)?|JX-?\d+|GC-?\d+|DOE-?\d(?:-\d)?|S40f|LNO|PTFE|PVDF|CNT|SBR|CMC|Super\s?P|bimodal|"
    r"Pilot-?\s?[AB]|\d+도급셀|5080|\d+(?:\.\d+)?\s?mg/cm[²2]|\bL\d\.\d\b|\d+(?:\.\d+)?\s?mAh/cm[²2]|"
    r"\d+(?:\.\d+)?\s?mpa|\b\d{1,2}\s?:\s?\d{1,2}(?:\s?:\s?\d{1,2})?\b|\d+(?:\.\d+)?\s?wt\.?%|"
    r"\d+(?:\.\d+)?\s?(?:um|μm|㎛)|\d{2}\s?(?:도|℃)|건식|습식|압연|calender|믹싱|코팅|바인더|도전재|전해액|집전체)", re.I)
_DESIGN_PARA = re.compile(r"mpa|mg/cm|wt|loading|로딩|비율|ratio|활물질|바인더|도전재|압연|두께|밀도|porosity|공극|전해액|조건|design|설계|BOM", re.I)


def design_keywords(text: str, table: str = "") -> tuple[str, str]:
    kws = []
    for m in _KW.finditer(text + " " + table):
        k = re.sub(r"\s+", " ", m.group(0)).strip()
        if k not in kws:
            kws.append(k)
    paras = [p for p in text.split(" ¶ ") if _DESIGN_PARA.search(p)]
    rows = [r for r in table.split("\n") if _DESIGN_PARA.search(r)]
    return "; ".join(kws[:25]), " / ".join((paras + rows)[:6])[:700]


def _tag_cols(tags):
    d = _tags_dict(tags)
    j = lambda k: "; ".join(d.get(k, []))
    mat = "; ".join(d.get("material", []) + d.get("anode", []) + d.get("cathode", []))
    return pd.Series({"material": mat, "loading_tag": j("loading"), "process": j("process"),
                      "cell_format": j("cell_format"), "supplier": j("supplier")})


# ----------------------------------------------------------------------------- orchestration
SLIDE_TABLES = ("slide_decks", "slide_sets", "slide_test_links")
SLIDE_TABLES_OPT = ("test_slide_facts", "owner_candidates", "slide_link_review", "test_context")


def _slide_owner(folder, people):
    if isinstance(folder, str) and PERSON.match(folder):
        return folder
    ps = str(people or "").split()
    return ps[0] if ps else ""


def _json_get(s, key):
    import json
    try:
        return json.loads(s).get(key) if isinstance(s, str) and s else None
    except ValueError:
        return None


def _conflicts(row) -> str:
    """Slide-stated conditions (set attrs, else slide text) vs DB fields."""
    out = []
    for key, db_key, fmt in (("temperature_c", "temperature_c", "{:g}℃"), ("pressure_mpa", "pressure_mpa", "{:g}MPa")):
        sv = _json_get(row.get("set_attrs"), key)
        dv = row.get(db_key)
        if sv is None or dv is None or dv != dv:
            continue
        try:
            if abs(float(sv) - float(dv)) > 1e-6:
                out.append(f"{key}: 장표 {fmt.format(float(sv))} ≠ DB {fmt.format(float(dv))}")
        except (TypeError, ValueError):
            pass
    return "; ".join(out)


def attach_to_db(project, conn=None) -> int:
    """Copy the slide tables (slides/*.parquet) into research.sqlite3, aggregate slide facts per design and
    (re)create the views v_test_slides / v_test_slide_facts."""
    from .slide_review import design_slide_facts
    project = Path(project)
    src = {t: project / "slides" / f"{t}.parquet" for t in SLIDE_TABLES}
    if not all(p.exists() for p in src.values()):
        return 0
    for t in SLIDE_TABLES_OPT:
        if (project / "slides" / f"{t}.parquet").exists():
            src[t] = project / "slides" / f"{t}.parquet"
    own = conn is None
    if own:
        dbp = project / "db" / "research.sqlite3"
        if not dbp.exists():
            return 0
        conn = sqlite3.connect(str(dbp))
    try:
        n = 0
        for t, p in src.items():
            df = pd.read_parquet(p)
            df.to_sql(t, conn, if_exists="replace", index=False)
            n += len(df)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_stl_test ON slide_test_links(test_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_ss_slide ON slide_sets(file_path, slide_no)")
        lcols = {r[1] for r in conn.execute("PRAGMA table_info(slide_test_links)")}
        rev = ", ".join(f"l.{c}" for c in ("review_status", "review_confidence", "review_design", "review_conditions",
                                           "review_note") if c in lcols)
        conn.execute("DROP VIEW IF EXISTS v_test_slides")
        conn.execute(f"""CREATE VIEW v_test_slides AS
            SELECT l.test_key, t.owner, t.test_date, t.set_no, t.sample_name, t.test_type,
                   coalesce(t.design_id, t.candidate_id) AS design_id, g.design_label,
                   l.confidence, l.score, l.team, l.file_name, l.slide_no, l.slide_title, l.set_label,
                   l.set_attrs, l.set_metrics, l.metric_check, l.evidence, {rev + ',' if rev else ''} l.file_path
            FROM slide_test_links l LEFT JOIN tests t ON t.test_key = l.test_key
            LEFT JOIN designs g ON g.design_id = coalesce(t.design_id, t.candidate_id)""")
        conn.execute("DROP VIEW IF EXISTS v_test_slide_facts")
        if "test_slide_facts" in src:
            conn.execute("CREATE INDEX IF NOT EXISTS ix_tsf_test ON test_slide_facts(test_key)")
            n += design_slide_facts(conn)
            tcols = {r[1] for r in conn.execute("PRAGMA table_info(tests)")}
            tsel = "".join(f"t.{c}, " for c in ("owner", "test_date", "set_no", "sample_name", "test_type",
                                                 "temperature_c", "pressure_mpa") if c in tcols)
            ctx = "test_context" in src
            if ctx:
                conn.execute("CREATE INDEX IF NOT EXISTS ix_tctx_test ON test_context(test_key)")
            conn.execute(f"""CREATE VIEW v_test_slide_facts AS
                SELECT f.test_key, {tsel}coalesce(t.design_id, t.candidate_id) AS design_id, g.design_label,
                       f.review_status, f.link_status, f.slide_file_name, f.slide_no, f.slide_set_label, f.slide_date,
                       f.slide_design, f.slide_conditions, f.slide_chg1_mah_g, f.slide_dchg1_mah_g, f.slide_ice_pct,
                       f.db_dchg1_mah, f.db_ice_pct, f.implied_active_mass_mg, f.metric_check, f.slide_owner_guess,
                       {'x.ctx_reason, x.ctx_detail, x.sibling_slide AS ctx_sibling_slide, ' if ctx else ''}
                       f.slide_attrs_json, f.slide_metrics_json
                FROM test_slide_facts f JOIN tests t ON t.test_key = f.test_key
                LEFT JOIN designs g ON g.design_id = coalesce(t.design_id, t.candidate_id)
                {'LEFT JOIN test_context x ON x.test_key = f.test_key' if ctx else ''}""")
        conn.commit()
        return n
    finally:
        if own:
            conn.close()


def build_slide_link(project, slide_roots, *, workers: int = 8, log=print, write_db: bool = True) -> dict:
    """Index decks under ``slide_roots`` (one or more team folders), extract set mentions and link them to tests."""
    from .slide_facts import extract_sets
    from .slide_context import data_only_context
    from .slide_review import (REVIEW_EXTRA, apply_review, build_test_facts, deck_families, evaluate_auto, load_review,
                               owner_candidates, review_status, slide_copies)
    t0 = time.time()
    project = Path(project)
    roots = _roots(slide_roots)
    out = project / "slides"
    out.mkdir(exist_ok=True)
    idx = index_slides(roots, out / "slide_index.parquet", workers=workers, log=log)
    S = idx[idx["slide_no"] > 0].reset_index(drop=True)
    S["slide_owner"] = [_slide_owner(f, p) for f, p in zip(S["owner_folder"], S["deck_people"])]
    S = pd.concat([S, slide_copies(S)], axis=1)
    sets = extract_sets(S).reset_index(drop=True)
    sets.insert(0, "set_id", np.arange(len(sets)))
    log(f"set mentions: {len(sets)}")
    tests = _load_tests(project)
    facts = test_cycle_facts(project)
    pool = match_tests(project, idx, cand_thr=CANDIDATE_THR - 2.0)
    pairs = _prune(_finalise(match_sets(project, pool, sets, tests, facts)), S)
    review = load_review(project)
    auto_pairs = pairs
    pairs, review_bad = apply_review(pairs, review, S, sets, facts)
    auto_eval = evaluate_auto(auto_pairs, pairs, review, S)
    if len(review):
        log(f"review decisions: {len(review)} rows / {review['test_key'].nunique()} tests, unresolved {len(review_bad)}")
    if len(pairs):
        pairs["rank"] = pairs.groupby("test_key")["score"].rank(method="first", ascending=False).astype(int)
    n_decks = idx["file_path"].nunique()
    errors = idx[idx["error"] != ""][["file_path", "error"]].drop_duplicates("file_path")
    tinfo = tests[["test_key", "owner_name", "test_date", "set_no", "sample_name", "temperature_c", "pressure_mpa"]]

    # --- all links
    lcols = ["test_key", "rank", "confidence", "score", "slide_score", "set_bonus", "team", "owner_folder", "file_name",
             "slide_no", "title", "file_date", "set_label", "set_metrics", "metric_check", "n_agree", "n_disagree",
             "evidence", "set_attrs", "set_row", "file_path"] + REVIEW_EXTRA
    links = pairs[lcols].merge(tinfo, on="test_key", how="left") if len(pairs) else pd.DataFrame(columns=lcols)
    if len(links):
        links["file_date"] = pd.to_datetime(links["file_date"]).dt.strftime("%Y-%m-%d")
        links["test_date"] = links["test_date"].dt.strftime("%Y-%m-%d")
        links["condition_conflict"] = [_conflicts(r) for r in links.to_dict("records")]

    # --- (B) tests without slides
    linked = set(pairs["test_key"]) if len(pairs) else set()
    people_any = set(S["slide_owner"]) | set(" ".join(S["deck_people"].fillna("")).split())
    tb = tests[~tests["test_key"].isin(linked)].copy()
    tb["owner_in_slides"] = tb["owner_base"].isin(people_any)
    rv_status = review_status(review)
    rv_note = (review.groupby("test_key")["note"].agg(lambda x: " | ".join(v for v in x if v)) if len(review)
               else pd.Series(dtype=str))
    tb["review_status"] = [rv_status.get(k, "") for k in tb["test_key"]]
    tb["review_note"] = [rv_note.get(k, "") for k in tb["test_key"]]
    keepc = ["test_key", "owner_name", "requester_code", "test_date", "set_no", "test_type", "test_type_inferred", "sample_name",
             "tags", "temperature_c", "pressure_mpa", "loading_raw", "n_channels", "anomaly", "context_path", "test_name",
             "owner_in_slides", "review_status", "review_note"]
    tb = tb[keepc].copy()
    tb["test_date"] = tb["test_date"].dt.strftime("%Y-%m-%d")

    # --- (C) tests with slides: best link + slide design facts + DB design
    dinfo = _design_info(project)
    cm = pd.DataFrame()
    if len(links):
        best = links[links["rank"] == 1].copy()
        decks = links.groupby("test_key").agg(n_decks=("file_path", "nunique"),
                                              other_decks=("file_name", lambda x: " | ".join(list(dict.fromkeys(x))[1:6])))
        pages = links.groupby(["test_key", "file_path"])["slide_no"].apply(lambda x: ",".join(map(str, sorted(set(x)))))
        best["slide_pages"] = [pages.get((k, f), "") for k, f in zip(best["test_key"], best["file_path"])]
        srow = S.set_index(["file_path", "slide_no"])
        kw = []
        for f, n in zip(best["file_path"], best["slide_no"]):
            r = srow.loc[(f, n)]
            r = r.iloc[0] if isinstance(r, pd.DataFrame) else r
            kw.append(design_keywords(r["text"], r["table_text"]))
        best["slide_design_keywords"] = [k[0] for k in kw]
        best["slide_design_text"] = [k[1] for k in kw]
        cm = best.drop(columns=["owner_name", "test_date", "set_no", "sample_name", "temperature_c", "pressure_mpa"]).merge(
            decks, on="test_key").merge(tests, on="test_key", how="left")
        cm = pd.concat([cm, cm["tags"].apply(_tag_cols)], axis=1)
        cm["design_id"] = cm["test_key"].map(lambda k: dinfo.get(k, (None,) * 3)[0])
        cm["design_label"] = cm["test_key"].map(lambda k: dinfo.get(k, (None,) * 3)[1])
        cm["test_date"] = cm["test_date"].dt.strftime("%Y-%m-%d")
        cm = cm[["test_key", "confidence", "score", "review_status", "owner_name", "test_date", "set_no", "test_type",
                 "test_type_inferred", "sample_name", "material", "loading_tag", "loading_raw", "process", "pressure_mpa",
                 "temperature_c", "cell_format", "supplier", "design_id", "design_label", "team", "file_name", "slide_pages",
                 "title", "file_date", "set_label", "review_design", "review_conditions", "set_attrs", "set_metrics",
                 "metric_check", "condition_conflict", "n_decks", "other_decks", "evidence", "slide_design_keywords",
                 "slide_design_text", "n_channels", "anomaly", "file_path"]].sort_values(["owner_name", "test_date"])

    # --- design-level view
    dz = pd.DataFrame()
    if len(cm):
        dz = cm[cm["design_id"].notna()].groupby(["design_id", "design_label"], dropna=False).agg(
            n_tests=("test_key", "nunique"), n_verified=("confidence", lambda x: int((x == "verified").sum())),
            n_confirmed=("confidence", lambda x: int(x.isin(["confirmed", "probable"]).sum())),
            owners=("owner_name", lambda x: ", ".join(sorted(set(map(str, x.dropna()))))),
            decks=("file_name", lambda x: " | ".join(list(dict.fromkeys(x))[:6])),
            set_labels=("set_label", lambda x: " | ".join([v for v in dict.fromkeys(x) if v][:6])),
            review_designs=("review_design", lambda x: " | ".join([v for v in dict.fromkeys(x) if isinstance(v, str) and v][:4])),
            slide_attrs=("set_attrs", lambda x: " | ".join([v for v in dict.fromkeys(x) if v and v != "{}"][:4])),
        ).reset_index().sort_values("n_tests", ascending=False)

    # --- (A) slides without data: experiment-like slides with no linked test (identical copies count once)
    hit = set(zip(pairs["file_path"], pairs["slide_no"])) if len(pairs) else set()
    nsets = sets.groupby(["file_path", "slide_no"]).size()
    S["n_sets"] = [int(nsets.get((f, n), 0)) for f, n in zip(S["file_path"], S["slide_no"])]
    S["exp_like"] = (S["n_result_words"] >= 2) | ((S["n_result_words"] >= 1) & S["has_table"]) | (S["n_sets"] > 0)
    S["linked"] = [(f, n) in hit for f, n in zip(S["file_path"], S["slide_no"])]
    S["linked_any_copy"] = S["content_hash"].isin(set(S.loc[S["linked"], "content_hash"]))
    sa = S[S["exp_like"] & ~S["linked_any_copy"] & S["is_canonical"]].copy()
    tests_by_owner = tests["owner_base"].value_counts()
    sa["owner_tests_in_db"] = sa["slide_owner"].map(tests_by_owner).fillna(0).astype(int)
    kws = [design_keywords(a, b) for a, b in zip(sa["text"], sa["table_text"])]
    sa["design_tokens"] = [k[0] for k in kws]
    sa["slide_text_snippet"] = sa["text"].str.slice(0, 300)
    sa["file_date"] = sa["file_date"].dt.strftime("%Y-%m-%d")
    sa = sa.rename(columns={"title": "slide_title", "slide_no": "page"})[
        ["team", "slide_owner", "owner_folder", "file_name", "file_date", "page", "slide_title", "n_sets", "design_tokens",
         "n_result_words", "has_table", "owner_tests_in_db", "n_copies", "file_path", "slide_text_snippet"]].sort_values(
        ["team", "slide_owner", "file_name", "page"])
    deck_sum = sa.groupby(["team", "slide_owner", "file_name", "file_path"]).agg(
        n_slides_without_data=("page", "size"), pages=("page", lambda x: ",".join(map(str, x))),
        owner_tests_in_db=("owner_tests_in_db", "first"),
        titles=("slide_title", lambda x: " / ".join(t[:40] for t in list(x)[:4]))).reset_index()
    deck_sum["n_slides_total"] = deck_sum["file_path"].map(S.groupby("file_path").size())
    deck_sum["no_owner_data"] = deck_sum["owner_tests_in_db"] == 0

    # --- set mentions with results but no test (slide-level 'data missing' at Set granularity)
    linked_sets = set(pairs.loc[pairs["set_row"] >= 0, "set_row"]) if len(pairs) else set()
    meta = S[["file_path", "slide_no", "team", "slide_owner", "file_name", "file_date", "title", "content_hash",
              "is_canonical"]]
    sets = sets.merge(meta, on=["file_path", "slide_no"], how="left")
    lt = (pairs[pairs["set_row"] >= 0].groupby("set_row")["test_key"].apply(lambda x: ",".join(dict.fromkeys(x)))
          .to_dict() if len(pairs) else {})
    sets["linked_tests"] = [lt.get(i, "") for i in sets["set_id"]]
    sig = sets["content_hash"].astype(str) + "|" + sets["set_no"].astype(str) + "|" + sets["set_label"].map(_norm)
    linked_sig = set(sig[sets["set_id"].isin(linked_sets)])
    has_num = sets["metrics"].fillna("").str.len() > 2
    ss = sets[has_num & ~sig.isin(linked_sig) & sets["is_canonical"].fillna(True).astype(bool)].copy()
    ss["file_date"] = pd.to_datetime(ss["file_date"]).dt.strftime("%Y-%m-%d")
    ss = ss[["team", "slide_owner", "file_name", "file_date", "slide_no", "title", "set_no", "set_label", "dates", "attrs",
             "metrics", "file_path"]].sort_values(["team", "slide_owner", "file_name", "slide_no"])
    numcmp = links[(links["n_agree"] + links["n_disagree"]) > 0] if len(links) else links
    conflicts = links[links["condition_conflict"] != ""] if len(links) else links

    # --- DB tables (parquet in slides/, copied into research.sqlite3)
    deck_tbl = S.groupby("file_path").agg(
        team=("team", "first"), owner_folder=("owner_folder", "first"), slide_owner=("slide_owner", "first"),
        deck_people=("deck_people", "first"), file_name=("file_name", "first"), file_date=("file_date", "first"),
        n_slides=("slide_no", "size"), n_exp_slides=("exp_like", "sum"), n_sets=("n_sets", "sum"),
        n_linked_slides=("linked", "sum"), n_copied_slides=("is_canonical", lambda x: int((~x).sum()))).reset_index()
    deck_tbl["file_date"] = pd.to_datetime(deck_tbl["file_date"]).dt.strftime("%Y-%m-%d")
    deck_tbl["n_linked_tests"] = deck_tbl["file_path"].map(links.groupby("file_path")["test_key"].nunique()
                                                           if len(links) else {}).fillna(0).astype(int)
    deck_tbl = deck_families(deck_tbl).sort_values(["team", "file_date", "file_name"]).reset_index(drop=True)
    deck_sum = deck_sum.merge(deck_tbl[["file_path", "family_id", "family_rank", "family_size"]], on="file_path", how="left")
    set_tbl = sets.drop(columns=["file_date"]).copy()
    set_tbl["set_no"] = set_tbl["set_no"].astype("Int64")
    link_tbl = links.rename(columns={"title": "slide_title", "set_row": "set_id"}).drop(
        columns=["owner_name", "test_date", "set_no", "sample_name", "temperature_c", "pressure_mpa"], errors="ignore")
    tfacts = build_test_facts(links, review, tests, facts)
    deck_owner = dict(zip(deck_tbl["file_path"], deck_tbl["slide_owner"]))
    owners_sug = owner_candidates(review, links, tests, deck_owner)
    ctx = data_only_context(tests, tfacts, deck_tbl, owners_sug, {k: v[0] for k, v in dinfo.items()},
                            {k: len(v[0]) for k, v in facts.items()})
    ctx_cols = ["test_key", "ctx_reason", "ctx_detail", "sibling_slide"]
    tb = tb.merge(ctx[ctx_cols], on="test_key", how="left")
    rv_tbl = review.copy()
    rv_tbl["resolved"] = ~rv_tbl.index.isin(review_bad.index) | (rv_tbl["decision"] != "link")
    for name, df in (("slide_decks", deck_tbl), ("slide_sets", set_tbl), ("slide_test_links", link_tbl),
                     ("test_slide_facts", tfacts), ("owner_candidates", owners_sug), ("slide_link_review", rv_tbl),
                     ("test_context", ctx)):
        df.to_parquet(out / f"{name}.parquet", index=False)
    n_db = attach_to_db(project) if write_db else 0
    dfacts = pd.DataFrame()
    if n_db:
        c = sqlite3.connect(str(project / "db" / "research.sqlite3"))
        try:
            dfacts = pd.read_sql("SELECT g.design_label, f.* FROM design_slide_facts f "
                                 "LEFT JOIN designs g ON g.design_id = f.design_id "
                                 "ORDER BY f.n_slide_designs DESC, f.n_tests_with_slides DESC", c)
        except (sqlite3.Error, pd.errors.DatabaseError):
            pass
        finally:
            c.close()
    if n_db and (project / "db" / "network.html").exists():
        try:  # refresh slide references shown in the design network
            from .design_network import build_network
            build_network(project)
        except Exception as e:  # the report is still valid without the network refresh
            log(f"network refresh skipped: {e}")

    # --- summary
    conf = cm["confidence"].value_counts() if len(cm) else pd.Series(dtype=int)
    rstat = rv_status.value_counts()
    summ = [("슬라이드 루트", " ; ".join(roots))]
    for team, g in S.groupby("team"):
        summ.append((f"[{team}] 덱 / 슬라이드 / Set 언급", f"{g['file_path'].nunique()} / {len(g)} / {int(g['n_sets'].sum())}"))
    summ += [
        ("파싱 오류 덱", len(errors)), ("실험 DB test 수", len(tests)),
        ("검토 반영 test 수 (link / no_slide / unsure)",
         f"{rv_status.size} ({int(rstat.get('link', 0))} / {int(rstat.get('no_slide', 0))} / {int(rstat.get('unsure', 0))})"),
        ("검토 행 중 장표 위치 확인 실패", len(review_bad)),
        ("(C) 장표 연결 test 수", len(cm)), ("  - confirmed (검토 확정)", int(conf.get("confirmed", 0))),
        ("  - probable (검토, 낮은 확신)", int(conf.get("probable", 0))),
        ("  - verified (장표 수치 = DB 수치)", int(conf.get("verified", 0))),
        ("  - matched (높음)", int(conf.get("matched", 0))), ("  - candidate (검토)", int(conf.get("candidate", 0))),
        ("(B) 장표 없는 test 수", len(tb)), ("  - 검토 결과 장표 없음 확정", int((tb["review_status"] == "no_slide").sum())),
        ("  - 담당자 이름이 장표에 전혀 없음", int((~tb["owner_in_slides"]).sum())),
        ("[데이터만 정황] 확실한 장표 연결 없는 test", len(ctx)),
        *[(f"  - {k}", int(v)) for k, v in ctx["ctx_reason"].value_counts().items()],
        ("  - 같은 실험군 장표 참조 있음", int((ctx["sibling_slide"] != "").sum())),
        ("(A) 데이터 없는 실험성 슬라이드 수 (동일 사본 1회)", len(sa)), ("(A') 결과수치 있는데 test 미연결 Set 언급", len(ss)),
        ("동일 내용 사본 슬라이드 (버전/복사)", int((~S["is_canonical"]).sum())),
        ("덱 버전군 (2개 이상 버전)", int((deck_tbl.drop_duplicates("family_id")["family_size"] > 1).sum())),
        ("장표 1st 방전용량(mAh/g)으로 활물질량 역산된 test", int(tfacts["implied_active_mass_mg"].notna().sum())),
        ("장표 사실이 붙은 DB 설계 / 장표 설계 문구 2종 이상(설계 묶음 점검)",
         f"{len(dfacts)} / {int((dfacts['n_slide_designs'] > 1).sum()) if len(dfacts) else 0}"),
        ("수치 대조 링크 (일치/불일치 포함)", len(numcmp)), ("조건 충돌 링크", len(conflicts)),
        ("research.sqlite3 기록 행", n_db), ("담당자 매칭 규칙", "이름 완전일치만 (별칭 없음)"),
        ("임계값 matched / candidate", f"{MATCHED_THR} / {CANDIDATE_THR}"), ("소요(초)", round(time.time() - t0, 1))]
    for r in auto_eval.itertuples(index=False):
        v = (f"{r.n_hit}/{r.n_tests}" + (f" (Set까지 {int(r.n_set_hit)})" if pd.notna(r.n_set_hit) else "")
             + (f", 같은 보고서군 {int(r.n_family_hit)}" if pd.notna(r.n_family_hit) else ""))
        summ.append((f"[자동매칭 검증] {r.check}", v))
    summ = pd.DataFrame(summ, columns=["항목", "값"])
    tb_owner = tests.set_index("test_key")["owner_base"].replace("", "(미상)")
    by_owner = pd.concat([
        tb["test_key"].map(tb_owner).value_counts().rename("B: 장표 없는 test"),
        sa.groupby("slide_owner").size().rename("A: 데이터 없는 슬라이드"),
        (cm["test_key"].map(tb_owner).value_counts() if len(cm) else pd.Series(dtype=int)).rename("C: 연결 test"),
        (cm.loc[cm["confidence"].isin(["confirmed", "probable"]), "test_key"].map(tb_owner).value_counts() if len(cm)
         else pd.Series(dtype=int)).rename("C 중 검토확정"),
        (cm.loc[cm["confidence"] == "verified", "test_key"].map(tb_owner).value_counts() if len(cm)
         else pd.Series(dtype=int)).rename("C 중 verified")], axis=1).fillna(0).astype(int).rename_axis("담당자").reset_index()
    by_owner["담당자"] = by_owner["담당자"].replace("", "(작성자 미상)")

    def w(df, name):
        df.to_csv(out / name, index=False, encoding="utf-8-sig")
    w(links, "slide_links.csv"); w(sa, "slides_without_data.csv"); w(tb, "tests_without_slides.csv")
    w(cm, "matched_designs.csv"); w(deck_sum, "decks_without_data.csv"); w(ss, "set_mentions_without_data.csv")
    w(tfacts, "test_slide_facts.csv"); w(owners_sug, "owner_candidates.csv"); w(ctx, "test_context.csv")
    ren = {"owner_name": "담당자", "test_date": "시험일", "test_type": "시험종류", "test_type_inferred": "추정종류",
           "sample_name": "샘플", "material": "소재", "loading_tag": "로딩(태그)", "loading_raw": "로딩(원본)", "process": "공정",
           "pressure_mpa": "압력(MPa)", "temperature_c": "온도(C)", "cell_format": "셀포맷", "supplier": "공급처",
           "design_id": "설계ID", "design_label": "설계라벨", "slide_pages": "페이지", "title": "장표제목", "slide_title": "장표제목",
           "file_date": "장표일자", "n_decks": "장표수", "other_decks": "기타장표", "evidence": "근거", "confidence": "신뢰도",
           "score": "점수", "slide_score": "슬라이드점수", "set_bonus": "Set가점", "slide_design_keywords": "장표 설계키워드",
           "slide_design_text": "장표 설계문구", "n_channels": "채널수", "anomaly": "이상", "team": "팀", "slide_owner": "장표담당",
           "owner_folder": "폴더", "file_name": "파일명", "page": "페이지", "slide_no": "페이지", "design_tokens": "설계토큰",
           "owner_tests_in_db": "담당자 DB test수", "file_path": "경로", "slide_text_snippet": "본문발췌", "n_result_words": "결과어수",
           "has_table": "표유무", "tags": "태그", "set_no": "Set", "requester_code": "사번", "context_path": "폴더경로",
           "test_name": "시험명", "owner_in_slides": "담당자 이름이 장표에 있음", "n_slides_without_data": "데이터無 슬라이드수",
           "pages": "페이지들", "titles": "제목들", "n_slides_total": "전체 슬라이드수", "no_owner_data": "담당자 DB 데이터0",
           "set_label": "장표 Set 표기", "set_attrs": "장표 설계/조건", "set_metrics": "장표 결과수치", "metric_check": "수치대조(장표/DB)",
           "condition_conflict": "조건충돌", "n_agree": "일치수", "n_disagree": "불일치수", "n_sets": "Set언급수", "dates": "날짜",
           "attrs": "장표 설계/조건", "metrics": "장표 결과수치", "rank": "순위", "set_row": "SetID", "n_tests": "test수",
           "n_verified": "verified수", "owners": "담당자들", "decks": "장표", "set_labels": "장표 Set 표기들", "slide_attrs": "장표 설계/조건",
           "review_status": "검토상태", "review_note": "검토메모", "review_design": "장표 설계(검토)",
           "review_conditions": "장표 조건(검토)", "review_confidence": "검토확신", "reviewer": "검토자", "owner_guess": "담당자 추정",
           "n_copies": "동일사본수", "family_id": "버전군ID", "family_key": "버전군키", "family_rank": "버전순위(1=최신)",
           "family_size": "버전수", "n_confirmed": "검토확정수", "review_designs": "장표 설계(검토)들", "link_status": "연결상태",
           "n_slide_links": "연결슬라이드수", "slide_file_name": "장표파일", "slide_set_label": "장표 Set", "slide_team": "장표팀",
           "slide_date": "장표일자", "slide_design": "장표 설계", "slide_conditions": "장표 조건",
           "slide_chg1_mah_g": "장표 1st충전(mAh/g)", "slide_dchg1_mah_g": "장표 1st방전(mAh/g)", "slide_ice_pct": "장표 ICE(%)",
           "db_chg1_mah": "DB 1st충전(mAh)", "db_dchg1_mah": "DB 1st방전(mAh)", "db_ice_pct": "DB ICE(%)",
           "implied_active_mass_mg": "역산 활물질량(mg)", "slide_owner_guess": "담당자 추정(장표)", "slide_evidence": "근거",
           "slide_file_path": "경로", "candidate_name": "후보 이름", "n_tests_supporting": "근거 test수",
           "n_code_tests": "사번 test수", "share": "비율", "from_review": "검토근거", "from_decks": "장표근거",
           "suggestion": "판단", "example_tests": "예시 test", "decision": "판정", "note": "메모", "resolved": "장표위치확인",
           "n_copied_slides": "사본 슬라이드수", "n_linked_tests": "연결 test수", "n_linked_slides": "연결 슬라이드수",
           "check": "검증항목", "n_hit": "슬라이드 일치", "n_set_hit": "Set까지 일치", "rate": "비율(Set 기준)",
           "n_family_hit": "같은 보고서군 일치(버전·페이지 무관)", "n_tests_with_slides": "장표 연결 test수",
           "n_slide_designs": "장표 설계 문구 종류", "slide_designs": "장표 설계들", "attr_consensus_json": "장표 속성 합의(JSON)",
           "attr_conflicts": "속성 불일치", "slide_ice_pct_mean": "장표 ICE 평균(%)",
           "slide_dchg1_mah_g_mean": "장표 1st방전 평균(mAh/g)", "implied_active_mass_mg_mean": "역산 활물질량 평균(mg)",
           "ctx_reason": "정황(추정)", "ctx_detail": "정황 근거", "role": "시험 역할", "report_timing": "보고 시점",
           "owner_eff": "담당자(추정 포함)", "owner_basis": "담당자 근거", "owner_first_deck": "담당자 첫 장표",
           "owner_last_deck": "담당자 최신 장표", "n_cycles": "사이클수", "group_basis": "실험군 기준", "n_group": "실험군 test수",
           "n_group_reported": "실험군 보고 test수", "sibling_tests": "보고된 같은 실험군 test", "sibling_slide": "같은 실험군 장표",
           "sibling_common": "샘플명 공통어",
           "sibling_design": "같은 실험군 장표 설계", "design_n_reported": "같은 설계 보고 test수", "ice_dev_pct": "ICE 편차(%p)",
           "ctx_flags": "데이터 신호"}
    tinfo_x = tests[["test_key", "owner_name", "test_date", "sample_name", "test_type_final"]].rename(
        columns={"test_type_final": "test_type"})
    ctx_x = tinfo_x.merge(ctx, on="test_key").sort_values(["ctx_reason", "owner_name", "test_date"])
    ctx_x["test_date"] = ctx_x["test_date"].dt.strftime("%Y-%m-%d")
    xl = out / "slide_link_report.xlsx"
    sheets = [("요약", summ), ("담당자별", by_owner), ("장표O_데이터X", sa), ("장표Set_데이터X", ss),
              ("장표O_데이터X(덱요약)", deck_sum), ("데이터O_장표X", tb), ("데이터만 정황", ctx_x), ("둘다(설계)", cm),
              ("설계별장표", dz),
              ("test별 장표사실", tfacts), ("설계별 장표사실", dfacts), ("검토결과", rv_tbl), ("자동매칭 검증", auto_eval),
              ("사번 담당자 후보", owners_sug),
              ("덱 버전군", deck_tbl[deck_tbl["family_size"] > 1].sort_values(["family_id", "family_rank"])),
              ("수치대조", numcmp), ("조건충돌", conflicts), ("링크후보", links)]
    with pd.ExcelWriter(xl, engine="xlsxwriter") as xw:
        for name, d in sheets:
            d = d.rename(columns=ren) if name not in ("요약", "담당자별") else d
            d = d.map(lambda v: v[:32000] if isinstance(v, str) else v)
            d.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes(1, 0)
            for ci, col in enumerate(d.columns):
                q = d.iloc[:, ci].astype(str).str.len().quantile(0.9) if len(d) else 8
                ws.set_column(ci, ci, min(60, max(8, int(q) + 2 if q == q else 8, len(str(col)) + 2)))
    res = dict(out=str(out), decks=n_decks, slides=len(S), set_mentions=len(sets), errors=errors, summary=summ,
               by_owner=by_owner, slides_without_data=sa, sets_without_data=ss, deck_summary=deck_sum,
               tests_without_slides=tb, matched=cm, designs=dz, links=links, test_facts=tfacts, review=rv_tbl,
               review_unresolved=review_bad, owner_candidates=owners_sug, deck_table=deck_tbl, auto_eval=auto_eval,
               design_facts=dfacts, context=ctx,
               seconds=time.time() - t0)
    log(summ.to_string(index=False))
    return res


DEFAULT_SLIDE_ROOTS = [r"d:\2026\장표\2팀", r"d:\2026\장표\4팀"]


def main(argv=None):
    ap = argparse.ArgumentParser(description="Link slides to cycler tests")
    ap.add_argument("--project", default=str(Path(__file__).resolve().parents[1] / "projects" / "PNE_initial"))
    ap.add_argument("--slides", nargs="+", default=DEFAULT_SLIDE_ROOTS, help="one or more team slide folders")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    build_slide_link(a.project, a.slides, workers=a.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
