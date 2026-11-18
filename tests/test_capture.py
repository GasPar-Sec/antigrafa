import pytest

from antigrafa.capture import (
    Disk,
    LvmThinSource,
    RbdSource,
    UnsupportedStorage,
    ZfsDatasetSource,
    ZfsZvolSource,
    guest_disks,
    make_source,
)


class FakeSSH:
    def __init__(self, replies=None):
        self.replies = replies or {}
        self.commands = []

    def run(self, cmd, timeout=120):
        self.commands.append(cmd)
        for prefix, reply in self.replies.items():
            if cmd.startswith(prefix):
                return reply
        return ""


QEMU_CONFIG = {
    "name": "web",
    "scsi0": "local-zfs:vm-100-disk-0,iothread=1,size=32G",
    "scsi1": "ceph:vm-100-disk-1,backup=0,size=100G",
    "ide2": "local:iso/debian.iso,media=cdrom,size=600M",
    "efidisk0": "local-lvm:vm-100-disk-2,efitype=4m,size=4M",
    "unused0": "local-zfs:vm-100-disk-9",
    "net0": "virtio=AA:BB,bridge=vmbr0",
}


def test_qemu_disks_respect_backup_flag_and_skip_cdrom():
    disks = guest_disks("qemu", QEMU_CONFIG)
    assert [(d.key, d.volid) for d in disks] == [
        ("efidisk0", "local-lvm:vm-100-disk-2"),
        ("scsi0", "local-zfs:vm-100-disk-0"),
    ]


def test_lxc_mountpoints_only_with_backup_flag():
    config = {
        "rootfs": "local-zfs:subvol-101-disk-0,size=8G",
        "mp0": "local-lvm:vm-101-disk-1,mp=/data,backup=1",
        "mp1": "local-lvm:vm-101-disk-2,mp=/cache",
    }
    assert [d.key for d in guest_disks("lxc", config)] == ["mp0", "rootfs"]


def test_passthrough_disk_is_rejected():
    with pytest.raises(UnsupportedStorage):
        guest_disks("qemu", {"scsi0": "/dev/disk/by-id/ata-XYZ,size=1T"})


def test_source_selection():
    ssh = FakeSSH()
    zfs = {"type": "zfspool", "pool": "rpool/data"}
    assert isinstance(make_source(ssh, zfs, Disk("scsi0", "local-zfs:vm-100-disk-0"), "s"), ZfsZvolSource)
    assert isinstance(make_source(ssh, zfs, Disk("rootfs", "local-zfs:subvol-101-disk-0"), "s"), ZfsDatasetSource)
    assert isinstance(make_source(ssh, {"type": "rbd", "pool": "vms"}, Disk("scsi0", "ceph:vm-1-disk-0"), "s"), RbdSource)
    lvm = {"type": "lvmthin", "vgname": "pve", "thinpool": "data"}
    assert isinstance(make_source(ssh, lvm, Disk("scsi0", "local-lvm:vm-1-disk-0"), "s"), LvmThinSource)
    with pytest.raises(UnsupportedStorage, match="qcow2"):
        make_source(ssh, {"type": "nfs", "path": "/mnt/pve/nfs"}, Disk("scsi0", "nfs:100/vm-100-disk-0.qcow2"), "s")
    with pytest.raises(UnsupportedStorage):
        make_source(ssh, {"type": "lvm", "vgname": "vg"}, Disk("scsi0", "lvm:vm-1-disk-0"), "s")


def test_zvol_sets_and_restores_snapdev():
    ssh = FakeSSH({"zfs get -H -o value,source snapdev": "hidden\tdefault\n", "zfs get -Hp": "34359738368\n"})
    src = ZfsZvolSource(ssh, {"type": "zfspool", "pool": "rpool/data"}, Disk("scsi0", "local-zfs:vm-100-disk-0"), "antigrafa_1")
    src.prepare()
    assert "zfs set snapdev=visible rpool/data/vm-100-disk-0" in ssh.commands
    assert src.size() == 34359738368
    assert src.stream_command() == "dd if=/dev/zvol/rpool/data/vm-100-disk-0@antigrafa_1 bs=4M status=none"
    src.cleanup()
    assert ssh.commands[-1] == "zfs inherit snapdev rpool/data/vm-100-disk-0"


def test_zvol_leaves_snapdev_alone_when_already_visible():
    ssh = FakeSSH({"zfs get -H -o value,source snapdev": "visible\tlocal\n"})
    src = ZfsZvolSource(ssh, {"type": "zfspool", "pool": "tank"}, Disk("scsi0", "z:vm-1-disk-0"), "s")
    src.prepare()
    src.cleanup()
    assert not any("zfs set" in c or "zfs inherit" in c for c in ssh.commands)


def test_zfs_dataset_tars_snapshot_directory():
    ssh = FakeSSH({"zfs get -H -o value mountpoint": "/rpool/data/subvol-101-disk-0\n"})
    src = ZfsDatasetSource(ssh, {"type": "zfspool", "pool": "rpool/data"}, Disk("rootfs", "z:subvol-101-disk-0"), "snap1")
    src.prepare()
    assert src.format == "tar" and src.size() is None
    assert "-C /rpool/data/subvol-101-disk-0/.zfs/snapshot/snap1 ." in src.stream_command()


def test_rbd_external_cluster_options():
    ssh = FakeSSH({"rbd": '{"size": 1073741824}'})
    cfg = {"type": "rbd", "pool": "vms", "monhost": "10.0.0.1 10.0.0.2", "username": "pve"}
    src = RbdSource(ssh, cfg, Disk("scsi0", "ceph-ext:vm-100-disk-0"), "s1")
    assert src.size() == 1073741824
    cmd = src.stream_command()
    assert cmd.startswith("rbd -m '10.0.0.1 10.0.0.2' --keyring /etc/pve/priv/ceph/ceph-ext.keyring --id pve")
    assert cmd.endswith("export --no-progress vms/vm-100-disk-0@s1 -")


def test_lvmthin_activates_proxmox_snapshot_volume():
    ssh = FakeSSH({"blockdev": "4194304\n"})
    src = LvmThinSource(ssh, {"type": "lvmthin", "vgname": "pve"}, Disk("scsi0", "local-lvm:vm-100-disk-0"), "s1")
    src.prepare()
    assert ssh.commands[0] == "lvchange -ay -K pve/snap_vm-100-disk-0_s1"
    assert src.size() == 4194304
    src.cleanup()
    assert ssh.commands[-1] == "lvchange -an pve/snap_vm-100-disk-0_s1"


def test_low_priority_prefix(monkeypatch):
    streamed = []

    class SSH(FakeSSH):
        def stream(self, cmd):
            streamed.append(cmd)

    src = RbdSource(SSH(), {"type": "rbd"}, Disk("scsi0", "ceph:vm-1-disk-0"), "s")
    src.open_stream()
    assert streamed[0].startswith("ionice -c 2 -n 7 nice -n 10 rbd export")


def test_dataset_stream_requires_prepare():
    disk = Disk(key="rootfs", volid="local-zfs:subvol-200-disk-0", options={})
    src = ZfsDatasetSource(FakeSSH(), {"type": "zfspool", "pool": "rpool/data"}, disk, "antigrafa_1")
    with pytest.raises(RuntimeError, match="prepare"):
        src.stream_command()
