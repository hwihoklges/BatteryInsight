"""Explicit unit mapping and bounded-memory CSV/Parquet ingestion.

source_row is a 1-based data-record number, not a physical line number:
quoted CSV fields may contain newlines. Original CSV bytes stay in place.
"""
from __future__ import annotations

import hashlib
import csv
from pathlib import Path
from collections.abc import Iterator
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

UNIT_FACTORS = {
    "voltage_v": {"V": 1., "mV": .001},
    "current_a": {"A": 1., "mA": .001, "uA": .000001},
    "capacity_ah": {"Ah": 1., "mAh": .001, "uAh": .000001},
    "time_s": {"s": 1., "ms": .001, "min": 60., "h": 3600.},
    "step_time_s": {"s": 1., "ms": .001, "min": 60., "h": 3600.},
    "temperature_c": {"degC": 1.}, "soc_pct": {"%": 1., "fraction": 100.},
}
REQUIRED = {"cycle", "step_id", "time_s", "voltage_v", "current_a"}
TEST_TYPES = {"Cycle", "Insitu", "RPT", "DCIR", "HPPC", "QPEED", "QC", "Formation", "GITT", "EIS", "Storage", "Unknown"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def validate_source(source: dict) -> None:
    for key in ("path", "cell_id", "test_id", "columns", "current_polarity"):
        if key not in source or source[key] is None or source[key] == "":
            raise ValueError(f"source requires explicit {key}")
    if source["current_polarity"] not in {"charge_positive", "discharge_positive", "unsigned_by_step"}:
        raise ValueError("Unknown current_polarity")
    if not isinstance(source.get("include", True), bool):
        raise ValueError("include must be a JSON boolean")
    cols = source["columns"]
    allowed = REQUIRED | set(UNIT_FACTORS) | {"mode", "end_flag", "step_kind", "test_type"}
    unexpected = set(cols) - allowed
    if unexpected:
        raise ValueError(f"Unsupported or reserved column targets: {sorted(unexpected)}")
    if not REQUIRED <= set(cols):
        raise ValueError(f"Missing canonical column mappings: {sorted(REQUIRED - set(cols))}")
    if "test_type" not in source and "test_type" not in cols:
        raise ValueError("Declare test_type or a per-record test_type mapping; never infer from filename")
    if "test_type" in source and source["test_type"] not in TEST_TYPES:
        raise ValueError(f"Unsupported test_type: {source['test_type']}")
    if source["current_polarity"] == "unsigned_by_step" and "step_kind" not in cols:
        raise ValueError("Unsigned currents require step_kind with explicit value_map")
    for target, mapping in cols.items():
        if not isinstance(mapping, dict) or not mapping.get("column"):
            raise ValueError(f"{target} mapping requires column")
        if target in UNIT_FACTORS and mapping.get("unit") not in UNIT_FACTORS[target]:
            raise ValueError(f"Explicit supported unit required for {target}: {list(UNIT_FACTORS[target])}")


def iter_raw(path: Path, source: dict, chunk_rows: int) -> Iterator[pd.DataFrame]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding=source.get("encoding", "utf-8-sig"), newline="") as stream:
            reader = csv.reader(stream, delimiter=source.get("delimiter", ","), strict=True)
            header = next(reader, None)
            if not header or any(not h.strip() for h in header) or len(set(header)) != len(header):
                raise ValueError("CSV requires a nonempty unique header; headerless Restore files are unsupported")
            rows = []
            for record, row in enumerate(reader, 1):
                if len(row) != len(header):
                    raise ValueError(f"Ragged CSV record {record}: expected {len(header)} fields, found {len(row)}")
                rows.append(row)
                if len(rows) == chunk_rows:
                    yield pd.DataFrame(rows, columns=header)
                    rows = []
            if rows:
                yield pd.DataFrame(rows, columns=header)
    elif path.suffix.lower() == ".parquet":
        for batch in pq.ParquetFile(path).iter_batches(batch_size=chunk_rows):
            yield batch.to_pandas()
    else:
        raise ValueError(f"Unsupported raw format {path.suffix}; use verified vendor CSV export")


def _numeric(series: pd.Series, clock: bool = False) -> pd.Series:
    result = pd.to_numeric(series, errors="coerce").astype(float)
    if clock:
        mask = result.isna() & series.astype(str).str.contains(":", regex=False)
        if mask.any():
            result.loc[mask] = pd.to_timedelta(series.loc[mask], errors="coerce").dt.total_seconds()
    return result


class Normalizer:
    def __init__(self, source: dict, source_path: Path, source_hash: str):
        validate_source(source)
        self.source, self.path, self.digest = source, source_path, source_hash
        self.row = 0
        self.segment = 0
        self.previous_key = None
        self.previous_time = None

    def convert(self, raw: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
        raw = raw.reset_index(drop=True)
        out = pd.DataFrame(index=raw.index)
        issues = []
        out["source_row"] = np.arange(self.row + 1, self.row + len(raw) + 1)
        out["source_path"] = str(self.path)
        out["source_hash"] = self.digest
        out["cell_id"], out["test_id"] = str(self.source["cell_id"]), str(self.source["test_id"])
        for target, mapping in self.source["columns"].items():
            column = mapping["column"]
            if column not in raw.columns:
                raise ValueError(f"Missing input column {column!r} in {self.path}")
            values = raw[column]
            if target in UNIT_FACTORS:
                numbers = _numeric(values, clock=target.endswith("time_s"))
                # Clock strings are always h:m:s, so a non-s declaration is ambiguous.
                if target.endswith("time_s") and mapping["unit"] != "s" and values.astype(str).str.contains(":", regex=False).any():
                    raise ValueError("Clock strings require unit=s")
                out[target] = numbers * UNIT_FACTORS[target][mapping["unit"]]
            elif target == "cycle":
                n = pd.to_numeric(values, errors="coerce")
                if n.isna().any() or not np.isfinite(n).all() or ((n % 1) != 0).any() or (n < 0).any():
                    raise ValueError("cycle must contain non-negative explicit integers")
                out[target] = n.astype("int64")
            else:
                if values.isna().any() and target in {"step_id", "test_type", "step_kind"}:
                    raise ValueError(f"Missing required labels: {target}")
                text = values.fillna("").astype(str).str.strip()
                if mapping.get("value_map") is not None:
                    vm = {str(k): str(v) for k, v in mapping["value_map"].items()}
                    unmapped = set(text) - set(vm)
                    if unmapped:
                        raise ValueError(f"Unmapped {target} values: {sorted(unmapped)[:8]}")
                    text = text.map(vm)
                out[target] = text
        if "test_type" not in out:
            out["test_type"] = self.source["test_type"]
        if not set(out["test_type"]) <= TEST_TYPES:
            raise ValueError("Unknown test_type labels: provide value_map")
        if (out["step_id"] == "").any():
            raise ValueError("step_id cannot be empty")
        for optional in ("capacity_ah", "step_time_s", "temperature_c", "soc_pct"):
            if optional not in out:
                out[optional] = np.nan
        for optional, default in (("mode", "unknown"), ("end_flag", "")):
            if optional not in out:
                out[optional] = default
        polarity = self.source["current_polarity"]
        if polarity == "discharge_positive":
            out["current_a"] = -out["current_a"]
        elif polarity == "unsigned_by_step":
            signs = out["step_kind"].map({"charge": 1., "discharge": -1., "rest": 0.})
            if signs.isna().any():
                raise ValueError("step_kind value_map must emit charge/discharge/rest")
            if ((signs == 0) & (out["current_a"].abs() > self.source.get("rest_current_max_a", 1e-6))).any():
                raise ValueError("Nonzero current labelled rest: resolve polarity/classification")
            out["current_a"] = out["current_a"].abs() * signs
        for target in ("time_s", "voltage_v", "current_a"):
            invalid = ~np.isfinite(out[target])
            if invalid.any():
                issues.append(self.issue("invalid_numeric", "error", f"{target}: {int(invalid.sum())} nonfinite rows", int(out.loc[invalid, "source_row"].iloc[0])))
        times = out["time_s"].to_numpy(dtype=float)
        if len(times):
            delta = np.diff(np.r_[self.previous_time if self.previous_time is not None else np.nan, times])
            bad_clock = np.flatnonzero(np.isfinite(delta) & (delta <= 0))
            for index in bad_clock:
                issue = self.issue("nonadvancing_total_time", "error", "Total time reset/duplicate; source order retained, no cross-cycle integration", int(out.source_row.iloc[index]))
                issue["cycle"] = int(out.cycle.iloc[index])
                issues.append(issue)
            self.previous_time = times[-1]
        outside = out["soc_pct"].notna() & ~out["soc_pct"].between(0, 101)
        if outside.any():
            issues.append(self.issue("soc_out_of_range", "warning", f"{int(outside.sum())} records outside 0..101%", int(out.loc[outside, "source_row"].iloc[0])))
        # Segment IDs refer to contiguous physical occurrences, not just StepNo.
        segment_ids = []
        for cycle, step, typ, mode in zip(out.cycle, out.step_id, out.test_type, out["mode"]):
            key = (cycle, step, typ, mode)
            if key != self.previous_key:
                self.segment += 1
                self.previous_key = key
            segment_ids.append(self.segment)
        out["segment_id"] = segment_ids
        self.row += len(raw)
        return out, issues

    def issue(self, code: str, severity: str, detail: str, source_row: int) -> dict:
        return dict(test_id=self.source["test_id"], cell_id=self.source["cell_id"], cycle=None,
                    code=code, severity=severity, detail=detail, source_row=source_row,
                    source_path=str(self.path), source_hash=self.digest)


def complete_cycle_chunks(chunks: Iterator[pd.DataFrame], max_cycle_rows: int = 2_000_000) -> Iterator[pd.DataFrame]:
    """Retain just the unfinished cycle across CSV record batches.

    Non-contiguous/restarted cycle numbering is rejected; such tests must be
    separated in the manifest instead of silently merging different runs.
    """
    carry = None
    previous_cycle = None
    for chunk in chunks:
        if chunk.empty:
            continue
        if (chunk.cycle.diff().dropna() < 0).any() or (previous_cycle is not None and chunk.cycle.iloc[0] < previous_cycle):
            raise ValueError("cycle counter regressed; split repeated test runs before ingestion")
        previous_cycle = chunk.cycle.iloc[-1]
        data = pd.concat([carry, chunk], ignore_index=True) if carry is not None else chunk
        last_cycle = data.cycle.iloc[-1]
        for _, group in data.loc[data.cycle != last_cycle].groupby("cycle", sort=False):
            if len(group) > max_cycle_rows:
                raise ValueError("max_cycle_rows exceeded; adjust explicitly or split input")
            yield group.reset_index(drop=True)
        carry = data.loc[data.cycle == last_cycle].copy()
        if len(carry) > max_cycle_rows:
            raise ValueError("max_cycle_rows exceeded; adjust explicitly or split input")
    if carry is not None and not carry.empty:
        yield carry.reset_index(drop=True)
