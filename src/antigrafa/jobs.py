"""Orchestration: several VMs and containers in parallel, one shared destination.

Works with any platform (see ``antigrafa.platforms``). For each guest:
  1. read its configuration and pick a reader per disk (fails before any snapshot);
  2. platform snapshot: instantaneous, the guest resumes right away;
  3. read every disk from the snapshot -> chunks -> destination (adaptive pace);
  4. delete the snapshot, whatever happens;
  5. syncfs, then the manifest: a backup only exists once it is complete.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, TypeVar

from . import __version__
from .adaptive import MiB, SourceThrottle, WriteController, WriteTuning
from .config import Tuning
from .index import IndexCorrupted, read_index, read_manifest, write_index, write_manifest
from .delta import read_records
from .pipeline import StreamStats, backup_records, raw_records
from .platforms.base import DiskSource, Guest, Platform
from .progress import Progress
from .reconnect import ReconnectingReader, StreamWatch
from .targets import Repository

log = logging.getLogger(__name__)

SNAP_PREFIX = "antigrafa_"

T = TypeVar("T")

# Phases of a guest backup, in order (keys of GuestResult.timings)
PHASES = ("wait", "prepare", "snapshot", "transfer", "cleanup", "flush")


class BackupError(Exception):
    pass


class BackupCancelled(BackupError):
    """Stopped on request: the snapshot is deleted and the partial backup removed."""

    def __init__(self) -> None:
        super().__init__("cancelled")


@dataclass
class DiskResult:
    key: str
    volume: str
    format: str
    size: int
    stats: StreamStats
    extra: dict = field(default_factory=dict)  # added to its manifest entry (see DiskSource)


@dataclass
class GuestResult:
    guest: Guest
    ok: bool
    error: str = ""
    snapshot: str = ""
    disks: list[DiskResult] = field(default_factory=list)
    duration: float = 0.0
    # Seconds spent in each phase reached (see PHASES), even when the backup failed
    timings: dict[str, float] = field(default_factory=dict)


@contextlib.contextmanager
def _timed(timings: dict[str, float], phase: str):
    """Adds the time spent in the block to ``timings[phase]``, even on error."""
    start = time.monotonic()
    try:
        yield
    finally:
        timings[phase] = timings.get(phase, 0.0) + time.monotonic() - start


def write_tuning(t: Tuning) -> WriteTuning:
    return WriteTuning(
        min_concurrency=t.writer_min_concurrency,
        max_concurrency=t.writer_max_concurrency,
        start_concurrency=t.writer_start_concurrency,
        target_latency=t.writer_target_latency_ms / 1000,
        start_level=t.compression_level,
        min_level=t.compression_min_level,
        max_level=t.compression_max_level,
    )


class BackupRunner:
    # Seconds between the tries to delete a snapshot (about 30 s in all)
    delete_retry_delays: tuple[float, ...] = (2, 4, 8, 16)

    def __init__(
        self,
        tuning: Tuning,
        platform: Platform,
        repo: Repository,
        progress: Progress | None = None,
        cancel: threading.Event | None = None,
        on_result: Callable[[GuestResult], None] | None = None,
    ) -> None:
        self.tuning = tuning
        # Set: the disks being read stop at the next chunk, guests not started never start
        self.cancel = cancel if cancel is not None else threading.Event()
        self.on_result = on_result  # called as each guest ends, from its thread
        self.platform = platform
        self.repo = repo
        self.progress = progress if progress is not None else Progress(enabled=False)
        self._guests_total = 0
        self._guests_done = 0
        self.chunk_size = tuning.chunk_size_mib * MiB
        # One controller and one writer pool for the destination,
        # shared by every concurrent backup.
        self.controller = WriteController(write_tuning(tuning))
        self.watch = StreamWatch(
            min_rate=tuning.stream_min_mib_s * MiB,
            window=tuning.stream_window_s,
            max_reconnects=tuning.stream_max_reconnects,
        )
        self._writers = ThreadPoolExecutor(max_workers=tuning.writer_max_concurrency, thread_name_prefix="writer")
        self._lock = threading.Lock()
        self._throttles: dict[str, SourceThrottle] = {}
        self._host_slots: dict[str, threading.Semaphore] = {}

    def close(self) -> None:
        self._writers.shutdown(wait=True)

    def _iowait(self, host: str) -> float:
        # A platform that cannot measure the load reports None: never throttled by load
        value = self.platform.host_iowait(host)
        return 0.0 if value is None else value

    def _throttle(self, host: str) -> SourceThrottle:
        with self._lock:
            if host not in self._throttles:
                t = self.tuning
                self._throttles[host] = SourceThrottle(
                    probe=lambda: self._iowait(host),
                    max_rate=t.source_max_mib_s * MiB if t.source_max_mib_s > 0 else None,
                    min_rate=t.source_min_mib_s * MiB,
                    high=t.host_iowait_high,
                    low=t.host_iowait_low,
                    name=host,
                )
            return self._throttles[host]

    def _host_slot(self, host: str) -> threading.Semaphore:
        with self._lock:
            if host not in self._host_slots:
                self._host_slots[host] = threading.Semaphore(self.tuning.max_parallel_per_host)
            return self._host_slots[host]

    def run(self, guests: list[Guest]) -> list[GuestResult]:
        self._guests_total = len(guests)
        self._guests_done = 0
        self._announce(guests)
        self._update_status()
        with ThreadPoolExecutor(max_workers=self.tuning.max_parallel_guests, thread_name_prefix="guest") as pool:
            return list(pool.map(self._safe_backup, guests))

    def _announce(self, guests: list[Guest]) -> None:
        """Every disk of the run in the overall progress from the start (estimated sizes)."""
        for g in guests:
            try:
                sizes = self.platform.disk_sizes(g)
            except Exception as exc:  # progress only: never stops the backup
                log.debug("%s: disk sizes unknown: %s", g.label, exc)
                continue
            for key, size in sizes.items():
                self.progress.expect(f"{g.id}/{key}", size)

    def _check_cancel(self) -> None:
        if self.cancel.is_set():
            raise BackupCancelled()

    def _update_status(self) -> None:
        self.progress.set_status(f"{self._guests_done}/{self._guests_total} guests")

    def _safe_backup(self, g: Guest) -> GuestResult:
        start = time.monotonic()
        timings: dict[str, float] = {}
        try:
            result = self.backup_guest(g, timings)
        except BackupCancelled as exc:
            log.warning("%s: cancelled", g.label)
            result = GuestResult(guest=g, ok=False, error=str(exc), timings=timings)
        except Exception as exc:
            log.error("%s: failed: %s", g.label, exc)
            result = GuestResult(guest=g, ok=False, error=str(exc), timings=timings)
        result.duration = time.monotonic() - start
        self.progress.drop_expected(f"{g.id}/", prefix=True)  # disks never read (failure, cancel)
        with self._lock:
            self._guests_done += 1
            self._update_status()
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception as exc:  # a display problem never fails the backup
                log.error("%s: result not reported: %s", g.label, exc)
        return result

    def backup_guest(self, g: Guest, timings: dict[str, float] | None = None) -> GuestResult:
        timings = {} if timings is None else timings
        slot = self._host_slot(g.host)
        with _timed(timings, "wait"):  # other guests of the same host first
            slot.acquire()
        try:
            self._check_cancel()  # cancelled while waiting: nothing done yet
            with _timed(timings, "prepare"):
                now = datetime.now(timezone.utc)
                snapname = SNAP_PREFIX + now.strftime("%Y%m%dT%H%M%S")
                previous_dir, previous = self._previous(g)
                plan = self.platform.plan_backup(g, snapname, self.tuning.low_priority, previous=previous)
                if not plan.disks:
                    raise BackupError("no disk to back up")
                for src in plan.disks:
                    log.debug("%s: %s: %s, read by %s (%s)", g.label, src.key, src.volume, type(src).__name__, src.format)
                self._warn_stale_snapshots(g)
                dest = self.repo.create_snapshot_dir(self.platform.name, g.kind, g.id, now)
            kept = False  # part of the snapshot stays as the base of the next backup
            try:
                self._check_cancel()
                log.info("%s: snapshot %s", g.label, snapname)
                with _timed(timings, "snapshot"):
                    self.platform.create_snapshot(g, snapname)
                try:
                    with _timed(timings, "transfer"):
                        results = [self._backup_disk(g, src, dest, previous_dir, previous) for src in plan.disks]
                except BaseException:
                    with _timed(timings, "cleanup"):
                        self._delete_snapshot(g, snapname)
                    raise
                with _timed(timings, "cleanup"):
                    kept = self._retried(g, snapname, lambda: self.platform.release_snapshot(g, snapname))
                with _timed(timings, "flush"):
                    t0 = time.monotonic()
                    self.repo.sync()
                    log.debug("%s: destination flushed in %.1f s", g.label, time.monotonic() - t0)
                    write_manifest(dest, self._manifest(g, now, snapname, plan.config, results))
            except BaseException:
                shutil.rmtree(dest, ignore_errors=True)  # chunks stay, the incomplete index goes
                if kept:  # a base for a backup that does not exist: the previous one stays
                    with contextlib.suppress(Exception):  # logged; the first error is the one to report
                        self._delete_snapshot(g, snapname)
                raise
        finally:
            slot.release()

        try:  # the new backup is complete: the base the previous one kept is useless
            self.platform.drop_previous_bases(g, snapname)
        except Exception as exc:
            log.warning("%s: previous base snapshot not deleted (remove it by hand): %s", g.label, exc)
        rel = str(dest.relative_to(self.repo.root))
        log.info("%s: backup complete -> %s", g.label, rel)
        return GuestResult(guest=g, ok=True, snapshot=rel, disks=results, timings=timings)

    def _backup_disk(
        self, g: Guest, src: DiskSource, dest: Path, previous_dir: Path | None = None, previous: dict | None = None
    ) -> DiskResult:
        try:
            src.prepare()
            size = src.size()
            log.info("%s: %s (%s) -> reading", g.label, src.key, src.volume)
            estimate = src.estimated_size() if size is None else None
            if size is not None:
                log.debug("%s: %s: size %d bytes", g.label, src.key, size)
            else:
                log.debug("%s: %s: size unknown (archive), estimate %s", g.label, src.key, estimate)
            self.progress.drop_expected(f"{g.id}/{src.key}")  # the task has the real size
            task = self.progress.task(
                size if size is not None else estimate,
                name=f"{g.id}/{src.key}",
                approximate=size is None,
            )
            reference = self._reference(g, src, previous_dir, previous)

            def advance(n: int) -> None:
                task.advance(n)
                self._check_cancel()  # raised through the read: stream closed, snapshot deleted

            with task:
                self._check_cancel()
                hashed = src.open_hashed_stream(reference, self.chunk_size) if reference else None
                read_mode = getattr(src, "read_mode", "")
                if read_mode:
                    log.info("%s: %s: %s", g.label, src.key, read_mode)
                if hashed is not None:
                    log.debug("%s: %s: hashed on the source, %d chunks of reference", g.label, src.key, len(reference))
                else:
                    log.debug("%s: %s: every chunk sent", g.label, src.key)
                stream = hashed or src.open_stream()
                chunk_size = self.chunk_size

                def parse(stdout, start: int):
                    if hashed is not None:  # numbered from chunk ``start``: the reference too
                        return read_records(stdout, reference[start:])
                    return raw_records(stdout, chunk_size)

                reader = ReconnectingReader(
                    stream, parse, src.reopener(chunk_size), self.watch, f"{g.label}: {src.key}", chunk_size
                )
                with reader:
                    store, throttle = self.repo.store, self._throttle(g.host)
                    digests, stats = backup_records(
                        reader.records(), store, self.controller, self._writers, throttle, advance
                    )
                stats.reconnects = reader.reconnects
        finally:
            try:
                src.cleanup()
            except Exception as exc:
                log.error("%s: %s: cleanup failed: %s", g.label, src.key, exc)

        if size is not None and stats.bytes_read != size:
            raise BackupError(f"{src.key}: read {stats.bytes_read} bytes instead of {size}")
        size = stats.bytes_read
        ext = "img" if src.format == "raw" else "tar"
        write_index(dest / f"{src.key}.{ext}.fidx", size, self.chunk_size, digests)
        rate = stats.bytes_read / MiB / stats.elapsed if stats.elapsed else 0.0
        skipped = f", {stats.skipped_chunks} unchanged chunks not read" if stats.skipped_chunks else ""
        if stats.reconnects:
            skipped += f", {stats.reconnects} new connection{'s' if stats.reconnects > 1 else ''}"
        log.info(
            "%s: %s: %.0f MiB in %.0f s (%.0f MiB/s), %d new chunks, %d reused, %.0f MiB sent%s",
            g.label, src.key, size / MiB, stats.elapsed, rate, stats.new_chunks, stats.reused_chunks,
            stats.sent_bytes / MiB, skipped,
        )
        return DiskResult(
            key=src.key, volume=src.volume, format=src.format, size=size, stats=stats, extra=getattr(src, "manifest_extra", dict)()
        )

    def _previous(self, g: Guest) -> tuple[Path | None, dict | None]:
        """Directory and manifest of the guest's last complete backup (None: none readable)."""
        previous = self.repo.latest_backup(self.platform.name, g.kind, g.id)
        if previous is None:
            return None, None
        try:
            manifest = read_manifest(previous)
        except (OSError, ValueError) as exc:
            log.warning("%s: previous backup unreadable (%s): every chunk sent", g.label, exc)
            return None, None
        return previous, manifest if isinstance(manifest, dict) else None

    def _reference(
        self, g: Guest, src: DiskSource, previous_dir: Path | None = None, previous: dict | None = None
    ) -> list[bytes]:
        """Digests of the same disk in the previous complete backup ([]: none usable)."""
        if previous_dir is None:
            previous_dir, previous = self._previous(g)
        if previous_dir is None or previous is None:
            return []
        try:
            disks = previous["disks"]
            entry = next((d for d in disks if d["key"] == src.key and d["format"] == src.format), None)
            if entry is None:
                return []
            _, chunk_size, digests = read_index(previous_dir / entry["index"])
        except (OSError, ValueError, KeyError, TypeError, IndexCorrupted) as exc:
            log.warning("%s: %s: previous backup unreadable (%s): every chunk sent", g.label, src.key, exc)
            return []
        return digests if chunk_size == self.chunk_size else []

    def _delete_snapshot(self, g: Guest, snapname: str) -> None:
        self._retried(g, snapname, lambda: self.platform.delete_snapshot(g, snapname))

    def _retried(self, g: Guest, snapname: str, action: Callable[[], T]) -> T:
        """Delete (all or part of) the snapshot, tried again on failure.

        After a stopped read (cancel, error), the reader on the host can keep the
        snapshot open a few more seconds ("dataset is busy" on ZFS).
        """
        for delay in (*self.delete_retry_delays, None):
            try:
                return action()
            except Exception as exc:
                if delay is None:
                    # A forgotten snapshot grows with every VM write: report it loudly.
                    log.error("%s: FAILED to delete snapshot %s, remove it by hand: %s", g.label, snapname, exc)
                    raise
                log.warning("%s: snapshot %s not deleted yet, new try in %g s: %s", g.label, snapname, delay, exc)
                time.sleep(delay)

    def _warn_stale_snapshots(self, g: Guest) -> None:
        try:
            stale = self.platform.leftover_snapshots(g, SNAP_PREFIX)
        except Exception:
            return
        if stale:
            log.warning("%s: Antigrafa snapshots left over from a previous run: %s", g.label, ", ".join(stale))

    def _manifest(self, g: Guest, when: datetime, snapname: str, config: dict, results: list[DiskResult]) -> dict:
        return {
            "format_version": 2,
            "antigrafa_version": __version__,
            "source": self.platform.name,
            "platform": self.platform.type,
            "guest": {"id": g.id, "kind": g.kind, "name": g.name, "host": g.host},
            "time": when.isoformat(),
            "snapshot": snapname,
            "config": config,  # to recreate the guest on restore
            "disks": [
                {
                    "key": r.key,
                    "volume": r.volume,
                    "format": r.format,
                    "size": r.size,
                    "index": f"{r.key}.{'img' if r.format == 'raw' else 'tar'}.fidx",
                    "stats": r.stats.as_dict(),
                    **r.extra,
                }
                for r in results
            ],
        }
