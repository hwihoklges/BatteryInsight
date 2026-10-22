"""Explicit retention policies and lossless, bounded-memory Parquet writing.

Retention and compression are distinct: Zstandard does not change measurements;
column/row selection deliberately removes information from the derived database.
Original instrument files are never deleted or rewritten.
"""
from __future__ import annotations

import copy
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


IDENTITY_COLUMNS = ["cell_id", "test_id", "test_type", "source_path", "source_hash",
                    "source_member", "source_member_row", "source_row", "record_index", "container_sha256",
                    "source_member_sha256", "channel_sha256", "source_id"]
CORE_COLUMNS = ["cycle", "step_id", "segment_id", "time_s", "step_time_s", "voltage_v",
                "current_a", "capacity_ah", "mode", "end_flag", "step_kind"]
VOLTAGE_PROFILE_COLUMNS = ["cycle", "step_id", "segment_id", "time_s", "step_time_s",
                           "voltage_v", "capacity_ah", "step_kind"]
OPTIONAL_COLUMNS = ["temperature_c", "soc_pct", "step_end", "state_code", "control_subtype", "save_class",
                    "step_type_code", "total_cycle", "current_cycle", "data_seq", "date_integer", "clock_integer"]
DEFAULTS = {"profile": "voltage_profiles", "compression_level": 19, "row_group_size": 131072,
            "keep_columns": VOLTAGE_PROFILE_COLUMNS, "keep_cycles": None,
            "keep_steps": None, "gold_csv": False}


def storage_policy(value=None):
    if value is None:
        value = {}
    if not isinstance(value, dict) or set(value) - set(DEFAULTS):
        raise ValueError("Unknown storage policy; choose profile, columns, cycles, steps, compression and row-group size")
    policy = copy.deepcopy(DEFAULTS | value)
    if policy["profile"] not in {"voltage_profiles", "curves", "features_only", "full_fields"}:
        raise ValueError("storage profile must be voltage_profiles, curves, features_only or full_fields")
    if "keep_columns" not in value and policy["profile"] == "curves":
        policy["keep_columns"] = CORE_COLUMNS + OPTIONAL_COLUMNS
    for key, lo, hi in (("compression_level", 1, 22), ("row_group_size", 1024, 2_000_000)):
        if type(policy[key]) is not int or not lo <= policy[key] <= hi:
            raise ValueError(f"{key} must be an integer in [{lo}, {hi}]")
    if type(policy["gold_csv"]) is not bool:
        raise ValueError("gold_csv must be a JSON boolean")
    columns = policy["keep_columns"]
    if not isinstance(columns, list) or any(not isinstance(v, str) or not v for v in columns) or len(set(columns)) != len(columns):
        raise ValueError("keep_columns must be distinct column names; identity columns are always retained")
    if policy["profile"] == "curves" and not set(CORE_COLUMNS) <= set(columns):
        raise ValueError("curves storage requires the core time/current/voltage/capacity/step columns")
    if policy["profile"] == "voltage_profiles" and not set(VOLTAGE_PROFILE_COLUMNS) <= set(columns):
        raise ValueError("voltage_profiles storage requires cycle/step/segment/time/voltage/capacity/step_kind columns")
    for key in ("keep_cycles", "keep_steps"):
        selected = policy[key]
        if selected is not None and (not isinstance(selected, list) or not selected or len({str(x) for x in selected}) != len(selected)):
            raise ValueError(f"{key} must be null or a nonempty list of distinct identifiers")
    if policy["keep_cycles"] is not None and any(type(x) is not int or x < 0 for x in policy["keep_cycles"]):
        raise ValueError("keep_cycles must contain nonnegative integer cycle identifiers")
    return policy


def retained_frame(frame, policy):
    if policy["profile"] == "features_only":
        return frame.iloc[:0, :0]
    keep = pd.Series(True, index=frame.index)
    if policy["keep_cycles"] is not None:
        keep &= frame["cycle"].isin(policy["keep_cycles"])
    if policy["keep_steps"] is not None:
        keep &= frame["step_id"].astype(str).isin([str(x) for x in policy["keep_steps"]])
    if policy["profile"] == "full_fields":
        columns = list(frame)
    else:
        unknown = set(policy["keep_columns"]) - set(CORE_COLUMNS + OPTIONAL_COLUMNS) - set(frame)
        if unknown:
            raise ValueError(f"Selected storage columns do not exist: {sorted(unknown)}")
        wanted = IDENTITY_COLUMNS + policy["keep_columns"]
        columns = list(dict.fromkeys(c for c in wanted if c in frame))
    return frame.loc[keep, columns]


def _arrow(frame):
    """Stable dtypes avoid null-only first chunks changing a stream's schema."""
    data = frame.copy()
    for column in data:
        if column in {"cycle", "segment_id", "source_row", "record_index"}:
            data[column] = pd.to_numeric(data[column], errors="raise").astype("int64")
        elif column in {"time_s", "step_time_s", "voltage_v", "current_a", "capacity_ah", "temperature_c", "soc_pct"}:
            data[column] = pd.to_numeric(data[column], errors="raise").astype("float64")
        elif data[column].dtype == "object" or isinstance(data[column].dtype, pd.StringDtype):
            data[column] = data[column].astype("string")
    return pa.Table.from_pandas(data, preserve_index=False)


class CompactWriter:
    def __init__(self, path: str | Path, policy=None):
        self.path = Path(path)
        self.policy = storage_policy(policy)
        self.writer = None
        self.schema = None
        self.source_rows = self.saved_rows = self.source_frame_bytes = 0
        self.available_columns, self.saved_columns = set(), []

    def write(self, frame):
        self.source_rows += len(frame)
        self.source_frame_bytes += int(frame.memory_usage(index=False, deep=True).sum())
        self.available_columns.update(frame.columns)
        selected = retained_frame(frame, self.policy)
        if selected.empty:
            return
        table = _arrow(selected)
        if self.writer is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.schema = table.schema
            self.saved_columns = table.column_names
            strings = [f.name for f in table.schema if pa.types.is_string(f.type) or pa.types.is_large_string(f.type)]
            floats = [f.name for f in table.schema if pa.types.is_floating(f.type)]
            self.writer = pq.ParquetWriter(self.path, self.schema, compression="zstd",
                                          compression_level=self.policy["compression_level"],
                                          use_dictionary=strings, use_byte_stream_split=floats,
                                          data_page_version="2.0", write_statistics=True,
                                          write_page_checksum=True)
        elif not table.schema.equals(self.schema, check_metadata=False):
            table = table.cast(self.schema)
        self.writer.write_table(table, row_group_size=self.policy["row_group_size"])
        self.saved_rows += len(selected)

    def close(self):
        if self.writer:
            self.writer.close()
            self.writer = None
        saved = set(self.saved_columns)
        limitations = []
        if "current_a" not in saved:
            limitations.append({"code": "missing_current", "requires_original_raw_for": ["dcir", "current_integration"],
                                "reason": "Current is not retained; DCIR and current integration cannot be recalculated from this signal table."})
        if not {"mode", "end_flag"} <= saved:
            limitations.append({"code": "missing_mode_or_end_flag", "requires_original_raw_for": ["validated_cc_cv_selection", "completion_validation"],
                                "reason": "Charge/discharge labels alone do not establish CC/CV mode or completion."})
        if self.saved_rows != self.source_rows:
            limitations.append({"code": "omitted_rows", "requires_original_raw_for": ["full_record_recalculation"],
                                "reason": "Some source records are not retained."})
        future_recalculation = {
            "voltage_profile_columns_retained": set(VOLTAGE_PROFILE_COLUMNS) <= saved,
            "current_column_retained": "current_a" in saved,
            "all_source_rows_retained": self.saved_rows == self.source_rows,
            "limitations": limitations,
            "scope": "Column retention only; valid measurements and protocol conditions must still be checked before recalculation.",
        }
        return {"profile": self.policy["profile"], "compression": "zstd",
                "compression_level": self.policy["compression_level"], "source_rows": self.source_rows,
                "saved_rows": self.saved_rows, "discarded_rows": self.source_rows - self.saved_rows,
                "saved_columns": self.saved_columns, "omitted_columns": sorted(self.available_columns - set(self.saved_columns)),
                "parquet_bytes": self.path.stat().st_size if self.path.exists() else 0,
                "precision": "float64_or_original_integer_no_rounding", "downsampled": False,
                "future_recalculation": future_recalculation,
                "original_raw_deleted": False, "row_selection": {k: self.policy[k] for k in ("keep_cycles", "keep_steps")}}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def save_compact_table(frame, path, policy=None):
    policy = storage_policy(policy)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = frame if len(frame.columns) else pd.DataFrame({"status": pd.Series(dtype="string")})
    table = _arrow(data)
    pq.write_table(table, path, compression="zstd", compression_level=policy["compression_level"],
                   use_dictionary=True, data_page_version="2.0", row_group_size=policy["row_group_size"],
                   write_page_checksum=True)
    if policy["gold_csv"]:
        data.to_csv(path.with_suffix(".csv"), index=False, encoding="utf-8-sig")
