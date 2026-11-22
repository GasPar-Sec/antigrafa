import io
import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from antigrafa.adaptive import WriteController, WriteTuning
from antigrafa.chunkstore import ChunkCorrupted, ChunkStore, decode_chunk, encode_chunk
from antigrafa.index import read_index, write_index
from antigrafa.pipeline import backup_delta, backup_stream, read_full, restore_stream

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
    # Zero regions are skipped, not written: only the non-zero chunks reach the file
    # (checked on every platform).
    written = io.BytesIO()
    restore_stream(store, digests, written, len(image))
    recorder = _WriteRecorder()
    restore_stream(store, digests, recorder, len(image))
    assert recorder.written == len(image) - 6 * CHUNK
    assert written.getvalue() == image
    # Zero regions are restored as holes (sparse file), where the OS reports blocks.
    if hasattr(os.stat_result, "st_blocks"):
        assert os.stat(out).st_blocks * 512 < len(image)


class _WriteRecorder(io.BytesIO):
    """Seekable output that counts the bytes actually written."""

    def __init__(self):
        super().__init__()
        self.written = 0

    def write(self, data):
        self.written += len(data)
        return super().write(data)


def test_backup_propagates_writer_errors(store):
    class Broken(ChunkStore):
        def write_blob(self, digest, blob):
            raise OSError("disk full")

    broken = Broken(store.root)
    ctl = WriteController()
    with ThreadPoolExecutor(2) as pool, pytest.raises(OSError, match="disk full"):
        backup_stream(io.BytesIO(os.urandom(CHUNK * 4)), broken, ctl, pool, chunk_size=CHUNK)
    assert ctl.limiter.active == 0


@pytest.mark.parametrize(
    ("blob", "message"),
    [
        (b"short", "truncated chunk"),
        (b"NOTCHUNK\x00data", "invalid chunk header"),
        (b"AGCHUNK1\x07data", "unknown encoding: 7"),
    ],
)
def test_decode_rejects_damaged_chunks(blob, message):
    with pytest.raises(ChunkCorrupted, match=message):
        decode_chunk(blob)


def test_write_blob_existing_chunk_writes_nothing(store):
    digest = store.insert(b"once")
    assert store.write_blob(digest, encode_chunk(b"once", 3)) == 0


def test_write_blob_with_fsync(tmp_path):
    synced = ChunkStore(tmp_path, sync_each=True)
    synced.init()
    digest = synced.insert(b"durable")
    assert synced.read(digest) == b"durable"


def test_concurrent_writer_of_same_chunk_wins_the_race(store, monkeypatch):
    """Windows refuses os.replace onto a chunk another writer just stored."""
    from antigrafa import chunkstore

    data = b"shared by two guests"
    blob = encode_chunk(data, 3)
    digest = chunkstore.digest_of(data)
    path = store.path_for(digest)

    def other_writer_first(src, dst):
        with open(dst, "wb") as f:
            f.write(blob)
        raise PermissionError(13, "Access is denied", str(dst))

    monkeypatch.setattr(chunkstore.os, "replace", other_writer_first)
    assert store.write_blob(digest, blob) == 0
    monkeypatch.undo()
    assert store.read(digest) == data
    assert [p.name for p in path.parent.iterdir()] == [path.name]  # temp file removed


def test_permission_error_without_chunk_is_raised(store, monkeypatch):
    from antigrafa import chunkstore

    def denied(src, dst):
        raise PermissionError(13, "Access is denied", str(dst))

    monkeypatch.setattr(chunkstore.os, "replace", denied)
    digest = chunkstore.digest_of(b"x")
    with pytest.raises(PermissionError):
        store.write_blob(digest, encode_chunk(b"x", 3))
    monkeypatch.undo()
    assert list(store.path_for(digest).parent.iterdir()) == []  # temp file removed


def test_other_write_errors_remove_temp_file(store, monkeypatch):
    from antigrafa import chunkstore

    def disk_full(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(chunkstore.os, "replace", disk_full)
    digest = chunkstore.digest_of(b"y")
    with pytest.raises(OSError, match="No space"):
        store.write_blob(digest, encode_chunk(b"y", 3))
    monkeypatch.undo()
    assert list(store.path_for(digest).parent.iterdir()) == []


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"tiny", "truncated index"),
        (b"NOTINDEX" + bytes(12), "not an Antigrafa index"),
        (b"AGFIDX01" + bytes(12) + b"odd", "invalid length"),
    ],
)
def test_read_index_rejects_damaged_files(tmp_path, raw, message):
    from antigrafa.index import IndexCorrupted

    path = tmp_path / "bad.fidx"
    path.write_bytes(raw)
    with pytest.raises(IndexCorrupted, match=message):
        read_index(path)


def test_write_index_rejects_bad_digests(tmp_path):
    with pytest.raises(ValueError, match="invalid digest size"):
        write_index(tmp_path / "x.fidx", 10, CHUNK, [b"short"])


def test_delta_backup_sends_and_stores_only_changes(store, tmp_path):
    from antigrafa.delta import MAGIC

    old = os.urandom(CHUNK * 4)
    ctl = WriteController(WriteTuning(max_concurrency=4))
    with ThreadPoolExecutor(4) as pool:
        reference, first = backup_stream(io.BytesIO(old), store, ctl, pool, chunk_size=CHUNK)
        assert first.sent_bytes == len(old) and first.unchanged_chunks == 0

        new = old[: 2 * CHUNK] + os.urandom(CHUNK) + old[3 * CHUNK :]
        changed = new[2 * CHUNK : 3 * CHUNK]
        from antigrafa.chunkstore import digest_of

        wire = io.BytesIO(
            MAGIC
            + b"S" + CHUNK.to_bytes(4, "big")
            + b"S" + CHUNK.to_bytes(4, "big")
            + b"D" + digest_of(changed) + CHUNK.to_bytes(4, "big") + changed
            + b"S" + CHUNK.to_bytes(4, "big")
            + b"E"
        )
        seen = []
        digests, stats = backup_delta(wire, reference, store, ctl, pool, on_read=seen.append)
    assert ctl.limiter.active == 0
    assert stats.bytes_read == len(new) and seen == [CHUNK] * 4
    assert stats.unchanged_chunks == 3 and stats.reused_chunks == 3
    assert stats.new_chunks == 1 and stats.sent_bytes == CHUNK
    assert digests[:2] == reference[:2] and digests[3] == reference[3]

    # A full image, as if every chunk had been read
    out = tmp_path / "restored.img"
    with open(out, "wb") as f:
        restore_stream(store, digests, f, len(new))
    assert out.read_bytes() == new


# --------------------------------------------------------------------------- parallel restore


def test_in_order_keeps_the_order_whatever_finishes_first():
    import random
    import time

    from antigrafa.pipeline import in_order

    def slow(i):
        time.sleep(random.random() / 100)
        return i * 10

    with ThreadPoolExecutor(4) as pool:
        assert list(in_order(pool, slow, range(40), ahead=8)) == [i * 10 for i in range(40)]


def test_in_order_stops_reading_when_the_consumer_stops():
    import threading

    from antigrafa.pipeline import in_order

    started = []
    gate = threading.Event()

    def read(i):
        started.append(i)
        if i == 0:
            gate.wait(5)
        return i

    with ThreadPoolExecutor(1) as pool:
        results = in_order(pool, read, range(1000), ahead=3)
        gate.set()
        assert next(results) == 0
        results.close()  # an error or a cancel on the writing side
    assert len(started) <= 4  # never the whole disk read for nothing


@pytest.mark.parametrize("workers", [1, 4])
def test_parallel_restore_gives_the_same_image(store, tmp_path, workers):
    image = os.urandom(CHUNK * 3) + bytes(CHUNK * 2) + os.urandom(CHUNK) + bytes(100)
    digests = [store.insert(image[i : i + CHUNK]) for i in range(0, len(image), CHUNK)]
    out = tmp_path / f"restored-{workers}.img"
    seen = []
    with open(out, "wb") as f:
        restore_stream(store, digests, f, len(image), on_chunk=seen.append, workers=workers, chunk_size=CHUNK)
    assert out.read_bytes() == image
    assert seen == [CHUNK] * 6 + [100]


def test_all_zero_chunks_are_known_by_their_digest_and_not_read(store, tmp_path):
    from antigrafa.chunkstore import digest_of

    zero = digest_of(bytes(CHUNK))
    data = os.urandom(CHUNK)
    digests = [zero, store.insert(data), zero]  # the zero chunk is not even in the store
    recorder = _WriteRecorder()
    restore_stream(store, digests, recorder, 3 * CHUNK, chunk_size=CHUNK)
    assert recorder.written == CHUNK  # holes for the zeros
    plain = io.BytesIO()
    restore_stream(store, digests, plain, 3 * CHUNK, sparse=False, chunk_size=CHUNK)
    assert plain.getvalue() == bytes(CHUNK) + data + bytes(CHUNK)


def test_restore_stops_on_a_damaged_chunk(store, tmp_path):
    good = store.insert(os.urandom(CHUNK))
    bad = store.insert(os.urandom(CHUNK))
    store.path_for(bad).write_bytes(b"garbage")
    with pytest.raises(ChunkCorrupted):
        restore_stream(store, [good, bad] + [good] * 50, io.BytesIO(), 52 * CHUNK, workers=4, chunk_size=CHUNK)
