"""Content-addressed chunk store (SHA-256), deduplicated across all backups.

On-disk layout: ``<root>/.chunks/<first 4 hex>/<sha256 hex>``.
Each file starts with a header (magic + encoding) followed by the data, either raw
or zstd-compressed. The digest is always computed over the raw data.
"""

from __future__ import annotations

import hashlib
import os
import struct
import threading
from pathlib import Path

from . import compress as _compress

MAGIC = b"AGCHUNK1"
ENC_RAW = 0
ENC_ZSTD = 1
_HEADER = struct.Struct("<8sB")


class ChunkCorrupted(Exception):
    pass


def digest_of(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def encode_chunk(data: bytes, level: int) -> bytes:
    """Compress the chunk; keep it raw when compression does not make it smaller."""
    packed = _compress.compress(data, level)
    if len(packed) < len(data):
        return _HEADER.pack(MAGIC, ENC_ZSTD) + packed
    return _HEADER.pack(MAGIC, ENC_RAW) + data


def decode_chunk(blob: bytes) -> bytes:
    if len(blob) < _HEADER.size:
        raise ChunkCorrupted("truncated chunk")
    magic, enc = _HEADER.unpack_from(blob)
    if magic != MAGIC:
        raise ChunkCorrupted("invalid chunk header")
    payload = blob[_HEADER.size :]
    if enc == ENC_RAW:
        return payload
    if enc == ENC_ZSTD:
        return _compress.decompress(payload)
    raise ChunkCorrupted(f"unknown encoding: {enc}")


class ChunkStore:
    def __init__(self, root: Path, sync_each: bool = False) -> None:
        self.root = Path(root)
        self.chunk_dir = self.root / ".chunks"
        self.sync_each = sync_each

    def init(self) -> None:
        self.chunk_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, digest: bytes) -> Path:
        h = digest.hex()
        return self.chunk_dir / h[:4] / h

    def has(self, digest: bytes) -> bool:
        return self.path_for(digest).exists()

    def write_blob(self, digest: bytes, blob: bytes) -> int:
        """Write an already-encoded chunk. Return the number of bytes written (0 if it existed)."""
        path = self.path_for(digest)
        if path.exists():
            return 0
        path.parent.mkdir(exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(blob)
                if self.sync_each:
                    f.flush()
                    os.fsync(f.fileno())
            os.replace(tmp, path)  # atomic: a chunk is either complete or absent
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return len(blob)

    def insert(self, data: bytes, level: int = 3) -> bytes:
        digest = digest_of(data)
        if not self.has(digest):
            self.write_blob(digest, encode_chunk(data, level))
        return digest

    def read(self, digest: bytes) -> bytes:
        """Read a chunk and check that it matches its digest."""
        data = decode_chunk(self.path_for(digest).read_bytes())
        if digest_of(data) != digest:
            raise ChunkCorrupted(f"digest mismatch for {digest.hex()}")
        return data
