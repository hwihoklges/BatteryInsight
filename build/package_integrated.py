"""Ship one integrated Windows package and refresh the complete source archive."""
from pathlib import Path
import hashlib
import json
import shutil
import uuid
import xml.etree.ElementTree as ET
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
ROOT = PROJECT.parents[1]
OUT = PROJECT.parent


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def main():
    from PyInstaller.archive.readers import CArchiveReader
    exe = PROJECT / "dist/BatteryInsight.exe"
    frozen = json.loads((ROOT / "work/traceability_321_frozen_qa/report.json").read_text(encoding="utf-8"))
    browser = json.loads((ROOT / "work/integrated_browser_qa/report.json").read_text(encoding="utf-8"))
    audit = json.loads((PROJECT / "build/runtime_audit.json").read_text(encoding="utf-8"))
    if any(report["status"] != "passed" for report in (frozen, browser, audit)):
        raise SystemExit("Frozen, browser and dependency checks must pass before release.")
    if sha(exe) != frozen["exe_sha256"]:
        raise SystemExit("The executable changed after frozen QA.")
    suite = ET.parse(ROOT / "work/traceability_321_tests.xml").getroot().find("testsuite")
    if suite is None or int(suite.attrib["errors"]) or int(suite.attrib["failures"]) or int(suite.attrib["skipped"]):
        raise SystemExit("All current related tests must pass.")
    baseline = json.loads((ROOT / "work/traceability_320_validation.json").read_text(encoding="utf-8"))
    if baseline["version"] != "3.2.0" or baseline["tests"]["regression"]["failures"] != "0":
        raise SystemExit("The previous release regression baseline is invalid.")
    suites = {"current_related": {key: suite.attrib[key] for key in ("tests", "errors", "failures", "skipped")},
              "previous_release_regression": {"version": baseline["version"],
                  "scope": "3.2.0 baseline before removing dedicated WIP presentation/demo; not a new full-suite run",
                  "results": baseline["tests"]["regression"]}}
    archive = CArchiveReader(str(exe))
    entries = {name.replace("\\", "/"): name for name in archive.toc}
    checked = {}
    paths = list((PROJECT / "battery_insight").glob("*.py"))
    paths += list((PROJECT / "battery_insight/native_cyc").glob("*.py"))
    paths += [path for directory in ("assets", "resources") for path in (PROJECT / "battery_insight" / directory).rglob("*") if path.is_file()]
    paths += [PROJECT / "THIRD_PARTY_NOTICES.txt"]
    for path in paths:
        name = path.relative_to(PROJECT).as_posix()
        checked[name] = hashlib.sha256(archive.extract(entries[name])).hexdigest() == sha(path)
    if not all(checked.values()):
        raise SystemExit("Embedded code/assets/notices differ from released source.")
    trace_browser = json.loads((ROOT / "work/traceability_321_browser_qa/report.json").read_text(encoding="utf-8"))
    if trace_browser["status"] != "passed":
        raise SystemExit("Traceability browser checks must pass.")
    compatibility = json.loads((ROOT / "work/traceability_321_compatibility.json").read_text(encoding="utf-8"))
    if not compatibility["all_existing_edges_readable"] or not compatibility["raw_links_valid"]:
        raise SystemExit("Existing reviewed project links must remain readable.")
    validation = {"application": "BatteryInsight", "version": "3.2.1", "exe_sha256": sha(exe), "exe_bytes": exe.stat().st_size,
        "tests": suites, "frozen_execution": {"status": frozen["status"], "scope": frozen["scope"], "summary": frozen["summary"],
            "selftest_checks": frozen["selftest_checks"], "gui_checks": frozen["gui_checks"], "checks": frozen["checks"]},
        "existing_comparison_browser": browser, "traceability_browser": trace_browser, "existing_project_compatibility": compatibility,
        "runtime_audit": audit, "embedded_artifacts_verified": len(checked),
        "build": json.loads((PROJECT / "build/build_environment.json").read_text(encoding="utf-8")),
        "scope": ["Single offline Windows x64 executable; no Python installation/API key required",
            "Native CYC 4100/4101 -> all-record Parquet -> catalogue -> reviewed classification -> document links -> existing workbench",
            "Project-local converted Parquet paths are relative; external folders remain references",
            "No LLM calls or ML training; official AIM formulas remain unconfigured unless verified export supplied",
            "Native units require confirmation before SI profile metrics; current/CC/CV-dependent metrics require richer inputs",
            "Current PPT/process/raw workflow verified on synthetic examples; no actual manufacturing PPT supplied",
            "No dedicated WIP before/after classification; generic process/cell/test links and identity/date checks retained",
            "Local LLM/RAG integration is a documented future plan, not implemented or bundled",
            "Archived PPT evidence, field-level citations, revision-bound edges and review states retained"]}
    patch_qa = json.loads((ROOT / "work/cyc_4101_patch_qa/report.json").read_text(encoding="utf-8"))
    if patch_qa["status"] != "passed":
        raise SystemExit("Actual 4101 source QA must pass.")
    # The unchanged native reader/service were fully checked on actual 4101
    # records in 3.1. Preserve that scoped baseline rather than call it current
    # manufacturing-PPT evidence.
    source_manifest = next((ROOT / "work/cyc_4101_patch_qa/project/converted").glob("*/conversion_manifest.json"))
    baseline_manifest = json.loads(source_manifest.read_text(encoding="utf-8"))
    baseline = baseline_manifest["files"][0]
    native_code = hashlib.sha256()
    for name in ("reader.py", "service.py"):
        native_code.update(name.encode())
        native_code.update((PROJECT / "battery_insight/native_cyc" / name).read_bytes())
    binding = {"cyc_sha256": patch_qa["input_sha256"], "cts_sha256": None, "code_sha256": native_code.hexdigest(), "storage": baseline_manifest["storage"]}
    if hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest() != baseline["fingerprint"]:
        raise SystemExit("Native code changed; actual CYC baseline must be repeated.")
    validation["unchanged_native_4101_engine_baseline"] = {key: patch_qa[key] for key in (
        "input_sha256", "source_unmodified", "rows", "all_values_exact", "parquet_values_exact", "library_registration", "reused", "legacy_files", "legacy_rows")}
    (PROJECT / "INTEGRATED_VALIDATION.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    (PROJECT / "SHA256.txt").write_text(sha(exe) + "  BatteryInsight.exe\n", encoding="utf-8")
    shutil.copyfile(PROJECT / "battery_insight/resources/통합사용안내.html", PROJECT / "통합사용안내.html")
    notices = json.loads((PROJECT / "build/third_party_manifest.json").read_text(encoding="utf-8"))
    (PROJECT / "requirements-integrated-build.txt").write_text("# Build computer only. Runtime users run the EXE.\n" +
        "\n".join(item["name"] + "==" + item["version"] for item in notices["distributions"]) + "\n", encoding="utf-8")
    # The source ZIP contains application code, tests, docs and synthetic demos;
    # never build work directories, the EXE, or actual user measurement files.
    source_zip = OUT / "battery_insight.zip"
    temporary = source_zip.with_name(source_zip.name + "." + uuid.uuid4().hex + ".tmp")
    source_files = [path for path in PROJECT.rglob("*") if path.is_file()
        and not any(part in {"__pycache__", ".pytest_cache", "dist", "pyinstaller", ".git"} for part in path.relative_to(PROJECT).parts)
        and path.suffix not in {".pyc", ".cyc", ".cts", ".exe"}
        and path.name != "release_manifest.json"]
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(source_files):
            bundle.write(path, "battery_insight/" + path.relative_to(PROJECT).as_posix())
    temporary.replace(source_zip)
    runtime_zip = OUT / "BatteryInsight_Integrated_Windows_x64.zip"
    temporary = runtime_zip.with_name(runtime_zip.name + "." + uuid.uuid4().hex + ".tmp")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        bundle.write(exe, exe.name)
        for name in ("바로실행.cmd", "통합판_시작.txt", "통합사용안내.html", "TRACEABILITY.md", "THIRD_PARTY_NOTICES.txt", "SHA256.txt"):
            bundle.write(PROJECT / name, name)
        bundle.write(PROJECT / "INTEGRATED_VALIDATION.json", "VALIDATION.json")
        bundle.write(source_zip, "source/BatteryInsight_source.zip")
        bundle.write(PROJECT / "examples/library_external_features.template.json", "examples/library_external_features.template.json")
        bundle.write(PROJECT / "examples/library_rules.template.json", "examples/library_rules.template.json")
    temporary.replace(runtime_zip)
    for path in (source_zip, runtime_zip):
        with zipfile.ZipFile(path) as bundle:
            if bundle.testzip() is not None:
                raise SystemExit("ZIP integrity check failed")
    manifest = {path.name: {"bytes": path.stat().st_size, "sha256": sha(path)} for path in (exe, runtime_zip, source_zip)}
    (PROJECT / "release_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
