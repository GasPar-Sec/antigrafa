"""Reading disks from a Proxmox snapshot, depending on the storage type.

The snapshot is created through the Proxmox API (consistent across all disks,
filesystems frozen through the qemu-guest-agent). It is instantaneous: the VM
resumes right away. Each ``VolumeSource`` then knows how to read the snapshot of
one volume remotely, over SSH, at whatever pace the destination allows.

Supported storages:
    zfspool  zvol (VM, raw disk) or ``subvol-*`` dataset (CT, tar archive)
    rbd      Ceph RBD (``rbd export`` of the snapshot)
    lvmthin  LVM-thin (``snap_<vol>_<snap>`` snapshot activated for reading)
"""

from __future__ import annotations

import json
import logging
import re
import shlex
from abc import abstractmethod
from dataclasses import dataclass, field

from ...delta import encode_reference, remote_command
from ...ssh import RemoteStream, SSHHost
from ..base import DiskSource, PlatformError

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


def guest_disks(guest_type: str, config: dict) -> list[Disk]:
    """Disks to back up, honouring Proxmox's ``backup`` flag."""
    disks = []
    for key in sorted(config):
        value = str(config[key])
        if guest_type == "qemu":
            if not _QEMU_DISK.match(key):
                continue
            volume, opts = parse_volume(value)
            if opts.get("media") == "cdrom" or volume == "none":
                continue
            if opts.get("backup", "1") == "0":
                continue
        else:
            if key == "rootfs":
                volume, opts = parse_volume(value)
            elif _LXC_MP.match(key):
                volume, opts = parse_volume(value)
                # Proxmox leaves mount points out of backups by default.
                if opts.get("backup", "0") != "1":
                    continue
            else:
                continue
        if ":" not in volume:
            raise UnsupportedStorage(f"{key}: {volume} is not a Proxmox volume (passthrough disk or bind mount?)")
        disks.append(Disk(key=key, volid=volume, options=opts))
    return disks


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
    ):
        self.ssh = ssh
        self.cfg = storage_cfg
        self.disk = disk
        self.snapname = snapname
        self.prefix = LOW_PRIORITY if low_priority else ""
        self.remote_hash = remote_hash

    @property
    def key(self) -> str:
        return self.disk.key

    @property
    def volume(self) -> str:
        return self.disk.volid

    @abstractmethod
    def stream_command(self) -> str: ...

    def open_stream(self) -> RemoteStream:
        return self.ssh.stream(self.prefix + self.stream_command())

    def open_hashed_stream(self, reference: list[bytes], chunk_size: int) -> RemoteStream | None:
        if not self.remote_hash:
            return None
        # The same read command, piped through the hashing script, at the same priority
        command = remote_command(self.stream_command(), chunk_size, self.prefix)
        return self.ssh.stream(command, input=encode_reference(reference))


def _wait_for_device(dev: str) -> str:
    q = shlex.quote(dev)
    return f"udevadm settle; for i in $(seq 1 50); do [ -e {q} ] && exit 0; sleep 0.2; done; exit 1"


class ZfsZvolSource(VolumeSource):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dataset = f"{self.cfg['pool']}/{self.disk.volname}"
        self.device = f"/dev/zvol/{self.dataset}@{self.snapname}"
        self._restore_snapdev: str | None = None  # command that puts snapdev back

    def prepare(self) -> None:
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

    def size(self) -> int:
        return int(self.ssh.run(f"zfs get -Hp -o value volsize {shlex.quote(self.dataset)}").strip())

    def stream_command(self) -> str:
        return f"dd if={shlex.quote(self.device)} bs=4M status=none"

    def cleanup(self) -> None:
        if self._restore_snapdev:
            self.ssh.run(self._restore_snapdev)
            self._restore_snapdev = None


class ZfsDatasetSource(VolumeSource):
    """Container on a ZFS dataset: no block device, so we archive the snapshot's files."""

    format = "tar"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.dataset = f"{self.cfg['pool']}/{self.disk.volname}"
        self.snapdir: str | None = None

    def prepare(self) -> None:
        mountpoint = self.ssh.run(f"zfs get -H -o value mountpoint {shlex.quote(self.dataset)}").strip()
        if not mountpoint.startswith("/"):
            raise UnsupportedStorage(f"{self.dataset} is not mounted (mountpoint={mountpoint})")
        self.snapdir = f"{mountpoint}/.zfs/snapshot/{self.snapname}"

    def size(self) -> None:
        return None

    def estimated_size(self) -> int | None:
        # Data held by the snapshot, uncompressed: the tar archive is about that size
        # (plus headers). Progress only: a failure must not stop the backup.
        snapshot = shlex.quote(f"{self.dataset}@{self.snapname}")
        try:
            return int(self.ssh.run(f"zfs get -Hp -o value logicalreferenced {snapshot}").strip())
        except Exception as exc:
            log.debug("%s: no size estimate (%s)", self.disk.key, exc)
            return None

    def stream_command(self) -> str:
        if not self.snapdir:
            raise RuntimeError("prepare() must be called first")
        return (
            "tar --numeric-owner --xattrs --acls --one-file-system --sparse "
            f"-cf - -C {shlex.quote(self.snapdir)} ."
        )


class RbdSource(VolumeSource):
    def _spec(self) -> str:
        pool = self.cfg.get("pool", "rbd")
        ns = self.cfg.get("namespace")
        image = f"{pool}/{ns}/{self.disk.volname}" if ns else f"{pool}/{self.disk.volname}"
        return f"{image}@{self.snapname}"

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


class LvmThinSource(VolumeSource):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.vg = self.cfg["vgname"]
        # Name Proxmox (LvmThinPlugin) gives to volume snapshots.
        self.snap_lv = f"snap_{self.disk.volname}_{self.snapname}"
        self.device = f"/dev/{self.vg}/{self.snap_lv}"
        self._activated = False

    def prepare(self) -> None:
        # Thin snapshots are flagged "activation skip": -K forces activation.
        self.ssh.run(f"lvchange -ay -K {shlex.quote(f'{self.vg}/{self.snap_lv}')}")
        self._activated = True
        self.ssh.run(_wait_for_device(self.device))

    def size(self) -> int:
        return int(self.ssh.run(f"blockdev --getsize64 {shlex.quote(self.device)}").strip())

    def stream_command(self) -> str:
        return f"dd if={shlex.quote(self.device)} bs=4M status=none"

    def cleanup(self) -> None:
        if self._activated:
            self.ssh.run(f"lvchange -an {shlex.quote(f'{self.vg}/{self.snap_lv}')}")
            self._activated = False


def make_source(
    ssh: SSHHost,
    storage_cfg: dict,
    disk: Disk,
    snapname: str,
    low_priority: bool = True,
    remote_hash: bool = False,
) -> VolumeSource:
    """Pick the right reader. Called before the snapshot so that we fail early."""
    stype = storage_cfg.get("type")
    args = (ssh, storage_cfg, disk, snapname, low_priority, remote_hash)
    if stype == "zfspool":
        if disk.volname.startswith("subvol-"):
            return ZfsDatasetSource(*args)
        return ZfsZvolSource(*args)
    if stype == "rbd":
        return RbdSource(*args)
    if stype == "lvmthin":
        return LvmThinSource(*args)
    if stype in ("dir", "nfs", "cifs", "glusterfs") and disk.volname.endswith(".qcow2"):
        raise UnsupportedStorage(
            f"{disk.key} ({disk.volid}): qcow2 is not supported yet "
            "(reading an internal snapshot of a qcow2 file in use is not safe)"
        )
    raise UnsupportedStorage(f"{disk.key} ({disk.volid}): storage type {stype!r} is not supported")
