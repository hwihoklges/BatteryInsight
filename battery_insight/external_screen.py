"""Opt-in screening of reviewed exported metrics joined to validated cycles."""
from __future__ import annotations
import pandas as pd
from .diagnostics import diagnose


def screen_external(cycles: pd.DataFrame, external: pd.DataFrame, settings: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    rules = settings.get("metrics", {})
    if not rules or external.empty:
        return pd.DataFrame(), pd.DataFrame()
    if cycles.empty:
        raise ValueError("External screening requires matching normalized cycles with completion evidence")
    keys = ["cell_id", "test_id", "cycle"]
    if cycles.duplicated(keys).any():
        raise ValueError("External screening cannot join ambiguous cycle/test-type keys")
    outputs, issues = [], []
    for metric_id, rule in rules.items():
        subset = external.loc[external.metric_id.eq(metric_id)].copy()
        if subset.empty:
            raise ValueError(f"Configured external metric absent: {metric_id}")
        if not rule.get("unit") or not subset.unit.eq(rule["unit"]).all():
            raise ValueError(f"Unit mismatch for external diagnostic metric {metric_id}")
        if subset.duplicated(keys).any():
            raise ValueError(f"Conflicting external metric versions/rows: {metric_id}")
        if subset.definition_version.nunique() != 1 or subset.source_revision.nunique() != 1:
            raise ValueError(f"Do not mix external metric definitions/revisions: {metric_id}")
        if metric_id in cycles.columns:
            raise ValueError(f"External metric would overwrite built-in column: {metric_id}")
        joined = cycles.merge(subset[keys + ["value", "source_path", "source_row", "source_sha256", "observed_until_cycle"]],
                              on=keys, how="inner", validate="one_to_one", suffixes=("", "_external"))
        unmatched = len(subset) - len(joined)
        if unmatched:
            issues.append(dict(code="external_unmatched_cycles", severity="warning", detail=f"{metric_id}: {unmatched} records have no raw-cycle match"))
        # Whole-test smoothing or future baseline metrics cannot masquerade as early evidence.
        future = joined.observed_until_cycle > joined.cycle
        if future.any() and not settings.get("allow_retrospective", False):
            issues.append(dict(code="external_future_information", severity="warning", detail=f"{metric_id}: {int(future.sum())} records withheld because cutoff is after cycle"))
            joined = joined.loc[~future].copy()
        if joined.empty:
            continue
        joined[metric_id] = joined.value
        joined["raw_source_path"] = joined.source_path
        joined["source_path"] = joined.source_path_external
        joined["source_hash"] = joined.source_sha256
        joined["source_row_start"] = joined.source_row
        joined["source_row_end"] = joined.source_row
        config = {k: v for k, v in settings.items() if k not in {"metrics", "allow_retrospective"}}
        config["metrics"] = {metric_id: rule}
        result = diagnose(joined, config)
        result["external_definition_version"] = subset.definition_version.iloc[0]
        result["external_source_revision"] = subset.source_revision.iloc[0]
        result["retrospective_allowed"] = bool(settings.get("allow_retrospective", False))
        result = result.merge(joined[keys + ["observed_until_cycle", "raw_source_path"]], on=keys, how="left", validate="many_to_one")
        outputs.append(result)
    return (pd.concat(outputs, ignore_index=True) if outputs else pd.DataFrame(), pd.DataFrame(issues))
