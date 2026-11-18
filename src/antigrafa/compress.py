"""zstd compression: standard library (Python 3.14+) or the ``zstandard`` package."""

from __future__ import annotations

try:
    from compression import zstd as _zstd

    def compress(data: bytes, level: int) -> bytes:
        return _zstd.compress(data, level=level)

    def decompress(data: bytes) -> bytes:
        return _zstd.decompress(data)

except ImportError:  # Python < 3.14
    import zstandard as _zstandard

    def compress(data: bytes, level: int) -> bytes:
        return _zstandard.ZstdCompressor(level=level).compress(data)

    def decompress(data: bytes) -> bytes:
        return _zstandard.ZstdDecompressor().decompress(data)
