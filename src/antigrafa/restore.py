"""Restoring a backup into its platform: the guest recreated from the repository.

The engine reads the backup (manifest, indexes, chunks read ahead in parallel and
checked); the platform recreates the guest and writes its disks back (see
``Platform.restore_guest``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .chunkstore import ChunkStore, digest_of
from .index import read_index, read_manifest
from .pipeline import READ_AHEAD, in_order
from .platforms.base import Platform, PlatformError, RestoreDisk, RestoreRequest
from .progress import Progress
from .targets import Repository

log = logging.getLogger(__name__)


class BackupDisk(RestoreDisk):
    """One disk of a backup in the repository."""

    def __init__(self, store: ChunkStore, entry: dict, size: int, chunk_size: int, digests: list[bytes],
                 jobs: int = 4) -> None:
        self.store = store
        self.key = str(entry["key"])
        self.format = str(entry.get("format", "raw"))
        self.size = size
        self.chunk_size = chunk_size
        self.digests = digests
        self.jobs = max(1, jobs)

    def chunks(self) -> Iterator[bytes | None]:
        zero = digest_of(bytes(self.chunk_size))

        def load(digest: bytes) -> bytes | None:
            return None if digest == zero else self.store.read(digest)

        with ThreadPoolExecutor(self.jobs, thread_name_prefix="restore") as pool:
            yield from in_order(pool, load, self.digests, self.jobs * READ_AHEAD)


def backup_disks(repo: Repository, backup_dir: Path, manifest: dict, jobs: int = 4) -> list[BackupDisk]:
    """Every disk of the backup, its index read (fails before anything is restored)."""
    disks = []
    for entry in manifest.get("disks") or []:
        size, chunk_size, digests = read_index(backup_dir / entry["index"])
        disks.append(BackupDisk(repo.store, entry, size, chunk_size, digests, jobs))
    if not disks:
        raise PlatformError("this backup has no disk")
    return disks


def restore_guest(
    platform: Platform,
    repo: Repository,
    backup: str,
    request: RestoreRequest,
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    jobs: int = 4,
) -> str:
    """Restore the backup ``backup`` (path in the repository) as ``request`` says."""
    backup_dir = repo.resolve(backup)
    manifest = read_manifest(backup_dir)
    if manifest.get("source") not in (None, platform.name):
        raise PlatformError(f"{backup} comes from source {manifest.get('source')}, not {platform.name}")
    disks = backup_disks(repo, backup_dir, manifest, jobs)
    log.info("restoring %s as %s %s", backup, manifest.get("guest", {}).get("kind", "?").upper(), request.guest_id)
    return platform.restore_guest(
        manifest, disks, request, progress if progress is not None else Progress(enabled=False),
        cancel if cancel is not None else threading.Event(),
    )
