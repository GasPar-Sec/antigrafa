import io
import logging
import re
import threading

import pytest

from antigrafa import progress as progress_module
from antigrafa.progress import Progress, ProgressLogHandler, human_bytes, human_duration


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Terminal(io.StringIO):
    def isatty(self):
        return True


def _progress(clock=None, **kwargs):
    kwargs.setdefault("enabled", True)
    kwargs.setdefault("stream", io.StringIO())
    kwargs.setdefault("interval", 0)
    return Progress(clock=clock or Clock(), width=lambda: 200, **kwargs)


def _screen(stream):
    """What the terminal shows: the text after the last carriage return."""
    return stream.getvalue().rsplit("\r", 1)[-1]


@pytest.mark.parametrize(
    ("seconds", "text"), [(0, "0s"), (59.9, "59s"), (60, "1m 00s"), (3599, "59m 59s"), (3600, "1h 00m"), (90061, "25h 01m")]
)
def test_human_duration(seconds, text):
    assert human_duration(seconds) == text


def test_human_bytes():
    assert human_bytes(512) == "512 B"
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(3 * 1024**4) == "3.0 TiB"


def test_known_size_shows_bar_rate_and_eta():
    clock = Clock()
    p = _progress(clock, label="backup")
    task = p.task(1000 * 1024**2)
    clock.now += 10
    task.advance(250 * 1024**2)
    line = p.line()
    assert line.startswith("backup  [#####---------------]  25%")
    assert "250.0 MiB/1000.0 MiB" in line
    assert "25.0 MiB/s" in line
    assert "ETA 30s" in line  # 750 MiB left at 25 MiB/s
    assert line.endswith("10s")


def test_unknown_size_shows_bytes_without_bar():
    clock = Clock()
    p = _progress(clock, label="backup")
    p.task(1000)  # known
    archive = p.task(None)  # tar archive: size unknown
    clock.now += 2
    archive.advance(4096)
    line = p.line()
    assert "[" not in line and "ETA" not in line
    assert line.startswith("backup  4.0 KiB  2.0 KiB/s")

    archive.close()  # once the archive is done, the bar comes back
    assert "[" in p.line()


def test_nothing_done_yet():
    p = _progress(label="restore")
    p.task(100)
    line = p.line()
    assert "  0%" in line and "/s" not in line and "ETA" not in line


def test_short_or_failed_task_does_not_freeze_the_percentage():
    p = _progress()
    failed = p.task(1000)
    failed.advance(100)
    failed.close()  # stopped at 100 of 1000
    other = p.task(100)
    other.advance(100)
    assert p.total == 200 and p.done_known == 200
    assert "100%" in p.line()
    other.close()  # already complete: nothing changes
    assert p.total == 200


def test_task_as_context_manager_closes_on_error():
    p = _progress()
    with pytest.raises(OSError), p.task(None) as task:
        task.advance(10)
        raise OSError("disk full")
    assert p.unknown_open == 0


def test_items_unit_and_status():
    clock = Clock()
    p = _progress(clock, label="verify", unit="chunks")
    task = p.task(40)
    clock.now += 4
    task.advance(10)
    p.set_status("2 snapshots")
    line = p.line()
    assert "10/40" in line and "2.5/s" in line and line.endswith("2 snapshots")


def test_disabled_never_writes():
    stream = Terminal()
    p = Progress(enabled=False, stream=stream, interval=0)
    task = p.task(10)
    task.advance(5)
    p.set_status("x")
    p.clear()
    p.redraw()
    with p.paused():
        pass
    p.finish()
    assert stream.getvalue() == ""


def test_enabled_by_default_only_on_a_terminal():
    assert Progress(stream=Terminal()).enabled is True
    assert Progress(stream=io.StringIO()).enabled is False  # redirected, cron...

    class Closed(io.StringIO):
        def isatty(self):
            raise ValueError("I/O operation on closed file")

    assert Progress(stream=Closed()).enabled is False
    assert Progress(stream=object()).enabled is False  # no isatty at all


def test_redraws_are_throttled():
    clock = Clock()
    stream = io.StringIO()
    p = _progress(clock, stream=stream, interval=0.5)
    task = p.task(100)
    task.advance(1)  # first draw
    task.advance(1)  # too soon: skipped
    assert stream.getvalue().count("\r") == 1
    clock.now += 0.6
    task.advance(1)
    assert stream.getvalue().count("\r") == 2
    p.set_status("forced")  # status changes always redraw
    assert stream.getvalue().count("\r") == 3


def test_shorter_line_erases_the_previous_one():
    clock = Clock()
    stream = io.StringIO()
    p = _progress(clock, stream=stream, label="backup")
    p.set_status("a long status text")
    long_len = len(_screen(stream))
    p.set_status("")
    screen = _screen(stream)
    assert len(screen) == long_len  # padded with spaces over the old text
    assert screen.rstrip() == p.line()


def test_line_never_wider_than_the_terminal():
    stream = io.StringIO()
    p = Progress(label="backup", enabled=True, stream=stream, interval=0, clock=Clock(), width=lambda: 30)
    p.set_status("x" * 100)
    screen = _screen(stream)
    assert 0 < len(screen) <= 29  # one column free: no wrap
    assert screen.startswith("backup") and "x" not in screen  # the long status was dropped

    # Even the fields that are never dropped are cut rather than wrapped
    p.label = "y" * 100
    p.redraw()
    assert _screen(stream) == "y" * 29


def test_running_disks_are_listed_with_their_own_percentage():
    clock = Clock()
    p = _progress(clock, label="backup")
    vm = p.task(1000, name="100/scsi0")
    ct = p.task(None, name="200/rootfs")  # tar archive: bytes, no percentage
    p.task(500)  # not named: counted, not listed
    clock.now += 1
    vm.advance(630)
    ct.advance(2048)
    assert p.line().endswith("|  100/scsi0 63%  200/rootfs 2.0 KiB")
    assert [t.name for t in p.running] == ["100/scsi0", "200/rootfs"]

    vm.close()  # finished disks leave the line
    assert p.line().endswith("|  200/rootfs 2.0 KiB")
    ct.close()
    assert "|" not in p.line() and p.running == []


def test_narrow_terminal_keeps_percentage_and_disks():
    clock = Clock()
    p = _progress(clock, label="backup")
    p.set_status("1/3 guests")
    task = p.task(1000 * 1024**2, name="100/scsi0")
    clock.now += 125
    task.advance(400 * 1024**2)
    full = p.line()
    assert "MiB/s" in full and "2m 05s" in full and "400.0 MiB/1000.0 MiB" in full

    narrow = p.line(width=70)
    assert len(narrow) <= 70
    # Dropped first: elapsed time, then amounts, then rate
    assert "2m 05s" not in narrow and "400.0 MiB/1000.0 MiB" not in narrow
    # Kept: label, bar and percentage, the disk being read
    assert narrow.startswith("backup  [########------------]  40%")
    assert narrow.endswith("100/scsi0 40%")

    tiny = p.line(width=45)
    assert tiny.startswith("backup  [########------------]  40%") and len(tiny) <= 45


def test_clear_paused_and_finish():
    stream = io.StringIO()
    p = _progress(stream=stream, label="backup")
    p.clear()  # nothing drawn yet: nothing to erase
    assert stream.getvalue() == ""

    with p.paused():  # nothing to show yet: not drawn after the block
        stream.write("message\n")
    assert stream.getvalue() == "message\n"

    p.task(10).advance(5)
    drawn = p.line()
    with p.paused():
        stream.write("hello\n")
    assert f"\r{' ' * len(drawn)}\rhello\n" in stream.getvalue()
    assert _screen(stream) == p.line()  # drawn again below the message

    p.finish()
    assert _screen(stream).strip() == ""
    p.task(10).advance(10)
    assert _screen(stream).strip() == ""  # finished: never drawn again


def test_log_records_go_above_the_line():
    stream = io.StringIO()
    p = _progress(stream=stream, label="backup")
    p.task(10).advance(5)
    logger = logging.getLogger("antigrafa.test.progress")
    handler = ProgressLogHandler(p)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("VM 100: snapshot")
    finally:
        logger.removeHandler(handler)
    out = stream.getvalue()
    assert "\rVM 100: snapshot\n" in out
    assert out.endswith(p.line())  # the line is back, below the message


def test_thread_safe_totals():
    p = _progress()
    tasks = [p.task(10_000) for _ in range(8)]

    def work(task):
        for _ in range(10_000):
            task.advance(1)

    threads = [threading.Thread(target=work, args=(t,)) for t in tasks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert p.done == p.done_known == p.total == 80_000


def test_estimated_size_shows_approximate_percentage():
    clock = Clock()
    p = _progress(clock, label="backup")
    ct = p.task(1000, name="150/rootfs", approximate=True)
    clock.now += 1
    ct.advance(630)
    line = p.line()
    assert "]~ 63%" in line  # overall: approximate
    assert line.endswith("150/rootfs ~63%")
    assert "ETA" in line

    ct.advance(370)  # estimate reached, archive not finished: never 100 %
    assert "]~ 99%" in p.line() and p.line().endswith("150/rootfs ~99%")
    ct.advance(500)  # past the estimate
    line = p.line()
    assert "~ 99%" in line and "ETA" not in line

    ct.close()  # now the real size is known
    assert p.total == p.done_known == 1500
    assert "] 100%" in p.line() and "~" not in p.line()


def test_exact_and_estimated_tasks_together():
    p = _progress(label="backup")
    vm = p.task(1000, name="100/scsi0")
    ct = p.task(1000, name="150/rootfs", approximate=True)
    vm.advance(1000)
    ct.advance(200)
    assert p.line().endswith("100/scsi0 100%  150/rootfs ~20%")
    assert "~ 60%" in p.line()
    ct.advance(800)
    ct.close()
    assert "] 100%" in p.line()


def test_approximate_without_estimate_is_unknown():
    p = _progress()
    task = p.task(None, name="x", approximate=True)  # no estimate available
    assert task.approximate is False and p.approximate_open == 0
    task.advance(10)
    assert p.line().endswith("x 10 B")


# --------------------------------------------------------------------------- multi-line


class Screen(io.StringIO):
    """A tiny terminal: applies \r, \n, cursor up (ESC[nA) and erase line (ESC[K)."""

    def isatty(self):
        return True

    def rows(self):
        rows, r, c, s, i = [""], 0, 0, self.getvalue(), 0
        while i < len(s):
            match = re.match(r"\x1b\[(\d*)([AK])", s[i:])
            if match:
                if match.group(2) == "A":
                    r = max(r - int(match.group(1) or 1), 0)
                else:
                    rows[r] = rows[r][:c]
                i += len(match.group(0))
                continue
            ch = s[i]
            if ch == "\r":
                c = 0
            elif ch == "\n":
                r, c = r + 1, 0
                rows.extend([""] * (r + 1 - len(rows)))
            else:
                line = rows[r].ljust(c)
                rows[r] = line[:c] + ch + line[c + 1 :]
                c += 1
            i += 1
        while rows and not rows[-1].strip():
            rows.pop()
        return [row.rstrip() for row in rows]


def _multi(clock=None, height=40, **kwargs):
    kwargs.setdefault("stream", Screen())
    return Progress(
        label="backup", enabled=True, multiline=True, interval=0, clock=clock or Clock(),
        width=lambda: 200, height=lambda: height, **kwargs,
    )


def test_one_bar_per_disk_when_guests_run_in_parallel():
    clock = Clock()
    p = _multi(clock)
    vm = p.task(1000 * 1024**2, name="100/scsi0")
    ct = p.task(400 * 1024**2, name="150/rootfs", approximate=True)
    p.set_status("0/2 guests")
    clock.now += 10
    vm.advance(630 * 1024**2)
    ct.advance(100 * 1024**2)
    rows = p.stream.rows()
    assert len(rows) == 3
    assert rows[0].startswith("  100/scsi0   [############--------]  63%  630.0 MiB/1000.0 MiB  63.0 MiB/s  ETA 5s")
    assert rows[1].startswith("  150/rootfs  [#####---------------]~ 25%  100.0 MiB/~400.0 MiB  10.0 MiB/s  ETA 30s")
    assert rows[2].startswith("backup  [##########----------]~ 52%") and rows[2].endswith("0/2 guests")
    assert "|" not in rows[2]  # the disks have their own lines


def test_single_disk_stays_on_one_line():
    p = _multi()
    task = p.task(100, name="100/scsi0")
    task.advance(40)
    assert p.stream.rows() == [p.line(width=199)]
    assert p.stream.rows()[0].endswith("|  100/scsi0 40%")


def test_block_shrinks_and_grows_without_leftovers():
    p = _multi()
    a = p.task(100, name="1/a")
    b = p.task(100, name="2/b")
    a.advance(10)
    b.advance(10)
    assert len(p.stream.rows()) == 3
    a.close()  # back to one line: the two old lines are erased
    rows = p.stream.rows()
    assert len(rows) == 1 and rows[0].endswith("|  2/b 10%")
    c = p.task(None, name="3/c")  # unknown size: bytes on its own line
    c.advance(2048)
    rows = p.stream.rows()
    assert len(rows) == 3 and rows[1].startswith("  3/c  2.0 KiB")


def test_log_messages_go_above_the_block():
    p = _multi()
    p.task(100, name="1/a").advance(10)
    p.task(100, name="2/b").advance(20)
    logger = logging.getLogger("antigrafa.test.multi")
    handler = ProgressLogHandler(p)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("VM 1: snapshot")
        logger.warning("VM 2: snapshot")
    finally:
        logger.removeHandler(handler)
    rows = p.stream.rows()
    assert rows[:2] == ["VM 1: snapshot", "VM 2: snapshot"]
    assert rows[2].startswith("  1/a") and rows[3].startswith("  2/b") and rows[4].startswith("backup")
    assert len(rows) == 5


def test_finish_erases_the_whole_block():
    p = _multi()
    p.task(100, name="1/a").advance(10)
    p.task(100, name="2/b").advance(20)
    p.finish()
    assert p.stream.rows() == []
    p.clear()  # nothing left to erase
    assert p.stream.rows() == []


def test_block_never_taller_than_the_terminal():
    p = _multi(height=6)  # room for 4 lines of tasks
    for i in range(7):
        p.task(100, name=f"{i}/disk").advance(i)
    rows = p.stream.rows()
    assert len(rows) == 5
    assert [r.split()[0] for r in rows[:3]] == ["0/disk", "1/disk", "2/disk"]
    assert rows[3] == "  ... 4 more"
    assert rows[4].startswith("backup")
    assert len(p.lines(width=100)) == 8  # no height: every task


def test_narrow_terminal_keeps_disk_percentages():
    p = _multi()
    a = p.task(1000 * 1024**2, name="100/scsi0")
    p.task(10, name="2/b")
    a.advance(400 * 1024**2)
    first = p.lines(width=40)[0]
    assert first.startswith("  100/scsi0  [########------------]  40%") and len(first) <= 40


def test_multiline_default_follows_the_terminal(monkeypatch):
    assert Progress(stream=io.StringIO()).multiline is False  # not a terminal
    assert Progress(stream=Screen(), enabled=False).multiline is False
    monkeypatch.setattr(progress_module, "_ansi_terminal", lambda stream: True)
    assert Progress(stream=Screen()).multiline is True
    assert Progress(stream=Screen(), multiline=False).multiline is False


def test_ansi_terminal_detection():
    tty = Screen()
    assert progress_module._ansi_terminal(io.StringIO(), "linux", {}) is False
    assert progress_module._ansi_terminal(tty, "linux", {"TERM": "dumb"}) is False
    assert progress_module._ansi_terminal(tty, "linux", {"TERM": "xterm-256color"}) is True
    assert progress_module._ansi_terminal(tty, "darwin", {}) is True
    # Windows: depends on the console accepting ANSI; a StringIO has no console handle
    assert progress_module._ansi_terminal(tty, "win32", {}) is False


class Kernel32:
    """Stands in for the Windows console API."""

    def __init__(self, get_ok=True, set_ok=True):
        self.get_ok, self.set_ok = get_ok, set_ok
        self.set_calls = []

    def GetConsoleMode(self, handle, mode_ref):
        mode_ref._obj.value = 0x0003
        return self.get_ok

    def SetConsoleMode(self, handle, mode):
        self.set_calls.append((handle, mode))
        return self.set_ok


class FileNo:
    def fileno(self):
        return 2


def test_windows_console_ansi_is_switched_on():
    k = Kernel32()
    assert progress_module._enable_windows_vt(FileNo(), k, lambda fd: 1000 + fd) is True
    assert k.set_calls == [(1002, 0x0003 | progress_module.ENABLE_VIRTUAL_TERMINAL_PROCESSING)]


def test_windows_console_without_ansi_falls_back_to_one_line():
    assert progress_module._enable_windows_vt(FileNo(), Kernel32(get_ok=False), lambda fd: 1) is False  # redirected
    assert progress_module._enable_windows_vt(FileNo(), Kernel32(set_ok=False), lambda fd: 1) is False  # old Windows

    def no_handle(fd):
        raise OSError("bad file descriptor")

    assert progress_module._enable_windows_vt(FileNo(), Kernel32(), no_handle) is False


# --------------------------------------------------------------------------- snapshot (web interface)


def test_snapshot_gives_plain_values():
    clock = Clock()
    p = _progress(clock, label="backup", enabled=False)
    p.set_status("0/2 guests")
    disk = p.task(1000, name="100/scsi0")
    tree = p.task(400, name="150/rootfs", approximate=True)
    clock.now += 10
    disk.advance(500)
    tree.advance(100)

    s = p.snapshot()
    assert s["label"] == "backup" and s["status"] == "0/2 guests" and s["unit"] == "bytes"
    assert (s["done"], s["total"]) == (600, 1400)
    assert s["percent"] == pytest.approx(600 / 1400 * 100)
    assert s["approximate"] and s["rate"] == pytest.approx(60.0) and s["elapsed"] == pytest.approx(10.0)
    assert s["eta"] == pytest.approx(800 / 60)
    first, second = s["tasks"]
    assert first == {
        "name": "100/scsi0", "done": 500, "total": 1000, "percent": 50.0, "approximate": False,
        "rate": pytest.approx(50.0), "eta": pytest.approx(10.0),
    }
    assert second["approximate"] and second["percent"] == 25.0

    tree.advance(1000)  # larger than its estimate: never 100 % until it ends
    assert p.snapshot()["tasks"][1]["percent"] == 99.0 and p.snapshot()["tasks"][1]["eta"] is None


def test_snapshot_with_an_unknown_size():
    p = _progress(enabled=False)
    assert p.snapshot()["percent"] is None and p.snapshot()["eta"] is None  # nothing yet
    task = p.task(None, name="150/rootfs")
    task.advance(300)
    s = p.snapshot()
    assert s["total"] is None and s["percent"] is None and s["eta"] is None and s["done"] == 300
    assert s["tasks"][0]["total"] is None and s["tasks"][0]["percent"] is None


# --------------------------------------------------------------------------- work announced in advance


def test_announced_disks_count_in_the_overall_total():
    clock = Clock()
    p = _progress(clock, label="backup", enabled=False)
    p.expect("1/scsi0", 1000)
    p.expect("2/scsi0", 3000)  # a guest not reached yet
    s = p.snapshot()
    assert s["total"] == 4000 and s["percent"] == 0 and s["approximate"]

    p.drop_expected("1/scsi0")  # its real size now comes from the task
    task = p.task(1200, name="1/scsi0")
    clock.now += 10
    task.advance(1200)
    task.close()
    s = p.snapshot()
    assert s["total"] == 4200 and s["percent"] == pytest.approx(1200 / 4200 * 100)
    assert s["eta"] == pytest.approx(3000 / 120)
    assert "1.2 KiB/~4.1 KiB" in p.line()

    p.drop_expected("2/", prefix=True)  # that guest failed before reading anything
    s = p.snapshot()
    assert s["total"] == 1200 and s["percent"] == 100 and not s["approximate"]
    assert "~" not in p.line()


def test_announced_size_is_never_negative():
    p = _progress(enabled=False)
    p.expect("1/scsi0", -5)
    assert p.snapshot()["total"] is None  # nothing to measure against yet
