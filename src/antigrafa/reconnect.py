"""A disk stream that becomes very slow is opened again on a new connection.

A TCP connection can stay slow for good while the network is fine for every other
one. A bond or a link aggregation sends each connection over one link: when that link
drops packets, TCP keeps waiting for retransmissions. Seen: a disk read at 0.4 MiB/s,
7% of its data sent twice, on a 1 Gbit/s network where a new connection ran at full
speed. A new connection has a new source port, so it most likely takes another link.

The reader measures how long it waits for the stream and how much of the disk arrives.
When, over a whole ``window``, it waited almost all the time and less than ``min_rate``
arrived, the stream is stopped and opened again at the first chunk not received yet:
only the chunk in progress is read twice. Time Antigrafa spends elsewhere (a slow
destination, the source throttle) is not spent waiting for the stream, so a slow
destination never looks like a slow stream.

Only reads that can start at a chunk are opened again (``DiskSource.reopener``); for
the others, a collapse is logged once.

A container's tar archive is made again by the new connection, not read from a fixed
image. Two runs give the same archive, but that is checked (``check``): the new stream
starts one chunk earlier, and that chunk must be the one already received. If not, the
disk fails (``ResumeMismatch``) rather than mix two different archives.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO

from .delta import NOT_READ
from .progress import human_bytes

log = logging.getLogger(__name__)

MiB = 1024 * 1024

# (digest, data, length) of one chunk, as ``pipeline.backup_records`` takes them
Record = tuple[bytes, Any, int]


@dataclass
class StreamWatch:
    """When a stream counts as collapsed (the ``stream_*`` keys of ``[tuning]``)."""

    min_rate: float = 1 * MiB  # bytes of disk per second; 0: never
    window: float = 60.0  # seconds the stream must stay that slow
    max_reconnects: int = 5  # new connections per disk
    busy: float = 0.9  # share of the window the reader spent waiting for the stream

    @property
    def enabled(self) -> bool:
        return self.min_rate > 0 and self.window > 0


class ResumeMismatch(RuntimeError):
    """The stream opened again does not give the chunks the first one gave."""


class Meter:
    """Wraps a stream: the time spent waiting in ``read``, and the disk bytes received."""

    def __init__(self, raw: BinaryIO, clock: Callable[[], float] = time.monotonic) -> None:
        self.raw = raw
        self._clock = clock
        self._lock = threading.Lock()
        self._waited = 0.0
        self._received = 0
        self._since: float | None = None  # start of the read in progress

    def read(self, size: int = -1) -> bytes:
        with self._lock:
            self._since = self._clock()
        data = b""
        try:
            data = self.raw.read(size) or b""
        finally:
            with self._lock:
                self._waited += self._clock() - self._since
                self._since = None
                self._received += len(data)
        return data

    def credit(self, length: int) -> None:
        """A chunk the node did not send because it did not change: it arrived all the same."""
        with self._lock:
            self._received += length

    def totals(self) -> tuple[float, int]:
        """Seconds spent waiting (the read in progress included), bytes received."""
        with self._lock:
            ongoing = self._clock() - self._since if self._since is not None else 0.0
            return self._waited + ongoing, self._received


class SlowDetector:
    """Says when a stream has been too slow for a whole window (see ``StreamWatch``)."""

    def __init__(self, watch: StreamWatch) -> None:
        self.watch = watch
        self._samples: deque[tuple[float, float, int]] = deque()

    def update(self, now: float, waited: float, received: int) -> float | None:
        """Adds a sample of the ``Meter`` totals; the rate (bytes/s) once the stream collapsed."""
        samples = self._samples
        samples.append((now, waited, received))
        # Keep the newest sample at least a window old, and those after it
        while len(samples) > 1 and now - samples[1][0] >= self.watch.window:
            samples.popleft()
        then, waited_then, received_then = samples[0]
        elapsed = now - then
        if elapsed < self.watch.window:
            return None
        rate = (received - received_then) / elapsed
        if waited - waited_then >= self.watch.busy * elapsed and rate < self.watch.min_rate:
            return rate
        return None


class _Watchdog:
    """Samples a ``Meter`` from its own thread; stops the stream once it collapsed."""

    def __init__(
        self,
        meter: Meter,
        watch: StreamWatch,
        decide: Callable[[float], bool],  # stop the stream at this rate?
        kill: Callable[[], None],
        clock: Callable[[], float],
    ) -> None:
        self.rate = 0.0
        self.fired = False
        self._meter, self._watch, self._decide, self._kill, self._clock = meter, watch, decide, kill, clock
        self._interval = min(5.0, watch.window / 4)
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="stream-watch", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        detector = SlowDetector(self._watch)
        while True:
            rate = detector.update(self._clock(), *self._meter.totals())
            if rate is not None:
                if self._decide(rate):
                    self.rate = rate
                    self.fired = True  # before the kill: a read it cuts short always sees it
                    self._kill()
                return
            if self._stopped.wait(self._interval):
                return

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join()


class ReconnectingReader:
    """The chunks of one disk, read again on a new connection when the stream collapses.

    ``parse(stream, start)``: the records of an open stream whose first chunk is chunk
    ``start``. ``reopen(start)``: the same read on a new connection, from chunk ``start``
    (None: this read cannot start mid-way). ``check``: the new connection starts one
    chunk earlier, whose digest must be the one received (``DiskSource.resume_check``).
    Used as a context manager, like the stream: on exit, the stream open at that time is
    closed and its exit status checked.
    """

    def __init__(
        self,
        stream: Any,
        parse: Callable[[BinaryIO, int], Iterator[Record]],
        reopen: Callable[[int], Any] | None,
        watch: StreamWatch,
        label: str,
        chunk_size: int,
        clock: Callable[[], float] = time.monotonic,
        check: bool = False,
    ) -> None:
        self.stream = stream
        self.parse = parse
        # Stopping a stream needs kill(), and replacing it close(abort)
        self.reopen = reopen if hasattr(stream, "kill") and hasattr(stream, "close") else None
        self.watch = watch
        self.label = label
        self.chunk_size = chunk_size
        self.check = check
        self.reconnects = 0
        self.meter: Meter | None = None  # of the stream being read
        self._clock = clock

    def __enter__(self) -> ReconnectingReader:
        self.stream.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stream.__exit__(exc_type, exc, tb)

    def records(self) -> Iterator[Record]:
        start = 0  # index of the next chunk
        first = 0  # index of the first chunk of the open stream
        last = b""  # digest of chunk ``start - 1``
        while True:
            meter = self.meter = Meter(self.stream.stdout, self._clock)
            dog = self._watchdog(meter) if self.watch.enabled else None
            parsed = self.parse(meter, first)
            try:
                for digest, data, length in parsed:
                    if dog is not None and dog.fired:
                        break  # possibly cut short by the stop: read again
                    if data is None or data is NOT_READ:
                        meter.credit(length)
                    if first < start:  # the chunk read again to check the new stream
                        first = start
                        if digest != last:
                            raise ResumeMismatch(
                                f"{self.label}: the new connection does not give the same data "
                                f"(chunk {start - 1} differs): the read cannot go on"
                            )
                        continue
                    yield digest, data, length
                    start += 1
                    first = start
                    last = digest
            except ResumeMismatch:
                raise
            except Exception:
                # A stream stopped in the middle of a record cannot be parsed to the end
                if dog is None or not dog.fired:
                    raise
            finally:
                if dog is not None:
                    dog.stop()
                close = getattr(parsed, "close", None)
                if close is not None:
                    close()
            if dog is None or not dog.fired:
                if first < start:  # ended before the chunk to check
                    raise ResumeMismatch(f"{self.label}: the new connection ended before chunk {start - 1}")
                return
            first = start - 1 if self.check and start else start
            self._reconnect(first, start, dog.rate)

    def _watchdog(self, meter: Meter) -> _Watchdog:
        # kill() is only called when _decide agreed, that is for a stream that has it
        stream = self.stream
        return _Watchdog(meter, self.watch, self._decide, lambda: stream.kill(), self._clock)

    def _decide(self, rate: float) -> bool:
        """Called by the watchdog thread: stop this stream for a new connection?"""
        slow = f"{self.label}: {rate / MiB:.2f} MiB/s for {self.watch.window:g} s, waiting for the node"
        if self.reopen is None:
            log.warning("%s (network or disk); this read cannot restart on a new connection", slow)
            return False
        if self.reconnects >= self.watch.max_reconnects:
            log.warning("%s, after %d new connections: going on with this one", slow, self.reconnects)
            return False
        return True

    def _reconnect(self, first: int, start: int, rate: float) -> None:
        """A new connection from chunk ``first``; ``start``: the next chunk not received."""
        self.reconnects += 1
        log.warning(
            "%s: %.2f MiB/s for %g s, waiting for the node: new connection from %s (%d/%d)",
            self.label, rate / MiB, self.watch.window, human_bytes(start * self.chunk_size),
            self.reconnects, self.watch.max_reconnects,
        )
        self.stream.close(abort=True)
        if self.reopen is None:  # never: _decide stops no stream that cannot be opened again
            raise RuntimeError(f"{self.label}: stream stopped but cannot be opened again")
        self.stream = self.reopen(first)
        self.stream.__enter__()
