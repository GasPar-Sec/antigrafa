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

    @abstractmethod
    def open_stream(self) -> Stream:
        """Start reading the snapshot of this disk."""

    def cleanup(self) -> None:
        """Undo what ``prepare`` did. Must be safe even if ``prepare`` failed."""


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
    def plan_backup(self, guest: Guest, snapshot: str, low_priority: bool = True) -> BackupPlan:
        """Configuration and disk readers of ``guest``, for the snapshot named ``snapshot``.

        Called before the snapshot exists: raise here if a disk cannot be backed up.
        """

    @abstractmethod
    def create_snapshot(self, guest: Guest, snapshot: str) -> None:
        """Consistent snapshot of every disk of ``guest``; returns once it exists."""

    @abstractmethod
    def delete_snapshot(self, guest: Guest, snapshot: str) -> None: ...

    def check_access(self, guests: list[Guest]) -> None:
        """Raise ``AccessDenied`` if the credentials cannot back up all of ``guests``.

        Called before anything is done (no snapshot, no write): a missing right stops
        the whole run with the list of what is missing, instead of failing halfway.
        With an empty list, checks that guests can be listed at all.
        """

    def leftover_snapshots(self, guest: Guest, prefix: str) -> list[str]:
        """Names of snapshots starting with ``prefix`` (left by an interrupted run)."""
        return []

    def host_iowait(self, host: str) -> float | None:
        """Fraction of CPU time ``host`` spends waiting on disks (0.0 to 1.0).

        ``None``: the platform cannot measure it, reads are then not throttled by load.
        """
        return None
