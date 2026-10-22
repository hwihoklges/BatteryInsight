"""Streaming, readonly adapter for the verified PNE 51-field small-cell schema.

Evidence: data_analysis_projects/hppc/hppc_analysis/ingest.py:15,28-57;
data_analysis/qpeed_analysis/cdm_csv.py:75-103 (field positions only).
Actual supplied cycle DataSeq 4: Restore I=4913830 -> converted 4.9138303 mA.
BOL StepEnd Q=64.676 mAh matches Restore Q=64675797. Thus this explicit
small-cell profile uses I[A],Q[Ah] = integer/1e9, not QPEED's /1e6 A/Ah.
The adapter never guesses another instrument range, zeros rest sensor current,
infers physical cell identity, or infers CC/CV, temperature or SOC.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
from zipfile import ZipFile

import numpy as np
import pandas as pd

from .ingest import sha256_file

RESTORE_SCHEMA = "pne_restore_51_smallcell_v1"
CHANNEL_RE = re.compile(r"^M(?P<module>\d+)Ch(?P<channel>\d+)(?:\[\d+\])?$", re.I)
MEMBER_RE = re.compile(r"^ch(?P<channel>\d+)_SaveData(?P<sequence>\d+)\.csv$", re.I)
INTEGER_RE = re.compile(r"^[+-]?\d+$")
RAW_COLUMNS = tuple(f"pne_raw_{i:02d}" for i in range(51))
MAX_MEMBERS = 10000
MAX_LINE_BYTES = 16384
MAX_BATCH_ROWS = 262144
MAX_SCHEDULE_BYTES = 1024 * 1024
STEP_TYPES = {1: "charge", 2: "discharge", 3: "rest", 6: "end"}
END_CODES = {64: "time_end", 65: "voltage_end", 66: "current_end", 69: "step_complete", 78: "soc_end", 154: "next_step"}
FIELD_CONTRACT = {
    "schema": RESTORE_SCHEMA, "field_count": 51,
    "canonical_units": {"time_s": "s", "step_time_s": "s", "voltage_v": "V", "current_a": "A", "capacity_ah": "Ah"},
    "scales": {"voltage_v": {"index": 8, "divisor": 1000000}, "current_a": {"index": 9, "divisor": 1000000000},
               "capacity_ah": {"index": 10, "divisor": 1000000000}, "step_time_s": {"index": 13, "divisor": 100},
               "time_s": {"index": 14, "divisor": 100}},
    "raw_field_positions": {"data_seq": 0, "record_kind": 1, "step_type_code": 2, "control_subtype": 3,
                            "save_class": 4, "state_code": 5, "step_no": 7, "total_cycle": 18, "current_cycle": 19,
                            "date_integer": 37, "clock_integer": 38},
    "mode_policy": "unknown; raw control_subtype is preserved without CC/CV inference",
    "rest_current_policy": "measured sensor current preserved; no zero replacement",
    "missing_policy": "no inferred SOC or temperature; malformed/blank integer fields are rejected",
    "identity_policy": "source_id is container-path/channel scoped, never a physical cell identity",
    "source_row_basis": "1-based physical member line; record_index is 1-based channel data-record order",
    "all_raw_fields_preserved": True,
}


def decode_member_name(name: str, flag_bits: int = 0) -> str:
    """Recover CP949 ZIP names only when the ZIP UTF-8 flag is absent."""
    if flag_bits & 0x800:
        return name
    try:
        return name.encode("cp437").decode("cp949")
    except UnicodeError:
        return name


def _safe_member(name):
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or any(":" in part for part in path.parts):
        raise ValueError(f"Unsafe PNE archive member path: {name}")
    return path


def _descriptor(container, channel_path, members, *, is_archive, container_hash, stat):
    channel_id = PurePosixPath(channel_path).name if channel_path != "." else container.name
    match = CHANNEL_RE.fullmatch(channel_id)
    if not match:
        raise ValueError(f"Unsupported PNE channel directory: {channel_id}")
    if len({m["sequence"] for m in members}) != len(members):
        raise ValueError(f"Duplicate Restore fragment number in {channel_path}")
    local_channels = {m["restore_channel_number"] for m in members}
    if len(local_channels) != 1:
        raise ValueError(f"Restore local channel mismatch within channel directory: {channel_path}")
    if len(members) > MAX_MEMBERS:
        raise ValueError("PNE source member limit exceeded")
    members.sort(key=lambda m: m["sequence"])
    key = f"{container.as_posix()}::{channel_path}"
    return {"source_id": "pne_" + hashlib.sha256(key.encode()).hexdigest()[:20], "schema": RESTORE_SCHEMA,
            "container": str(container), "source_path": str(container), "is_archive": is_archive,
            "container_sha256": container_hash, "container_stat": stat, "channel_id": channel_id,
            "module_number": int(match["module"]), "channel_number": int(match["channel"]),
            "restore_channel_number": next(iter(local_channels)),
            "channel_number_policy": "Directory/global and Restore/module-local numbers are retained separately",
            "channel_path": channel_path,
            "channel_label": members[0]["display_name"].replace("\\", "/").rsplit("/", 2)[0] if "/" in members[0]["display_name"].replace("\\", "/") else channel_id,
            "members": members, "member_count": len(members), "source_bytes": sum(m["bytes"] for m in members),
            "field_contract": FIELD_CONTRACT.copy()}


def discover_pne_sources(path: str | Path) -> list[dict]:
    """Discover channel-scoped Restore sources; never extract archive members.

    ZIP archive bytes are hashed once. Folder members are inspected by stat.
    Iteration later hashes each member and adds sha256/channel_sha256 to the
    supplied descriptor. Binary .cyc/.cts, backups and converted CSVs are ignored.
    """
    container = Path(path).resolve()
    if container.is_dir() and container.name.lower() == "restore":
        container = container.parent
    groups = {}
    is_archive = container.is_file() and container.suffix.lower() == ".zip"
    container_hash, container_stat = None, None
    if is_archive:
        before = container.stat()
        container_hash = sha256_file(container)
        container_stat = [before.st_size, before.st_mtime_ns]
        with ZipFile(container) as archive:
            seen = set()
            for info in archive.infolist():
                name = info.filename.replace("\\", "/")
                p = PurePosixPath(name)
                found = MEMBER_RE.fullmatch(p.name)
                if info.is_dir() or not found or p.parent.name.lower() != "restore":
                    continue
                _safe_member(name)
                channel = p.parent.parent
                channel_match = CHANNEL_RE.fullmatch(channel.name)
                if not channel_match:
                    continue
                if info.filename in seen:
                    raise ValueError(f"Duplicate ZIP member: {info.filename}")
                seen.add(info.filename)
                if info.flag_bits & 1:
                    raise ValueError("Encrypted PNE members are not supported")
                groups.setdefault(channel.as_posix(), []).append({"member": info.filename,
                    "display_name": decode_member_name(info.filename, info.flag_bits), "sequence": int(found["sequence"]),
                    "restore_channel_number": int(found["channel"]),
                    "bytes": info.file_size, "compressed_bytes": info.compress_size, "crc32": info.CRC,
                    "zip_flags": info.flag_bits, "sha256": None})
        after = container.stat()
        if [after.st_size, after.st_mtime_ns] != container_stat:
            raise ValueError("PNE archive changed during discovery")
    elif container.is_dir():
        for directory, _, names in os.walk(container, followlinks=False):
            parent = Path(directory)
            if parent.name.lower() != "restore":
                continue
            channel = parent.parent
            channel_match = CHANNEL_RE.fullmatch(channel.name)
            if not channel_match:
                continue
            for name in names:
                found = MEMBER_RE.fullmatch(name)
                if not found:
                    continue
                actual = (parent / name).resolve()
                if not actual.is_relative_to(container):
                    raise ValueError("Restore member resolves outside the supplied folder")
                stat = actual.stat()
                relative = actual.relative_to(container).as_posix()
                groups.setdefault(channel.relative_to(container).as_posix(), []).append({"member": relative,
                    "display_name": relative, "sequence": int(found["sequence"]), "bytes": stat.st_size,
                    "restore_channel_number": int(found["channel"]),
                    "mtime_ns": stat.st_mtime_ns, "sha256": None})
    else:
        raise ValueError("PNE input must be a ZIP or a folder containing Restore/ch*_SaveData*.csv")
    if not groups:
        raise ValueError("No supported Restore/ch*_SaveData*.csv sources found; binary/converted files are not imported as duplicates")
    sources = [_descriptor(container, channel, members, is_archive=is_archive, container_hash=container_hash, stat=container_stat)
               for channel, members in sorted(groups.items())]
    # Exact schedule hashes are protocol fingerprints, not parsed CC/CV rules.
    archive = ZipFile(container) if is_archive else None
    try:
        for source in sources:
            schedules = []
            if archive is not None:
                for info in archive.infolist():
                    p = _safe_member(info.filename)
                    if p.parent.as_posix() == source["channel_path"] and p.suffix.lower() == ".sch":
                        if info.file_size > MAX_SCHEDULE_BYTES:
                            raise ValueError("PNE schedule exceeds supported metadata size")
                        with archive.open(info) as stream:
                            digest = hashlib.sha256(stream.read(MAX_SCHEDULE_BYTES + 1)).hexdigest()
                        schedules.append({"member": info.filename, "display_name": decode_member_name(info.filename, info.flag_bits),
                                          "bytes": info.file_size, "sha256": digest})
            else:
                for p in (container / source["channel_path"]).iterdir():
                    if p.is_file() and p.suffix.lower() == ".sch":
                        if not p.resolve().is_relative_to(container) or p.stat().st_size > MAX_SCHEDULE_BYTES:
                            raise ValueError("Unsafe or oversized PNE schedule")
                        schedules.append({"member": p.relative_to(container).as_posix(), "display_name": p.name,
                                          "bytes": p.stat().st_size, "mtime_ns": p.stat().st_mtime_ns, "sha256": sha256_file(p)})
            unique = {p["sha256"] for p in schedules}
            source["schedules"] = schedules
            source["protocol_key"] = "pne_sch_sha256:" + next(iter(unique)) if len(unique) == 1 else None
            source["protocol_status"] = "unique_schedule_content" if len(unique) == 1 else "missing_schedule" if not unique else "conflicting_schedule_contents"
            source["protocol_key_basis"] = "Exact complete schedule bytes; no semantic normalization or mode inference"
    finally:
        if archive is not None:
            archive.close()
    return sources


@contextmanager
def _member_stream(source, member, archive=None):
    if source["is_archive"]:
        if archive is None:
            raise ValueError("Archive handle required")
        with archive.open(member["member"], "r") as stream:
            yield stream
    else:
        root = Path(source["container"]).resolve()
        path = (root / _safe_member(member["member"])).resolve()
        if not path.is_relative_to(root):
            raise ValueError("Restore member resolves outside its source folder")
        with path.open("rb") as stream:
            yield stream


def _member_hash(source, member, archive):
    digest, count = hashlib.sha256(), 0
    if not source["is_archive"]:
        stat = (Path(source["container"]) / member["member"]).stat()
        if stat.st_size != member["bytes"] or stat.st_mtime_ns != member["mtime_ns"]:
            raise ValueError(f"Restore member changed after discovery: {member['member']}")
    with _member_stream(source, member, archive) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
            count += len(block)
    if count != member["bytes"]:
        raise ValueError(f"Restore member changed size: {member['member']}")
    value = digest.hexdigest()
    if member.get("sha256") not in (None, value):
        raise ValueError(f"Restore member hash changed: {member['member']}")
    member["sha256"] = value


def _device_datetime(date_value, clock_value):
    if date_value == 0 or clock_value < 0:
        return None
    date_text, clock_text = str(date_value), f"{clock_value:09d}"
    if len(date_text) != 8 or len(clock_text) != 9:
        return None
    try:
        return datetime.strptime(date_text + clock_text, "%Y%m%d%H%M%S%f").isoformat(timespec="milliseconds")
    except ValueError:
        return None


def _frame(values, rows, indices, segments, source, member):
    a = np.asarray(values, dtype=np.int64)
    frame = pd.DataFrame(a, columns=RAW_COLUMNS)
    kind, current = a[:, 2], a[:, 9] / 1e9
    canonical = {
        "record_index": np.asarray(indices, dtype=np.int64), "data_seq": a[:, 0], "cycle": a[:, 18],
        "total_cycle": a[:, 18], "current_cycle": a[:, 19], "step_id": a[:, 7], "segment_id": segments,
        "step_type": [STEP_TYPES.get(int(code), f"unknown_{code}") for code in kind],
        "step_type_code": kind, "control_subtype": a[:, 3], "save_class": a[:, 4], "state_code": a[:, 5],
        "step_end": a[:, 4] == 1,
        "end_flag": [END_CODES.get(int(code), f"unknown_{code}") if end == 1 else "" for code, end in zip(a[:, 5], a[:, 4])],
        "mode": "unknown", "time_s": a[:, 14] / 100., "step_time_s": a[:, 13] / 100.,
        "voltage_v": a[:, 8] / 1e6, "current_a": current, "sensor_current_a": current,
        "capacity_ah": a[:, 10] / 1e9,
        "current_direction_consistent": np.where(kind == 1, current >= 0, np.where(kind == 2, current <= 0, True)),
        "source_id": source["source_id"], "source_path": source["container"], "channel_id": source["channel_id"],
        "restore_channel_number": member["restore_channel_number"],
        "source_member": member["member"], "source_member_display": member["display_name"],
        "source_row": np.asarray(rows, dtype=np.int64), "source_hash": member["sha256"],
        "source_member_sha256": member["sha256"], "container_sha256": source.get("container_sha256"),
        "channel_sha256": source["channel_sha256"], "pne_schema": RESTORE_SCHEMA,
        "protocol_key": source.get("protocol_key"), "date_integer": a[:, 37], "clock_integer": a[:, 38],
        "datetime_local": [_device_datetime(int(d), int(t)) for d, t in zip(a[:, 37], a[:, 38])],
    }
    return pd.concat([frame, pd.DataFrame(canonical)], axis=1)


def iter_pne_batches(source: dict, batch_size: int = 65536):
    """Yield bounded canonical+raw DataFrames in acquisition order.

    Fully consume the iterator before committing output: hashes are checked
    while parsing as well as before yielding. Blank lines are not data records;
    every nonblank row must have exactly 51 nonblank signed int64 fields.
    Fragment boundaries never reset record_index or segment_id. Duplicate or
    reset DataSeq/total time aborts rather than sorting, dropping or repairing.
    """
    if source.get("schema") != RESTORE_SCHEMA:
        raise ValueError("Explicit supported small-cell Restore schema is required")
    if not isinstance(batch_size, int) or isinstance(batch_size, bool) or not 1 <= batch_size <= MAX_BATCH_ROWS:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_ROWS}")
    container = Path(source["container"])
    members = source.get("members", [])
    if not members or len(members) > MAX_MEMBERS:
        raise ValueError("Invalid Restore member count")
    if len({m["sequence"] for m in members}) != len(members):
        raise ValueError("Duplicate Restore fragment number")
    archive = ZipFile(container) if source["is_archive"] else None
    try:
        if source["is_archive"] and sha256_file(container) != source["container_sha256"]:
            raise ValueError("PNE archive changed after discovery")
        for schedule in source.get("schedules", []):
            _member_hash(source, schedule, archive)
        for member in members:
            _member_hash(source, member, archive)
        source["channel_sha256"] = hashlib.sha256(json.dumps(
            {"raw": [(m["member"], m["sha256"]) for m in sorted(members, key=lambda m: m["sequence"])],
             "schedules": [(m["member"], m["sha256"]) for m in source.get("schedules", [])]},
            ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        previous_seq, previous_time, previous_key, previous_end = None, None, None, False
        record_index, segment_index = 0, 0
        for member in sorted(members, key=lambda m: m["sequence"]):
            values, lines, indices, segments = [], [], [], []
            digest, row_number, member_count = hashlib.sha256(), 0, 0
            with _member_stream(source, member, archive) as stream:
                while True:
                    raw = stream.readline(MAX_LINE_BYTES + 1)
                    if not raw:
                        break
                    digest.update(raw)
                    row_number += 1
                    if len(raw) > MAX_LINE_BYTES:
                        raise ValueError(f"Restore line exceeds limit: {member['member']}:{row_number}")
                    try:
                        text = raw.decode("utf-8-sig" if row_number == 1 else "ascii").strip()
                    except UnicodeError as exc:
                        raise ValueError(f"Unsupported Restore text encoding: {member['member']}:{row_number}") from exc
                    if not text:
                        continue
                    fields = text.split(",")
                    if len(fields) != 51 or any(not INTEGER_RE.fullmatch(v.strip()) for v in fields):
                        raise ValueError(f"Expected exactly 51 nonblank integer Restore fields: {member['member']}:{row_number}")
                    value = [int(v) for v in fields]
                    if any(not -(2**63) <= v < 2**63 for v in value):
                        raise ValueError(f"Restore integer exceeds int64: {member['member']}:{row_number}")
                    seq, total_time = value[0], value[14]
                    if seq <= 0 or previous_seq is not None and seq <= previous_seq:
                        raise ValueError(f"DataSeq duplicated, reordered or reset: {member['member']}:{row_number}")
                    if total_time < 0 or value[13] < 0 or previous_time is not None and total_time < previous_time:
                        raise ValueError(f"Total/step time is negative or resets: {member['member']}:{row_number}")
                    if not 0 <= value[8] <= 10000000:
                        raise ValueError(f"Voltage outside supported single-cell range; verify schema: {member['member']}:{row_number}")
                    key = (value[18], value[19], value[7], value[2], value[3])
                    if previous_key != key or previous_end:
                        segment_index += 1
                    record_index += 1
                    member_count += 1
                    values.append(value)
                    lines.append(row_number)
                    indices.append(record_index)
                    segments.append(segment_index)
                    previous_seq, previous_time, previous_key, previous_end = seq, total_time, key, value[4] == 1
                    if len(values) == batch_size:
                        yield _frame(values, lines, indices, segments, source, member)
                        values, lines, indices, segments = [], [], [], []
            if digest.hexdigest() != member["sha256"]:
                raise ValueError(f"Restore member changed while parsing: {member['member']}")
            if not source["is_archive"]:
                stat = (container / member["member"]).stat()
                if [stat.st_size, stat.st_mtime_ns] != [member["bytes"], member["mtime_ns"]]:
                    raise ValueError(f"Restore member changed while parsing: {member['member']}")
            if not member_count:
                raise ValueError(f"Empty Restore member: {member['member']}")
            if values:
                yield _frame(values, lines, indices, segments, source, member)
        if source["is_archive"] and sha256_file(container) != source["container_sha256"]:
            raise ValueError("PNE archive changed while parsing")
    finally:
        if archive is not None:
            archive.close()
