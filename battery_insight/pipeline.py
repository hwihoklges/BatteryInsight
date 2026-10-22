"""Transactional, content-addressed local Bronze/Silver/Gold runs."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import html
import json
from pathlib import Path
import platform
import sqlite3
import sys
import numpy as np
import pandas as pd

from . import __version__
from .ingest import Normalizer, complete_cycle_chunks, iter_raw, sha256_file, validate_source


def digest_json(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")


def code_fingerprint() -> str:
    return digest_json({p.name: sha256_file(p) for p in sorted(Path(__file__).parent.glob("*.py"))})


def _frame(parts: list[pd.DataFrame]) -> pd.DataFrame:
    return pd.concat([p for p in parts if not p.empty], ignore_index=True) if any(not p.empty for p in parts) else pd.DataFrame()


def _save_table(frame: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Empty table has an explicit status column so all output files are readable.
    data = frame if len(frame.columns) else pd.DataFrame({"status": pd.Series(dtype="str")})
    data.to_parquet(path.with_suffix(".parquet"), index=False, compression="zstd")
    data.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")


def _report(path: Path, tables: dict[str, pd.DataFrame], metadata: dict) -> None:
    labels = {"diagnostics": "확인할 이상 지표·원인 후보", "quality": "데이터 품질 이슈", "life_summary": "수명 관측 요약",
              "cycles": "셀·사이클 지표", "pulses": "펄스·휴지 지표", "external_features": "기존 엔진 / AIM 외부 지표"}
    body = []
    for name, frame in tables.items():
        body.append(f'<section><h2>{labels.get(name, name)}</h2><p>{len(frame):,}행 · <a href="gold/{name}.csv">전체 CSV</a></p>')
        body.append(frame.head(100).to_html(index=False, escape=True, na_rep="—", float_format=lambda n: f"{n:.6g}") if not frame.empty else "<p>산출 가능한 기록 없음</p>")
        body.append("</section>")
    title = "전기화학 데이터 검토"
    path.write_text(f'''<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>{title}</title>
<style>body{{font:15px/1.6 system-ui;margin:32px;color:#172d40;background:#f5f8fb}}section{{background:white;padding:20px;margin:24px 0;overflow:auto;border-radius:12px}}h1,h2{{color:#0d5963}}table{{border-collapse:collapse;font-size:13px;white-space:nowrap}}td,th{{border:1px solid #dbe4eb;padding:7px;text-align:left}}th{{background:#edf4f5}}code{{overflow-wrap:anywhere}}a{{color:#006e80}}</style>
<h1>{title}</h1><p>run <code>{html.escape(metadata['run_id'])}</code> · AIM: {html.escape(metadata['aim_status'])}</p>
<p>이상 지표는 검토 후보입니다. 관측된 데이터와 규칙의 근거를 확인한 뒤 판정하세요. EOL 미도달은 관측 중단(censored)으로 기록합니다.</p>
<p><a href="run_manifest.json">재현 manifest</a> · <a href="config_snapshot.json">분석 설정</a> · <a href="feature_dictionary.json">지표 사전</a></p>
{''.join(body)}</html>''', encoding="utf-8")


def _dictionary(cycles: pd.DataFrame, pulses: pd.DataFrame) -> list[dict]:
    units = [("_ah_per_v", "Ah/V"), ("_v_per_ah", "V/Ah"), ("_capacity_ah", "Ah"), ("_energy_wh", "Wh"),
             ("_pct", "%"), ("_voltage_v", "V"), ("_ohm", "ohm"), ("_mohm", "mohm"), ("_s", "s"), ("_c", "degC"), ("_ah", "Ah"), ("_wh", "Wh"), ("_v", "V"), ("_a", "A")]
    rows = []
    for table, frame in (("cycles", cycles), ("pulses", pulses)):
        for column in frame.columns:
            unit = next((u for suffix, u in units if column.lower().endswith(suffix)), None)
            rows.append(dict(table=table, feature_id=column, unit=unit, definition_version=__version__,
                             origin="measured" if "measured" in column else "derived_or_metadata",
                             definition_source="battery_insight/features.py"))
    return rows


def run_pipeline(config_path: str | Path, output_root: str | Path) -> Path:
    from .features import extract_features, finalize_cycles
    from .diagnostics import diagnose, life_summary
    from .adapters import import_external_features
    from .external_screen import screen_external

    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if config.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    sources = config.get("sources", [])
    external_specs = config.get("external_features", [])
    if not sources and not external_specs:
        raise ValueError("At least one raw source or external feature export is required")
    settings = config.get("features", {})
    chunk_rows = int(config.get("chunk_rows", 100_000))
    if chunk_rows <= 0:
        raise ValueError("chunk_rows must be positive")
    output_root = Path(output_root).resolve()
    snapshot = []
    seen_tests, seen_paths = set(), set()
    for source in sources:
        validate_source(source)
        source_path = (config_path.parent / source["path"]).resolve()
        if source["test_id"] in seen_tests:
            raise ValueError("One source file per test_id in v1; split/merge explicitly with a verified converter")
        if source_path in seen_paths:
            raise ValueError("Duplicate source path; do not double-count the same data")
        seen_tests.add(source["test_id"])
        seen_paths.add(source_path)
        if not source_path.is_file():
            raise FileNotFoundError(source_path)
        snapshot.append(dict(kind="raw", path=str(source_path), sha256=sha256_file(source_path),
                             bytes=source_path.stat().st_size, test_id=source["test_id"], cell_id=source["cell_id"]))
    for spec in external_specs:
        p = (config_path.parent / spec["path"]).resolve()
        if not p.is_file():
            raise FileNotFoundError(p)
        snapshot.append(dict(kind="external_features", path=str(p), sha256=sha256_file(p), bytes=p.stat().st_size))
    runtime = {"python": platform.python_version(), "numpy": np.__version__, "pandas": pd.__version__}
    import pyarrow, scipy
    runtime.update(pyarrow=pyarrow.__version__, scipy=scipy.__version__)
    identity = dict(config_hash=digest_json(config), inputs=snapshot, code_hash=code_fingerprint(), runtime=runtime)
    run_id = digest_json(identity)[:24]
    destination = output_root / "runs" / run_id
    if (destination / "run_manifest.json").is_file():
        previous = json.loads((destination / "run_manifest.json").read_text(encoding="utf-8"))
        if previous.get("identity") != identity:
            raise ValueError("Existing run identity mismatch")
        for rel, expected in previous.get("artifact_sha256", {}).items():
            p = destination / rel
            if not p.is_file() or sha256_file(p) != expected:
                raise ValueError(f"Existing artifact changed or missing: {rel}")
        return destination
    staging = destination.parent / f".{run_id}.staging"
    staging.mkdir(parents=True, exist_ok=False)
    save_json(staging / "config_snapshot.json", config)
    save_json(staging / "input_snapshot.json", snapshot)
    cycle_parts, pulse_parts, quality_parts, external_parts = [], [], [], []
    try:
        for index, source in enumerate(sources):
            record = snapshot[index]
            source_path = Path(record["path"])
            normalizer = Normalizer(source, source_path, record["sha256"])
            partition = hashlib.sha256(str(source["test_id"]).encode()).hexdigest()[:16]
            def normalized_chunks():
                for part_no, raw in enumerate(iter_raw(source_path, source, chunk_rows)):
                    bronze = raw.copy()
                    if "_source_record" in bronze:
                        raise ValueError("Reserved bronze column _source_record already exists")
                    bronze["_source_record"] = range(normalizer.row + 1, normalizer.row + len(raw) + 1)
                    bronze_path = staging / "bronze" / partition / f"part-{part_no:06d}.parquet"
                    bronze_path.parent.mkdir(parents=True, exist_ok=True)
                    bronze.to_parquet(bronze_path, index=False, compression="zstd")
                    silver, issues = normalizer.convert(raw)
                    quality_parts.append(pd.DataFrame(issues))
                    silver_path = staging / "silver" / partition / f"part-{part_no:06d}.parquet"
                    silver_path.parent.mkdir(parents=True, exist_ok=True)
                    silver.to_parquet(silver_path, index=False, compression="zstd")
                    yield silver
            for cycle in complete_cycle_chunks(normalized_chunks(), config.get("max_cycle_rows", 2_000_000)):
                features, pulses, issues = extract_features(cycle, settings)
                for frame in (features, pulses):
                    if not frame.empty:
                        frame["include"] = bool(source.get("include", True))
                        frame["mapping_method"] = "explicit_manifest"
                        for key, value in source.get("conditions", {}).items():
                            if key in frame.columns:
                                raise ValueError(f"Condition field would overwrite feature: {key}")
                            frame[f"condition_{key}"] = value
                if not issues.empty:
                    for key in ("source_path", "source_hash"):
                        if key not in issues:
                            issues[key] = str(source_path) if key == "source_path" else record["sha256"]
                cycle_parts.append(features)
                pulse_parts.append(pulses)
                quality_parts.append(issues)
            if normalizer.row == 0:
                raise ValueError(f"Empty raw source: {source_path}")
            if sha256_file(source_path) != record["sha256"]:
                raise ValueError(f"Input changed during ingestion: {source_path}")
        for ext_index, spec in enumerate(external_specs):
            imported = import_external_features((config_path.parent / spec["path"]).resolve(), spec["mapping"])
            expected_hash = snapshot[len(sources) + ext_index]["sha256"]
            if not imported.empty and not imported.source_sha256.eq(expected_hash).all():
                raise ValueError("External input changed between snapshot and read")
            external_parts.append(imported)
        cycles = _frame(cycle_parts)
        if not cycles.empty:
            cycles = finalize_cycles(cycles, settings)
        eligible = cycles.loc[cycles["include"].eq(True)].copy() if "include" in cycles else cycles
        diagnostics = diagnose(eligible, config.get("diagnostics", {}))
        external = _frame(external_parts)
        if not external.empty:
            metric_keys = ["cell_id", "test_id", "cycle", "metric_id"]
            if external.duplicated(metric_keys).any():
                raise ValueError("Duplicate or conflicting external metrics across export files")
        ext_diag, ext_issues = screen_external(eligible, external, config.get("external_diagnostics", {}))
        diagnostics = _frame([diagnostics, ext_diag])
        quality_parts.append(ext_issues)
        if not diagnostics.empty:
            severity_order = {"critical": 0, "error": 1, "alert": 2, "warning": 3, "watch": 4, "info": 5, "ok": 6}
            diagnostics["_priority"] = diagnostics.severity.map(severity_order).fillna(7)
            diagnostics = diagnostics.sort_values(["_priority", "robust_score"], ascending=[True, False], kind="stable").drop(columns="_priority")
        for source_record in snapshot:
            if sha256_file(Path(source_record["path"])) != source_record["sha256"]:
                raise ValueError(f"Input changed during analysis: {source_record['path']}")
        life = life_summary(eligible, config.get("life", {}))
        tables = {"diagnostics": diagnostics, "quality": _frame(quality_parts), "life_summary": life,
                  "cycles": cycles, "pulses": _frame(pulse_parts), "external_features": _frame(external_parts)}
        for name, frame in tables.items():
            _save_table(frame, staging / "gold" / name)
        save_json(staging / "feature_dictionary.json", _dictionary(cycles, tables["pulses"]))
        metadata = dict(run_id=run_id, status="complete", package_version=__version__, identity=identity,
                        created_at=datetime.now(timezone.utc).isoformat(),
                        aim_status="imported" if any(s.get("mapping", {}).get("namespace") == "aim" for s in external_specs) else "not_configured",
                        rows={k: len(v) for k, v in tables.items()}, input_readonly=True,
                        limitations=["No trained lifetime model", "Candidate causes require review", "External metric screening requires explicit external_diagnostics rules and raw-cycle join"])
        _report(staging / "report.html", tables, metadata)
        metadata["artifact_sha256"] = {p.relative_to(staging).as_posix(): sha256_file(p) for p in sorted(staging.rglob("*")) if p.is_file()}
        save_json(staging / "run_manifest.json", metadata)
        staging.rename(destination)
        with sqlite3.connect(output_root / "catalog.sqlite3") as db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, path TEXT, config_hash TEXT, created_at TEXT)")
            db.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?)", (run_id, str(destination), identity["config_hash"], metadata["created_at"]))
        return destination
    except Exception as exc:
        save_json(staging / "failure.json", {"status": "failed", "error": type(exc).__name__, "message": str(exc), "run_id": run_id})
        raise
