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
# DB owner -> slide folder presumed to be the same person (DB 임수현 has no folder; 양수현 decks mention its tests)
OWNER_ALIAS = {"임수현": ["양수현"]}
MAX_DF = 60               # tokens in more tests than this are ignored (generic)

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


def _parse_deck(path: str, root: str):
    p = Path(path)
    rel = p.relative_to(root)
    parts = rel.parts
    owner = parts[0] if len(parts) > 1 else ""
    st = p.stat()
    base = dict(file_path=str(p), owner_folder=owner, file_name=p.name, rel_dir="/".join(parts[:-1]),
                size=st.st_size, mtime=st.st_mtime, author="", modified="", error="")
    fdate = _file_date(p.stem)
    try:
        with zipfile.ZipFile(path) as z:
            try:
                core = z.read("docProps/core.xml").decode("utf8", "ignore")
                m = re.search(r"<dc:creator>(.*?)</dc:creator>", core, re.S)
                base["author"] = m.group(1) if m else ""
                m = re.search(r"<dcterms:modified[^>]*>(.*?)</dcterms:modified>", core, re.S)
                base["modified"] = m.group(1)[:10] if m else ""
            except Exception:
                pass
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


def index_slides(slide_root, cache_path=None, workers: int = 8, log=print) -> pd.DataFrame:
    slide_root = str(slide_root)
    files = []
    for d, _, fs in os.walk(slide_root):
        for f in fs:
            if f.startswith("~$") or not f.lower().endswith((".pptx", ".pptm")):
                continue
            fp = os.path.join(d, f)
            try:
                st = os.stat(fp)
                files.append((fp, st.st_size, st.st_mtime))
            except OSError:
                pass
    cache = pd.DataFrame()
    if cache_path and Path(cache_path).exists():
        try:
            cache = pd.read_parquet(cache_path)
        except Exception:
            cache = pd.DataFrame()
    keep, todo = [], []
    if len(cache):
        sig = cache.groupby("file_path")[["size", "mtime"]].first()
        known = {k: (int(r["size"]), float(r["mtime"])) for k, r in sig.iterrows()}
    else:
        known = {}
    for fp, sz, mt in files:
        (keep if known.get(fp) == (sz, mt) else todo).append(fp)
    log(f"decks: {len(files)} (cached {len(keep)}, to parse {len(todo)})")
    new = []
    if todo:
        with ThreadPoolExecutor(workers) as ex:
            for n, rows in enumerate(ex.map(lambda f: _parse_deck(f, slide_root), todo), 1):
                new.extend(rows)
                if n % 50 == 0:
                    log(f"  parsed {n}/{len(todo)}")
    parts = []
    if len(cache):
        parts.append(cache[cache["file_path"].isin(set(keep))])
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
    t = pd.read_csv(project / "dx" / "dx_tests.csv", dtype={"test_key": str})
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

    # owner
    folders = S["owner_folder"].to_numpy()
    person_f = set(f for f in set(folders) if PERSON.match(f))
    own = np.zeros((nT, nS), np.float32)
    for i, r in enumerate(tests.to_dict("records")):
        people = set()
        if r["owner_base"]:
            people.add(r["owner_base"])
            people.update(OWNER_ALIAS.get(r["owner_base"], []))
        for seg in str(r.get("context_path") or "").split("/"):
            seg = _owner_base(seg.strip())
            if seg in person_f:
                people.add(seg)
        if not people:
            continue
        same = np.isin(folders, list(people))
        other = np.isin(folders, list(person_f)) & ~same
        own[i] = np.where(same, 2.0, np.where(other, -1.5, 0.0))
        if r["owner_base"] and not (people & person_f):
            own[i] = np.where(same, 2.0, 0.0)  # owner has no slide folder: no penalty

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
        if own[i, j] > 0:
            ev.append("owner")
        if prox[i, j] > 0.5:
            ev.append("near")
        if hit:
            ev.append("tok:" + ",".join(hit[:8]))
        if extra[i, j] > 0:
            ev.append("cond")
        recs.append((i, j, float(score[i, j]), "; ".join(ev), float(cov[i, j]), float(dscore[i, j])))
    if not recs:
        return pd.DataFrame()
    pr = pd.DataFrame(recs, columns=["ti", "sj", "score", "evidence", "token_cov", "date_score"])
    pr["confidence"] = np.where(pr["score"] >= matched_thr, "matched", "candidate")
    pr["test_key"] = tests["test_key"].to_numpy()[pr["ti"]]
    for c in ["file_path", "file_name", "owner_folder", "slide_no", "title", "file_date"]:
        pr[c] = S[c].to_numpy()[pr["sj"]]
    pr["slide_row"] = pr["sj"]
    return pr.sort_values(["test_key", "score"], ascending=[True, False]).reset_index(drop=True)


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
def build_slide_link(project, slide_root, *, workers: int = 8, log=print) -> dict:
    t0 = time.time()
    project = Path(project)
    out = project / "slides"
    out.mkdir(exist_ok=True)
    idx = index_slides(slide_root, out / "slide_index.parquet", workers=workers, log=log)
    tests = _load_tests(project)
    pairs = match_tests(project, idx)
    S = idx[idx["slide_no"] > 0].reset_index(drop=True)
    n_decks = idx["file_path"].nunique()
    errors = idx[idx["error"] != ""][["file_path", "error"]].drop_duplicates("file_path")

    # --- all pairs
    links = pairs.drop(columns=["ti", "sj", "slide_row"]) if len(pairs) else pd.DataFrame()
    if len(links):
        links = links.merge(tests[["test_key", "owner_name", "test_date", "sample_name"]], on="test_key", how="left")
        links["file_date"] = links["file_date"].dt.strftime("%Y-%m-%d")
        links["test_date"] = links["test_date"].dt.strftime("%Y-%m-%d")
    # --- (B) tests without slides
    linked = set(pairs["test_key"]) if len(pairs) else set()
    best = pairs.groupby("test_key")["score"].max() if len(pairs) else pd.Series(dtype=float)
    tb = tests[~tests["test_key"].isin(linked)].copy()
    tb["best_score_below_thr"] = tb["test_key"].map(
        # best sub-threshold score is not tracked; mark owner folder availability instead
        lambda k: np.nan)
    tb = tb.drop(columns=["best_score_below_thr"])
    folders = set(S["owner_folder"])
    tb["owner_has_slide_folder"] = tb["owner_base"].isin(folders) | tb["owner_base"].map(
        lambda o: any(a in folders for a in OWNER_ALIAS.get(o, [])))
    keepc = ["test_key", "owner_name", "requester_code", "test_date", "set_no", "test_type", "test_type_inferred", "sample_name",
             "tags", "temperature_c", "pressure_mpa", "loading_raw", "n_channels", "anomaly", "context_path", "test_name",
             "owner_has_slide_folder"]
    tb = tb[keepc].copy()
    tb["test_date"] = tb["test_date"].dt.strftime("%Y-%m-%d")

    # --- (C) matched designs (one row per test)
    dinfo = _design_info(project)
    cm = pd.DataFrame()
    if len(pairs):
        pr = pairs.copy()
        pr["rank"] = pr.groupby("test_key")["score"].rank(method="first", ascending=False)
        bestp = pr[pr["rank"] == 1].set_index("test_key")
        # best deck = deck of best slide; all slides of that deck above threshold listed
        rows = []
        for k, g in pr.groupby("test_key"):
            b = g.iloc[0]
            deck = g[g["file_path"] == b["file_path"]]
            decks_all = g.groupby("file_path")["score"].max().sort_values(ascending=False)
            s = S.iloc[int(b["slide_row"])]
            kw, dtxt = design_keywords(s["text"], s["table_text"])
            rows.append(dict(test_key=k, confidence=("matched" if b["score"] >= MATCHED_THR else "candidate"),
                             score=round(b["score"], 2), evidence=b["evidence"], slide_owner_folder=b["owner_folder"],
                             deck_path=b["file_path"], slide_pages=",".join(str(int(x)) for x in sorted(deck["slide_no"])),
                             slide_title=b["title"], slide_file_date=b["file_date"].strftime("%Y-%m-%d"),
                             n_decks=len(decks_all), other_decks=" | ".join(Path(p).name for p in decks_all.index[1:6]),
                             slide_design_keywords=kw, slide_design_text=dtxt))
        cm = pd.DataFrame(rows).merge(tests, on="test_key", how="left")
        cm = pd.concat([cm, cm["tags"].apply(_tag_cols)], axis=1)
        cm["design_id"] = cm["test_key"].map(lambda k: dinfo.get(k, (None,) * 3)[0])
        cm["design_label"] = cm["test_key"].map(lambda k: dinfo.get(k, (None,) * 3)[1])
        cm["test_date"] = cm["test_date"].dt.strftime("%Y-%m-%d")
        cm["test_type_final"] = cm["test_type_final"]
        cm = cm[["test_key", "confidence", "score", "owner_name", "test_date", "test_type", "test_type_inferred", "sample_name",
                 "material", "loading_tag", "loading_raw", "process", "pressure_mpa", "temperature_c", "cell_format", "supplier",
                 "design_id", "design_label", "deck_path", "slide_pages", "slide_title", "slide_file_date", "n_decks", "other_decks",
                 "evidence", "slide_design_keywords", "slide_design_text", "n_channels", "anomaly"]].sort_values(
            ["owner_name", "test_date"])

    # --- (A) slides without data
    hit_rows = set(pairs["slide_row"]) if len(pairs) else set()
    S["exp_like"] = (S["n_result_words"] >= 2) | ((S["n_result_words"] >= 1) & S["has_table"])
    sa = S[S["exp_like"] & ~S.index.isin(hit_rows)].copy()
    tests_by_owner = tests["owner_base"].value_counts()
    for src, dsts in OWNER_ALIAS.items():
        for dst in dsts:
            tests_by_owner[dst] = tests_by_owner.get(dst, 0) + tests_by_owner.get(src, 0)
    sa["owner_tests_in_db"] = sa["owner_folder"].map(tests_by_owner).fillna(0).astype(int)
    kws = [design_keywords(a, b) for a, b in zip(sa["text"], sa["table_text"])]
    sa["design_tokens"] = [k[0] for k in kws]
    sa["slide_text_snippet"] = sa["text"].str.slice(0, 300)
    sa["file_date"] = sa["file_date"].dt.strftime("%Y-%m-%d")
    sa = sa.rename(columns={"title": "slide_title", "slide_no": "page"})[
        ["owner_folder", "file_name", "file_date", "page", "slide_title", "design_tokens", "n_result_words", "has_table",
         "owner_tests_in_db", "file_path", "slide_text_snippet"]].sort_values(["owner_folder", "file_name", "page"])
    deck_sum = sa.groupby(["owner_folder", "file_name", "file_path"]).agg(
        n_slides_without_data=("page", "size"), pages=("page", lambda x: ",".join(map(str, x))),
        owner_tests_in_db=("owner_tests_in_db", "first"),
        titles=("slide_title", lambda x: " / ".join(t[:40] for t in list(x)[:4]))).reset_index()
    deck_sum["n_slides_total"] = deck_sum["file_path"].map(S.groupby("file_path").size())
    deck_sum["no_owner_data"] = deck_sum["owner_tests_in_db"] == 0

    # --- summary
    n_cm = cm["test_key"].nunique() if len(cm) else 0
    n_hi = int((cm["confidence"] == "matched").sum()) if len(cm) else 0
    summ = pd.DataFrame([
        ("슬라이드 루트", str(slide_root)), ("덱 수", n_decks), ("슬라이드 수", len(S)), ("파싱 오류 덱", len(errors)),
        ("실험 DB test 수", len(tests)),
        ("(C) 매칭 test 수 (high+candidate)", n_cm), ("  - high(matched)", n_hi), ("  - candidate(검토)", n_cm - n_hi),
        ("(B) 장표 없는 test 수", len(tb)), ("(A) 데이터 없는 실험성 슬라이드 수", len(sa)),
        ("  - 그 중 owner 폴더 DB 데이터 0건 슬라이드", int((sa["owner_tests_in_db"] == 0).sum())),
        ("임계값 matched / candidate", f"{MATCHED_THR} / {CANDIDATE_THR}"), ("소요(초)", round(time.time() - t0, 1)),
    ], columns=["항목", "값"])
    by_owner = pd.concat([
        tb["owner_name"].fillna("(미상)").value_counts().rename("B: 장표 없는 test"),
        sa["owner_folder"].value_counts().rename("A: 데이터 없는 슬라이드"),
        (cm["owner_name"].fillna("(미상)").value_counts() if len(cm) else pd.Series(dtype=int)).rename("C: 매칭 test")], axis=1
    ).fillna(0).astype(int).rename_axis("owner").reset_index()

    # --- write
    def w(df, name):
        df.to_csv(out / name, index=False, encoding="utf-8-sig")
    w(links, "slide_links.csv"); w(sa, "slides_without_data.csv"); w(tb, "tests_without_slides.csv")
    w(cm, "matched_designs.csv"); w(deck_sum, "decks_without_data.csv")
    ren = {"test_key": "test_key", "owner_name": "담당자", "test_date": "시험일", "test_type": "시험종류", "test_type_inferred": "추정종류",
           "sample_name": "샘플", "material": "소재", "loading_tag": "로딩(태그)", "loading_raw": "로딩(원본)", "process": "공정",
           "pressure_mpa": "압력(MPa)", "temperature_c": "온도(C)", "cell_format": "셀포맷", "supplier": "공급처",
           "design_id": "설계ID", "design_label": "설계라벨", "deck_path": "장표파일", "slide_pages": "페이지", "slide_title": "장표제목",
           "slide_file_date": "장표일자", "n_decks": "장표수", "other_decks": "기타장표", "evidence": "근거", "confidence": "신뢰도",
           "score": "점수", "slide_design_keywords": "장표 설계키워드", "slide_design_text": "장표 설계문구", "n_channels": "채널수",
           "anomaly": "이상", "owner_folder": "폴더(담당)", "file_name": "파일명", "file_date": "파일일자", "page": "페이지",
           "design_tokens": "설계토큰", "owner_tests_in_db": "담당자 DB test수", "file_path": "경로", "slide_text_snippet": "본문발췌",
           "n_result_words": "결과어수", "has_table": "표유무", "tags": "태그", "set_no": "Set", "requester_code": "사번",
           "context_path": "폴더경로", "test_name": "시험명", "owner_has_slide_folder": "담당자 장표폴더 있음",
           "n_slides_without_data": "데이터無 슬라이드수", "pages": "페이지들", "titles": "제목들", "n_slides_total": "전체 슬라이드수",
           "no_owner_data": "담당자 DB 데이터0", "token_cov": "토큰커버리지", "date_score": "날짜점수", "slide_no": "페이지",
           "title": "장표제목"}
    xl = out / "slide_link_report.xlsx"
    with pd.ExcelWriter(xl, engine="openpyxl") as xw:
        sheets = [("요약", summ), ("담당자별", by_owner), ("장표O_데이터X", sa), ("장표O_데이터X(덱요약)", deck_sum),
                  ("데이터O_장표X", tb), ("둘다(설계)", cm), ("링크후보", links)]
        for name, d in sheets:
            d = d.rename(columns=ren) if name not in ("요약", "담당자별") else d
            d = d.map(lambda v: v[:32000] if isinstance(v, str) else v)
            d.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            for ci, col in enumerate(d.columns, 1):
                q = d.iloc[:, ci - 1].astype(str).str.len().quantile(0.9) if len(d) else 8
                width = min(60, max(8, int(q) + 2 if q == q else 8, len(str(col)) + 2))
                ws.column_dimensions[ws.cell(1, ci).column_letter].width = width
    res = dict(out=str(out), decks=n_decks, slides=len(S), errors=errors, summary=summ, by_owner=by_owner,
               slides_without_data=sa, deck_summary=deck_sum, tests_without_slides=tb, matched=cm, links=links,
               seconds=time.time() - t0)
    log(summ.to_string(index=False))
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description="Link slides to cycler tests")
    ap.add_argument("--project", default=str(Path(__file__).resolve().parents[1] / "projects" / "PNE_initial"))
    ap.add_argument("--slides", default=r"d:\2026\장표\2팀")
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args(argv)
    build_slide_link(a.project, a.slides, workers=a.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
