"""Bounded request-local reuse must retain artifact change detection."""
import pandas as pd
import pyarrow.parquet as pq
import pytest

from battery_insight import metric_packs
from test_workbench_data import measured_run, seal_run


def test_voltage_only_cv_checks_schema_without_decoding_rows_and_still_verifies_hash(tmp_path, monkeypatch):
    _, run, row, _, silver = measured_run(tmp_path)
    frame = pd.read_parquet(silver).drop(columns=["current_a", "mode"])
    frame.to_parquet(silver, index=False)
    seal_run(run)

    def forbidden(*args, **kwargs):
        pytest.fail("No row decoding is needed when the CV signals are not retained")

    monkeypatch.setattr(pq.ParquetFile, "iter_batches", forbidden)
    assert metric_packs._cv_durations(run, row) == {}
    with silver.open("ab") as stream:
        stream.write(b"modified")
    with pytest.raises(ValueError, match="modified"):
        metric_packs._cv_durations(run, row)
