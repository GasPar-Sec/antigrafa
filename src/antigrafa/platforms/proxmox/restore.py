"""Restoring a backup into Proxmox VE: the guest recreated, its disks written back.

Everything is done over SSH as root (the API token only reads): ``pvesm`` allocates
the volumes, the disks are written on the node, the configuration file is written
last. In order:

1. checks, before anything changes: the target id (free, or ``overwrite`` of a stopped
   guest of the same kind, on that node, without snapshots), each target storage
   (supported, allowed for this content, available on the node). A running guest being
   replaced is shut down here only when asked (``stop``: clean shutdown, or fail;
   ``force-stop``: then a hard stop); otherwise it keeps running while its disks are
   copied, and is shut down (stopped hard if needed) just before step 4;
2. the id reserved: a configuration with ``lock: create`` (new guest), or the guest
   locked (``overwrite``);
3. each disk allocated on its storage and written: a VM disk by a small script that
   writes the chunks at their offsets (chunks of zeros are not even sent to a storage
   that gives zeroed volumes), a container file system by ``tar``;
4. the configuration written with the new volumes, which also lifts the lock. When a
   guest is replaced (``overwrite``), its previous volumes are deleted only then, once
   the restored guest is complete; its disks that were not backed up stay attached;
5. the guest started, when asked (``start``).

``disk-only`` changes nothing in an existing guest but adds the disks of the backup to
it as ``unusedN`` (``qm/pct rescan``): steps 1 and 3 only, the guest may be running.

On failure, the volumes created are freed and the reservation removed. The configuration
is restored as it was backed up (same MAC addresses, SMBIOS UUID and vmgenid).
"""

from __future__ import annotations

import logging
import re
import shlex
import threading

from ...progress import Progress
from ..base import PlatformError, RestoreDisk, RestoreRequest
from .api import PVE_TYPES
from .capture import _LXC_MP, _QEMU_DISK, parse_size, parse_volume

log = logging.getLogger(__name__)

# argv: path, "1" when the volume reads as zeros already. Standard input: records
# b"W" + offset (8) + length (4) + data, b"Z" + offset (8) + length (4) (zeros), b"E".
# Flushed to the disk every SYNC_EVERY bytes: otherwise the node's memory takes GiBs
# ahead of a slow disk, the progress reaches 100 % long before the disk has them, and
# the end (or a cancel) waits for all of it to be written.
WRITE_SCRIPT = r'''
import os, struct, sys

SYNC_EVERY = 256 << 20
sync = getattr(os, "fdatasync", os.fsync)


def read_full(stream, size):
    parts = []
    while size:
        buf = stream.read(size)
        if not buf:
            break
        parts.append(buf)
        size -= len(buf)
    return b"".join(parts)


def write_at(fd, data, offset):
    os.lseek(fd, offset, os.SEEK_SET)
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def receive(fd, inp, zeroed, sync_every=SYNC_EVERY):
    written = 0
    unsynced = 0
    while True:
        kind = read_full(inp, 1)
        if kind == b"E":
            return written
        head = read_full(inp, 12)
        if kind not in (b"W", b"Z") or len(head) != 12:
            raise IOError("damaged input after %d bytes" % written)
        offset, length = struct.unpack(">QI", head)
        if kind == b"W":
            data = read_full(inp, length)
            if len(data) != length:
                raise IOError("input ended inside a chunk at %d" % offset)
            write_at(fd, data, offset)
            unsynced += length
        elif not zeroed:
            write_at(fd, bytes(length), offset)
            unsynced += length
        written += length
        if unsynced >= sync_every:  # the sender waits for the disk, not for the memory
            sync(fd)
            unsynced = 0


if __name__ == "__main__":
    path, zeroed = sys.argv[1], sys.argv[2] == "1"
    fd = os.open(path, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    try:
        total = receive(fd, sys.stdin.buffer, zeroed)
        sync(fd)
    finally:
        os.close(fd)
    sys.stdout.write("%d\n" % total)
'''

# Storages a disk can be written back to: storage type -> (volume names need ".raw",
# a new volume reads as zeros)
VM_STORAGES = {
    "zfspool": (False, True),
    "lvmthin": (False, True),
    "lvm": (False, False),  # thick: a new volume holds what was there before
    "dir": (True, True),
    "nfs": (True, True),
    "cifs": (True, True),
}
CT_STORAGES = {"zfspool"}  # a container's files: extracted into a ZFS dataset

STOP_MODES = ("", "stop", "force-stop")
SHUTDOWN_TIMEOUT = 300  # seconds for a clean shutdown of the guest replaced
STOP_TIMEOUT = 120  # seconds for the hard stop that follows (force-stop)

# Not restored: lock and snapshot bookkeeping, and the checksum of the old file
DROPPED = {"digest", "lock", "parent", "snaptime", "vmstate", "pending"}
_CREATED = re.compile(r"'([^']+)'")
_UNUSED = re.compile(r"^unused(\d+)$")


class RestoreError(PlatformError):
    pass


def _bytes(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def restore_locations(api, kind: str) -> list[dict]:
    """Each node, and its storages a guest of ``kind`` can be restored to (see
    ``Platform.restore_locations``): enabled on the node, holding that content (VM disks:
    images; container files: rootdir), of a supported type. An offline node lists none."""
    if kind not in PVE_TYPES:
        raise RestoreError(f"unknown guest kind {kind!r}")
    content, supported = ("images", VM_STORAGES) if kind == "vm" else ("rootdir", CT_STORAGES)
    locations = []
    for entry in sorted(api.nodes(), key=lambda n: str(n.get("node", ""))):
        node = str(entry.get("node", ""))
        online = entry.get("status") == "online"
        storages, error = [], ""
        if online:
            try:
                found = api.node_storages(node, content)
            except PlatformError as exc:
                found, error = [], str(exc)
            storages = [
                {"id": str(s["storage"]), "type": s.get("type", ""), "avail": _bytes(s.get("avail")),
                 "total": _bytes(s.get("total")), "shared": bool(s.get("shared"))}
                for s in sorted(found, key=lambda s: str(s.get("storage", "")))
                if s.get("storage") and s.get("type") in supported and s.get("active", 1)
            ]
        locations.append({"host": node, "online": online, "storages": storages, "error": error})
    return locations


def size_option(size: int) -> str:
    """Bytes as Proxmox writes a disk size: 32G, 528K..."""
    for unit, factor in (("T", 1 << 40), ("G", 1 << 30), ("M", 1 << 20), ("K", 1 << 10)):
        if size % factor == 0:
            return f"{size // factor}{unit}"
    return str(size)


def _is_disk(ptype: str, key: str) -> bool:
    if ptype == "qemu":
        return bool(_QEMU_DISK.match(key))
    return key == "rootfs" or bool(_LXC_MP.match(key))


def _join(volume: str, options: dict) -> str:
    return ",".join([volume] + [f"{k}={v}" if v != "" else k for k, v in options.items()])


def _volume_of(value) -> str:
    volume, opts = parse_volume(str(value))
    return "" if opts.get("media") == "cdrom" or volume == "none" or ":" not in volume else volume


def restored_config(ptype: str, backed_up: dict, new_volumes: dict[str, tuple[str, int | None]],
                    current: dict | None = None) -> dict:
    """The configuration to write: the backed-up one, with the new volumes.

    ``new_volumes``: disk key -> (volume id, size in bytes or None to keep the option).
    ``current``: the configuration of the guest being replaced. Its disks that were not
    backed up (``backup=0``) stay attached: the backup has nothing to put in their place.
    Its other volumes are no longer referenced (see ``replaced_volumes``).
    """
    out: dict = {}
    for key, value in backed_up.items():
        if key in DROPPED or _UNUSED.match(key):
            continue
        if key in new_volumes:
            volid, size = new_volumes[key]
            _, opts = parse_volume(str(value))
            if size is not None:
                opts["size"] = size_option(size)
            out[key] = _join(volid, opts)
        elif _is_disk(ptype, key) and _volume_of(value):
            # A disk that was not backed up (backup=0): the guest being replaced keeps it
            if current is not None and key in current:
                out[key] = current[key]
        else:
            out[key] = value
    return out


def replaced_volumes(ptype: str, config: dict, current: dict, gid: str) -> tuple[list[str], list[str]]:
    """Volumes of the guest replaced that ``config`` no longer uses: (to delete, to leave).

    Its disks and ``unusedN`` volumes, except those still attached. Only the volumes of
    this guest (``vm-<id>-...``, ``subvol-<id>-...``) are deleted: a volume of another
    guest (a linked clone's base) is left as it is.
    """
    kept = {_volume_of(v) for k, v in config.items() if _is_disk(ptype, k) or _UNUSED.match(k)}
    owned = re.compile(rf"^(?:vm|subvol|base)-{re.escape(gid)}-")
    delete, leave = [], []
    for key in sorted(current):
        if not (_is_disk(ptype, key) or _UNUSED.match(key)):
            continue
        volume = _volume_of(current[key])
        if not volume or volume in kept or volume in delete or volume in leave:
            continue
        name = volume.split(":", 1)[1].split("/")[-1]
        (delete if owned.match(name) else leave).append(volume)
    return delete, leave


def config_text(config: dict) -> str:
    """The configuration file of the guest (``/etc/pve/.../<id>.conf``)."""
    lines = []
    description = config.get("description")
    if description:
        lines += [f"#{line}" for line in str(description).splitlines()]
    for key in sorted(config):
        value = config[key]
        if key == "description":
            continue
        if key == "lxc" and isinstance(value, list):  # raw LXC keys: [[name, value], ...]
            lines += [f"{name}: {raw}" for name, raw in value]
            continue
        lines.append(f"{key}: {value}")
    return "\n".join(lines) + "\n"


def _q(value: str) -> str:
    return shlex.quote(value)


class ProxmoxRestore:
    """One restore: ``run`` checks, writes, and cleans up on failure."""

    def __init__(self, platform, manifest: dict, disks: list[RestoreDisk], request: RestoreRequest,
                 progress: Progress | None = None, cancel: threading.Event | None = None) -> None:
        self.platform = platform
        self.api = platform.api
        self.manifest = manifest
        self.disks = disks
        self.request = request
        self.progress = progress if progress is not None else Progress(enabled=False)
        self.cancel = cancel if cancel is not None else threading.Event()
        guest = manifest.get("guest") or {}
        if guest.get("kind") not in PVE_TYPES:
            raise RestoreError("this backup does not name the kind of its guest")
        self.kind = guest["kind"]
        self.ptype = PVE_TYPES[self.kind]
        self.tool = "qm" if self.kind == "vm" else "pct"
        self.gid = str(request.guest_id)
        if not self.gid.isdigit() or int(self.gid) < 100:
            raise RestoreError(f"{self.gid!r} is not a Proxmox guest id (100 or more)")
        self.node = request.host or guest.get("host") or ""
        if not self.node:
            raise RestoreError("no host to restore to: give one")
        if request.stop not in STOP_MODES:
            raise RestoreError(f"stop: {request.stop!r} is not one of {', '.join(m for m in STOP_MODES if m)}")
        if request.overwrite and request.disk_only:
            raise RestoreError("overwrite or disk-only: not both")
        if request.stop and not request.overwrite:
            raise RestoreError(f"{request.stop} only applies when replacing a guest (overwrite)")
        if request.start and request.disk_only:
            raise RestoreError("start does not apply to disk-only (the guest is not changed)")
        self.ssh = platform.ssh_for_host(self.node)
        self.config_path = f"/etc/pve/nodes/{self.node}/{'qemu-server' if self.kind == 'vm' else 'lxc'}/{self.gid}.conf"
        self.label = f"{self.kind.upper()} {self.gid}"
        # Filled by run(): to undo on failure
        self._created: list[str] = []
        self._reserved = False
        self._locked = False
        self._existing = None  # the guest replaced (overwrite)
        self._running = False  # the guest replaced is running (stop asked)
        self._stopped_it = False  # this restore stopped it

    # ----------------------------------------------------------------- checks

    def _check(self) -> tuple[dict | None, list[tuple[RestoreDisk, str, dict]]]:
        """Current configuration of the guest replaced (None: new), and each disk's storage."""
        existing = next((g for g in self.api.guests() if g.id == self.gid), None)
        current = None
        if existing is None and self.request.disk_only:
            raise RestoreError(f"disk-only: there is no guest {self.gid} to add the disks to")
        if existing is not None:
            what = f"{existing.label} on {existing.host}"
            if not (self.request.overwrite or self.request.disk_only):
                raise RestoreError(f"id {self.gid} is taken ({what}): replace it with its disks (overwrite), "
                                   "add the disks to it as unused (disk-only), or choose another id")
            if existing.kind != self.kind:
                raise RestoreError(f"id {self.gid} is a {existing.kind.upper()} ({what}): this backup is a {self.kind.upper()}")
            if existing.host != self.node:
                raise RestoreError(f"{what}: restore it on {existing.host}")
        if existing is not None and self.request.overwrite:
            status = self.ssh.run(f"{self.tool} status {_q(self.gid)}")
            # Running: stopped before the restore (stop, force-stop), or else just before it is replaced
            self._running = "status: stopped" not in status
            snapshots = [s.get("name") for s in self.api.list_snapshots(existing) if s.get("name") != "current"]
            if snapshots:
                raise RestoreError(f"{what} has snapshots ({', '.join(snapshots)}): delete them first, "
                                   "they would no longer match its disks")
            current = self.api.guest_config(existing)
            self._existing = existing
        targets = []
        for disk in self.disks:
            entry = next((d for d in self.manifest.get("disks", []) if d.get("key") == disk.key), {})
            storage = self.request.storage or str(entry.get("volume", "")).split(":", 1)[0]
            if not storage:
                raise RestoreError(f"{disk.key}: no storage to restore it to")
            cfg = self.api.storage(storage)
            stype = cfg.get("type")
            content = str(cfg.get("content", ""))
            if self.kind == "vm" or disk.format == "raw":
                if stype not in VM_STORAGES:
                    raise RestoreError(f"{disk.key}: restoring to storage type {stype} ({storage}) is not supported yet")
                needed = "images"
            else:
                if stype not in CT_STORAGES:
                    raise RestoreError(f"{disk.key}: restoring a container's files to storage type {stype} "
                                       f"({storage}) is not supported yet (ZFS only)")
                needed = "rootdir"
            if needed not in content.split(","):
                raise RestoreError(f"{disk.key}: storage {storage} does not hold {needed} (content: {content})")
            nodes = cfg.get("nodes")
            if nodes and self.node not in str(nodes).split(","):
                raise RestoreError(f"{disk.key}: storage {storage} is not available on {self.node}")
            targets.append((disk, storage, cfg))
        return current, targets

    # ----------------------------------------------------------------- steps

    def run(self) -> str:
        current, targets = self._check()
        if self.request.disk_only:
            return self._run_disk_only(targets)
        if self._running and self.request.stop:
            # Asked: stopped before anything else (nothing created yet, nothing to undo)
            self._stop_guest(self.request.stop, "before the restore")
            self._running = False
        try:
            self._reserve(current is not None)
            new_volumes = self._restore_disks(targets)
            if self._running:
                # Only now, with every disk written: stopped just before it is replaced
                current = self._stop_for_overwrite()
            config = restored_config(self.ptype, self.manifest.get("config") or {}, new_volumes, current)
            boot_off = self._copy_boot_off(config)
            self._write_config(config)
        except BaseException:
            self._undo()
            raise
        done = f"{self.label} restored on {self.node} ({len(new_volumes)} disk(s))"
        if self._stopped_it:
            done += ", stopped for the restore"
        if current is not None:
            done += self._delete_replaced(config, current)
        if boot_off:
            done += (f"; start at boot turned off (a copy of {self.kind.upper()} {boot_off}, "
                     "with the same addresses)")
        done += self._start() if self.request.start else "; not started"
        log.info("%s", done)
        return done

    def _restore_disks(self, targets) -> dict[str, tuple[str, int | None]]:
        new_volumes: dict[str, tuple[str, int | None]] = {}
        for disk, storage, cfg in targets:
            self._raise_if_cancelled()
            new_volumes[disk.key] = self._restore_disk(disk, storage, cfg)
        self._raise_if_cancelled()
        return new_volumes

    def _run_disk_only(self, targets) -> str:
        """Add the disks to the existing guest as unusedN; nothing else changes."""
        try:
            new_volumes = self._restore_disks(targets)
            # Proxmox attaches the volumes of the guest that no configuration uses, as unusedN
            self.ssh.run(f"{self.tool} rescan --vmid {_q(self.gid)}")
        except BaseException:
            self._undo()
            raise
        self._created.clear()
        added = ", ".join(f"{key} -> {volid}" for key, (volid, _) in new_volumes.items())
        done = f"{self.label} on {self.node}: {len(new_volumes)} disk(s) added as unused ({added})"
        log.info("%s", done)
        return done

    def _delete_replaced(self, config: dict, current: dict) -> str:
        """Delete the volumes of the guest replaced; their outcome, for the final message."""
        delete, leave = replaced_volumes(self.ptype, config, current, self.gid)
        failed = []
        for volid in delete:
            try:
                self.ssh.run(f"pvesm free {_q(volid)}", timeout=600)
                log.info("%s: previous volume %s deleted", self.label, volid)
            except Exception as exc:
                log.error("%s: previous volume %s not deleted, remove it by hand: %s", self.label, volid, exc)
                failed.append(volid)
        text = f"; {len(delete) - len(failed)} previous volume(s) deleted" if delete else ""
        if failed:
            text += f"; NOT deleted (remove by hand): {', '.join(failed)}"
        if leave:
            log.warning("%s: %s belong to another guest: left as they are", self.label, ", ".join(leave))
            text += f"; left as they are (another guest's): {', '.join(leave)}"
        return text

    def _start(self) -> str:
        """Start the restored guest: the restore is done, a failure here undoes nothing."""
        try:
            self.ssh.run(f"{self.tool} start {_q(self.gid)}", timeout=STOP_TIMEOUT)
        except Exception as exc:
            raise RestoreError(f"{self.label} restored, but it did not start: {exc}") from exc
        log.info("%s started", self.label)
        return "; started"

    def _is_stopped(self) -> bool:
        return "status: stopped" in self.ssh.run(f"{self.tool} status {_q(self.gid)}")

    def _stop_for_overwrite(self) -> dict:
        """Stop the running guest being replaced, once its disks are copied: a shutdown,
        then a hard stop if needed. Returns its configuration as it is then.

        Its lock is lifted for the shutdown (Proxmox refuses to stop a locked guest), then
        set again until the restored configuration replaces it.
        """
        self.ssh.run(f"{self.tool} unlock {_q(self.gid)}")
        self._locked = False
        try:
            self._stop_guest("force-stop", "to replace it")
        except RestoreError as exc:
            raise self._keep_as_unused(exc) from exc
        self._raise_if_cancelled()
        self._reserve(True)
        return self.api.guest_config(self._existing)  # changed while the disks were written?

    def _keep_as_unused(self, stop_error: RestoreError) -> RestoreError:
        """The guest could not be stopped to be replaced: its copied disks stay, as unused.

        Nothing else changes in it. If they cannot be attached, they are removed (undo).
        """
        volumes = list(self._created)
        try:
            self.ssh.run(f"{self.tool} rescan --vmid {_q(self.gid)}")
        except Exception as exc:
            log.error("%s: the restored disks could not be attached as unused, they are removed: %s",
                      self.label, exc)
            return stop_error
        self._created.clear()  # attached to the guest now: kept by the undo
        return RestoreError(f"{self.label} could not be stopped within {STOP_TIMEOUT // 60} minutes: not replaced; "
                            f"the restored disks are attached to it as unused ({', '.join(volumes)})")

    def _stop_guest(self, mode: str, when: str) -> None:
        """Stop the guest being replaced: a clean shutdown, then (force-stop) a hard stop."""
        log.warning("%s is running: shutting it down %s (up to %d s)", self.label, when, SHUTDOWN_TIMEOUT)
        try:
            self.ssh.run(f"{self.tool} shutdown {_q(self.gid)} --timeout {SHUTDOWN_TIMEOUT}",
                         timeout=SHUTDOWN_TIMEOUT + 60)
        except Exception as exc:  # timed out, or the guest refused: its status decides
            log.warning("%s: shutdown: %s", self.label, exc)
        if self._is_stopped():
            self._stopped_it = True
            log.info("%s shut down", self.label)
            return
        if mode != "force-stop":
            raise RestoreError(f"{self.label} did not shut down within {SHUTDOWN_TIMEOUT // 60} minutes: "
                               "the guest was not changed (force-stop stops it hard)")
        log.warning("%s did not shut down: stopping it hard (up to %d s)", self.label, STOP_TIMEOUT)
        try:
            self.ssh.run(f"{self.tool} stop {_q(self.gid)}", timeout=STOP_TIMEOUT)
        except Exception as exc:
            log.warning("%s: stop: %s", self.label, exc)
        if not self._is_stopped():
            raise RestoreError(f"{self.label} could not be stopped within {STOP_TIMEOUT // 60} minutes: "
                               "the guest was not changed")
        self._stopped_it = True
        log.info("%s stopped", self.label)

    def _copy_boot_off(self, config: dict) -> str:
        """A copy under another id must not start at boot next to its original.

        It keeps the same MAC and IP addresses. Returns the original id when ``onboot`` was
        turned off, else "".
        """
        original = str((self.manifest.get("guest") or {}).get("id", ""))
        if not original or original == self.gid:
            return ""
        if str(config.get("onboot", "0")).strip() in ("", "0"):
            return ""
        config["onboot"] = 0
        return original

    def _raise_if_cancelled(self) -> None:
        if self.cancel.is_set():
            raise RestoreError("cancelled")

    def _reserve(self, existing: bool) -> None:
        if existing:
            self.ssh.run(f"{self.tool} set {_q(self.gid)} --lock create")
            self._locked = True
            return
        # noclobber: fails if the file exists; the cluster file system refuses an id used elsewhere
        self.ssh.run(f"set -C; printf 'lock: create\\n' > {_q(self.config_path)}")
        self._reserved = True

    def _volume_name(self, disk: RestoreDisk, storage: str, raw_suffix: bool) -> str:
        listing = self.ssh.run(f"pvesm list {_q(storage)} --vmid {_q(self.gid)}")
        used = {line.split()[0].split(":", 1)[-1].split("/")[-1] for line in listing.splitlines()[1:] if line.strip()}
        prefix = "subvol" if disk.format == "tar" else "vm"
        n = 0
        while True:
            name = f"{prefix}-{self.gid}-disk-{n}" + (".raw" if raw_suffix else "")
            if name not in used and name not in {c.split(":", 1)[-1].split("/")[-1] for c in self._created}:
                return name
            n += 1

    def _allocate(self, storage: str, name: str, size: int, fmt: str = "") -> str:
        """A new volume of ``size`` bytes (0: no size, a dataset without quota).

        ``fmt``: "raw" for an image file, "subvol" for a container's ZFS dataset.
        """
        fmt = f" --format {fmt}" if fmt else ""
        kib = (size + 1023) // 1024  # pvesm alloc counts in KiB
        out = self.ssh.run(f"pvesm alloc {_q(storage)} {_q(self.gid)} {_q(name)} {kib}{fmt}")
        m = _CREATED.search(out)
        if not m:
            raise RestoreError(f"pvesm alloc on {storage}: unexpected answer {out.strip()!r}")
        volid = m.group(1)
        self._created.append(volid)
        return volid

    def _restore_disk(self, disk: RestoreDisk, storage: str, cfg: dict) -> tuple[str, int | None]:
        if disk.format == "tar":
            option = parse_volume(str((self.manifest.get("config") or {}).get(disk.key, "")))[1].get("size", "")
            size = parse_size(option) if option else None
            volid = self._allocate(storage, self._volume_name(disk, storage, False), size or 0, "subvol")
            path = self.ssh.run(f"pvesm path {_q(volid)}").strip()
            log.info("%s: %s -> %s (files into %s)", self.label, disk.key, volid, path)
            self._send_tar(disk, path)
            return volid, size
        raw_suffix, zeroed = VM_STORAGES[cfg["type"]]
        volid = self._allocate(storage, self._volume_name(disk, storage, raw_suffix), disk.size,
                               "raw" if raw_suffix else "")
        path = self.ssh.run(f"pvesm path {_q(volid)}").strip()
        if not raw_suffix:  # a block device: wait for it
            self.ssh.run(f"udevadm settle; for i in $(seq 1 50); do [ -e {_q(path)} ] && exit 0; sleep 0.2; done; exit 1")
        log.info("%s: %s -> %s (%s)", self.label, disk.key, volid, path)
        self._send_raw(disk, path, zeroed)
        return volid, disk.size

    def _send_raw(self, disk: RestoreDisk, path: str, zeroed: bool) -> None:
        command = f"python3 -c {_q(WRITE_SCRIPT)} {_q(path)} {'1' if zeroed else '0'}"
        zeros: bytes | None = None
        offset = 0
        with self.progress.task(disk.size, name=disk.key) as task, self.ssh.sink(command) as sink:
            for data in disk.chunks():
                self._raise_if_cancelled()
                if data is not None and len(data) == disk.chunk_size:
                    zeros = zeros if zeros is not None else bytes(disk.chunk_size)
                    if data == zeros:
                        data = None
                length = disk.chunk_size if data is None else len(data)
                length = min(length, disk.size - offset)
                if data is None:
                    sink.write(b"Z" + offset.to_bytes(8, "big") + length.to_bytes(4, "big"))
                else:
                    sink.write(b"W" + offset.to_bytes(8, "big") + length.to_bytes(4, "big"))
                    sink.write(data)
                offset += length
                task.advance(length)
            sink.write(b"E")
            log.info("%s: %s sent, the node writes its last data to the disk", self.label, disk.key)
            self.progress.set_status(f"{disk.key}: writing to the disk")
            try:
                written = int(sink.close().strip() or 0)
            finally:
                self.progress.set_status("")
        if offset != disk.size or written != disk.size:
            raise RestoreError(f"{disk.key}: {written} bytes written instead of {disk.size}")

    def _send_tar(self, disk: RestoreDisk, path: str) -> None:
        command = (f"tar --numeric-owner --xattrs --xattrs-include='*' --acls -xpf - -C {_q(path)}")
        with self.progress.task(disk.size, name=disk.key) as task, self.ssh.sink(command) as sink:
            for data in disk.chunks():
                self._raise_if_cancelled()
                if data is None:
                    data = bytes(disk.chunk_size)
                sink.write(data)
                task.advance(len(data))
            sink.close()

    def _write_config(self, config: dict) -> None:
        tmp = f"{self.config_path}.tmp.antigrafa"
        self.ssh.run(f"printf '%s' {_q(config_text(config))} > {_q(tmp)} && mv -f {_q(tmp)} {_q(self.config_path)}")
        self._reserved = self._locked = False  # the new configuration has no lock
        self._created.clear()  # the volumes belong to the guest now

    def _undo(self) -> None:
        """Remove what this restore created; the guest replaced keeps its disks.

        A guest replaced is unlocked first (quick, and what matters most), then the
        volumes are freed, then the reservation of a new id is removed. Each step is tried even when the previous one failed or was interrupted
        (a second Ctrl+C): the interruption that started the undo is raised afterwards.
        """
        if self._locked:
            try:
                self.ssh.run(f"{self.tool} unlock {_q(self.gid)}")
                self._locked = False
                log.info("%s unlocked", self.label)
            except BaseException as exc:
                log.error("%s: still locked, run on %s: %s unlock %s (%s)", self.label, self.node, self.tool,
                          self.gid, exc or type(exc).__name__)
        for volid in reversed(self._created):
            try:
                self.ssh.run(f"pvesm free {_q(volid)}")
            except BaseException as exc:
                log.error("%s: volume %s not freed, remove it by hand: %s", self.label, volid,
                          exc or type(exc).__name__)
        self._created.clear()
        if self._reserved:  # last: the id stays taken until its new volumes are gone
            try:
                self.ssh.run(f"rm -f {_q(self.config_path)}")
                self._reserved = False
            except BaseException as exc:
                log.error("%s: reservation %s not removed, remove it by hand: %s", self.label, self.config_path,
                          exc or type(exc).__name__)
        if self._stopped_it:
            log.warning("%s was stopped for the restore and stays stopped: start it again if needed", self.label)
        if self._stopped_it:
            log.warning("%s was stopped for the restore and stays stopped: start it again if needed", self.label)
