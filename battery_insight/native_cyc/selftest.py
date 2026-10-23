"""Small native-only fixtures and offline executable self-test."""
from pathlib import Path
import struct
import sys
import tempfile


def fixture_folder(folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    header = bytearray(392)
    struct.pack_into("<ii", header, 0, 20180615, 4100)
    signature = b"PNE record data file."
    header[72:72 + len(signature)] = signature
    struct.pack_into("<i", header, 328, 30)
    struct.pack_into("<30H", header, 332, 22, 6, 38, 40, 1, 2, 3, 36, 26, 27,
                     11, 37, 34, 35, 23, 24, 12, 4, 10, 25, 33, 41, 50, 42,
                     44, 45, 46, 47, 48, 49)
    expected = [(1., 0., 3100.125, 0.), (2., 1., 3200.25, .5), (3., 2., 3300.5, .75),
                (4., 0., 3300.5, 0.), (5., 1., 3200.25, .25), (6., 2., 3100.125, .5)]
    records = []
    for values in expected:
        record = bytearray(120)
        for offset, value in zip((12, 16, 28, 36), values):
            struct.pack_into("<f", record, offset, value)
        records.append(record)
    paths = [(folder / "sample.cyc", records), (folder / "sample_org.cyc", records[:3]),
             (folder / "하위 폴더" / "sample.cyc", records[:4])]
    for path, payload in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(header + b"".join(payload) + bytes(12))
    cts_header = bytearray(896)
    struct.pack_into("<ii", cts_header, 0, 20180615, 4100)
    cts_signature = b"PNE Cycler step end data file."
    cts_header[72:72 + len(cts_signature)] = cts_signature
    steps = []
    for step_no, begin, end, cycle, total in ((1, 0, 2, 1, 2.), (2, 3, 5, 2, 4.)):
        record = bytearray(172)
        record[1] = step_no
        struct.pack_into("<ii", record, 8, begin, end)
        struct.pack_into("<i", record, 20, cycle)
        struct.pack_into("<ff", record, 52, 2., total)
        steps.append(record)
    (folder / "sample.cts").write_bytes(cts_header + b"".join(steps))
    return expected


def fixture_4101(folder):
    """Independent 42-slot header and 37-float records; no trailing bytes."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    ids = (22, 6, 38, 40, 1, 2, 3, 36, 26, 27, 11, 37, 34, 35, 23, 24,
           12, 4, 10, 25, 33, 41, 50, 42, 44, 45, 46, 47, 75, 76, 77, 78, 79, 73, 74, 48, 49)
    header = bytearray(416)
    struct.pack_into("<ii", header, 0, 20220405, 4101)
    header[72:93] = b"PNE record data file."
    struct.pack_into("<i", header, 328, 37)
    struct.pack_into("<37H", header, 332, *ids)
    expected = [(1., 0., 613.968994140625, 0.), (2., 60., 3500.125, .125),
                (7., 0., 4321.5, -987.25), (8., 1.25, 3389.5380859375, 27669.05859375)]
    records = []
    for seq, time, voltage, capacity in expected:
        values = {22: seq, 6: time, 1: voltage, 3: capacity}
        records.append(struct.pack("<37f", *(values.get(code, 0.) for code in ids)))
    (folder / "sample_4101.cyc").write_bytes(header + b"".join(records))
    return expected


def run_self_test():
    import pyarrow.parquet as pq
    from . import __version__
    from .service import convert_folder, default_output, digest
    with tempfile.TemporaryDirectory(prefix="cyc_parquet_") as temporary:
        raw = Path(temporary) / "원본 파일"
        expected = fixture_folder(raw)
        before = {str(path): digest(path) for path in raw.rglob("*") if path.is_file()}
        result = convert_folder(raw)
        output = default_output(raw)
        table = pq.read_table(output / "parquet/sample.parquet")
        actual = list(zip(*(table[name].to_pylist() for name in
                           ("data_seq", "step_time_s", "voltage_native", "capacity_native"))))
        orphan = pq.read_table(output / "parquet/하위 폴더/sample.parquet")
        checks = {
            "native_only_input": all(Path(name).suffix in {".cyc", ".cts"} for name in before),
            "all_files_including_backup": result["summary"]["complete"] == 3 and result["summary"]["failed"] == 0,
            "all_thirteen_records_preserved": result["summary"]["rows"] == 13,
            "exact_native_time_voltage_capacity": actual == expected,
            "cts_cycle_labels": table["cycle"].to_pylist() == [1, 1, 1, 2, 2, 2],
            "cts_total_time": table["total_time_s"].to_pylist() == [0., 1., 2., 2., 3., 4.],
            "missing_cts_records_retained": orphan.num_rows == 4 and orphan["cycle"].null_count == 4,
            "units_not_guessed": table.schema.field("capacity_native").metadata[b"unit"] == b"native_unverified",
            "no_metric_or_csv_output": not list(output.rglob("*.csv")) and not (output / "gold").exists(),
        }
        hashes = {str(path): digest(path) for path in output.rglob("*.parquet")}
        repeat = convert_folder(raw)
        checks["all_files_reused"] = repeat["summary"]["reused"] == 3 and all(digest(path) == value for path, value in hashes.items())
        checks["original_bytes_unchanged"] = all(digest(path) == value for path, value in before.items())
        new_raw = Path(temporary) / "4101 원본"
        new_expected = fixture_4101(new_raw)
        new_result = convert_folder(new_raw)
        new_table = pq.read_table(default_output(new_raw) / "parquet/sample_4101.parquet")
        checks["4101_reserved_header_all_records_exact"] = new_result["summary"]["rows"] == len(new_expected) and list(zip(*(
            new_table[name].to_pylist() for name in ("data_seq", "step_time_s", "voltage_native", "capacity_native")))) == new_expected
        return {"status": "success" if all(checks.values()) else "failed", "version": __version__,
                "checks": checks, "runtime": {"frozen": bool(getattr(sys, "frozen", False)),
                                              "python": sys.version.split()[0]}}
