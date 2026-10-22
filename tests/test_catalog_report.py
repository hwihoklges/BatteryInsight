import json
import pandas as pd
import pytest
from battery_insight.catalog_report import write_catalog


def test_catalog_preserves_all_rows_and_escapes_embedded_script(tmp_path):
    data = pd.DataFrame([dict(namespace="cyclediag", metric_id="cyclediag.CE", group="효율", unit="%", unit_status="verified_source",
                             description_ko="test </script><script>alert(1)</script>", is_metadata=False)])
    manifest = write_catalog(data, tmp_path)
    page = (tmp_path / "metrics_catalog.html").read_text(encoding="utf-8")
    assert "test </script><script>" not in page
    assert "\\u003c/script>" in page
    loaded = pd.read_csv(tmp_path / "metrics_catalog.csv", encoding="utf-8-sig")
    assert loaded.description_ko.iloc[0] == data.description_ko.iloc[0]
    assert manifest["total_rows"] == 1
    assert not manifest["raw_data_analysis_executed"]


def test_catalog_never_hides_duplicate_metric_identities(tmp_path):
    data = pd.DataFrame([dict(namespace="x", metric_id="x.q")] * 2)
    with pytest.raises(ValueError, match="duplicate"):
        write_catalog(data, tmp_path)
