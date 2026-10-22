"""Explicit, read-only bridges to existing feature exports and source catalogs.

No legacy Python module is imported or executed. Pickle and native instrument
formats are deliberately excluded. Units, feature definitions, source revision,
and the latest cycle seen by an estimator must be declared by the caller.
"""
from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
from typing import Any, Mapping

import pandas as pd


REQUIRED_METADATA = ("cell_id", "test_id", "cycle", "definition_version",
                     "source_revision", "observed_until_cycle")
OUTPUT_COLUMNS = [*REQUIRED_METADATA[:3], "metric_id", "value", "unit",
                  *REQUIRED_METADATA[3:], "source_path", "source_sha256",
                  "source_row", "quality_status", "role", "family"]
_REVISION = re.compile(r"(?:[0-9a-fA-F]{40}|(?:sha256:)?[0-9a-fA-F]{64})\Z")
_NAMESPACE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*\Z")
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__",
              ".pytest_cache", "build", "dist"}
_CODE_EXTENSIONS = {".py", ".md", ".toml", ".json"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_table(path: Path, config: Mapping[str, Any]) -> tuple[pd.DataFrame, str]:
    """Hash exactly the bytes decoded, with strict CSV record widths."""
    blob = path.read_bytes()
    digest = hashlib.sha256(blob).hexdigest()
    suffix = path.suffix.lower()
    if suffix == ".csv":
        text = blob.decode(config.get("encoding", "utf-8-sig"))
        delimiter = config.get("delimiter", ",")
        if not isinstance(delimiter, str) or len(delimiter) != 1:
            raise ValueError("delimiter must be one explicit character")
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True))
        if not rows:
            raise ValueError("external CSV has no header")
        header = rows[0]
        if not header or any(not value.strip() for value in header):
            raise ValueError("external CSV has empty column names")
        if len(set(header)) != len(header):
            raise ValueError("external CSV has duplicate column names")
        records = []
        for record_number, row in enumerate(rows[1:], 2):
            if len(row) != len(header):
                raise ValueError(f"CSV record {record_number}: expected {len(header)} fields, got {len(row)}")
            records.append(row)
        return pd.DataFrame(records, columns=header), digest
    if suffix == ".parquet":
        return pd.read_parquet(io.BytesIO(blob)), digest
    if suffix in {".jsonl", ".ndjson"}:
        records = []
        for line_number, line in enumerate(blob.decode(config.get("encoding", "utf-8-sig")).splitlines(), 1):
            if not line.strip():
                raise ValueError(f"JSONL line {line_number} is empty")
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(f"JSONL line {line_number} must be an object")
            records.append(record)
        return pd.DataFrame(records), digest
    raise ValueError("external features must be CSV, Parquet, JSONL, or NDJSON; pickle is never loaded")


def _mapped(frame: pd.DataFrame, config: Mapping[str, Any], name: str) -> pd.Series:
    columns = config.get("columns", {})
    constants = config.get("constants", {})
    if name in columns and name in constants:
        raise ValueError(f"{name}: choose a column OR a constant")
    if name in columns:
        source = columns[name]
        if source not in frame.columns:
            raise ValueError(f"missing explicitly mapped column {source!r} for {name}")
        return frame[source].copy()
    if name in constants:
        if isinstance(constants[name], (dict, list, tuple)):
            raise ValueError(f"{name}: constant must be scalar")
        return pd.Series(constants[name], index=frame.index)
    raise ValueError(f"explicit mapping or constant required for {name}")


def _nonempty(series: pd.Series, name: str) -> pd.Series:
    if series.isna().any():
        raise ValueError(f"{name} contains missing metadata")
    out = series.astype(str).str.strip()
    if out.eq("").any():
        raise ValueError(f"{name} contains empty metadata")
    return out


def _cycle(series: pd.Series, name: str) -> pd.Series:
    numbers = pd.to_numeric(series, errors="raise")
    if numbers.isna().any() or not numbers.map(lambda x: math.isfinite(float(x))).all():
        raise ValueError(f"{name} must contain finite cycle numbers")
    if ((numbers < 0) | (numbers != numbers.round())).any():
        raise ValueError(f"{name} must contain nonnegative integer cycles")
    return numbers.astype("int64")


def _values(series: pd.Series) -> tuple[pd.Series, pd.Series]:
    # Empty cells are retained as missing; malformed numeric strings are errors.
    normalized = series.replace(r"^\s*$", None, regex=True)
    numeric = pd.to_numeric(normalized, errors="raise").astype(float)
    if numeric.dropna().map(lambda x: not math.isfinite(x)).any():
        raise ValueError("external feature values must be finite or missing")
    quality = pd.Series("ok", index=series.index)
    quality.loc[numeric.isna()] = "missing"
    return numeric, quality


def import_external_features(path: str | Path, mapping: Mapping[str, Any]) -> pd.DataFrame:
    """Import an explicitly described export into the versioned long contract.

    Wide mapping example::

        {"namespace": "cyclediag", "layout": "wide",
         "columns": {"cell_id": "cell_id", "cycle": "cycle"},
         "constants": {"test_id": "test-01", "definition_version": "reviewed-v1",
                       "source_revision": "<full 40-char git commit or 64-char SHA256>",
                       "observed_until_cycle": 300},
         "metrics": {"CE": {"metric_id": "CE", "unit": "%",
                            "role": "indicator", "family": "efficiency"}}}

    For long exports map ``metric_id``, ``value``, and ``unit`` in ``columns``
    (or ``constants``). ``metrics`` is then a mandatory allowlist keyed by the
    source metric ID; each entry declares the expected unit and output ID.
    An unconfigured AIM namespace has no defaults and cannot invent metrics.
    Duplicate output keys are rejected even when values agree, to prevent
    accidentally double-counting repeated exports or SOC-specific records.
    """
    source = Path(path).expanduser().resolve(strict=True)
    namespace = mapping.get("namespace")
    if not isinstance(namespace, str) or not _NAMESPACE.fullmatch(namespace):
        raise ValueError("an explicit simple namespace is required")
    layout = mapping.get("layout", "wide")
    if layout not in {"wide", "long"}:
        raise ValueError("layout must be wide or long")
    metrics = mapping.get("metrics")
    if not isinstance(metrics, Mapping) or not metrics:
        raise ValueError("metrics must explicitly declare source IDs, metric_id, and unit")
    output_ids: set[str] = set()
    for source_metric, spec in metrics.items():
        if not isinstance(source_metric, str) or not isinstance(spec, Mapping):
            raise ValueError("each metric specification must be a mapping with a string source key")
        for field in ("metric_id", "unit"):
            if not isinstance(spec.get(field), str) or not spec[field].strip():
                raise ValueError(f"metric {source_metric!r} requires explicit {field}")
        if spec["metric_id"] in output_ids:
            raise ValueError("multiple source metrics map to the same metric_id")
        output_ids.add(spec["metric_id"])
    frame, digest = _read_table(source, mapping)
    if frame.columns.duplicated().any():
        raise ValueError("external table has duplicate column names")
    base = pd.DataFrame({name: _mapped(frame, mapping, name) for name in REQUIRED_METADATA})
    for name in ("cell_id", "test_id", "definition_version", "source_revision"):
        base[name] = _nonempty(base[name], name)
    if not base["source_revision"].map(lambda x: bool(_REVISION.fullmatch(x))).all():
        raise ValueError("source_revision requires a full 40-char git commit or 64-char SHA256")
    for name in ("cycle", "observed_until_cycle"):
        base[name] = _cycle(base[name], name)
    if (base["observed_until_cycle"] < base["cycle"]).any():
        raise ValueError("observed_until_cycle cannot precede the feature's cycle")
    base["source_path"] = str(source)
    base["source_sha256"] = digest
    base["source_row"] = frame.index + 1  # one-based data record; excludes CSV header
    source_ids = _mapped(frame, mapping, "metric_id") if layout == "long" else None
    if source_ids is not None:
        source_ids = _nonempty(source_ids, "metric_id")
        unknown = sorted(set(source_ids) - set(metrics))
        if unknown:
            raise ValueError(f"long export contains undeclared metrics: {unknown[:10]}")
        all_values = _mapped(frame, mapping, "value")
        all_units = _nonempty(_mapped(frame, mapping, "unit"), "unit")
    parts = []
    for source_metric, spec in metrics.items():
        if layout == "wide":
            if source_metric not in frame:
                raise ValueError(f"missing explicitly configured metric column {source_metric!r}")
            part = base.copy()
            values = frame[source_metric]
        else:
            selected = source_ids.eq(source_metric)
            part = base.loc[selected].copy()
            values = all_values.loc[selected]
            if not all_units.loc[selected].eq(spec["unit"]).all():
                raise ValueError(f"unit mismatch for {source_metric!r}; no implicit conversion is performed")
        part["metric_id"] = f"{namespace}.{spec['metric_id']}"
        part["unit"] = spec["unit"]
        part["value"], part["quality_status"] = _values(values)
        part["role"] = spec.get("role", "unclassified")
        part["family"] = spec.get("family", part["metric_id"].iloc[0] if len(part) else f"{namespace}.{spec['metric_id']}")
        parts.append(part)
    output = pd.concat(parts, ignore_index=True)[OUTPUT_COLUMNS]
    keys = ["cell_id", "test_id", "cycle", "metric_id", "definition_version", "source_revision"]
    duplicates = output.duplicated(keys, keep=False)
    if duplicates.any():
        example = output.loc[duplicates, keys].head(3).to_dict("records")
        raise ValueError(f"duplicate external feature keys; include SOC/protocol in explicit metric/test mapping: {example}")
    return output.sort_values(["cell_id", "test_id", "cycle", "metric_id"], kind="stable").reset_index(drop=True)


def known_export_mapping(kind: str, *, test_id: str, source_revision: str,
                         definition_version: str, observed_until_cycle: int) -> dict[str, Any]:
    """Return a deliberately small bridge for source-verified scalar columns.

    These do not claim coverage of every feature or AIM. The caller reviews and
    extends ``metrics`` for additional columns; no units are guessed by name.
    A dataset-level cutoff is required because legacy code can smooth or fit
    using later cycles. Use an earlier cutoff only after proving causality.
    """
    if kind == "cyclediag":
        namespace, cycle_column = "cyclediag", "cycle"
        metrics = {
            "dchgCapa": {"unit": "Ah", "role": "target", "family": "capacity_discharge"},
            "chgCapa": {"unit": "Ah", "role": "indicator", "family": "capacity_charge"},
            "CE": {"unit": "%", "role": "indicator", "family": "efficiency"},
            "SoHQ": {"unit": "%", "role": "target", "family": "capacity_discharge"},
        }
    elif kind == "data_analysis_l2_cycles":
        namespace, cycle_column = "l2", "CycleNum"
        metrics = {
            "Q_discharge_mAh": {"unit": "mAh", "role": "target", "family": "capacity_discharge"},
            "Q_charge_mAh": {"unit": "mAh", "role": "indicator", "family": "capacity_charge"},
            "CE": {"unit": "1", "role": "indicator", "family": "efficiency"},
            "retention": {"unit": "1", "role": "target", "family": "capacity_discharge"},
        }
    else:
        raise ValueError("known exports: cyclediag, data_analysis_l2_cycles; AIM requires its actual schema")
    return {"namespace": namespace, "layout": "wide",
            "columns": {"cell_id": "cell_id", "cycle": cycle_column},
            "constants": {"test_id": test_id, "source_revision": source_revision,
                          "definition_version": definition_version,
                          "observed_until_cycle": observed_until_cycle},
            "metrics": {name: {"metric_id": name, **spec} for name, spec in metrics.items()}}


def _target_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _target_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return None


def _strings(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return [item.value for item in node.elts
                if isinstance(item, ast.Constant) and isinstance(item.value, str)]
    return []


def _ast_inventory(text: str, relative_path: str) -> tuple[list[dict], list[dict]]:
    """Extract literal names only; do not eval dynamic expressions or imports."""
    tree = ast.parse(text, filename=relative_path)
    features, symbols = [], []
    catalog_file = Path(relative_path).name in {"lges_catalog.py", "indicator_registry.py"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if re.search(r"aim|life|predict|feature|indicator|capacity|dcir|dqdv|dvdq", node.name, re.I):
                symbols.append({"name": node.name, "kind": type(node).__name__, "line": node.lineno})
        if isinstance(node, ast.Assign):
            assignments = [(target, node.value) for target in node.targets]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            assignments = [(node.target, node.value)]
        else:
            continue
        for target, value in assignments:
            owner = _target_name(target)
            names: list[str] = []
            category = "literal_candidate"
            if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant) and isinstance(target.slice.value, str):
                holder = _target_name(target.value)
                if holder and holder.split(".")[-1] in {"row", "val", "out", "features", "result"}:
                    owner, names = holder, [target.slice.value]
            if owner and isinstance(value, ast.Dict) and (catalog_file or owner.split(".")[-1] in {"val", "features", "metrics"}):
                names = [key.value for key in value.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)]
                if owner == "FAMILIES":
                    category = "family_member"
                    for key, members in zip(value.keys, value.values):
                        if isinstance(key, ast.Constant) and isinstance(key.value, str):
                            for member in _strings(members):
                                features.append({"name": member, "category": category, "owner": key.value, "line": node.lineno})
                    names = []
                elif owner == "EXACT_ALIASES":
                    category = "retired_alias"
            elif owner and isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                if catalog_file or owner.endswith(("_COLS", "_COLUMNS", "_FEATURES")):
                    names = _strings(value)
                    category = "catalog_literal"
            for name in names:
                features.append({"name": name, "category": category, "owner": owner, "line": node.lineno})
    # Source location remains evidence; presence does not establish a validated definition.
    unique = {(f["name"], f["category"], f["owner"], f["line"]): f for f in features}
    return list(unique.values()), symbols


def inspect_assets(roots: list[str], *, max_files: int = 20000,
                   max_file_bytes: int = 2_000_000) -> dict[str, Any]:
    """Inventory local source files without executing or copying their contents.

    Symlink/junction traversal is excluded. JSON/TOML/Markdown are counted and
    hashed only; Python feature candidates are parsed by AST. Dynamic f-string
    generated columns require a reviewed export and are not silently invented.
    ``complete=False`` reports limits, unreadable files, and parse failures.
    """
    if max_files < 1 or max_file_bytes < 1:
        raise ValueError("inventory limits must be positive")
    result: dict[str, Any] = {"schema_version": "1", "roots": [], "files": [],
                              "feature_candidates": [], "symbols": [], "issues": [],
                              "complete": True, "execution": "AST only; source modules never executed"}
    visited: set[str] = set()
    exhausted = False
    for raw_root in roots:
        root = Path(raw_root).expanduser()
        if root.is_symlink() or getattr(root, "is_junction", lambda: False)():
            result["issues"].append({"path": str(root), "reason": "linked root skipped"})
            continue
        root = root.resolve()
        result["roots"].append(str(root))
        if not root.is_dir():
            result["issues"].append({"path": str(root), "reason": "root missing or not directory"})
            continue
        for directory, dirs, files in os.walk(root, followlinks=False,
                                              onerror=lambda error: result["issues"].append({"path": error.filename, "reason": str(error)})):
            dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS
                             and not (Path(directory) / d).is_symlink()
                             and not getattr(Path(directory) / d, "is_junction", lambda: False)())
            for filename in sorted(files):
                path = Path(directory) / filename
                if path.suffix.lower() not in _CODE_EXTENSIONS or path.is_symlink():
                    continue
                key = os.path.normcase(str(path.resolve()))
                if key in visited:
                    continue
                visited.add(key)
                if len(result["files"]) >= max_files:
                    result["issues"].append({"path": str(root), "reason": "max_files reached"})
                    exhausted = True
                    break
                relative = path.relative_to(root).as_posix()
                try:
                    size = path.stat().st_size
                    if size > max_file_bytes:
                        result["issues"].append({"path": str(path), "reason": "max_file_bytes exceeded"})
                        continue
                    blob = path.read_bytes()
                    file_record = {"root": str(root), "path": relative, "bytes": len(blob),
                                   "sha256": hashlib.sha256(blob).hexdigest()}
                    result["files"].append(file_record)
                    if path.suffix.lower() == ".py":
                        # tokenize respects Python encoding cookies without loading code.
                        import tokenize
                        encoding, _ = tokenize.detect_encoding(io.BytesIO(blob).readline)
                        candidates, symbols = _ast_inventory(blob.decode(encoding), relative)
                        provenance = {"root": str(root), "path": relative, "source_sha256": file_record["sha256"]}
                        result["feature_candidates"].extend({**provenance, **item} for item in candidates)
                        result["symbols"].extend({**provenance, **item} for item in symbols)
                except (OSError, UnicodeError, SyntaxError, ValueError) as error:
                    result["issues"].append({"path": str(path), "reason": type(error).__name__ + ": " + str(error)[:180]})
            if exhausted:
                break
        if exhausted:
            break
    result["complete"] = not result["issues"]
    result["counts"] = {"files": len(result["files"]), "feature_candidates": len(result["feature_candidates"]),
                        "symbols": len(result["symbols"]), "issues": len(result["issues"])}
    return result
