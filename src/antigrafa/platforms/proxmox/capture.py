"""Reading disks from a Proxmox snapshot, depending on the storage type.

The snapshot is created through the Proxmox API (consistent across all disks,
filesystems frozen through the qemu-guest-agent), or with ``snapshot_mode = "storage"``
by each ``VolumeSource`` on its own storage, under the name Proxmox would give it. It
is instantaneous: the VM resumes right away. Each ``VolumeSource`` then knows how to
read the snapshot of one volume remotely, over SSH, at whatever pace the destination
allows.

A stopped VM (``snapshot_mode = "qemu"``, locked meanwhile) needs no snapshot: each
source then reads the volume itself (``live``), which also works on storages without
snapshots. A running VM is read through QEMU instead (see ``.qemu``).

Supported storages:
    zfspool         zvol (VM, raw disk) or ``subvol-*`` dataset (CT, tar archive)
    rbd             Ceph RBD (``rbd export`` of the snapshot)
    lvmthin         LVM-thin (``snap_<vol>_<snap>`` snapshot activated for reading)
    lvm             LVM thick: the logical volume itself, stopped VM only
    iscsi           iSCSI LUN used directly as a disk, stopped VM only
    dir, nfs, cifs  raw image files, stopped VM only
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from abc import abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from ...delta import changed_command, encode_changed, encode_reference, remote_command
from ...ssh import RemoteStream, SSHHost
from ..base import DiskSource, PlatformError
from .zfsdiff import changed_ranges

log = logging.getLogger(__name__)

LOW_PRIORITY = "ionice -c 2 -n 7 nice -n 10 "

_QEMU_DISK = re.compile(r"^(ide|sata|scsi|virtio|efidisk|tpmstate)\d+$")
_LXC_MP = re.compile(r"^mp\d+$")


class UnsupportedStorage(PlatformError):
    pass


@dataclass
class Disk:
    key: str  # scsi0, rootfs, mp0...
    volid: str  # local-zfs:vm-100-disk-0
    options: dict = field(default_factory=dict)

    @property
    def storage(self) -> str:
        return self.volid.split(":", 1)[0]

    @property
    def volname(self) -> str:
        return self.volid.split(":", 1)[1]


def parse_volume(value: str) -> tuple[str, dict]:
    parts = value.split(",")
    volume = parts[0]
    options = {}
    for p in parts[1:]:
        k, _, v = p.partition("=")
        options[k] = v
    if "=" in volume:  # explicit "volume=..." form
        k, _, volume = volume.partition("=")
    return volume, options


_SIZE = re.compile(r"^(\d+(?:\.\d+)?)([KMGT]?)$")
_UNITS = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def parse_size(value: str) -> int | None:
    """Proxmox size option (32G, 528K, 1.5T, 4096) in bytes; None if it is not one."""
    m = _SIZE.match(value.strip())
    return int(float(m.group(1)) * _UNITS[m.group(2)]) if m else None


def guest_volumes(guest_type: str, config: dict) -> list[tuple[Disk, bool]]:
    """Every disk of the guest (CD-ROMs aside), with whether it is to be backed up."""
    volumes = []
    for key in sorted(config):
        value = str(config[key])
        if guest_type == "qemu":
            if not _QEMU_DISK.match(key):
                continue
            volume, opts = parse_volume(value)
            if opts.get("media") == "cdrom" or volume == "none":
                continue
            backed_up = opts.get("backup", "1") != "0"
        elif key == "rootfs":
            volume, opts = parse_volume(value)
            backed_up = True
        elif _LXC_MP.match(key):
            volume, opts = parse_volume(value)
            # Proxmox leaves mount points out of backups by default.
            backed_up = opts.get("backup", "0") == "1"
        else:
            continue
        volumes.append((Disk(key=key, volid=volume, options=opts), backed_up))
    return volumes


def guest_disks(guest_type: str, config: dict) -> list[Disk]:
    """Disks to back up, honouring Proxmox's ``backup`` flag."""
    disks = []
    for disk, backed_up in guest_volumes(guest_type, config):
        if not backed_up:
            continue
        if ":" not in disk.volid:
            raise UnsupportedStorage(
                f"{disk.key}: {disk.volid} is not a Proxmox volume (passthrough disk or bind mount?)"
            )
        disks.append(disk)
    return disks


# Proxmox storage types without snapshots at all; file storages have them in qcow2 only
NO_SNAPSHOT_TYPES = {"lvm", "iscsi", "iscsidirect"}
FILE_TYPES = {"dir", "nfs", "cifs", "glusterfs", "cephfs"}


def snapshot_blockers(guest_type: str, config: dict, storage_of: Callable[[str], dict | None]) -> list[str]:
    """Disks Proxmox cannot snapshot ("snapshot feature is not available"), with why.

    Proxmox checks every disk of the guest, ``backup=0`` ones included: a single one
    makes the whole snapshot fail. ``storage_of`` returns a storage's configuration,
    None when unknown (that disk is then left for Proxmox to judge).
    """
    blockers = []
    for disk, _ in guest_volumes(guest_type, config):
        if ":" not in disk.volid:
            if guest_type == "lxc":
                continue  # bind and device mounts: never part of a container snapshot
            blockers.append(f"{disk.key} ({disk.volid}): passthrough disk, no snapshot possible")
            continue
        cfg = storage_of(disk.storage)
        stype = (cfg or {}).get("type")
        if stype in NO_SNAPSHOT_TYPES:
            blockers.append(f"{disk.key} ({disk.volid}): storage type {stype} has no snapshots")
        elif stype in FILE_TYPES and not disk.volname.endswith(".qcow2"):
            blockers.append(f"{disk.key} ({disk.volid}): on storage type {stype}, only qcow2 files have snapshots")
    return blockers


_ERE_SPECIAL = set("\\.[]()*+?{}|^$")  # special in an extended regular expression


def stop_readers_command(marker: str) -> str:
    """Kill what still reads ``marker`` on the host, then wait (10 s at most) until it is gone.

    Killing ssh here does not stop the command on the host at once: until it notices,
    it keeps the snapshot open, and Proxmox cannot delete a snapshot in use (ZFS:
    "dataset is busy"; the guest then stays locked "snapshot-delete").
    """
    escaped = "".join("\\" + ch if ch in _ERE_SPECIAL else ch for ch in marker[1:])
    # "[x]..." matches the reader's command line, never this command's own
    pattern = shlex.quote(f"[{marker[0]}]{escaped}")
    return (
        f"pkill -KILL -f -- {pattern}; i=0; "
        f"while pgrep -f -- {pattern} >/dev/null && [ $i -lt 50 ]; do sleep 0.2; i=$((i+1)); done; true"
    )


class VolumeSource(DiskSource):
    """One Proxmox volume, read from its snapshot by a command run over SSH."""

    def __init__(
        self,
        ssh: SSHHost,
        storage_cfg: dict,
        disk: Disk,
        snapname: str,
        low_priority: bool = True,
        remote_hash: bool = False,  # python3 available on the node: hash it there
        live: bool = False,  # read the volume itself: nothing writes to it (stopped VM)
    ):
        self.ssh = ssh
        self.cfg = storage_cfg
        self.disk = disk
        self.snapname = snapname
        self.live = live
        self.prefix = LOW_PRIORITY if low_priority else ""
        self.remote_hash = remote_hash
        self._streams: list[RemoteStream] = []
        # The read last opened, for reopener(): "plain", "hashed" (with the reference it
        # compares with), or "" when it cannot start mid-way
        self._read = ""
        self._reference: list[bytes] = []

    @property
    def key(self) -> str:
        return self.disk.key

    @property
    def volume(self) -> str:
        return self.disk.volid

    @abstractmethod
    def stream_command(self) -> str: ...

    def stream_command_from(self, offset: int) -> str | None:
        """``stream_command`` starting ``offset`` bytes in; None when this read cannot."""
        return None

    @abstractmethod
    def reader_marker(self) -> str:
        """Text found only in the command line that reads this disk (to stop it)."""

    # Storage snapshots (snapshot_mode = "storage"): Antigrafa snapshots each volume
    # itself, under the name Proxmox would give it, so that reading is the same.

    @abstractmethod
    def take_snapshot(self) -> None:
        """Snapshot this volume on its storage."""

    @abstractmethod
    def drop_snapshot(self) -> None:
        """Delete the snapshot of this volume; nothing to do when it does not exist."""

    @abstractmethod
    def snapshots(self) -> list[str]:
        """Names of the snapshots of this volume (to find leftovers)."""

    def open_stream(self) -> RemoteStream:
        self._read = "plain"
        return self._opened(self.ssh.stream(self.prefix + self.stream_command()))

    def _opened(self, stream: RemoteStream) -> RemoteStream:
        self._streams.append(stream)
        return stream

    def cleanup(self) -> None:
        # Before anything else touches the snapshot: nothing may read it any more
        if any(getattr(s, "aborted", False) for s in self._streams):
            self.ssh.run(stop_readers_command(self.reader_marker()))
        self._streams.clear()

    def open_hashed_stream(self, reference: list[bytes], chunk_size: int) -> RemoteStream | None:
        if not self.remote_hash:
            return None
        self._read, self._reference = "hashed", reference
        # The same read command, piped through the hashing script, at the same priority
        command = remote_command(self.stream_command(), chunk_size, self.prefix)
        return self._opened(self.ssh.stream(command, input=encode_reference(reference)))

    def reopener(self, chunk_size: int) -> Callable[[int], RemoteStream] | None:
        if not self._read or self.stream_command_from(0) is None:
            return None
        hashed, reference = self._read == "hashed", self._reference

        def reopen(start: int) -> RemoteStream:
            read = self.stream_command_from(start * chunk_size)
            if read is None:  # never: checked above
                raise RuntimeError(f"{self.key}: this read cannot start mid-way")
            if not hashed:
                return self._opened(self.ssh.stream(self.prefix + read))
            # The script numbers chunks from its first one: the reference from there on
            command = remote_command(read, chunk_size, self.prefix)
            return self._opened(self.ssh.stream(command, input=encode_reference(reference[start:])))

        return reopen


def _wait_for_device(dev: str) -> str:
    q = shlex.quote(dev)
    return f"udevadm settle; for i in $(seq 1 50); do [ -e {q} ] && exit 0; sleep 0.2; done; exit 1"


def _if_exists(check: str, action: str) -> str:
    """``action`` only when ``check`` succeeds: deleting twice is not an error."""
    return f"if {check} >/dev/null 2>&1; then {action}; fi"


class _LiveOnly:
    """A storage without snapshots: readable only with the guest stopped."""

    disk: Disk
    cfg: dict

    def _no_snapshot(self) -> UnsupportedStorage:
        return UnsupportedStorage(
            f"{self.disk.key} ({self.disk.volid}): storage type {self.cfg.get('type')} has no snapshots"
        )

    def take_snapshot(self) -> None:
        raise self._no_snapshot()

    def drop_snapshot(self) -> None:
        raise self._no_snapshot()

    def snapshots(self) -> list[str]:
        return []


class _LogicalVolume:
    """An LVM logical volume activated for reading, deactivated afterwards if we did it."""

    ssh: SSHHost
    vg: str
    _activated: bool

    def _activate(self, lv: str, force: bool = False) -> None:
        spec = shlex.quote(f"{self.vg}/{lv}")
        if not force:
            # 5th character of lv_attr: "a" when active (shared LVM: inactive while stopped)
            attr = self.ssh.run(f"lvs --noheadings -o lv_attr {spec}").strip()
            if len(attr) > 4 and attr[4] == "a":
                return
        # Thin snapshots are flagged "activation skip": -K forces activation.
        self.ssh.run(f"lvchange -ay{' -K' if force else ''} {spec}")
        self._activated = True

    def _deactivate(self, lv: str) -> None:
        if self._activated:
            self.ssh.run(f"lvchange -an {shlex.quote(f'{self.vg}/{lv}')}")
            self._activated = False


def direct_read(path: str, offset: int = 0) -> str:
    """dd that bypasses the page cache (O_DIRECT), as vzdump does.

    A buffered read of a whole disk fills the node's memory with data nobody reads
    again (twice on a zvol: page cache and ZFS ARC), and the kernel swaps the guests'
    memory out to make room. Block devices always accept O_DIRECT.

    ``offset``: start that many bytes in (a multiple of 4096, as O_DIRECT requires).
    """
    start = f",skip_bytes skip={int(offset)}" if offset else ""
    return f"dd if={shlex.quote(path)} bs=4M iflag=direct{start} status=none"


def direct_read_if_supported(path: str, offset: int = 0) -> str:
    """Same for a file: O_DIRECT if its filesystem accepts it (tested on one block).

    A single ``sh -c`` command, so that a prefix (``ionice``, ``nice``) applies to it.
    """
    q = shlex.quote(path)
    start = f" iflag=skip_bytes skip={int(offset)}" if offset else ""
    script = (
        f"if dd if={q} bs=4096 count=1 iflag=direct of=/dev/null status=none 2>/dev/null; "
        f"then {direct_read(path, offset)}; else dd if={q} bs=4M{start} status=none; fi"
    )
    return f"sh -c {shlex.quote(script)}"


class _BlockDevice:
    """Read with dd; the size from the device itself."""

    ssh: SSHHost
    device: str

    def size(self) -> int:
        return int(self.ssh.run(f"blockdev --getsize64 {shlex.quote(self.device)}").strip())

    def stream_command(self) -> str:
        return direct_read(self.device)

    def stream_command_from(self, offset: int) -> str | None:
        return direct_read(self.device, offset)

    def reader_marker(self) -> str:
        return self.device


class _ZfsSnapshots:
    """zvol and dataset alike: ``<pool>/<volume>@<snapshot>``."""

    ssh: SSHHost
    dataset: str
    snapname: str

    def take_snapshot(self) -> None:
        self.ssh.run(f"zfs snapshot {shlex.quote(f'{self.dataset}@{self.snapname}')}")

    def drop_snapshot(self) -> None:
        snap = shlex.quote(f"{self.dataset}@{self.snapname}")
        self.ssh.run(_if_exists(f"zfs list -H -o name -t snapshot {snap}", f"zfs destroy {snap}"))

    def snapshots(self) -> list[str]:
        out = self.ssh.run(f"zfs list -H -o name -t snapshot -d 1 {shlex.quote(self.dataset)}")
        return [line.split("@", 1)[1] for line in out.splitlines() if "@" in line]


class ZfsZvolSource(_ZfsSnapshots, VolumeSource):
    """A zvol, read from its snapshot. Incremental when a base snapshot is known.

    ``keeps_base``: the snapshot of this backup stays on the node as the base of the
    next one (``incremental``, storage snapshots). ``base``: (name, guid) of the
    snapshot the previous backup read, kept by it: only the chunks ZFS says changed
    since then are read (see ``zfsdiff``); anything unexpected, and it is a full read.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dataset = f"{self.cfg['pool']}/{self.disk.volname}"
        self.device = f"/dev/zvol/{self.dataset}" + ("" if self.live else f"@{self.snapname}")
        self._restore_snapdev: str | None = None  # command that puts snapdev back
        self.keeps_base = False
        self.base: tuple[str, str] | None = None
        self.base_size: int | None = None  # size of the volume in that backup
        self.snapshot_guid = ""
        self._size: int | None = None

    def _guid(self, snapshot: str) -> str:
        """GUID of ``dataset@snapshot``, "" when it does not exist."""
        snap = shlex.quote(f"{self.dataset}@{snapshot}")
        return self.ssh.run(f"zfs get -Hp -o value guid {snap} 2>/dev/null || true").strip()

    def manifest_extra(self) -> dict:
        if self.keeps_base and self.snapshot_guid:
            return {"source_snapshot": {"name": self.snapname, "guid": self.snapshot_guid}}
        return {}

    def open_hashed_stream(self, reference: list[bytes], chunk_size: int) -> RemoteStream | None:
        ranges = self._changes(reference, chunk_size)
        if ranges is None:
            return super().open_hashed_stream(reference, chunk_size)
        self._read = ""  # only the changed chunks: this read cannot start mid-way yet
        command = changed_command(self.device, chunk_size, self.size(), self.prefix)
        return self._opened(self.ssh.stream(command, input=encode_changed(reference, ranges)))

    def _changes(self, reference: list[bytes], chunk_size: int) -> list[tuple[int, int]] | None:
        """Chunks changed since the base, or None (``read_mode`` says why) for a full read."""
        if self.base is None or self.live:
            return None
        name, guid = self.base
        size = self.size()
        total = (size + chunk_size - 1) // chunk_size
        why = ""
        if not self.remote_hash:
            why = "needs python3 on the node and remote_hash"
        elif self.base_size != size:
            why = f"the volume changed size ({self.base_size} -> {size} bytes)"
        elif len(reference) != total:
            why = "the previous backup has another number of chunks"
        elif self._guid(name) != guid:
            why = f"base snapshot {name} is gone"
        if not why:
            try:
                ranges, records = changed_ranges(
                    self.ssh, f"{self.dataset}@{name}", f"{self.dataset}@{self.snapname}", size, chunk_size, self.prefix
                )
            except Exception as exc:  # whatever went wrong, a full read is always right
                why = f"changes since {name} unknown: {exc}"
            else:
                changed = sum(last - first + 1 for first, last in ranges)
                self.read_mode = f"incremental since {name}: {changed} of {total} chunks changed ({records} ZFS records)"
                return ranges
        self.read_mode = f"full read: {why}"
        return None

    def drop_base(self) -> None:
        """Delete the previous base snapshot, once a newer backup is complete."""
        if self.base is None or self.base[0] == self.snapname:
            return
        name, guid = self.base
        if self._guid(name) == guid:  # never a snapshot that is not the one we kept
            self.ssh.run(f"zfs destroy {shlex.quote(f'{self.dataset}@{name}')}")

    def prepare(self) -> None:
        if self.live:
            self.ssh.run(_wait_for_device(self.device))
            return
        # zvol snapshots only get a device node when snapdev=visible.
        ds = shlex.quote(self.dataset)
        value, source = self.ssh.run(f"zfs get -H -o value,source snapdev {ds}").split()[:2]
        if value != "visible":
            if source == "local":
                self._restore_snapdev = f"zfs set snapdev={shlex.quote(value)} {ds}"
            else:
                self._restore_snapdev = f"zfs inherit snapdev {ds}"
            self.ssh.run(f"zfs set snapdev=visible {ds}")
        self.ssh.run(_wait_for_device(self.device))
        if self.keeps_base:
            self.snapshot_guid = self._guid(self.snapname)

    def size(self) -> int:
        if self._size is None:
            self._size = int(self.ssh.run(f"zfs get -Hp -o value volsize {shlex.quote(self.dataset)}").strip())
        return self._size

    def stream_command(self) -> str:
        return direct_read(self.device)

    def stream_command_from(self, offset: int) -> str | None:
        return direct_read(self.device, offset)

    def reader_marker(self) -> str:
        return self.device

    def cleanup(self) -> None:
        super().cleanup()
        if self._restore_snapdev:
            self.ssh.run(self._restore_snapdev)
            self._restore_snapdev = None


# tar of a container's files. Two runs on the same files give the same archive, byte
# for byte, so that a read can start again in the middle (``stream_command_from``):
# - no atime nor ctime in the pax headers (reading a live tree changes atime);
# - the pax header names without the PID (older tar versions put it in by default);
# - sparse files in format 0.0: 0.1 and 1.0 put the PID in a header name.
TAR_CREATE = (
    "tar --numeric-owner --xattrs --acls --one-file-system --sparse --sparse-version=0.0 "
    "--pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime"
)


class ZfsDatasetSource(_ZfsSnapshots, VolumeSource):
    """Container on a ZFS dataset: no block device, so we archive the snapshot's files."""

    format = "tar"
    resume_check = True  # a new connection makes the archive again: checked

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dataset = f"{self.cfg['pool']}/{self.disk.volname}"
        self.snapdir: str | None = None

    def prepare(self) -> None:
        mountpoint = self.ssh.run(f"zfs get -H -o value mountpoint {shlex.quote(self.dataset)}").strip()
        if not mountpoint.startswith("/"):
            raise UnsupportedStorage(f"{self.dataset} is not mounted (mountpoint={mountpoint})")
        self.snapdir = mountpoint if self.live else f"{mountpoint}/.zfs/snapshot/{self.snapname}"

    def size(self) -> None:
        return None

    def estimated_size(self) -> int | None:
        # Data held by the snapshot, uncompressed: the tar archive is about that size
        # (plus headers). Progress only: a failure must not stop the backup.
        snapshot = shlex.quote(self.dataset if self.live else f"{self.dataset}@{self.snapname}")
        try:
            return int(self.ssh.run(f"zfs get -Hp -o value logicalreferenced {snapshot}").strip())
        except Exception as exc:
            log.debug("%s: no size estimate (%s)", self.disk.key, exc)
            return None

    def stream_command(self) -> str:
        if not self.snapdir:
            raise RuntimeError("prepare() must be called first")
        return f"{TAR_CREATE} -cf - -C {shlex.quote(self.snapdir)} ."

    def stream_command_from(self, offset: int) -> str | None:
        # The same tar again, its first ``offset`` bytes dropped on the node: they are
        # read from the disk again, but not sent. The archive is the same byte for byte
        # (see TAR_CREATE), and the reader checks it on the chunk before (resume_check).
        if not offset:
            return self.stream_command()
        script = f"set -o pipefail; {self.stream_command()} | tail -c +{int(offset) + 1}"
        return f"bash -c {shlex.quote(script)}"

    def reader_marker(self) -> str:
        if self.live:
            return f"-C {self.snapdir} "
        return f".zfs/snapshot/{self.snapname}"


class RbdSource(VolumeSource):
    def _image(self) -> str:
        pool = self.cfg.get("pool", "rbd")
        ns = self.cfg.get("namespace")
        return f"{pool}/{ns}/{self.disk.volname}" if ns else f"{pool}/{self.disk.volname}"

    def _spec(self) -> str:
        return self._image() if self.live else f"{self._image()}@{self.snapname}"

    def take_snapshot(self) -> None:
        self.ssh.run(f"{self._rbd()} snap create {shlex.quote(self._spec())}")

    def drop_snapshot(self) -> None:
        spec = shlex.quote(self._spec())
        self.ssh.run(_if_exists(f"{self._rbd()} info {spec}", f"{self._rbd()} snap rm --no-progress {spec}"))

    def snapshots(self) -> list[str]:
        out = self.ssh.run(f"{self._rbd()} snap ls --format json {shlex.quote(self._image())}")
        return [s["name"] for s in json.loads(out or "[]") if "name" in s]

    def _rbd(self) -> str:
        cmd = "rbd"
        if self.cfg.get("monhost"):  # external Ceph cluster: same options Proxmox uses
            storeid = self.disk.storage
            cmd += f" -m {shlex.quote(self.cfg['monhost'])}"
            cmd += f" --keyring {shlex.quote(f'/etc/pve/priv/ceph/{storeid}.keyring')}"
            cmd += f" --id {shlex.quote(self.cfg.get('username', 'admin'))}"
        return cmd

    def size(self) -> int:
        out = self.ssh.run(f"{self._rbd()} info --format json {shlex.quote(self._spec())}")
        return int(json.loads(out)["size"])

    def stream_command(self) -> str:
        return f"{self._rbd()} export --no-progress {shlex.quote(self._spec())} -"

    def reader_marker(self) -> str:
        return self._spec()


class LvmThinSource(_LogicalVolume, _BlockDevice, VolumeSource):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vg = self.cfg["vgname"]
        # Name Proxmox (LvmThinPlugin) gives to volume snapshots.
        self.snap_lv = f"snap_{self.disk.volname}_{self.snapname}"
        self.lv = self.disk.volname if self.live else self.snap_lv
        self.device = f"/dev/{self.vg}/{self.lv}"
        self._activated = False

    def prepare(self) -> None:
        self._activate(self.lv, force=not self.live)
        self.ssh.run(_wait_for_device(self.device))

    def take_snapshot(self) -> None:
        # A thin snapshot: no size, flagged "activation skip" like those of Proxmox
        origin = shlex.quote(f"{self.vg}/{self.disk.volname}")
        self.ssh.run(f"lvcreate -s -n {shlex.quote(self.snap_lv)} {origin}")

    def drop_snapshot(self) -> None:
        lv = shlex.quote(f"{self.vg}/{self.snap_lv}")
        self.ssh.run(_if_exists(f"lvs {lv}", f"lvremove -f {lv}"))

    def snapshots(self) -> list[str]:
        out = self.ssh.run(f"lvs --noheadings -o lv_name {shlex.quote(self.vg)}")
        prefix = f"snap_{self.disk.volname}_"
        return [name[len(prefix):] for name in out.split() if name.startswith(prefix)]

    def cleanup(self) -> None:
        super().cleanup()
        self._deactivate(self.lv)


class LvmSource(_LiveOnly, _LogicalVolume, _BlockDevice, VolumeSource):
    """LVM thick, often on an iSCSI or FC LUN: the logical volume itself."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vg = self.cfg["vgname"]
        self.device = f"/dev/{self.vg}/{self.disk.volname}"
        self._activated = False

    def prepare(self) -> None:
        self._activate(self.disk.volname)
        self.ssh.run(_wait_for_device(self.device))

    def cleanup(self) -> None:
        super().cleanup()
        self._deactivate(self.disk.volname)


_ISCSI_VOLNAME = re.compile(r"^\d+\.\d+\.\d+\.(\S+)$")


class IscsiSource(_LiveOnly, _BlockDevice, VolumeSource):
    """A whole iSCSI LUN used as a disk (``0.0.8.scsi-<wwid>``): its /dev/disk/by-id device."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        m = _ISCSI_VOLNAME.match(self.disk.volname)
        if not m:
            raise UnsupportedStorage(f"{self.disk.key} ({self.disk.volid}): not an iSCSI LUN name")
        self.device = f"/dev/disk/by-id/{m.group(1)}"

    def prepare(self) -> None:
        self.ssh.run(_wait_for_device(self.device))


class RawFileSource(_LiveOnly, _BlockDevice, VolumeSource):
    """A raw image file on a directory storage (dir, NFS, CIFS): ``<path>/images/<volname>``."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.device = f"{self.cfg['path'].rstrip('/')}/images/{self.disk.volname}"

    def prepare(self) -> None:
        self.ssh.run(f"test -f {shlex.quote(self.device)}")

    def size(self) -> int:
        return int(self.ssh.run(f"stat -L -c %s {shlex.quote(self.device)}").strip())

    def stream_command(self) -> str:
        # Some filesystems refuse O_DIRECT (CIFS without cache=none, tmpfs...)
        return direct_read_if_supported(self.device)

    def stream_command_from(self, offset: int) -> str | None:
        return direct_read_if_supported(self.device, offset)


# Storages without snapshots: read only when nothing writes to them (stopped VM)
LIVE_ONLY_TYPES = {"lvm", "iscsi"}
RAW_FILE_TYPES = {"dir", "nfs", "cifs"}


def make_source(
    ssh: SSHHost,
    storage_cfg: dict,
    disk: Disk,
    snapname: str,
    low_priority: bool = True,
    remote_hash: bool = False,
    live: bool = False,
) -> VolumeSource:
    """Pick the right reader. Called before the snapshot so that we fail early.

    ``live``: the volume itself is read (the VM is stopped and locked).
    """
    stype = storage_cfg.get("type")
    args = (ssh, storage_cfg, disk, snapname, low_priority, remote_hash, live)
    if stype == "zfspool":
        if disk.volname.startswith("subvol-"):
            return ZfsDatasetSource(*args)
        return ZfsZvolSource(*args)
    if stype == "rbd":
        return RbdSource(*args)
    if stype == "lvmthin":
        return LvmThinSource(*args)
    raw_file = stype in RAW_FILE_TYPES and disk.volname.endswith(".raw")
    if (stype in LIVE_ONLY_TYPES or raw_file) and not live:
        raise UnsupportedStorage(
            f"{disk.key} ({disk.volid}): storage type {stype} has no snapshots: back it up with "
            'snapshot_mode = "qemu" (VMs: QEMU keeps the frozen view while the VM runs), '
            "or flag it backup=0"
        )
    if stype == "lvm":
        return LvmSource(*args)
    if stype == "iscsi":
        return IscsiSource(*args)
    if raw_file:
        return RawFileSource(*args)
    if stype in ("dir", "nfs", "cifs", "glusterfs") and disk.volname.endswith(".qcow2"):
        raise UnsupportedStorage(
            f"{disk.key} ({disk.volid}): qcow2 is not supported yet "
            "(reading an internal snapshot of a qcow2 file in use is not safe)"
        )
    raise UnsupportedStorage(f"{disk.key} ({disk.volid}): storage type {stype!r} is not supported")
