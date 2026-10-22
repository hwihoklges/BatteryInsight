"""Design-condition similarity from explicit engineering scales and composition.

Categorical values use exact equality. Composition percentages are never
renormalized, and one composition vector contributes one field weight.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from itertools import combinations

import numpy as np
import pandas as pd

from .relations import _finite_number, _flag, _included, _optional, _text


SCHEMA_VERSION = "design_relations_v1"
_UNKNOWN = {"", "?", "unknown", "unverified", "needs_review", "미상", "미확인"}
_PERCENT_UNITS = {"%", "wt%", "vol%", "mol%"}


def _missing(value: object) -> bool:
    return value is None or bool(pd.isna(value)) or isinstance(value, str) and value.strip().lower() in _UNKNOWN


def _number(value: object, label: str) -> float | None:
    if _missing(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        raise ValueError(f"{label} must be numeric, not boolean")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric or missing") from exc
    return result if math.isfinite(result) else None


def _config(settings: dict) -> dict:
    defaults = {"group_column": "design_group", "min_fields": 3,
                "min_coverage": 0.6, "min_similarity": 0.0,
                "composition_sum_tolerance": 1.0}
    if set(settings) - set(defaults):
        raise ValueError(f"Unknown design settings: {sorted(set(settings) - set(defaults))}")
    cfg = defaults | settings
    cfg["group_column"] = _text(cfg["group_column"], "group_column")
    minimum = _finite_number(cfg["min_fields"], "min_fields")
    if minimum < 1 or minimum != int(minimum):
        raise ValueError("min_fields must be a positive integer")
    cfg["min_fields"] = int(minimum)
    for key in ("min_coverage", "min_similarity", "composition_sum_tolerance"):
        cfg[key] = _finite_number(cfg[key], key)
    if not 0 < cfg["min_coverage"] <= 1:
        raise ValueError("min_coverage must be in (0, 1]")
    if not 0 <= cfg["min_similarity"] <= 1:
        raise ValueError("min_similarity must be in [0, 1]")
    if not 0 <= cfg["composition_sum_tolerance"] <= 1:
        raise ValueError("composition_sum_tolerance must be in [0, 1] percentage points")
    return cfg


def _fields(fields: list[dict]) -> list[dict]:
    specs, field_ids, sources, groups = [], set(), set(), set()
    for field in fields:
        field_id = _text(field.get("field_id"), "field_id")
        if field_id in field_ids:
            raise ValueError(f"Duplicate field_id: {field_id}")
        field_ids.add(field_id)
        if "include" in field and not _flag(field["include"]):
            continue
        kind = field.get("kind")
        if kind not in {"categorical", "numeric", "composition"}:
            raise ValueError(f"Unknown design field kind for {field_id}: {kind!r}")
        weight = _finite_number(field.get("weight", 1), f"weight for {field_id}")
        if weight <= 0:
            raise ValueError(f"weight must be positive for {field_id}")
        group = _text(field.get("group", field_id), f"group for {field_id}")
        if group in groups:
            raise ValueError(f"Only one representative field per design group is allowed: {group}")
        groups.add(group)
        spec = {"field_id": field_id, "metric_id": field_id,
                "label": _text(field.get("label", field_id), "label"),
                "kind": kind, "group": group, "family": group, "weight": weight,
                "unit": "" if kind == "categorical" else _text(field.get("unit"), f"unit for {field_id}")}
        if kind != "categorical" and spec["unit"].lower() in _UNKNOWN | {"none", "null", "nan"}:
            raise ValueError(f"Unknown unit for design field {field_id}")
        if kind == "numeric":
            if "scale" not in field:
                raise ValueError(f"An explicit engineering difference scale is required for {field_id}")
            scale = _finite_number(field["scale"], f"scale for {field_id}")
            if scale <= 0:
                raise ValueError(f"scale must be positive for {field_id}")
            spec["scale"] = scale
        if kind == "composition":
            if spec["unit"] != "wt%" or field.get("composition_basis") not in {"electrode_total", "active_material_total"}:
                raise ValueError(f"Composition {field_id} requires unit='wt%' and explicit composition_basis='electrode_total' or 'active_material_total'")
            components = field.get("components")
            if not isinstance(components, list) or len(components) < 2:
                raise ValueError(f"Composition {field_id} requires at least two named component columns")
            components = [_text(component, f"component for {field_id}") for component in components]
            if len(components) != len(set(components)):
                raise ValueError(f"Duplicate component in composition {field_id}")
            spec["components"] = components
            spec["composition_basis"] = field["composition_basis"]
            consumed = set(components)
        else:
            consumed = {field_id}
        overlap = sources & consumed
        if overlap:
            raise ValueError(f"Design source columns would receive duplicate weight: {sorted(overlap)}")
        sources.update(consumed)
        specs.append(spec)
    return specs


def _feature(record: pd.Series, spec: dict, cfg: dict) -> dict:
    field_id, kind = spec["field_id"], spec["kind"]
    value, missing_components = None, []
    if kind == "categorical":
        raw = record.get(field_id)
        if not _missing(raw):
            value = _text(raw, field_id)
    elif kind == "numeric":
        value = _number(record.get(field_id), field_id)
        if value is not None and spec["unit"] in _PERCENT_UNITS and not 0 <= value <= 100:
            raise ValueError(f"Percentage field {field_id} must be within 0..100")
    else:
        components = {key: _number(record.get(key), key) for key in spec["components"]}
        for key, amount in components.items():
            if amount is not None and not 0 <= amount <= 100:
                raise ValueError(f"Composition component {key} must be within 0..100 wt%")
        missing_components = [key for key, amount in components.items() if amount is None]
        if not missing_components:
            total = math.fsum(components.values())
            if abs(total - 100.0) > cfg["composition_sum_tolerance"] + 1e-12:
                raise ValueError(f"Composition {field_id} for {record['experiment_id']} sums to {total:g}, expected 100 wt% within {cfg['composition_sum_tolerance']:g}; no renormalization is applied")
            value = components
    return {"metric_id": field_id, "label": spec["label"], "unit": spec["unit"],
            "family": spec["family"], "kind": kind, "weight": spec["weight"],
            "value": value, "scaled": None, "valid_count": int(value is not None),
            "status": "valid" if value is not None else "missing",
            "missing_components": missing_components}


def _difference(left: object, right: object, spec: dict) -> tuple[float, float | None]:
    if spec["kind"] == "categorical":
        return float(left != right), None
    if spec["kind"] == "numeric":
        delta = right - left
        distance = min(abs(delta) / spec["scale"], 1.0)
        return distance, delta if math.isfinite(delta) else None
    distance = math.fsum(abs(right[key] - left[key]) for key in spec["components"]) / 200.0
    return min(distance, 1.0), None


def build_design_relations(experiments: pd.DataFrame, fields: list[dict], settings: dict) -> dict:
    """Return a graph compatible with the electrochemical graph, using design only.

    Each component vector is one weighted field. No missing category, numeric
    field, or composition component is zero-filled. Numeric scales are fixed
    engineering intervals supplied by the user, never sample extrema.
    """
    cfg, specs = _config(settings), _fields(fields)
    if "experiment_id" not in experiments:
        raise ValueError("experiments is missing experiment_id")
    selected = experiments.copy()
    selected["experiment_id"] = selected["experiment_id"].map(lambda value: _text(value, "experiment_id"))
    if selected["experiment_id"].duplicated().any():
        raise ValueError("Duplicate experiment_id in design registry")
    selected = _included(selected)
    nodes = []
    for _, record in selected.sort_values("experiment_id").iterrows():
        group = record.get(cfg["group_column"])
        cohort = "전체 설계" if _missing(group) else _text(group, cfg["group_column"])
        node = {key: _optional(record.get(key)) for key in ("cell_id", "test_id", "protocol", "chemistry", "temperature_c")}
        node.update({"experiment_id": record["experiment_id"],
                     "label": _optional(record.get("label")) or record["experiment_id"],
                     "cohort": cohort, "design_group": cohort,
                     "features": [_feature(record, spec, cfg) for spec in specs]})
        node["available_metrics"] = sum(f["value"] is not None for f in node["features"])
        nodes.append(node)
    feature_map = {node["experiment_id"]: {f["metric_id"]: f for f in node["features"]} for node in nodes}
    spec_map = {spec["field_id"]: spec for spec in specs}
    total_weight = math.fsum(spec["weight"] for spec in specs)
    if not math.isfinite(total_weight):
        raise ValueError("Total field weight must be finite")
    cohorts, edges, excluded = [], [], []
    nodes_by_cohort = defaultdict(list)
    for node in nodes:
        nodes_by_cohort[node["cohort"]].append(node)
    for group, members in sorted(nodes_by_cohort.items()):
        cohort = {"cohort": group, "experiment_ids": [node["experiment_id"] for node in members],
                  "informative_metrics": [spec["field_id"] for spec in specs],
                  "scales": [], "excluded_metrics": []}
        for left_node, right_node in combinations(members, 2):
            source, target = left_node["experiment_id"], right_node["experiment_id"]
            left, right = feature_map[source], feature_map[target]
            shared = [spec["field_id"] for spec in specs if left[spec["field_id"]]["value"] is not None and right[spec["field_id"]]["value"] is not None]
            shared_set = set(shared)
            shared_weight = math.fsum(spec_map[key]["weight"] for key in shared)
            coverage = shared_weight / total_weight if total_weight else 0.0
            pair = {"source": source, "target": target, "cohort": group,
                    "shared_metrics": len(shared), "coverage": coverage,
                    "shared_weight": shared_weight, "total_weight": total_weight,
                    "missing_metrics": [spec["field_id"] for spec in specs if spec["field_id"] not in shared_set]}
            reason = "insufficient_shared_fields" if len(shared) < cfg["min_fields"] else "insufficient_weighted_coverage" if coverage < cfg["min_coverage"] else None
            if reason:
                excluded.append(pair | {"reason": reason})
                continue
            contributors = []
            for key in shared:
                spec = spec_map[key]
                distance, delta = _difference(left[key]["value"], right[key]["value"], spec)
                contributors.append({"metric_id": key, "label": spec["label"], "kind": spec["kind"],
                                     "unit": spec["unit"], "weight": spec["weight"],
                                     "left": left[key]["value"], "right": right[key]["value"],
                                     "raw_delta": delta, "standardized_distance": distance,
                                     "weighted_distance": distance * (spec["weight"] / shared_weight)})
            distance = math.fsum(c["weighted_distance"] for c in contributors)
            similarity = max(0.0, min(1.0, 1.0 - distance))
            if similarity < cfg["min_similarity"]:
                excluded.append(pair | {"reason": "below_similarity_threshold", "similarity": similarity})
                continue
            contributors.sort(key=lambda contribution: (-contribution["weighted_distance"], contribution["metric_id"]))
            edges.append(pair | {"similarity": similarity, "contributors": contributors})
        cohorts.append(cohort)
    payload = {"schema_version": SCHEMA_VERSION, "settings": cfg, "nodes": nodes, "edges": edges,
               "cohorts": cohorts, "metrics": specs, "field_specs": specs, "excluded_pairs": excluded,
               "metric_correlations": [], "metadata": {
                   "method": "design_similarity", "distance": "weighted_gower", "machine_learning": False,
                   "entity": "cell_specific_test", "similarity_is_correlation": False,
                   "missing_values_imputed": False, "compositions_renormalized": False,
                   "disabled_reason": "no_design_fields_configured" if not specs else None,
                   "composition_basis": "explicit per field: electrode_total or active_material_total; wt%", "total_field_weight": total_weight,
                   "similarity_formula": "1 - sum(weight * distance) / sum(shared field weights)",
                   "numeric_distance": "min(abs(right-left)/explicit engineering scale, 1)",
                   "categorical_distance": "0 for exact equality, otherwise 1; no assumed chemical embedding",
                   "composition_distance": "min(sum(abs(component differences))/200, 1); one weight per vector",
                   "coverage_denominator": "total configured field weight including missing fields",
                   "p_values_computed": False,
                   "limitations": ["Design similarity describes recorded conditions, not performance, causality or statistical significance.",
                                   "Material and binder identities require separate categorical fields; matching wt% does not establish matching chemistry.",
                                   "Numeric engineering scales and field weights are explicit assumptions supplied in configuration.",
                                   "A missing composition component makes the entire vector missing; observed zero is a valid component value.",
                                   "Composition percentages use the explicitly declared total electrode or total active-material mass basis; the two denominators are never inferred or converted.",
                                   "Only the same design_group is compared; this grouping is distinct from the electrochemical cohort.",
                                   "Pair coverage must be inspected because different missing fields can yield different comparison subsets."]}}
    return json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
