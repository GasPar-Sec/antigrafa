"""Adaptive control at both ends of the transfer.

* ``WriteController`` (destination): measures chunk write latency and adjusts the
  number of parallel writes (AIMD, like TCP) and the zstd compression level.
  There is one controller per destination, shared by every running backup:
  the destination is the shared resource.

* ``SourceThrottle`` (Proxmox node): caps the snapshot read rate based on the node's
  iowait. Reading a snapshot loads the same disks the VMs use, so we slow down as
  soon as the node shows signs of waiting on its disks.

Neither sits in the VMs' write path: at worst, the backup gets slower.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

MiB = 1024 * 1024


class AdjustableLimiter:
    """A semaphore whose limit can change while it is in use."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self._limit = limit
        self._active = 0
        self._waits = 0
        self._cond = threading.Condition()

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    def set_limit(self, limit: int) -> None:
        with self._cond:
            self._limit = max(1, limit)
            self._cond.notify_all()

    def acquire(self) -> None:
        with self._cond:
            if self._active >= self._limit:
                self._waits += 1
                while self._active >= self._limit:
                    self._cond.wait()
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify()

    def take_waits(self) -> int:
        """How many times a producer had to wait since the last call."""
        with self._cond:
            waits, self._waits = self._waits, 0
            return waits


@dataclass(frozen=True)
class WriteTuning:
    min_concurrency: int = 1
    max_concurrency: int = 8
    start_concurrency: int = 2
    target_latency: float = 0.25  # seconds to write one chunk
    start_level: int = 3
    min_level: int = 1
    max_level: int = 9
    interval: float = 2.0  # seconds between adjustments
    min_samples: int = 4


class WriteController:
    def __init__(self, tuning: WriteTuning = WriteTuning(), clock: Callable[[], float] = time.monotonic):
        self.t = tuning
        self._clock = clock
        start = min(max(tuning.start_concurrency, tuning.min_concurrency), tuning.max_concurrency)
        self.limiter = AdjustableLimiter(start)
        self._level = min(max(tuning.start_level, tuning.min_level), tuning.max_level)
        self._lock = threading.Lock()
        self.latency_ewma: float | None = None
        self.total_raw = 0
        self.total_stored = 0
        self._reset_window(clock())

    @property
    def level(self) -> int:
        return self._level

    @property
    def concurrency(self) -> int:
        return self.limiter.limit

    def _reset_window(self, now: float) -> None:
        self._window_start = now
        self._n = 0
        self._compress_s = 0.0
        self._write_s = 0.0

    def record(self, compress_s: float, write_s: float, raw_bytes: int, stored_bytes: int) -> None:
        with self._lock:
            self._n += 1
            self._compress_s += compress_s
            self._write_s += write_s
            self.total_raw += raw_bytes
            self.total_stored += stored_bytes
            self.latency_ewma = (
                write_s if self.latency_ewma is None else 0.8 * self.latency_ewma + 0.2 * write_s
            )
            now = self._clock()
            if now - self._window_start >= self.t.interval and self._n >= self.t.min_samples:
                self._adjust()
                self._reset_window(now)

    def _adjust(self) -> None:
        avg_write = self._write_s / self._n
        avg_comp = self._compress_s / self._n
        saturated = self.limiter.take_waits() > 0
        conc = self.limiter.limit
        target = self.t.target_latency

        # Concurrency: multiplicative decrease when the destination saturates,
        # additive increase when it keeps up and producers are waiting.
        if avg_write > 2 * target:
            new = max(self.t.min_concurrency, int(conc * 0.7))
        elif avg_write <= target and saturated:
            new = min(self.t.max_concurrency, conc + 1)
        else:
            new = conc
        if new != conc:
            self.limiter.set_limit(new)
            log.info("destination: %d parallel writes (mean latency %.0f ms)", new, avg_write * 1000)

        # Compression: slow destination -> compress harder to write less;
        # CPU-bound -> compress less.
        if avg_write > 1.5 * avg_comp and self._level < self.t.max_level:
            self._level += 1
            log.info("zstd level %d (destination slower than CPU)", self._level)
        elif avg_comp > 1.5 * avg_write and self._level > self.t.min_level:
            self._level -= 1
            log.info("zstd level %d (CPU slower than destination)", self._level)

    def stats(self) -> dict:
        return {
            "concurrency": self.concurrency,
            "level": self.level,
            "latency_ms": round((self.latency_ewma or 0) * 1000, 1),
            "raw_bytes": self.total_raw,
            "stored_bytes": self.total_stored,
        }


class SourceThrottle:
    """Token bucket whose rate follows the source node's iowait.

    ``max_rate=None``: no cap while the node is quiet. As soon as iowait exceeds
    ``high``, the rate is capped at half the observed throughput, then halved again
    for as long as it does not come back down. Below ``low``, it grows by 25% per interval.
    """

    def __init__(
        self,
        probe: Callable[[], float] | None = None,
        *,
        max_rate: float | None = None,
        min_rate: float = 10 * MiB,
        high: float = 0.10,
        low: float = 0.03,
        interval: float = 5.0,
        name: str = "",
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._probe = probe
        self.max_rate = max_rate
        self.min_rate = min_rate
        self.high = high
        self.low = low
        self.interval = interval
        self.name = name
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self.rate: float | None = max_rate
        now = clock()
        self._tokens = 0.0
        self._last = now
        self._window_bytes = 0
        self._window_start = now
        self._next_probe = now

    def consume(self, n: int) -> None:
        self._maybe_probe()
        with self._lock:
            self._window_bytes += n
            rate = self.rate
            if rate is None:
                return
            now = self._clock()
            self._tokens = min(rate, self._tokens + (now - self._last) * rate)
            self._last = now
            self._tokens -= n
            delay = -self._tokens / rate if self._tokens < 0 else 0.0
        if delay > 0:
            self._sleep(delay)

    def _maybe_probe(self) -> None:
        if self._probe is None:
            return
        now = self._clock()
        with self._lock:
            if now < self._next_probe:
                return
            self._next_probe = now + self.interval
            elapsed = max(now - self._window_start, 1e-6)
            observed = self._window_bytes / elapsed
            self._window_bytes = 0
            self._window_start = now
        try:
            wait = float(self._probe())
        except Exception as exc:  # the API may be briefly unavailable
            log.warning("node %s: cannot read iowait (%s), keeping current rate", self.name, exc)
            return
        with self._lock:
            self._apply(wait, observed)

    def _apply(self, wait: float, observed: float) -> None:
        old = self.rate
        if wait >= self.high:
            base = self.rate if self.rate is not None else max(observed, self.min_rate)
            self.rate = max(self.min_rate, base * 0.5)
        elif wait <= self.low and self.rate is not None:
            if self.max_rate is None and observed < 0.5 * self.rate:
                self.rate = None  # the cap is no longer binding: lift it
            else:
                new = self.rate * 1.25
                self.rate = min(new, self.max_rate) if self.max_rate is not None else new
        if self.rate != old:
            log.info(
                "node %s: iowait %.1f%%, reads %s",
                self.name,
                wait * 100,
                "uncapped" if self.rate is None else f"capped at {self.rate / MiB:.0f} MiB/s",
            )
