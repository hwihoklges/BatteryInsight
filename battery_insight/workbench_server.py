"""Local-only comparison workspace and reviewed registration changes.

The HTTP service binds to loopback and writes only beneath its output directory.
It never modifies a source registration, raw file, or original pipeline config.
"""
from __future__ import annotations

import base64
import copy
import hashlib
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import threading
from urllib.parse import parse_qs, urlparse

import pandas as pd

from .experiment_updates import _atomic_json, _load, load_registrations, update_experiments
from .ingest import sha256_file
from .pipeline import digest_json


MODULES = ["network", "crossview", "evidence", "controlled", "registration", "trajectory", "replicates", "history", "correlation"]
MAX_BODY = 16 * 1024 * 1024
# Mutable published state only. Immutable lake runs and generated run inputs may
# remain after a failed update; none of those uncommitted runs becomes current.
STATE_FILES = ("update_state.json", "experiments.csv", "observations.parquet", "design_registry_snapshot.json",
               "workbench_options.json", "workbench_project.json", "comparison_history.json", "comparison_snapshot.json",
               "comparison_reference.json", "comparison_profiles.json", "registry/experiments.csv", "registry/relations.json",
               "dashboard/index.html", "dashboard/data.json", "dashboard/analysis.json")


class WorkspaceRestoreError(RuntimeError):
    """A real filesystem failure prevented restoration of some published files."""


def now():
    return datetime.now(timezone.utc).isoformat()


def defaults(bundle):
    settings = bundle.get("comparison", {})
    metric_ids = [m["metric_id"] for m in bundle.get("metrics", []) if m.get("selected_by_default", True)]
    field_ids = [m["field_id"] for m in bundle.get("design_fields", []) if m.get("include", True)]
    lo, hi = settings.get("life_min", 1), settings.get("life_max", 100)
    mid = (lo + hi) / 2
    windows = [{"label": "전체", "life_min": lo, "life_max": hi}]
    if hi - lo >= 4:
        windows += [{"label": "초기", "life_min": lo, "life_max": math.floor(mid)},
                    {"label": "후기", "life_min": math.floor(mid) + 1, "life_max": hi}]
    return {"enabled_modules": MODULES.copy(), "metric_ids": metric_ids, "design_field_ids": field_ids,
            "life_min": lo, "life_max": hi, "summary": settings.get("summary", "median"),
            "min_overlap": min(3, len(metric_ids)) or 1, "min_coverage": .6,
            "scale_mode": "current", "cross_filter": {"design_min": 0, "design_max": 1, "electrochem_min": 0, "electrochem_max": 1},
            "controlled": {"target": "", "controls": [], "tolerances": {}},
            "windows": windows, "replicate_group_by": ["design_id", "batch_id"]}


def _frame_snapshot(analysis):
    graphs = analysis.get("graphs", {})
    values, edges, scales, ids = {}, {}, {}, set()
    for mode, graph in graphs.items():
        for node in graph.get("nodes", []):
            ids.add(node["experiment_id"])
            for feature in node.get("features", []):
                values[json.dumps([mode, node["experiment_id"], feature["metric_id"]])] = feature.get("value")
        for edge in graph.get("edges", []):
            key = json.dumps([mode, *sorted([edge["source"], edge["target"]])])
            edges[key] = {"similarity": edge["similarity"], "coverage": edge.get("coverage")}
        if mode == "electrochem":
            scales = _scale_identity({c["cohort"]: c.get("scales", []) for c in graph.get("cohorts", [])})
    return {"experiment_ids": sorted(ids), "values": values, "edges": edges, "scales": scales,
            "options": analysis.get("options", {})}


def _scale_identity(scales):
    """Sample counts are population metadata, not a change to standardization."""
    return {cohort: sorted([{key: row.get(key) for key in ("metric_id", "center", "scale", "method")}
                             for row in records], key=lambda row: row["metric_id"])
            for cohort, records in scales.items()}


def history_entry(previous, current, *, reference_id=None):
    """Compare graph snapshots, retaining counts and bounded auditable changes."""
    before = previous or {"experiment_ids": [], "values": {}, "edges": {}, "scales": {}, "options": {}}
    old_ids, new_ids = set(before["experiment_ids"]), set(current["experiment_ids"])
    value_changes = [{"identity": json.loads(key), "before": before["values"][key], "after": value}
                     for key, value in current["values"].items()
                     if key in before["values"] and before["values"][key] != value]
    edge_changes = [{"identity": json.loads(key), "before": before["edges"][key], "after": value}
                    for key, value in current["edges"].items()
                    if key in before["edges"] and before["edges"][key] != value]
    scale_changed = bool(previous and _scale_identity(before["scales"]) != _scale_identity(current["scales"]))
    option_changed = bool(previous and before["options"] != current["options"])
    reasons = []
    if not previous:
        reasons.append("initial_snapshot")
    if new_ids != old_ids:
        reasons.append("population_changed")
    if option_changed:
        reasons.append("comparison_options_changed")
    if value_changes:
        reasons.append("summarized_values_changed")
    if scale_changed:
        reasons.append("standardization_changed")
    return {"id": digest_json(current)[:20], "generated_at": now(), "reason": ", ".join(reasons) or "connections_changed",
            "reference_id": reference_id, "node_count": len(new_ids), "edge_count": len(current["edges"]),
            "added_experiments": sorted(new_ids - old_ids), "removed_experiments": sorted(old_ids - new_ids),
            "changed_values": len(value_changes), "changed_edges": len(edge_changes),
            "added_edges": len(set(current["edges"]) - set(before["edges"])),
            "removed_edges": len(set(before["edges"]) - set(current["edges"])),
            "scale_changed": scale_changed, "options_changed": option_changed,
            "value_changes": value_changes[:200], "edge_changes": edge_changes[:200], "detail_limit": 200}


class Workbench:
    def __init__(self, config_path, output, *, initialize=True, verify_all=False):
        self.original_config = Path(config_path).resolve()
        self.output = Path(output).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.config_path = self._effective_config()
        self.bundle, self.analysis = {}, {}
        self.last_error = None
        self._state_generation = 0
        self._http_state_cache = None
        self.verify_all = verify_all
        self.options = _load(self.output / "workbench_options.json") if (self.output / "workbench_options.json").exists() else None
        if initialize:
            self.refresh()

    def _effective_config(self):
        state_path = self.output / "workbench_project.json"
        if state_path.exists():
            state = _load(state_path)
            if state.get("original_config") != str(self.original_config):
                raise ValueError("This output workspace belongs to a different relations config")
            managed = self.output / "registry" / "relations.json"
            if state.get("managed_registry") and managed.exists():
                return managed
        return self.original_config

    def _config_rows(self):
        config = _load(self.config_path)
        inbox = (self.config_path.parent / config["inbox"]).resolve()
        registry = (self.config_path.parent / config["registrations"]).resolve()
        rows = load_registrations(registry, inbox)
        return config, inbox, registry, rows

    def _output_path(self, path):
        """Validate each exact file before backup, replacement or unlink."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.output / candidate
        resolved = candidate.resolve()
        if resolved == self.output or not resolved.is_relative_to(self.output) or resolved != candidate.absolute():
            raise ValueError("Transaction files must resolve directly inside the output workspace")
        if resolved.exists() and not resolved.is_file():
            raise ValueError("Transaction target must be a regular file")
        return resolved

    @contextmanager
    def _transaction(self, extra_paths=()):
        """Restore an explicit file set and in-memory results after any failure.

        No directory is recursively removed. A file absent before the operation
        is individually unlinked on rollback. Every path is revalidated then.
        """
        paths = [self._output_path(path) for path in (*STATE_FILES, *extra_paths)]
        paths += [self._output_path(path.with_name(path.name + ".tmp")) for path in paths]
        backups = {path: path.read_bytes() if path.exists() else None for path in dict.fromkeys(paths)}
        memory = (copy.deepcopy(self.bundle), copy.deepcopy(self.analysis), copy.deepcopy(self.options), self.config_path, self.last_error)
        try:
            yield
        except Exception as error:
            failures = []
            for path, content in backups.items():
                try:
                    safe = self._output_path(path)
                    if content is None:
                        safe.unlink(missing_ok=True)
                    elif not safe.exists() or safe.read_bytes() != content:
                        safe.parent.mkdir(parents=True, exist_ok=True)
                        safe.write_bytes(content)
                except OSError as restore_error:
                    failures.append(f"{path.name}: {restore_error}")
                except ValueError as restore_error:
                    failures.append(f"{path.name}: {restore_error}")
            self.bundle, self.analysis, self.options, self.config_path, self.last_error = memory
            if failures:
                self.last_error = "Workspace restoration incomplete: " + "; ".join(failures)
                raise WorkspaceRestoreError(self.last_error) from error
            raise

    def _analyze(self, options):
        from .workbench_analysis import analyze_bundle
        if not isinstance(options, dict):
            raise ValueError("Comparison options must be an object")
        bundle = self.bundle
        ref_id = options.get("reference_id") if options.get("scale_mode") == "fixed" else None
        if ref_id and (bundle.get("baseline") or {}).get("reference_id") != ref_id:
            if not re.fullmatch(r"[a-f0-9]{20}", str(ref_id)):
                raise ValueError("Unknown comparison reference")
            path = self.output / "references" / (ref_id + ".json")
            if not path.is_file():
                raise ValueError("Saved comparison reference is missing")
            bundle = dict(bundle, baseline=_load(path))
        return analyze_bundle(bundle, options)

    def _sync_active_reference(self):
        """Publish the reference actually used without replacing the latest save.

        The analysis carries this record too because /api/compare returns the
        analysis directly; a client need not wait for its next state poll.
        """
        active = None
        if self.options.get("scale_mode") == "fixed":
            reference_id = self.analysis.get("metadata", {}).get("reference", {}).get("reference_id")
            if reference_id != self.options.get("reference_id"):
                raise ValueError("Applied analysis and fixed reference identity differ")
            latest = self.bundle.get("baseline") or {}
            if latest.get("reference_id") == reference_id:
                active = copy.deepcopy(latest)
            else:
                if not isinstance(reference_id, str) or not re.fullmatch(r"[a-f0-9]{20}", reference_id):
                    raise ValueError("Unknown active comparison reference")
                active = _load(self._output_path(self.output / "references" / (reference_id + ".json")))
                if active.get("reference_id") != reference_id:
                    raise ValueError("Active comparison reference file has a different identity")
        self.bundle["active_reference"] = copy.deepcopy(active)
        self.analysis["active_reference"] = copy.deepcopy(active)

    def refresh(self):
        from .workbench_data import build_workbench_bundle
        with self.lock, self._transaction():
            if _load(self.config_path).get("parquet_library"):
                from .library_workbench import build_library_bundle
                self.bundle = build_library_bundle(self.config_path, self.output)
            elif _load(self.config_path).get("database_path"):
                from .database_workbench import build_database_bundle
                self.bundle = build_database_bundle(self.config_path, self.output)
            else:
                payload = update_experiments(self.config_path, self.output, verify_all=self.verify_all, render_report=False)
                config, _, registry, rows = self._config_rows()
                self.bundle = build_workbench_bundle(self.output, config, rows, payload)
                self.bundle["registry_hash"] = sha256_file(registry)
            self.bundle["source_config"] = str(self.original_config)
            baseline_file = self.output / "comparison_reference.json"
            if baseline_file.exists():
                self.bundle["baseline"] = _load(baseline_file)
            options = self._reconcile_options(self.options) if self.options is not None else defaults(self.bundle)
            self.analysis = self._analyze(options)
            self.options = self.analysis.get("options", options)
            self._sync_active_reference()
            self._persist()
            self.last_error = None
            return self.state()

    def _reconcile_options(self, previous):
        """Removed measurements stay unselected; never replace an empty choice with all."""
        options, notices = copy.deepcopy(previous), []
        for key, specs, identifier in (("metric_ids", self.bundle.get("metrics", []), "metric_id"),
                                      ("design_field_ids", self.bundle.get("design_fields", []), "field_id")):
            known = {item[identifier] for item in specs}
            selected = options.get(key, [])
            missing = [item for item in selected if item not in known]
            if missing:
                notices.append({"reason": "selected_items_no_longer_available", "selection": key, "items": missing})
            options[key] = [item for item in selected if item in known]
        count = len(options["metric_ids"])
        if count and options.get("min_overlap", 1) > count:
            options["min_overlap"] = count
            notices.append({"reason": "minimum_overlap_adjusted_to_remaining_selection", "value": count})
        known_ids = {r["experiment_id"] for r in self.bundle.get("experiments", [])}
        if options.get("reference_experiment_id") and options["reference_experiment_id"] not in known_ids:
            notices.append({"reason": "reference_experiment_no_longer_registered", "experiment_id": options["reference_experiment_id"]})
            options.pop("reference_experiment_id")
        control = options.get("controlled", {})
        known_fields = set(options["design_field_ids"]) | {key for row in self.bundle.get("experiments", []) for key in row}
        if control.get("target") and control["target"] not in options["design_field_ids"]:
            control.update(target="", controls=[], tolerances={})
            notices.append({"reason": "controlled_target_unselected"})
        self.bundle["selection_notices"] = notices
        return options

    def _persist(self):
        from .workbench_report import write_workbench_report
        # Published analytical state changes only through controller operations.
        # A generation prevents object-id reuse from reviving an old HTTP cache.
        self._state_generation += 1
        self._http_state_cache = None
        snapshot = _frame_snapshot(self.analysis)
        path = self.output / "comparison_snapshot.json"
        previous = _load(path) if path.exists() else None
        if previous:
            previous["scales"] = _scale_identity(previous.get("scales", {}))
        history = self.history()
        if previous != snapshot:
            history.append(history_entry(previous, snapshot, reference_id=self.options.get("reference_id")))
            _atomic_json(self.output / "comparison_history.json", history[-100:])
            _atomic_json(path, snapshot)
        self.bundle["history"] = history[-100:]
        self.bundle["profiles"] = self.profiles()
        _atomic_json(self.output / "workbench_options.json", self.options)
        _atomic_json(self.output / "workbench_project.json", {"original_config": str(self.original_config),
                     "managed_registry": self.config_path != self.original_config})
        write_workbench_report(self.bundle, self.analysis, self.output / "dashboard")

    def compare(self, options):
        if not isinstance(options, dict):
            raise ValueError("Comparison options must be an object")
        with self.lock, self._transaction():
            analysis = self._analyze(options)
            self.analysis, self.options = analysis, analysis.get("options", copy.deepcopy(options))
            self._sync_active_reference()
            self._persist()
            return analysis

    def state(self):
        with self.lock:
            return {"bundle": self.bundle, "analysis": self.analysis, "profiles": self.profiles(),
                    "history": self.history(), "capabilities": {"server": True, "registration": True, "evidence": True, **self.bundle.get("capabilities", {})},
                    "registry_hash": self.bundle.get("registry_hash"), "last_error": self.last_error}

    def http_state(self, if_none_match=None):
        """Serialize each published state once; unchanged polls send no body.

        Profile/history content is reread so even an external edit with restored
        timestamps is noticed. This cache never substitutes for raw verification.
        """
        with self.lock:
            profiles, history = self.profiles(), self.history()
            key = (self._state_generation, id(self.bundle), id(self.analysis),
                   digest_json([profiles, history]), self.last_error)
            cached = self._http_state_cache
            if cached is None or cached[0] != key:
                state = {"bundle": self.bundle, "analysis": self.analysis, "profiles": profiles,
                         "history": history, "capabilities": {"server": True, "registration": True,
                            "evidence": True, **self.bundle.get("capabilities", {})},
                         "registry_hash": self.bundle.get("registry_hash"), "last_error": self.last_error}
                encoded = json.dumps(state, ensure_ascii=False, allow_nan=False).encode("utf-8")
                etag = '"' + hashlib.sha256(encoded).hexdigest() + '"'
                cached = self._http_state_cache = (key, etag, encoded)
            _, etag, encoded = cached
            tags = {tag.strip().removeprefix("W/") for tag in (if_none_match or "").split(",")}
            return (304, b"", etag) if etag in tags or "*" in tags else (200, encoded, etag)

    def profiles(self):
        path = self.output / "comparison_profiles.json"
        return _load(path) if path.exists() else []

    def history(self):
        path = self.output / "comparison_history.json"
        return _load(path) if path.exists() else []

    def save_profile(self, name, options, visible_modules):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError("Profile name must have 1–80 characters")
        if not isinstance(visible_modules, list) or set(visible_modules) - set(MODULES):
            raise ValueError("Unknown visible module")
        with self.lock:
            verified = self._analyze(options)
            profile = {"id": digest_json(name.strip())[:20], "name": name.strip(),
                       "options": verified.get("options", options), "visible_modules": visible_modules,
                       "updated_at": now()}
            profiles = [p for p in self.profiles() if p["id"] != profile["id"]] + [profile]
            if len(profiles) > 100:
                raise ValueError("At most 100 named profiles can be stored")
            _atomic_json(self.output / "comparison_profiles.json", profiles)
            return {"profile": profile, "profiles": profiles}

    def save_baseline(self, options=None):
        with self.lock:
            selected = copy.deepcopy(options or self.options)
            selected["scale_mode"] = "current"
            selected.pop("reference_id", None)
            analysis = self._analyze(selected)
            graph = analysis["graphs"]["electrochem"]
            reference = {"scales": {c["cohort"]: c.get("scales", []) for c in graph.get("cohorts", [])},
                         "summary": analysis["options"].get("summary", "median"),
                         "metrics": [{"metric_id": m["metric_id"], "unit": m["unit"]} for m in graph.get("metrics", [])],
                         "experiment_ids": [n["experiment_id"] for n in graph.get("nodes", [])],
                         "life_min": analysis["options"]["life_min"], "life_max": analysis["options"]["life_max"]}
            if not any(reference["scales"].values()):
                raise ValueError("No informative electrochemical scales are available to save")
            reference["reference_id"] = digest_json(reference)[:20]
            reference["created_at"] = now()
            reference_path = self._output_path(self.output / "references" / (reference["reference_id"] + ".json"))
            with self._transaction(extra_paths=[reference_path]):
                _atomic_json(reference_path, reference)
                _atomic_json(self.output / "comparison_reference.json", reference)
                self.bundle["baseline"] = reference
                selected.update(scale_mode="fixed", reference_id=reference["reference_id"])
                self.compare(selected)
                return {"reference": reference, "bundle": self.bundle, "analysis": self.analysis}

    def evidence(self, experiment_id, cycle=None):
        from .workbench_data import read_evidence
        with self.lock:
            if self.bundle.get("source_mode") == "parquet_library":
                from .library_workbench import library_evidence
                reference = self.bundle.get("evidence_references", {}).get(experiment_id)
                if reference is None:
                    raise ValueError("Parquet source reference not found")
                return library_evidence(reference, cycle)
            if self.bundle.get("source_mode") == "database":
                reference = self.bundle.get("evidence_references", {}).get(experiment_id)
                if reference is None:
                    raise ValueError("Experiment has no enabled completed database run")
                return read_evidence(self.output, experiment_id, cycle, reference=reference)
            return read_evidence(self.output, experiment_id, cycle)

    def _require_registration(self):
        if _load(self.config_path).get("database_path") or _load(self.config_path).get("parquet_library"):
            raise ValueError(self.bundle.get("capabilities", {}).get("registration_reason", "Database registration is read-only here; edit the database experiments.csv metadata and refresh"))

    def preview_upload(self, content, filename, sheet=None, mapping=None):
        self._require_registration()
        from .workbench_data import parse_registration_upload
        parsed = parse_registration_upload(content, filename, sheet)
        if parsed.get("requires_sheet_selection"):
            _, _, registry, _ = self._config_rows()
            return dict(parsed, validation={"valid": False, "errors": ["Select a worksheet before validating this upload"],
                                            "requires_sheet_selection": True}, registry_hash=sha256_file(registry))
        rows = parsed["rows"]
        if mapping:
            if not isinstance(mapping, dict) or any(v not in parsed["columns"] for v in mapping.values()):
                raise ValueError("Column mapping must use uploaded column names")
            # Explicit mapping adds canonical names while retaining unselected metadata.
            rows = [dict(row, **{target: row.get(source, "") for target, source in mapping.items()}) for row in rows]
        return dict(parsed, rows=rows, **self.validate_registration(rows))

    def _prepare_rows(self, incoming, mode):
        self._require_registration()
        if mode not in {"merge", "replace"}:
            raise ValueError("Registration mode must be merge or replace")
        if not isinstance(incoming, list) or len(incoming) > 10000 or not all(isinstance(r, dict) for r in incoming):
            raise ValueError("Registration must contain at most 10,000 row objects")
        if any(not all(isinstance(k, str) and k.strip() for k in r) for r in incoming):
            raise ValueError("Registration column names must be nonempty strings")
        if any(isinstance(v, (dict, list)) for r in incoming for v in r.values()):
            raise ValueError("Registration values must be scalar cells")
        config, inbox, registry, existing = self._config_rows()
        ids = [str(r.get("experiment_id", "")).strip() for r in incoming]
        if any(not i for i in ids) or len(ids) != len(set(ids)):
            raise ValueError("Upload requires unique, nonempty experiment_id values")
        current = {r["experiment_id"]: dict(r) for r in existing}
        merged = current.copy() if mode == "merge" else {}
        for row, identity in zip(incoming, ids):
            merged[identity] = dict(merged.get(identity, {})) | row | {"experiment_id": identity}
        rows = list(merged.values())
        for row in rows:
            # Existing config_path entries must keep their original relative base.
            if row.get("config_path"):
                row["config_path"] = str((self.config_path.parent / str(row["config_path"])).resolve())
            row["include"] = str(row.get("include", True)).lower()
        candidate = self._output_path(self.output / "registry" / "preview.csv")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        try:
            pd.DataFrame(rows).to_csv(candidate, index=False, encoding="utf-8-sig")
            verified = load_registrations(candidate, inbox)
        finally:
            candidate.unlink(missing_ok=True)
        enabled = pd.DataFrame([r for r in verified if r["include"]])
        if not enabled.empty:
            for cohort, members in enabled.groupby("cohort"):
                guards = ["protocol", "test_type"] + ([] if config["comparison"].get("allow_mixed_chemistry", False) else ["chemistry"])
                for key in guards:
                    if members[key].nunique() > 1:
                        raise ValueError(f"Cohort {cohort!r} mixes {key}")
        if "design" in config:
            from .design_relations import build_design_relations
            design = config["design"]
            frame = pd.DataFrame(verified) if verified else pd.DataFrame(columns=["experiment_id"])
            build_design_relations(frame, design["fields"], design.get("settings", {}))
        return config, registry, verified, len(set(ids) - set(current)), len(set(ids) & set(current))

    def validate_registration(self, rows, mode="merge"):
        with self.lock:
            self._require_registration()
            _, _, registry, _ = self._config_rows()
            try:
                _, _, verified, added, updated = self._prepare_rows(rows, mode)
                return {"validation": {"valid": True, "errors": [], "total": len(verified), "added": added, "updated": updated},
                        "registry_hash": sha256_file(registry)}
            except (ValueError, TypeError, KeyError, OSError) as exc:
                return {"validation": {"valid": False, "errors": [str(exc)]}, "registry_hash": sha256_file(registry)}

    def apply_registration(self, rows, *, mode="merge", expected_registry_hash=None):
        with self.lock:
            config, registry, verified, added, updated = self._prepare_rows(rows, mode)
            if not expected_registry_hash or sha256_file(registry) != expected_registry_hash:
                raise ValueError("Registration changed since preview; preview the current rows again")
            managed = self.output / "registry"
            managed.mkdir(parents=True, exist_ok=True)
            new_registry, new_config = managed / "experiments.csv", managed / "relations.json"
            converted = copy.deepcopy(config)
            for key in ("inbox", "pipeline_template"):
                converted[key] = str((self.config_path.parent / config[key]).resolve())
            converted["registrations"] = str(new_registry)
            archived = None
            if new_registry.exists():
                old_hash = sha256_file(new_registry)
                archived = self._output_path(managed / "versions" / (old_hash + ".csv"))
            with self._transaction(extra_paths=[archived] if archived else []):
                if archived and not archived.exists():
                    archived.parent.mkdir(parents=True, exist_ok=True)
                    archived.write_bytes(new_registry.read_bytes())
                temporary = self._output_path(new_registry.with_suffix(".csv.tmp"))
                pd.DataFrame(verified).to_csv(temporary, index=False, encoding="utf-8-sig")
                temporary.replace(new_registry)
                _atomic_json(new_config, converted)
                self.config_path = new_config
                _atomic_json(self.output / "workbench_project.json", {"original_config": str(self.original_config), "managed_registry": True})
                self.refresh()
                return {"saved": True, "added": added, "updated": updated, "registration_path": str(new_registry),
                        "registry_hash": self.bundle["registry_hash"], "bundle": self.bundle, "analysis": self.analysis}


def make_server(workbench, port=8765):
    if not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("Port must be 0..65535")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send_json(self, value, status=200):
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
            Handler.send_encoded(self, encoded, status)

        def send_encoded(self, encoded, status=200, etag=None):
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                if status != 304:
                    self.send_header("Content-Length", str(len(encoded)))
                if etag is not None:
                    self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(encoded)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # The operation has already committed. A cancelled browser request
                # must not trigger a second response or roll back that operation.
                self.close_connection = True

        def local_request(self, *, write=False):
            host = self.headers.get("Host", "")
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if host not in allowed:
                raise PermissionError("Use the local application URL")
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + h for h in allowed}:
                raise PermissionError("Cross-origin requests are not accepted")
            if write and self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise ValueError("Requests must use application/json")

        def do_GET(self):
            try:
                self.local_request()
                parsed = urlparse(self.path)
                if parsed.path == "/api/state":
                    status, encoded, etag = workbench.http_state(self.headers.get("If-None-Match"))
                    self.send_encoded(encoded, status, etag)
                elif parsed.path == "/api/profiles":
                    self.send_json({"profiles": workbench.profiles()})
                elif parsed.path == "/api/history":
                    self.send_json({"history": workbench.history()})
                elif parsed.path == "/api/evidence":
                    params = parse_qs(parsed.query)
                    identity = params.get("experiment_id", [""])[0]
                    cycle = float(params["cycle"][0]) if params.get("cycle") else None
                    if cycle is not None and not math.isfinite(cycle):
                        raise ValueError("Cycle must be finite")
                    self.send_json(workbench.evidence(identity, cycle))
                elif parsed.path in {"/", "/index.html", "/data.json", "/analysis.json"}:
                    filename = "index.html" if parsed.path in {"/", "/index.html"} else parsed.path[1:]
                    path = workbench.output / "dashboard" / filename
                    content = path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8" if filename.endswith(".html") else "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.end_headers()
                    self.wfile.write(content)
                else:
                    self.send_json({"error": "Not found"}, 404)
            except PermissionError as exc:
                self.send_json({"error": str(exc)}, 403)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.send_json({"error": str(exc)}, 400)

        def do_POST(self):
            try:
                self.local_request(write=True)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise ValueError("Request body must be between 1 byte and 16 MiB")
                def reject_constant(value):
                    raise ValueError("JSON numbers must be finite")
                def finite_float(value):
                    number = float(value)
                    if not math.isfinite(number):
                        raise ValueError("JSON numbers must be finite")
                    return number
                body = json.loads(self.rfile.read(size), parse_constant=reject_constant, parse_float=finite_float)
                if not isinstance(body, dict):
                    raise ValueError("Request must be a JSON object")
                route = urlparse(self.path).path
                if route == "/api/compare":
                    result = workbench.compare(body["options"])
                elif route == "/api/profiles":
                    result = workbench.save_profile(body.get("name"), body.get("options", {}), body.get("visible_modules", MODULES))
                elif route == "/api/baseline":
                    result = workbench.save_baseline(body.get("options"))
                elif route == "/api/refresh":
                    result = workbench.refresh()
                elif route == "/api/registrations/preview":
                    try:
                        content = base64.b64decode(body["content_base64"], validate=True)
                    except (ValueError, TypeError) as exc:
                        raise ValueError("Upload must contain valid base64 bytes") from exc
                    result = workbench.preview_upload(content, body["filename"], body.get("sheet"), body.get("mapping"))
                elif route == "/api/registrations/validate":
                    result = workbench.validate_registration(body["rows"], body.get("mode", "merge"))
                elif route == "/api/registrations/apply":
                    result = workbench.apply_registration(body["rows"], mode=body.get("mode", "merge"),
                                                         expected_registry_hash=body.get("expected_registry_hash"))
                else:
                    self.send_json({"error": "Not found"}, 404)
                    return
                self.send_json(result)
            except WorkspaceRestoreError as exc:
                self.send_json({"error": str(exc), "code": "recovery_required"}, 500)
            except PermissionError as exc:
                self.send_json({"error": str(exc)}, 403)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.send_json({"error": str(exc)}, 400)
            except Exception:
                self.send_json({"error": "Local operation failed; the previous results remain available", "code": "internal_error"}, 500)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve_workbench(config, output, *, port=8765, interval=30, verify_all=False, open_browser=False):
    if not math.isfinite(interval) or interval < 1:
        raise ValueError("Watch interval must be at least one second")
    controller = Workbench(config, output, verify_all=verify_all)
    server = make_server(controller, port)
    stopped = threading.Event()

    def watch():
        while not stopped.wait(interval):
            try:
                controller.refresh()
            except Exception as exc:
                controller.last_error = str(exc)
                print(f"Update deferred: {exc}", flush=True)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    address = f"http://127.0.0.1:{server.server_port}/"
    print(f"Local comparison workspace: {address}", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(address)
    try:
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        print("Stopped. Saved results and comparison profiles remain available.")
    finally:
        stopped.set()
        server.server_close()

