import threading

from antigrafa.adaptive import MiB, AdjustableLimiter, SourceThrottle, WriteController, WriteTuning


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_limiter_blocks_and_counts_waits():
    lim = AdjustableLimiter(1)
    lim.acquire()
    done = threading.Event()

    def other():
        lim.acquire()
        done.set()

    th = threading.Thread(target=other)
    th.start()
    assert not done.wait(0.1)
    lim.set_limit(2)  # raising the limit releases the waiting producer
    assert done.wait(1)
    th.join()
    assert lim.take_waits() == 1
    assert lim.take_waits() == 0


def _window(ctl, clock, write_s, compress_s, saturated):
    if saturated:
        ctl.limiter._waits += 1
    for _ in range(5):
        ctl.record(compress_s, write_s, 4 * MiB, 2 * MiB)
    clock.t += 3
    ctl.record(compress_s, write_s, 4 * MiB, 2 * MiB)


def test_write_controller_ramps_up_when_destination_is_fast():
    clock = FakeClock()
    ctl = WriteController(WriteTuning(start_concurrency=2, max_concurrency=5, target_latency=0.25), clock=clock)
    for _ in range(10):
        _window(ctl, clock, write_s=0.05, compress_s=0.05, saturated=True)
    assert ctl.concurrency == 5  # capped at the maximum


def test_write_controller_backs_off_and_compresses_more_on_slow_destination():
    clock = FakeClock()
    ctl = WriteController(
        WriteTuning(start_concurrency=8, max_concurrency=8, target_latency=0.25, start_level=3, max_level=9),
        clock=clock,
    )
    _window(ctl, clock, write_s=2.0, compress_s=0.05, saturated=True)
    assert ctl.concurrency == 5  # 8 * 0.7
    assert ctl.level == 4
    for _ in range(20):
        _window(ctl, clock, write_s=2.0, compress_s=0.05, saturated=True)
    assert ctl.concurrency == 1
    assert ctl.level == 9


def test_write_controller_lowers_compression_when_cpu_bound():
    clock = FakeClock()
    ctl = WriteController(WriteTuning(start_level=5, min_level=1), clock=clock)
    for _ in range(10):
        _window(ctl, clock, write_s=0.01, compress_s=0.5, saturated=False)
    assert ctl.level == 1


def test_source_throttle_caps_on_iowait_and_releases():
    clock = FakeClock()
    iowait = {"v": 0.0}
    th = SourceThrottle(
        probe=lambda: iowait["v"], max_rate=None, min_rate=10 * MiB, interval=5, clock=clock, sleep=clock.sleep
    )
    th.consume(MiB)
    assert th.rate is None  # quiet node: no cap

    # 200 MiB/s observed, then the node waits on its disks.
    clock.t += 5
    th._window_bytes = 1000 * MiB
    iowait["v"] = 0.25
    th.consume(MiB)
    assert th.rate == 100 * MiB

    clock.t += 5
    th.consume(MiB)
    assert th.rate == 50 * MiB

    # The token bucket really makes callers wait.
    before = clock.t
    th.consume(100 * MiB)
    assert clock.t - before >= 1.0

    # Back to quiet: the rate climbs, then the cap is lifted.
    iowait["v"] = 0.0
    clock.t += 5
    th._window_bytes = 50 * 5 * MiB
    th.consume(MiB)
    assert th.rate == 62.5 * MiB
    clock.t += 5
    th._window_bytes = 0
    th.consume(MiB)
    assert th.rate is None


def test_source_throttle_respects_floor_and_ceiling():
    clock = FakeClock()
    th = SourceThrottle(probe=lambda: 0.9, max_rate=40 * MiB, min_rate=10 * MiB, interval=1, clock=clock, sleep=clock.sleep)
    for _ in range(10):
        clock.t += 1
        th.consume(1)
    assert th.rate == 10 * MiB
    th._probe = lambda: 0.0
    for _ in range(20):
        clock.t += 1
        th.consume(1)
    assert th.rate == 40 * MiB


def test_source_throttle_survives_probe_errors():
    def boom():
        raise RuntimeError("API unavailable")

    th = SourceThrottle(probe=boom, max_rate=20 * MiB)
    th.consume(1)
    assert th.rate == 20 * MiB
