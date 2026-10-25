"""Per-channel defect screening (불량 셀 자동 판정).

Each run (test_key:channel) is compared with the *other* channels of the same test, so the rules
are relative and need no absolute spec. Capacity in Ah depends on the electrode mass, which differs
cell to cell, so capacity alone only ever gives ``suspect``; mass-free signals (ICE, CE, cycle life,
a channel that never discharged) can give ``defect``.

Rules (defaults in ``DEFAULTS``; override per project in ``projects/<p>/criteria.json`` → ``cell_qc``):

* dead        - never charged/discharged, or discharged < ``dead_ice_pct`` % of its charge, while the
                other channels of the test ran normally (charge-only protocols on every channel are fine)
* low_ice     - ICE below the other channels' median by more than ``ice_drop_pp``
* low_ce      - median CE below the others by more than ``ce_drop_pp`` (same protocol only)
* early_fade  - reached 80 % before ``fade_ratio`` x the others' cycles (others >= ``fade_min_cycles``)
* low_capacity / high_capacity - 1st discharge outside ``cap_low`` / ``cap_high`` x others (suspect only)

Status: ``defect`` needs >= ``min_siblings_defect`` other channels; with fewer, the deviation must be
``single_sibling_factor`` x the threshold, otherwise ``suspect``. When half or more of a 3+ channel test
trip the same rule (``majority_share``) the verdict is held at ``suspect``. Single-channel tests only get
an ICE floor (suspect).

Human decisions in ``dx/cell_qc_override.csv`` (run_id or test_key, decision keep|exclude, note) win.
Output: ``db/run_qc.parquet``. research_db drops ``defect`` runs from cell/design metrics.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULTS = {
    "ice_drop_pp": 5.0,          # ICE this many %p below the other channels -> low_ice
    "ice_max_valid": 105.0,      # ICE above this is a bookkeeping artefact, ignored
    "ice_cont_pct": 95.0,        # other channels at/above this ICE were measured after formation -> suspect only
    "ce_drop_pp": 2.0,           # median CE this many %p below the others -> low_ce
    "ce_max_valid": 101.0,       # median CE above this is a bookkeeping artefact, ignored
    "fade_ratio": 0.5,           # cycles-to-80 % below this x the others -> early_fade
    "fade_min_cycles": 20,       # ... only when the others last at least this many cycles
    "cap_low": 0.85,             # 1st discharge below this x the others -> low_capacity (suspect)
    "cap_high": 1.15,            # 1st discharge above this x the others -> high_capacity (suspect)
    "dead_ah": 1e-3,             # channel never moved more than this (Ah) -> dead
    "dead_ice_pct": 5.0,         # discharged < this % of its charge while the others did -> dead
    "min_siblings_defect": 2,    # other channels needed for an automatic 'defect'
    "single_sibling_factor": 2.0,  # with one other channel: 2x the %p drop, or half the life ratio
    "majority_share": 0.5,       # 3+ channel test: a rule tripped by this share of channels -> suspect only
    "single_ice_floor": 50.0,    # single-channel test: ICE below this -> suspect
}
CRITERIA_FILE = "criteria.json"
OVERRIDE_FILE = Path("dx") / "cell_qc_override.csv"
OVERRIDE_COLUMNS = ["run_id", "decision", "note"]
DEFECT_RULES = {"dead", "low_ice", "low_ce", "early_fade"}


def load_criteria(project) -> dict:
    """Defaults updated with ``criteria.json`` → ``cell_qc``; writes a template on first use."""
    path = Path(project) / CRITERIA_FILE
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
    crit = dict(DEFAULTS)
    crit.update({k: v for k, v in (data.get("cell_qc") or {}).items() if k in DEFAULTS})
    if "cell_qc" not in data:
        data["cell_qc"] = dict(DEFAULTS)
        try:
            path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return crit


def load_overrides(project) -> dict:
    """key (run_id or test_key) -> (decision, note); creates an empty template when missing."""
    path = Path(project) / OVERRIDE_FILE
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            pd.DataFrame(columns=OVERRIDE_COLUMNS).to_csv(path, index=False, encoding="utf-8-sig")
        except OSError:
            pass
        return {}
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig").fillna("")
    out = {}
    for _, r in df.iterrows():
        key, dec = r.get("run_id", "").strip(), r.get("decision", "").strip().lower()
        if key and dec in ("keep", "exclude"):
            out[key] = (dec, r.get("note", "").strip())
    return out


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
    """Return one row per run with the signals, the automatic verdict and the final status."""
    crit = {**DEFAULTS, **(criteria or {})}
    c = cycles[~cycles["run_id"].astype(str).str.startswith("ds:")]
    c = c.sort_values(["run_id", "cycle_index"])
    runs = pd.DataFrame({"run_id": c["run_id"].unique()})
    runs["test_key"] = runs["run_id"].str.split(":", n=1).str[0]
    runs["channel"] = runs["run_id"].str.split(":", n=1).str[1]

    # mass-free formation signals from the first cycle that has any charge
    mx = c.groupby("run_id")[["charge_capacity_ah", "discharge_capacity_ah"]].max()
    first = c[c["charge_capacity_ah"] > crit["dead_ah"]].groupby("run_id").first()
    runs = runs.join(mx.rename(columns={"charge_capacity_ah": "max_chg_ah",
                                        "discharge_capacity_ah": "max_dchg_ah"}), on="run_id")
    runs = runs.join(first[["charge_capacity_ah", "discharge_capacity_ah"]]
                     .rename(columns={"charge_capacity_ah": "chg1_ah", "discharge_capacity_ah": "dchg1_ah"}),
                     on="run_id")
    ice = 100 * runs["dchg1_ah"] / runs["chg1_ah"]
    runs["ice_pct"] = ice.where((ice >= 0) & (ice <= crit["ice_max_valid"]))

    # dead = this channel never gave charge back while the other channels did (a charge-only or
    # storage protocol run on every channel is not a defect)
    moved = runs[["max_chg_ah", "max_dchg_ah"]].fillna(0).max(axis=1) > crit["dead_ah"]
    ratio = 100 * runs["max_dchg_ah"].fillna(0) / runs["max_chg_ah"].where(runs["max_chg_ah"] > crit["dead_ah"])
    runs["dchg_ratio"] = ratio
    t = runs.groupby("test_key")
    sib_moved = moved.groupby(runs["test_key"]).transform("sum") - moved.astype(int)
    sib_ratio = ratio.groupby(runs["test_key"]).transform(_others_median)
    runs["dead"] = np.where(~moved, sib_moved > 0,
                            (ratio < crit["dead_ice_pct"]) & (sib_ratio >= 10 * crit["dead_ice_pct"]))
    alive = ~runs["dead"]

    sig = None
    if profile is not None and "protocol_signature" in profile:
        sig = profile.set_index("run_id")["protocol_signature"]
    runs["protocol"] = runs["run_id"].map(sig) if sig is not None else np.nan
    if metrics is not None and len(metrics):
        m = metrics[metrics["run_id"].isin(runs["run_id"])]
        ce = m[m["metric_id"] == "ce_median_pct"].set_index("run_id")["value"]
        c80 = m[m["metric_id"] == "cycles_to_80pct"].set_index("run_id")
        runs["ce_pct"] = runs["run_id"].map(ce)
        runs["ce_pct"] = runs["ce_pct"].where(runs["ce_pct"] <= crit["ce_max_valid"])
        runs["c80"] = runs["run_id"].map(c80["value"]) if len(c80) else np.nan
        runs["c80_censored"] = runs["run_id"].map(c80["censored"]).fillna(True).astype(bool) if len(c80) else True
    else:
        runs["ce_pct"], runs["c80"], runs["c80_censored"] = np.nan, np.nan, True

    t = runs.groupby("test_key")
    runs["n_channels"] = t["run_id"].transform("size")
    # dead channels are left out of the reference; formation (ICE, 1st discharge) is common to every
    # channel of the test, cycling metrics (CE, life) only compare channels on the same protocol
    def ref(col, key):
        v = runs[col].where(alive)
        return v.groupby(key).transform(_others_median), v.groupby(key).transform(_others_count)

    runs["ice_others"], runs["ice_n"] = ref("ice_pct", runs["test_key"])
    runs["cap_others"], runs["cap_n"] = ref("dchg1_ah", runs["test_key"])
    pk = runs["test_key"] + "|" + runs["protocol"].astype(object).where(runs["protocol"].notna(), "?").astype(str)
    runs["ce_others"], runs["ce_n"] = ref("ce_pct", pk)
    # censored runs count as their last cycle (a lower bound)
    runs["c80_others"], runs["c80_n"] = ref("c80", pk)

    runs["ice_dev"] = runs["ice_pct"] - runs["ice_others"]
    runs["cap_rel"] = runs["dchg1_ah"] / runs["cap_others"]
    runs["ce_dev"] = runs["ce_pct"] - runs["ce_others"]
    runs["c80_rel"] = runs["c80"] / runs["c80_others"]

    all_flags = []
    for r in runs.itertuples(index=False):
        flags = []  # (rule, severity, text)

        def rel_rule(rule, value, thr, strong_thr, n, text):
            """``value`` below ``thr`` flags; with too few other channels it must also pass ``strong_thr``."""
            if n is None or n < 1 or pd.isna(value) or value >= thr:
                return
            strong = n >= crit["min_siblings_defect"] or value < strong_thr
            flags.append((rule, "defect" if strong else "suspect", text))

        if r.dead:
            flags.append(("dead", "defect", "충·방전 용량 없음 (다른 채널은 정상 진행)" if pd.isna(r.dchg_ratio)
                          else f"방전 {r.dchg_ratio:.1f}% (충전만 됨, 다른 채널은 방전)"))
        if not flags:
            k = crit["single_sibling_factor"]
            if not pd.isna(r.ice_dev):
                # siblings at >= ice_cont_pct were measured after formation; a gap then is a data split
                weak = r.ice_others >= crit["ice_cont_pct"]
                rel_rule("low_ice", r.ice_dev, -crit["ice_drop_pp"],
                         -np.inf if weak else -crit["ice_drop_pp"] * k, 1 if weak else r.ice_n,
                         f"ICE {r.ice_pct:.1f}% (다른 채널 {r.ice_others:.1f}%)")
            if not pd.isna(r.ce_dev):
                rel_rule("low_ce", r.ce_dev, -crit["ce_drop_pp"], -crit["ce_drop_pp"] * k, r.ce_n,
                         f"CE 중앙값 {r.ce_pct:.2f}% (다른 채널 {r.ce_others:.2f}%)")
            if not r.c80_censored and not pd.isna(r.c80_others) and r.c80_others >= crit["fade_min_cycles"]:
                rel_rule("early_fade", r.c80_rel, crit["fade_ratio"], crit["fade_ratio"] / k, r.c80_n,
                         f"80% 도달 {r.c80:.0f} cyc (다른 채널 {r.c80_others:.0f})")
            if not pd.isna(r.cap_rel):
                if r.cap_rel < crit["cap_low"] and r.cap_n >= 1:
                    flags.append(("low_capacity", "suspect", f"첫 방전 용량 {r.cap_rel:.2f}배"))
                elif r.cap_rel > crit["cap_high"] and r.cap_n >= 2:
                    flags.append(("high_capacity", "suspect", f"첫 방전 용량 {r.cap_rel:.2f}배"))
            if r.n_channels == 1 and not pd.isna(r.ice_pct) and r.ice_pct < crit["single_ice_floor"]:
                flags.append(("low_ice_single", "suspect", f"ICE {r.ice_pct:.1f}% (단독 채널)"))
        all_flags.append(flags)

    # when half or more of a 3+ channel test trips the same rule, which side is normal is unclear
    hits = {}
    for (tk, n), flags in zip(zip(runs["test_key"], runs["n_channels"]), all_flags):
        for rule, _, _ in flags:
            hits[(tk, rule)] = hits.get((tk, rule), 0) + 1
    statuses, reasons = [], []
    for tk, n, flags in zip(runs["test_key"], runs["n_channels"], all_flags):
        out = []
        for rule, sev, text in flags:
            if sev == "defect" and rule != "dead" and n >= 3 and hits[(tk, rule)] / n >= crit["majority_share"]:
                sev, text = "suspect", text + f" — {hits[(tk, rule)]}/{n} 채널 해당, 판정 보류"
            out.append((rule, sev, text))
        sev = {s for _, s, _ in out}
        statuses.append("defect" if "defect" in sev else "suspect" if sev else "ok")
        reasons.append("; ".join(f"{rule}: {text}" for rule, _, text in out))
    runs["qc_auto"] = statuses
    runs["qc_reasons"] = reasons

    overrides = overrides or {}
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
    cols = ["run_id", "test_key", "channel", "n_channels", "protocol", "ice_pct", "ice_others", "ice_dev",
            "dchg1_ah", "cap_rel", "ce_pct", "ce_others", "ce_dev", "c80", "c80_censored", "c80_others",
            "c80_rel", "qc_auto", "qc_status", "qc_reasons", "qc_source", "qc_note"]
    return runs[cols].reset_index(drop=True)


def output_path(project) -> Path:
    return Path(project) / "db" / "run_qc.parquet"


def build_cell_qc(project) -> dict:
    """Screen every run of ``project`` and write ``db/run_qc.parquet``; returns counts."""
    project = Path(project)
    db = project / "db"
    cyc_path = db / "run_cycles.parquet"
    if not cyc_path.exists():
        return {"runs": 0}
    cycles = pd.read_parquet(cyc_path, columns=["run_id", "cycle_index", "discharge_capacity_ah",
                                                "charge_capacity_ah"])
    profile = pd.read_parquet(db / "run_profile.parquet") if (db / "run_profile.parquet").exists() else None
    metrics = pd.read_parquet(db / "run_metrics.parquet") if (db / "run_metrics.parquet").exists() else None
    qc = screen(cycles, profile, metrics, load_criteria(project), load_overrides(project))
    qc.to_parquet(output_path(project), index=False, compression="zstd")
    rule_counts = {}
    for text in qc.loc[qc["qc_status"] != "ok", "qc_reasons"]:
        for part in filter(None, text.split("; ")):
            rule = part.split(":", 1)[0]
            rule_counts[rule] = rule_counts.get(rule, 0) + 1
    return {"runs": len(qc), **qc["qc_status"].value_counts().to_dict(),
            "user_overrides": int((qc["qc_source"] == "user").sum()), "rules": rule_counts}


def load_status(project) -> dict:
    """run_id -> (qc_status, qc_reasons); empty when the screen has not run."""
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
