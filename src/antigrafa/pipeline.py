"""Transfer of a stream (raw disk or tar archive) into the chunk store.

The calling thread reads and hashes; compressing and writing new chunks goes to a
shared pool, throttled by the ``WriteController``. When the destination slows down,
the reader waits, the SSH pipe fills up and the read on the node slows down by
itself. The VM never waits.
"""

from __future__ import annotations

import time
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from typing import BinaryIO

from .adaptive import SourceThrottle, WriteController
from .chunkstore import ChunkStore, digest_of, encode_chunk

DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024


@dataclass
class StreamStats:
    bytes_read: int = 0
    chunks: int = 0
    new_chunks: int = 0
    reused_chunks: int = 0
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


def backup_stream(
    stream: BinaryIO,
    store: ChunkStore,
    controller: WriteController,
    executor: Executor,
    throttle: SourceThrottle | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> tuple[list[bytes], StreamStats]:
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
        while True:
            data = read_full(stream, chunk_size)
            if not data:
                break
            if throttle is not None:
                throttle.consume(len(data))
            digest = digest_of(data)
            digests.append(digest)
            stats.bytes_read += len(data)
            stats.chunks += 1
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


def restore_stream(store: ChunkStore, digests: list[bytes], out: BinaryIO, size: int, sparse: bool = True) -> None:
    """Rebuild the original stream. All-zero chunks become holes when the output allows it."""
    sparse = sparse and out.seekable()
    for digest in digests:
        data = store.read(digest)
        zeros = _ZEROS.get(len(data)) or _ZEROS.setdefault(len(data), bytes(len(data)))
        if sparse and data == zeros:
            out.seek(len(data), 1)
        else:
            out.write(data)
    if sparse:
        out.truncate(size)
