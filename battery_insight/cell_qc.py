"""Conservative per-run QC screening, not a diagnosis of physical failure.

Relative rules require a known, matching test/protocol and usable peer evidence.
``defect`` retains the downstream exclusion meaning, not causal certainty.
Incomplete starts and dead-like observations are at most ``suspect``. Missing
evidence is ``not_assessed``, not ``ok``; per-rule coverage is returned separately.
Human keep/exclude decisions always win, including runs with no cycle rows.
Optional configuration is read-only; malformed configuration raises ValueError.
"""
from __future__ import annotations

import csv
import json
from io import StringIO
from numbers import Real
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULTS = {
    "ice_drop_pp": 5.0,          # ICE this many %p below the other channels -> low_ice
    "ice_max_valid": 105.0,      # ICE above this is a bookkeeping artefact, ignored
    "ice_cont_pct": 95.0,        # possible post-formation context; do not pool with lower initial ICE
    "ce_drop_pp": 2.0,           # median CE this many %p below the others -> low_ce
    "ce_max_valid": 101.0,       # median CE above this is a bookkeeping artefact, ignored
    "fade_ratio": 0.5,           # cycles-to-80 % below this x the others -> early_fade
    "fade_min_cycles": 20,       # ... only when the others last at least this many cycles
    "cap_low": 0.85,             # 1st discharge below this x the others -> low_capacity (suspect)
    "cap_high": 1.15,            # 1st discharge above this x the others -> high_capacity (suspect)
    "dead_ah": 1e-3,             # low/no throughput is unconfirmed, never an automatic physical diagnosis
    "dead_ice_pct": 5.0,         # low discharge/charge ratio -> suspect only
    "min_siblings_defect": 2,    # other channels needed for an automatic 'defect'
    "single_sibling_factor": 2.0,  # with one other channel: 2x the %p drop, or half the life ratio
    "majority_share": 0.5,       # 3+ channel test: a rule tripped by this share of channels -> suspect only
    "single_ice_floor": 50.0,    # no comparable peers: ICE below this -> suspect, never defect
}
CRITERIA_FILE = "criteria.json"
OVERRIDE_FILE = Path("dx") / "cell_qc_override.csv"
OVERRIDE_COLUMNS = ["run_id", "decision", "note"]
DEFECT_RULES = {"low_ice", "low_ce", "early_fade"}


def load_criteria(project) -> dict:
    """Read optional criteria without creating or changing any project files."""
    path = Path(project) / CRITERIA_FILE
    if not path.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique_pairs)
        if not isinstance(data, dict):
            raise ValueError("criteria root must be an object")
        return _criteria(data["cell_qc"] if "cell_qc" in data else {})
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def _unique_pairs(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key: {key}")
        out[key] = value
    return out


def _criteria(values):
    if not isinstance(values, dict):
        raise ValueError("cell_qc criteria must be an object")
    unknown = set(values) - DEFAULTS.keys()
    if unknown:
        raise ValueError(f"unknown cell_qc criteria: {sorted(unknown, key=str)}")
    crit = {**DEFAULTS, **values}
    for key, value in crit.items():
        if isinstance(value, bool) or not isinstance(value, Real) or not np.isfinite(value):
            raise ValueError(f"cell_qc.{key} must be a finite number")
        if value <= 0:
            raise ValueError(f"cell_qc.{key} must be positive")
    for key in ("min_siblings_defect", "fade_min_cycles"):
        if int(crit[key]) != crit[key]:
            raise ValueError(f"cell_qc.{key} must be an integer")
    if not (0 < crit["fade_ratio"] < 1 and 0 < crit["cap_low"] < 1 < crit["cap_high"]):
        raise ValueError("require fade_ratio and cap_low < 1 < cap_high")
    if not (0.5 <= crit["majority_share"] <= 1 and crit["single_sibling_factor"] >= 1):
        raise ValueError("require majority_share in [0.5, 1] and single_sibling_factor >= 1")
    if not (crit["dead_ice_pct"] < crit["single_ice_floor"] <= crit["ice_cont_pct"]
            <= crit["ice_max_valid"]):
        raise ValueError("require dead_ice_pct < single_ice_floor <= ice_cont_pct <= ice_max_valid")
    if (crit["ice_drop_pp"] >= crit["ice_max_valid"]
            or crit["ce_drop_pp"] >= crit["ce_max_valid"]):
        raise ValueError("efficiency drop must be smaller than its valid maximum")
    return crit


def load_overrides(project) -> dict:
    """Read key -> (keep|exclude, note); run_id wins over test_key in screen.

    CSV rows require exactly one of run_id/test_key. A bare test key in the
    legacy run_id column remains supported. Identities are checked by screen.
    """
    path = Path(project) / OVERRIDE_FILE
    if not path.exists():
        return {}
    try:
        rows = list(csv.reader(StringIO(path.read_text(encoding="utf-8-sig")), strict=True))
    except (ValueError, csv.Error) as exc:
        raise ValueError(f"{path}: invalid override CSV: {exc}") from exc
    header = rows[0] if rows else []
    if (len(header) != len(set(header)) or "decision" not in header
            or not {"run_id", "test_key"} & set(header)
            or set(header) - {"run_id", "test_key", "decision", "note"}):
        raise ValueError(f"{path}: expected run_id or test_key, decision, optional note")
    out = {}
    for line, row in enumerate(rows[1:], 2):
        if not row:
            continue
        if len(row) != len(header):
            raise ValueError(f"{path}: row {line} has {len(row)} fields, expected {len(header)}")
        r = dict(zip(header, row))
        rid, tk = r.get("run_id", "").strip(), r.get("test_key", "").strip()
        if bool(rid) == bool(tk):
            raise ValueError(f"{path}: each row needs exactly one run_id or test_key")
        if tk and ":" in tk:
            raise ValueError(f"{path}: test_key must not contain a channel")
        key = rid or tk
        if key in out:
            raise ValueError(f"{path}: duplicate override identity: {key}")
        out[key] = (r["decision"].strip().lower(), r.get("note", "").strip())
    return _overrides(out)


def _overrides(values, identities=None):
    if not isinstance(values, dict):
        raise ValueError("cell_qc overrides must be a mapping")
    out = {}
    for key, hit in values.items():
        if not isinstance(key, str) or not key.strip() or key != key.strip():
            raise ValueError("override identity must be a nonblank, trimmed string")
        if (not isinstance(hit, (tuple, list)) or len(hit) != 2
                or hit[0] not in ("keep", "exclude") or not isinstance(hit[1], str)):
            raise ValueError(f"invalid override for {key}: expected (keep|exclude, note)")
        if identities is not None and key not in identities:
            raise ValueError(f"unknown override identity: {key}")
        out[key] = tuple(hit)
    return out


def _boolean(value):
    """Unknown/missing is not False (especially for event censoring)."""
    if pd.isna(value):
        return None
    if isinstance(value, str):
        return {"true": True, "false": False, "1": True, "0": False}.get(value.strip().lower())
    if value in (True, False):
        return bool(value)
    return None


def _others_median(values: pd.Series) -> pd.Series:
    """Median of the other members of the group (NaN when alone)."""
    v = values.to_numpy(float)
    ok = ~np.isnan(v)
    out = np.full(len(v), np.nan)
    for i in range(len(v)):
        o = v[ok & (np.arange(len(v)) != i)]
        if len(o):
            out[i] = np.median(o)
    return pd.Series(out, index=values.index)


def _others_count(values: pd.Series) -> pd.Series:
    n = values.notna().sum()
    return values.notna().map(lambda x: n - 1 if x else n)


def screen(cycles: pd.DataFrame, profile: pd.DataFrame | None, metrics: pd.DataFrame | None,
           criteria: dict | None = None, overrides: dict | None = None) -> pd.DataFrame:
    """Return the union of cycle/profile/metric run IDs, without mutating inputs.

    Unknown override identities are errors. ``ok`` means assessed rules passed,
    not that every rule was assessable; see qc_assessed_rules and qc_not_assessed.
    Dataset-only IDs are retained but never used as a test reference group.
    """
    crit = _criteria({} if criteria is None else criteria)
    frames = []
    for name, frame in (("cycles", cycles), ("profile", profile), ("metrics", metrics)):
        frame = pd.DataFrame() if frame is None else frame.copy()
        if "run_id" not in frame:
            if len(frame):
                raise ValueError(f"{name} requires run_id")
            frame["run_id"] = pd.Series(dtype=str)
        if frame["run_id"].map(lambda v: not isinstance(v, str) or not v.strip()
                               or v != v.strip()).any():
            raise ValueError(f"{name} contains invalid run_id")
        frames.append(frame)
    c, p, m = frames
    runs = pd.DataFrame({"run_id": pd.concat([f["run_id"] for f in frames]).drop_duplicates()})
    runs = runs.reset_index(drop=True)
    parts = runs["run_id"].str.partition(":").reindex(columns=[0, 1, 2])
    runs["test_key"] = parts[0].where(parts[1].eq(":") & parts[2].ne("") & parts[0].ne("ds"), "")
    runs["channel"] = parts[2]
    overrides = _overrides({} if overrides is None else overrides,
                           set(runs["run_id"]) | (set(runs["test_key"]) - {""}))
    if runs.empty:
        return pd.DataFrame({col: pd.Series(dtype=OUTPUT_DTYPES[col]) for col in OUTPUT_COLUMNS})
    if p["run_id"].duplicated().any():
        raise ValueError("profile has duplicate run_id")
    p = p.set_index("run_id")
    sig = p.get("protocol_signature", pd.Series(dtype=str))
    runs["protocol"] = runs["run_id"].map(sig).astype("string").str.strip()
    unknown = runs["protocol"].str.lower().isin(["", "none", "nan", "unknown", "n/a"])
    unknown |= runs["protocol"].str.contains("?", regex=False, na=False)
    runs["protocol"] = runs["protocol"].mask(unknown)
    known = runs["protocol"].notna() & runs["test_key"].ne("")
    # Tuples avoid delimiter collisions; unknown protocols never form shared groups.
    runs["reference_group"] = [
        (tk, proto) if valid else ("", rid)
        for tk, proto, valid, rid in zip(runs["test_key"], runs["protocol"], known, runs["run_id"])
    ]
    key = runs["reference_group"]
    runs["n_channels"] = runs.groupby("test_key")["run_id"].transform("size")
    runs.loc[runs["test_key"].eq(""), "n_channels"] = 1
    for col in ("cycle_index", "charge_capacity_ah", "discharge_capacity_ah"):
        c[col] = pd.to_numeric(c.get(col, pd.Series(index=c.index, dtype=float)), errors="coerce").astype(float)
        c[col] = c[col].where(np.isfinite(c[col]) & c[col].ge(0))
    if c.duplicated(["run_id", "cycle_index"]).any():
        raise ValueError("cycles has duplicate run_id/cycle_index")
    c = c.sort_values(["run_id", "cycle_index"], kind="stable")
    chg, dchg = c["charge_capacity_ah"], c["discharge_capacity_ah"]
    full = chg.gt(crit["dead_ah"]) & dchg.gt(crit["dead_ah"])
    if "is_full" in c:
        full &= c["is_full"].map(_boolean).eq(True)
    c["usable_pair"] = full & c["cycle_index"].ge(1)
    c["ice"] = (100 * dchg / chg.where(chg.gt(crit["dead_ah"]))).where(
        lambda v: v.between(0, crit["ice_max_valid"]))
    grouped = c.groupby("run_id")
    first = grouped.head(1).set_index("run_id")
    runs["n_cycles"] = runs["run_id"].map(grouped.size()).fillna(0).astype(int)
    runs["n_full_cycles"] = runs["run_id"].map(grouped["usable_pair"].sum()).fillna(0).astype(int)
    runs["ice_pct"] = runs["run_id"].map(first["ice"])
    runs["dchg1_ah"] = runs["run_id"].map(first["discharge_capacity_ah"])
    first_full = runs["run_id"].map(first["usable_pair"]).eq(True)
    starts_at_one = runs["run_id"].map(first["cycle_index"]).eq(1)
    recovery = set()
    for rid, g in grouped:
        later = g.iloc[1:]
        later = later[later["usable_pair"] & later["ice"].notna()]
        if later.empty:
            continue
        initial = g.iloc[0]
        if (not initial["usable_pair"]
                or (initial["discharge_capacity_ah"] < crit["cap_low"] * later["discharge_capacity_ah"].median()
                    and initial["ice"] + crit["ice_drop_pp"] < later["ice"].median()
                    and later["ice"].median() >= crit["ice_cont_pct"])):
            recovery.add(rid)
    runs["incomplete_start"] = runs["n_cycles"].gt(0) & (
        ~first_full | ~starts_at_one | runs["run_id"].isin(recovery))
    mx = grouped[["charge_capacity_ah", "discharge_capacity_ah"]].max()
    max_chg = runs["run_id"].map(mx["charge_capacity_ah"])
    max_dchg = runs["run_id"].map(mx["discharge_capacity_ah"])
    ratio = 100 * max_dchg / max_chg.where(max_chg.gt(crit["dead_ah"]))
    dead_like = (max_chg.le(crit["dead_ah"]) & max_dchg.le(crit["dead_ah"])) | ratio.lt(crit["dead_ice_pct"])
    available = runs["n_full_cycles"].gt(0) & ~dead_like
    quality = runs["run_id"].map(p.get("quality", pd.Series(dtype=str)))
    available &= ~quality.astype("string").fillna("").str.lower().isin(
        ["insufficient", "invalid", "unavailable", "error"])
    excluded = runs.apply(
        lambda r: (overrides.get(r.run_id) or overrides.get(r.test_key) or ("", ""))[0] == "exclude", axis=1)
    reference = available & known & ~excluded & ~runs["incomplete_start"] & runs["n_full_cycles"].ge(2)

    runs["ce_pct"], runs["c80"], runs["c80_censored"] = np.nan, np.nan, True
    censor_known = pd.Series(False, index=runs.index)
    if len(m):
        if not {"metric_id", "value"} <= set(m):
            raise ValueError("metrics requires metric_id and value")
        m = m[m["metric_id"].isin(["ce_median_pct", "cycles_to_80pct"])].copy()
        if m.duplicated(["run_id", "metric_id"]).any():
            raise ValueError("metrics has duplicate run_id/metric_id")
        m["value"] = pd.to_numeric(m["value"], errors="coerce").astype(float)
        m["value"] = m["value"].where(np.isfinite(m["value"]) & m["value"].ge(0))
        if "quality" in m:
            m.loc[m["quality"].fillna("").str.lower().isin(
                ["insufficient", "invalid", "unavailable", "error"]), "value"] = np.nan
        ce = m[m["metric_id"].eq("ce_median_pct")].set_index("run_id")
        c80 = m[m["metric_id"].eq("cycles_to_80pct")].set_index("run_id")
        runs["ce_pct"] = runs["run_id"].map(ce["value"]).where(lambda v: v.le(crit["ce_max_valid"]))
        runs["c80"] = runs["run_id"].map(c80["value"]).where(lambda v: v.gt(0))
        censored = runs["run_id"].map(c80.get("censored", pd.Series(dtype=object))).map(_boolean)
        censor_known = censored.notna()
        runs["c80_censored"] = censored.map(lambda v: True if v is None else bool(v))

    def ref(col, eligible):
        v = runs[col].where(reference & eligible)
        return v.groupby(key).transform(_others_median), v.groupby(key).transform(_others_count)

    # First-cycle efficiency is compared only within the same observed formation context.
    continuation = runs["ice_pct"].ge(crit["ice_cont_pct"]).fillna(False)
    formation_key = pd.Series(list(zip(key, continuation)), index=runs.index)
    original_key = key
    key = formation_key
    runs["ice_others"], runs["ice_n"] = ref("ice_pct", runs["ice_pct"].notna())
    runs["cap_others"], runs["cap_n"] = ref("dchg1_ah", runs["ice_pct"].notna())
    key = original_key
    runs["ce_others"], runs["ce_n"] = ref("ce_pct", runs["ce_pct"].notna())
    # A censored life is a lower bound, never an observed failure.
    runs["c80_others"], runs["c80_n"] = ref("c80", censor_known)
    runs["ice_dev"] = runs["ice_pct"] - runs["ice_others"]
    runs["cap_rel"] = runs["dchg1_ah"] / runs["cap_others"]
    runs["ce_dev"] = runs["ce_pct"] - runs["ce_others"]
    runs["c80_rel"] = runs["c80"] / runs["c80_others"]

    all_flags, assessed_all, missing_all = [], [], []
    for i, r in enumerate(runs.itertuples(index=False)):
        flags, assessed, missing = [], [], []
        if not r.n_cycles:
            missing.append("no_cycles")
        elif not available.iloc[i]:
            missing.append("no_usable_cycles")
        if not known.iloc[i]:
            missing.append("unknown_protocol_or_test")
        if r.incomplete_start:
            missing.append("incomplete_start")
            if r.run_id in recovery:
                flags.append(("incomplete_start", "suspect", "Initial capacity/ICE recovered, start may be partial or formation"))
        if dead_like.iloc[i]:
            missing.append("dead_unconfirmed")
            flags.append(("dead", "suspect", "No/low discharge observed, completion and physical cause unconfirmed"))

        usable = available.iloc[i] and known.iloc[i] and not r.incomplete_start

        def rel_rule(rule, value, thr, strong_thr, n, text, valid=True):
            if not usable or not valid or pd.isna(value) or n < 1:
                missing.append(f"{rule}:insufficient_comparable_evidence")
                return
            assessed.append(rule)
            if value < thr:
                strong = n >= crit["min_siblings_defect"] or value < strong_thr
                # A lone cycle cannot distinguish a truncated start from a cell anomaly.
                strong &= r.n_full_cycles >= 2
                flags.append((rule, "defect" if strong else "suspect", text))

        k = crit["single_sibling_factor"]
        rel_rule("low_ice", r.ice_dev, -crit["ice_drop_pp"], -crit["ice_drop_pp"] * k,
                 r.ice_n, f"ICE {r.ice_pct:.1f}% (peers {r.ice_others:.1f}%), relative outlier")
        rel_rule("low_ce", r.ce_dev, -crit["ce_drop_pp"], -crit["ce_drop_pp"] * k,
                 r.ce_n, f"CE {r.ce_pct:.2f}% (peers {r.ce_others:.2f}%), relative outlier")
        rel_rule("early_fade", r.c80_rel, crit["fade_ratio"], crit["fade_ratio"] / k, r.c80_n,
                 f"80% at {r.c80:.0f} cycles (peers {r.c80_others:.0f}), relative outlier",
                 censor_known.iloc[i] and not r.c80_censored and r.c80_others >= crit["fade_min_cycles"])
        if usable and pd.notna(r.ice_pct) and pd.notna(r.cap_rel) and r.cap_n >= 1:
            assessed.append("low_capacity")
            if r.cap_rel < crit["cap_low"]:
                flags.append(("low_capacity", "suspect", f"First discharge {r.cap_rel:.2f}x peers, mass/context unconfirmed"))
            if r.cap_n >= 2:
                assessed.append("high_capacity")
                if r.cap_rel > crit["cap_high"]:
                    flags.append(("high_capacity", "suspect", f"First discharge {r.cap_rel:.2f}x peers, mass/context unconfirmed"))
            else:
                missing.append("high_capacity:insufficient_comparable_evidence")
        else:
            missing.append("capacity:insufficient_comparable_evidence")
        if (available.iloc[i] and not r.incomplete_start and pd.notna(r.ice_pct)
                and r.ice_n < 1 and r.ice_pct < crit["single_ice_floor"]):
            flags.append(("low_ice_single", "suspect", f"ICE {r.ice_pct:.1f}%, no comparable peers"))
        all_flags.append(flags)
        assessed_all.append(assessed)
        missing_all.append(missing)

    # Majority is measured among assessable members of the rule's reference group,
    # not padded by missing profiles or unrelated protocols.
    hits, totals = {}, {}
    for i, (flags, assessed) in enumerate(zip(all_flags, assessed_all)):
        if excluded.iloc[i]:
            continue
        for rule in assessed:
            group = formation_key.iloc[i] if rule == "low_ice" else key.iloc[i]
            totals[(group, rule)] = totals.get((group, rule), 0) + 1
        for rule, _, _ in flags:
            group = formation_key.iloc[i] if rule == "low_ice" else key.iloc[i]
            hits[(group, rule)] = hits.get((group, rule), 0) + 1
        # Surviving peers are also members of the life comparison group. Excluding
        # them from the denominator could treat two short lives vs one survivor as
        # a two-channel test and bypass the majority-ambiguity safeguard.
        if ("early_fade" not in assessed and reference.iloc[i] and censor_known.iloc[i]
                and pd.notna(runs.at[i, "c80"])):
            group = key.iloc[i]
            totals[(group, "early_fade")] = totals.get((group, "early_fade"), 0) + 1
    statuses, reasons = [], []
    for i, flags in enumerate(all_flags):
        out = []
        for rule, sev, text in flags:
            group = formation_key.iloc[i] if rule == "low_ice" else key.iloc[i]
            n = totals.get((group, rule), 0)
            if sev == "defect" and n >= 3 and hits.get((group, rule), 0) / n >= crit["majority_share"]:
                sev, text = "suspect", text + " — majority ambiguous"
            out.append((rule, sev, text))
        severities = {s for _, s, _ in out}
        statuses.append("defect" if "defect" in severities else "suspect" if severities
                        else "ok" if assessed_all[i] else "not_assessed")
        reasons.append("; ".join(f"{rule}: {text}" for rule, _, text in out))
    runs["qc_auto"], runs["qc_reasons"] = statuses, reasons
    runs["qc_assessed_rules"] = [";".join(a) for a in assessed_all]
    runs["qc_not_assessed"] = [";".join(a) for a in missing_all]

    final, source, notes = [], [], []
    for r in runs.itertuples(index=False):
        hit = overrides.get(r.run_id) or overrides.get(r.test_key)
        if hit:
            final.append("defect" if hit[0] == "exclude" else "ok")
            source.append("user")
            notes.append(hit[1])
        else:
            final.append(r.qc_auto)
            source.append("auto")
            notes.append("")
    runs["qc_status"], runs["qc_source"], runs["qc_note"] = final, source, notes
    return runs[OUTPUT_COLUMNS].reset_index(drop=True)


OUTPUT_COLUMNS = [
    "run_id", "test_key", "channel", "n_channels", "protocol", "ice_pct", "ice_others", "ice_dev",
    "dchg1_ah", "cap_rel", "ce_pct", "ce_others", "ce_dev", "c80", "c80_censored", "c80_others",
    "c80_rel", "qc_auto", "qc_status", "qc_reasons", "qc_source", "qc_note",
    "n_cycles", "n_full_cycles", "incomplete_start", "qc_assessed_rules", "qc_not_assessed",
]
OUTPUT_DTYPES = {
    col: ("int64" if col in {"n_channels", "n_cycles", "n_full_cycles"}
          else "bool" if col in {"c80_censored", "incomplete_start"}
          else "str" if col in {"run_id", "test_key", "channel", "protocol", "qc_auto", "qc_status",
                               "qc_reasons", "qc_source", "qc_note", "qc_assessed_rules", "qc_not_assessed"}
          else "float64")
    for col in OUTPUT_COLUMNS
}


def output_path(project) -> Path:
    return Path(project) / "db" / "run_qc.parquet"


def build_cell_qc(project) -> dict:
    """Write only db/run_qc.parquet; return status counts (including not_assessed).

    Absent cycles do not bypass profiles, configuration validation, or overrides.
    Input/configuration files are never created or modified.
    """
    project = Path(project)
    db = project / "db"
    cyc_path = db / "run_cycles.parquet"
    cycles = pd.read_parquet(cyc_path) if cyc_path.exists() else pd.DataFrame()
    profile = pd.read_parquet(db / "run_profile.parquet") if (db / "run_profile.parquet").exists() else None
    metrics = pd.read_parquet(db / "run_metrics.parquet") if (db / "run_metrics.parquet").exists() else None
    qc = screen(cycles, profile, metrics, load_criteria(project), load_overrides(project))
    db.mkdir(parents=True, exist_ok=True)
    qc.to_parquet(output_path(project), index=False, compression="zstd")
    rule_counts = {}
    for text in qc.loc[qc["qc_status"] != "ok", "qc_reasons"]:
        for part in filter(None, text.split("; ")):
            rule = part.split(":", 1)[0]
            rule_counts[rule] = rule_counts.get(rule, 0) + 1
    return {"runs": len(qc), **qc["qc_status"].value_counts().to_dict(),
            "user_overrides": int((qc["qc_source"] == "user").sum()), "rules": rule_counts}


def load_status(project) -> dict:
    """run_id -> (qc_status, qc_reasons); empty when the screen has not run.

    Status may be not_assessed. Full rule coverage is in run_qc.parquet;
    this two-item tuple remains compatible with existing exclusion consumers.
    """
    p = output_path(project)
    if not p.exists():
        return {}
    q = pd.read_parquet(p, columns=["run_id", "qc_status", "qc_reasons"])
    return {r.run_id: (r.qc_status, r.qc_reasons) for r in q.itertuples(index=False)}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project")
    print(json.dumps(build_cell_qc(ap.parse_args().project), ensure_ascii=False, indent=1))
