"""Extract per-set experiment facts (labels, design attributes, metrics) from slide index rows."""
from __future__ import annotations

import json
import re
from collections import OrderedDict

import pandas as pd

__all__ = ["extract_sets", "metric_family", "metric_key", "parse_dates"]

SET_RE = re.compile(r"^\s*(?:(\d{6})[_\s-]+)?#?\s*set\s*#?\s*(\d{1,3})(?![0-9]|\s*-\s*\d)\s*[:：.)\-_ ]*\s*(.*)$", re.I | re.S)
NUM_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)\s*(?:%|±\s*\d+(?:\.\d+)?\s*%?)?\s*$")
LEAD_NUM_RE = re.compile(r"^\s*([-+]?\d+(?:\.\d+)?)")
SETHDR_RE = re.compile(r"^\s*(?:set\s*(?:#|no\.?|number)?|no\.?|#|셀\s*번호)\s*$", re.I)
HDR_WORDS = {"cycle", "구분", "rate", "항목", "no", "no.", "cell", "set", "item"}
_SITES = ("오창", "대전", "마곡", "청주")

_RE_PTFE = re.compile(r"ptfe\s*[:=]?\s*(\d+(?:\.\d+)?)", re.I)
_RE_POR = re.compile(r"(?<![A-Za-z])P\s*(\d{2})\s*%|(?:공극률|porosity)\s*[:=]?\s*(\d+(?:\.\d+)?)", re.I)
_RE_RATIO = re.compile(r"(?<![\d:.])(\d{1,2}):(\d{1,2})(?![\d:])")
_RE_LOAD = re.compile(r"loading\s*[:=]?\s*(\d+\.\d+)", re.I)
_RE_PRESS = re.compile(r"(\d+(?:\.\d+)?)\s*mpa", re.I)
_RE_TEMP = re.compile(r"(\d{2})\s*(?:℃|°\s*C|도씨)")
_RE_VOLT = re.compile(r"(\d\.\d)\s*[-~]\s*(\d\.\d)\s*V", re.I)
_RE_MAT = re.compile(r"(?<![A-Za-z0-9.])([A-Za-z]{1,4}\d{2,5}[A-Za-z]?)(?![A-Za-z0-9.])")

_RE_D_FULL = re.compile(r"(?<!\d)(20\d{2})[.\-/]?(\d{2})[.\-/]?(\d{2})(?!\d)")
_RE_D_YY = re.compile(r"(?<![\d.])(2\d)[.\-](\d{1,2})[.\-](\d{1,2})(?!\d)")
_RE_D_6 = re.compile(r"(?<!\d)(2\d)(\d{2})(\d{2})(?!\d)")
_RE_D_MD = re.compile(r"(?<![\d./])(\d{1,2})\s*/\s*(\d{1,2})(?![\d/])")
_RE_D_KR = re.compile(r"(\d{1,2})\s*월\s*(\d{1,2})\s*일?")


def _setm(m):
    return int(m.group(2)), ((m.group(1) + " ") if m.group(1) else "") + m.group(3)


def _valid(m, d):
    return 1 <= m <= 12 and 1 <= d <= 31


def parse_dates(s: str) -> list[str]:
    out: list[str] = []
    rest = s or ""
    for rx, kind in ((_RE_D_FULL, "f"), (_RE_D_YY, "f"), (_RE_D_6, "f")):
        def sub(mo, kind=kind):
            a, b, c = mo.groups()
            y = a if len(a) == 4 else "20" + a
            if _valid(int(b), int(c)):
                out.append(f"{y}{int(b):02d}{int(c):02d}")
                return " "
            return mo.group(0)
        rest = rx.sub(sub, rest)
    for rx in (_RE_D_KR, _RE_D_MD):
        def sub2(mo):
            m, d = int(mo.group(1)), int(mo.group(2))
            if _valid(m, d):
                out.append(f"{m:02d}{d:02d}")
                return " "
            return mo.group(0)
        rest = rx.sub(sub2, rest)
    return list(OrderedDict.fromkeys(out))


def _slug(s: str) -> str:
    return re.sub(r"[^\w.]+", "_", s.lower()).strip("_")


def _fmt_c(x: str) -> str:
    f = float(x)
    return str(int(f)) if f == int(f) else str(f)


def metric_key(header: str, ctx: str = "") -> str | None:
    h = (header or "").lower().strip()
    if not h:
        return None
    c = (ctx or "").lower()
    m = re.search(r"(\d+)\s*(?:cyc|cycle|th|사이클|회)", h)
    if m:
        n = int(m.group(1))
    elif re.search(r"1st|first|첫|초기|initial", h):
        n = 1
    else:
        n = None
    has_rate = bool(re.search(r"(?<![\d.])\d+(?:\.\d+)?\s*c\b", h)) and n is None
    if re.search(r"formation|화성|형성", h) and n is None:
        n, has_rate = 1, False
    if re.search(r"retention|유지율|용량\s*유지|cap\.?\s*ret", h):
        return None if has_rate else f"retention_{n}cyc_pct" if n else "retention_pct"
    if re.search(r"\beff|효율|\bce\b|coulomb|\bice\b", h):
        if has_rate:
            return None
        if n in (None, 1):
            return "ice_pct"
        return f"ce_{n}cyc_pct"
    if re.search(r"dcir|\bir\b|저항", h):
        return "dcir_" + (_slug(h) if "dcir" not in _slug(h) else _slug(h).replace("dcir", "", 1).strip("_") or "val")
    if re.search(r"discharge|방전", h):
        return f"discharge_{n}cyc_mah_g" if n and n != 1 else "discharge_1st_mah_g" if n == 1 else None
    if re.search(r"charge|충전", h):
        return f"charge_{n}cyc_mah_g" if n and n != 1 else "charge_1st_mah_g" if n == 1 else None
    m = re.match(r"^\W*(\d+(?:\.\d+)?)\s*c\s*(?:\(\s*%\s*\))?\W*$", h)
    if m:
        return f"rate_{_fmt_c(m.group(1))}c_pct"
    return None


def metric_family(key: str):
    """Map a metric key to (family, n): n is cycle count (or C-rate for rate_*), None if n/a."""
    k = key or ""
    for pat, fam in ((r"^retention_(\d+)cyc_pct$", "retention_pct"), (r"^ce_(\d+)cyc_pct$", "ce_pct"),
                     (r"^discharge_(\d+)cyc_mah_g$", "discharge_mah_g"), (r"^charge_(\d+)cyc_mah_g$", "charge_mah_g")):
        m = re.match(pat, k)
        if m:
            return fam, int(m.group(1))
    m = re.match(r"^(discharge|charge)_1st_mah_g$", k)
    if m:
        return f"{m.group(1)}_mah_g", 1
    if k == "ice_pct":
        return "ice_pct", 1
    m = re.match(r"^rate_(\d+(?:\.\d+)?)c_pct$", k)
    if m:
        return "rate_pct", float(m.group(1))
    if k.startswith("dcir_"):
        return "dcir", None
    return k, None


def _attr_key(name: str) -> str | None:
    h = name.lower()
    if "ptfe" in h:
        return "ptfe_wt"
    if re.search(r"porosity|공극", h):
        return "porosity_pct"
    if "loading" in h or "로딩" in h:
        return "loading_mah_cm2"
    if re.search(r"mpa|압력|pressure", h):
        return "pressure_mpa"
    if re.search(r"온도|temp", h):
        return "temperature_c"
    return None


def _num(cell: str):
    m = NUM_RE.match(cell or "")
    return float(m.group(1)) if m else None


def _label_attrs(text: str) -> dict:
    a: dict = {}
    m = _RE_PTFE.search(text)
    if m:
        a["ptfe_wt"] = float(m.group(1))
    m = _RE_POR.search(text)
    if m:
        a["porosity_pct"] = float(m.group(1) or m.group(2))
    m = _RE_RATIO.search(text)
    if m:
        a["ratio"] = f"{m.group(1)}:{m.group(2)}"
    m = _RE_LOAD.search(text)
    if m:
        a["loading_mah_cm2"] = float(m.group(1))
    m = _RE_PRESS.search(text)
    if m:
        a["pressure_mpa"] = float(m.group(1))
    m = _RE_TEMP.search(text)
    if m:
        a["temperature_c"] = float(m.group(1))
    mats = []
    for x in _RE_MAT.findall(text):
        u = x.upper()
        if re.fullmatch(r"P\d+", u) or u.startswith("MPA"):
            continue
        if u not in mats:
            mats.append(u)
    if mats:
        a["materials"] = mats
    low = text.lower()
    i_d = min([i for i in (low.find("건식"), low.find("dry")) if i >= 0], default=-1)
    i_w = min([i for i in (low.find("습식"), low.find("wet")) if i >= 0], default=-1)
    if i_d >= 0 and (i_w < 0 or i_d < i_w):
        a["wet_dry"] = "dry"
    elif i_w >= 0:
        a["wet_dry"] = "wet"
    for s in _SITES:
        if s in text:
            a["site"] = s
            break
    return a


def _tokens(label: str) -> list[str]:
    toks = []
    s = label.lower()
    for m in _RE_RATIO.finditer(s):
        toks.append(f"{m.group(1)}:{m.group(2)}")
    s = _RE_RATIO.sub(" ", s)
    for t in re.split(r"[\s_:,;()\[\]{}/%+=~·|*]+", s):
        t = t.strip(".-")
        if t:
            toks.append(t)
    return toks


class _Acc:
    __slots__ = ("labels", "attrs", "mvals", "sources", "dates", "reps")

    def __init__(self):
        self.labels: list[str] = []
        self.attrs: dict = {}
        self.mvals: dict[str, list[float]] = {}
        self.sources: list[str] = []
        self.dates: list[str] = []
        self.reps = 1


def _split_table(t: str):
    rows = []
    for line in t.split("\n"):
        if not line.strip():
            continue
        cells = [c.strip() for c in line.split(" | ")]
        if cells and cells[-1].endswith("|"):
            cells[-1] = cells[-1].rstrip("| ").strip()
        rows.append(cells)
    return rows


def _add_label(acc: _Acc, label: str, source: str):
    label = label.strip()
    if label and label not in acc.labels:
        acc.labels.append(label)
        for k, v in _label_attrs(label).items():
            acc.attrs.setdefault(k, v)
        for d in parse_dates(label):
            if d not in acc.dates:
                acc.dates.append(d)
    if source not in acc.sources:
        acc.sources.append(source)


_UNIT_RE = re.compile(r"^(?:m?[ωΩ]|mω|ohm|%|mah/g|[μu]m|mm)$", re.I)
_MULTI_RE = re.compile(r"^[-+]?\d+(?:\.\d+)?(?:\s+[-+]?\d+(?:\.\d+)?)+$")
_AVG = {"avg", "avg.", "average", "평균", "mean"}
_NAME_COLS = {"set_name", "setname", "name", "lot_name", "lot", "샘플명", "시료명", "sample", "set_명"}


def _put_value(acc: _Acc, key: str, val: float, rep: bool):
    lst = acc.mvals.setdefault(key, [])
    if rep or not lst:
        lst.append(val)


def _store_cell(acc, name, ctx, cell, rep):
    """Store one (header/attr name, cell) into acc as metric or attribute."""
    cell = cell.strip()
    if not cell or cell in {"-", "–", "—", "N/A", "n/a"}:
        return
    mk = metric_key(name, ctx)
    v = _num(cell)
    if v is None and _MULTI_RE.match(cell):
        vs = [float(x) for x in cell.split()]
        if mk or not _attr_key(name):
            key = mk or ("raw:" + _slug(name))
            if len(key) <= 64:
                for x in vs:
                    _put_value(acc, key, x, True)
            return
        v = sum(vs) / len(vs)
        cell = str(v)
    if mk and v is not None:
        _put_value(acc, mk, v, rep)
        return
    ak = _attr_key(name)
    if ak:
        m = LEAD_NUM_RE.match(cell)
        if m:
            acc.attrs.setdefault(ak, float(m.group(1)))
            return
    slug = _slug(name)
    if not slug or len(slug) > 60:
        return
    if v is not None:
        _put_value(acc, ("dcir_" if slug.startswith("soc_") or re.search("저항|resist|dcir", slug) and not slug.startswith("dcir") else "raw:") + slug, v, rep)
        return
    if slug in _NAME_COLS:
        _add_label(acc, cell, "table_row")
        return
    if len(cell) <= 40 and slug not in {"note", "비고"}:
        acc.attrs.setdefault("x:" + slug, cell)
        for k, vv in _label_attrs(cell).items():
            acc.attrs.setdefault(k, vv)


def _table_mentions(rows, get):
    header: list[str] | None = None
    ctx = ""
    block_has_data = False
    subnames = None
    last_first = ""
    grouped = False
    trans_cols: list | None = None  # per column (set_no|None, label)
    cur = None
    blk: dict = {}

    def fill_row(acc, rest, rep):
        for j, c in enumerate(rest, 1):
            nm = header[j] if j < len(header) else ""
            if nm:
                _store_cell(acc, nm, ctx, c, rep)

    def start_trans(cols, first):
        nonlocal trans_cols, header, block_has_data, cur, ctx, subnames, grouped, last_first
        trans_cols = cols
        header, block_has_data, cur = None, False, None
        ctx, subnames, last_first = first, None, ""
        keys = [c[0] if c[0] is not None else c[1] for c in cols if c]
        grouped = len(set(keys)) < len(keys)

    for cells in rows:
        first = cells[0] if cells else ""
        rest = cells[1:]
        if not SET_RE.match(first):
            setcols = [SET_RE.match(c) for c in rest]
            n_set = sum(1 for m in setcols if m)
            if n_set >= 2 and n_set >= len([c for c in rest if c]) * 0.6:
                cols = []
                for m in setcols:
                    cols.append(_setm(m) if m else (cols[-1] if cols else None))
                start_trans(cols, first)
                continue
            if SETHDR_RE.match(first) and len([c for c in rest if c]) >= 2 and all(
                re.fullmatch(r"#?\d{1,3}", c) for c in rest if c
            ):
                cols = []
                for c in rest:
                    cols.append((int(c.lstrip("#")), "") if c else (cols[-1] if cols else None))
                start_trans(cols, first)
                continue
        sm = SET_RE.match(first)
        if (not sm and header and SETHDR_RE.match(header[0]) and re.fullmatch(r"#?\d{1,3}", first)
                and any(_num(c) is not None for c in rest)):
            sm = SET_RE.match("set" + first.lstrip("#"))
        if first.lower() in _AVG and header is not None:
            continue
        if sm:
            if header is None:
                header = []
            sn, lab = _setm(sm)
            if re.search(r"set\s*#?\s*\d", lab, re.I):
                continue
            trans_cols = None
            acc = get(sn, lab)
            _add_label(acc, lab, "table_row")
            rep = id(acc) in blk
            blk.setdefault(id(acc), True)
            cur = acc
            block_has_data = True
            fill_row(acc, rest, rep)
            continue
        has_num = any(_num(c) is not None for c in rest)
        if trans_cols is not None:
            if (grouped or first.lower().startswith("soc")) and not has_num and any(rest):
                subnames = rest
                if first:
                    ctx = first
            elif any(c for c in rest):
                if first:
                    last_first = first
                labels = [c for j, c in enumerate(rest)
                          if (j >= len(trans_cols) or not trans_cols[j]) and c and _num(c) is None
                          and not _UNIT_RE.match(c)]
                base = first or last_first
                if labels:
                    base = "_".join([base] + labels)
                num_first = _num(first) is not None
                for j, c in enumerate(rest):
                    if j < len(trans_cols) and trans_cols[j] and c:
                        sn, lab = trans_cols[j]
                        acc = get(sn, lab)
                        _add_label(acc, lab, "table_col")
                        name = base
                        if subnames and j < len(subnames) and subnames[j]:
                            name = f"{base}_{subnames[j]}"
                        if num_first:
                            name = f"{ctx}_{name}"
                        _store_cell(acc, name, "", c, False)
            continue
        if (first and has_num and header is not None and first.lower() not in HDR_WORDS
                and not re.match(r"^cycle", first.lower()) and not re.fullmatch(r"[\d.\s]+", first)
                and any(metric_key(header[j], ctx) and _num(c) is not None
                        for j, c in enumerate(rest, 1) if j < len(header))):
            acc = get(None, first)
            _add_label(acc, first, "table_row")
            rep = id(acc) in blk
            blk.setdefault(id(acc), True)
            cur = acc
            block_has_data = True
            fill_row(acc, rest, rep)
            continue
        if not first and has_num and cur is not None and header is not None:
            fill_row(cur, rest, True)
            cur.reps += 1
            continue
        if block_has_data:
            header, block_has_data, cur, blk = None, False, None, {}
        if header is None or not any(header):
            header = list(cells)
            ctx = first
        else:
            prev = ""
            for j in range(1, len(header)):
                if header[j]:
                    prev = header[j]
                elif prev and j < len(cells) and cells[j]:
                    header[j] = prev
            for j, c in enumerate(cells):
                if j < len(header):
                    if c and j:
                        header[j] = (header[j] + " " + c).strip()
                else:
                    header.append(c)

_SLIDE_TEMP = _RE_TEMP
_SET_LINE = re.compile(r"^\s*(?:\d{6}[_\s-]+)?#?\s*set\s*#?\s*\d{1,3}(?![0-9])", re.I)


def extract_sets(index: pd.DataFrame) -> pd.DataFrame:
    out = []
    cols = ["file_path", "slide_no", "text", "table_text"]
    for fp, sno, text, table in zip(*(index[c].tolist() for c in cols)):
        text = text if isinstance(text, str) else ""
        table = table if isinstance(table, str) else ""
        sets: dict = {}

        def get(sn, lab, sets=sets):
            if sn is not None:
                key = ("n", sn)
            else:
                key = ("l", _slug(lab) or lab)
            if key not in sets:
                sets[key] = (sn, _Acc())
            return sets[key][1]

        if table and ("Set" in table or "set" in table.lower() or "\n" in table):
            _table_mentions(_split_table(table), get)
        slide_ctx: dict = {}
        for line in text.split(" ¶ "):
            sm = SET_RE.match(line) if _SET_LINE.match(line) else None
            if sm:
                sn, lab = _setm(sm)
                acc = get(sn, lab)
                if not lab.strip() and acc.labels:
                    continue
                _add_label(acc, lab, "text")
            else:
                tm = {m.group(1) for m in _RE_TEMP.finditer(line)}
                if tm:
                    slide_ctx.setdefault("temps", set()).update(tm)
                vm = _RE_VOLT.search(line)
                if vm:
                    lo, hi = sorted((float(vm.group(1)), float(vm.group(2))))
                    slide_ctx.setdefault("volts", set()).add(f"{lo}-{hi}")
        if not sets:
            continue
        for (kind, _), (sn, acc) in sets.items():
            attrs = dict(acc.attrs)
            temps = slide_ctx.get("temps")
            if temps and len(temps) == 1:
                attrs.setdefault("temperature_c", float(next(iter(temps))))
            volts = slide_ctx.get("volts")
            if volts and len(volts) == 1:
                attrs.setdefault("voltage_range", next(iter(volts)))
            metrics = {k: round(sum(v) / len(v), 4) for k, v in acc.mvals.items() if v}
            nvals = max([len(v) for v in acc.mvals.values()] + [0])
            label = next((l for l in acc.labels if l), "")
            if sn is None and not metrics:
                continue
            toks = list(OrderedDict.fromkeys(t for l in acc.labels for t in _tokens(l)))
            src = next((s for s in ("table_row", "table_col", "text") if s in acc.sources), "text")
            out.append((fp, sno, sn, label, " ".join(toks), " ".join(acc.dates),
                        json.dumps(attrs, ensure_ascii=False), json.dumps(metrics, ensure_ascii=False),
                        src, nvals))
    df = pd.DataFrame(out, columns=["file_path", "slide_no", "set_no", "set_label", "label_tokens",
                                    "dates", "attrs", "metrics", "source", "n_values"])
    df["set_no"] = df["set_no"].astype("Int64")
    return df