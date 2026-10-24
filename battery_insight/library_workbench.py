"""Bridge the integrated Parquet catalogue into the existing comparison UI."""
from __future__ import annotations

import json
import math
from pathlib import Path
import numpy as np
import pandas as pd

from .library import atomic_json, file_hash, get_dataset, identity, library_rows, validate_links
from .workbench_data import json_safe, build_workbench_bundle

AXIS_LABELS = {
    "recorded_cycle": "기록 cycle · 수명 매핑 미확정",
    "derived_cycle": "재구성 cycle (step 패턴 추정, PNE 라벨 없음)",
    "mixed": "비교군별 축 혼재: 기록 cycle / 재구성 cycle",
}

# Computed once per process: profile_metrics mixes this into every cache key
# so a code change invalidates cached metrics without re-hashing this module
# on every call (2,932 files per full bundle build).
_MODULE_HASH = file_hash(__file__)

METRICS = [
    {"metric_id": "profile.mean_voltage_v", "label": "기록점 평균 전압", "column": "mean_voltage_v", "unit": "V", "family": "profile_mean_voltage", "role": "indicator", "table": "profile"},
    {"metric_id": "profile.voltage_span_v", "label": "관측 전압 범위", "column": "voltage_span_v", "unit": "V", "family": "profile_voltage_span", "role": "indicator", "table": "profile"},
    {"metric_id": "profile.capacity_span_ah", "label": "관측 용량 범위 · 충방전 용량 아님", "column": "capacity_span_ah", "unit": "Ah", "family": "profile_capacity_span", "role": "indicator", "table": "profile"},
    {"metric_id": "profile.time_span_s", "label": "전체 경과 시간 관측 범위", "column": "time_span_s", "unit": "s", "family": "profile_time_span", "role": "indicator", "table": "profile"},
]


def metadata_binding(row):
    return identity({"metadata": row["metadata"] if row["metadata_current"] else {},
                     "review_status": row["review_status"], "format": row["format"]})


def factors(row):
    if row["format"] == "normalized_curve":
        return 1., 1.
    metadata = row["metadata"] if row["metadata_current"] else {}
    return ({"V": 1., "mV": .001}.get(metadata.get("voltage_unit")),
            {"Ah": 1., "mAh": .001, "uAh": .000001}.get(metadata.get("capacity_unit")))


def _cycle_labels_available(file):
    """Cheaply decide whether the Parquet 'cycle' column carries any PNE label.

    Uses per row-group null_count statistics when available; falls back to an
    actual (column-only) read only if a row group lacks statistics.
    """
    if "cycle" not in file.schema_arrow.names:
        return False
    try:
        index = file.schema_arrow.get_field_index("cycle")
        total = file.metadata.num_rows
        nulls = 0
        for group in range(file.metadata.num_row_groups):
            column_meta = file.metadata.row_group(group).column(index)
            statistics = column_meta.statistics
            if statistics is None or statistics.null_count is None:
                raise ValueError("row group lacks null_count statistics")
            nulls += statistics.null_count
        return nulls < total
    except Exception:
        table = file.read(columns=["cycle"])
        return table.column("cycle").null_count < table.num_rows


def _load_derived_cycles(project, row):
    """Lazily load the DERIVED cycle sidecar (module/sidecar may not exist yet).

    Returns (frame, fingerprint) or (None, None). The fingerprint is a cheap
    content hash of the sidecar rows so the profile_metrics cache key changes
    whenever the derived sidecar is rebuilt.
    """
    try:
        from .derived_cycles import load_row_cycles
    except ImportError:
        return None, None
    frame = load_row_cycles(project, row["dataset_id"], row["sha256"])
    if frame is None or frame.empty:
        return None, None
    frame = frame.sort_values("start_row").reset_index(drop=True)
    fingerprint = str(int(pd.util.hash_pandas_object(frame, index=False).sum()))
    return frame, fingerprint


def profile_metrics(project, row, *, verified=False):
    """Compute (or load cached) per-cycle profile metrics for one dataset.

    ``verified=True`` lets a caller that has *just* confirmed the source is
    unchanged (e.g. ``build_library_bundle``'s own ``get_dataset`` call, or
    ``library_evidence``'s explicit sha256 check) skip a second redundant
    freshness check here -- important across a 2,932-file / 4 GB NAS library.
    """
    import pyarrow.parquet as pq
    vf, qf = factors(row)
    file = pq.ParquetFile(row["path"])
    labelled = _cycle_labels_available(file)
    derived_frame = derived_fingerprint = None
    if not labelled and row["format"] == "native_cyc":
        derived_frame, derived_fingerprint = _load_derived_cycles(project, row)
    if not labelled and derived_frame is None:
        # No real PNE cycle labels and no (or no usable) DERIVED sidecar.
        empty = pd.DataFrame(columns=["cycle"] + [item["column"] for item in METRICS])
        empty.attrs["axis_kind"] = "recorded_cycle"
        return empty
    use_derived = derived_frame is not None
    key_parts = [row["sha256"], vf, qf, _MODULE_HASH]
    if use_derived:
        key_parts += ["derived_cycle_v1", derived_fingerprint]
    key = identity(key_parts)
    cache = Path(project) / "profile_metrics" / (row["dataset_id"] + "_" + key[:20] + ".parquet")
    if not verified:
        get_dataset(project, row["dataset_id"])
    stamp = cache.with_suffix(".json")
    cache_valid = False
    stamp_data = {}
    if cache.exists() and stamp.exists():
        try:
            stamp_data = json.loads(stamp.read_text(encoding="utf-8"))
            cache_valid = stamp_data.get("sha256") == file_hash(cache)
        except (OSError, ValueError):
            pass
    if cache_valid:
        frame = pd.read_parquet(cache)
        frame.attrs["axis_kind"] = stamp_data.get("axis_kind", "recorded_cycle")
        return frame
    native = row["format"] == "native_cyc"
    vcol, qcol, tcol = ("voltage_native", "capacity_native", "total_time_s") if native else ("voltage_v", "capacity_ah", "time_s")
    wanted = (["cycle"] if labelled else []) + [vcol, qcol, tcol]
    cols = [name for name in wanted if name in file.schema_arrow.names]
    stats = {}
    start_rows = derived_frame["start_row"].to_numpy(dtype=np.int64) if use_derived else None
    cycle_derived_values = derived_frame["cycle_derived"].to_numpy(dtype=np.int64) if use_derived else None
    row_offset = 0
    for batch in file.iter_batches(batch_size=65536, columns=cols):
        data = batch.to_pandas()
        n = len(data)
        if use_derived:
            # Map each 0-based row index to the DERIVED cycle covering it
            # (never touches/overwrites the source Parquet's own column).
            indices = np.arange(row_offset, row_offset + n, dtype=np.int64)
            segment = np.searchsorted(start_rows, indices, side="right") - 1
            segment = np.clip(segment, 0, len(cycle_derived_values) - 1)
            data = data.copy()
            data["cycle"] = cycle_derived_values[segment]
        row_offset += n
        for cycle, group in data.groupby("cycle", dropna=True, sort=False):
            if not math.isfinite(float(cycle)) or float(cycle) % 1:
                continue
            entry = stats.setdefault(int(cycle), {"n": 0, "vsum": 0., "vn": 0, "vmin": math.inf, "vmax": -math.inf,
                "qmin": math.inf, "qmax": -math.inf, "tn": 0, "tmin": math.inf, "tmax": -math.inf})
            entry["n"] += len(group)
            for column, prefix in ((vcol, "v"), (qcol, "q"), (tcol, "t")):
                if column not in group:
                    continue
                values = pd.to_numeric(group[column], errors="coerce").to_numpy(dtype=float)
                values = values[np.isfinite(values)]
                if len(values):
                    entry[prefix + "min"] = min(entry[prefix + "min"], float(values.min()))
                    entry[prefix + "max"] = max(entry[prefix + "max"], float(values.max()))
                    if prefix == "v":
                        entry["vsum"] += float(values.sum())
                        entry["vn"] += len(values)
                    elif prefix == "t":
                        entry["tn"] += len(values)
    records = []
    for cycle, entry in sorted(stats.items()):
        span = lambda prefix: entry[prefix + "max"] - entry[prefix + "min"] if math.isfinite(entry[prefix + "min"]) else None
        records.append({"cycle": cycle,
            "mean_voltage_v": entry["vsum"] / entry["vn"] * vf if vf is not None and entry["vn"] else None,
            "voltage_span_v": span("v") * vf if vf is not None and span("v") is not None else None,
            "capacity_span_ah": span("q") * qf if qf is not None and span("q") is not None else None,
            "time_span_s": span("t") if entry["tn"] == entry["n"] else None})
    frame = pd.DataFrame(records, columns=["cycle"] + [item["column"] for item in METRICS])
    if file_hash(row["path"]) != row["sha256"]:
        raise ValueError("지표 계산 중 Parquet이 변경됐습니다.")
    cache.parent.mkdir(parents=True, exist_ok=True)
    temporary = cache.with_suffix(".tmp")
    frame.to_parquet(temporary, index=False, compression="zstd", compression_level=19)
    temporary.replace(cache)
    axis_kind = "derived_cycle" if use_derived else "recorded_cycle"
    atomic_json(stamp, {"sha256": file_hash(cache), "source_sha256": row["sha256"], "axis_kind": axis_kind})
    frame.attrs["axis_kind"] = axis_kind
    return frame


def workbench_config(project):
    project = Path(project).resolve()
    path = project / "library_workbench.json"
    if not path.exists():
        template = Path(__file__).parent / "resources/database_workbench.template.json"
        if not template.exists():
            template = Path(__file__).parents[1] / "examples/database_workbench.template.json"
        config = json.loads(template.read_text(encoding="utf-8"))
        config.pop("database_path", None)
        config["parquet_library"] = "."
        config["metrics"] = METRICS
        config["comparison"] = {"life_min": 0, "life_max": 1, "summary": "median", "min_points": 2, "min_overlap": 1, "allow_mixed_chemistry": False}
        atomic_json(path, config)
    return path


def build_library_bundle(config_path, output):
    config_path, output = Path(config_path).resolve(), Path(output).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    project = (config_path.parent / config["parquet_library"]).resolve()
    rows, observations, references, issues = [], [], {}, []
    specs = {item["metric_id"]: dict(item) for item in METRICS}
    invalid_links = {link["dataset_id"] for link in validate_links(project) if not link["valid"]}
    registry_hash = file_hash(project / "library.sqlite3")
    axis_kinds_seen = set()
    for source in library_rows(project):
        get_dataset(project, source["dataset_id"])
        if source["dataset_id"] in invalid_links:
            source["metadata_current"] = False
            issues.append({"experiment_id": source["dataset_id"], "reason": "document_link_stale_requires_review"})
        meta = source["metadata"] if source["metadata_current"] else {}
        test = meta.get("test_type") or "Unknown"
        cohort_keys = ("protocol", "chemistry", "temperature_c", "pressure_mpa", "charge_rate_c", "discharge_rate_c")
        cohort = identity([meta.get("cohort"), test, meta.get("rpt_stage")] + [meta.get(key) for key in cohort_keys])[:16]
        physical = meta.get("physical_cell_id")
        row = {**meta, "experiment_id": source["dataset_id"], "test_id": source["dataset_id"],
            "cell_id": physical or "source_" + source["dataset_id"], "physical_cell_id": physical,
            "source_cell_id": "source_" + source["dataset_id"],
            "cell_identity_status": "mapped_physical_alias" if physical else "provisional_source_identity",
            "label": meta.get("sample_name") or source["relative_path"], "cohort": cohort, "test_type": test,
            "protocol": meta.get("protocol"), "chemistry": meta.get("chemistry"), "temperature_c": meta.get("temperature_c"),
            "include": meta.get("include") is not False, "axis_kind": "recorded_cycle", "axis_label": AXIS_LABELS["recorded_cycle"]}
        rows.append(row)
        frame = profile_metrics(project, source, verified=True)
        axis_kind = frame.attrs.get("axis_kind", "recorded_cycle")
        axis_kinds_seen.add(axis_kind)
        if axis_kind == "derived_cycle":
            row.update(axis_kind="derived_cycle", axis_label=AXIS_LABELS["derived_cycle"])
        for item in frame.to_dict("records"):
            for spec in METRICS:
                value = item[spec["column"]]
                observations.append({"experiment_id": source["dataset_id"], "life_index": float(item["cycle"]),
                    "metric_id": spec["metric_id"], "unit": spec["unit"], "value": value,
                    "observed_until_life": float(item["cycle"]), "include": row["include"]})
        references[source["dataset_id"]] = {"project": str(project), "dataset_id": source["dataset_id"], "sha256": source["sha256"],
                                             "metadata_binding": metadata_binding(source)}
        external_path = project / "external_metrics" / (source["dataset_id"] + ".parquet")
        if external_path.exists():
            stamp = json.loads(external_path.with_suffix(".json").read_text(encoding="utf-8"))
            if stamp.get("dataset_sha256") != source["sha256"]:
                issues.append({"experiment_id": source["dataset_id"], "reason": "external_metrics_source_changed_requires_reimport"})
            elif stamp.get("sha256") != file_hash(external_path):
                raise ValueError("외부 지표 저장 파일이 변경됐습니다.")
            else:
                external = pd.read_parquet(external_path)
                for item in external.to_dict("records"):
                    if item.get("role", "indicator") != "indicator":
                        continue
                    if not math.isfinite(float(item["observed_until_cycle"])):
                        continue
                    mid = "external." + str(item["metric_id"])
                    new = {"metric_id": mid, "label": str(item["metric_id"]), "column": str(item["metric_id"]),
                        "unit": item["unit"], "family": "external." + str(item.get("family") or item["metric_id"]),
                        "role": "indicator", "table": "library_external", "selected_by_default": False,
                        "definition_version": item["definition_version"], "source_revision": item["source_revision"]}
                    if mid in specs and any(specs[mid][key] != new[key] for key in ("unit", "family", "definition_version", "source_revision")):
                        raise ValueError("외부 지표 정의/단위가 충돌합니다: " + mid)
                    specs[mid] = new
                    observations.append({"experiment_id": source["dataset_id"], "life_index": float(item["cycle"]),
                        "metric_id": mid, "value": item["value"], "unit": item["unit"],
                        "observed_until_life": float(item["observed_until_cycle"]), "include": row["include"],
                        "source_sha256": item["source_sha256"], "source_row": item["source_row"],
                        "definition_version": item["definition_version"], "source_revision": item["source_revision"]})
        if source["format"] == "native_cyc" and None in factors(source):
            issues.append({"experiment_id": source["dataset_id"], "reason": "native_units_unconfirmed_voltage_or_capacity_metrics_unavailable"})
    config["metrics"] = list(specs.values())
    indices = [item["life_index"] for item in observations]
    lo, hi = (min(indices), max(indices)) if indices else (0, 1)
    config["comparison"].update(life_min=lo, life_max=hi, summary="snapshot" if lo == hi else "median", min_points=1 if lo == hi else 2)
    bundle_axis_kind = next(iter(axis_kinds_seen)) if len(axis_kinds_seen) == 1 else "mixed" if axis_kinds_seen else "recorded_cycle"
    # Curves and profile metrics have their own source references. No fabricated
    # legacy run, current trace, CC/CV mode or lifetime mapping is created.
    payload = {"schema_version": "library_workbench_v1", "ingestion": {"records": []}, "issues": issues}
    bundle = build_workbench_bundle(output, config, rows, payload, observation_records=json_safe(observations))
    bundle.update(source_mode="parquet_library", evidence_references=references, library_project=str(project),
        registry_hash=registry_hash, axis_kind=bundle_axis_kind,
        axis_label=AXIS_LABELS.get(bundle_axis_kind, AXIS_LABELS["recorded_cycle"]), axis_by_cohort=[],
        capabilities={"registration": False, "registration_reason": "실험 정보는 BatteryInsight 통합 화면의 Parquet 분류 탭에서 수정하세요."})
    bundle["issues"] += issues
    if file_hash(project / "library.sqlite3") != registry_hash:
        raise ValueError("비교 계산 중 실험 등록 정보가 변경됐습니다. 다시 새로고침하세요.")
    for reference in references.values():
        if metadata_binding(get_dataset(project, reference["dataset_id"])) != reference["metadata_binding"]:
            raise ValueError("비교 계산 중 원본 또는 장표 정보가 변경됐습니다. 다시 새로고침하세요.")
    return json_safe(bundle)


def library_evidence(reference, cycle=None):
    import pyarrow.parquet as pq
    project = Path(reference["project"])
    row = get_dataset(project, reference["dataset_id"])
    if any(link["dataset_id"] == row["dataset_id"] and not link["valid"] for link in validate_links(project)):
        row["metadata_current"] = False
    if row["sha256"] != reference["sha256"]:
        raise ValueError("화면 생성 후 원본이 변경됐습니다. 새로고침하세요.")
    if reference.get("metadata_binding") and metadata_binding(row) != reference["metadata_binding"]:
        raise ValueError("화면 생성 후 실험 정보·단위가 변경됐습니다. 비교 작업실을 새로고침하세요.")
    frame = profile_metrics(project, row, verified=True)
    cycles = frame["cycle"].tolist()
    if cycle is None:
        cycle = cycles[0] if cycles else None
    file = pq.ParquetFile(row["path"])
    vf, qf = factors(row)
    native = row["format"] == "native_cyc"
    segments, points_seen = {}, 0
    max_points = 200000
    for batch in file.iter_batches(batch_size=65536):
        data = batch.to_pandas()
        if cycle is not None and "cycle" in data:
            data = data.loc[data.cycle.eq(cycle)]
        points_seen += len(data)
        if points_seen > max_points:
            raise ValueError("선택 구간이 너무 큽니다. 명확한 cycle을 선택하세요(200,000점 제한).")
        if data.empty:
            continue
        segcol = "segment_id" if "segment_id" in data else "step_id" if "step_id" in data else None
        groups = data.groupby(segcol, dropna=False, sort=False) if segcol else [(0, data)]
        for segment, group in groups:
            key = str(segment)
            segments.setdefault(key, []).append(group)
    curves = []
    for segment, parts in segments.items():
        group = pd.concat(parts, ignore_index=True)
        selection = np.unique(np.linspace(0, len(group) - 1, min(1200, len(group)), dtype=int))
        points = []
        for index in selection:
            item = group.iloc[index]
            v, q = item.get("voltage_native" if native else "voltage_v"), item.get("capacity_native" if native else "capacity_ah")
            time = item.get("total_time_s", item.get("time_s"))
            if time is None or not math.isfinite(float(time)):
                time = item.get("step_time_s")
            points.append({"time_s": time,
                "voltage_v": float(v) * vf if vf is not None else None,
                "capacity_ah": float(q) * qf if qf is not None else None,
                "current_a": None, "soc_pct": None, "source_row": item.get("source_row"),
                "voltage_native": v, "capacity_native": q, "step_time_s": item.get("step_time_s")})
        curves.append({"cycle": cycle, "leg": "unknown", "direction_basis": "not_inferred_without_current_or_step_kind",
            "segment_id": segment, "step_id": str(group.iloc[0].get("step", group.iloc[0].get("step_id", ""))),
            "points": points, "dqdv": [], "dvdq": [], "derivative": {"status": "not_supported", "reason": "current_and_CC_mode_not_retained"},
            "source": {"path": row["path"], "sha256": row["sha256"], "row_start": group.iloc[0].get("source_row"), "row_end": group.iloc[-1].get("source_row")},
            "sampling": {"original_points": len(group), "returned_points": len(points), "downsampled": len(points) < len(group), "method": "display_only_even_source_rows"}})
    return json_safe({"schema_version": "experiment_evidence_v1", "experiment_id": row["dataset_id"], "selected_cycle": cycle,
        "cycles": [{"cycle": value, "life_index": value} for value in cycles], "curves": curves,
        "cycles_table": frame.to_dict("records"), "pulses": [], "quality": [], "diagnostics": profile_review(frame), "external_features": [], "life_summary": [],
        "warnings": [{"reason": "native_units_unconfirmed"}] if vf is None or qf is None else [],
        "available_signals": ["step_time_s", "voltage_native", "capacity_native"], "unavailable_signals": ["current_a", "mode"],
        "source_hash": row["sha256"], "run_path": str(project),
        "axis_kind": frame.attrs.get("axis_kind", "recorded_cycle"),
        "axis_label": AXIS_LABELS.get(frame.attrs.get("axis_kind", "recorded_cycle"), AXIS_LABELS["recorded_cycle"]),
        "native_view": native and (vf is None or qf is None),
        "raw_modified": False, "provenance_scope": "Read-only verified Parquet; display sampling does not alter stored records"})


def profile_review(frame, *, baseline_points=3, robust_z_limit=4.0):
    """Descriptive within-test baseline checks; no cause or life prediction."""
    reviews = []
    for spec in METRICS:
        column = spec["column"]
        valid = frame.loc[pd.to_numeric(frame.get(column, pd.Series(dtype=float)), errors="coerce").notna()] if column in frame else frame.iloc[:0]
        if len(valid) <= baseline_points:
            reviews.append({"metric_id": spec["metric_id"], "status": "unavailable", "reason": "insufficient_verified_cycles", "unit": spec["unit"]})
            continue
        values = valid[column].to_numpy(dtype=float)
        baseline = values[:baseline_points]
        center = float(np.median(baseline))
        mad = float(np.median(np.abs(baseline - center)))
        if not mad > 0:
            reviews.append({"metric_id": spec["metric_id"], "status": "unavailable", "reason": "constant_baseline_requires_explicit_engineering_threshold", "baseline": center, "unit": spec["unit"]})
            continue
        for item, value in zip(valid.iloc[baseline_points:].to_dict("records"), values[baseline_points:]):
            score = abs(.67448975 * (float(value) - center) / mad)
            if score > robust_z_limit:
                reviews.append({"metric_id": spec["metric_id"], "cycle": item["cycle"], "value": value,
                    "unit": spec["unit"], "status": "needs_review", "reason": "within_test_baseline_deviation",
                    "baseline": center, "baseline_cycles": valid.iloc[:baseline_points].cycle.tolist(),
                    "robust_z": score, "threshold": robust_z_limit, "causal_diagnosis": False})
    return json_safe(reviews)
