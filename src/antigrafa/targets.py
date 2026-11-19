"""Backup destinations: local disk or mounted NFS share.

Repository layout:

    <root>/.chunks/...                             shared chunks (deduplication)
    <root>/<source>/vm/<guest id>/<timestamp>/     one VM backup
    <root>/<source>/ct/<guest id>/<timestamp>/     one container backup
        manifest.json                              written last = backup is complete
        <disk>.img.fidx | <disk>.tar.fidx          chunk index

``<source>`` is the name of the ``[source.<name>]`` section: guests of different
clusters or platforms never mix, even when their ids are the same.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .chunkstore import ChunkStore
from .index import MANIFEST_NAME, read_manifest
from .platforms.base import KINDS, check_name

TS_FORMAT = "%Y-%m-%dT%H-%M-%SZ"

log = logging.getLogger(__name__)


class TargetUnavailable(Exception):
    pass


@dataclass
class TargetConfig:
    name: str
    path: str
    kind: str = "local"  # "local" or "nfs"
    # "file": fsync every chunk (safe, slow); "filesystem": syncfs at the end of the
    # backup, before the manifest (default); "none": leave it to the kernel.
    sync: str = "filesystem"

    def __post_init__(self) -> None:
        if self.kind not in ("local", "nfs"):
            raise ValueError(f"target {self.name}: unknown kind {self.kind!r}")
        if self.sync not in ("file", "filesystem", "none"):
            raise ValueError(f"target {self.name}: unknown sync mode {self.sync!r}")


def _unescape_mount(field: str) -> str:
    return field.replace("\\040", " ").replace("\\011", "\t").replace("\\134", "\\")


def mount_fstype(path: str, mounts_file: str = "/proc/mounts") -> str | None:
    """Filesystem type of the mount containing ``path`` (longest matching mount point)."""
    real = os.path.realpath(path)
    best, best_type = "", None
    with open(mounts_file) as f:
        for line in f:
            parts = line.split()
            if len(parts) < 3:
                continue
            mnt, fstype = _unescape_mount(parts[1]), parts[2]
            if (real == mnt or real.startswith(mnt.rstrip("/") + "/")) and len(mnt) >= len(best):
                best, best_type = mnt, fstype
    return best_type


def _libc() -> ctypes.CDLL | None:
    """The C library, or None where there is none to load (Windows)."""
    if os.name != "posix":
        return None
    try:
        # find_library() may return None (no ldconfig): CDLL(None) is then the
        # running process, which links the C library anyway.
        return ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    except OSError:
        return None


def filesystem_sync_supported() -> bool:
    """True when the whole filesystem can be flushed at once (syncfs or sync)."""
    return hasattr(os, "sync")


def syncfs(path: Path) -> None:
    """Flush the filesystem containing ``path`` to stable storage (local or NFS)."""
    libc = _libc()
    if libc is None or not hasattr(libc, "syncfs"):
        if not filesystem_sync_supported():
            raise OSError(errno.ENOSYS, "no filesystem-wide sync on this platform", str(path))
        os.sync()
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        if libc.syncfs(fd) != 0:
            err = ctypes.get_errno()
            raise OSError(err, os.strerror(err), str(path))
    finally:
        os.close(fd)


class Repository:
    def __init__(self, cfg: TargetConfig) -> None:
        self.cfg = cfg
        self.root = Path(cfg.path)
        # Without a filesystem-wide flush (Windows), "filesystem" falls back to an
        # fsync per chunk: slower, but chunks are still on disk before the manifest.
        self._flush_at_end = cfg.sync != "none" and filesystem_sync_supported()
        sync_each = cfg.sync == "file" or (cfg.sync == "filesystem" and not self._flush_at_end)
        self.store = ChunkStore(self.root, sync_each=sync_each)

    def open(self) -> None:
        """Check that the destination is usable before writing anything."""
        if not self.root.is_dir():
            raise TargetUnavailable(f"{self.cfg.name}: {self.root} does not exist")
        if self.cfg.kind == "nfs":
            # Safety net: if the share is not mounted we would write to the local disk.
            fstype = mount_fstype(str(self.root))
            if not fstype or not fstype.startswith("nfs"):
                raise TargetUnavailable(
                    f"{self.cfg.name}: {self.root} is not on an NFS mount (type: {fstype})"
                )
        self.store.init()
        log.debug("target %s: %s (%s, sync=%s)", self.cfg.name, self.root, self.cfg.kind, self.cfg.sync)

    def sync(self) -> None:
        if self._flush_at_end:
            syncfs(self.root)

    def create_snapshot_dir(self, source: str, kind: str, guest_id: str, when: datetime) -> Path:
        if kind not in KINDS:
            raise ValueError(f"unknown guest kind {kind!r}")
        # Each part becomes one directory: never "..", never a separator
        parts = (check_name(source, "source name"), kind, check_name(guest_id, "guest id"))
        path = self.root.joinpath(*parts, when.strftime(TS_FORMAT))
        path.mkdir(parents=True, exist_ok=False)
        return path

    def resolve(self, snapshot: str) -> Path:
        path = (self.root / snapshot).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"invalid backup path: {snapshot}")
        return path

    def list_snapshots(self) -> list[tuple[str, dict]]:
        """Complete backups (with a manifest), sorted by path."""
        found = []
        for source in sorted(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(".")):
            for kind in KINDS:
                base = source / kind
                if not base.is_dir():
                    continue
                for manifest in sorted(base.glob(f"*/*/{MANIFEST_NAME}")):
                    snap = manifest.parent
                    found.append((str(snap.relative_to(self.root)), read_manifest(snap)))
        return found
