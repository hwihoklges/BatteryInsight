"""Offline end-to-end checks for the integrated application, using synthetic data."""
from pathlib import Path
import json
import shutil
import tempfile
import sys


def run_self_test():
    from . import library
    from .native_cyc.selftest import fixture_folder
    from .library_workbench import workbench_config
    from .workbench_server import Workbench, make_server
    from .knowledge_registry import KnowledgeRegistry
    import openpyxl
    checks = {}
    with tempfile.TemporaryDirectory(prefix="batteryinsight_integrated_") as temporary:
        base = Path(temporary)
        raw, project = base / "원본", base / "프로젝트"
        fixture_folder(raw)
        (raw / "repeat.cyc").write_bytes((raw / "sample.cyc").read_bytes())
        (raw / "repeat.cts").write_bytes((raw / "sample.cts").read_bytes())
        original = {str(path): library.file_hash(path) for path in raw.rglob("*") if path.is_file()}
        imported = library.convert_and_register(project, raw)
        rows = library.library_rows(project)
        checks["native_conversion_and_auto_registration"] = imported["status"] == "success" and len(rows) == 4
        checks["all_nineteen_records_preserved"] = sum(row["rows"] for row in rows) == 19
        checks["filename_hints_not_confirmed"] = all(row["review_status"] == "unclassified" for row in rows)
        wb = Workbench(workbench_config(project), project / "workbench")
        checks["unconfirmed_units_not_scaled"] = all(item["value"] is None for item in wb.bundle["observations"] if item["unit"] in {"V", "Ah"})
        main = next(row for row in rows if row["relative_path"] == "sample.parquet")
        evidence = wb.evidence(main["dataset_id"])
        checks["native_values_view_available"] = evidence["native_view"] and evidence["curves"][0]["points"][0]["voltage_native"] == 3100.125
        for row in rows:
            if row["relative_path"] in {"sample.parquet", "repeat.parquet"}:
                library.assign_metadata(project, row["dataset_id"], {"physical_cell_id": "cell-A", "sample_name": "sample-A",
                    "design_id": "design-A", "batch_id": "batch-A", "test_type": "Cycle", "protocol": "standard", "chemistry": "synthetic", "temperature_c": 25,
                    "voltage_unit": "mV", "capacity_unit": "uAh", "test_date": "2026-10-22", "date_kind": "test_start", "namespace": "synthetic", "set_no": "001"})
        wb.refresh()
        checks["unit_confirmed_profile_metrics"] = any(item["value"] is not None for item in wb.bundle["observations"] if item["unit"] == "V")
        compared = wb.compare({**wb.options, "enabled_modules": ["replicates", "evidence"], "summary": "snapshot", "life_min": 1, "life_max": 1,
                               "windows": [{"label": "snapshot", "life_min": 1, "life_max": 1}], "min_points": 1})
        means = wb.analysis["replicates"]
        checks["same_physical_cell_not_double_counted"] = bool(means) and all(item.get("n_cells", item.get("cell_count", 1)) == 1 for item in means)
        checks["modules_can_be_disabled"] = wb.analysis["options"]["enabled_modules"] == ["replicates", "evidence"]
        template = library.export_registration(project, base / "registration.csv")
        checks["registration_roundtrip"] = library.import_registration(project, template)["confirmed"] == 2
        library.set_rules(project, [{"rule_id": "reviewed-backup", "path_glob": "sample_org.parquet", "metadata": {"test_type": "Cycle"}}])
        checks["reviewed_rule_classification"] = len(library.apply_rules(project)["applied"]) == 1
        document = base / "실험장표.xlsx"
        book = openpyxl.Workbook()
        book.active.append(["sample-A", "2026-10-22", "001", "synthetic"])
        book.save(document)
        book.close()
        library.import_documents(project, [document])
        with KnowledgeRegistry(project / "knowledge/knowledge.sqlite3") as registry:
            evidence_ids = [item["evidence_id"] for item in registry.list_evidence()]
            record = registry.upsert_records([{"status": "confirmed", "sample_id": "sample-A", "sample_name": "sample-A", "experiment_name": "synthetic experiment",
                "test_date": "2026-10-22", "date_kind": "test_start", "namespace": "synthetic", "set_no": "001", "test_type": "Cycle", "evidence_ids": evidence_ids}], reviewer="self-test", reason="synthetic document")[0]
        checks["document_candidates_from_reviewed_keys"] = len(library.knowledge_candidates(project, main["dataset_id"])) == 1
        library.link_record(project, main["dataset_id"], record["record_id"])
        checks["document_link_confirmed"] = library.validate_links(project)[0]["valid"]
        with KnowledgeRegistry(project / "knowledge/knowledge.sqlite3") as registry:
            registry.upsert_records([{"record_id": record["record_id"], "experiment_name": "revised"}], reviewer="self-test", reason="revision check")
        checks["document_revision_invalidates_link"] = not library.validate_links(project)[0]["valid"]
        checks["stale_document_link_requires_review"] = library.get_dataset(project, main["dataset_id"])["review_status"] == "needs_review"
        repeat = library.convert_and_register(project, raw)
        checks["repeat_reuses_all_native_files"] = repeat["conversion"]["summary"]["reused"] == 4
        checks["source_unchanged"] = all(library.file_hash(path) == value for path, value in original.items())
        # The project-local Parquet paths remain resolvable after a project copy.
        moved = base / "copied-project"
        shutil.copytree(project, moved)
        checks["project_local_data_portable"] = all(Path(row["path"]).is_relative_to(moved) and Path(row["path"]).is_file() for row in library.library_rows(moved))
        from .native_cyc.selftest import run_self_test as native_self_test
        checks["4100_and_4101_native_contracts"] = native_self_test()["status"] == "success"
        from .traceability_demo import create_demo
        from .traceability import snapshot as trace_snapshot
        from .traceability_report import build_report
        demo_project = base / "합성 공정 이력"
        create_demo(demo_project)
        lineage = trace_snapshot(demo_project)
        checks["ppt_process_cell_test_raw_lineage"] = len(lineage["entities"]) == 6 and len(lineage["records"]) == 2 and all(item["valid"] for item in lineage["raw_links"])
        checks["independent_cell_test_links"] = len({row["physical_cell_id"] for row in lineage["records"]}) == 2 and \
            all(edge["relation"] != "conditioned_for" for edge in lineage["edges"])
        checks["ppt_archives_are_project_local"] = all(item["proof"]["valid"] and item["proof"]["archived"] for item in lineage["documents"])
        checks["offline_lineage_report"] = build_report(demo_project).is_file()
        from .copilot_cli import prepare
        requests = prepare(demo_project, [lineage["documents"][0]["document_id"]])
        checks["copilot_requests_build_offline_without_cli"] = requests["count"] > 0 and requests["llm_calls"] == 0
    return {"status": "success" if all(checks.values()) else "failed", "checks": checks,
            "runtime": {"frozen": bool(getattr(sys, "frozen", False)), "python": sys.version.split()[0]}, "llm_calls": 0}
