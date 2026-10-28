"""Files already recorded by an earlier conversion are not converted again."""
import shutil

from battery_insight import library
from cyc2parquet.selftest import fixture_folder
from cyc2parquet.service import convert_folder


def test_records_imported_under_sources_prevent_reconversion(tmp_path):
    raw, project = tmp_path / "cycler/data/세트1", tmp_path / "project"
    fixture_folder(raw)
    convert_folder(raw, tmp_path / "elsewhere/세트1_parquet")
    imported = project / "sources/PNE99/모음집/세트1_parquet"
    shutil.copytree(tmp_path / "elsewhere/세트1_parquet", imported)
    library.scan_folder(project, project / "sources/PNE99")
    before = library.library_rows(project)
    assert len(before) == 3

    result = library.convert_and_register(project, raw)
    summary = result["conversion"]["summary"]
    assert result["status"] == "success"
    assert summary["existing"] == 3 and summary["complete"] == 0
    assert not list((project / "converted").rglob("*.parquet"))
    assert result["existing_registered"] == 0
    assert len(library.library_rows(project)) == 3

    with (raw / "sample.cyc").open("ab") as stream:
        stream.write(bytes(120))
    grown = library.convert_and_register(project, raw)
    decisions = {item["source"]: item["decision"]["reason"] for item in grown["conversion"]["files"]}
    assert decisions["sample.cyc"] == "record_source_size_changed"
    assert grown["conversion"]["summary"]["complete"] == 1 and grown["conversion"]["summary"]["existing"] == 2


def test_existing_record_outside_registered_roots_is_registered_once(tmp_path):
    raw, project = tmp_path / "raw", tmp_path / "project"
    fixture_folder(raw)
    outside = project / "converted/manual_output"
    convert_folder(raw, outside)
    result = library.convert_and_register(project, raw)
    assert result["conversion"]["summary"]["existing"] == 3
    assert result["existing_registered"] == 1
    assert len(library.library_rows(project)) == 3
    again = library.convert_and_register(project, raw)
    assert again["existing_registered"] == 0 and len(library.library_rows(project)) == 3


def test_project_conversion_diagnosis_report(tmp_path):
    raw, project = tmp_path / "raw", tmp_path / "project"
    fixture_folder(raw)
    (raw / "broken.cyc").write_bytes((raw / "sample.cyc").read_bytes()[:-5])
    library.convert_and_register(project, raw)
    result = library.diagnose_conversions(project)
    assert result["summary"]["open_failures"] == 1
    assert result["summary"]["failure_codes_open"] == {"truncated_records": 1}
    assert (project / "reports/conversion_diagnosis/diagnosis.html").is_file()
