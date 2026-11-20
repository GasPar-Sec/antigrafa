"""Backups started from the web interface: one at a time, run in the background.

Nothing here depends on Django: the views only call ``BackupManager``.

Everything that can be checked quickly is checked before ``start`` returns (the
configuration, the destination, the guests, the token's rights), so that the page
shows the error at once. The backup then runs in its own thread; the page follows
it through ``status``. The last runs (state, results, log, report) stay in memory
until the server stops.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from ..config import Config, ConfigError, load_config
from ..jobs import BackupRunner, GuestResult
from ..operations import new_data_line, open_platform, open_repository, report_lines, select_guests
from ..platforms.base import Guest
from ..progress import Progress

log = logging.getLogger(__name__)

HISTORY = 20  # runs kept in memory
LOG_LINES = 2000  # log lines kept per run
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


class Busy(Exception):
    """A backup is already starting or running."""


def _now() -> datetime:
    return datetime.now().astimezone()


def guest_dict(g: Guest) -> dict:
    return {"id": g.id, "kind": g.kind, "name": g.name, "host": g.host, "status": g.status, "label": g.label}


def result_dict(r: GuestResult) -> dict:
    return {
        "guest": guest_dict(r.guest),
        "ok": r.ok,
        "error": r.error,
        "snapshot": r.snapshot,
        "duration": r.duration,
        "timings": r.timings,
        "disks": [
            {
                "key": d.key,
                "size": d.size,
                "elapsed": d.stats.elapsed,
                "new_chunks": d.stats.new_chunks,
                "reused_chunks": d.stats.reused_chunks,
                "sent_bytes": d.stats.sent_bytes,
            }
            for d in r.disks
        ],
    }


@dataclass
class Run:
    id: int
    user: str
    source: str
    target: str
    guests: list[Guest]
    started: datetime = field(default_factory=_now)
    progress: Progress = field(default_factory=lambda: Progress(label="backup", enabled=False))
    cancel: threading.Event = field(default_factory=threading.Event)
    # running -> (cancelling ->) ok | failed | cancelled
    state: str = "running"
    cancelled_by: str = ""
    results: list[GuestResult] = field(default_factory=list)
    finished: datetime | None = None
    error: str = ""  # the run itself failed (not one guest)
    report: list[str] = field(default_factory=list)
    logs: deque = field(default_factory=lambda: deque(maxlen=LOG_LINES))  # the last lines
    log_total: int = 0  # lines ever logged (the oldest may be gone from ``logs``)

    def add_log(self, line: str) -> None:
        self.logs.append(line)
        self.log_total += 1

    @property
    def active(self) -> bool:
        return self.state in ("running", "cancelling")

    def summary(self) -> dict:
        ok = sum(1 for r in self.results if r.ok)
        return {
            "id": self.id,
            "user": self.user,
            "source": self.source,
            "target": self.target,
            "state": self.state,
            "started": self.started.isoformat(),
            "finished": self.finished.isoformat() if self.finished else None,
            "guests_total": len(self.guests),
            "guests_done": len(self.results),
            "guests_ok": ok,
            "guests_failed": len(self.results) - ok,
        }

    def details(self, logs_after: int = 0) -> dict:
        """Everything about the run; ``logs_after``: only the log lines after that many."""
        lines, total = list(self.logs), self.log_total
        first = total - len(lines)  # number of the oldest line kept
        return {
            **self.summary(),
            "cancelled_by": self.cancelled_by,
            "error": self.error,
            "guests": [guest_dict(g) for g in self.guests],
            "progress": self.progress.snapshot() if self.active else None,
            "results": [result_dict(r) for r in self.results],
            "report": self.report,
            "log_total": total,
            "logs": lines[max(logs_after - first, 0) :] if logs_after <= total else lines,
        }


class _RunLog(logging.Handler):
    """Copies the engine's log records into the log of the running backup."""

    def __init__(self, manager: BackupManager) -> None:
        super().__init__()
        self.manager = manager
        self.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        run = self.manager.current
        if run is not None and not record.name.startswith(__package__):
            run.add_log(self.format(record))


class BackupManager:
    def __init__(
        self,
        config_path: str,
        load: Callable[[str], Config] = load_config,
        runner_class=BackupRunner,
    ) -> None:
        self.config_path = config_path
        self._load = load
        self._runner_class = runner_class
        self._lock = threading.Lock()
        self._busy = False  # starting or running
        self._runs: deque[Run] = deque(maxlen=HISTORY)  # newest first
        self._threads: dict[int, threading.Thread] = {}
        self._next_id = 1
        self.current: Run | None = None
        self._log = _RunLog(self)
        logging.getLogger("antigrafa").addHandler(self._log)

    def close(self) -> None:
        logging.getLogger("antigrafa").removeHandler(self._log)

    # ----------------------------------------------------------------- reading

    def config(self) -> Config:
        """Read again on every use: changes to the file apply without a restart."""
        return self._load(self.config_path)

    def choices(self) -> dict:
        cfg = self.config()
        return {"sources": sorted(cfg.sources), "targets": sorted(cfg.targets)}

    def guests(self, source: str | None) -> list[dict]:
        platform = open_platform(self.config(), source or None)
        guests = platform.guests()
        if not guests:
            platform.check_access([])  # nothing visible: no guest, or no right?
        return [guest_dict(g) for g in guests]

    def run(self, run_id: int) -> Run | None:
        with self._lock:
            return next((r for r in self._runs if r.id == run_id), None)

    def status(self) -> dict:
        with self._lock:
            runs = list(self._runs)
        return {"busy": self._busy, "runs": [r.summary() for r in runs]}

    # ----------------------------------------------------------------- control

    def start(
        self,
        *,
        source: str | None,
        target: str | None,
        guests: list[str] | None = None,
        every: bool = False,
        exclude: list[str] | None = None,
        user: str = "",
    ) -> Run:
        """Check everything, then start the backup in the background (raises on error)."""
        with self._lock:
            if self._busy:
                raise Busy("a backup is already running: wait for it to end, or cancel it")
            self._busy = True
        try:
            cfg = self.config()
            platform = open_platform(cfg, source or None)
            repo = open_repository(cfg, target or None)
            selected = select_guests(platform.guests(), guests, every, exclude)
            if not selected:
                raise ConfigError("no guest to back up")
            platform.check_access(selected)  # every right, before the first snapshot
            with self._lock:
                run = Run(self._next_id, user, platform.name, repo.cfg.name, selected)
                self._next_id += 1
            runner = self._runner_class(
                cfg.tuning, platform, repo, progress=run.progress, cancel=run.cancel, on_result=run.results.append
            )
            thread = threading.Thread(target=self._execute, args=(run, runner), name=f"backup-{run.id}", daemon=True)
            with self._lock:
                self._runs.appendleft(run)
                self._threads[run.id] = thread
                self.current = run
            thread.start()
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        log.info("backup %d started by %s: %d guest(s) of %s to %s", run.id, user, len(selected), run.source, run.target)
        return run

    def _execute(self, run: Run, runner) -> None:
        start = time.monotonic()
        try:
            runner.run(run.guests)
        except Exception as exc:  # the engine itself failed: report it on the page
            logging.getLogger("antigrafa.jobs").exception("backup %d failed", run.id)
            run.error = str(exc) or type(exc).__name__
        finally:
            try:
                runner.close()
            finally:
                self._finish(run, runner, time.monotonic() - start)

    def _finish(self, run: Run, runner, elapsed: float) -> None:
        try:
            run.report = report_lines(run.results, run.started, elapsed)[1:]
            new_data = new_data_line(runner.controller.stats())
            if new_data:
                run.report.append(new_data)
        except Exception as exc:  # pragma: no cover - a report problem never hides the end of the run
            run.report = [f"report unavailable: {exc}"]
        run.finished = _now()
        if run.cancel.is_set():
            run.state = "cancelled"
        elif run.error or len(run.results) < len(run.guests) or not all(r.ok for r in run.results):
            run.state = "failed"
        else:
            run.state = "ok"
        with self._lock:
            self.current = None
            self._threads.pop(run.id, None)
            self._busy = False
        log.info("backup %d ended: %s", run.id, run.state)

    def cancel(self, run_id: int, user: str = "") -> bool:
        """Ask the running backup to stop (snapshots deleted, partial backups removed)."""
        with self._lock:
            run = self.current
            if run is None or run.id != run_id or run.state != "running":
                return False
            run.state = "cancelling"
            run.cancelled_by = user
            run.cancel.set()
        logging.getLogger("antigrafa.jobs").warning("backup %d: cancel requested by %s", run_id, user or "?")
        return True

    def shutdown(self, timeout: float | None = None) -> None:
        """Server stopping: cancel the running backup and wait for its cleanup (snapshots)."""
        with self._lock:
            run = self.current
            thread = self._threads.get(run.id) if run else None
        if run is not None and run.state == "running":
            self.cancel(run.id, "server shutdown")
        if thread is not None:
            thread.join(timeout)
        self.close()
