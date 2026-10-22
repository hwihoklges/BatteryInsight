"""Static DLL closure and licence coverage audit for the local frozen build."""
from pathlib import Path
import ast
import hashlib
import importlib.util
import json
import os
import pefile

project = Path(__file__).resolve().parents[1]
analysis = project / "build/pyinstaller/integrated/Analysis-00.toc"
def entries(value):
    if isinstance(value, (list, tuple)):
        if len(value) == 3 and all(isinstance(x, str) for x in value) and value[2] in {"BINARY", "EXTENSION", "DATA", "PYMODULE", "PYSOURCE"}:
            yield value
        else:
            for item in value:
                yield from entries(item)
rows = list(entries(ast.literal_eval(analysis.read_text(encoding="utf-8"))))
binaries = [row for row in rows if row[2] in {"BINARY", "EXTENSION"}]
names = {Path(row[0].replace("\\", "/")).name.casefold() for row in binaries}
system = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
missing, external_vc, references = set(), set(), 0
for name, source, kind in binaries:
    try:
        pe = pefile.PE(source, fast_load=True)
    except pefile.PEFormatError:
        continue
    pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_IMPORT'], pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT']])
    for entry in list(getattr(pe, "DIRECTORY_ENTRY_IMPORT", [])) + list(getattr(pe, "DIRECTORY_ENTRY_DELAY_IMPORT", [])):
        dll = entry.dll.decode("ascii").casefold()
        references += 1
        if dll in names or dll.startswith(("api-ms-win-", "ext-ms-win-")):
            continue
        if dll.startswith(("vcruntime", "msvcp", "concrt", "vcomp")):
            external_vc.add(dll)
        if not (system / dll).is_file():
            missing.add(dll)
    pe.close()
spec = importlib.util.spec_from_file_location("integrated_notices", project / "build/collect_notices.py")
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)
mapping, unresolved = collector.resolved_distributions(analysis)
notice = json.loads((project / "build/third_party_manifest.json").read_text(encoding="utf-8"))
covered = {row["name"].casefold() for row in notice["distributions"]}
used = {name for values in mapping.values() for name in values}
uncovered = sorted(name for name in used if name.casefold() not in covered)
modules = [row[0] for row in rows if row[2] == "PYMODULE"]
excluded_present = [name for name in modules if name.split('.')[0] in {"torch", "tensorflow", "sklearn", "xgboost", "lightgbm"}]
report = {"status": "passed" if not (missing or external_vc or unresolved or uncovered or excluded_present) else "failed",
    "scope": "Static Analysis TOC and PE imports; Windows system DLLs excluded. Runtime tested separately.",
    "analysis_sha256": hashlib.sha256(analysis.read_bytes()).hexdigest(),
    "binary_files": len(binaries), "python_modules": len(modules), "dll_references": references,
    "missing_non_os_dlls": sorted(missing), "external_vc_runtime": sorted(external_vc),
    "runtime_distributions": sorted(used), "uncovered_licences": uncovered, "unresolved_distributions": unresolved,
    "ml_modules_present": excluded_present}
(project / "build/runtime_audit.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False))
