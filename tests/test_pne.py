"""Native Restore provenance, explicit units and streaming boundaries."""
import copy
import hashlib
from pathlib import Path
import zipfile

import pandas as pd
import pytest

from battery_insight.pne import RAW_COLUMNS, RESTORE_SCHEMA, decode_member_name, discover_pne_sources, iter_pne_batches


def row(seq, *, time=None, kind=1, step=5, end=False, **changes):
    values = [0] * 51
    for index, value in {0: seq, 1: 2, 2: kind, 3: 1, 4: 1 if end else 2, 5: 66 if end else 0,
                         7: step, 8: 3743518, 9: 4913830 if kind == 1 else -4913830 if kind == 2 else 244,
                         10: 1229, 13: seq * 100, 14: seq * 100 if time is None else time,
                         18: 2, 19: 1, 37: 20260522, 38: 205635497}.items():
        values[index] = value
    for key, value in changes.items():
        values[int(key)] = value
    return ",".join(map(str, values)) + "\n"


def folder_source(tmp_path, rows=None):
    channel = tmp_path / "job" / "M01Ch001[001]"
    restore = channel / "Restore"
    restore.mkdir(parents=True)
    first = restore / "ch01_SaveData0001.csv"
    first.write_text(rows or row(1) + row(2), encoding="utf-8")
    return discover_pne_sources(channel)[0], first


def test_folder_units_are_small_cell_si_and_all_source_integers_are_retained(tmp_path):
    source, raw = folder_source(tmp_path, row(1) + row(2, kind=3, step=6))
    before = raw.read_bytes()
    batches = list(iter_pne_batches(source, batch_size=1))
    frame = pd.concat(batches, ignore_index=True)
    assert frame.loc[0, "current_a"] == pytest.approx(.004913830)
    assert frame.loc[0, "capacity_ah"] == pytest.approx(.000001229)
    assert frame.loc[0, "voltage_v"] == pytest.approx(3.743518)
    assert frame.loc[0, "time_s"] == 1.
    assert frame.loc[1, "current_a"] == 244 / 1e9
    assert frame.loc[0, "datetime_local"] == "2026-05-22T20:56:35.497"
    assert set(RAW_COLUMNS).issubset(frame)
    assert frame.loc[0, "pne_raw_09"] == 4913830
    assert frame["mode"].tolist() == ["unknown", "unknown"]
    assert "cell_id" not in frame and "soc_pct" not in frame and "temperature_c" not in frame
    assert source["members"][0]["sha256"] == hashlib.sha256(before).hexdigest()
    assert frame.source_hash.eq(hashlib.sha256(before).hexdigest()).all()
    assert raw.read_bytes() == before


def test_fragment_order_record_index_member_row_and_step_boundary_survive_batches(tmp_path):
    source, first = folder_source(tmp_path, row(1) + "\n" + row(2, end=True))
    (first.parent / "ch01_SaveData0010.csv").write_text(row(5) + row(6), encoding="utf-8")
    (first.parent / "ch01_SaveData0002.csv").write_text(row(3) + row(4), encoding="utf-8")
    source = discover_pne_sources(first.parent.parent)[0]
    frame = pd.concat(iter_pne_batches(source, batch_size=1), ignore_index=True)
    assert frame.data_seq.tolist() == frame.record_index.tolist() == [1, 2, 3, 4, 5, 6]
    assert frame.source_row.tolist() == [1, 3, 1, 2, 1, 2]
    assert frame.segment_id.tolist() == [1, 1, 2, 2, 2, 2]
    assert frame.loc[1, "step_end"] and frame.loc[1, "end_flag"] == "current_end"
    assert len(frame.source_member.unique()) == 3 and len(frame.source_hash.unique()) == 3


def test_zip_members_stream_without_extraction_or_duplicate_binary_import(tmp_path):
    path = tmp_path / "raw.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("실험/M02Ch065[065]/Restore/ch65_SaveData0002.csv", row(3))
        archive.writestr("실험/M02Ch065[065]/Restore/ch65_SaveData0001.csv", row(1) + row(2))
        for name in ("source.cyc", "source_org.cyc", "converted_raw.csv", "source_StepEnd.csv"):
            archive.writestr("실험/M02Ch065[065]/" + name, "never read as raw")
    before = path.read_bytes()
    sources = discover_pne_sources(path)
    assert len(sources) == 1 and sources[0]["channel_id"] == "M02Ch065[065]"
    assert sources[0]["member_count"] == 2 and sources[0]["module_number"] == 2
    frame = pd.concat(iter_pne_batches(sources[0], batch_size=2), ignore_index=True)
    assert frame.record_index.tolist() == [1, 2, 3]
    assert frame.container_sha256.eq(hashlib.sha256(before).hexdigest()).all()
    assert frame.source_member.str.startswith("실험/").all()
    assert list(tmp_path.iterdir()) == [path] and path.read_bytes() == before


def test_zip_name_cp949_recovery_preserves_original_and_utf8_names():
    intended = "실험/구동압/M01Ch001[001]/Restore/ch01_SaveData0001.csv"
    original = intended.encode("cp949").decode("cp437")
    assert original != intended and decode_member_name(original) == intended
    assert decode_member_name(intended, 0x800) == intended


def test_global_directory_and_module_local_restore_channel_are_distinct(tmp_path):
    archive = tmp_path / "module2.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("M02Ch065[065]/Restore/ch01_SaveData0001.csv", row(1))
    source = discover_pne_sources(archive)[0]
    assert source["channel_number"] == 65 and source["restore_channel_number"] == 1
    frame = next(iter_pne_batches(source))
    assert frame.loc[0, "channel_id"] == "M02Ch065[065]" and frame.loc[0, "restore_channel_number"] == 1


@pytest.mark.parametrize("bad", ["1,2,3\n", row(1).replace(",4913830,", ",,", 1), row(1).replace(",4913830,", ",1.5,", 1), row(1, **{"50": 2**63})])
def test_unknown_or_partial_schema_never_gets_zero_filled(tmp_path, bad):
    source, _ = folder_source(tmp_path, bad)
    with pytest.raises(ValueError, match="51 nonblank|int64"):
        list(iter_pne_batches(source))


@pytest.mark.parametrize("second,match", [(row(1), "DataSeq"), (row(2, time=0), "time"), (row(2, **{"8": 20000000}), "Voltage")])
def test_sequence_clock_and_voltage_contracts_fail_without_repair(tmp_path, second, match):
    source, first = folder_source(tmp_path, row(1))
    (first.parent / "ch01_SaveData0002.csv").write_text(second, encoding="utf-8")
    source = discover_pne_sources(first.parent.parent)[0]
    with pytest.raises(ValueError, match=match):
        list(iter_pne_batches(source, batch_size=1))


def test_sign_conflicts_are_preserved_and_flagged(tmp_path):
    source, _ = folder_source(tmp_path, row(1, **{"9": -4913830}))
    frame = next(iter_pne_batches(source))
    assert frame.loc[0, "current_a"] < 0 and not frame.loc[0, "current_direction_consistent"]


def test_duplicate_fragment_and_channel_mismatch_rejected(tmp_path):
    source, first = folder_source(tmp_path)
    (first.parent / "ch01_SaveData1.csv").write_text(row(3), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        discover_pne_sources(first.parent.parent)
    (first.parent / "ch01_SaveData1.csv").unlink()
    (first.parent / "ch02_SaveData2.csv").write_text(row(3), encoding="utf-8")
    with pytest.raises(ValueError, match="mismatch"):
        discover_pne_sources(first.parent.parent)


def test_changed_folder_or_archive_is_not_read_under_old_identity(tmp_path):
    source, first = folder_source(tmp_path)
    first.write_text(row(1) + row(2) + row(3), encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        list(iter_pne_batches(source))
    archive = tmp_path / "raw.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("M01Ch001[001]/Restore/ch01_SaveData0001.csv", row(1))
    source = discover_pne_sources(archive)[0]
    with zipfile.ZipFile(archive, "a") as z:
        z.writestr("metadata.txt", "changed")
    with pytest.raises(ValueError, match="changed"):
        list(iter_pne_batches(source))


def test_schedule_hash_protocol_is_explicit_and_ambiguous_content_is_unassigned(tmp_path):
    source, first = folder_source(tmp_path)
    channel = first.parent.parent
    (channel / "program.sch").write_bytes(b"exact binary schedule")
    (channel / "duplicate.sch").write_bytes(b"exact binary schedule")
    source = discover_pne_sources(channel)[0]
    assert source["protocol_key"] == "pne_sch_sha256:" + hashlib.sha256(b"exact binary schedule").hexdigest()
    assert next(iter_pne_batches(source)).protocol_key.eq(source["protocol_key"]).all()
    (channel / "duplicate.sch").write_bytes(b"different schedule")
    source = discover_pne_sources(channel)[0]
    assert source["protocol_key"] is None and source["protocol_status"] == "conflicting_schedule_contents"


def test_schema_must_be_explicit_and_iterator_batch_size_is_bounded(tmp_path):
    source, _ = folder_source(tmp_path)
    invalid = copy.deepcopy(source)
    invalid["schema"] = "different_range"
    with pytest.raises(ValueError, match="schema"):
        list(iter_pne_batches(invalid))
    with pytest.raises(ValueError, match="batch_size"):
        list(iter_pne_batches(source, batch_size=0))


def test_unsafe_archive_path_is_not_extracted_or_accepted(tmp_path):
    archive = tmp_path / "raw.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../M01Ch001[001]/Restore/ch01_SaveData0001.csv", row(1))
    with pytest.raises(ValueError, match="Unsafe"):
        discover_pne_sources(archive)
