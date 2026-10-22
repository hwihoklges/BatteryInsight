import pandas as pd
import pytest
from battery_insight.ingest import iter_raw, Normalizer
from test_ingest import source


@pytest.mark.parametrize("text", ["a,b\n1,2,3\n", "a,b\n1\n", "a,a\n1,2\n", "a,\n1,2\n"])
def test_csv_never_silently_discards_fields(tmp_path, text):
    p = tmp_path / "x.csv"
    p.write_text(text)
    with pytest.raises(ValueError):
        list(iter_raw(p, {}, 10))


def test_quoted_newline_is_one_data_record(tmp_path):
    p = tmp_path / "x.csv"
    p.write_bytes(b'a,b\n1,"two\nlines"\n2,ok\n')
    chunks = list(iter_raw(p, {}, 1))
    assert len(chunks) == 2
    assert chunks[0].iloc[0].b == "two\nlines"


def test_total_time_reset_across_chunk_cycle_boundary(tmp_path):
    normalizer = Normalizer(source(), tmp_path / "x.csv", "abc")
    normalizer.convert(pd.DataFrame({"cy": [1, 1], "st": [1, 1], "t": [0, 1000], "v": [3000, 3000], "i": [1000, 1000]}))
    _, issues = normalizer.convert(pd.DataFrame({"cy": [2, 2], "st": [1, 1], "t": [0, 1000], "v": [3000, 3000], "i": [1000, 1000]}))
    found = [x for x in issues if x["code"] == "nonadvancing_total_time"]
    assert found[0]["source_row"] == 3
    assert found[0]["cycle"] == 2
