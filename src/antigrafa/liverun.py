"""A job run by another process (the scheduler, ``antigrafa job run``), as the web page shows it.

While a job runs, ``runs/<id>.json`` in the scheduler's directory says where it stands,
in the form of the web interface's own runs: progress, results, log, report. The
process running it writes it every second, and once more at the end; the most recent
``HISTORY`` stay, so the page also lists the jobs that ended. The web interface writes
its own runs there too, when they start and end, so they stay listed after a restart.

To cancel it, the web interface writes ``runs/<id>.cancel``: the process running the
job sees it within a second and cancels the backup (snapshots deleted, unfinished
backups removed), as for a backup started from the page.

A run whose file stops changing (``STALE`` seconds) while it says "running" lost its
process (killed, machine stopped): the page shows it as failed.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

from .jobs import GuestResult
from .platforms.base import Guest
from .progress import Progress
from .runlock import read_json, write_json

log = logging.getLogger("antigrafa.scheduler")

RUNS_DIR = "runs"
PUBLISH = 1.0  # seconds between two writes of the file
STALE = 60  # seconds without a write: the process running the job is gone
HISTORY = 20  # files kept
LOG_LINES = 2000  # log lines kept per run
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
# What the runs list shows of a run (the details add the rest)
SUMMARY_KEYS = ("kind", "user", "source", "target", "state", "started", "finished", "guests_total", "guests_done",
                "guests_ok", "guests_failed", "guest", "job")
ACTIVE = ("running", "cancelling")
# Who ran it, as the page says "started by ..."
BY = {"scheduler": "scheduler", "cli": "command line"}


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
                "reconnects": d.stats.reconnects,
            }
            for d in r.disks
        ],
    }


class _Capture(logging.Handler):
    """The log of the process while the job runs: shown with the run, as on the page."""

    def __init__(self, live: LiveRun) -> None:
        super().__init__(logging.INFO)
        self.live = live
        self.setFormatter(logging.Formatter(LOG_FORMAT, datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.live.add_log(self.format(record))
        except Exception:  # pragma: no cover - never breaks the backup
            self.handleError(record)


class LiveRun:
    """The file of one job run, written while it runs: ``with LiveRun(...) as live:``.

    ``live.cancel`` is set when the page asks to cancel, or when ``stop`` is set (the
    scheduler stops): give it to the backup.
    """

    def __init__(
        self,
        directory: Path,
        job: str,
        *,
        by: str,
        user: str = "",
        progress: Progress | None = None,
        stop: threading.Event | None = None,
        publish: float | None = None,
    ) -> None:
        self.directory = Path(directory) / RUNS_DIR
        self.job = job
        self.user = user or BY.get(by, by)
        self.started = _now()
        self.uid = f"{self.started:%Y%m%dT%H%M%S%f}-{os.getpid()}-{job}"
        self.path = self.directory / f"{self.uid}.json"
        self.cancel_path = self.directory / f"{self.uid}.cancel"
        self.progress = progress or Progress(label=f"job {job}", enabled=False)
        self.cancel = threading.Event()
        self.state = "running"
        self.cancelled_by = ""
        self.error = ""
        self.source = ""
        self.target = ""
        self.guests: list[Guest] = []
        self.results: list[GuestResult] = []
        self.report: list[str] = []
        self.finished: datetime | None = None
        self._stop = stop
        self._publish = publish if publish is not None else PUBLISH
        self._lock = threading.Lock()
        self._logs: deque[str] = deque(maxlen=LOG_LINES)
        self._log_total = 0
        self._done = threading.Event()
        self._thread: threading.Thread | None = None
        self._capture = _Capture(self)
        self._failed = False  # the file could not be written (said once)

    # ------------------------------------------------------------- the job

    def add_log(self, line: str) -> None:
        with self._lock:
            self._logs.append(line)
            self._log_total += 1

    def begin(self, source: str, target: str, guests: list[Guest]) -> None:
        """The guests are known: the page shows them."""
        self.source, self.target, self.guests = source, target, list(guests)

    def finish(self, state: str, error: str = "", report: list[str] | None = None) -> None:
        self.report = list(report or [])
        self.error = error
        self.finished = _now()
        self.state = state

    def request_cancel(self, by: str) -> None:
        with self._lock:
            if self.state != "running":
                return
            self.state = "cancelling"
            self.cancelled_by = by
        self.cancel.set()
        log.warning("job %s: cancel requested by %s", self.job, by)

    # ------------------------------------------------------------- the file

    def data(self) -> dict:
        with self._lock:
            lines, total = list(self._logs), self._log_total
        results = list(self.results)  # appended by the backup threads
        ok = sum(1 for r in results if r.ok)
        active = self.state in ACTIVE
        return {
            "kind": "backup",
            "user": self.user,
            "source": self.source,
            "target": self.target,
            "state": self.state,
            "started": self.started.isoformat(),
            "finished": self.finished.isoformat() if self.finished else None,
            "guests_total": len(self.guests),
            "guests_done": len(results),
            "guests_ok": ok,
            "guests_failed": len(results) - ok,
            "guest": "",
            "job": self.job,
            "cancelled_by": self.cancelled_by,
            "error": self.error,
            "guests": [guest_dict(g) for g in self.guests],
            "progress": self.progress.snapshot() if active else None,
            "results": [result_dict(r) for r in results],
            "report": self.report,
            "backup": "",
            "restore": None,
            "message": "",
            "log_total": total,
            "logs": lines,
            "pid": os.getpid(),
            "heartbeat": _now().isoformat(),
        }

    def publish(self) -> None:
        try:
            write_json(self.path, self.data())
        except OSError as exc:
            if not self._failed:  # the backup goes on; the page just cannot follow it
                log.warning("job %s: cannot write %s (the web page cannot show it): %s", self.job, self.path, exc)
            self._failed = True

    def _check_cancel(self) -> None:
        if self._stop is not None and self._stop.is_set():
            self.request_cancel("scheduler stopping")
        elif self.state == "running" and self.cancel_path.exists():
            asked = read_json(self.cancel_path) or {}
            self.request_cancel(str(asked.get("by") or "the web interface"))

    def _loop(self) -> None:
        while not self._done.wait(self._publish):
            self._check_cancel()
            self.publish()

    def __enter__(self) -> LiveRun:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            prune(self.directory.parent, keep=HISTORY - 1)
        except OSError as exc:
            log.warning("job %s: cannot use %s (the web page cannot show it): %s", self.job, self.directory, exc)
            self._failed = True
        logging.getLogger("antigrafa").addHandler(self._capture)
        self.publish()
        self._thread = threading.Thread(target=self._loop, name=f"live-{self.job}", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, kind, exc, tb) -> None:
        self._done.set()
        if self._thread is not None:
            self._thread.join()
        logging.getLogger("antigrafa").removeHandler(self._capture)
        if self.state in ACTIVE:  # ended without finish(): it failed
            stopped = isinstance(exc, KeyboardInterrupt) or self.cancel.is_set()
            self.finish("cancelled" if stopped else "failed", str(exc or "") or (kind.__name__ if kind else ""))
        self.publish()
        try:
            self.cancel_path.unlink(missing_ok=True)
        except OSError:
            pass


# ---------------------------------------------------------------------- the web side


def prune(directory: Path, keep: int = HISTORY) -> None:
    """Remove the oldest run files beyond ``keep`` (the names start with the start time)."""
    folder = Path(directory) / RUNS_DIR
    files = sorted(folder.glob("*.json"))
    for old in files[: max(0, len(files) - keep)]:
        old.unlink(missing_ok=True)
        old.with_suffix(".cancel").unlink(missing_ok=True)


def read_runs(directory: Path, cache: dict | None = None) -> list[tuple[str, dict]]:
    """The run files, oldest first: (id, content). ``cache``: kept between calls, only
    the files that changed are read again."""
    folder = Path(directory) / RUNS_DIR
    cache = cache if cache is not None else {}
    found = []
    try:
        paths = sorted(folder.glob("*.json"))
    except OSError:
        return []
    for path in paths:
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            continue
        known = cache.get(path.stem)
        if known is None or known[0] != stamp:
            data = read_json(path)
            if data is None:  # being replaced: the previous content, if any
                if known is None:
                    continue
                data = known[1]
            else:
                cache[path.stem] = (stamp, data)
        else:
            data = known[1]
        found.append((path.stem, data))
    for gone in set(cache) - {uid for uid, _ in found}:
        del cache[gone]
    return found


def shown(data: dict, now: datetime | None = None) -> dict:
    """The run as the page shows it: "running" without news for ``STALE`` seconds lost its process."""
    if data.get("state") not in ACTIVE:
        return data
    now = now or _now()
    try:
        beat = datetime.fromisoformat(data.get("heartbeat") or "")
    except ValueError:
        beat = None
    if beat is not None and abs((now - beat).total_seconds()) <= STALE:
        return data
    since = f" (no news since {beat:%H:%M:%S})" if beat else ""
    return {**data, "state": "failed", "progress": None,
            "error": data.get("error") or f"the process running it stopped before the end{since}"}


def request_cancel(directory: Path, uid: str, user: str) -> None:
    """Ask the process running the job to cancel it (raises OSError if it cannot be asked)."""
    write_json(Path(directory) / RUNS_DIR / f"{uid}.cancel", {"by": user, "at": _now().isoformat()})
