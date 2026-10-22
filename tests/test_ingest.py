from pathlib import Path
import numpy as np
import pandas as pd
import pytest
from battery_insight.ingest import Normalizer, complete_cycle_chunks, validate_source


def source():
    return dict(path="x.csv", cell_id="a", test_id="t", test_type="Cycle", current_polarity="charge_positive",
                columns={"cycle": {"column": "cy"}, "step_id": {"column": "st"},
                         "time_s": {"column": "t", "unit": "ms"}, "voltage_v": {"column": "v", "unit": "mV"},
                         "current_a": {"column": "i", "unit": "mA"}})


def test_explicit_units_and_segment_continuity():
    s = source()
    n = Normalizer(s, Path("x.csv"), "abc")
    a, _ = n.convert(pd.DataFrame({"cy": [1, 1], "st": [1, 1], "t": [0, 1000], "v": [3500, 3500], "i": [1000, 1000]}))
    b, _ = n.convert(pd.DataFrame({"cy": [1, 1], "st": [1, 2], "t": [2000, 3000], "v": [3500, 3500], "i": [1000, -1000]}))
    assert a.current_a.tolist() == [1, 1]
    assert a.voltage_v.tolist() == [3.5, 3.5]
    assert b.time_s.tolist() == [2, 3]
    assert b.source_row.tolist() == [3, 4]
    assert b.segment_id.tolist() == [1, 2]


def test_unknown_units_and_unsigned_ambiguity_rejected():
    s = source()
    s["columns"]["current_a"].pop("unit")
    with pytest.raises(ValueError, match="unit"):
        validate_source(s)
    s = source()
    s["current_polarity"] = "unsigned_by_step"
    with pytest.raises(ValueError, match="step_kind"):
        validate_source(s)


def test_cycle_buffer_split_boundary_and_regression():
    chunks = [pd.DataFrame({"cycle": [1, 1, 2], "x": [1, 2, 3]}), pd.DataFrame({"cycle": [2, 3], "x": [4, 5]})]
    result = list(complete_cycle_chunks(iter(chunks)))
    assert [x.x.tolist() for x in result] == [[1, 2], [3, 4], [5]]
    with pytest.raises(ValueError, match="regressed"):
        list(complete_cycle_chunks(iter([pd.DataFrame({"cycle": [1, 2, 1]})])))


def test_bounded_cycle_buffer():
    with pytest.raises(ValueError, match="max_cycle_rows"):
        list(complete_cycle_chunks(iter([pd.DataFrame({"cycle": [1] * 4})]), max_cycle_rows=3))
