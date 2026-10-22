"""Composable, non-ML analyses for the local experiment workbench.

Selections change the actual graph calculation. Explicit empty selections never
fall back to all fields. Additional analyses only run when their module is on.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import defaultdict

import numpy as np
import pandas as pd

from .design_relations import build_design_relations
from .relations import _finite_number, _flag, build_relations


MODULES = ("network", "crossview", "evidence", "controlled", "registration", "trajectory", "replicates", "history", "correlation")
_EXPERIMENT_COLUMNS = ("experiment_id", "label", "cohort", "cell_id", "test_id", "test_type", "protocol", "chemistry", "temperature_c")
_OBSERVATION_COLUMNS = ("experiment_id", "life_index", "metric_id", "value", "unit")


def _clean(value):
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(item) for item in value]
    if isinstance(value, np.generic):
        value = value.item()
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _missing(value):
    return value is None or isinstance(value, str) and value.strip().lower() in {"", "?", "unknown", "unverified", "needs_review", "미상", "미확인"} or isinstance(value, float) and not math.isfinite(value)


def _selection(options, key, specifications, id_key):
    available = [spec[id_key] for spec in specifications]
    if len(available) != len(set(available)):
        raise ValueError(f"Duplicate available {id_key}")
    chosen = options.get(key, [spec[id_key] for spec in specifications if spec.get("selected_by_default", True) and _flag(spec.get("include", True))])
    if not isinstance(chosen, list) or any(not isinstance(item, str) for item in chosen):
        raise ValueError(f"{key} must be a list of identifiers")
    if len(chosen) != len(set(chosen)) or set(chosen) - set(available):
        raise ValueError(f"{key} contains duplicate or unknown identifiers")
    return chosen


def _normalize(bundle, options):
    if not isinstance(options, dict):
        raise ValueError("Workbench options must be an object")
    allowed = {"enabled_modules", "metric_ids", "design_field_ids", "life_min", "life_max", "summary", "min_overlap", "min_points",
               "min_coverage", "scale_mode", "reference_id", "reference_experiment_id", "cross_filter", "controlled", "windows", "replicate_group_by"}
    if set(options) - allowed:
        raise ValueError(f"Unknown workbench options: {sorted(set(options) - allowed)}")
    comparison = bundle.get("comparison", {})
    result = copy.deepcopy(options)
    modules = result.get("enabled_modules", list(MODULES))
    if not isinstance(modules, list) or any(not isinstance(module, str) for module in modules) or len(modules) != len(set(modules)) or set(modules) - set(MODULES):
        raise ValueError("enabled_modules contains unknown or duplicate module names")
    result["enabled_modules"] = modules
    result["metric_ids"] = _selection(result, "metric_ids", bundle.get("metrics", []), "metric_id")
    result["design_field_ids"] = _selection(result, "design_field_ids", bundle.get("design_fields", []), "field_id")
    for name in ("life_min", "life_max"):
        result[name] = _finite_number(result.get(name, comparison.get(name)), name)
    result["summary"] = result.get("summary", comparison.get("summary", "median"))
    if result["summary"] not in {"median", "change", "snapshot"}:
        raise ValueError("summary must be median, change or snapshot")
    snapshot = result["summary"] == "snapshot"
    if snapshot and result["life_min"] != result["life_max"]:
        raise ValueError("snapshot requires life_min == life_max at one explicit observation index")
    if not snapshot and result["life_min"] >= result["life_max"]:
        raise ValueError("life_min must be smaller than life_max")
    result["scale_mode"] = result.get("scale_mode", "current")
    if result["scale_mode"] not in {"current", "fixed"}:
        raise ValueError("scale_mode must be current or fixed")
    minimum = _finite_number(result.get("min_overlap", min(3, max(1, len(result["metric_ids"])))), "min_overlap")
    if minimum < 1 or minimum != int(minimum) or result["metric_ids"] and minimum > len(result["metric_ids"]):
        raise ValueError("min_overlap must be a positive integer no greater than the selected metric count")
    result["min_overlap"] = int(minimum)
    points = _finite_number(result.get("min_points", comparison.get("min_points", 3)), "min_points")
    minimum_points = 1 if snapshot else 2
    if points < minimum_points or points != int(points):
        raise ValueError(f"min_points must be an integer >= {minimum_points}")
    result["min_points"] = 1 if snapshot else int(points)
    result["min_coverage"] = _finite_number(result.get("min_coverage", comparison.get("min_overlap_fraction", 0.6)), "min_coverage")
    if not 0 < result["min_coverage"] <= 1:
        raise ValueError("min_coverage must be in (0, 1]")
    reference = result.get("reference_experiment_id")
    if reference is not None and reference not in {row["experiment_id"] for row in bundle.get("experiments", [])}:
        raise ValueError("Unknown reference_experiment_id")
    cross = {"design_min": 0.0, "design_max": 1.0, "electrochem_min": 0.0, "electrochem_max": 1.0}
    supplied = result.get("cross_filter", {})
    if not isinstance(supplied, dict) or set(supplied) - set(cross):
        raise ValueError("Unknown cross_filter options")
    for key, value in (cross | supplied).items():
        cross[key] = _finite_number(value, key)
        if not 0 <= cross[key] <= 1:
            raise ValueError("cross_filter bounds must be within [0, 1]")
    if cross["design_min"] > cross["design_max"] or cross["electrochem_min"] > cross["electrochem_max"]:
        raise ValueError("cross_filter minimum exceeds maximum")
    result["cross_filter"] = cross
    windows = result.get("windows", [{"label": "Current window", "life_min": result["life_min"], "life_max": result["life_max"]}])
    if not isinstance(windows, list):
        raise ValueError("windows must be a list")
    normalized_windows, labels = [], set()
    for window in windows:
        label = window.get("label")
        if not isinstance(label, str) or not label.strip() or label in labels:
            raise ValueError("Each window needs a distinct nonempty label")
        start, end = _finite_number(window.get("life_min"), "window life_min"), _finite_number(window.get("life_max"), "window life_max")
        if snapshot and start != end:
            raise ValueError("snapshot windows require life_min == life_max")
        if not snapshot and start >= end:
            raise ValueError("Window life_min must be smaller than life_max")
        normalized_windows.append({"label": label, "life_min": start, "life_max": end})
        labels.add(label)
    result["windows"] = normalized_windows
    grouping = result.get("replicate_group_by", ["design_id", "batch_id"])
    if not isinstance(grouping, list) or not grouping or any(not isinstance(key, str) or not key for key in grouping) or len(grouping) != len(set(grouping)):
        raise ValueError("replicate_group_by needs distinct nonempty column names")
    result["replicate_group_by"] = grouping
    result.setdefault("controlled", {})
    return result


def _reference_scales(bundle, options, metrics):
    if options["scale_mode"] != "fixed":
        return None, {"scale_mode": "current", "reference_id": None}
    baseline = bundle.get("baseline")
    if not isinstance(baseline, dict) or not baseline.get("reference_id"):
        raise ValueError("Fixed scaling requires a saved baseline with reference_id and scales")
    if options.get("reference_id") and options["reference_id"] != baseline["reference_id"]:
        raise ValueError("Requested reference_id does not match the supplied baseline")
    summary = baseline.get("summary", baseline.get("comparison", {}).get("summary"))
    if summary is not None and summary != options["summary"]:
        raise ValueError("Fixed reference summary differs from current summary (median, change or snapshot)")
    if "metrics" in baseline:
        reference_units = {metric["metric_id"]: metric["unit"] for metric in baseline["metrics"]}
        for metric in metrics:
            if metric["metric_id"] in reference_units and reference_units[metric["metric_id"]] != metric["unit"]:
                raise ValueError(f"Fixed reference unit mismatch for {metric['metric_id']}")
    scales = baseline.get("scales", baseline.get("cohorts"))
    if isinstance(scales, list):
        scales = {record["cohort"]: record["scales"] for record in scales}
    if not isinstance(scales, dict):
        raise ValueError("Fixed baseline scales must map cohorts to their scale lists")
    options["reference_id"] = baseline["reference_id"]
    return scales, {"scale_mode": "fixed", "reference_id": baseline["reference_id"],
                    "reference_contract": "summary_and_units_checked" if summary is not None and "metrics" in baseline else "scales_only"}


def _empty_electrochem(experiments, settings):
    nodes = []
    for row in sorted(experiments.to_dict("records"), key=lambda row: row["experiment_id"]):
        node = {key: row.get(key) for key in _EXPERIMENT_COLUMNS}
        node["label"] = node.get("label") or node["experiment_id"]
        node.update(features=[], available_metrics=0)
        nodes.append(node)
    cohorts = [{"cohort": cohort, "experiment_ids": [node["experiment_id"] for node in nodes if node["cohort"] == cohort],
                "informative_metrics": [], "scales": [], "excluded_metrics": []} for cohort in sorted({node["cohort"] for node in nodes})]
    return {"schema_version": "experiment_relations_v1", "settings": settings, "nodes": nodes, "edges": [], "metrics": [],
            "cohorts": cohorts, "metric_correlations": [], "excluded_pairs": [],
            "metadata": {"method": "observational_similarity", "disabled_reason": "no_metrics_selected", "similarity_is_correlation": False,
                         "missing_values_imputed": False, "machine_learning": False}}


def _cross_pairs(electrochem, design, options):
    design_pairs = {tuple(sorted((edge["source"], edge["target"]))): edge for edge in design["edges"]}
    values = []
    bounds, reference = options["cross_filter"], options.get("reference_experiment_id")
    for edge in electrochem["edges"]:
        key = tuple(sorted((edge["source"], edge["target"])))
        other = design_pairs.get(key)
        if other is None or reference is not None and reference not in key:
            continue
        if not bounds["design_min"] <= other["similarity"] <= bounds["design_max"] or not bounds["electrochem_min"] <= edge["similarity"] <= bounds["electrochem_max"]:
            continue
        values.append({"source": key[0], "target": key[1], "design_similarity": other["similarity"],
                       "electrochem_similarity": edge["similarity"], "design_coverage": other["coverage"],
                       "electrochem_coverage": edge["coverage"], "design_shared_metrics": other["shared_metrics"],
                       "electrochem_shared_metrics": edge["shared_metrics"]})
    return values


def _field_delta(left, right, spec):
    kind = spec["kind"]
    result = {"left": left, "right": right, "unit": spec["unit"], "kind": kind,
              "coupled": kind == "composition", "delta": None}
    if kind == "numeric":
        result["delta"] = right - left
        result["absolute_difference"] = abs(result["delta"])
    elif kind == "composition":
        deltas = {key: right[key] - left[key] for key in spec["components"]}
        result.update(component_deltas=deltas, absolute_difference=math.fsum(abs(value) for value in deltas.values()) / 2,
                      tolerance_unit="percentage points (half L1 component sum)", composition_basis=spec["composition_basis"])
    else:
        result["absolute_difference"] = float(left != right)
    return result


def _control_specs(fields, design_specs):
    if not isinstance(fields, list):
        raise ValueError("control_fields must be a list")
    specs, seen = {}, set()
    for entry in fields:
        if not isinstance(entry, dict):
            raise ValueError("Each control field must be an object")
        field_id = entry.get("field_id")
        if not isinstance(field_id, str) or not field_id.strip() or field_id in seen:
            raise ValueError("Control field identifiers must be nonempty and unique")
        seen.add(field_id)
        kind = entry.get("kind")
        if kind not in {"numeric", "categorical"}:
            raise ValueError("Control fields support numeric or categorical values")
        unit = entry.get("unit", "")
        if kind == "numeric" and (not isinstance(unit, str) or _missing(unit) or unit.strip().lower() in {"none", "null", "nan"}):
            raise ValueError(f"Numeric control {field_id} requires an explicit unit")
        spec = {"field_id": field_id, "label": entry.get("label", field_id), "kind": kind, "unit": unit}
        if field_id in design_specs:
            if any(spec[key] != design_specs[field_id][key] for key in ("kind", "unit")):
                raise ValueError(f"Conflicting design/control definition for {field_id}")
            continue
        specs[field_id] = spec
    return specs


def _control_value(value, spec):
    if _missing(value):
        return None
    if spec["kind"] == "categorical":
        if not isinstance(value, str):
            raise ValueError(f"Categorical control {spec['field_id']} must be a string")
        return value.strip()
    if isinstance(value, bool):
        raise ValueError(f"Numeric control {spec['field_id']} must not be boolean")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Numeric control {spec['field_id']} must be numeric or missing") from exc
    return number if math.isfinite(number) else None


def _controlled_pairs(graph, rows, options, control_fields=None):
    cfg = options["controlled"]
    if not isinstance(cfg, dict) or set(cfg) - {"target", "controls", "tolerances"}:
        raise ValueError("Unknown controlled comparison settings")
    if not cfg.get("target"):
        return [], {"status": "target_not_selected"}
    design_specs = {spec["field_id"]: spec for spec in graph["field_specs"]}
    context_specs = _control_specs(control_fields if control_fields is not None else [], design_specs)
    specs = design_specs | context_specs
    target = cfg["target"]
    if target not in design_specs:
        raise ValueError("controlled.target must be a selected design field")
    controls = cfg.get("controls", [key for key in specs if key != target])
    if not isinstance(controls, list) or len(controls) != len(set(controls)) or target in controls or set(controls) - set(specs):
        raise ValueError("controlled.controls must contain distinct selected design fields or declared control_fields other than target")
    tolerances = cfg.get("tolerances", {})
    if not isinstance(tolerances, dict) or set(tolerances) - ({target} | set(controls)):
        raise ValueError("controlled.tolerances refers to an unselected control or target")
    checked = {}
    for key in [target] + controls:
        tolerance = _finite_number(tolerances.get(key, 0), f"tolerance for {key}")
        if tolerance < 0 or specs[key]["kind"] == "categorical" and tolerance != 0:
            raise ValueError("Numeric tolerances must be nonnegative; categorical controls use exact equality")
        checked[key] = tolerance
    cfg.update(controls=controls, tolerances=checked)
    metadata = {"status": "enabled", "target": target, "controls": controls,
                "uncontrolled_fields": [key for key in specs if key not in controls and key != target],
                "scope": "declared design and metadata controls plus protocol, test_type and electrochemical cohort guards",
                "metadata_control_fields": list(context_specs), "metadata_controls_affect_graph_distance": False,
                "causal_effect_estimated": False, "composition_changes_are_coupled": specs[target]["kind"] == "composition"}
    by_id = {row["experiment_id"]: row for row in rows}
    feature_cache = {}

    def entity_features(node):
        entity = node["experiment_id"]
        if entity not in feature_cache:
            values = {feature["metric_id"]: feature["value"] for feature in node["features"]}
            for key in controls:
                if key in context_specs:
                    values[key] = _control_value(by_id[entity].get(key), context_specs[key])
            feature_cache[entity] = values
        return feature_cache[entity]

    # Generate only same-design-cohort candidates. Retain the old node ordering
    # and parse controls lazily, so isolated/excluded tests keep their semantics.
    groups = defaultdict(list)
    for index, node in enumerate(graph["nodes"]):
        groups[node["cohort"]].append((index, node))
    reference = options.get("reference_experiment_id")
    if reference is not None:
        reference_node = next((item for members in groups.values() for item in members
                               if item[1]["experiment_id"] == reference), None)
        candidates = []
        if reference_node is not None:
            for item in groups[reference_node[1]["cohort"]]:
                if item[0] != reference_node[0]:
                    left, right = sorted((item, reference_node), key=lambda entry: entry[0])
                    candidates.append((left, right))
        pairs = ((left[1], right[1]) for left, right in sorted(candidates, key=lambda pair: (pair[0][0], pair[1][0])))
    else:
        positions = {index: offset for members in groups.values() for offset, (index, _) in enumerate(members)}
        pairs = ((node, other) for index, node in enumerate(graph["nodes"])
                 for _, other in groups[node["cohort"]][positions[index] + 1:])
    results = []
    for left, right in pairs:
        source, destination = left["experiment_id"], right["experiment_id"]
        left_features, right_features = entity_features(left), entity_features(right)
        differences, changed, confounders, missing = {}, [], [], []
        for key in [target] + controls:
            left_value, right_value = left_features[key], right_features[key]
            if left_value is None or right_value is None:
                missing.append(key)
                continue
            delta = _field_delta(left_value, right_value, specs[key])
            differences[key] = delta
            if delta["absolute_difference"] > checked[key]:
                changed.append(key)
                if key in controls:
                    confounders.append(key)
        for key in ("protocol", "test_type", "cohort"):
            left_value, right_value = by_id[source].get(key), by_id[destination].get(key)
            if _missing(left_value) or _missing(right_value):
                missing.append(key)
            elif left_value != right_value:
                confounders.append(key)
        status = "missing" if missing else "confounded" if confounders else "unchanged_target" if target not in changed else "matched"
        results.append({"source": source, "target": destination, "status": status, "changed": changed,
                        "confounders": list(dict.fromkeys(confounders)), "missing": list(dict.fromkeys(missing)), "delta": differences,
                        "uncontrolled_fields": metadata["uncontrolled_fields"], "causal_effect_estimated": False})
    return results, metadata


def _replicates(graph, rows, options, control_fields=None):
    """Summarize tests within physical cells, then average equally across cells.

    Repeated tests on one cell retain the existing median representative. The
    displayed group mean and sample SD use one such value per physical cell,
    never one value per observation or repeated test. Unknown conditions do not
    establish a match, and an RPT stage is never inferred from a local cycle ID.
    """
    by_id = {row["experiment_id"]: row for row in rows}
    required = ["cohort", "protocol", "test_type", "chemistry", "temperature_c"]
    keys = list(dict.fromkeys(options["replicate_group_by"] + required))
    specs = _control_specs(control_fields if control_fields is not None else [], {})
    numeric_units = {"temperature_c": "degC", "pressure_mpa": "MPa", "charge_rate_c": "C", "discharge_rate_c": "C",
                     "voltage_min_v": "V", "voltage_max_v": "V", "rpt_life_cycle": "cycle"}
    for field_id, unit in numeric_units.items():
        specs.setdefault(field_id, {"field_id": field_id, "kind": "numeric", "unit": unit})
    specs.setdefault("formation_id", {"field_id": "formation_id", "kind": "categorical", "unit": ""})
    specs.setdefault("rpt_stage", {"field_id": "rpt_stage", "kind": "categorical", "unit": ""})

    def scope(row):
        return str(row.get("cohort")), str(row.get("test_type"))

    # A condition recorded for any test in a cohort/type must be recorded for
    # its peers too. Entirely absent optional conditions remain explicitly
    # unverified instead of suppressing all legacy groups.
    conditions = {}
    for row in rows:
        active = conditions.setdefault(scope(row), set())
        active.update(key for key in specs if key not in keys and not _missing(row.get(key)))
        if str(row.get("test_type", "")).strip().lower() == "rpt":
            active.add("rpt_stage")

    def group_value(row, key):
        value = row.get(key)
        if _missing(value):
            return None
        if key in specs:
            return _control_value(value, specs[key])
        if isinstance(value, (dict, list, tuple, bool)):
            raise ValueError(f"Replicate group field {key} must be a scalar identifier")
        return str(value).strip()

    buckets, excluded = {}, []
    for node in graph["nodes"]:
        row = by_id[node["experiment_id"]]
        row_keys = keys + sorted(conditions[scope(row)] - set(keys))
        group = {key: group_value(row, key) for key in row_keys}
        missing = [key for key, value in group.items() if value is None]
        if _missing(row.get("cell_id")):
            missing.append("cell_id")
        if str(row.get("cell_identity_status", "")).startswith("provisional"):
            missing.append("physical_cell_id_unconfirmed")
        if missing:
            excluded.append({"experiment_id": node["experiment_id"], "missing": missing})
            continue
        group_key = json.dumps(group, sort_keys=True, ensure_ascii=False, allow_nan=False)
        cell = str(row["cell_id"]).strip()
        for feature in node["features"]:
            bucket = buckets.setdefault((group_key, feature["metric_id"], feature["unit"]), {})
            bucket.setdefault(cell, []).append({"experiment_id": node["experiment_id"], "value": feature["value"],
                                               "status": feature["status"]})
    metrics = {metric["metric_id"]: metric for metric in graph["metrics"]}
    window = {key: options[key] for key in ("life_min", "life_max", "summary")}
    result = []
    for (group_key, metric_id, unit), cells in sorted(buckets.items()):
        group, values = json.loads(group_key), []
        for cell, tests in sorted(cells.items()):
            tests = sorted(tests, key=lambda test: test["experiment_id"])
            valid = [test for test in tests if test["value"] is not None]
            values.append({"cell_id": cell, "value": float(np.median([test["value"] for test in valid])) if valid else None,
                           "n_tests": len(valid), "n_total_tests": len(tests),
                           "experiment_ids": [test["experiment_id"] for test in tests],
                           "valid_experiment_ids": [test["experiment_id"] for test in valid], "test_values": tests})
        measurements = [value["value"] for value in values if value["value"] is not None]
        count = len(measurements)
        identity = json.dumps({"group": group, **window}, sort_keys=True, ensure_ascii=False, allow_nan=False)
        result.append({"group": group, "group_id": "replicate_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20],
                       "metric_id": metric_id, "label": metrics[metric_id].get("label", metric_id), "unit": unit, **window,
                       "aggregation": "cell_equal_weight_mean", "cell_summary": "median",
                       "n_cells": count, "n_total_cells": len(values), "missing_count": len(values) - count,
                       "n_tests": sum(value["n_tests"] for value in values), "n_total_tests": sum(value["n_total_tests"] for value in values),
                       "experiment_ids": sorted(test["experiment_id"] for tests in cells.values() for test in tests),
                       "mean": math.fsum(value / count for value in measurements) if count else None,
                       "std": float(np.std(measurements, ddof=1)) if count >= 2 else None,
                       "median": float(np.median(measurements)) if count else None,
                       "q1": float(np.percentile(measurements, 25)) if count >= 2 else None,
                       "q3": float(np.percentile(measurements, 75)) if count >= 2 else None,
                       "values": values, "spread_status": "descriptive" if count >= 2 else "single_cell" if count else "no_valid_cells"})
    automatic = sorted(set().union(*conditions.values()) - set(keys)) if conditions else []
    return result, {"status": "enabled", "group_by": keys + automatic, "requested_group_by": options["replicate_group_by"],
                    "automatic_group_by": [key for key in keys + automatic if key not in options["replicate_group_by"]],
                    "grouping_scopes": [{"cohort": cohort, "test_type": test_type, "condition_fields": sorted(active),
                                         "unrecorded_optional_conditions": sorted(set(specs) - set(keys) - active)}
                                        for (cohort, test_type), active in sorted(conditions.items())],
                    "condition_units": {key: spec["unit"] for key, spec in specs.items()},
                    "excluded_entities": excluded, "aggregation": "cell_equal_weight_mean", "cell_summary": "median",
                    "physical_cell_identity_required": True,
                    "provisional_entities": [{"experiment_id": row["experiment_id"], "source_cell_id": row.get("source_cell_id"),
                                              "cell_id": row.get("cell_id")} for row in rows
                                             if str(row.get("cell_identity_status", "")).startswith("provisional")],
                    "std_definition": "sample standard deviation across valid physical-cell representatives (ddof=1); null for n<2",
                    "missing_count_definition": "physical cells with no valid test summary for this metric in this group/window",
                    "unit_of_analysis": "one median representative per physical cell, followed by an equal-weight arithmetic mean across cells",
                    "test_summary": window, "conditions_verified_only_when_recorded": True,
                    "rpt_stage_inferred": False, "confidence_intervals_computed": False}


def analyze_bundle(bundle: dict, options: dict) -> dict:
    """Recalculate selected dimensions and enabled descriptive analyses.

    ``base_payload`` is context only and is never reused as a shortcut for changed
    metric selections. History and registration persistence belong to the server.
    """
    cfg = _normalize(bundle, options)
    enabled = set(cfg["enabled_modules"])
    rows = [_clean(copy.deepcopy(row)) for row in bundle.get("experiments", []) if _flag(row.get("include", True))]
    experiments = pd.DataFrame(rows)
    for column in _EXPERIMENT_COLUMNS:
        if column not in experiments:
            experiments[column] = pd.Series(index=experiments.index, dtype="object")
    if experiments["experiment_id"].duplicated().any():
        raise ValueError("Duplicate experiment_id in analysis bundle")
    metrics = [copy.deepcopy(metric) for metric in bundle.get("metrics", []) if metric["metric_id"] in cfg["metric_ids"]]
    fields = [copy.deepcopy(field) for field in bundle.get("design_fields", []) if field["field_id"] in cfg["design_field_ids"]]
    for spec in metrics + fields:
        spec["include"] = True  # Explicit user selection is authoritative.
    observations = pd.DataFrame(bundle.get("observations", []))
    for column in _OBSERVATION_COLUMNS:
        if column not in observations:
            observations[column] = pd.Series(index=observations.index, dtype="object")
    known_ids = {row["experiment_id"] for row in bundle.get("experiments", [])}
    if set(observations["experiment_id"]) - known_ids:
        raise ValueError("Observations contain an unregistered experiment_id")
    known_metrics = {metric["metric_id"] for metric in bundle.get("metrics", [])}
    if set(observations["metric_id"]) - known_metrics:
        raise ValueError("Observations contain an unknown metric_id")
    observations = observations.loc[observations["metric_id"].isin(cfg["metric_ids"]) & observations["experiment_id"].isin(experiments["experiment_id"])].copy()
    fixed_scales, reference_meta = _reference_scales(bundle, cfg, metrics)
    comparison = copy.deepcopy(bundle.get("comparison", {}))
    comparison.update(life_min=cfg["life_min"], life_max=cfg["life_max"], summary=cfg["summary"], min_overlap=cfg["min_overlap"],
                      min_overlap_fraction=cfg["min_coverage"], min_points=cfg["min_points"], edge_min_similarity=0.0, fixed_scales=fixed_scales)
    external_ids = {metric["metric_id"] for metric in metrics if metric.get("table") == "external_features"}

    graph_cache = {}

    def graph_for(settings, compute_correlations=False):
        # All other settings are fixed for this invocation. Cache is request-local:
        # changed metrics, data, reference, or observation cutoffs cannot go stale.
        key = (settings["life_min"], settings["life_max"])
        cached = graph_cache.get(key)
        if cached is not None:
            return cached if compute_correlations else cached | {"metric_correlations": []}
        eligible = observations.copy()
        cutoff_exclusions = []
        if metrics and not eligible.empty:
            if "include" in eligible:
                eligible = eligible.loc[eligible["include"].map(_flag)].copy()
            in_window = pd.to_numeric(eligible["life_index"], errors="raise").between(settings["life_min"], settings["life_max"])
            cutoff = pd.to_numeric(eligible.get("observed_until_life", pd.Series(index=eligible.index, dtype=float)), errors="coerce")
            cutoff_finite = pd.Series(np.isfinite(cutoff), index=eligible.index)
            external = eligible["metric_id"].isin(external_ids)
            missing_cutoff = in_window & external & ~cutoff_finite
            future = in_window & cutoff_finite & (cutoff > settings["life_max"])
            for reason, mask in (("missing_observed_until_life", missing_cutoff), ("observes_beyond_window", future)):
                for (entity, metric_id), group in eligible.loc[mask].groupby(["experiment_id", "metric_id"]):
                    cutoff_exclusions.append({"experiment_id": entity, "metric_id": metric_id, "reason": reason, "rows": len(group)})
            eligible = eligible.loc[~(missing_cutoff | future)].copy()
        graph = build_relations(experiments, eligible, metrics, settings, compute_correlations=compute_correlations) if metrics else _empty_electrochem(experiments, settings)
        graph["metadata"]["cutoff_exclusions"] = cutoff_exclusions
        graph["metadata"]["external_cutoff_policy"] = "external observations require finite observed_until_life <= this window life_max"
        graph_cache[key] = graph
        return graph

    electrochem = graph_for(comparison, compute_correlations="correlation" in enabled)
    design_settings = copy.deepcopy(bundle.get("design_settings", {}))
    design_settings.update(min_similarity=0.0, min_coverage=cfg["min_coverage"])
    if fields:
        design_settings["min_fields"] = min(design_settings.get("min_fields", 3), len(fields))
    design = build_design_relations(experiments, fields, design_settings)
    cross = _cross_pairs(electrochem, design, cfg) if "crossview" in enabled else []
    controlled, controlled_meta = _controlled_pairs(design, rows, cfg, bundle.get("control_fields", [])) if "controlled" in enabled else ([], {"status": "disabled"})
    trajectories, window_graphs = [], []
    if "trajectory" in enabled:
        series = {}
        for window in cfg["windows"]:
            window_settings = comparison | {"life_min": window["life_min"], "life_max": window["life_max"]}
            window_graph = graph_for(window_settings)
            window_graph["metric_correlations"] = []
            window_graphs.append(window | {"electrochem": window_graph})
            for node in window_graph["nodes"]:
                for feature in node["features"]:
                    item = series.setdefault((node["experiment_id"], feature["metric_id"]),
                                             {"experiment_id": node["experiment_id"], "metric_id": feature["metric_id"], "unit": feature["unit"], "points": []})
                    item["points"].append(window | {"value": feature["value"], "status": feature["status"], "valid_count": feature["valid_count"]})
        trajectories = list(series.values())
    replicates, replicate_meta = _replicates(electrochem, rows, cfg, bundle.get("control_fields", [])) if "replicates" in enabled else ([], {"status": "disabled"})
    evidence = []
    if "evidence" in enabled:
        for name, graph in (("electrochem", electrochem), ("design", design)):
            evidence.extend({"view": name, "experiment_id": node["experiment_id"], "features": node["features"]} for node in graph["nodes"])
    payload = {"graphs": {"electrochem": electrochem, "design": design}, "cross_pairs": cross, "controlled_pairs": controlled,
               "trajectories": trajectories, "window_graphs": window_graphs, "replicates": replicates,
               "evidence": evidence, "options": cfg, "metadata": {
                   "machine_learning": False, "causal_effect_estimated": False, "scores_combined_or_subtracted": False,
                   "core_graphs_recalculated": True, "enabled_modules": cfg["enabled_modules"],
                   "selected_metric_count": len(metrics), "selected_design_field_count": len(fields),
                   "reference": reference_meta, "controlled": controlled_meta, "replicates": replicate_meta,
                   "axis_kind": bundle.get("axis_kind", "life_index"), "axis_label": bundle.get("axis_label", "Life index"),
                   "axis_by_cohort": bundle.get("axis_by_cohort", []),
                   "registration_and_history": "managed by local workbench server",
                   "trajectory_population": "same enabled registered tests retained in every window; unavailable summaries remain missing",
                   "trajectory_scale_policy": "fixed reference across windows" if fixed_scales is not None else "current cohort scale is recalculated per window; raw metric trajectories are directly interpretable, edge changes also depend on scale",
                   "limitations": ["Design and electrochemical similarities use different formulas and are never combined into a difference score.",
                                   "Controlled matches only establish agreement in recorded controls; unmeasured differences may remain.",
                                   "Composition changes are coupled; changing one fraction requires specifying compensating components.",
                                   "Pairwise scores are computed before display thresholds, with missingness and coverage guards retained.",
                                   "Cell-level replicate summaries are descriptive and do not estimate confidence or causal effects."]}}
    return json.loads(json.dumps(_clean(payload), ensure_ascii=False, allow_nan=False))
