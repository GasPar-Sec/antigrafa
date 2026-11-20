"""Progress display on the terminal, shared by every thread of a command.

One line, redrawn in place with a carriage return (works in every terminal)::

    backup  [########------------]  41%  11.2 GiB/27.0 GiB  212 MiB/s  ETA 1m 15s  2m 03s  1/3 guests  |  100/scsi0 63%

When several disks are read at once (guests backed up in parallel) and the terminal
understands ANSI sequences, one bar per disk, above the overall line::

      100/scsi0   [############--------]  63%  12.6 GiB/20.0 GiB  140 MiB/s  ETA 54s
      150/rootfs  [####----------------] ~22%  1.2 GiB/~5.4 GiB  72 MiB/s  ETA 59s
    backup  [########------------]  41%  13.8 GiB/25.4 GiB  212 MiB/s  ETA 56s  2m 03s  1/3 guests

Work is split into *tasks* (one per disk being read, one per restore...). A task with
a known size counts towards the percentage; a task without one (a tar archive of a
container) still counts in the bytes and the rate. An *approximate* task (estimated
size) counts towards the percentage, shown with a ``~`` and capped at 99 % until it
ends. A named task (``<guest id>/<disk>``) is shown while it runs.

On a narrow terminal, the least useful fields go first (elapsed time, amounts,
rate...): the percentages stay visible. Disabled, nothing is ever written: by default
the display is only shown when the output is a terminal (never in cron).

Log messages go through ``ProgressLogHandler``: the display is erased, the message is
written, then the display is drawn again below it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import sys
import threading
import time
from typing import Callable, TextIO

BAR_WIDTH = 20
ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004  # Windows console mode flag
UP = "\x1b[{}A"  # cursor up n lines
ERASE = "\x1b[K"  # erase to the end of the line


def human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)  # pragma: no cover - the loop always returns


def human_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class Task:
    """One piece of work: ``advance`` as it goes, ``close`` when it ends (or fails)."""

    def __init__(
        self, progress: Progress, total: int | None, name: str = "", approximate: bool = False, start: float = 0.0
    ) -> None:
        self._progress = progress
        self.total = total
        self.approximate = approximate and total is not None  # total is an estimate
        self.name = name  # shown while the task runs ("" = not shown)
        self.done = 0
        self.start = start  # clock value when the task began (its own rate and ETA)

    def advance(self, n: int) -> None:
        self._progress._advance(self, n)

    def close(self) -> None:
        self._progress._close(self)

    def __enter__(self) -> Task:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class Progress:
    def __init__(
        self,
        label: str = "",
        *,
        unit: str = "bytes",  # "bytes" or "items" (chunks...)
        enabled: bool | None = None,  # None: only when ``stream`` is a terminal
        multiline: bool | None = None,  # one bar per task; None: when the terminal can
        stream: TextIO | None = None,
        interval: float = 0.2,  # at most one redraw every ``interval`` seconds
        clock: Callable[[], float] = time.monotonic,
        width: Callable[[], int] | None = None,
        height: Callable[[], int] | None = None,
    ) -> None:
        self.stream = stream if stream is not None else sys.stderr
        if enabled is None:
            enabled = _is_terminal(self.stream)
        self.enabled = enabled
        if multiline is None:
            multiline = enabled and _ansi_terminal(self.stream)
        self.multiline = multiline
        self.label = label
        self.unit = unit
        self.status = ""
        self.interval = interval
        self._clock = clock
        self._width = width or (lambda: shutil.get_terminal_size().columns)
        self._height = height or (lambda: shutil.get_terminal_size().lines)
        self._lock = threading.RLock()
        self._start = clock()
        self._last_draw = float("-inf")
        self._drawn = 0  # length of the line on screen (0: nothing shown)
        self._lines = 0  # multiline: number of lines of the block on screen
        # Totals over every task, open or closed
        self.total = 0  # sum of the known sizes
        self.done_known = 0  # bytes done in tasks of known size
        self.done = 0  # every byte done (known or not)
        self.unknown_open = 0  # open tasks of unknown size
        self.approximate_open = 0  # open tasks whose size is an estimate
        self._running: list[Task] = []  # open named tasks, in start order
        # Work announced but not started (disks of guests not reached yet): name -> size
        self._expected: dict[str, int] = {}

    # ----------------------------------------------------------------- tasks

    def task(self, total: int | None = None, name: str = "", approximate: bool = False) -> Task:
        task = Task(self, total, name, approximate, start=self._clock())
        with self._lock:
            if total is None:
                self.unknown_open += 1
            else:
                self.total += total
            if task.approximate:
                self.approximate_open += 1
            if name:
                self._running.append(task)
        return task

    def expect(self, name: str, size: int) -> None:
        """Count ``size`` in the overall total until the task ``name`` starts (estimate)."""
        with self._lock:
            self._expected[name] = max(int(size), 0)

    def drop_expected(self, name: str, prefix: bool = False) -> None:
        """The work ``name`` started (its task has the real size) or will not happen.

        ``prefix``: every name starting with ``name`` (all the disks of a guest).
        """
        with self._lock:
            for key in [k for k in self._expected if k == name or (prefix and k.startswith(name))]:
                del self._expected[key]
            self._maybe_draw(force=True)

    def _overall(self) -> tuple[int, bool]:
        """Total of the whole run (tasks plus what is announced), and whether it is an estimate."""
        return self.total + sum(self._expected.values()), self.approximate_open > 0 or bool(self._expected)

    @property
    def running(self) -> list[Task]:
        with self._lock:
            return list(self._running)

    def _advance(self, task: Task, n: int) -> None:
        with self._lock:
            task.done += n
            self.done += n
            if task.total is not None:
                self.done_known += n
            self._maybe_draw()

    def _close(self, task: Task) -> None:
        with self._lock:
            if task in self._running:
                self._running.remove(task)
            if task.approximate:
                self.approximate_open -= 1
                task.approximate = False
            if task.total is None:
                self.unknown_open -= 1
            elif task.done != task.total:
                # Short, failed or larger than estimated: the total becomes what was read
                self.total += task.done - task.total
                task.total = task.done
            # Always drawn: the end of a task is never hidden by the redraw throttle
            self._maybe_draw(force=True)

    def set_status(self, text: str) -> None:
        with self._lock:
            self.status = text
            self._maybe_draw(force=True)

    # ----------------------------------------------------------------- state

    def snapshot(self) -> dict:
        """Where things stand, as plain values (the web interface draws its own bars).

        ``percent``, ``total`` and ``eta`` are None when unknown (an archive of unknown
        size is being read); ``approximate``: the percentage relies on an estimate.
        """
        with self._lock:
            elapsed = max(self._clock() - self._start, 1e-6)
            rate = self.done / elapsed
            total, approximate = self._overall()
            known = bool(total) and not self.unknown_open
            left = total - self.done_known
            return {
                "label": self.label,
                "unit": self.unit,
                "status": self.status,
                "done": self.done,
                "total": total if known else None,
                "percent": _fraction(self.done_known, total, approximate) * 100 if known else None,
                "approximate": approximate,
                "rate": rate,
                "eta": left / rate if known and rate and left > 0 else None,
                "elapsed": elapsed,
                "tasks": [self._task_snapshot(t) for t in self._running],
            }

    def _task_snapshot(self, task: Task) -> dict:
        elapsed = max(self._clock() - task.start, 1e-6)
        rate = task.done / elapsed
        total = task.total or None
        return {
            "name": task.name,
            "done": task.done,
            "total": total,
            "percent": _fraction(task.done, total, task.approximate) * 100 if total else None,
            "approximate": task.approximate,
            "rate": rate,
            "eta": (total - task.done) / rate if total and rate and total > task.done else None,
        }

    # ----------------------------------------------------------------- text

    def line(self, width: int | None = None, tasks: bool = True) -> str:
        """The overall line, without drawing it; at most ``width`` characters.

        ``tasks``: list the running tasks at its end (single-line display).
        """
        with self._lock:
            return _fit(self._fields(tasks), width)

    def lines(self, width: int | None = None, height: int | None = None) -> list[str]:
        """What the display shows: one line, or one per running task then the overall line.

        At most ``height`` lines: a block taller than the terminal could not be redrawn.
        """
        with self._lock:
            if not (self.multiline and len(self._running) > 1):
                return [self.line(width)]
            shown = self._running
            more = 0
            room = None if height is None else max(height - 2, 2)  # task lines that fit
            if room is not None and len(shown) > room:
                shown, more = shown[: room - 1], len(shown) - (room - 1)
            name_width = max(len(t.name) for t in shown)
            out = [_fit(self._task_fields(t, name_width), width) for t in shown]
            if more:
                out.append(_fit([(0, f"  ... {more} more")], width))
            out.append(self.line(width, tasks=False))
            return out

    def _fields(self, tasks: bool = True) -> list[tuple[int, str]]:
        """(rank, text) of every field, in display order. Rank 0 is never dropped."""
        elapsed = max(self._clock() - self._start, 1e-6)
        rate = self.done / elapsed
        fields = [(0, self.label)] if self.label else []
        total, approximate = self._overall()
        if total and not self.unknown_open:
            fields.append((0, self._bar(self.done_known, total, approximate)))
            estimate = "~" if self._expected else ""
            fields.append((5, f"{self._amount(self.done)}/{estimate}{self._amount(total)}"))
        else:
            fields.append((5, self._amount(self.done)))
        if self.done:
            fields.append((4, f"{self._amount(rate)}/s"))
            left = total - self.done_known
            if total and not self.unknown_open and left > 0:  # never when past the estimate
                fields.append((2, f"ETA {human_duration(left / rate)}"))
        fields.append((6, human_duration(elapsed)))
        if self.status:
            fields.append((2, self.status))
        if tasks and self._running:
            fields.append((1, "|"))
            fields.extend((1, self._describe(task)) for task in self._running)
        return fields

    def _task_fields(self, task: Task, name_width: int) -> list[tuple[int, str]]:
        """One task on its own line: name, bar, amount, rate, ETA."""
        elapsed = max(self._clock() - task.start, 1e-6)
        rate = task.done / elapsed
        fields = [(0, "  " + task.name.ljust(name_width))]
        if task.total:
            fields.append((0, self._bar(task.done, task.total, task.approximate)))
            estimate = "~" if task.approximate else ""
            fields.append((5, f"{self._amount(task.done)}/{estimate}{self._amount(task.total)}"))
        else:
            fields.append((0, self._amount(task.done)))
        if task.done:
            fields.append((4, f"{self._amount(rate)}/s"))
            if task.total and task.total > task.done:
                fields.append((2, f"ETA {human_duration((task.total - task.done) / rate)}"))
        return fields

    def _bar(self, done: int, total: int, approximate: bool) -> str:
        fraction = _fraction(done, total, approximate)
        filled = int(fraction * BAR_WIDTH)
        mark = "~" if approximate else " "
        return f"[{'#' * filled}{'-' * (BAR_WIDTH - filled)}]{mark}{fraction * 100:3.0f}%"

    def _describe(self, task: Task) -> str:
        if task.total:
            mark = "~" if task.approximate else ""
            return f"{task.name} {mark}{_fraction(task.done, task.total, task.approximate) * 100:.0f}%"
        return f"{task.name} {self._amount(task.done)}"

    def _amount(self, n: float) -> str:
        return human_bytes(n) if self.unit == "bytes" else f"{n:.0f}" if n == int(n) else f"{n:.1f}"

    # ----------------------------------------------------------------- drawing

    def _maybe_draw(self, force: bool = False) -> None:
        if not self.enabled:
            return
        now = self._clock()
        if not force and now - self._last_draw < self.interval:
            return
        self._last_draw = now
        self._draw()

    def _draw(self) -> None:
        # Never wider than the terminal: a wrapped line could not be erased or redrawn
        width = max(self._width() - 1, 10)
        if self.multiline:
            self._draw_block(self.lines(width, self._height()))
            return
        text = self.line(width=width)
        pad = " " * max(self._drawn - len(text), 0)
        self.stream.write(f"\r{text}{pad}")
        self.stream.flush()
        self._drawn = len(text)

    def _draw_block(self, lines: list[str]) -> None:
        """Rewrite the block in place: back to its first line, each line erased and rewritten."""
        out = ["\r", _up(self._lines - 1), "\n".join(line + ERASE for line in lines)]
        extra = self._lines - len(lines)
        if extra > 0:  # the block got shorter: erase the old lines below, come back
            out.append(("\n" + ERASE) * extra + _up(extra))
        self.stream.write("".join(out))
        self.stream.flush()
        self._lines = len(lines)
        self._drawn = len(lines[-1])

    def clear(self) -> None:
        """Erase the display (before writing anything else to the terminal)."""
        with self._lock:
            if not (self.enabled and self._drawn):
                return
            if self.multiline:
                up = self._lines - 1
                self.stream.write("\r" + _up(up) + ERASE + ("\n" + ERASE) * up + _up(up))
                self._lines = 0
            else:
                self.stream.write("\r" + " " * self._drawn + "\r")
            self.stream.flush()
            self._drawn = 0

    def redraw(self) -> None:
        with self._lock:
            if self.enabled:
                self._last_draw = self._clock()
                self._draw()

    @contextlib.contextmanager
    def paused(self):
        """Write to the terminal inside the block: the display is erased, then drawn again."""
        with self._lock:
            self.clear()
            try:
                yield
            finally:
                if self.done or self.status:
                    self.redraw()

    def finish(self) -> None:
        """Erase the display for good: the command prints its own summary."""
        with self._lock:
            self.clear()
            self.enabled = False


def _fraction(done: int, total: int, approximate: bool) -> float:
    """Share done. An estimate never reaches 100 %: only the end of the task does."""
    return min(done / total, 0.99 if approximate else 1.0)


def _join(fields: list[tuple[int, str]]) -> str:
    return "  ".join(text for _, text in fields)


def _fit(fields: list[tuple[int, str]], width: int | None) -> str:
    """Join the fields, dropping the least useful ones (highest rank) until it fits."""
    fields = list(fields)
    while width is not None and len(_join(fields)) > width and any(rank for rank, _ in fields):
        worst = max(rank for rank, _ in fields)
        index = max(i for i, (rank, _) in enumerate(fields) if rank == worst)
        del fields[index]
    text = _join(fields)
    return text if width is None else text[:width]


def _up(n: int) -> str:
    return UP.format(n) if n > 0 else ""


def _is_terminal(stream: TextIO) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):  # closed or not a real file
        return False


def _ansi_terminal(stream: TextIO, platform: str = sys.platform, environ=os.environ) -> bool:
    """Whether cursor movements can be used on ``stream`` (multi-line display)."""
    if not _is_terminal(stream) or environ.get("TERM") == "dumb":
        return False
    if platform == "win32":
        return _enable_windows_vt(stream)
    return True


def _enable_windows_vt(stream: TextIO, kernel32=None, get_handle=None) -> bool:
    """Windows 10+: ANSI sequences work once enabled on the console. False if not possible."""
    try:
        import ctypes

        if kernel32 is None:
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if get_handle is None:
            import msvcrt

            get_handle = msvcrt.get_osfhandle  # type: ignore[attr-defined]
        handle = get_handle(stream.fileno())
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # not a console (redirected...)
        return bool(kernel32.SetConsoleMode(handle, mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING))
    except Exception:  # old Windows, no console...: single-line display
        return False


class ProgressLogHandler(logging.StreamHandler):
    """Writes log records above the progress display instead of through it."""

    def __init__(self, progress: Progress, stream: TextIO | None = None) -> None:
        super().__init__(stream if stream is not None else progress.stream)
        self.progress = progress

    def emit(self, record: logging.LogRecord) -> None:
        with self.progress.paused():
            super().emit(record)
