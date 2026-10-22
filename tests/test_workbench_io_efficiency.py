"""Bounded request-local reuse must retain artifact change detection."""
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import pytest

from battery_insight import database_workbench, metric_packs, workbench_data
from test_database_workbench import database_fixture
from test_workbench_data import measured_run, seal_run


def test_database_refresh_reads_each_cycle_table_once_even_on_cold_cache(tmp_path, monkeypatch):
    config, _, output, rows = database_fixture(tmp_path)
    actual = pd.read_parquet
    reads = []

    def counted(path, *args, **kwargs):
        reads.append(Path(path))
        return actual(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", counted)
    first = database_workbench.build_database_bundle(config, output)
    assert len([path for path in reads if path.name == "cycles.parquet"]) == len(rows)
    reads.clear()
    second = database_workbench.build_database_bundle(config, output)
    assert first["observations"] == second["observations"]
    assert len([path for path in reads if path.name == "cycles.parquet"]) == len(rows)


def test_code_identity_is_hashed_once_per_file_per_refresh(tmp_path, monkeypatch):
    config, _, output, _ = database_fixture(tmp_path)
    actual = database_workbench.sha256_file
    counts = {"workbench_data.py": 0, "metric_packs.py": 0}

    def counted(path):
        name = Path(path).name
        if name in counts:
            counts[name] += 1
        return actual(path)

    for module in (database_workbench, metric_packs, workbench_data):
        monkeypatch.setattr(module, "sha256_file", counted)
    database_workbench.build_database_bundle(config, output)
    assert counts == {"workbench_data.py": 1, "metric_packs.py": 1}
    database_workbench.build_database_bundle(config, output)
    assert counts == {"workbench_data.py": 2, "metric_packs.py": 2}


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


def test_next_refresh_rejects_changed_cycles_despite_request_local_reuse(tmp_path):
    config, _, output, rows = database_fixture(tmp_path)
    database_workbench.build_database_bundle(config, output)
    path = Path(rows[0]["run_path"]) / "gold" / "cycles.parquet"
    frame = pd.read_parquet(path)
    frame.loc[0, "charge_mean_voltage_v"] = 99
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match="modified"):
        database_workbench.build_database_bundle(config, output)
