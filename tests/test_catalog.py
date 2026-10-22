import hashlib
import json

import pytest

from battery_insight.catalog import build_catalog


def fixture_sources(tmp_path):
    features = tmp_path / "cyclediag" / "features"
    legacy = tmp_path / "studio" / "db_pipeline" / "lgensol"
    features.mkdir(parents=True)
    legacy.mkdir(parents=True)
    marker = tmp_path / "SOURCE_EXECUTED"
    (features / "lges_catalog.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('should never run')\n"
        "FEATURE_SET_LGES='reviewed-test'\nMAX_DQDV_PEAKS=8\n"
        "RESISTANCE_OFFSETS_S=(0.1,10.0,30.0,60.0)\n"
        "CAPACITY_COLS=['CE','SoHQ','chgCapa_CCratio']\n"
        "DELTA_ABS_COLS=['SoHQ']\nDELTA_PCT_COLS=['EoC_dchgR_10s']\n",
        encoding="utf-8")
    (features / "indicator_registry.py").write_text(
        "import re\n"
        "EXACT_ALIASES={'CI':('CE','CI == 100 - CE')}\n"
        "FAMILIES={'capacity':('SoHQ','capacity')}\n"
        "_META_EXACT=frozenset({'cell_id','cycle'})\n"
        "_TARGET_EXACT=frozenset({'SoHQ','capacity'})\n"
        "_COVARIATE_EXACT=frozenset({'temperature'})\n"
        "_QC_EXACT=frozenset({'quality_score'})\n"
        "_QC_PATTERNS=(re.compile(r'^dcir_fit_'),)\n"
        "_DIAGNOSIS_PATTERNS=(re.compile(r'_score$'),)\n"
        "_DIAGNOSIS_EXEMPT=frozenset({'quality_score'})\n", encoding="utf-8")
    (legacy / "func_feature_extract.py").write_text(
        "class GetFeature:\n"
        "    def __init__(self):\n"
        "        self.val={'cell_id':[], 'chgCapa_CCratio':[], 'EoC_V1':[]}\n"
        "    def optional(self):\n"
        "        self.val['not_initialized'].append(1)\n", encoding="utf-8")
    return features.parent, legacy.parent.parent, marker


def test_complete_generated_peak_and_resistance_names_without_execution(tmp_path):
    cycle, studio, marker = fixture_sources(tmp_path)
    result = build_catalog(cycle, studio)
    assert not marker.exists()
    names = set(result.loc[result.namespace.eq("cyclediag"), "feature_name"])
    assert len([name for name in names if "_peak" in name]) == 64
    assert all(f"{prefix}_{suffix}" in names for prefix in ("EoC_dchgR", "EoD_chgR")
               for suffix in ("0p1s", "10s", "30s", "60s"))
    assert "delta_SoHQ" in names
    assert "EoC_dchgR_10s_inc" in names
    assert not result.metric_id.duplicated().any()


def test_registry_formula_role_family_and_unreviewed_default(tmp_path):
    cycle, studio, _ = fixture_sources(tmp_path)
    result = build_catalog(cycle, studio).set_index("metric_id")
    assert result.loc["cyclediag.CI", "retired_alias_of"] == "CE"
    assert result.loc["cyclediag.CI", "alias_formula"] == "CI == 100 - CE"
    assert result.loc["cyclediag.delta_SoHQ", "role"] == "target"
    assert result.loc["cyclediag.delta_SoHQ", "family"] == "capacity"
    assert result.loc["cyclediag.CE", "role_basis"] == "registry_default_needs_review"
    assert result.loc["cyclediag.CE", "unit"] == "needs_review"
    assert result.loc["cyclediag.CE", "unit_status"] == "needs_review"


def test_source_provenance_and_legacy_optional_references_are_distinct(tmp_path):
    cycle, studio, _ = fixture_sources(tmp_path)
    result = build_catalog(cycle, studio).set_index("metric_id")
    assert result.loc["studio_legacy.cell_id", "is_metadata"]
    assert result.loc["studio_legacy.not_initialized", "origin"] == "legacy_reference"
    assert "legacy_initialized" in result.loc["studio_legacy.chgCapa_CCratio", "origin"]
    assert "cyclediag.chgCapa_CCratio" in result.index
    assert "studio_legacy.chgCapa_CCratio" in result.index
    source = cycle / "features" / "lges_catalog.py"
    row = result.loc["cyclediag.CE"]
    assert row.source_sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert json.loads(row.evidence_json)[0]["line"] > 0
    assert row.required_data and row.validity_notes


def test_header_only_read_does_not_parse_or_hash_raw_records(tmp_path):
    cycle, studio, _ = fixture_sources(tmp_path)
    export = tmp_path / "export.csv"
    export.write_text('cell_id,cycle,CE,unique_export_metric\n"unterminated data record\n', encoding="utf-8")
    first = build_catalog(cycle, studio, [str(export)]).set_index("metric_id")
    assert first.loc["cyclediag.cell_id", "is_metadata"]
    custom = first.loc["cyclediag.unique_export_metric"]
    assert custom.origin == "observed_export_header"
    assert custom.source_hash_scope == "decoded_header_utf8"
    export.write_text('cell_id,cycle,CE,unique_export_metric\ncompletely different data\n', encoding="utf-8")
    second = build_catalog(cycle, studio, [str(export)]).set_index("metric_id")
    assert custom.source_sha256 == second.loc["cyclediag.unique_export_metric", "source_sha256"]
    ce_evidence = json.loads(second.loc["cyclediag.CE", "evidence_json"])
    assert {entry["origin"] for entry in ce_evidence} == {"core_declared", "observed_export_header"}


def test_duplicate_export_header_is_rejected(tmp_path):
    cycle, studio, _ = fixture_sources(tmp_path)
    export = tmp_path / "export.csv"
    export.write_text("CE,CE\n1,2\n")
    with pytest.raises(ValueError, match="invalid export header"):
        build_catalog(cycle, studio, [str(export)])


def test_soc_dataclass_expansion_is_source_declared(tmp_path):
    cycle, studio, _ = fixture_sources(tmp_path)
    (cycle / "features" / "dcir_decompose.py").write_text(
        "class DcirFitResult:\n    R_ct: float = 0\n    dcir_fit_valid: bool = False\n")
    (cycle / "features" / "enrich_assb.py").write_text("_SOC_ORDER=(80,50,20)\n")
    result = build_catalog(cycle, studio).set_index("metric_id")
    assert all(f"cyclediag.R_ct_soc{soc}" in result.index for soc in (80, 50, 20))
    assert result.loc["cyclediag.dcir_fit_valid_soc20", "role"] == "qc"
    assert result.loc["cyclediag.R_ct_soc20", "origin"] == "enrich_dataclass_generated"


def test_changed_source_snapshot_never_claims_verified_units(tmp_path):
    cycle, studio, _ = fixture_sources(tmp_path)
    (cycle / "features" / "lges_extract.py").write_text("# A future revision with unknown units\n")
    result = build_catalog(cycle, studio)
    assert not result.unit_status.eq("verified_source").any()
