"""4101 binary contract, independent of the parser's layout constants."""
import struct

import pyarrow as pa
import pytest

from battery_insight.native_cyc import reader
from battery_insight.native_cyc.service import convert_folder


IDS = (22, 6, 38, 40, 1, 2, 3, 36, 26, 27, 11, 37, 34, 35, 23, 24,
       12, 4, 10, 25, 33, 41, 50, 42, 44, 45, 46, 47, 75, 76, 77, 78,
       79, 73, 74, 48, 49)


def write_4101(path, ids=IDS):
    header = bytearray(416)
    struct.pack_into("<ii", header, 0, 20220405, 4101)
    header[72:93] = b"PNE record data file."
    struct.pack_into("<i", header, 328, len(ids))
    struct.pack_into(f"<{len(ids)}H", header, 332, *ids)
    expected = [(1., 0., 613.968994140625, 0.), (2., 60., 3500.125, .125),
                (7., 0., 4321.5, -987.25), (8., 1.25, 3389.5380859375, 27669.05859375)]
    content = bytearray(header)
    for seq, time, voltage, capacity in expected:
        values = {22: seq, 6: time, 1: voltage, 3: capacity}
        content.extend(struct.pack(f"<{len(ids)}f", *(values.get(code, 999.) for code in ids)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return expected


@pytest.mark.parametrize("ids", [IDS, tuple(reversed(IDS)), (3, 1, 6, 22)])
def test_reserved_header_capacity_and_field_ids_control_record_offsets(tmp_path, ids):
    path = tmp_path / "native.cyc"
    expected = write_4101(path, ids)
    info = reader.inspect_file(path)
    assert info["data_start"] == 416 and info["column_capacity"] == 42
    assert info["record_size"] == len(ids) * 4 and info["record_count"] == 4
    table = pa.concat_tables(list(reader.iter_batches(path, batch_rows=2)))
    assert list(zip(*(table[name].to_pylist() for name in ("data_seq", "step_time_s", "voltage_native", "capacity_native")))) == expected
    assert table["source_row"].to_pylist() == [1, 2, 3, 4]
    assert table["cycle"].null_count == 4
    assert table.schema.metadata[b"format_id"].startswith(b"pne_cyc_4101_")


@pytest.mark.parametrize("mutation", ["truncated", "version", "reserve", "missing_field", "duplicate_field"])
def test_invalid_4101_never_publishes_a_partial_or_misaligned_curve(tmp_path, mutation):
    path = tmp_path / "raw/native.cyc"
    write_4101(path)
    content = bytearray(path.read_bytes())
    if mutation == "truncated":
        content = content[:-1]
    elif mutation == "version":
        struct.pack_into("<i", content, 4, 4102)
    elif mutation == "reserve":
        content[414] = 1
    elif mutation == "missing_field":
        struct.pack_into("<H", content, 332, 999)
    else:
        struct.pack_into("<H", content, 334, 22)
    path.write_bytes(content)
    result = convert_folder(path.parent, tmp_path / "output")
    assert result["status"] == "failed"
    assert not list((tmp_path / "output").rglob("*.parquet"))
    assert path.read_bytes() == content
