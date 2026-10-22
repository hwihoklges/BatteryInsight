"""Folder-only, bounded-memory native CYC to Parquet conversion.

No experiment classification, CSV intermediate, or indicator calculations.
Each input file remains a separate file; native backups are never silently removed.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
import hashlib
import json
import os
from pathlib import Path
import uuid
from urllib.parse import quote

FORMAT_VERSION = 1
STORAGE = {"compression": "zstd", "compression_level": 19, "batch_rows": 65536,
           "row_filter": None, "downsampling": None, "precision_reduction": False}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def default_output(source):
    source = Path(source).resolve()
    return source.parent / (source.name + "_parquet")


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(data)
    return value.hexdigest()


def _stamp(path):
    stat = Path(path).stat()
    return [stat.st_size, stat.st_mtime_ns]


def _validate_paths(source, output):
    if not source.is_dir():
        raise ValueError(f"데이터 폴더를 찾을 수 없습니다: {source}")
    if output == source or output.is_relative_to(source):
        raise ValueError("저장 폴더는 원본 폴더 바깥에 있어야 합니다. 데이터 폴더를 선택하세요.")
    if output.exists() and not output.is_dir():
        raise ValueError("저장 위치에 같은 이름의 파일이 있습니다.")


def discover(source):
    paths, skipped = [], []
    for root, directories, files in os.walk(source, followlinks=False):
        retained = []
        for name in directories:
            path = Path(root) / name
            if path.is_symlink() or path.is_junction():
                skipped.append(str(path.relative_to(source)))
            else:
                retained.append(name)
        directories[:] = sorted(retained, key=str.casefold)
        for name in sorted(files, key=str.casefold):
            path = Path(root) / name
            if path.suffix.casefold() == ".cyc":
                if path.is_symlink():
                    skipped.append(str(path.relative_to(source)))
                else:
                    paths.append(path)
    return paths, skipped


@contextmanager
def _lock(output):
    output.mkdir(parents=True, exist_ok=True)
    stream = (output / ".converter.lock").open("a+b")
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    locked = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            raise RuntimeError("다른 변환 작업이 이 저장 폴더를 사용 중입니다.") from exc
        yield
    finally:
        if locked:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def _code_hash():
    root = Path(__file__).parent
    value = hashlib.sha256()
    for name in ("reader.py", "service.py"):
        value.update(name.encode())
        value.update((root / name).read_bytes())
    return value.hexdigest()


def _old_manifest(output):
    path = output / "conversion_manifest.json"
    if not path.exists():
        if (output / "result.html").exists():
            raise ValueError("저장 폴더에 다른 result.html 파일이 있습니다. 다른 저장 위치를 선택하세요.")
        return {}
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != FORMAT_VERSION or manifest.get("converter") != "cyc_parquet":
        raise ValueError("이 저장 폴더는 다른 프로그램에서 사용 중입니다. 다른 저장 위치를 선택하세요.")
    return manifest


def _safe_destination(output, relative):
    path = output / "parquet" / Path(relative).with_suffix(".parquet")
    path = path.resolve()
    if not path.is_relative_to(output.resolve()):
        raise ValueError("저장 경로가 출력 폴더 밖으로 연결되어 있습니다.")
    return path


def _write_one(path, output, relative, code_hash, previous):
    import pyarrow as pa
    import pyarrow.parquet as pq
    from . import reader

    info = reader.inspect_file(path)
    cts = Path(info["cts_path"]) if info.get("cts_path") else None
    sources = [path] + ([cts] if cts else [])
    stamps = {str(p): _stamp(p) for p in sources}
    hashes = {str(p): digest(p) for p in sources}
    identity = {"cyc_sha256": hashes[str(path)], "cts_sha256": hashes.get(str(cts)),
                "code_sha256": code_hash, "storage": STORAGE}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    destination = _safe_destination(output, relative)
    artifact = destination.relative_to(output).as_posix()
    common = {"source": relative, "parquet": artifact, "source_sha256": hashes[str(path)],
              "source_bytes": path.stat().st_size, "record_count": info["record_count"],
              "fingerprint": fingerprint, "cts_source": cts.name if cts else None,
              "cts_sha256": hashes.get(str(cts)), "layout": info.get("layout"),
              "units": info.get("units"), "warnings": info.get("warnings", [])}
    owner = previous if previous and previous.get("status") == "complete" else (previous or {}).get("last_complete")
    if owner and owner.get("fingerprint") == fingerprint and destination.is_file():
        if owner.get("parquet_sha256") == digest(destination) and pq.ParquetFile(destination).metadata.num_rows == info["record_count"]:
            if any(_stamp(p) != stamps[str(p)] for p in sources):
                raise RuntimeError("원본이 확인 중에 변경됐습니다. 기록이 완료된 파일로 다시 실행하세요.")
            return {**owner, **common, "reused": True}
    if destination.exists() and not (owner and owner.get("source") == relative and owner.get("parquet") == artifact):
        raise ValueError(f"같은 이름의 기존 파일을 덮어쓸 수 없습니다: {artifact}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    writer, rows, matched = None, 0, 0
    cycle_min = cycle_max = None
    try:
        for batch in reader.iter_batches(path, batch_rows=STORAGE["batch_rows"]):
            table = pa.Table.from_batches([batch]) if isinstance(batch, pa.RecordBatch) else batch
            if writer is None:
                metadata = dict(table.schema.metadata or {})
                metadata.update({b"converter": b"cyc_parquet", b"source_relative_path": relative.encode("utf-8"),
                                 b"source_sha256": hashes[str(path)].encode(), b"native_values_preserved": b"true",
                                 b"storage": json.dumps(STORAGE).encode()})
                schema = table.schema.with_metadata(metadata)
                writer = pq.ParquetWriter(temporary, schema, compression="zstd", compression_level=19,
                    use_dictionary=[name for name in ("cycle", "step", "segment_id") if name in table.column_names],
                    use_byte_stream_split=[field.name for field in schema if pa.types.is_floating(field.type)],
                    data_page_version="2.0", write_statistics=True)
            writer.write_table(table, row_group_size=STORAGE["batch_rows"])
            rows += table.num_rows
            if "cts_matched" in table.column_names:
                import pyarrow.compute as pc
                matched += pc.sum(table["cts_matched"]).as_py() or 0
            if "cycle" in table.column_names:
                import pyarrow.compute as pc
                bounds = pc.min_max(table["cycle"]).as_py()
                if bounds["min"] is not None:
                    cycle_min = bounds["min"] if cycle_min is None else min(cycle_min, bounds["min"])
                    cycle_max = bounds["max"] if cycle_max is None else max(cycle_max, bounds["max"])
        if writer is None:
            raise ValueError("저장할 완전한 CYC 기록이 없습니다.")
        writer.close()
        writer = None
        if rows != info["record_count"] or pq.ParquetFile(temporary).metadata.num_rows != info["record_count"]:
            raise RuntimeError("원본 기록 수와 Parquet 저장 행 수가 일치하지 않습니다.")
        if any(_stamp(p) != stamps[str(p)] or digest(p) != hashes[str(p)] for p in sources):
            raise RuntimeError("원본이 변환 중에 변경됐습니다. 기록이 완료된 파일로 다시 실행하세요.")
        file_hash = digest(temporary)
        temporary.replace(destination)
        return {**common, "status": "complete", "rows": rows, "reused": False,
                "parquet_bytes": destination.stat().st_size, "parquet_sha256": file_hash,
                "cts_matched_rows": matched, "cts_unmatched_rows": rows - matched,
                "cycle_min": cycle_min, "cycle_max": cycle_max}
    finally:
        if writer is not None:
            writer.close()
        temporary.unlink(missing_ok=True)


def convert_folder(source, output=None, *, progress=None):
    import pyarrow as pa
    from . import __version__
    source = Path(source).resolve()
    output = Path(output).resolve() if output is not None else default_output(source)
    _validate_paths(source, output)
    paths, skipped = discover(source)
    if not paths:
        raise ValueError("선택한 폴더와 하위 폴더에 .cyc 파일이 없습니다.")
    pa.set_cpu_count(1)
    pa.set_io_thread_count(1)
    with _lock(output):
        previous = _old_manifest(output)
        if previous and Path(previous["input"]).resolve() != source:
            raise ValueError("이 저장 폴더는 다른 원본 폴더의 결과입니다. 다른 저장 위치를 선택하세요.")
        old = {item["source"]: item for item in previous.get("files", [])}
        code_hash = _code_hash()
        log_dir = output / "logs"
        log_dir.mkdir(exist_ok=True)
        log_path = log_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8] + ".jsonl")
        def emit(event):
            entry = {"at": datetime.now(timezone.utc).isoformat(), **event}
            with log_path.open("a", encoding="utf-8") as log:
                log.write(json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n")
            if progress:
                progress(entry)
        emit({"message": f"CYC 파일 {len(paths):,}개를 찾았습니다. 전체 기록을 변환합니다.", "total": len(paths)})
        files = []
        for index, path in enumerate(paths, 1):
            relative = path.relative_to(source).as_posix()
            emit({"message": f"읽는 중: {relative}", "path": relative, "total": len(paths), "index": index - 1})
            try:
                result = _write_one(path, output, relative, code_hash, old.get(relative))
            except Exception as exc:
                result = {"source": relative, "status": "failed", "error": str(exc), "error_type": type(exc).__name__, "reused": False}
                prior = old.get(relative, {})
                last_complete = prior if prior.get("status") == "complete" else prior.get("last_complete")
                if last_complete:
                    result["last_complete"] = last_complete
            files.append(result)
            emit({"index": index, "total": len(paths), "path": relative, "status": result["status"],
                  "rows": result.get("rows", 0), "reused": result["reused"],
                  "message": f"{relative}: " + (f"{result['rows']:,}행 {'재사용' if result['reused'] else '완료'}" if result["status"] == "complete" else result["error"])})
        complete = [item for item in files if item["status"] == "complete"]
        removed = sorted(set(old) - {item["source"] for item in files})
        summary = {"files_total": len(files), "complete": len(complete), "failed": len(files) - len(complete),
                   "reused": sum(item["reused"] for item in complete), "rows": sum(item["rows"] for item in complete),
                   "source_bytes": sum(item["source_bytes"] for item in complete),
                   "parquet_bytes": sum(item["parquet_bytes"] for item in complete),
                   "cts_unmatched_rows": sum(item["cts_unmatched_rows"] for item in complete)}
        status = "success" if not summary["failed"] else "partial_failure" if complete else "failed"
        result = {"schema_version": FORMAT_VERSION, "converter": "cyc_parquet", "version": __version__,
                  "status": status, "input": str(source), "output": str(output),
                  "generated_at": datetime.now(timezone.utc).isoformat(), "storage": STORAGE,
                  "summary": summary, "files": files, "skipped_links": skipped,
                  "not_present_in_current_input": removed,
                  "native_units": "Voltage/capacity native numeric values preserved; units not inferred.",
                  "original_files_modified": False, "metrics_calculated": False}
        atomic_json(output / "conversion_manifest.json", result)
        _report(output, result)
        emit({"message": f"완료 {summary['complete']} / {summary['files_total']} 파일 · {summary['rows']:,}행 · 실패 {summary['failed']}", "summary": summary})
        return result


def _report(output, manifest):
    summary = manifest["summary"]
    rows = []
    for item in manifest["files"]:
        if item["status"] == "complete":
            target = '<a href="' + escape(quote(item["parquet"], safe="/"), quote=True) + '">Parquet</a>'
            details = "재사용" if item["reused"] else "완료"
            details += f" · CTS 연결 {item['cts_matched_rows']:,}/{item['rows']:,}행"
        else:
            target, details = "", escape(item["error"])
        rows.append(f"<tr><td>{escape(item['source'])}</td><td>{item.get('rows', 0):,}</td><td>{target}</td><td>{details}</td></tr>")
    html = f'''<!doctype html><html lang="ko"><meta charset="utf-8"><title>CYC → Parquet</title>
<style>body{{max-width:1100px;margin:40px auto;padding:0 24px;background:#f3f7f7;color:#173b42;font:16px/1.8 system-ui,sans-serif}}table{{width:100%;border-collapse:collapse;background:white}}td,th{{padding:12px;border-bottom:1px solid #dce6e7;text-align:left}}a{{color:#087b83}}code{{background:#e2eeee;padding:3px}}</style>
<h1>CYC → Parquet 변환 결과</h1><p>완료 {summary['complete']} / {summary['files_total']} 파일 · 실패 {summary['failed']} · 재사용 {summary['reused']}<br>완료 파일의 전체 기록 <b>{summary['rows']:,}행</b> 보존 · {summary['parquet_bytes']/1e6:.2f} MB</p>
<p><code>step_time_s</code>: 원본 스텝 경과 시간(초) · <code>voltage_native</code>/<code>capacity_native</code>: CYC에 기록된 전압·용량 숫자 그대로. 단위는 자동 추정하거나 V/Ah로 환산하지 않습니다.</p>
<p>같은 이름의 CTS가 있으면 사이클·스텝·전체 경과 시간을 연결합니다. CTS가 없거나 연결되지 않는 기록도 보존하며 해당 식별값은 null입니다. 모든 .cyc 파일을 각각 저장하므로 _org 등 사본도 별도 결과로 남습니다.</p>
<p>원본 변경 없음 · CSV 중간 파일 없음 · 지표 계산 없음 · 다운샘플링 없음 · Zstandard 압축 19</p>
<table><tr><th>원본</th><th>저장 행</th><th>파일</th><th>결과</th></tr>{''.join(rows)}</table>
<p><a href="conversion_manifest.json">출처·무결성·단위·경고 기록</a> / 실행 로그: logs/</p>
<p>이전 실행에 있었으나 현재 원본 폴더에 없는 파일: {len(manifest['not_present_in_current_input'])}개. 이전 Parquet 파일은 삭제하지 않습니다. 현재 목록은 이번 실행의 파일만 표시합니다.</p></html>'''
    path = output / "result.html"
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(html, encoding="utf-8")
    temporary.replace(path)
