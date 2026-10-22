"""Explicitly registered raw files -> cached runs -> a local relation report.

Watching runs only while this command is alive. No OS task, remote upload, or
raw filename identity inference is performed. Unregistered files remain pending.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import time

import pandas as pd

from .ingest import sha256_file
from .pipeline import code_fingerprint, digest_json, run_pipeline


LOCAL_METRICS = {
    # Explicit response metrics; sharing a family prevents weighting capacity
    # and its baseline-normalized retention representation twice.
    "discharge_capacity_ah": ("Ah", "discharge_capacity"),
    "charge_capacity_ah": ("Ah", "charge_capacity"),
    "capacity_retention_pct": ("%", "discharge_capacity"),
    "coulombic_efficiency_pct": ("%", "coulombic_efficiency"),
    "charge_mean_voltage_v": ("V", "charge_voltage"),
    "discharge_mean_voltage_v": ("V", "discharge_voltage"),
    "charge_dqdv_peak_voltage_v": ("V", "charge_dqdv_peak_position"),
    "discharge_dqdv_peak_voltage_v": ("V", "discharge_dqdv_peak_position"),
    "charge_dqdv_peak_ah_per_v": ("Ah/V", "charge_dqdv_peak_amplitude"),
    "discharge_dqdv_peak_ah_per_v": ("Ah/V", "discharge_dqdv_peak_amplitude"),
    "discharge_dvdq_low_soc_v_per_ah": ("V/Ah", "discharge_low_soc_dvdq"),
    "EoC_dchgR_60s_mohm": ("mohm", "eoc_resistance_60s"),
}


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _inside(path: Path, root: Path):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _bool(value):
    if value in (True, "true", "True", "1", 1, ""):
        return True
    if value in (False, "false", "False", "0", 0):
        return False
    raise ValueError(f"include must be true/false: {value!r}")


def load_registrations(path: Path, inbox: Path) -> list[dict]:
    frame = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")
    required = {"experiment_id", "cell_id", "test_id", "raw_path", "cohort", "protocol", "chemistry", "test_type"}
    if not required <= set(frame):
        raise ValueError(f"Registration columns missing: {sorted(required - set(frame))}")
    for column in required:
        if frame[column].str.strip().eq("").any():
            raise ValueError(f"Blank registration {column}")
    if frame.experiment_id.duplicated().any() or frame.duplicated(["cell_id", "test_id"]).any():
        raise ValueError("Duplicate experiment_id or cell/test identity")
    records, paths = [], set()
    for row in frame.to_dict("records"):
        raw = (inbox / row["raw_path"]).resolve()
        if not _inside(raw, inbox):
            raise ValueError("Registered raw_path must stay inside inbox")
        if raw in paths:
            raise ValueError("One raw file cannot be assigned to multiple experiments")
        paths.add(raw)
        row["raw_path"] = str(raw)
        row["include"] = _bool(row.get("include", "true"))
        row["label"] = row.get("label") or row["experiment_id"]
        value = row.get("temperature_c", "")
        row["temperature_c"] = float(value) if value else None
        if row["temperature_c"] is not None and not math.isfinite(row["temperature_c"]):
            raise ValueError("temperature_c must be finite")
        records.append(row)
    return records


def validate_metrics(metrics: list[dict]):
    if not metrics:
        raise ValueError("Select representative metrics explicitly")
    seen, families = set(), set()
    for spec in metrics:
        for name in ("metric_id", "label", "unit", "family", "column"):
            if not isinstance(spec.get(name), str) or not spec[name].strip():
                raise ValueError(f"Metric requires {name}")
        if spec["metric_id"] in seen or spec["family"] in families:
            raise ValueError("Select one metric per identity and family")
        seen.add(spec["metric_id"])
        families.add(spec["family"])
        if spec.get("role", "indicator") != "indicator":
            raise ValueError("Relations use indicator metrics only; targets/QC/conditions are context")
        if spec.get("table", "cycles") == "cycles":
            expected = LOCAL_METRICS.get(spec["column"])
            if expected != (spec["unit"], spec["family"]):
                raise ValueError(f"Use a reviewed local indicator/unit/family: {spec['column']}")
        elif spec["table"] == "external_features":
            if not spec.get("definition_version") or not spec.get("source_revision"):
                raise ValueError("External metrics require definition_version and source_revision")
        else:
            raise ValueError("Metric table must be cycles or external_features")


def _run_config(template_path: Path, row: dict) -> dict:
    config = copy.deepcopy(_load(template_path))
    if len(config.get("sources", [])) != 1:
        raise ValueError("Relations template must contain exactly one raw source")
    source = config["sources"][0]
    source.update(path=row["raw_path"], cell_id=row["cell_id"], test_id=row["test_id"],
                  test_type=row["test_type"], include=True)
    source.setdefault("conditions", {}).update({key: row[key] for key in ("protocol", "chemistry", "temperature_c")})
    external = config.get("external_features", [])
    if row.get("external_path"):
        if len(external) != 1:
            raise ValueError("external_path requires exactly one external_features mapping in the template")
        external[0]["path"] = row["external_path"]
    for spec in external:
        spec["path"] = str((template_path.parent / spec["path"]).resolve())
    return config


def _verified_table(run: Path, name: str) -> pd.DataFrame:
    manifest = _load(run / "run_manifest.json")
    relative = f"gold/{name}.parquet"
    p = run / relative
    if manifest.get("status") != "complete" or manifest.get("artifact_sha256", {}).get(relative) != sha256_file(p):
        raise ValueError(f"Completed run table missing or modified: {relative}")
    return pd.read_parquet(p)


def observations_from_run(run: Path, row: dict, metrics: list[dict], settings: dict) -> tuple[list, list]:
    cycles = _verified_table(run, "cycles")
    if cycles.empty:
        raise ValueError("No raw cycles were extracted")
    cycles = cycles.loc[cycles.cell_id.eq(row["cell_id"]) & cycles.test_id.eq(row["test_id"])].copy()
    cycles = cycles.loc[cycles["include"].eq(True)]
    cycles = cycles.loc[cycles.test_type.eq(row["test_type"])]
    if cycles.empty or cycles.life_index.isna().all():
        raise ValueError("No matching cycles with a life_index; verify test_type and registration")
    if cycles.cycle.duplicated().any() or cycles.life_index.dropna().duplicated().any():
        raise ValueError("Ambiguous cycle/life_index mapping")
    observations, issues = [], []
    external = None
    for spec in metrics:
        if spec.get("table", "cycles") == "cycles":
            if spec["column"] not in cycles:
                issues.append({"experiment_id": row["experiment_id"], "metric_id": spec["metric_id"], "reason": "column_not_emitted"})
                continue
            data = cycles[["life_index", spec["column"]]].rename(columns={spec["column"]: "value"})
        else:
            if external is None:
                external = _verified_table(run, "external_features")
            if external.empty:
                continue
            selected = external.loc[external.cell_id.eq(row["cell_id"]) & external.test_id.eq(row["test_id"]) & external.metric_id.eq(spec["column"])].copy()
            for column in ("unit", "definition_version", "source_revision"):
                if not selected[column].eq(spec[column]).all():
                    raise ValueError(f"External {column} differs: {spec['metric_id']}")
            if "role" in selected and not selected.role.eq("indicator").all():
                raise ValueError("External target/QC/condition cannot enter similarity")
            window = cycles.life_index.between(settings["life_min"], settings["life_max"])
            cutoff = cycles.loc[window, "cycle"].max()
            permitted = selected.observed_until_cycle.le(cutoff)
            if (~permitted).any():
                issues.append({"experiment_id": row["experiment_id"], "metric_id": spec["metric_id"], "reason": "observes_beyond_comparison_window", "rows": int((~permitted).sum())})
            data = selected.loc[permitted].merge(cycles[["cycle", "life_index"]], on="cycle", validate="many_to_one")[["life_index", "value"]]
        for point in data.itertuples(index=False):
            if pd.isna(point.life_index):
                continue
            value = float(point.value) if pd.notna(point.value) else None
            observations.append(dict(experiment_id=row["experiment_id"], life_index=float(point.life_index),
                                     metric_id=spec["metric_id"], value=value if value is not None and math.isfinite(value) else None,
                                     unit=spec["unit"]))
    return observations, issues


def update_experiments(config_path: str | Path, output: str | Path, *, verify_all=False, render_report=True) -> dict:
    from .relations import build_relations
    from .design_relations import build_design_relations
    from .relations_report import write_relations_report

    config_path, output = Path(config_path).resolve(), Path(output).resolve()
    config = _load(config_path)
    if config.get("schema_version") != 1:
        raise ValueError("Relations config schema_version must be 1")
    metrics, settings = config["metrics"], config["comparison"]
    validate_metrics(metrics)
    # Validate the comparison contract before any raw computation.
    build_relations(pd.DataFrame(columns=["experiment_id", "cohort"]),
                    pd.DataFrame(columns=["experiment_id", "life_index", "metric_id", "value", "unit"]), metrics, settings)
    inbox = (config_path.parent / config["inbox"]).resolve()
    if not inbox.is_dir():
        raise ValueError(f"Raw inbox not found: {inbox}")
    if _inside(output, inbox) or _inside(inbox, output):
        raise ValueError("Inbox and output must be separate, non-nested directories")
    registration = (config_path.parent / config["registrations"]).resolve()
    registry_hash = sha256_file(registration)
    rows = load_registrations(registration, inbox)
    if sha256_file(registration) != registry_hash:
        raise ValueError("Experiment registration changed while reading; retry")
    enabled = pd.DataFrame([r for r in rows if r["include"]])
    if not enabled.empty:
        for cohort, members in enabled.groupby("cohort"):
            guards = ("protocol", "test_type") if settings.get("allow_mixed_chemistry", False) else ("protocol", "chemistry", "test_type")
            for key in guards:
                if members[key].nunique() > 1:
                    raise ValueError(f"Cohort {cohort!r} mixes {key}; use separate comparison groups")
    design_graph = None
    if "design" in config:
        design = config["design"]
        if not isinstance(design, dict) or not isinstance(design.get("fields"), list):
            raise ValueError("design must declare fields and settings")
        design_rows = [dict(row) for row in rows if row["include"]]
        group_key = design.get("settings", {}).get("group_column", "design_group")
        for row in design_rows:
            row[group_key] = row.get(group_key) or "전체 등록 설계"
        design_frame = pd.DataFrame(design_rows)
        if design_frame.empty:
            design_frame = pd.DataFrame(columns=["experiment_id", "cell_id", "test_id", "label", group_key])
        design_graph = build_design_relations(design_frame, design["fields"], design.get("settings", {}))
        design_graph["metadata"].update(registration_source=str(registration), registration_sha256=registry_hash,
                                         value_policy="explicit_registry_only", raw_feature_inference=False,
                                         population="all enabled registrations, including raw-pending tests")
    template = (config_path.parent / config["pipeline_template"]).resolve()
    settle = float(config.get("settle_seconds", 30))
    if not math.isfinite(settle) or settle < 0:
        raise ValueError("settle_seconds must be finite and non-negative")
    patterns = config.get("patterns", ["*.csv", "*.parquet"])
    if not isinstance(patterns, list) or not all(isinstance(p, str) and p and not Path(p).is_absolute() and ".." not in Path(p).parts for p in patterns):
        raise ValueError("Use relative inbox glob patterns")
    files = {p.resolve() for pattern in patterns for p in inbox.glob(pattern) if p.is_file()}
    if any(not _inside(p, inbox) for p in files):
        raise ValueError("Discovered raw path escapes inbox")
    state_path = output / "update_state.json"
    state = _load(state_path) if state_path.exists() else {"records": {}}
    next_state = {"records": {}}
    registered = {Path(r["raw_path"]) for r in rows}
    status = {"scanned": len(files), "processed": 0, "reused": 0,
              "pending": [{"path": str(p), "reason": "unregistered"} for p in sorted(files - registered)],
              "failed": [], "records": [], "issues": []}
    experiments, observations = [], []
    code_hash = code_fingerprint()
    for row in rows:
        p = Path(row["raw_path"])
        if not row["include"]:
            status["pending"].append({"path": str(p), "reason": "excluded_by_registration"})
            continue
        if p not in files:
            status["pending"].append({"path": str(p), "reason": "not_found_or_pattern_mismatch"})
            continue
        try:
            stat = p.stat()
            if time.time() - stat.st_mtime < settle:
                status["pending"].append({"path": str(p), "reason": "file_still_settling"})
                continue
            local_template = (config_path.parent / row["config_path"]).resolve() if row.get("config_path") else template
            run_config = _run_config(local_template, row)
            external_snapshots = [{"path": item["path"], "sha256": sha256_file(Path(item["path"]))} for item in run_config.get("external_features", [])]
            signature = digest_json({"path": str(p), "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns,
                                     "run_config": run_config, "registration": row, "code_hash": code_hash,
                                     "config": config, "external": external_snapshots})
            previous = state["records"].get(row["experiment_id"], {})
            reused = previous.get("signature") == signature and Path(previous.get("run_path", "")).is_dir()
            if reused and verify_all:
                reused = previous.get("raw_sha256") == sha256_file(p)
            if reused:
                run = Path(previous["run_path"])
                raw_hash = previous["raw_sha256"]
            else:
                raw_hash = sha256_file(p)
                cfg_path = output / "generated_configs" / (digest_json(row["experiment_id"])[:20] + ".json")
                _atomic_json(cfg_path, run_config)
                run = run_pipeline(cfg_path, output / "lake")
                snapshot = _load(run / "input_snapshot.json")
                if snapshot[0]["sha256"] != raw_hash:
                    raise ValueError("Raw changed before parsing; retry after file stabilizes")
            selected, issues = observations_from_run(run, row, metrics, settings)
            after = p.stat()
            if (after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_mtime_ns):
                raise ValueError("Raw changed during update; retry after file stabilizes")
            next_state["records"][row["experiment_id"]] = {"signature": signature, "run_path": str(run), "raw_sha256": raw_hash}
            experiments.append({key: row[key] for key in ("experiment_id", "label", "cell_id", "test_id", "cohort", "protocol", "chemistry", "temperature_c")})
            observations.extend(selected)
            status["issues"].extend(issues)
            status["reused" if reused else "processed"] += 1
            status["records"].append({"experiment_id": row["experiment_id"], "path": str(p), "run_path": str(run), "raw_sha256": raw_hash, "state": "reused" if reused else "processed"})
        except (ValueError, OSError, KeyError, TypeError) as exc:
            status["failed"].append({"path": str(p), "experiment_id": row["experiment_id"], "error": str(exc)})
    experiment_frame = pd.DataFrame(experiments, columns=["experiment_id", "label", "cell_id", "test_id", "cohort", "protocol", "chemistry", "temperature_c"])
    observation_frame = pd.DataFrame(observations, columns=["experiment_id", "life_index", "metric_id", "value", "unit"])
    payload = build_relations(experiment_frame, observation_frame, metrics, settings)
    if sha256_file(registration) != registry_hash:
        raise ValueError("Experiment registration changed during update; retry with a stable registry")
    if design_graph is not None:
        payload["design_graph"] = design_graph
        payload["metadata"]["relationship_views"] = ["electrode_design", "electrochemical_metrics"]
        _atomic_json(output / "design_registry_snapshot.json", {"source": str(registration), "sha256": registry_hash,
                                                                "fields": config["design"]["fields"], "records": design_rows})
    payload.update(generated_at=datetime.now(timezone.utc).isoformat(), is_demo=bool(config.get("is_demo", False)), ingestion=status)
    payload["metadata"]["refresh"] = "CLI watch process only; unchanged size/mtime uses cache. Use --verify-all for full content verification."
    output.mkdir(parents=True, exist_ok=True)
    report = output / "dashboard"
    if render_report:
        write_relations_report(payload, report)
        _atomic_json(report / "data.json", payload)
    observation_frame.to_parquet(output / "observations.parquet", index=False, compression="zstd")
    experiment_frame.to_csv(output / "experiments.csv", index=False, encoding="utf-8-sig")
    _atomic_json(state_path, next_state)
    return payload


def watch_experiments(config_path, output, *, interval=30, verify_all=False, max_updates=None):
    if not math.isfinite(interval) or interval < 1:
        raise ValueError("Watch interval must be at least one second")
    count = 0
    while True:
        payload = update_experiments(config_path, output, verify_all=verify_all)
        yield payload
        count += 1
        if max_updates is not None and count >= max_updates:
            return
        time.sleep(interval)
