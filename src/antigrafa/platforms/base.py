"""What the backup engine expects from a virtualization platform.

The engine (``jobs``, ``cli``, the destination) knows nothing about a particular
hypervisor. Each platform (Proxmox VE today; VMware, Hyper-V... later) provides:

* the list of guests (``Platform.guests``);
* a ``BackupPlan`` for one guest: its configuration and one ``DiskSource`` per disk,
  built *before* the snapshot so that an unsupported disk fails early;
* a crash-consistent snapshot it can create and delete;
* optionally, a load measure of the host running the guest, used to slow reads down.

Snapshot first, read afterwards, at the destination's pace: the guest never waits
for the backup.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from collections.abc import Callable, Iterator
from typing import BinaryIO, Protocol

KINDS = ("vm", "ct")  # virtual machine, container

# Guest ids and source names become directory names in the repository
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]*$")


class PlatformError(Exception):
    """Error reported by a platform (API refused, unreachable host...)."""


class AccessDenied(PlatformError):
    """The credentials lack rights: checked before any action, nothing was done."""


def check_name(value: str, what: str) -> str:
    """Refuse a name that is not a single, safe path component."""
    if not _SAFE_NAME.match(value):
        raise ValueError(f"invalid {what} {value!r}: use letters, digits, '.', '_' or '-'")
    return value


@dataclass(frozen=True)
class Guest:
    id: str  # the platform's own identifier: "100" (Proxmox), "vm-42" (VMware)...
    kind: str  # "vm" or "ct"
    name: str
    host: str  # machine running the guest: Proxmox node, ESXi or Hyper-V host
    status: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown guest kind {self.kind!r}")
        check_name(self.id, "guest id")

    @property
    def label(self) -> str:
        return f"{self.kind.upper()} {self.id} ({self.name})"


class Stream(Protocol):
    """An open disk stream: a context manager exposing the bytes as ``stdout``."""

    stdout: BinaryIO

    def __enter__(self) -> Stream: ...

    def __exit__(self, exc_type, exc, tb) -> None: ...


class DiskSource(ABC):
    """Reads one disk of the snapshot."""

    key: str  # name of the disk in the guest: scsi0, rootfs...
    volume: str  # where the platform stores it, recorded in the manifest
    format: str = "raw"  # "raw" (disk image) or "tar" (file tree)
    # How the open stream reads the disk, for the log ("incremental since ...")
    read_mode: str = ""

    def manifest_extra(self) -> dict:
        """What this disk adds to its entry in the manifest (what the next backup needs)."""
        return {}

    def prepare(self) -> None:
        """Make the snapshot readable (device node, activation...)."""

    @abstractmethod
    def size(self) -> int | None:
        """Exact size of the stream, when known in advance."""

    def estimated_size(self) -> int | None:
        """Approximate size when ``size`` is unknown (a file archive), for progress only.

        Never used to check the backup. Must not raise: return None instead.
        """
        return None

    def open_hashed_stream(self, reference: list[bytes], chunk_size: int) -> Stream | None:
        """Read the snapshot hashed on the source host, against the digests of the
        previous backup: only changed chunks are sent (protocol: ``antigrafa.delta``).

        ``None``: not supported here, ``open_stream`` is used instead.
        """
        return None

    @abstractmethod
    def open_stream(self) -> Stream:
        """Start reading the snapshot of this disk."""

    def reopener(self, chunk_size: int) -> Callable[[int], Stream] | None:
        """After ``open_stream`` or ``open_hashed_stream``: opens that same read again on a
        new connection, from a given chunk on, when the first connection became too slow
        (see ``antigrafa.reconnect``). Its streams, and the first one, then also have
        ``kill()`` and ``close(abort)``.

        None: this read cannot start mid-way.
        """
        return None

    def cleanup(self) -> None:
        """Undo what ``prepare`` did. Must be safe even if ``prepare`` failed."""


@dataclass
class RestoreRequest:
    """Where a backup goes back: which guest id, host, storage, and whether it may replace one."""

    guest_id: str
    host: str = ""  # "": the host the guest was on
    storage: str = ""  # "": each disk on the storage it came from
    overwrite: bool = False  # the guest id exists: replace it, disks included (stopped)
    disk_only: bool = False  # the guest id exists: only add the disks to it, as unused
    start: bool = False  # start the guest once restored
    # The guest replaced is running: "" it keeps running while its disks are copied, then
    # is shut down (stopped hard if needed) just before it is replaced; "stop" shut it down
    # before the restore starts (or fail), "force-stop" the same, stopped hard if needed
    stop: str = ""


class RestoreDisk(ABC):
    """One disk of a backup, as the platform writes it back."""

    key: str  # name of the disk in the guest: scsi0, rootfs...
    format: str  # "raw" or "tar"
    size: int  # bytes of the stream
    chunk_size: int

    @abstractmethod
    def chunks(self) -> Iterator[bytes | None]:
        """The stream, chunk by chunk, in order; None: a whole chunk of zeros (not read)."""


@dataclass
class BackupPlan:
    config: dict  # guest configuration, kept in the manifest to recreate the guest
    disks: list[DiskSource] = field(default_factory=list)


class Platform(ABC):
    """One configured source: a cluster or a host of a given platform."""

    type: str  # "proxmox", ...

    def __init__(self, name: str) -> None:
        self.name = name  # name of the [source.<name>] section

    @abstractmethod
    def guests(self) -> list[Guest]:
        """Every guest that can be backed up, sorted by id."""

    @abstractmethod
    def plan_backup(
        self, guest: Guest, snapshot: str, low_priority: bool = True, previous: dict | None = None
    ) -> BackupPlan:
        """Configuration and disk readers of ``guest``, for the snapshot named ``snapshot``.

        Called before the snapshot exists: raise here if a disk cannot be backed up.
        ``previous``: manifest of the guest's last complete backup (incremental reads).
        """

    @abstractmethod
    def create_snapshot(self, guest: Guest, snapshot: str) -> None:
        """Consistent snapshot of every disk of ``guest``; returns once it exists."""

    @abstractmethod
    def delete_snapshot(self, guest: Guest, snapshot: str) -> None:
        """All of ``snapshot``, also what ``release_snapshot`` kept."""

    def release_snapshot(self, guest: Guest, snapshot: str) -> bool:
        """Every disk was read: delete the snapshot, except what the next backup will
        use as its base. True when something was kept (``delete_snapshot`` removes it
        if the backup fails after all)."""
        self.delete_snapshot(guest, snapshot)
        return False

    def drop_previous_bases(self, guest: Guest, snapshot: str) -> None:
        """The backup of ``snapshot`` is complete: what the previous one kept can go."""

    def check_access(self, guests: list[Guest]) -> None:
        """Raise ``AccessDenied`` if the credentials cannot back up all of ``guests``.

        Called before anything is done (no snapshot, no write): a missing right stops
        the whole run with the list of what is missing, instead of failing halfway.
        With an empty list, checks that guests can be listed at all.
        """

    def disk_sizes(self, guest: Guest) -> dict[str, int]:
        """Expected size of each disk to back up, by key, from the configuration (cheap).

        Only for the progress of a whole run, before the guest's turn; {}: unknown.
        """
        return {}

    def leftover_snapshots(self, guest: Guest, prefix: str) -> list[str]:
        """Names of snapshots starting with ``prefix`` (left by an interrupted run)."""
        return []

    def restore_guest(
        self, manifest: dict, disks: list[RestoreDisk], request: RestoreRequest, progress, cancel
    ) -> str:
        """Recreate the guest of ``manifest`` from its ``disks``; returns what was created.

        Checks everything it can before changing anything; on failure, removes what it
        created. ``progress``: a ``Progress`` (one task per disk); ``cancel``: an Event.
        """
        raise PlatformError(f"restoring guests is not supported on {self.type}")

    def hosts(self) -> list[dict]:
        """The hosts of this source, for the overview of the web page: ``name``,
        ``status`` ("online", "offline"...) and ``address`` (how Antigrafa reaches it).
        [] when the platform cannot list them."""
        return []

    def restore_locations(self, kind: str) -> list[dict]:
        """Where a guest of ``kind`` can be restored: each host, and its storages that can
        hold its disks.

        ``[{"host", "online", "storages": [{"id", "type", "avail", "total", "shared"}]}]``,
        sizes in bytes (None: unknown).
        """
        raise PlatformError(f"restoring guests is not supported on {self.type}")

    def host_iowait(self, host: str) -> float | None:
        """Fraction of CPU time ``host`` spends waiting on disks (0.0 to 1.0).

        ``None``: the platform cannot measure it, reads are then not throttled by load.
        """
        return None
