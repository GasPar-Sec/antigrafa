"""Recurring backups: the ``[job.<name>]`` sections, run by ``antigrafa scheduler``.

The scheduler reads the configuration again at every check (every 15 seconds): jobs
added, changed or disabled apply without a restart. A job that is due goes into a
queue; one worker runs the queue in order, one job at a time, and waits for the run
lock (see ``antigrafa.runlock``) while a backup or a restore started elsewhere (the web
interface, ``antigrafa job run``) is in progress. A job is never queued twice.

- cron and ``at``: the next time after now. Times missed while the scheduler was not
  running are not caught up. A job still running at its next time runs again right
  after (once).
- ``every``: from the start of the previous run (kept in the job's state, so across
  restarts); a job that never ran is due at once.

What the web interface and ``antigrafa job list`` show comes from files in the
scheduler's directory: ``jobs/<name>.json`` (the start and result of each job's last
run), ``scheduler.json`` (the scheduler's heartbeat, queue and running job) and
``runs/`` (the running job and the last ones, followed on the page as its own runs:
see ``antigrafa.liverun``).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import monitor, notify
from .config import Config, ConfigError, JobConfig, load_config
from .jobs import BackupRunner
from .liverun import LiveRun
from .operations import backup_state, new_data_line, prepare_backup, report_lines
from .progress import Progress
from .runlock import RunLock, describe, read_json, write_json
from .schedule import Every

log = logging.getLogger("antigrafa.scheduler")

TICK = 15.0  # seconds between checks
HEARTBEAT_STALE = 90  # seconds without a heartbeat: the scheduler is not running
STATUS_FILE = "scheduler.json"
JOBS_DIR = "jobs"


def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def _parse(text) -> datetime | None:
    if not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------- state


def read_job_state(directory: Path, name: str) -> dict:
    return read_json(Path(directory) / JOBS_DIR / f"{name}.json") or {}


def write_job_state(directory: Path, name: str, state: dict) -> None:
    folder = Path(directory) / JOBS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    write_json(folder / f"{name}.json", state)


def _update_state(directory: Path, name: str, **changes) -> None:
    """Never fails a backup: a state that cannot be written is logged."""
    try:
        state = read_job_state(directory, name)
        state.update(changes)
        write_job_state(directory, name, state)
    except OSError as exc:
        log.warning("job %s: cannot write its state in %s: %s", name, directory, exc)


def record_start(directory: Path, name: str, started: datetime) -> None:
    _update_state(directory, name, last_start=_iso(started))


def record_end(directory: Path, name: str, result: dict) -> None:
    _update_state(directory, name, last=result)


def job_result(started: datetime, state: str, guests: int, results, *, by: str, user: str = "", error: str = "") -> dict:
    ok = sum(1 for r in results if r.ok)
    return {
        "started": _iso(started),
        "finished": _iso(_now()),
        "state": state,
        "by": by,  # "scheduler", "web" or "cli"
        "user": user,
        "guests_total": guests,
        "guests_ok": ok,
        "guests_failed": len(results) - ok,
        "error": error,
    }


def last_start(state: dict) -> datetime | None:
    return _parse(state.get("last_start"))


# ---------------------------------------------------------------------- reading


def describe_job(job: JobConfig) -> str:
    """'every guest of lab, except 101 -> nas', 'guests 100, 101 of lab -> nas'."""
    source = job.source or "the source"
    what = f"every guest of {source}" if job.all else f"guest{'s' if len(job.guests) > 1 else ''} " \
        f"{', '.join(job.guests)} of {source}"
    if job.exclude:
        what += f", except {', '.join(job.exclude)}"
    return f"{what} -> {job.target or 'the target'}"


def next_run(job: JobConfig, state: dict, now: datetime) -> datetime | None:
    """When the scheduler runs the job next (None: disabled)."""
    if not job.enabled:
        return None
    return job.timing.next_run(now, last_start(state))


def scheduler_status(directory: Path, now: datetime | None = None) -> dict:
    """The scheduler as it last said: running or not, its queue and the job it runs."""
    now = now or _now()
    status = read_json(Path(directory) / STATUS_FILE) or {}
    beat = _parse(status.get("heartbeat"))
    alive = bool(beat) and not status.get("stopped") and abs((now - beat).total_seconds()) <= HEARTBEAT_STALE
    return {
        "alive": alive,
        "pid": status.get("pid"),
        "started": status.get("started"),
        "heartbeat": status.get("heartbeat"),
        "stopped": status.get("stopped"),
        "queue": status.get("queue", []) if alive else [],
        "running": status.get("running") if alive else None,
    }


def job_summaries(cfg: Config, now: datetime | None = None) -> list[dict]:
    """Every job: what, when, next run, last run (for ``job list`` and the web page)."""
    now = now or _now()
    directory = cfg.scheduler.directory
    out = []
    for name, job in sorted(cfg.jobs.items()):
        state = read_job_state(directory, name)
        upcoming = next_run(job, state, now)
        out.append({
            "name": name,
            "enabled": job.enabled,
            "source": job.source,
            "target": job.target,
            "what": describe_job(job),
            "schedule": job.timing.describe(),
            "next": _iso(max(upcoming, now) if upcoming else None),
            "last_start": state.get("last_start"),
            "last": state.get("last"),
        })
    return out


# ---------------------------------------------------------------------- running


def execute_job(
    cfg: Config,
    job: JobConfig,
    *,
    by: str,
    user: str = "",
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    runner_class=BackupRunner,
    live: LiveRun | None = None,
) -> tuple[dict, list[str]]:
    """Back up the guests of ``job`` now (the caller holds the run lock): its result, as kept
    in the job's state, and the report lines (also logged). ``live``: the file the web page
    follows the run through (see ``antigrafa.liverun``)."""
    cancel = cancel if cancel is not None else threading.Event()
    directory = cfg.scheduler.directory
    started = _now()
    clock = time.monotonic()
    record_start(directory, job.name, started)
    results: list = live.results if live is not None else []
    selected: list = []
    error = ""
    runner = None
    source, target = job.source or "?", job.target or "?"  # the names, once known
    try:
        platform, repo, selected = prepare_backup(cfg, job.source or None, job.target or None,
                                                  job.guests, job.all, job.exclude)
        source, target = platform.name, repo.cfg.name
        log.info("job %s: %d guest(s) of %s to %s", job.name, len(selected), platform.name, repo.cfg.name)
        if live is not None:
            live.begin(platform.name, repo.cfg.name, selected)
        runner = runner_class(cfg.tuning, platform, repo, progress=progress, cancel=cancel, on_result=results.append)
        runner.run(selected)
    except Exception as exc:  # the job failed as a whole: its state and the log say why
        error = str(exc) or type(exc).__name__
        log.error("job %s failed: %s", job.name, error)
    finally:
        if runner is not None:
            runner.close()
    elapsed = time.monotonic() - clock
    lines: list[str] = []
    if selected:
        lines = report_lines(results, started, elapsed)[1:]
        new_data = new_data_line(runner.controller.stats()) if runner is not None else None
        if new_data:
            lines.append(new_data)
    state = backup_state(len(selected), results, error, cancel.is_set())
    result = job_result(started, state, len(selected), results, by=by, user=user, error=error)
    record_end(directory, job.name, result)
    for line in lines:
        log.info("%s", line)
    log.info("job %s ended: %s", job.name, state)
    if live is not None:
        live.finish(state, error, lines)
    notify.send(cfg.notify, notify.backup_notification(
        state, source=source, target=target, results=results, report=lines, error=error, job=job.name, by=by,
        user=user))
    return result, lines


class Scheduler:
    def __init__(
        self,
        config_path: str,
        load: Callable[[str], Config] = load_config,
        execute: Callable = execute_job,
        now: Callable[[], datetime] = _now,
        tick: float = TICK,
        lock_poll: float = 5.0,
        watch: monitor.SystemWatch | None = None,
    ) -> None:
        self.config_path = config_path
        self._load = load
        self._execute = execute
        self._now = now
        self._tick = tick
        self._lock_poll = lock_poll
        self.stop = threading.Event()  # set: no new job, the running backup is cancelled
        self.started = now()
        self._cond = threading.Condition()
        self._queue: deque[str] = deque()
        self._running: str | None = None
        self._next: dict[str, tuple[tuple, datetime]] = {}  # cron and at: name -> (schedule, next time)
        self._started_here: dict[str, datetime] = {}  # every: the last starts, if the state is not writable
        self._config_error = ""
        self._directory: Path | None = None
        self._watch = watch or monitor.SystemWatch()
        self._next_check = 0.0  # time.monotonic() of the next system check

    # ------------------------------------------------------------- checking

    def queue(self) -> list[str]:
        with self._cond:
            return list(self._queue)

    @property
    def running(self) -> str | None:
        return self._running

    def check(self, now: datetime | None = None) -> list[str]:
        """Queue the jobs that are due; the names queued."""
        now = now or self._now()
        try:
            cfg = self._load(self.config_path)
        except ConfigError as exc:
            if str(exc) != self._config_error:  # once, not every 15 seconds
                log.error("configuration: %s (jobs are not checked until it is fixed)", exc)
            self._config_error = str(exc)
            return []
        if self._config_error:
            log.info("configuration fixed")
            self._config_error = ""
        self._directory = cfg.scheduler.directory
        queued = []
        for name in set(self._next) - {n for n, j in cfg.jobs.items() if j.enabled}:
            del self._next[name]  # removed or disabled
        for name, job in cfg.jobs.items():
            if job.enabled and self._due(name, job, cfg, now):
                queued.append(name)
        if queued:
            with self._cond:
                self._queue.extend(queued)
                self._cond.notify_all()
            for name in queued:
                log.info("job %s is due (%s): queued", name, cfg.jobs[name].timing.describe())
        self._write_status()
        return queued

    def _due(self, name: str, job: JobConfig, cfg: Config, now: datetime) -> bool:
        with self._cond:
            queued, running = name in self._queue, name == self._running
        if isinstance(job.timing, Every):
            if queued or running:  # its next time counts from this run's start
                return False
            started = [t for t in (last_start(read_job_state(cfg.scheduler.directory, name)),
                                   self._started_here.get(name)) if t]
            return now >= job.timing.next_run(now, max(started) if started else None)
        key = (job.schedule, str(job.at), tuple(job.days))
        known = self._next.get(name)
        if known is None or known[0] != key:  # new job, or new schedule: from now on
            self._next[name] = (key, job.timing.next_run(now))
            return False
        if now < known[1]:
            return False
        self._next[name] = (key, job.timing.next_run(now))
        if queued:
            log.info("job %s is due again but still waits in the queue: not queued twice", name)
            return False
        return True

    def _write_status(self, stopped: bool = False) -> None:
        if self._directory is None:
            return
        with self._cond:
            status = {"pid": os.getpid(), "started": _iso(self.started), "heartbeat": _iso(self._now()),
                      "queue": list(self._queue), "running": self._running}
        if stopped:
            status["stopped"] = status["heartbeat"]
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            write_json(self._directory / STATUS_FILE, status)
        except OSError as exc:
            log.warning("cannot write %s in %s: %s", STATUS_FILE, self._directory, exc)

    # -------------------------------------------------------------- running

    def work(self) -> None:
        """The worker: runs the queue, one job at a time, until ``stop``."""
        while not self.stop.is_set():
            with self._cond:
                if not self._queue:
                    self._cond.wait(self._tick)
                    continue
                name = self._queue.popleft()
                self._running = name
            self._write_status()
            try:
                self.run_job(name)
            except Exception:  # pragma: no cover - never stops the scheduler
                log.exception("job %s", name)
            finally:
                with self._cond:
                    self._running = None
                self._write_status()

    def run_job(self, name: str) -> dict | None:
        try:
            cfg = self._load(self.config_path)
        except ConfigError as exc:
            log.error("job %s not run: configuration: %s", name, exc)
            return None
        job = cfg.jobs.get(name)
        if job is None or not job.enabled:
            log.info("job %s not run: %s since it was queued", name, "removed" if job is None else "disabled")
            return None
        lock = RunLock(cfg.scheduler.directory)
        try:
            got = lock.acquire({"by": "scheduler", "job": name, "what": "backup"}, self.stop, self._lock_poll,
                               on_wait=lambda holder: log.info("job %s waits for %s to end", name, describe(holder)))
        except OSError as exc:
            log.error("job %s not run: cannot take the run lock in %s: %s", name, cfg.scheduler.directory, exc)
            return None
        if not got:
            return None  # stopping
        try:
            self._started_here[name] = self._now()
            # The page follows the run, and may cancel it; so does stopping the scheduler
            with LiveRun(cfg.scheduler.directory, name, by="scheduler", stop=self.stop) as live:
                result, _ = self._execute(cfg, job, by="scheduler", cancel=live.cancel, progress=live.progress,
                                          live=live)
            return result
        finally:
            lock.release()

    def system_check(self, force: bool = False) -> None:
        """The checks behind the "system" notifications, every ``check_minutes`` (see ``monitor``)."""
        if not force and time.monotonic() < self._next_check:
            return
        try:
            cfg = self._load(self.config_path)
        except ConfigError:
            return  # check() already says it
        minutes = cfg.scheduler.check_minutes
        self._next_check = time.monotonic() + (minutes or 60) * 60
        if not minutes:
            return
        try:
            self._watch.check(cfg)
        except Exception:  # pragma: no cover - never stops the scheduler
            log.exception("system check")

    def _previous_stop(self, cfg: Config) -> None:
        """Started again: notify if the last run of the scheduler ended without stopping."""
        previous = read_json(cfg.scheduler.directory / STATUS_FILE)
        if previous and not previous.get("stopped") and previous.get("heartbeat"):
            monitor.scheduler_event(cfg, "The scheduler started again after an unexpected stop", [
                f"Its last sign of life: {previous['heartbeat'][:19].replace('T', ' ')} UTC.",
                "Jobs due meanwhile did not run.",
            ])

    def serve(self) -> int:
        """Run until ``stop`` (Ctrl+C, SIGTERM): the running backup is then cancelled and cleaned up."""
        cfg = None
        try:
            cfg = self._load(self.config_path)
            jobs = [n for n, j in cfg.jobs.items() if j.enabled]
            log.info("scheduler started: %d job(s) enabled%s (state in %s)", len(jobs),
                     f" ({', '.join(sorted(jobs))})" if jobs else "", cfg.scheduler.directory)
            self._previous_stop(cfg)
        except ConfigError as exc:
            log.error("configuration: %s", exc)
        worker = threading.Thread(target=self.work, name="scheduler-worker", daemon=True)
        worker.start()
        try:
            while not self.stop.is_set():
                self.check()
                self.system_check()
                self.stop.wait(self._tick)
        except KeyboardInterrupt:
            log.info("scheduler stopping%s", f": job {self._running} is cancelled" if self._running else "")
        finally:
            running = self._running
            self.stop.set()
            with self._cond:
                self._cond.notify_all()
            worker.join()
            self._write_status(stopped=True)
            if cfg is not None:
                monitor.scheduler_event(cfg, "The scheduler stopped", [
                    "Jobs do not run until it starts again." + (f" Job {running} was cancelled." if running else ""),
                ])
        return 0
