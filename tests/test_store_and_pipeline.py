import io
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from antigrafa.adaptive import WriteController, WriteTuning
from antigrafa.chunkstore import ChunkCorrupted, ChunkStore, decode_chunk, encode_chunk
from antigrafa.index import read_index, write_index
from antigrafa.pipeline import backup_stream, read_full, restore_stream

CHUNK = 64 * 1024


@pytest.fixture
def store(tmp_path):
    s = ChunkStore(tmp_path)
    s.init()
    return s


def test_encode_roundtrip_compressible_and_random():
    text = b"proxmox " * 10000
    assert decode_chunk(encode_chunk(text, 3)) == text
    rnd = os.urandom(5000)  # incompressible: stored raw
    blob = encode_chunk(rnd, 3)
    assert decode_chunk(blob) == rnd
    assert len(blob) == len(rnd) + 9


def test_store_dedup_and_corruption(store):
    d1 = store.insert(b"a" * 1000)
    d2 = store.insert(b"a" * 1000)
    assert d1 == d2
    assert store.read(d1) == b"a" * 1000
    path = store.path_for(d1)
    path.write_bytes(encode_chunk(b"b" * 1000, 3))
    with pytest.raises(ChunkCorrupted):
        store.read(d1)


def test_index_roundtrip(tmp_path):
    digests = [bytes([i]) * 32 for i in range(5)]
    write_index(tmp_path / "x.fidx", 12345, CHUNK, digests)
    assert read_index(tmp_path / "x.fidx") == (12345, CHUNK, digests)


class Trickle(io.RawIOBase):
    """Simulates a pipe that returns smaller pieces than requested."""

    def __init__(self, data, piece=7000):
        self._buf = io.BytesIO(data)
        self._piece = piece

    def readable(self):
        return True

    def read(self, n=-1):
        return self._buf.read(min(n, self._piece) if n > 0 else self._piece)


def _disk_image():
    # Fake disk: zero, repeated and random regions, unaligned size.
    return bytes(CHUNK * 3) + os.urandom(CHUNK * 2) + (b"xyz" * CHUNK)[: CHUNK * 2] + bytes(CHUNK * 3) + os.urandom(1234)


def test_read_full_on_pipe():
    data = os.urandom(50000)
    stream = Trickle(data)
    assert read_full(stream, 30000) == data[:30000]
    assert read_full(stream, 30000) == data[30000:]
    assert read_full(stream, 30000) == b""


def test_backup_and_restore_roundtrip(store, tmp_path):
    image = _disk_image()
    ctl = WriteController(WriteTuning(max_concurrency=4))
    with ThreadPoolExecutor(4) as pool:
        digests, stats = backup_stream(Trickle(image), store, ctl, pool, chunk_size=CHUNK)
        assert stats.bytes_read == len(image)
        assert stats.chunks == len(digests) == 11
        # 3+3 identical zero chunks and 2 "xyz" chunks: dedup within one stream
        assert stats.new_chunks < stats.chunks

        # Second pass: everything exists already, nothing is written.
        digests2, stats2 = backup_stream(Trickle(image), store, ctl, pool, chunk_size=CHUNK)
        assert digests2 == digests
        assert stats2.new_chunks == 0 and stats2.stored_bytes == 0
    assert ctl.limiter.active == 0

    out = tmp_path / "restored.img"
    with open(out, "wb") as f:
        restore_stream(store, digests, f, len(image))
    assert out.read_bytes() == image
    # Zero regions are restored as holes (sparse file).
    assert os.stat(out).st_blocks * 512 < len(image)


def test_backup_propagates_writer_errors(store):
    class Broken(ChunkStore):
        def write_blob(self, digest, blob):
            raise OSError("disk full")

    broken = Broken(store.root)
    ctl = WriteController()
    with ThreadPoolExecutor(2) as pool, pytest.raises(OSError, match="disk full"):
        backup_stream(io.BytesIO(os.urandom(CHUNK * 4)), broken, ctl, pool, chunk_size=CHUNK)
    assert ctl.limiter.active == 0
