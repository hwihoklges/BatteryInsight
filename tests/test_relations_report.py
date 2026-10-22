import json
import re

import pytest

from battery_insight.relations_report import write_relations_report


def payload():
    return {"schema_version": "experiment_relations_v1", "nodes": [], "edges": [], "metrics": [],
            "cohorts": [], "metric_correlations": [], "excluded_pairs": [], "settings": {},
            "metadata": {}, "is_demo": True, "generated_at": "2026-10-21T00:00:00Z"}


def test_standalone_report_escapes_script_termination_and_roundtrips(tmp_path):
    data = payload()
    hostile = '</script><script>globalThis.bad=true</script>\u2028'
    data["metadata"]["label"] = hostile
    page = write_relations_report(data, tmp_path)
    text = page.read_text(encoding="utf-8")
    embedded = re.search(r'<script id="relations-data" type="application/json">(.*?)</script>', text, re.S).group(1)
    assert json.loads(embedded) == data
    assert hostile not in text
    assert json.loads((tmp_path / "data.json").read_text(encoding="utf-8")) == data
    assert '__RELATIONS_' not in text
    assert 'src="http' not in text and '<link' not in text
    assert 'innerHTML' not in text
    assert not list(tmp_path.glob('*.tmp'))


def test_nonfinite_payload_does_not_overwrite_previous_report(tmp_path):
    data = payload()
    page = write_relations_report(data, tmp_path)
    before = page.read_bytes()
    data["metadata"]["bad_number"] = float('nan')
    with pytest.raises(ValueError):
        write_relations_report(data, tmp_path)
    assert page.read_bytes() == before
    assert json.loads((tmp_path / "data.json").read_text(encoding="utf-8")) == payload()
