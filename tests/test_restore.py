"""Restoring a backup into Proxmox VE: checks, writes, configuration, cleanup.

The node is simulated: the commands are recorded, and what a VM disk receives goes
through the real write script into a local file, compared with the original image.
"""

import io
import logging
import os
import re
import subprocess
import sys
import threading

import pytest

from antigrafa.chunkstore import ChunkStore, digest_of
from antigrafa.index import write_index, write_manifest
from antigrafa.platforms.base import Guest, RestoreDisk, RestoreRequest
from antigrafa.platforms.proxmox import ProxmoxPlatform, ProxmoxSettings
from antigrafa.platforms.proxmox import restore as pr
from antigrafa.platforms.proxmox.restore import (
    RestoreError,
    config_text,
    replaced_volumes,
    restored_config,
    size_option,
)
from antigrafa.progress import Progress
from antigrafa.restore import backup_disks, restore_guest
from antigrafa.targets import Repository, TargetConfig

CHUNK = 1024


def _script():
    namespace = {"__name__": "antigrafa_remote"}
    exec(compile(pr.WRITE_SCRIPT, "remote", "exec"), namespace)  # noqa: S102 - our own script
    return namespace


def _records(*records):
    out = b""
    for kind, offset, data in records:
        length = len(data) if isinstance(data, bytes) else data
        out += kind + offset.to_bytes(8, "big") + length.to_bytes(4, "big") + (data if kind == b"W" else b"")
    return out + b"E"


# --------------------------------------------------------------------------- the write script


@pytest.mark.parametrize("zeroed", [True, False])
def test_write_script_puts_each_chunk_at_its_offset(tmp_path, zeroed):
    target = tmp_path / "disk"
    target.write_bytes(b"\xff" * 3000)  # what the volume held before
    fd = os.open(target, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    try:
        total = _script()["receive"](fd, io.BytesIO(_records((b"W", 0, b"a" * 1000), (b"Z", 1000, 1000),
                                                             (b"W", 2000, b"b" * 1000))), zeroed)
    finally:
        os.close(fd)
    assert total == 3000
    # A zeroed volume already reads as zeros: they are not written
    middle = b"\xff" * 1000 if zeroed else bytes(1000)
    assert target.read_bytes() == b"a" * 1000 + middle + b"b" * 1000


def test_write_script_flushes_to_the_disk_as_it_goes(tmp_path):
    # Otherwise the node's memory runs GiBs ahead of a slow disk: 100 % long before the end
    target = tmp_path / "disk"
    target.write_bytes(bytes(4000))
    script = _script()
    synced = []
    script["sync"] = synced.append
    fd = os.open(target, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    try:
        records = _records((b"W", 0, b"a" * 1000), (b"Z", 1000, 1000), (b"W", 2000, b"b" * 1000),
                           (b"W", 3000, b"c" * 1000))
        assert script["receive"](fd, io.BytesIO(records), False, sync_every=1500) == 4000
        assert len(synced) == 2  # after 2000 and 4000 bytes written
        synced.clear()
        script["receive"](fd, io.BytesIO(records), True, sync_every=1500)  # zeros not written: not counted
        assert len(synced) == 1 and script["SYNC_EVERY"] == 256 << 20
    finally:
        os.close(fd)


def test_write_script_rejects_a_damaged_input(tmp_path):
    target = tmp_path / "disk"
    target.write_bytes(bytes(100))
    fd = os.open(target, os.O_WRONLY | getattr(os, "O_BINARY", 0))
    try:
        with pytest.raises(IOError, match="input ended inside a chunk"):
            _script()["receive"](fd, io.BytesIO(b"W" + (0).to_bytes(8, "big") + (50).to_bytes(4, "big") + b"x"), True)
        with pytest.raises(IOError, match="damaged input"):
            _script()["receive"](fd, io.BytesIO(b"X"), True)
    finally:
        os.close(fd)


def test_write_script_runs_on_a_real_interpreter(tmp_path):
    target = tmp_path / "disk"
    target.write_bytes(bytes(2048))
    res = subprocess.run([sys.executable, "-c", pr.WRITE_SCRIPT, str(target), "1"],
                         input=_records((b"W", 1024, b"z" * 1024)), capture_output=True, timeout=60)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == b"1024"
    assert target.read_bytes() == bytes(1024) + b"z" * 1024


# --------------------------------------------------------------------------- the configuration


VM_CONFIG = {
    "name": "nextcloud",
    "memory": "4096",
    "net0": "virtio=BC:24:11:00:00:01,bridge=vmbr0",
    "smbios1": "uuid=6f1e0d1c-0000-4000-8000-000000000001",
    "vmgenid": "1c2d3e4f-0000-4000-8000-000000000002",
    "scsi0": "ZFS:vm-650-disk-0,iothread=1,size=32G",
    "scsi1": "ISCSI-QNAP:0.0.8.scsi-36e8,backup=0,size=1T",
    "ide2": "local:iso/debian.iso,media=cdrom",
    "efidisk0": "ZFS:vm-650-disk-1,efitype=4m,size=528K",
    "unused0": "ZFS:vm-650-disk-9",
    "digest": "abc",
    "lock": "backup",
    "description": "Nextcloud\nsecond line",
}


def test_new_guest_configuration():
    out = restored_config("qemu", VM_CONFIG, {"scsi0": ("ZFS:vm-999-disk-0", 32 << 30),
                                              "efidisk0": ("ZFS:vm-999-disk-1", 528 << 10)})
    assert out["scsi0"] == "ZFS:vm-999-disk-0,iothread=1,size=32G"
    assert out["efidisk0"] == "ZFS:vm-999-disk-1,efitype=4m,size=528K"
    assert out["ide2"] == "local:iso/debian.iso,media=cdrom"  # CD-ROMs stay
    assert "scsi1" not in out  # not backed up: nothing to attach to a new guest
    assert not {"digest", "lock", "unused0"} & set(out)
    # An exact copy: same MAC address, SMBIOS UUID, vmgenid
    assert out["net0"] == VM_CONFIG["net0"] and out["smbios1"] == VM_CONFIG["smbios1"]
    assert out["vmgenid"] == VM_CONFIG["vmgenid"]


def test_replaced_guest_volumes_are_deleted():
    current = {**VM_CONFIG, "scsi2": "ZFS:vm-650-disk-5,size=8G", "unused0": "ZFS:vm-650-disk-9",
               "scsi3": "ZFS:base-600-disk-0/vm-650-disk-7,size=8G", "unused1": "ZFS:vm-600-disk-4"}
    out = restored_config("qemu", VM_CONFIG, {"scsi0": ("ZFS:vm-650-disk-2", 32 << 30),
                                              "efidisk0": ("ZFS:vm-650-disk-3", 528 << 10)}, current)
    assert out["scsi1"] == current["scsi1"]  # backup=0: the guest keeps it, untouched
    assert "scsi2" not in out  # added after the backup: no longer attached
    assert not any(k.startswith("unused") for k in out)
    delete, leave = replaced_volumes("qemu", out, current, "650")
    assert delete == ["ZFS:vm-650-disk-1", "ZFS:vm-650-disk-0", "ZFS:vm-650-disk-5",
                      "ZFS:base-600-disk-0/vm-650-disk-7", "ZFS:vm-650-disk-9"]
    assert leave == ["ZFS:vm-600-disk-4"]  # another guest's volume: never deleted
    assert "ISCSI-QNAP:0.0.8.scsi-36e8" not in delete + leave  # still attached


def test_container_configuration_and_file():
    config = {"hostname": "dns", "rootfs": "ZFS:subvol-150-disk-1,size=8G", "mp0": "big:vm-150-disk-2,mp=/data",
              "lxc": [["lxc.apparmor.profile", "unconfined"]], "arch": "amd64"}
    out = restored_config("lxc", config, {"rootfs": ("ZFS:subvol-900-disk-0", None)})
    assert out["rootfs"] == "ZFS:subvol-900-disk-0,size=8G" and "mp0" not in out
    text = config_text(out)
    assert text == ("arch: amd64\nhostname: dns\nlxc.apparmor.profile: unconfined\n"
                    "rootfs: ZFS:subvol-900-disk-0,size=8G\n")
    assert config_text({"description": "a\nb", "memory": 512}) == "#a\n#b\nmemory: 512\n"


@pytest.mark.parametrize(("size", "text"), [(32 << 30, "32G"), (528 << 10, "528K"), (1 << 40, "1T"), (1000, "1000")])
def test_size_option(size, text):
    assert size_option(size) == text


# --------------------------------------------------------------------------- the restore on a simulated node


class MemoryDisk(RestoreDisk):
    def __init__(self, key, data, fmt="raw", chunk_size=CHUNK):
        self.key, self.format, self.size, self.chunk_size = key, fmt, len(data), chunk_size
        self.data = data

    def chunks(self):
        zero = bytes(self.chunk_size)
        for i in range(0, len(self.data), self.chunk_size):
            chunk = self.data[i : i + self.chunk_size]
            yield None if chunk == zero else chunk


class Sink:
    def __init__(self, node, command):
        self.node, self.command, self.data, self.closed = node, command, bytearray(), False

    def write(self, data):
        self.data += data

    def close(self, abort=False):
        if self.closed:
            return ""
        self.closed = True
        if abort:
            return ""
        return self.node.received(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.close(abort=True)


class Node:
    """A Proxmox node: volumes are local files, commands are recorded."""

    def __init__(self, tmp_path, status="status: stopped", fail=(), stops_on=()):
        self.tmp, self.status, self.fail = tmp_path, status, tuple(fail)
        self.stops_on = tuple(stops_on)  # commands that stop the guest
        self.commands, self.sinks, self.files, self.timeouts = [], [], {}, {}

    def run(self, cmd, timeout=120):
        from antigrafa.ssh import RemoteCommandError

        self.commands.append(cmd)
        self.timeouts[cmd] = timeout
        if any(f in cmd for f in self.fail):
            raise RemoteCommandError("pve1", cmd, 1, "refused")
        if cmd.startswith(self.stops_on):
            self.status = "status: stopped"
        if cmd.startswith(("qm status", "pct status")):
            return self.status + "\n"
        if cmd.startswith("pvesm alloc"):
            storage, vmid, name, kib = cmd.split()[2:6]
            assert ("--format subvol" in cmd) == name.startswith("subvol-")  # Proxmox refuses it otherwise
            path = self.tmp / name
            path.write_bytes(b"\xee" * (int(kib) * 1024) if "subvol" not in name else b"")
            self.files[f"{storage}:{name}"] = path
            return f"successfully created '{storage}:{name}'\n"
        if cmd.startswith("pvesm path"):
            return str(self.files[cmd.split()[2]]) + "\n"
        if cmd.startswith("pvesm list"):
            return "Volid Format Type Size VMID\nZFS:vm-650-disk-0 raw images 1 650\n"
        return ""

    def sink(self, command):
        sink = Sink(self, command)
        self.sinks.append(sink)
        return sink

    def received(self, sink):
        if "python3 -c" not in sink.command:
            return ""  # tar
        path, zeroed = re.search(r" (\S+) ([01])$", sink.command).groups()
        fd = os.open(path.strip("'"), os.O_WRONLY | getattr(os, "O_BINARY", 0))
        try:
            return str(_script()["receive"](fd, io.BytesIO(bytes(sink.data)), zeroed == "1"))
        finally:
            os.close(fd)


STORAGES = {
    "ZFS": {"type": "zfspool", "pool": "ZFS", "content": "images,rootdir"},
    "thick": {"type": "lvm", "vgname": "data", "content": "images"},
    "iso": {"type": "dir", "path": "/var/lib/vz", "content": "iso,vztmpl"},
    "ceph": {"type": "rbd", "pool": "vms", "content": "images"},
    "far": {"type": "zfspool", "pool": "ZFS", "content": "images", "nodes": "pve2"},
}


class API:
    def __init__(self, guests=(), snapshots=(), current=None):
        self._guests, self._snapshots, self.current = list(guests), list(snapshots), current

    def guests(self):
        return self._guests

    def storage(self, sid):
        return STORAGES[sid]

    def list_snapshots(self, g):
        return [{"name": n} for n in self._snapshots] + [{"name": "current"}]

    def guest_config(self, g):
        return self.current


MANIFEST = {
    "source": "c1",
    "guest": {"id": "650", "kind": "vm", "name": "nextcloud", "host": "pve1"},
    "config": {"name": "nextcloud", "scsi0": "ZFS:vm-650-disk-0,iothread=1,size=4K", "net0": "virtio,bridge=vmbr0"},
    "disks": [{"key": "scsi0", "volume": "ZFS:vm-650-disk-0", "format": "raw", "size": 4096, "index": "scsi0.img.fidx"}],
}


def _platform(node, api):
    settings = ProxmoxSettings(url="https://pve", token_id="a@pve!b")
    return ProxmoxPlatform("c1", settings, api=api, ssh_for_host=lambda host: node)


IMAGE = b"A" * CHUNK + bytes(CHUNK) + os.urandom(CHUNK) + b"B" * CHUNK


def _restore(node, api, request, disks=None, manifest=MANIFEST, cancel=None):
    disks = disks if disks is not None else [MemoryDisk("scsi0", IMAGE)]
    return _platform(node, api).restore_guest(manifest, disks, request, None, cancel)


def test_vm_restored_as_a_new_guest(tmp_path):
    node = Node(tmp_path)
    done = _restore(node, API(), RestoreRequest("901"))
    assert done == "VM 901 restored on pve1 (1 disk(s)); not started"
    steps = [c.split()[0] if not c.startswith("set -C") else "reserve" for c in node.commands]
    assert steps[0] == "reserve" and "pvesm" in steps and steps[-1] == "printf"
    assert "/etc/pve/nodes/pve1/qemu-server/901.conf" in node.commands[0]
    assert any(c == "pvesm alloc ZFS 901 vm-901-disk-0 4" for c in node.commands)  # 4 KiB
    # The volume holds the image, zeros skipped (the new zvol reads as zeros)
    restored = node.files["ZFS:vm-901-disk-0"].read_bytes()
    assert restored[:CHUNK] == IMAGE[:CHUNK] and restored[2 * CHUNK :] == IMAGE[2 * CHUNK :]
    assert restored[CHUNK : 2 * CHUNK] == b"\xee" * CHUNK  # not written: the real zvol reads zeros there
    config = node.commands[-1]
    assert "scsi0: ZFS:vm-901-disk-0,iothread=1,size=4K" in config and "lock" not in config
    assert config.endswith("901.conf.tmp.antigrafa /etc/pve/nodes/pve1/qemu-server/901.conf")


def test_the_last_write_to_the_disk_is_shown(tmp_path, caplog):
    # Everything sent is not everything on the disk yet: the page and the log say so
    caplog.set_level(logging.INFO)
    progress = Progress(label="restore", enabled=False)
    node = Node(tmp_path)
    seen = []
    received = node.received
    node.received = lambda sink: seen.append(progress.snapshot()["status"]) or received(sink)
    _platform(node, API()).restore_guest(MANIFEST, [MemoryDisk("scsi0", IMAGE)], RestoreRequest("901"), progress, None)
    assert seen == ["scsi0: writing to the disk"] and progress.snapshot()["status"] == ""
    assert "VM 901: scsi0 sent, the node writes its last data to the disk" in caplog.text


class ClusterAPI:
    """Nodes and their storages, as Proxmox lists them."""

    def __init__(self, fail_on=""):
        self.fail_on, self.asked = fail_on, []

    def nodes(self):
        return [{"node": "pve2", "status": "online"}, {"node": "pve1", "status": "online"},
                {"node": "pve3", "status": "offline"}]

    def node_storages(self, node, content):
        from antigrafa.platforms.proxmox.api import ProxmoxError

        self.asked.append((node, content))
        if node == self.fail_on:
            raise ProxmoxError("timeout")
        return [
            {"storage": "local-zfs", "type": "zfspool", "avail": 100 << 30, "total": 200 << 30, "active": 1},
            {"storage": "data", "type": "lvmthin", "avail": "5", "total": "10", "active": 1},
            {"storage": "ceph", "type": "rbd", "avail": 1, "total": 2, "active": 1},  # not supported yet
            {"storage": "nfs", "type": "nfs", "active": 0, "shared": 1},  # not mounted
        ]


def test_restore_locations_for_a_vm():
    api = ClusterAPI()
    locations = pr.restore_locations(api, "vm")
    assert [(l["host"], l["online"]) for l in locations] == [("pve1", True), ("pve2", True), ("pve3", False)]
    assert locations[0]["storages"] == [
        {"id": "data", "type": "lvmthin", "avail": 5, "total": 10, "shared": False},
        {"id": "local-zfs", "type": "zfspool", "avail": 100 << 30, "total": 200 << 30, "shared": False},
    ]
    assert locations[2]["storages"] == [] and ("pve3", "images") not in api.asked  # offline: not asked
    assert ("pve1", "images") in api.asked


def test_restore_locations_for_a_container_and_errors():
    api = ClusterAPI(fail_on="pve2")
    locations = pr.restore_locations(api, "ct")
    assert [s["id"] for s in locations[0]["storages"]] == ["local-zfs"]  # container files: ZFS only
    assert ("pve1", "rootdir") in api.asked
    assert locations[1]["storages"] == [] and locations[1]["error"] == "timeout"  # the others still listed
    with pytest.raises(RestoreError, match="unknown guest kind"):
        pr.restore_locations(api, "pod")
    platform = ProxmoxPlatform("c1", ProxmoxSettings(url="https://pve", token_id="a@pve!b"), api=ClusterAPI())
    assert platform.restore_locations("vm")[0]["host"] == "pve1"


def test_thick_lvm_gets_its_zeros_written(tmp_path):
    node = Node(tmp_path)
    _restore(node, API(), RestoreRequest("901", storage="thick"))
    assert node.files["thick:vm-901-disk-0"].read_bytes() == IMAGE  # old data overwritten by zeros


@pytest.mark.parametrize(
    ("api", "request_", "status", "message"),
    [
        (API([Guest("650", "vm", "nc", "pve1", "running")]), RestoreRequest("650"), "status: running",
         "id 650 is taken .*: replace it with its disks \\(overwrite\\), add the disks to it as unused "
         "\\(disk-only\\), or choose another id"),
        (API([Guest("650", "vm", "nc", "pve1")], snapshots=["before"]),
         RestoreRequest("650", overwrite=True, stop="force-stop"), "status: running",
         "has snapshots"),  # running and refused: never stopped
        (API([Guest("650", "ct", "nc", "pve1")]), RestoreRequest("650", overwrite=True), "status: stopped",
         "id 650 is a CT"),
        (API([Guest("650", "vm", "nc", "pve2")]), RestoreRequest("650", overwrite=True), "status: stopped",
         "restore it on pve2"),
        (API([Guest("650", "vm", "nc", "pve1")], snapshots=["before"]), RestoreRequest("650", overwrite=True),
         "status: stopped", "has snapshots \\(before\\)"),
        (API(), RestoreRequest("901", storage="ceph"), "", "storage type rbd .* not supported yet"),
        (API(), RestoreRequest("901", storage="iso"), "", "does not hold images"),
        (API(), RestoreRequest("901", storage="far"), "", "not available on pve1"),
        (API(), RestoreRequest("abc"), "", "not a Proxmox guest id"),
    ],
)
def test_refused_before_anything_changes(tmp_path, api, request_, status, message):
    node = Node(tmp_path, status=status)
    with pytest.raises(RestoreError, match=message):
        _restore(node, api, request_)
    assert all(c.startswith(("qm status", "pct status")) for c in node.commands)  # only looked


def test_replacing_a_stopped_guest_deletes_its_previous_disks(tmp_path):
    current = {"name": "nextcloud", "scsi0": "ZFS:vm-650-disk-0,size=4K", "net0": "virtio,bridge=vmbr0"}
    node = Node(tmp_path)
    done = _restore(node, API([Guest("650", "vm", "nc", "pve1")], current=current), RestoreRequest("650", overwrite=True))
    assert done == "VM 650 restored on pve1 (1 disk(s)); 1 previous volume(s) deleted; not started"
    assert node.commands[1] == "qm set 650 --lock create"
    assert any(c == "pvesm alloc ZFS 650 vm-650-disk-1 4" for c in node.commands)  # disk-0 exists
    config = next(c for c in node.commands if c.startswith("printf"))
    assert "scsi0: ZFS:vm-650-disk-1," in config and "unused" not in config
    # Deleted only once the restored guest is complete
    assert node.commands[-1] == "pvesm free ZFS:vm-650-disk-0"
    assert node.commands.index("pvesm free ZFS:vm-650-disk-0") > node.commands.index(config)


def test_a_previous_disk_that_cannot_be_deleted_is_reported(tmp_path):
    current = {"scsi0": "ZFS:vm-650-disk-0,size=4K"}
    node = Node(tmp_path, fail=("pvesm free ZFS:vm-650-disk-0",))
    done = _restore(node, API([Guest("650", "vm", "nc", "pve1")], current=current), RestoreRequest("650", overwrite=True))
    assert "0 previous volume(s) deleted; NOT deleted (remove by hand): ZFS:vm-650-disk-0" in done
    assert "ZFS:vm-650-disk-1" in node.files  # the restore itself stands


def test_disk_only_adds_the_disks_as_unused(tmp_path):
    current = {"scsi0": "ZFS:vm-650-disk-0,size=4K"}
    node = Node(tmp_path, status="status: running")  # running: it does not matter
    api = API([Guest("650", "vm", "nc", "pve1")], current=current, snapshots=["before"])  # neither do snapshots
    done = _restore(node, api, RestoreRequest("650", disk_only=True))
    assert done == "VM 650 on pve1: 1 disk(s) added as unused (scsi0 -> ZFS:vm-650-disk-1)"
    assert node.files["ZFS:vm-650-disk-1"].read_bytes()[: CHUNK] == IMAGE[:CHUNK]
    assert node.commands[-1] == "qm rescan --vmid 650"  # Proxmox attaches it as unusedN
    # Nothing else changes: no lock, no configuration written, no stop, nothing deleted
    assert not any(c.startswith(("qm set", "printf", "set -C", "pvesm free", "qm status", "qm shutdown"))
                   for c in node.commands)


def test_disk_only_failure_frees_the_new_volumes(tmp_path):
    node = Node(tmp_path, fail=("qm rescan",))
    api = API([Guest("650", "vm", "nc", "pve1")], current={"scsi0": "ZFS:vm-650-disk-0,size=4K"})
    with pytest.raises(Exception, match="refused"):
        _restore(node, api, RestoreRequest("650", disk_only=True))
    assert "pvesm free ZFS:vm-650-disk-1" in node.commands


def test_disk_only_needs_an_existing_guest(tmp_path):
    node = Node(tmp_path)
    with pytest.raises(RestoreError, match="disk-only: there is no guest 901"):
        _restore(node, API(), RestoreRequest("901", disk_only=True))
    assert node.commands == []


def test_start_once_restored(tmp_path):
    node = Node(tmp_path)
    done = _restore(node, API(), RestoreRequest("901", start=True))
    assert done == "VM 901 restored on pve1 (1 disk(s)); started"
    assert node.commands[-1] == "qm start 901"


def test_start_failure_keeps_the_restored_guest(tmp_path):
    node = Node(tmp_path, fail=("qm start",))
    with pytest.raises(RestoreError, match="VM 901 restored, but it did not start"):
        _restore(node, API(), RestoreRequest("901", start=True))
    assert "ZFS:vm-901-disk-0" in node.files
    assert not any(c.startswith(("pvesm free", "rm -f")) for c in node.commands)  # nothing undone


RUNNING = API([Guest("650", "vm", "nc", "pve1")], current={"scsi0": "ZFS:vm-650-disk-0,size=4K"})


def test_overwrite_stops_a_running_guest_only_once_the_disks_are_written(tmp_path):
    node = Node(tmp_path, status="status: running", stops_on=("qm shutdown",))
    done = _restore(node, RUNNING, RestoreRequest("650", overwrite=True))  # no stop option needed
    assert done.startswith("VM 650 restored on pve1 (1 disk(s)), stopped for the restore;")
    c = node.commands
    shutdown = c.index("qm shutdown 650 --timeout 300")
    assert node.timeouts["qm shutdown 650 --timeout 300"] > 300  # SSH waits longer than Proxmox
    # Locked while its disks are copied (it keeps running), unlocked to be shut down,
    # locked again until the restored configuration replaces it
    assert c.index("qm set 650 --lock create") < c.index("pvesm alloc ZFS 650 vm-650-disk-1 4")
    assert c.index("pvesm path ZFS:vm-650-disk-1") < c.index("qm unlock 650") < shutdown
    relock = len(c) - 1 - c[::-1].index("qm set 650 --lock create")
    config = next(i for i, x in enumerate(c) if x.startswith("printf"))
    assert shutdown < relock < config
    assert not any(x.startswith("qm stop") for x in c)


def test_overwrite_stops_hard_when_the_shutdown_fails(tmp_path):
    node = Node(tmp_path, status="status: running", fail=("qm shutdown",), stops_on=("qm stop",))
    done = _restore(node, RUNNING, RestoreRequest("650", overwrite=True, start=True))
    assert "stopped for the restore" in done and done.endswith("; started")
    c = node.commands
    assert c.index("pvesm path ZFS:vm-650-disk-1") < c.index("qm shutdown 650 --timeout 300") < c.index("qm stop 650")


def test_overwrite_keeps_the_disks_as_unused_when_the_guest_cannot_be_stopped(tmp_path):
    node = Node(tmp_path, status="status: running", fail=("qm shutdown", "qm stop"))
    with pytest.raises(RestoreError, match="could not be stopped within 2 minutes: not replaced; the restored "
                                           "disks are attached to it as unused \\(ZFS:vm-650-disk-1\\)"):
        _restore(node, RUNNING, RestoreRequest("650", overwrite=True))
    c = node.commands
    assert c[-1] == "qm rescan --vmid 650"  # the copied disk attached as unusedN
    assert not any(x.startswith(("printf", "pvesm free")) for x in c)  # configuration unchanged, disk kept
    assert c.count("qm unlock 650") == 1  # unlocked for the shutdown, not locked again


def test_copied_disks_removed_when_they_cannot_be_attached(tmp_path):
    node = Node(tmp_path, status="status: running", fail=("qm shutdown", "qm stop", "qm rescan"))
    with pytest.raises(RestoreError, match="could not be stopped within 2 minutes: the guest was not changed"):
        _restore(node, RUNNING, RestoreRequest("650", overwrite=True))
    assert node.commands[-1] == "pvesm free ZFS:vm-650-disk-1"


def test_stop_shuts_down_before_the_restore(tmp_path):
    node = Node(tmp_path, status="status: running", stops_on=("qm shutdown",))
    done = _restore(node, RUNNING, RestoreRequest("650", overwrite=True, stop="stop"))
    assert done.startswith("VM 650 restored on pve1 (1 disk(s)), stopped for the restore;")
    c = node.commands
    assert c[:3] == ["qm status 650", "qm shutdown 650 --timeout 300", "qm status 650"]  # first of all
    assert node.timeouts["qm shutdown 650 --timeout 300"] > 300  # SSH waits longer than Proxmox
    assert c.index("qm set 650 --lock create") > 1
    assert "qm unlock 650" not in c and c.count("qm shutdown 650 --timeout 300") == 1  # not stopped again


def test_stop_fails_when_the_guest_does_not_shut_down(tmp_path):
    node = Node(tmp_path, status="status: running", fail=("qm shutdown",))
    with pytest.raises(RestoreError, match="did not shut down within 5 minutes: the guest was not changed"):
        _restore(node, RUNNING, RestoreRequest("650", overwrite=True, stop="stop"))
    # Nothing done at all: no hard stop, no lock, no volume
    assert not any(c.startswith(("qm stop", "qm set", "pvesm")) for c in node.commands)


def test_force_stop_stops_hard_before_the_restore(tmp_path):
    node = Node(tmp_path, status="status: running", fail=("qm shutdown",), stops_on=("qm stop",))
    done = _restore(node, RUNNING, RestoreRequest("650", overwrite=True, stop="force-stop", start=True))
    assert "stopped for the restore" in done and done.endswith("; started")
    assert node.timeouts["qm stop 650"] == 120
    c = node.commands
    assert c.index("qm shutdown 650 --timeout 300") < c.index("qm stop 650") < c.index("qm set 650 --lock create")


def test_force_stop_fails_when_the_guest_cannot_be_stopped(tmp_path):
    node = Node(tmp_path, status="status: running", fail=("qm shutdown", "qm stop"))
    with pytest.raises(RestoreError, match="could not be stopped within 2 minutes: the guest was not changed"):
        _restore(node, RUNNING, RestoreRequest("650", overwrite=True, stop="force-stop"))
    assert not any(c.startswith(("qm set", "pvesm", "printf")) for c in node.commands)


def test_container_shut_down_with_pct(tmp_path):
    api = API([Guest("150", "ct", "dns", "pve1")], current={"rootfs": "ZFS:subvol-150-disk-0,size=8G"})
    manifest = {
        "source": "c1",
        "guest": {"id": "150", "kind": "ct", "name": "dns", "host": "pve1"},
        "config": {"hostname": "dns", "rootfs": "ZFS:subvol-150-disk-0,size=8G"},
        "disks": [{"key": "rootfs", "volume": "ZFS:subvol-150-disk-0", "format": "tar", "size": CHUNK}],
    }
    node = Node(tmp_path, status="status: running", stops_on=("pct shutdown",))
    _restore(node, api, RestoreRequest("150", overwrite=True, stop="stop"),
             disks=[MemoryDisk("rootfs", b"x" * CHUNK, fmt="tar")], manifest=manifest)
    assert "pct shutdown 150 --timeout 300" in node.commands


def test_a_refused_restore_never_stops_the_guest(tmp_path):
    node = Node(tmp_path, status="status: running", stops_on=("qm shutdown",))
    with pytest.raises(RestoreError, match="storage type rbd"):
        _restore(node, RUNNING, RestoreRequest("650", overwrite=True, storage="ceph", stop="force-stop"))
    assert node.commands == ["qm status 650"]


def test_failure_after_the_stop_says_the_guest_stays_stopped(tmp_path, caplog):
    node = Node(tmp_path, status="status: running", stops_on=("qm shutdown",), fail=("mv -f",))
    with pytest.raises(Exception, match="refused"):
        _restore(node, RUNNING, RestoreRequest("650", overwrite=True))
    assert "was stopped for the restore and stays stopped" in caplog.text
    assert node.commands.count("qm unlock 650") == 2  # for the shutdown, then by the undo
    assert node.commands[-1] == "pvesm free ZFS:vm-650-disk-1"


def test_interrupted_restore_unlocks_the_guest_first(tmp_path):
    """Ctrl+C while a disk is written: unlocked, even if the undo is interrupted again."""

    class Interrupted(Node):
        def sink(self, command):
            raise KeyboardInterrupt

        def run(self, cmd, timeout=120):
            out = super().run(cmd, timeout)
            if cmd.startswith("pvesm free"):
                raise KeyboardInterrupt  # a second Ctrl+C during the undo
            return out

    node = Interrupted(tmp_path, status="status: running")
    with pytest.raises(KeyboardInterrupt):
        _restore(node, RUNNING, RestoreRequest("650", overwrite=True))
    c = node.commands
    assert c.index("qm unlock 650") < c.index("pvesm free ZFS:vm-650-disk-1")  # unlocked first
    assert not any(x.startswith("qm shutdown") for x in c)  # never stopped: the disks were not written


def test_cancel_while_copying_unlocks_a_replaced_guest(tmp_path):
    cancel = threading.Event()

    class Cancelled(Node):
        def received(self, sink):
            cancel.set()
            return super().received(sink)

    node = Cancelled(tmp_path, status="status: running")
    with pytest.raises(RestoreError, match="cancelled"):
        _restore(node, RUNNING, RestoreRequest("650", overwrite=True), cancel=cancel)
    assert "qm unlock 650" in node.commands
    assert not any(x.startswith(("qm shutdown", "qm stop", "printf")) for x in node.commands)


@pytest.mark.parametrize(
    ("request_", "message"),
    [
        (RestoreRequest("650", stop="stop"), "only applies when replacing a guest"),
        (RestoreRequest("650", overwrite=True, stop="now"), "is not one of stop, force-stop"),
        (RestoreRequest("650", overwrite=True, disk_only=True), "overwrite or disk-only: not both"),
        (RestoreRequest("650", disk_only=True, start=True), "start does not apply to disk-only"),
        (RestoreRequest("650", disk_only=True, stop="stop"), "only applies when replacing a guest"),
    ],
)
def test_stop_options_checked(tmp_path, request_, message):
    node = Node(tmp_path)
    with pytest.raises(RestoreError, match=message):
        _restore(node, API(), request_)
    assert node.commands == []


def test_copy_under_another_id_does_not_start_at_boot(tmp_path):
    manifest = {**MANIFEST, "config": {**MANIFEST["config"], "onboot": 1}}
    node = Node(tmp_path)
    done = _restore(node, API(), RestoreRequest("901"), manifest=manifest)
    assert "start at boot turned off (a copy of VM 650" in done
    config = node.commands[-1]
    assert "onboot: 0" in config and "onboot: 1" not in config
    assert "net0: virtio,bridge=vmbr0" in config  # otherwise an exact copy


def test_same_id_keeps_start_at_boot(tmp_path):
    manifest = {**MANIFEST, "config": {**MANIFEST["config"], "onboot": 1}}
    node = Node(tmp_path)
    done = _restore(node, API(), RestoreRequest("650"), manifest=manifest)
    assert "start at boot" not in done
    assert "onboot: 1" in node.commands[-1]


def test_failure_frees_the_new_volumes_and_the_reservation(tmp_path):
    node = Node(tmp_path, fail=("mv -f",))  # the configuration cannot be written
    with pytest.raises(Exception, match="refused"):
        _restore(node, API(), RestoreRequest("901"))
    assert "pvesm free ZFS:vm-901-disk-0" in node.commands
    assert "rm -f /etc/pve/nodes/pve1/qemu-server/901.conf" in node.commands


def test_failure_unlocks_the_guest_being_replaced(tmp_path):
    node = Node(tmp_path, fail=("pvesm alloc",))
    current = {"scsi0": "ZFS:vm-650-disk-0,size=4K"}
    with pytest.raises(Exception, match="refused"):
        _restore(node, API([Guest("650", "vm", "nc", "pve1")], current=current), RestoreRequest("650", overwrite=True))
    assert node.commands[-1] == "qm unlock 650"
    assert not any(c.startswith("rm -f") for c in node.commands)  # its configuration stays


def test_cancel_stops_and_cleans_up(tmp_path):
    cancel = threading.Event()

    class Cancelled(MemoryDisk):
        def chunks(self):
            for n, chunk in enumerate(super().chunks()):
                if n == 1:
                    cancel.set()  # from the web page, while the disk is written
                yield chunk

    node = Node(tmp_path)
    with pytest.raises(RestoreError, match="cancelled"):
        _restore(node, API(), RestoreRequest("901"), disks=[Cancelled("scsi0", IMAGE)], cancel=cancel)
    assert node.sinks[0].closed  # the write on the node stopped
    assert "pvesm free ZFS:vm-901-disk-0" in node.commands
    assert node.commands[-1] == "rm -f /etc/pve/nodes/pve1/qemu-server/901.conf"


def test_cancel_before_any_disk_removes_the_reservation(tmp_path):
    node = Node(tmp_path)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RestoreError, match="cancelled"):
        _restore(node, API(), RestoreRequest("901"), cancel=cancel)
    assert not any(c.startswith("pvesm alloc") for c in node.commands)
    assert node.commands[-1] == "rm -f /etc/pve/nodes/pve1/qemu-server/901.conf"


def test_container_files_extracted_into_a_new_dataset(tmp_path):
    manifest = {
        "guest": {"id": "150", "kind": "ct", "name": "dns", "host": "pve1"},
        "config": {"hostname": "dns", "rootfs": "ZFS:subvol-150-disk-1,size=8G"},
        "disks": [{"key": "rootfs", "volume": "ZFS:subvol-150-disk-1", "format": "tar", "size": 3000}],
    }
    archive = os.urandom(3000)
    node = Node(tmp_path)
    done = _restore(node, API(), RestoreRequest("902"), disks=[MemoryDisk("rootfs", archive, fmt="tar")],
                    manifest=manifest)
    assert done.startswith("CT 902 restored on pve1")
    assert any(c == f"pvesm alloc ZFS 902 subvol-902-disk-0 {8 << 20} --format subvol" for c in node.commands)
    [sink] = node.sinks
    assert sink.command.startswith("tar --numeric-owner --xattrs") and bytes(sink.data) == archive
    assert "/etc/pve/nodes/pve1/lxc/902.conf" in node.commands[-1]
    assert "rootfs: ZFS:subvol-902-disk-0,size=8G" in node.commands[-1]


# --------------------------------------------------------------------------- from the repository


def test_restore_reads_the_backup_from_the_repository(tmp_path):
    repo = Repository(TargetConfig(name="t", path=str(tmp_path / "repo")))
    (tmp_path / "repo").mkdir()
    repo.open()
    backup = tmp_path / "repo" / "c1" / "vm" / "650" / "2026-11-21T10-00-00Z"
    backup.mkdir(parents=True)
    chunks = [IMAGE[i : i + CHUNK] for i in range(0, len(IMAGE), CHUNK)]
    digests = [repo.store.insert(c) if c != bytes(CHUNK) else digest_of(c) for c in chunks]  # zeros never stored
    write_index(backup / "scsi0.img.fidx", len(IMAGE), CHUNK, digests)
    write_manifest(backup, {**MANIFEST, "disks": [{**MANIFEST["disks"][0], "size": len(IMAGE)}]})

    [disk] = backup_disks(repo, backup, {"disks": [{"key": "scsi0", "index": "scsi0.img.fidx"}]}, jobs=2)
    assert b"".join(c if c is not None else bytes(CHUNK) for c in disk.chunks()) == IMAGE

    node = Node(tmp_path)
    done = restore_guest(_platform(node, API()), repo, "c1/vm/650/2026-11-21T10-00-00Z", RestoreRequest("901"))
    assert done.startswith("VM 901 restored")
    with pytest.raises(Exception, match="comes from source c1"):
        other = _platform(node, API())
        other.name = "c2"
        restore_guest(other, repo, "c1/vm/650/2026-11-21T10-00-00Z", RestoreRequest("902"))


def test_guest_id_asked_on_a_terminal(monkeypatch):
    from antigrafa import cli

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True, raising=False)
    assert cli._ask_guest_id("650", ask=lambda prompt: "") == "650"  # Enter: the original id
    assert cli._ask_guest_id("650", ask=lambda prompt: " 901 ") == "901"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    assert cli._ask_guest_id("650", ask=lambda prompt: pytest.fail("never asked without a terminal")) == "650"


def test_restore_guest_command(tmp_path, monkeypatch, capsys):
    from antigrafa import cli
    from antigrafa.progress import Progress

    repo = Repository(TargetConfig(name="t", path=str(tmp_path / "repo")))
    (tmp_path / "repo").mkdir()
    repo.open()
    backup = tmp_path / "repo" / "c1" / "vm" / "650" / "2026-11-21T10-00-00Z"
    backup.mkdir(parents=True)
    write_index(backup / "scsi0.img.fidx", len(IMAGE), CHUNK,
                [repo.store.insert(IMAGE[i : i + CHUNK]) for i in range(0, len(IMAGE), CHUNK)])
    write_manifest(backup, {**MANIFEST, "disks": [{**MANIFEST["disks"][0], "size": len(IMAGE)}]})
    node, sources = Node(tmp_path), []

    def platform(cfg, source):
        sources.append(source)
        return _platform(node, API())

    monkeypatch.setattr(cli, "_repo", lambda cfg, target: repo)
    monkeypatch.setattr(cli, "_platform", platform)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)
    args = cli.build_parser().parse_args(["restore-guest", "c1/vm/650/2026-11-21T10-00-00Z", "--host", "pve1",
                                          "--storage", "thick", "--jobs", "2"])
    args.progress_bar = Progress(enabled=False)
    assert cli.cmd_restore_guest(None, args) == 0
    assert sources == ["c1"]  # the source the backup came from
    # No terminal, no --id: the original id; it is free on this simulated cluster
    assert "VM 650 restored on pve1" in capsys.readouterr().out
    assert node.files["thick:vm-650-disk-1"].read_bytes() == IMAGE  # disk-0 already listed


def test_restore_guest_stop_options(capsys):
    from antigrafa import cli

    parse = cli.build_parser().parse_args
    assert parse(["restore-guest", "c1/vm/650/x", "--overwrite", "--stop"]).stop == "stop"
    assert parse(["restore-guest", "c1/vm/650/x", "--overwrite", "--force-stop"]).stop == "force-stop"
    assert parse(["restore-guest", "c1/vm/650/x"]).stop is None
    with pytest.raises(SystemExit):
        parse(["restore-guest", "c1/vm/650/x", "--overwrite", "--stop", "--force-stop"])  # one or the other
    with pytest.raises(SystemExit) as exc:
        cli.main(["restore-guest", "c1/vm/650/x", "--force-stop"])
    assert exc.value.code == 2
    assert "--force-stop only applies with --overwrite" in capsys.readouterr().err


def test_restore_guest_existing_guest_options(capsys):
    from antigrafa import cli

    parse = cli.build_parser().parse_args
    args = parse(["restore-guest", "c1/vm/650/x", "--disk-only"])
    assert args.disk_only and not args.overwrite and not args.start
    assert parse(["restore-guest", "c1/vm/650/x", "--overwrite", "--force-stop", "--start"]).start
    with pytest.raises(SystemExit):
        parse(["restore-guest", "c1/vm/650/x", "--overwrite", "--disk-only"])  # one or the other
    with pytest.raises(SystemExit):
        cli.main(["restore-guest", "c1/vm/650/x", "--disk-only", "--stop"])  # disk-only stops nothing
    assert "--stop only applies with --overwrite" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        cli.main(["restore-guest", "c1/vm/650/x", "--disk-only", "--start"])
    assert exc.value.code == 2
    assert "--start does not apply with --disk-only" in capsys.readouterr().err
