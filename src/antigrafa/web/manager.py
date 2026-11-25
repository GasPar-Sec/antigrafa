"""Backups and restores started from the web interface: one at a time, in the background.

Nothing here depends on Django: the views only call ``BackupManager``.

Everything that can be checked quickly is checked before ``start`` (or
``start_restore``) returns (the configuration, the destination, the guests, the
token's rights, the backup to restore), so that the page shows the error at once.
The run then goes on in its own thread; the page follows it through ``status``. The
last runs (state, results, log, report) stay in memory until the server stops.

One run at a time, with the scheduler too: a run holds the run lock of the scheduler's
directory (see ``antigrafa.runlock``); while a scheduled job runs, nothing starts here.
A job started from the page ("run now") records its start and result in the job's
state, as the scheduler does.

The jobs run by the scheduler (or ``antigrafa job run``) show among the runs of this
server, with their progress, results and log, and can be cancelled from here: their
process writes them to the scheduler's directory (see ``antigrafa.liverun``).
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
from ..index import read_manifest
from ..jobs import BackupRunner, GuestResult
from .. import liverun
from .. import scheduler as jobs_state
from ..liverun import guest_dict, result_dict
from ..operations import backup_state, new_data_line, open_platform, open_repository, report_lines, select_guests
from ..platforms.base import Guest, RestoreRequest
from ..progress import Progress
from ..restore import restore_guest
from ..runlock import RunLock, describe

log = logging.getLogger(__name__)

HISTORY = 20  # runs kept in memory
LOG_LINES = 2000  # log lines kept per run
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"


RESTORE_MODES = ("new", "overwrite", "disk-only")
STOP_MODES = ("", "stop", "force-stop")


class Busy(Exception):
    """A backup or a restore is already starting or running."""


def _now() -> datetime:
    return datetime.now().astimezone()


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
    kind: str = "backup"  # or "restore"
    # A restore: the backup restored (path in the target), what was asked, what was done
    backup: str = ""
    request: RestoreRequest | None = None
    message: str = ""
    job: str = ""  # a [job.<name>] started from the page
    lock: RunLock | None = field(default=None, repr=False)

    def add_log(self, line: str) -> None:
        self.logs.append(line)
        self.log_total += 1

    @property
    def active(self) -> bool:
        return self.state in ("running", "cancelling")

    def summary(self) -> dict:
        if self.kind == "restore":
            done = 0 if self.active else 1
            ok = 1 if self.state == "ok" else 0
        else:
            done = len(self.results)
            ok = sum(1 for r in self.results if r.ok)
        return {
            "id": self.id,
            "kind": self.kind,
            "user": self.user,
            "source": self.source,
            "target": self.target,
            "state": self.state,
            "started": self.started.isoformat(),
            "finished": self.finished.isoformat() if self.finished else None,
            "guests_total": len(self.guests),
            "guests_done": done,
            "guests_ok": ok,
            "guests_failed": done - ok,
            "guest": self.guests[0].label if self.kind == "restore" and self.guests else "",
            "job": self.job,
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
            "backup": self.backup,
            "restore": _request_dict(self.request) if self.request else None,
            "message": self.message,
            "log_total": total,
            "logs": lines[max(logs_after - first, 0) :] if logs_after <= total else lines,
        }


class ExternalRun:
    """A job run by another process (the scheduler, ``antigrafa job run``), as its file says."""

    def __init__(self, run_id: int, uid: str, data: dict) -> None:
        self.id = run_id
        self.uid = uid
        self.data = data
        self.kind = data.get("kind") or "backup"
        self.state = data.get("state") or "failed"
        self.started = data.get("started") or ""

    @property
    def active(self) -> bool:
        return self.state in liverun.ACTIVE

    def summary(self) -> dict:
        return {"id": self.id, **{k: self.data.get(k) for k in liverun.SUMMARY_KEYS}, "external": True}

    def details(self, logs_after: int = 0) -> dict:
        lines = list(self.data.get("logs") or [])
        total = int(self.data.get("log_total") or 0)
        first = total - len(lines)
        out = {k: v for k, v in self.data.items() if k not in ("pid", "heartbeat")}
        return {**out, **self.summary(), "logs": lines[max(logs_after - first, 0):] if logs_after <= total else lines}


def _started(run) -> str:
    return run.started if isinstance(run, ExternalRun) else run.started.isoformat()


def _request_dict(r: RestoreRequest) -> dict:
    mode = "overwrite" if r.overwrite else "disk-only" if r.disk_only else "new"
    return {"guest_id": r.guest_id, "host": r.host, "storage": r.storage, "mode": mode, "stop": r.stop,
            "start": r.start}


def backup_dict(path: str, manifest: dict) -> dict:
    """One backup of a target, as the restore page lists it."""
    guest = manifest.get("guest") or {}
    disks = manifest.get("disks") or []
    parts = path.split("/")
    return {
        "path": path,
        "source": manifest.get("source") or parts[0],
        "kind": guest.get("kind") or (parts[1] if len(parts) > 1 else ""),
        "id": str(guest.get("id") or (parts[2] if len(parts) > 2 else "")),
        "name": guest.get("name") or "",
        "host": guest.get("host") or "",
        "time": manifest.get("time") or "",
        "size": sum(int(d.get("size") or 0) for d in disks),
        "disks": [{"key": d.get("key"), "volume": d.get("volume"), "size": d.get("size")} for d in disks],
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
        self._external_ids: dict[str, int] = {}  # file of a job run elsewhere -> its id here
        self._external_cache: dict = {}  # the files already read (see liverun.read_runs)
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

    def backups(self, target: str | None) -> list[dict]:
        """The complete backups of a target, the most recent first."""
        repo = open_repository(self.config(), target or None)
        found = [backup_dict(path.replace("\\", "/"), manifest) for path, manifest in repo.list_snapshots()]
        return sorted(found, key=lambda b: (b["time"], b["path"]), reverse=True)

    def restore_locations(self, source: str | None, kind: str) -> list[dict]:
        """The nodes of a source and, for each, the storages a guest of ``kind`` can go to."""
        if kind not in ("vm", "ct"):
            raise ConfigError(f"unknown guest kind {kind!r}")
        return open_platform(self.config(), source or None).restore_locations(kind)

    def run(self, run_id: int) -> Run | ExternalRun | None:
        with self._lock:
            run = next((r for r in self._runs if r.id == run_id), None)
        if run is None:
            run = next((r for r in self._external_runs() if r.id == run_id), None)
        return run

    def _directory(self):
        try:
            return self.config().scheduler.directory
        except ConfigError:
            return None

    def _external_runs(self, directory=None) -> list[ExternalRun]:
        """The jobs run by the scheduler or ``antigrafa job run``, oldest first."""
        directory = directory if directory is not None else self._directory()
        if directory is None:
            return []
        now = _now()
        found = []
        for uid, data in liverun.read_runs(directory, self._external_cache):
            with self._lock:
                if uid not in self._external_ids:
                    self._external_ids[uid] = self._next_id
                    self._next_id += 1
                run_id = self._external_ids[uid]
            found.append(ExternalRun(run_id, uid, liverun.shown(data, now)))
        return found

    def status(self) -> dict:
        """The runs of this server and of the scheduler, newest first; "external": what runs
        elsewhere (the scheduler), if anything, with "run": its id in the list when it is a job."""
        with self._lock:
            own = list(self._runs)
            busy = self._busy
        directory = self._directory()
        others = self._external_runs(directory) if directory is not None else []
        runs = sorted(own + others, key=_started, reverse=True)[:HISTORY]
        external = None
        if not busy and directory is not None:
            try:
                holder = RunLock(directory).holder()
            except OSError:
                holder = None
            if holder is not None:
                external = {**holder, "label": describe(holder)}
                running = next((r for r in others if r.active and r.data.get("job") == holder.get("job")), None)
                if running is not None:
                    external["run"] = running.id
        return {"busy": busy or external is not None, "runs": [r.summary() for r in runs], "external": external}

    def jobs(self) -> dict:
        """The [job.<name>] sections: what, when, next and last run; and the scheduler's state."""
        cfg = self.config()
        return {
            "scheduler": jobs_state.scheduler_status(cfg.scheduler.directory),
            "jobs": jobs_state.job_summaries(cfg),
        }

    def start_job(self, name: str, user: str = "") -> Run:
        """Run a job now, from the page: followed here like any backup."""
        job = self.config().jobs.get(name)
        if job is None:
            raise ConfigError(f"unknown job {name}")
        return self.start(source=job.source or None, target=job.target or None, guests=job.guests, every=job.all,
                          exclude=job.exclude, user=user, job=name)

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
        job: str = "",
    ) -> Run:
        """Check everything, then start the backup in the background (raises on error)."""
        lock = self._claim("backup", user, job)
        try:
            cfg = self.config()
            platform = open_platform(cfg, source or None)
            repo = open_repository(cfg, target or None)
            selected = select_guests(platform.guests(), guests, every, exclude)
            if not selected:
                raise ConfigError("no guest to back up")
            platform.check_access(selected)  # every right, before the first snapshot
            with self._lock:
                run = Run(self._next_id, user, platform.name, repo.cfg.name, selected, job=job, lock=lock)
                self._next_id += 1
            runner = self._runner_class(
                cfg.tuning, platform, repo, progress=run.progress, cancel=run.cancel, on_result=run.results.append
            )
            thread = threading.Thread(target=self._execute, args=(run, runner), name=f"backup-{run.id}", daemon=True)
            with self._lock:
                self._runs.appendleft(run)
                self._threads[run.id] = thread
                self.current = run
            if job:
                jobs_state.record_start(cfg.scheduler.directory, job, run.started)
            thread.start()
        except BaseException:
            self._unclaim(lock)
            raise
        log.info("backup %d started by %s: %d guest(s) of %s to %s%s", run.id, user, len(selected), run.source,
                 run.target, f" (job {job})" if job else "")
        return run

    def _claim(self, what: str, user: str, job: str = "") -> RunLock:
        """Nothing runs here nor elsewhere (the scheduler): the run lock is ours."""
        with self._lock:
            if self._busy:
                running = self.current.kind if self.current else "backup"
                raise Busy(f"a {running} is already running: wait for it to end, or cancel it")
            self._busy = True
        try:
            directory = self.config().scheduler.directory
            lock = RunLock(directory)
            try:
                got = lock.try_acquire({"by": "web", "what": what, "user": user, "job": job})
            except OSError as exc:
                raise ConfigError(f"cannot take the run lock in {directory} ([scheduler] data_dir): {exc}") from None
            if not got:
                raise Busy(f"{describe(lock.holder())} is running: wait for it to end")
        except BaseException:
            with self._lock:
                self._busy = False
            raise
        return lock

    def _unclaim(self, lock: RunLock | None) -> None:
        if lock is not None:
            lock.release()
        with self._lock:
            self._busy = False

    def start_restore(
        self,
        *,
        target: str | None,
        backup: str,
        guest_id: str = "",
        host: str = "",
        storage: str = "",
        mode: str = "new",
        stop: str = "",
        start: bool = False,
        user: str = "",
    ) -> Run:
        """Check the request and the backup, then restore it in the background (raises on error).

        ``mode``: "new" (the id must be free), "overwrite" or "disk-only"; ``guest_id``:
        "" for the original id. The platform checks the rest (id, storages) when it starts.
        """
        if mode not in RESTORE_MODES:
            raise ConfigError(f"unknown restore mode {mode!r}")
        if stop not in STOP_MODES:
            raise ConfigError(f"unknown stop mode {stop!r}")
        if stop and mode != "overwrite":
            raise ConfigError("stopping the guest first only applies when replacing it")
        if start and mode == "disk-only":
            raise ConfigError("disk-only does not change the guest: nothing to start")
        if guest_id and not guest_id.isdigit():
            raise ConfigError(f"{guest_id!r} is not a guest id")
        lock = self._claim("restore", user)
        try:
            cfg = self.config()
            repo = open_repository(cfg, target or None)
            try:
                backup_dir = repo.resolve(backup)
            except ValueError as exc:
                raise ConfigError(str(exc)) from None
            if not backup_dir.is_dir():
                raise ConfigError(f"no backup {backup} in target {repo.cfg.name}")
            manifest = read_manifest(backup_dir)
            platform = open_platform(cfg, manifest.get("source") or None)
            original = manifest.get("guest") or {}
            request = RestoreRequest(
                guest_id=guest_id or str(original.get("id", "")), host=host, storage=storage,
                overwrite=mode == "overwrite", disk_only=mode == "disk-only", stop=stop, start=start,
            )
            guest = Guest(request.guest_id, str(original.get("kind", "")), str(original.get("name", "")),
                          host or str(original.get("host", "")))
            with self._lock:
                run = Run(self._next_id, user, platform.name, repo.cfg.name, [guest], kind="restore",
                          backup=backup, request=request, lock=lock)
                run.progress = Progress(label="restore", enabled=False)
                self._next_id += 1
            thread = threading.Thread(target=self._execute_restore, args=(run, platform, repo),
                                      name=f"restore-{run.id}", daemon=True)
            with self._lock:
                self._runs.appendleft(run)
                self._threads[run.id] = thread
                self.current = run
            thread.start()
        except BaseException:
            self._unclaim(lock)
            raise
        log.info("restore %d started by %s: %s from %s as %s (%s)", run.id, user, backup, run.target,
                 request.guest_id, mode)
        return run

    def _execute_restore(self, run: Run, platform, repo) -> None:
        start = time.monotonic()
        try:
            run.message = restore_guest(platform, repo, run.backup, run.request, run.progress, run.cancel)
        except BaseException as exc:  # shown on the page; the platform cleaned up what it created
            logging.getLogger("antigrafa.restore").error("restore %d failed: %s", run.id, exc or type(exc).__name__)
            run.error = str(exc) or type(exc).__name__
        finally:
            self._finish(run, None, time.monotonic() - start)

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
        if run.kind == "backup":
            try:
                run.report = report_lines(run.results, run.started, elapsed)[1:]
                new_data = new_data_line(runner.controller.stats())
                if new_data:
                    run.report.append(new_data)
            except Exception as exc:  # pragma: no cover - a report problem never hides the end of the run
                run.report = [f"report unavailable: {exc}"]
        run.finished = _now()
        if run.kind == "restore":
            # Cancelled only if it stopped: a cancel that came too late leaves a finished restore
            state = "ok" if not run.error else "cancelled" if run.cancel.is_set() else "failed"
        else:
            state = backup_state(len(run.guests), run.results, run.error, run.cancel.is_set())
        if run.job:  # before the run shows as ended: the job's result is there when the page looks
            try:
                directory = self.config().scheduler.directory
            except ConfigError as exc:
                log.warning("job %s: its result is not recorded: %s", run.job, exc)
            else:
                jobs_state.record_end(directory, run.job, jobs_state.job_result(
                    run.started, state, len(run.guests), run.results, by="web", user=run.user, error=run.error))
        with self._lock:
            self.current = None
            self._threads.pop(run.id, None)
        self._unclaim(run.lock)
        run.lock = None
        run.state = state  # last: a run seen ended never keeps the next one from starting
        log.info("%s %d ended: %s", run.kind, run.id, run.state)

    def cancel(self, run_id: int, user: str = "") -> bool:
        """Ask the running backup to stop (snapshots deleted, partial backups removed), or the
        running restore (what it created removed, the guest replaced unlocked). A job of the
        scheduler: its process is asked, and stops it within a second."""
        with self._lock:
            run = self.current
            own = run is not None and run.id == run_id
        if not own:
            return self._cancel_external(run_id, user)
        with self._lock:
            if run.state != "running":
                return False
            run.state = "cancelling"
            run.cancelled_by = user
            run.cancel.set()
        logging.getLogger("antigrafa.jobs").warning("%s %d: cancel requested by %s", run.kind, run_id, user or "?")
        return True

    def _cancel_external(self, run_id: int, user: str) -> bool:
        directory = self._directory()
        run = next((r for r in self._external_runs(directory) if r.id == run_id), None)
        if run is None or run.state != "running":
            return False
        try:
            liverun.request_cancel(directory, run.uid, user)
        except OSError as exc:
            raise ConfigError(f"cannot ask the scheduler to cancel job {run.data.get('job')}: {exc}") from None
        log.warning("job %s (run %d): cancel requested by %s", run.data.get("job"), run_id, user or "?")
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
