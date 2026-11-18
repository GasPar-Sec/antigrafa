"""Fixed-size chunk index (ordered list of digests) and backup manifest."""

from __future__ import annotations

import json
import os
import struct
from pathlib import Path
from typing import Iterable

FIDX_MAGIC = b"AGFIDX01"
_FIDX_HEAD = struct.Struct("<8sQI")  # magic, total size, chunk size
DIGEST_SIZE = 32


class IndexCorrupted(Exception):
    pass


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_index(path: Path, size: int, chunk_size: int, digests: Iterable[bytes]) -> None:
    body = b"".join(digests)
    if len(body) % DIGEST_SIZE:
        raise ValueError("invalid digest size")
    _atomic_write(Path(path), _FIDX_HEAD.pack(FIDX_MAGIC, size, chunk_size) + body)


def read_index(path: Path) -> tuple[int, int, list[bytes]]:
    raw = Path(path).read_bytes()
    if len(raw) < _FIDX_HEAD.size:
        raise IndexCorrupted(f"{path}: truncated index")
    magic, size, chunk_size = _FIDX_HEAD.unpack_from(raw)
    if magic != FIDX_MAGIC:
        raise IndexCorrupted(f"{path}: not an Antigrafa index")
    body = raw[_FIDX_HEAD.size :]
    if len(body) % DIGEST_SIZE:
        raise IndexCorrupted(f"{path}: invalid length")
    digests = [body[i : i + DIGEST_SIZE] for i in range(0, len(body), DIGEST_SIZE)]
    return size, chunk_size, digests


MANIFEST_NAME = "manifest.json"


def write_manifest(directory: Path, manifest: dict) -> None:
    """The manifest is written last: its presence means the backup is complete."""
    data = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
    _atomic_write(Path(directory) / MANIFEST_NAME, data)


def read_manifest(directory: Path) -> dict:
    return json.loads((Path(directory) / MANIFEST_NAME).read_text())
