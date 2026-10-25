"""One-command, incremental build of the design comparison database.

derived steps -> derived cycles -> cycle profiles -> run metrics -> validation -> research DB -> design network.
Each stage is skipped when its input fingerprint (file size/mtime of inputs and
the stage's own source code) matches the previous successful build, so a
no-change rebuild touches only a handful of files on the NAS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import time

STATE_FILE = "build_state.json"


def _stat(path):
    try:
        info = Path(path).stat()
        return [info.st_size, info.st_mtime_ns]
    except OSError:
        return None


def _code(module):
    return hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()[:16]


def _fingerprint(inputs, modules):
    payload = {"inputs": {str(p): _stat(p) for p in inputs}, "code": [_code(m) for m in modules]}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def build_database(project, *, workers=8, network=True, force=False, profile_every=10, progress=print):
    """``profile_every``: keep a resampled V–Q curve every N-th cycle (+special cycles); 0 disables."""
    from . import derived_cycles, derived_steps, research_db, run_metrics
    project = Path(project).resolve()
    db = project / "db"
    db.mkdir(parents=True, exist_ok=True)
    state_path = db / STATE_FILE
    try:
        state = {} if force else json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    stages, started = {}, time.perf_counter()
    library_db = project / "library.sqlite3"
    dx = project / "dx"
    derived = project / "derived"

    def stage(name, function, inputs, outputs, modules):
        begin = time.perf_counter()
        key = _fingerprint(inputs, modules)
        if state.get(name) == key and all(Path(p).exists() for p in outputs):
            stages[name] = {"seconds": round(time.perf_counter() - begin, 1), "result": "unchanged"}
            if progress:
                progress(f"[{name}] 변경 없음")
            return
        if progress:
            progress(f"[{name}] 시작")
        value = function()
        # Fingerprint after the run so the stage's own outputs (e.g. sidecar dirs) are captured.
        state[name] = _fingerprint(inputs, modules)
        state_path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        stages[name] = {"seconds": round(time.perf_counter() - begin, 1), "result": _brief(value)}
        if progress:
            progress(f"[{name}] 완료 {stages[name]['seconds']} s")

    steps_dir, cycles_dir = derived / "steps", derived / "cycles"
    summary = derived_cycles.cycles_summary_path(project)
    run_outputs = [db / "run_profile.parquet", db / "run_metrics.parquet", db / "run_cycles.parquet"]
    stage("steps", lambda: derived_steps.build_step_tables(project, workers=workers, progress=progress),
          [library_db, steps_dir], [steps_dir], [derived_steps])
    stage("cycles", lambda: derived_cycles.build_cycle_tables(project, workers=workers, progress=progress),
          [library_db, steps_dir, cycles_dir, summary], [summary], [derived_steps, derived_cycles])
    if profile_every:
        from . import cycle_profiles
        profiles = db / "cycle_profiles.parquet"
        stage("profiles", lambda: cycle_profiles.build_cycle_profiles(project, every=profile_every, workers=workers,
                                                                      progress=progress),
              [library_db, summary, profiles, f"every={profile_every}"], [profiles], [cycle_profiles])  # setting is part of the key
    stage("run_metrics", lambda: run_metrics.build_run_metrics(project, workers=workers),
          [library_db, summary, dx / "dx_files.csv"], run_outputs, [run_metrics])
    from . import metric_validation
    stage("validation", lambda: _validation_brief(metric_validation.validate_metrics(project, workers=workers)),
          [library_db, steps_dir], [db / "metric_validation.json"], [metric_validation, derived_cycles])
    stage("research_db", lambda: research_db.build_research_db(project),
          [dx / "dx_files.csv", dx / "dx_tests.csv", db / "design_map.csv", *run_outputs[:2],
           project / "slides" / "slide_test_links.parquet"],
          [db / "research.sqlite3"], [research_db, run_metrics])
    if network:
        from . import design_network
        stage("network", lambda: str(design_network.build_network(project)),
              [db / "research.sqlite3"], [db / "network.html"], [design_network])
    return {"project": str(project), "seconds": round(time.perf_counter() - started, 1), "stages": stages}


def _validation_brief(summary):
    if not isinstance(summary, dict):
        return summary
    return {key: value for key, value in summary.items() if key not in {"worst_datasets"}}


def _brief(value):
    if hasattr(value, "columns") and "status" in getattr(value, "columns", []):
        return value["status"].value_counts().to_dict()
    if isinstance(value, (dict, list, str, int, float)) or value is None:
        return value
    return str(value)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build derived tables, metrics, research DB and design network")
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--no-network", action="store_true")
    parser.add_argument("--force", action="store_true", help="ignore the stage fingerprints and rebuild")
    parser.add_argument("--profile-every", type=int, default=10, help="V-Q profile every N cycles (0 = off)")
    args = parser.parse_args(argv)
    result = build_database(args.project, workers=args.workers, network=not args.no_network, force=args.force,
                            profile_every=args.profile_every)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
