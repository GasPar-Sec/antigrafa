"""Transfer of a stream (raw disk or tar archive) into the chunk store.

The calling thread reads and hashes; compressing and writing new chunks goes to a
shared pool, throttled by the ``WriteController``. When the destination slows down,
the reader waits, the SSH pipe fills up and the read on the node slows down by
itself. The VM never waits.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator, Sequence
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from typing import BinaryIO, Callable

from .adaptive import SourceThrottle, WriteController
from .chunkstore import ChunkStore, digest_of, encode_chunk
from .delta import NOT_READ, read_records

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024


@dataclass
class StreamStats:
    bytes_read: int = 0
    chunks: int = 0
    new_chunks: int = 0
    reused_chunks: int = 0  # already in the store (unchanged ones included)
    unchanged_chunks: int = 0  # same as the previous backup: not even sent
    skipped_chunks: int = 0  # of those, not even read on the source (incremental)
    sent_bytes: int = 0  # chunk data that crossed the network
    stored_bytes: int = 0
    elapsed: float = 0.0

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class _Pending:
    futures: list[Future] = field(default_factory=list)

    def reap(self, stats: StreamStats, block: bool = False) -> None:
        keep = []
        for fut in self.futures:
            if block or fut.done():
                stats.stored_bytes += fut.result()  # re-raises any worker error
            else:
                keep.append(fut)
        self.futures = keep

    def cancel(self, controller: WriteController) -> None:
        for fut in self.futures:
            # A task cancelled before it started will never give its slot back: do it
            # here, otherwise the limiter (shared by every backup) would leak.
            if fut.cancel():
                controller.limiter.release()


def read_full(stream: BinaryIO, size: int) -> bytes:
    """Read exactly ``size`` bytes, or fewer at end of stream (pipes return partial reads)."""
    parts = []
    remaining = size
    while remaining:
        buf = stream.read(remaining)
        if not buf:
            break
        parts.append(buf)
        remaining -= len(buf)
    return b"".join(parts)


def _chunks(stream: BinaryIO, chunk_size: int) -> Iterator[tuple[bytes, bytes, int]]:
    while True:
        data = read_full(stream, chunk_size)
        if not data:
            return
        yield digest_of(data), data, len(data)


def backup_stream(
    stream: BinaryIO,
    store: ChunkStore,
    controller: WriteController,
    executor: Executor,
    throttle: SourceThrottle | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    on_read: Callable[[int], None] | None = None,
) -> tuple[list[bytes], StreamStats]:
    """Back up a raw stream. ``on_read(n)`` is called after each chunk read (progress)."""
    return backup_records(_chunks(stream, chunk_size), store, controller, executor, throttle, on_read)


def backup_delta(
    stream: BinaryIO,
    reference: Sequence[bytes],
    store: ChunkStore,
    controller: WriteController,
    executor: Executor,
    throttle: SourceThrottle | None = None,
    on_read: Callable[[int], None] | None = None,
) -> tuple[list[bytes], StreamStats]:
    """Back up a stream hashed on the source host against ``reference`` (see ``delta``)."""
    return backup_records(read_records(stream, reference), store, controller, executor, throttle, on_read)


def backup_records(
    records: Iterable[tuple[bytes, bytes | None, int]],
    store: ChunkStore,
    controller: WriteController,
    executor: Executor,
    throttle: SourceThrottle | None = None,
    on_read: Callable[[int], None] | None = None,
) -> tuple[list[bytes], StreamStats]:
    """Store chunks given as (digest, data, length); data None: unchanged, already stored."""
    stats = StreamStats()
    digests: list[bytes] = []
    seen: set[bytes] = set()
    pending = _Pending()
    start = time.monotonic()

    def work(data: bytes, digest: bytes) -> int:
        try:
            t0 = time.perf_counter()
            blob = encode_chunk(data, controller.level)
            t1 = time.perf_counter()
            written = store.write_blob(digest, blob)
            t2 = time.perf_counter()
            controller.record(t1 - t0, t2 - t1, len(data), written)
            return written
        finally:
            controller.limiter.release()

    try:
        for digest, data, length in records:
            # A chunk the source read counts for its load, even unchanged; every chunk
            # counts for the progress
            skipped = data is NOT_READ
            if throttle is not None and not skipped:
                throttle.consume(length)
            if on_read is not None:
                on_read(length)
            digests.append(digest)
            stats.bytes_read += length
            stats.chunks += 1
            if data is None or skipped:  # unchanged since the previous backup, which stored it
                stats.reused_chunks += 1
                stats.unchanged_chunks += 1
                stats.skipped_chunks += skipped
                continue
            stats.sent_bytes += length
            if digest in seen or store.has(digest):
                stats.reused_chunks += 1
                continue
            seen.add(digest)
            stats.new_chunks += 1
            controller.limiter.acquire()  # blocks when the destination cannot keep up
            try:
                pending.futures.append(executor.submit(work, data, digest))
            except BaseException:
                controller.limiter.release()
                raise
            pending.reap(stats)
        pending.reap(stats, block=True)
    except BaseException:
        pending.cancel(controller)
        raise
    stats.elapsed = time.monotonic() - start
    return digests, stats


_ZEROS: dict[int, bytes] = {}


def restore_stream(
    store: ChunkStore,
    digests: list[bytes],
    out: BinaryIO,
    size: int,
    sparse: bool = True,
    on_chunk: Callable[[int], None] | None = None,
) -> None:
    """Rebuild the original stream. All-zero chunks become holes when the output allows it.

    ``on_chunk(n)`` is called after each chunk, written or skipped (progress).
    """
    sparse = sparse and out.seekable()
    for digest in digests:
        data = store.read(digest)
        zeros = _ZEROS.get(len(data)) or _ZEROS.setdefault(len(data), bytes(len(data)))
        if sparse and data == zeros:
            out.seek(len(data), 1)
        else:
            out.write(data)
        if on_chunk is not None:
            on_chunk(len(data))
    if sparse:
        out.truncate(size)
