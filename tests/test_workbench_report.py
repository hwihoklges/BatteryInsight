"""Untrusted experiment labels must remain data inside the standalone report."""
import json
import re

import pytest

from battery_insight.workbench_report import write_workbench_report


def test_embedded_user_text_cannot_close_script(tmp_path):
    label = '</script><script>alert("raw label")</script>\u2028\u2029한글 __ANALYSIS__ __JS__'
    bundle = {"experiments": [{"experiment_id": "001", "label": label}]}
    path = write_workbench_report(bundle, {"options": {}}, tmp_path)
    html = path.read_text(encoding="utf-8")
    embedded = re.search(r'<script id="bundle-data" type="application/json">(.*?)</script>', html, re.S).group(1)
    assert "<" not in embedded
    assert "\u2028" not in embedded and "\u2029" not in embedded
    assert json.loads(embedded) == bundle
    assert label not in html
    assert json.loads((tmp_path / "data.json").read_text(encoding="utf-8")) == bundle
    assert '<script src=' not in html and '<link rel="stylesheet"' not in html


@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), -float("inf")])
def test_invalid_number_does_not_replace_previous_report(tmp_path, invalid):
    write_workbench_report({"experiments": []}, {"options": {}}, tmp_path)
    previous = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError):
        write_workbench_report({"experiments": []}, {"result": invalid}, tmp_path)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == previous
