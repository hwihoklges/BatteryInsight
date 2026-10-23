"""Content-addressed local copies keep slide evidence usable after moves."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import uuid

from .library import file_hash


def asset_path(project, document):
    sha, kind = document.get("sha256", ""), document.get("format", "")
    if not re.fullmatch(r"[0-9a-f]{64}", sha) or kind not in {"pptx", "pdf", "xlsx", "png", "jpg", "jpeg", "tif", "tiff", "bmp", "webp"}:
        raise ValueError("지원하는 문서 SHA와 형식이 필요합니다.")
    return Path(project).resolve() / "knowledge/documents" / (sha + "." + kind)


def save_document_copy(project, source, document):
    destination = asset_path(project, document)
    if destination.exists():
        if file_hash(destination) != document["sha256"]:
            raise ValueError("보관된 장표 사본의 해시가 다릅니다. 원본 확인 후 사본을 복구하세요.")
        return destination
    source = Path(source).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
    digest, length = hashlib.sha256(), 0
    try:
        with source.open("rb") as inp, temporary.open("xb") as out:
            for chunk in iter(lambda: inp.read(1024 * 1024), b""):
                length += len(chunk)
                if length > 100 * 1024 * 1024:
                    raise ValueError("장표 보관 한도 100 MB를 초과했습니다.")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != document["sha256"]:
            raise ValueError("장표가 추출 후 변경됐습니다. 안정된 원본을 다시 등록하세요.")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def document_proof(project, document):
    """Prefer the immutable project copy; never fetch a remote reference."""
    try:
        archived = asset_path(project, document)
    except ValueError:
        return {"valid": False, "archived": False, "path": None, "uri": None, "reason": "document_format_unsupported"}
    candidates = [archived]
    if document.get("source_path"):
        candidates.append(Path(document["source_path"]))
    for path in candidates:
        if path.is_file():
            expected_bytes = document.get("metadata", {}).get("file_bytes")
            valid = (expected_bytes is None or path.stat().st_size == expected_bytes) and file_hash(path) == document["sha256"]
            # A damaged archive is visible as damaged; do not silently hide it
            # with a different copy from a machine-specific original location.
            return {"valid": valid, "archived": path == archived, "path": str(path),
                    "uri": path.resolve().as_uri(), "reason": None if valid else "document_hash_changed"}
    return {"valid": False, "archived": False, "path": None, "uri": None, "reason": "document_copy_missing"}
