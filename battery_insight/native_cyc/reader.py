"""Streaming PNE 4100/4101 CYC reader with versioned header capacities.

No Restore/CSV/schedule/unit-detector dependency. Every CYC record is emitted.
Unverified voltage/capacity units remain native; optional CTS only adds labels.
"""
from __future__ import annotations

from array import array
from collections import defaultdict
from dataclasses import dataclass
import json
import math
from pathlib import Path
import struct
import sys

import pyarrow as pa


FORMAT_ID = "pne_native_cyc_float32_v2"
HEADER_MAGIC = (20180615, 4100)
DATA_START = 404
RECORD_SIZE = 120
TAIL_SIZE = 0
FIELD_IDS = (22, 6, 38, 40, 1, 2, 3, 36, 26, 27, 11, 37, 34, 35, 23,
             24, 12, 4, 10, 25, 33, 41, 50, 42, 44, 45, 46, 47, 48, 49)
CYC_SIGNATURE = b"PNE record data file."
CTS_SIGNATURE = b"PNE Cycler step end data file."
CTS_START = 896
CTS_RECORD_SIZE = 172
MAX_CTS_BYTES = 128 * 1024 * 1024
# PS_RECORD_FILE_HEADER reserves more WORD slots than nColumnCount. The old
# reader's 392-byte start included 12 bytes of this reserve in every row and
# compensated with shifted offsets. Real float records start after ALL slots.
# 4100: 36 slots -> 404 bytes; 4101: 42 slots -> 416 bytes.
LAYOUTS = {(20180615, 4100): 36, (20220405, 4101): 42}
REQUIRED_FIELDS = {"data_seq": 22, "step_time_s": 6, "voltage_native": 1, "capacity_native": 3}


class CycFormatError(ValueError):
    """The input does not match the independently checked binary contract."""


def _field(name, datatype, *, unit, definition, nullable=True):
    return pa.field(name, datatype, nullable=nullable,
                    metadata={b"unit": unit.encode(), b"definition": definition.encode()})


SCHEMA = pa.schema([
    _field("source_row", pa.int64(), unit="1-based record", definition="Physical CYC record ordinal; never filtered.", nullable=False),
    _field("data_seq", pa.float64(), unit="native sequence", definition="Native float32 DataSeq promoted exactly to float64.", nullable=False),
    _field("step_time_s", pa.float64(), unit="s", definition="Native StepTime; resets by step. Not total elapsed time.", nullable=False),
    _field("voltage_native", pa.float64(), unit="native_unverified", definition="Native voltage numeric value; physical unit is not established by this header.", nullable=False),
    _field("capacity_native", pa.float64(), unit="native_unverified", definition="Native capacity numeric value; physical unit is not established by this header.", nullable=False),
    _field("cycle", pa.int64(), unit="native CTS TotalCycle", definition="CTS int32 TotalCycle at offset 20; not an inferred lifetime cycle."),
    _field("step", pa.int32(), unit="native CTS StepNo", definition="CTS uint8 StepNo at offset 1."),
    _field("segment_id", pa.int64(), unit="1-based CTS record", definition="Ordinal of the unique CTS record covering this CYC record."),
    _field("total_time_s", pa.float64(), unit="s", definition="Derived from verified CTS endpoint: float64(TotalTime)-float64(StepTime)+native CYC StepTime; null if unavailable."),
    _field("cts_matched", pa.bool_(), unit="boolean", definition="Exactly one valid CTS range labels this CYC row.", nullable=False),
], metadata={
    b"format_id": FORMAT_ID.encode(), b"storage_contract": b"all_native_cyc_records_no_downsampling_no_rounding",
    b"native_numeric_type": b"IEEE754 little-endian float32, exactly promoted to float64",
    b"unit_policy": b"No magnitude-based scaling or unit guessing. Voltage and capacity remain native_unverified.",
    b"time_policy": b"step_time_s is native step-local time; total_time_s is separate optional CTS-derived elapsed time.",
})


@dataclass(frozen=True)
class _Step:
    ordinal: int
    start: int
    end: int
    cycle: int | None
    step: int
    step_time: float
    time_offset: float | None


@dataclass
class _Plan:
    path: Path
    signature: tuple
    record_count: int
    data_start: int
    record_size: int
    offsets: dict[str, int]
    cts_path: Path | None
    cts_signature: tuple | None
    spans: list[tuple[int, int, _Step]]
    info: dict


def _signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ino


def _check_unchanged(path, expected):
    if _signature(path) != expected:
        raise CycFormatError(f"Input changed while reading: {path}")


def _cts_spans(path, cyc, record_count, warnings, *, data_start, record_size, time_offset):
    """Use unique valid ranges only; ambiguity removes labels, never CYC rows."""
    cts = path.with_suffix(".cts")
    if not cts.is_file():
        warnings.append({"code": "cts_missing", "message": "Matching .cts is absent; all CYC rows remain, with cycle/step/total_time_s null."})
        return None, None, [], {"status": "missing", "matched_rows": 0}
    before = _signature(cts)
    if before[0] > MAX_CTS_BYTES:
        warnings.append({"code": "cts_too_large", "message": "CTS exceeds metadata read limit; CYC records are preserved without labels."})
        return cts, before, [], {"status": "unsupported", "matched_rows": 0}
    content = cts.read_bytes()
    _check_unchanged(cts, before)
    valid_header = (len(content) >= CTS_START and struct.unpack_from("<ii", content) == HEADER_MAGIC
                    and content[72:72 + len(CTS_SIGNATURE)] == CTS_SIGNATURE
                    and (len(content) - CTS_START) % CTS_RECORD_SIZE == 0)
    if not valid_header:
        warnings.append({"code": "cts_unsupported", "message": "Matching CTS does not match verified 4100/896/172 layout; CYC rows are preserved without labels."})
        return cts, before, [], {"status": "unsupported", "matched_rows": 0}
    count = (len(content) - CTS_START) // CTS_RECORD_SIZE
    records, events = {}, defaultdict(list)
    invalid, unverified_time = 0, 0
    for ordinal in range(1, count + 1):
        base = CTS_START + (ordinal - 1) * CTS_RECORD_SIZE
        start, end = struct.unpack_from("<ii", content, base + 8)
        if not 0 <= start <= end < record_count:
            invalid += 1
            continue
        cycle = struct.unpack_from("<i", content, base + 20)[0]
        step_time, total_time = struct.unpack_from("<ff", content, base + 52)
        cyc.seek(data_start + end * record_size + time_offset)
        raw = cyc.read(4)
        if len(raw) != 4:
            raise CycFormatError("CYC endpoint became unavailable while reading CTS labels")
        endpoint_time = struct.unpack("<f", raw)[0]
        # Source float32 endpoints must agree exactly. Use double precision only
        # for this separately named derived coordinate; never rewrite StepTime.
        time_valid = (math.isfinite(step_time) and math.isfinite(total_time)
                      and step_time >= 0 and total_time >= step_time and step_time == endpoint_time)
        offset = float(total_time) - float(step_time) if time_valid else None
        if not time_valid:
            unverified_time += 1
        item = _Step(ordinal, start, end, cycle if cycle >= 0 else None,
                     content[base + 1], step_time, offset)
        records[ordinal] = item
        events[start].append((True, ordinal))
        events[end + 1].append((False, ordinal))
    active, spans = set(), []
    previous, overlap_rows = None, 0
    for position in sorted(events):
        if previous is not None and position > previous:
            if len(active) == 1:
                spans.append((previous, position, records[next(iter(active))]))
            elif len(active) > 1:
                overlap_rows += position - previous
        for entering, ordinal in events[position]:
            if entering:
                active.add(ordinal)
            else:
                active.remove(ordinal)
        previous = position
    matched = sum(end - start for start, end, _ in spans)
    if invalid:
        warnings.append({"code": "cts_invalid_ranges", "count": invalid,
                         "message": "Invalid/out-of-bounds CTS ranges are not used; CYC rows are preserved."})
    if overlap_rows:
        warnings.append({"code": "cts_ambiguous_rows", "count": overlap_rows,
                         "message": "Overlapping CTS ranges leave affected CYC labels null."})
    if matched != record_count:
        warnings.append({"code": "cts_unmatched_rows", "count": record_count - matched,
                         "message": "Unmatched CYC records are retained with null labels and total_time_s."})
    if unverified_time:
        warnings.append({"code": "cts_time_unverified", "count": unverified_time,
                         "message": "CTS time endpoint did not verify; affected total_time_s remains null."})
    return cts, before, spans, {"status": "matched" if matched == record_count else "partial",
                               "layout": "pne_cts_4100_o896_r172", "record_count": count,
                               "matched_rows": matched, "unmatched_rows": record_count - matched,
                               "ambiguous_rows": overlap_rows, "invalid_ranges": invalid,
                               "unverified_time_ranges": unverified_time}


def _make_plan(cyc_path):
    path = Path(cyc_path).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".cyc":
        raise CycFormatError(f"Choose an existing native .cyc file: {path}")
    before = _signature(path)
    warnings = [{"code": "native_units_unverified",
                 "message": "Voltage and capacity retain exact native numbers. Their physical units are not identified by the supported header; no V/Ah scaling is applied."}]
    with path.open("rb") as source:
        prefix = source.read(332)
        if len(prefix) < 332:
            raise CycFormatError(f"CYC header is truncated: {before[0]:,} bytes; at least 332 header bytes required")
        magic = struct.unpack_from("<ii", prefix)
        capacity = LAYOUTS.get(magic)
        if capacity is None:
            raise CycFormatError(f"미지원 CYC 버전 {magic[0]} / {magic[1]}. 지원: 4100(20180615), 4101(20220405).")
        data_start = 332 + 2 * capacity
        header = prefix + source.read(data_start - len(prefix))
        if len(header) != data_start:
            raise CycFormatError(f"CYC header is truncated: version {magic[1]} requires {data_start} bytes")
        if header[72:72 + len(CYC_SIGNATURE)] != CYC_SIGNATURE:
            raise CycFormatError("Unsupported CYC signature")
        fields = struct.unpack_from("<i", header, 328)[0]
        if not 1 <= fields <= capacity:
            raise CycFormatError(f"Invalid CYC field count {fields}; version {magic[1]} reserves {capacity} slots")
        ids = struct.unpack_from(f"<{fields}H", header, 332)
        if len(set(ids)) != len(ids) or not set(REQUIRED_FIELDS.values()).issubset(ids):
            raise CycFormatError("Unsupported CYC field table: duplicate fields or required time/voltage/capacity field missing")
        if any(header[332 + 2 * fields:]):
            raise CycFormatError("Unsupported CYC header reserve: nonzero unused column slots")
        record_size = fields * 4
        count, tail = divmod(before[0] - data_start, record_size)
        if tail or count < 1:
            raise CycFormatError(f"CYC length mismatch/truncated: version {magic[1]}, actual {before[0]:,} bytes; "
                                 f"requires {data_start} + N*{record_size} bytes (remainder {tail}). No partial record is discarded.")
        offsets = {name: ids.index(code) * 4 for name, code in REQUIRED_FIELDS.items()}
        cts, cts_signature, spans, coverage = _cts_spans(path, source, count, warnings,
            data_start=data_start, record_size=record_size, time_offset=offsets["step_time_s"])
    _check_unchanged(path, before)
    units = {field.name: field.metadata[b"unit"].decode() for field in SCHEMA}
    layout = f"pne_cyc_{magic[1]}_h{data_start}_r{record_size}_v2"
    info = {"schema_version": 2, "path": str(path), "layout": layout,
            "header_magic": list(magic), "data_start": data_start, "record_size": record_size,
            "column_count": fields, "column_capacity": capacity, "column_ids": list(ids),
            "record_count": count, "trailing_bytes": tail, "source_bytes": before[0],
            "cts_path": str(cts) if cts else None, "cts": coverage, "units": units,
            "unit_policy": "preserve_native_no_inference", "warnings": warnings,
            "columns": list(SCHEMA.names), "native_offsets": offsets,
            "time_definition": {"step_time_s": "Native per-step seconds; may reset at each step.",
                                "total_time_s": "Optional CTS-derived elapsed seconds; null without a unique verified CTS time range."}}
    return _Plan(path, before, count, data_start, record_size, offsets, cts, cts_signature, spans, info)


def inspect_file(cyc_path) -> dict:
    """Validate the binary contract and report optional CTS coverage and units."""
    return _make_plan(cyc_path).info


def iter_batches(cyc_path, batch_rows=65536):
    """Yield every native CYC record exactly once as a consistent Arrow table."""
    if type(batch_rows) is not int or not 1 <= batch_rows <= 1_000_000:
        raise ValueError("batch_rows must be an integer from 1 to 1,000,000")
    plan = _make_plan(cyc_path)
    metadata = dict(SCHEMA.metadata)
    metadata[b"format_id"] = plan.info["layout"].encode()
    metadata[b"source_cyc"] = str(plan.path).encode("utf-8")
    metadata[b"source_cts"] = (str(plan.cts_path) if plan.cts_path else "").encode("utf-8")
    metadata[b"reader_inspection"] = json.dumps(plan.info, ensure_ascii=False, sort_keys=True).encode("utf-8")
    schema = SCHEMA.with_metadata(metadata)
    span_index = 0
    with plan.path.open("rb") as source:
        source.seek(plan.data_start)
        for start in range(0, plan.record_count, batch_rows):
            length = min(batch_rows, plan.record_count - start)
            raw = source.read(length * plan.record_size)
            if len(raw) != length * plan.record_size:
                raise CycFormatError("CYC file was truncated while converting")
            values = array("f")
            values.frombytes(raw)
            if sys.byteorder != "little":
                values.byteswap()
            width = plan.record_size // 4
            step_times = values[plan.offsets["step_time_s"] // 4::width]
            columns = [pa.array(range(start + 1, start + length + 1), type=pa.int64()),
                       pa.array(values[plan.offsets["data_seq"] // 4::width], type=pa.float64()),
                       pa.array(step_times, type=pa.float64()),
                       pa.array(values[plan.offsets["voltage_native"] // 4::width], type=pa.float64()),
                       pa.array(values[plan.offsets["capacity_native"] // 4::width], type=pa.float64())]
            cycles, steps, segments, total_times = ([None] * length for _ in range(4))
            matched = [False] * length
            while span_index < len(plan.spans) and plan.spans[span_index][1] <= start:
                span_index += 1
            position = span_index
            while position < len(plan.spans) and plan.spans[position][0] < start + length:
                span_start, span_end, item = plan.spans[position]
                lo, hi = max(span_start, start) - start, min(span_end, start + length) - start
                for local in range(lo, hi):
                    cycles[local], steps[local], segments[local] = item.cycle, item.step, item.ordinal
                    matched[local] = True
                    if item.time_offset is not None and math.isfinite(step_times[local]) and 0 <= step_times[local] <= item.step_time:
                        total_times[local] = item.time_offset + float(step_times[local])
                position += 1
            columns.extend([pa.array(cycles, type=pa.int64()), pa.array(steps, type=pa.int32()),
                            pa.array(segments, type=pa.int64()), pa.array(total_times, type=pa.float64()),
                            pa.array(matched, type=pa.bool_())])
            yield pa.Table.from_arrays(columns, schema=schema)
    _check_unchanged(plan.path, plan.signature)
    if plan.cts_path:
        _check_unchanged(plan.cts_path, plan.cts_signature)
