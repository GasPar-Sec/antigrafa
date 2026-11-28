import io
import logging
import os
import threading
import time

import pytest

from antigrafa import delta
from antigrafa.chunkstore import digest_of
from antigrafa.pipeline import raw_records
from antigrafa.reconnect import Meter, ReconnectingReader, SlowDetector, StreamWatch

MiB = 1024 * 1024
CHUNK = 4096

WATCH = StreamWatch(min_rate=1 * MiB, window=60)
# Real time, kept short: a stream stalled for 0.2 s has collapsed
FAST = StreamWatch(min_rate=1 * MiB, window=0.2)


class Stalling:
    """A stream that delivers ``data``, then blocks (a collapsed connection) until killed.

    ``delay``: blocks that long instead, then delivers ``rest`` (a slow stream).
    """

    def __init__(self, data, stall=True, delay=None, rest=b""):
        self.stdout = self
        self._data = io.BytesIO(data)
        self.stall, self.delay, self.rest = stall, delay, rest
        self.killed = threading.Event()
        self.closed = None  # abort flag given to close()

    def read(self, n):
        buf = self._data.read(n)
        if buf or not self.stall:
            return buf
        if self.delay is not None:
            self.killed.wait(self.delay)
            self.stall, self._data = False, io.BytesIO(self.rest)
            return self._data.read(n)
        self.killed.wait(10)  # never hangs the tests
        return b""

    def kill(self):
        self.killed.set()

    def close(self, abort=False):
        self.closed = abort

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close(abort=exc[0] is not None)


def _chunks(data):
    return [data[i : i + CHUNK] for i in range(0, len(data), CHUNK)]


def _raw(stream, start):
    return raw_records(stream, CHUNK)


def _relay(data, reference):
    """What the node sends for a hashed read: the real script, run here."""
    namespace = {"__name__": "remote"}
    exec(compile(delta.REMOTE_SCRIPT, "remote", "exec"), namespace)  # noqa: S102 - our own script
    out = io.BytesIO()
    namespace["relay"](io.BytesIO(data), io.BytesIO(delta.encode_reference(reference)), out, CHUNK)
    return out.getvalue()


# --------------------------------------------------------------------------- detection


def test_no_verdict_before_a_whole_window():
    d = SlowDetector(WATCH)
    assert d.update(0, 0, 0) is None
    assert d.update(59, 59, 0) is None


def test_collapse_when_the_reader_waited_and_little_arrived():
    d = SlowDetector(WATCH)
    d.update(0, 0, 0)
    assert d.update(60, 58, 24 * MiB) == pytest.approx(0.4 * MiB)


def test_slow_destination_is_not_a_slow_stream():
    d = SlowDetector(WATCH)
    d.update(0, 0, 0)
    # Little arrived, but the reader was busy elsewhere (writes, throttle) most of the time
    assert d.update(60, 10, 1 * MiB) is None


def test_fast_stream_is_kept():
    d = SlowDetector(WATCH)
    d.update(0, 0, 0)
    assert d.update(60, 60, 120 * MiB) is None


def test_only_the_last_window_counts():
    d = SlowDetector(WATCH)
    for t in range(0, 61, 5):  # 100 MiB/s for a minute
        assert d.update(t, t, t * 100 * MiB) is None
    stalled = 60 * 100 * MiB  # then nothing more
    verdicts = [d.update(t, t, stalled) for t in range(65, 121, 5)]
    assert verdicts == [None] * 11 + [0.0]  # once the whole last minute was stalled


def test_disabled_watch():
    assert not StreamWatch(min_rate=0).enabled and not StreamWatch(window=0).enabled
    assert StreamWatch().enabled


def test_meter_counts_the_read_in_progress():
    clock = [0.0]

    class Slow:
        def read(self, n):
            clock[0] += 10  # blocked 10 s: the watchdog sees it before the read returns
            assert meter.totals() == (10, 0)
            return b"x" * n

    meter = Meter(Slow(), lambda: clock[0])
    assert meter.read(5) == b"xxxxx"
    meter.credit(100)  # an unchanged chunk, not sent
    assert meter.totals() == (10, 105)


# --------------------------------------------------------------------------- new connection


def test_collapsed_raw_stream_resumes_where_it_stopped(caplog):
    data = os.urandom(5 * CHUNK + 100)
    first = Stalling(data[: 2 * CHUNK + 1000])  # two chunks and part of the third, then nothing
    opened = []

    def reopen(start):
        opened.append(start)
        return Stalling(data[start * CHUNK :], stall=False)

    reader = ReconnectingReader(first, _raw, reopen, FAST, "VM 1 (web): scsi0", CHUNK)
    with caplog.at_level(logging.WARNING), reader:
        records = list(reader.records())

    assert [r[1] for r in records] == _chunks(data)  # the partial chunk was read again, once
    assert all(digest == digest_of(chunk) for digest, chunk, _ in records)
    assert opened == [2] and reader.reconnects == 1
    assert first.closed is True  # replaced: closed as aborted, its exit status ignored
    assert reader.stream.closed is False  # the last one: closed normally, status checked
    assert "VM 1 (web): scsi0: 0.0" in caplog.text and "new connection from 8.0 KiB (1/5)" in caplog.text


def test_collapsed_hashed_stream_resumes_with_the_rest_of_the_reference():
    old = os.urandom(6 * CHUNK)
    new = bytearray(old)
    new[3 * CHUNK + 5] ^= 1  # chunks 3 and 5 changed
    new[5 * CHUNK] ^= 1
    new = bytes(new)
    reference = [digest_of(c) for c in _chunks(old)]
    wire = _relay(new, reference)
    cut = len(delta.MAGIC) + 3 * 5 + 100  # three unchanged chunks, then in the middle of chunk 3
    first = Stalling(wire[:cut])
    opened = []

    def reopen(start):
        opened.append(start)
        return Stalling(_relay(new[start * CHUNK :], reference[start:]), stall=False)

    def parse(stream, start):
        return delta.read_records(stream, reference[start:])

    reader = ReconnectingReader(first, parse, reopen, FAST, "VM 1: scsi0", CHUNK)
    with reader:
        records = list(reader.records())

    assert records == list(delta.read_records(io.BytesIO(wire), reference))  # as if never cut
    assert [r[1] is None for r in records] == [True, True, True, False, True, False]
    assert opened == [3]


def test_unchanged_chunks_count_as_received():
    # A hashed read of an unchanged disk sends 5 bytes per chunk: it is not slow for that
    data = os.urandom(4 * CHUNK)
    reference = [digest_of(c) for c in _chunks(data)]
    reader = ReconnectingReader(
        Stalling(_relay(data, reference), stall=False),
        lambda stream, start: delta.read_records(stream, reference[start:]),
        None,
        StreamWatch(min_rate=0),
        "VM 1: scsi0",
        CHUNK,
    )
    with reader:
        assert len(list(reader.records())) == 4
    assert reader.meter.totals()[1] >= 4 * CHUNK


def test_read_that_cannot_restart_is_only_reported(caplog):
    data = os.urandom(3 * CHUNK)
    slow = Stalling(data[:100], delay=0.5, rest=data[100:])  # stalls, then recovers by itself
    reader = ReconnectingReader(slow, _raw, None, FAST, "CT 7: rootfs", CHUNK)
    with caplog.at_level(logging.WARNING), reader:
        records = list(reader.records())
    assert [r[1] for r in records] == _chunks(data) and reader.reconnects == 0
    assert caplog.text.count("cannot restart on a new connection") == 1
    assert not slow.killed.is_set()


def test_stream_that_cannot_be_killed_is_not_replaced():
    class Plain:  # no kill(): never stopped, even with a reopen
        def __init__(self, data):
            self.stdout = io.BytesIO(data)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

    data = os.urandom(2 * CHUNK)
    reader = ReconnectingReader(Plain(data), _raw, lambda start: pytest.fail("reopened"), FAST, "x", CHUNK)
    assert reader.reopen is None
    with reader:
        assert [r[1] for r in reader.records()] == _chunks(data)


def test_new_connections_are_limited(caplog):
    data = os.urandom(4 * CHUNK)
    watch = StreamWatch(min_rate=1 * MiB, window=0.2, max_reconnects=1)
    opened = []

    def reopen(start):
        opened.append(start)
        # just as slow: after the limit, the read goes on with this connection
        return Stalling(data[start * CHUNK : (start + 1) * CHUNK], delay=0.6, rest=data[(start + 1) * CHUNK :])

    reader = ReconnectingReader(Stalling(data[:CHUNK]), _raw, reopen, watch, "VM 1: scsi0", CHUNK)
    with caplog.at_level(logging.WARNING), reader:
        records = list(reader.records())
    assert [r[1] for r in records] == _chunks(data)
    assert opened == [1] and reader.reconnects == 1
    assert "after 1 new connections: going on with this one" in caplog.text


def test_error_of_a_live_stream_is_raised():
    wire = bytearray(_relay(os.urandom(2 * CHUNK), []))
    wire[-10] ^= 1  # corrupted in transit, not stopped by the watchdog
    reader = ReconnectingReader(Stalling(bytes(wire), stall=False), lambda s, start: delta.read_records(s, []),
                                lambda start: pytest.fail("reopened"), FAST, "x", CHUNK)
    with pytest.raises(delta.DeltaError, match="digest mismatch"), reader:
        list(reader.records())


def test_watchdog_stops_with_the_reader():
    data = os.urandom(4 * CHUNK)
    reader = ReconnectingReader(Stalling(data, stall=False), _raw, None, WATCH, "x", CHUNK)
    with reader:
        records = reader.records()
        next(records)
        records.close()  # the backup stopped early (error, cancel)
    time.sleep(0.05)
    assert not [t for t in threading.enumerate() if t.name == "stream-watch"]
