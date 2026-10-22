"""Descriptive experiment relationships in a fixed observation window, without ML.

Network edges are distances between representative metric summaries, not Pearson
correlations. Metric correlations use one median summary per cell and cohort.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from itertools import combinations, combinations_with_replacement

import numpy as np
import pandas as pd


SCHEMA_VERSION = "experiment_relations_v1"
_UNKNOWN_UNITS = {"", "?", "unknown", "unverified", "needs_review", "none", "null", "nan"}


def _flag(value: object) -> bool:
    if value is True or isinstance(value, np.bool_) and bool(value):
        return True
    if value is False or isinstance(value, np.bool_) and not bool(value):
        return False
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError("include must contain true or false, without missing values")


def _included(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.loc[frame["include"].map(_flag)].copy() if "include" in frame else frame.copy()


def _text(value: object, field: str) -> str:
    if pd.isna(value) or not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must contain nonempty strings")
    return value.strip()


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _settings(settings: dict) -> dict:
    defaults = {"summary": "median", "min_points": 3, "min_overlap": 3,
                "min_overlap_fraction": 0.6, "metric_spearman_min_n": 5,
                "window_tolerance": 0.0, "edge_min_similarity": 0.0,
                "allow_mixed_chemistry": False, "fixed_scales": None}
    allowed = set(defaults) | {"life_min", "life_max"}
    if set(settings) - allowed:
        raise ValueError(f"Unknown relation settings: {sorted(set(settings) - allowed)}")
    if not {"life_min", "life_max"}.issubset(settings):
        raise ValueError("life_min and life_max are required for a common observation window")
    result = defaults | settings
    if result["fixed_scales"] is not None:
        result["fixed_scales"] = _fixed_scale_map(result["fixed_scales"])
    if not isinstance(result["allow_mixed_chemistry"], bool):
        raise ValueError("allow_mixed_chemistry must be an explicit boolean")
    for key in ("life_min", "life_max", "window_tolerance", "min_overlap_fraction", "edge_min_similarity"):
        result[key] = _finite_number(result[key], key)
    if result["summary"] not in {"median", "change", "snapshot"}:
        raise ValueError("summary must be median, change or snapshot")
    snapshot = result["summary"] == "snapshot"
    if snapshot:
        if result["life_min"] != result["life_max"]:
            raise ValueError("snapshot requires life_min == life_max at one explicit observation index")
        if result["window_tolerance"] != 0:
            raise ValueError("snapshot requires window_tolerance=0; no nearest-point substitution")
    else:
        if result["life_min"] >= result["life_max"]:
            raise ValueError("life_min must be smaller than life_max")
        if not 0 <= result["window_tolerance"] < (result["life_max"] - result["life_min"]) / 2:
            raise ValueError("window_tolerance must be nonnegative and less than half the window")
    if not 0 < result["min_overlap_fraction"] <= 1:
        raise ValueError("min_overlap_fraction must be in (0, 1]")
    if not 0 <= result["edge_min_similarity"] <= 1:
        raise ValueError("edge_min_similarity must be in [0, 1]")
    for key, minimum in (("min_points", 1 if snapshot else 2), ("min_overlap", 1), ("metric_spearman_min_n", 3)):
        number = _finite_number(result[key], key)
        if number != int(number) or number < minimum:
            raise ValueError(f"{key} must be an integer >= {minimum}")
        result[key] = int(number)
    if snapshot:
        result["min_points"] = 1
    return result


def _metric_spec(metrics: list[dict]) -> tuple[list[dict], set[str]]:
    selected, seen_ids, families, disabled = [], set(), set(), set()
    for entry in metrics:
        metric_id = _text(entry.get("metric_id"), "metric_id")
        if metric_id in seen_ids:
            raise ValueError(f"Duplicate metric_id: {metric_id}")
        seen_ids.add(metric_id)
        if "include" in entry and not _flag(entry["include"]):
            disabled.add(metric_id)
            continue
        if entry.get("role", "indicator") != "indicator":
            raise ValueError(f"Only indicator metrics may enter similarity: {metric_id} has role {entry.get('role')!r}")
        unit = _text(entry.get("unit"), f"unit for {metric_id}")
        if unit.lower() in _UNKNOWN_UNITS:
            raise ValueError(f"Unknown unit for {metric_id}; explicitly confirm the source unit")
        family = _text(entry.get("family"), f"family for {metric_id}")
        if family in families:
            raise ValueError(f"Only one representative metric per family is allowed: {family}")
        families.add(family)
        selected.append({"metric_id": metric_id, "label": _text(entry.get("label", metric_id), "label"),
                         "unit": unit, "family": family})
    if not selected:
        raise ValueError("At least one selected metric is required")
    return selected, disabled


def _optional(value: object) -> object:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _summary(points: pd.DataFrame, spec: dict, settings: dict) -> dict:
    valid = points.loc[np.isfinite(points["value"])].sort_values("life_index")
    count = len(valid)
    first = float(valid.iloc[0]["life_index"]) if count else None
    last = float(valid.iloc[-1]["life_index"]) if count else None
    status, value = "valid", None
    if count == 0:
        status = "no_finite_values"
    elif count < settings["min_points"]:
        status = "insufficient_points"
    elif first > settings["life_min"] + settings["window_tolerance"] or last < settings["life_max"] - settings["window_tolerance"]:
        status = "incomplete_window"
    else:
        values = valid["value"].to_numpy(dtype=float)
        with np.errstate(over="ignore", invalid="ignore"):
            value = (float(values[0]) if settings["summary"] == "snapshot" else
                     float(np.median(values)) if settings["summary"] == "median" else float(values[-1] - values[0]))
        if not math.isfinite(value):
            status, value = "numeric_range_exceeded", None
    return spec | {"value": value, "scaled": None, "valid_count": count,
                   "first_life": first, "last_life": last, "status": status}


def _scale(values: list[float]) -> tuple[float | None, float | None, str]:
    array = np.asarray(values, dtype=float)
    if len(array) < 2:
        return None, None, "insufficient_entities"
    if float(array.min()) == float(array.max()):
        return float(array[0]), None, "constant"
    with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
        center = float(np.median(array))
        candidates = [(float(np.median(np.abs(array - center))) * 1.4826, "median_mad"),
                      (float(np.percentile(array, 75) - np.percentile(array, 25)) / 1.349, "median_iqr"),
                      (float(np.std(array, ddof=0)), "median_population_std")]
    for scale, method in candidates:
        if math.isfinite(center) and math.isfinite(scale) and scale > 0:
            return center, scale, method
    return None, None, "numeric_range_exceeded"


def _fixed_scale_map(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("fixed_scales must map cohort names to reference scale lists")
    result = {}
    for cohort, scales in value.items():
        cohort = _text(cohort, "fixed scale cohort")
        if not isinstance(scales, list):
            raise ValueError("Each fixed_scales cohort must contain a scale list")
        records, seen = [], set()
        for scale in scales:
            metric_id = _text(scale.get("metric_id"), "fixed scale metric_id")
            if metric_id in seen:
                raise ValueError(f"Duplicate fixed scale for {cohort}/{metric_id}")
            seen.add(metric_id)
            center = _finite_number(scale.get("center"), "fixed scale center")
            spread = _finite_number(scale.get("scale"), "fixed scale spread")
            if spread <= 0:
                raise ValueError("Fixed scale spread must be positive")
            records.append({"metric_id": metric_id, "center": center, "scale": spread,
                            "method": str(scale.get("method", "reference"))})
        result[cohort] = records
    return result


def build_relations(experiments: pd.DataFrame, observations: pd.DataFrame,
                    metrics: list[dict], settings: dict, *, compute_correlations: bool = True) -> dict:
    """Build JSON-safe network and descriptive cell-level Spearman correlations.

    Data outside [life_min, life_max] never supplies endpoint coverage. ``change``
    is last minus first inside this window; neither mode interpolates. Units must
    equal the explicitly declared metric unit. ``include=False`` rows are ignored.
    ``window_tolerance`` is an explicit endpoint tolerance in life-index units.
    """
    if not isinstance(compute_correlations, bool):
        raise ValueError("compute_correlations must be a boolean")
    cfg = _settings(settings)
    specs, disabled = _metric_spec(metrics)
    spec_by_id = {m["metric_id"]: m for m in specs}
    for frame, required, name in ((experiments, {"experiment_id", "cohort"}, "experiments"),
                                  (observations, {"experiment_id", "life_index", "metric_id", "value", "unit"}, "observations")):
        if not required.issubset(frame.columns):
            raise ValueError(f"{name} is missing columns: {sorted(required - set(frame.columns))}")
    all_experiments = experiments.copy()
    all_experiments["experiment_id"] = all_experiments["experiment_id"].map(lambda x: _text(x, "experiment_id"))
    if all_experiments["experiment_id"].duplicated().any():
        raise ValueError("Duplicate experiment_id; an entity must identify one cell-specific test")
    selected = _included(all_experiments)
    selected["cohort"] = selected["cohort"].map(lambda x: _text(x, "cohort"))
    for cohort_id, members in selected.groupby("cohort", sort=False):
        for condition in ("protocol", "chemistry"):
            if condition == "chemistry" and cfg["allow_mixed_chemistry"]:
                continue
            if condition in members:
                declared = {_text(value, condition) for value in members[condition] if value is not None and not pd.isna(value) and str(value).strip()}
                if len(declared) > 1:
                    raise ValueError(f"Cohort {cohort_id!r} mixes {condition}; assign separate comparable cohorts")
    data = _included(observations)
    data["experiment_id"] = data["experiment_id"].map(lambda x: _text(x, "experiment_id"))
    data["metric_id"] = data["metric_id"].map(lambda x: _text(x, "metric_id"))
    unknown_entities = set(data["experiment_id"]) - set(all_experiments["experiment_id"])
    if unknown_entities:
        raise ValueError(f"Unknown experiment_id: {sorted(unknown_entities)}")
    unknown_metrics = set(data["metric_id"]) - set(spec_by_id) - disabled
    if unknown_metrics:
        raise ValueError(f"Unknown metric_id: {sorted(unknown_metrics)}")
    data = data.loc[data["experiment_id"].isin(selected["experiment_id"]) & data["metric_id"].isin(spec_by_id)].copy()
    try:
        data["life_index"] = pd.to_numeric(data["life_index"], errors="raise").astype(float)
        data["value"] = pd.to_numeric(data["value"], errors="raise").astype(float)
    except (ValueError, TypeError) as exc:
        raise ValueError("life_index and value must be numeric (missing metric values are allowed)") from exc
    if not np.isfinite(data["life_index"]).all():
        raise ValueError("life_index must be finite")
    if data.duplicated(["experiment_id", "life_index", "metric_id"]).any():
        raise ValueError("Duplicate experiment_id + life_index + metric_id observation")
    for metric_id, rows in data.groupby("metric_id", sort=False):
        expected = spec_by_id[metric_id]["unit"]
        if rows["unit"].isna().any() or not rows["unit"].eq(expected).all():
            raise ValueError(f"Unit mismatch for {metric_id}: expected exactly {expected!r}")
    nonfinite_count = int((~np.isfinite(data["value"])).sum())
    outside_count = int((~data["life_index"].between(cfg["life_min"], cfg["life_max"])).sum())
    data = data.loc[data["life_index"].between(cfg["life_min"], cfg["life_max"])].copy()
    groups = {(entity, metric): group for (entity, metric), group in data.groupby(["experiment_id", "metric_id"], sort=False)}
    empty = data.iloc[:0]
    nodes = []
    for _, record in selected.sort_values("experiment_id").iterrows():
        entity = record["experiment_id"]
        node = {key: _optional(record.get(key)) for key in ("cell_id", "source_cell_id", "cell_identity_status", "test_id", "protocol", "chemistry", "temperature_c")}
        if node["cell_id"] is not None:
            node["cell_id"] = _text(node["cell_id"], "cell_id")
        node.update({"experiment_id": entity, "label": _optional(record.get("label")) or entity,
                     "cohort": record["cohort"], "features": [_summary(groups.get((entity, spec["metric_id"]), empty), spec, cfg) for spec in specs]})
        node["available_metrics"] = sum(feature["status"] == "valid" for feature in node["features"])
        nodes.append(node)
    features = {n["experiment_id"]: {f["metric_id"]: f for f in n["features"]} for n in nodes}
    cohorts, edges, excluded_pairs, correlations = [], [], [], []
    nodes_by_cohort = defaultdict(list)
    for node in nodes:
        nodes_by_cohort[node["cohort"]].append(node)
    for cohort_id, cohort_nodes in sorted(nodes_by_cohort.items()):
        cohort = {"cohort": cohort_id, "experiment_ids": [n["experiment_id"] for n in cohort_nodes],
                  "informative_metrics": [], "scales": [], "excluded_metrics": []}
        for spec in specs:
            metric_id = spec["metric_id"]
            values = [features[n["experiment_id"]][metric_id]["value"] for n in cohort_nodes]
            available = [v for v in values if v is not None]
            if cfg["fixed_scales"] is None:
                center, scale, method = _scale(available)
            else:
                reference = next((r for r in cfg["fixed_scales"].get(cohort_id, []) if r["metric_id"] == metric_id), None)
                if reference is None:
                    center, scale, method = None, None, "fixed_scale_unavailable"
                else:
                    center, scale, method = reference["center"], reference["scale"], "fixed:" + reference["method"]
            if scale is None:
                cohort["excluded_metrics"].append({"metric_id": metric_id, "reason": method})
                continue
            scaled_values = [(value - center) / scale if value is not None else None for value in values]
            if any(value is not None and not math.isfinite(value) for value in scaled_values):
                cohort["excluded_metrics"].append({"metric_id": metric_id, "reason": "numeric_range_exceeded"})
                continue
            cohort["informative_metrics"].append(metric_id)
            cohort["scales"].append({"metric_id": metric_id, "center": center, "scale": scale,
                                     "method": method, "n_entities": len(available)})
            for node, value in zip(cohort_nodes, scaled_values):
                features[node["experiment_id"]][metric_id]["scaled"] = value
        informative = cohort["informative_metrics"]
        for left_node, right_node in combinations(cohort_nodes, 2):
            left_id, right_id = left_node["experiment_id"], right_node["experiment_id"]
            left, right = features[left_id], features[right_id]
            shared = [m for m in informative if left[m]["scaled"] is not None and right[m]["scaled"] is not None]
            shared_set = set(shared)
            coverage = len(shared) / len(informative) if informative else 0.0
            pair = {"source": left_id, "target": right_id, "cohort": cohort_id,
                    "shared_metrics": len(shared), "coverage": coverage,
                    "missing_metrics": [m for m in informative if m not in shared_set]}
            reason = ("insufficient_shared_metrics" if len(shared) < cfg["min_overlap"] else
                      "insufficient_metric_coverage" if coverage < cfg["min_overlap_fraction"] else None)
            if reason:
                excluded_pairs.append(pair | {"reason": reason})
                continue
            contributors = [{"metric_id": m, "label": spec_by_id[m]["label"],
                             "left": left[m]["value"], "right": right[m]["value"],
                             "raw_delta": right[m]["value"] - left[m]["value"],
                             "standardized_distance": abs(right[m]["scaled"] - left[m]["scaled"])} for m in shared]
            rms = math.hypot(*(c["standardized_distance"] for c in contributors)) / math.sqrt(len(shared))
            similarity = 1.0 / (1.0 + rms)
            if similarity < cfg["edge_min_similarity"]:
                excluded_pairs.append(pair | {"reason": "below_similarity_threshold", "similarity": similarity})
                continue
            contributors.sort(key=lambda c: (-c["standardized_distance"], c["metric_id"]))
            edges.append(pair | {"similarity": similarity, "contributors": contributors})
        known_nodes = [n for n in cohort_nodes if n["cell_id"] is not None and not str(n.get("cell_identity_status", "")).startswith("provisional")]
        cell_counts = Counter(node["cell_id"] for node in known_nodes)
        if compute_correlations:
            correlations.extend(_cell_correlations(cohort_id, cohort_nodes, known_nodes, features, specs, cfg))
        cohort["repeated_cell_ids"] = sorted(cell for cell, count in cell_counts.items() if count > 1)
        cohorts.append(cohort)
    result = {"schema_version": SCHEMA_VERSION, "settings": cfg, "metrics": specs, "nodes": nodes,
              "edges": edges, "cohorts": cohorts, "metric_correlations": correlations,
              "excluded_pairs": excluded_pairs, "metadata": {
                  "machine_learning": False, "entity": "cell_specific_test", "summary": cfg["summary"],
                  "method": "observational_similarity",
                  "allow_mixed_chemistry": cfg["allow_mixed_chemistry"],
                  "scale_mode": "fixed" if cfg["fixed_scales"] is not None else "current",
                  "chemistry_scope": "explicitly permitted mixed chemistry within assigned cohort" if cfg["allow_mixed_chemistry"] else "same declared chemistry within cohort",
                  "similarity_formula": "1 / (1 + RMS of common-metric robust standardized differences)",
                  "similarity_is_correlation": False, "missing_values_imputed": False,
                  "outside_window_rows_ignored": outside_count, "nonfinite_values_treated_as_missing": nonfinite_count,
                  "correlation_unit": "one median summary per cell within cohort; repeated tests are collapsed",
                  "correlation_layout": "upper_triangle_including_diagonal", "p_values_computed": False,
                  "scaling": "fixed reference center and scale; missing reference dimensions excluded, with no current-population fallback" if cfg["fixed_scales"] is not None else "within-cohort median center; 1.4826*MAD, then IQR/1.349, then population SD; constants excluded",
                  "coverage_denominator": "all informative selected metric dimensions within cohort",
                  "limitations": ["Descriptive similarity is not causality or statistical significance.",
                                  "Only entities in the same explicitly assigned cohort are compared.",
                                  "Cohort definitions must align protocol, units and life-index basis; temperature and explicitly permitted chemistry differences require interpretation.",
                                  "Scale and network layout may change when new experiments are added.",
                                  "Window endpoint coverage is required; no interpolation or extrapolation is performed.",
                                  "Cell medians reduce repeated-test counting; other shared batch effects remain possible."]}}
    # A serialization roundtrip also catches accidental numpy/pandas or NaN output.
    return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))


def _cell_correlations(cohort_id, cohort_nodes, known_nodes, features, specs, cfg):
    """Prepare each physical-cell metric median once, preserving pairwise missingness."""
    metric_ids = [spec["metric_id"] for spec in specs]
    by_cell = defaultdict(list)
    for node in known_nodes:
        by_cell[node["cell_id"]].append(node["experiment_id"])
    medians = {}
    for cell, entities in sorted(by_cell.items()):
        medians[cell] = {}
        for metric in metric_ids:
            values = [features[entity][metric]["value"] for entity in entities
                      if features[entity][metric]["value"] is not None]
            medians[cell][metric] = float(np.median(values)) if values else None
    valid_entities = {metric: {node["experiment_id"] for node in known_nodes
                               if features[node["experiment_id"]][metric]["value"] is not None}
                      for metric in metric_ids}
    excluded_without_cell = sum(node["cell_id"] is None for node in cohort_nodes)
    excluded_provisional = sum(str(node.get("cell_identity_status", "")).startswith("provisional") for node in cohort_nodes)
    correlations = []
    for left_metric, right_metric in combinations_with_replacement(metric_ids, 2):
        paired_entities = len(valid_entities[left_metric] & valid_entities[right_metric])
        cell_pairs = [[values[left_metric], values[right_metric]] for values in medians.values()
                      if values[left_metric] is not None and values[right_metric] is not None]
        n_cells, rho = len(cell_pairs), None
        if n_cells < cfg["metric_spearman_min_n"]:
            status = "insufficient_cells"
        else:
            matrix = pd.DataFrame(cell_pairs, columns=["left", "right"])
            if matrix["left"].nunique() < 2 or matrix["right"].nunique() < 2:
                status = "constant"
            else:
                rho = float(matrix["left"].rank(method="average").corr(matrix["right"].rank(method="average")))
                status = "valid" if math.isfinite(rho) else "numeric_range_exceeded"
                if status != "valid":
                    rho = None
        correlations.append({"cohort": cohort_id, "left_metric": left_metric, "right_metric": right_metric,
                             "rho": rho, "n_entities": paired_entities, "n_cells": n_cells,
                             "status": status, "unit_of_analysis": "cell_median",
                             "excluded_entities_without_cell_id": excluded_without_cell,
                             "excluded_provisional_entities": excluded_provisional})
    return correlations
