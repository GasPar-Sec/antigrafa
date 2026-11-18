"""Orchestration: several VMs and containers in parallel, one shared destination.

For each guest:
  1. read its configuration and pick a reader per disk (fails before any snapshot);
  2. Proxmox snapshot: instantaneous, the VM resumes right away;
  3. read every disk from the snapshot over SSH -> chunks -> destination (adaptive pace);
  4. delete the snapshot, whatever happens;
  5. syncfs, then the manifest: a backup only exists once it is complete.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .adaptive import MiB, SourceThrottle, WriteController, WriteTuning
from .capture import Disk, VolumeSource, guest_disks, make_source
from .config import Tuning
from .index import write_index, write_manifest
from .pipeline import StreamStats, backup_stream
from .proxmox import Guest, ProxmoxAPI
from .ssh import SSHHost
from .targets import Repository

log = logging.getLogger(__name__)

SNAP_PREFIX = "antigrafa_"


class BackupError(Exception):
    pass


@dataclass
class DiskResult:
    key: str
    volid: str
    format: str
    size: int
    stats: StreamStats


@dataclass
class GuestResult:
    guest: Guest
    ok: bool
    error: str = ""
    snapshot: str = ""
    disks: list[DiskResult] = field(default_factory=list)
    duration: float = 0.0


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
    def __init__(
        self,
        tuning: Tuning,
        api: ProxmoxAPI,
        repo: Repository,
        ssh_for_node: Callable[[str], SSHHost],
    ) -> None:
        self.tuning = tuning
        self.api = api
        self.repo = repo
        self.ssh_for_node = ssh_for_node
        self.chunk_size = tuning.chunk_size_mib * MiB
        # One controller and one writer pool for the destination,
        # shared by every concurrent backup.
        self.controller = WriteController(write_tuning(tuning))
        self._writers = ThreadPoolExecutor(max_workers=tuning.writer_max_concurrency, thread_name_prefix="writer")
        self._lock = threading.Lock()
        self._throttles: dict[str, SourceThrottle] = {}
        self._node_slots: dict[str, threading.Semaphore] = {}

    def close(self) -> None:
        self._writers.shutdown(wait=True)

    def _throttle(self, node: str) -> SourceThrottle:
        with self._lock:
            if node not in self._throttles:
                t = self.tuning
                self._throttles[node] = SourceThrottle(
                    probe=lambda: self.api.node_iowait(node),
                    max_rate=t.source_max_mib_s * MiB if t.source_max_mib_s > 0 else None,
                    min_rate=t.source_min_mib_s * MiB,
                    high=t.node_iowait_high,
                    low=t.node_iowait_low,
                    name=node,
                )
            return self._throttles[node]

    def _node_slot(self, node: str) -> threading.Semaphore:
        with self._lock:
            if node not in self._node_slots:
                self._node_slots[node] = threading.Semaphore(self.tuning.max_parallel_per_node)
            return self._node_slots[node]

    def run(self, guests: list[Guest]) -> list[GuestResult]:
        with ThreadPoolExecutor(max_workers=self.tuning.max_parallel_guests, thread_name_prefix="guest") as pool:
            return list(pool.map(self._safe_backup, guests))

    def _safe_backup(self, g: Guest) -> GuestResult:
        start = time.monotonic()
        try:
            result = self.backup_guest(g)
        except Exception as exc:
            log.error("%s: failed: %s", g.label, exc)
            result = GuestResult(guest=g, ok=False, error=str(exc))
        result.duration = time.monotonic() - start
        return result

    def backup_guest(self, g: Guest) -> GuestResult:
        with self._node_slot(g.node):
            config = self.api.guest_config(g)
            disks = guest_disks(g.type, config)
            if not disks:
                raise BackupError("no disk to back up")

            now = datetime.now(timezone.utc)
            snapname = SNAP_PREFIX + now.strftime("%Y%m%dT%H%M%S")
            ssh = self.ssh_for_node(g.node)
            storages = {d.storage: self.api.storage(d.storage) for d in disks}
            sources = [
                make_source(ssh, storages[d.storage], d, snapname, self.tuning.low_priority) for d in disks
            ]
            self._warn_stale_snapshots(g)

            dest = self.repo.create_snapshot_dir(g.type, g.vmid, now)
            try:
                log.info("%s: snapshot %s", g.label, snapname)
                self.api.create_snapshot(g, snapname, "Temporary Antigrafa snapshot, deleted after the backup")
                try:
                    results = [self._backup_disk(g, src, dest) for src in sources]
                finally:
                    self._delete_snapshot(g, snapname)
                self.repo.sync()
                write_manifest(dest, self._manifest(g, now, snapname, config, results))
            except BaseException:
                shutil.rmtree(dest, ignore_errors=True)  # chunks stay, the incomplete index goes
                raise

        rel = str(dest.relative_to(self.repo.root))
        log.info("%s: backup complete -> %s", g.label, rel)
        return GuestResult(guest=g, ok=True, snapshot=rel, disks=results)

    def _backup_disk(self, g: Guest, src: VolumeSource, dest: Path) -> DiskResult:
        disk: Disk = src.disk
        try:
            src.prepare()
            size = src.size()
            log.info("%s: %s (%s) -> reading", g.label, disk.key, disk.volid)
            with src.open_stream() as stream:
                digests, stats = backup_stream(
                    stream.stdout,
                    self.repo.store,
                    self.controller,
                    self._writers,
                    self._throttle(g.node),
                    self.chunk_size,
                )
        finally:
            try:
                src.cleanup()
            except Exception as exc:
                log.error("%s: %s: cleanup failed: %s", g.label, disk.key, exc)

        if size is not None and stats.bytes_read != size:
            raise BackupError(f"{disk.key}: read {stats.bytes_read} bytes instead of {size}")
        size = stats.bytes_read
        ext = "img" if src.format == "raw" else "tar"
        write_index(dest / f"{disk.key}.{ext}.fidx", size, self.chunk_size, digests)
        rate = stats.bytes_read / MiB / stats.elapsed if stats.elapsed else 0.0
        log.info(
            "%s: %s: %.0f MiB in %.0f s (%.0f MiB/s), %d new chunks, %d reused",
            g.label, disk.key, size / MiB, stats.elapsed, rate, stats.new_chunks, stats.reused_chunks,
        )
        return DiskResult(key=disk.key, volid=disk.volid, format=src.format, size=size, stats=stats)

    def _delete_snapshot(self, g: Guest, snapname: str) -> None:
        try:
            self.api.delete_snapshot(g, snapname)
        except Exception as exc:
            # A forgotten snapshot grows with every VM write: report it loudly.
            log.error("%s: FAILED to delete snapshot %s, remove it by hand: %s", g.label, snapname, exc)
            raise

    def _warn_stale_snapshots(self, g: Guest) -> None:
        try:
            stale = [s["name"] for s in self.api.list_snapshots(g) if s.get("name", "").startswith(SNAP_PREFIX)]
        except Exception:
            return
        if stale:
            log.warning("%s: Antigrafa snapshots left over from a previous run: %s", g.label, ", ".join(stale))

    def _manifest(self, g: Guest, when: datetime, snapname: str, config: dict, results: list[DiskResult]) -> dict:
        return {
            "format_version": 1,
            "antigrafa_version": __version__,
            "guest_type": g.type,
            "vmid": g.vmid,
            "name": g.name,
            "node": g.node,
            "time": when.isoformat(),
            "proxmox_snapshot": snapname,
            "config": config,  # to recreate the guest on restore
            "disks": [
                {
                    "key": r.key,
                    "volid": r.volid,
                    "format": r.format,
                    "size": r.size,
                    "index": f"{r.key}.{'img' if r.format == 'raw' else 'tar'}.fidx",
                    "stats": r.stats.as_dict(),
                }
                for r in results
            ],
        }
