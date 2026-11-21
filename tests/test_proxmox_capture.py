import pytest

from antigrafa.platforms.proxmox.capture import (
    Disk,
    LvmThinSource,
    RbdSource,
    UnsupportedStorage,
    ZfsDatasetSource,
    ZfsZvolSource,
    guest_disks,
    make_source,
    parse_size,
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
    assert src.stream_command() == "dd if=/dev/zvol/rpool/data/vm-100-disk-0@antigrafa_1 bs=4M iflag=direct status=none"
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


def test_dataset_size_estimate_from_snapshot():
    ssh = FakeSSH({"zfs get -Hp -o value logicalreferenced": "989855744\n"})
    src = ZfsDatasetSource(ssh, {"type": "zfspool", "pool": "rpool/data"}, Disk("rootfs", "z:subvol-150-disk-0"), "antigrafa_1")
    assert src.size() is None  # the exact size stays unknown: the backup is not checked against it
    assert src.estimated_size() == 989855744
    assert ssh.commands[-1] == "zfs get -Hp -o value logicalreferenced rpool/data/subvol-150-disk-0@antigrafa_1"


def test_dataset_size_estimate_failure_is_harmless():
    class Broken(FakeSSH):
        def run(self, cmd, timeout=120):
            raise RuntimeError("zfs: dataset busy")

    src = ZfsDatasetSource(Broken(), {"type": "zfspool", "pool": "p"}, Disk("rootfs", "z:subvol-1-disk-0"), "s")
    assert src.estimated_size() is None
    garbage = ZfsDatasetSource(FakeSSH({"zfs get": "-\n"}), {"type": "zfspool", "pool": "p"}, Disk("rootfs", "z:subvol-1-disk-0"), "s")
    assert garbage.estimated_size() is None


def test_block_devices_need_no_estimate():
    src = RbdSource(FakeSSH(), {"type": "rbd"}, Disk("scsi0", "ceph:vm-1-disk-0"), "s")
    assert src.estimated_size() is None  # exact size known: default of DiskSource


class StreamingSSH(FakeSSH):
    def stream(self, cmd, input=None):
        self.streamed = (cmd, input)
        return "stream"


def test_hashed_stream_pipes_the_read_through_the_script():
    import shlex

    from antigrafa import delta

    ssh = StreamingSSH()
    zfs = {"type": "zfspool", "pool": "rpool/data"}
    src = make_source(ssh, zfs, Disk("scsi0", "local-zfs:vm-100-disk-0"), "snap1", remote_hash=True)
    reference = [b"a" * 32, b"b" * 32]
    assert src.open_hashed_stream(reference, 4096) == "stream"
    cmd, sent = ssh.streamed
    assert sent == delta.encode_reference(reference)
    argv = shlex.split(cmd)
    # Same priority as a plain read, for the script and the read it runs
    assert argv[:7] == ["ionice", "-c", "2", "-n", "7", "nice", "-n"]
    assert "python3" in argv and argv[-2] == "4096"
    assert argv[-1] == src.stream_command()


def test_hashed_stream_disabled():
    ssh = StreamingSSH()
    src = make_source(ssh, {"type": "rbd", "pool": "vms"}, Disk("scsi0", "ceph:vm-1-disk-0"), "s")
    assert src.open_hashed_stream([b"a" * 32], 4096) is None  # remote_hash off by default here


# --------------------------------------------------------------------------- stopped reads


class Stream:
    def __init__(self, aborted):
        self.aborted = aborted


class StreamSSH(FakeSSH):
    def __init__(self, replies=None, aborted=True):
        super().__init__(replies)
        self.aborted = aborted

    def stream(self, cmd, input=None):
        return Stream(self.aborted)


def _stop(marker_pattern):
    return lambda cmd: cmd.startswith(f"pkill -KILL -f -- '{marker_pattern}'")


def test_aborted_read_kills_the_reader_before_snapdev_is_restored():
    ssh = StreamSSH({"zfs get -H -o value,source snapdev": "hidden\tdefault\n"})
    src = ZfsZvolSource(ssh, {"type": "zfspool", "pool": "rpool/data"}, Disk("scsi0", "z:vm-100-disk-0"), "antigrafa_1")
    src.prepare()
    src.open_stream()
    src.cleanup()
    stop, restore = ssh.commands[-2:]
    assert _stop("[/]dev/zvol/rpool/data/vm-100-disk-0@antigrafa_1")(stop)
    assert "pgrep -f" in stop and stop.endswith("; true")  # waits, never fails the cleanup
    assert restore == "zfs inherit snapdev rpool/data/vm-100-disk-0"
    src.cleanup()  # once only
    assert sum("pkill" in c for c in ssh.commands) == 1


def test_finished_read_kills_nothing():
    ssh = StreamSSH({"zfs get -H -o value,source snapdev": "visible\tlocal\n"}, aborted=False)
    src = ZfsZvolSource(ssh, {"type": "zfspool", "pool": "tank"}, Disk("scsi0", "z:vm-1-disk-0"), "s")
    src.prepare()
    src.open_stream()
    src.cleanup()
    assert not any("pkill" in c for c in ssh.commands)


def test_aborted_hashed_read_kills_the_reader_too():
    ssh = StreamSSH()
    src = RbdSource(ssh, {"type": "rbd", "pool": "vms"}, Disk("scsi0", "ceph:vm-1-disk-0"), "s1", remote_hash=True)
    src.open_hashed_stream([b"\0" * 32], 4 * 1024 * 1024)
    src.cleanup()
    assert _stop("[v]ms/vm-1-disk-0@s1")(ssh.commands[-1])


def test_every_storage_has_its_own_reader_marker():
    ssh = StreamSSH({"zfs get -H -o value mountpoint": "/rpool/data/subvol-101-disk-0\n"})
    tree = ZfsDatasetSource(ssh, {"type": "zfspool", "pool": "rpool/data"}, Disk("rootfs", "z:subvol-101-disk-0"), "s1")
    tree.prepare()
    tree.open_stream()
    tree.cleanup()
    # "." is special in a regular expression: in brackets, it is just a dot
    assert _stop("[.]zfs/snapshot/s1")(ssh.commands[-1])

    lvm = LvmThinSource(ssh, {"type": "lvmthin", "vgname": "pve"}, Disk("scsi0", "local-lvm:vm-100-disk-0"), "s1")
    lvm.prepare()
    lvm.open_stream()
    lvm.cleanup()
    stop, deactivate = ssh.commands[-2:]
    assert _stop("[/]dev/pve/snap_vm-100-disk-0_s1")(stop)
    assert deactivate == "lvchange -an pve/snap_vm-100-disk-0_s1"


def test_stop_command_escapes_regular_expression_characters():
    from antigrafa.platforms.proxmox.capture import stop_readers_command

    cmd = stop_readers_command("pool/vm-1+x.y@s")
    assert cmd.startswith(r"pkill -KILL -f -- '[p]ool/vm-1\+x\.y@s'")


@pytest.mark.parametrize(
    ("value", "size"),
    [("32G", 32 * 1024**3), ("528K", 528 * 1024), ("4M", 4 * 1024**2), ("1.5T", int(1.5 * 1024**4)),
     ("4096", 4096), (" 8G ", 8 * 1024**3), ("", None), ("big", None), ("8 G", None)],
)
def test_parse_size(value, size):
    assert parse_size(value) == size


# --------------------------------------------------------------------------- storage snapshots


def test_guest_volumes_include_disks_not_backed_up():
    from antigrafa.platforms.proxmox.capture import guest_volumes

    volumes = [(d.key, backed_up) for d, backed_up in guest_volumes("qemu", QEMU_CONFIG)]
    assert volumes == [("efidisk0", True), ("scsi0", True), ("scsi1", False)]  # no CD-ROM, no unused


STORAGES = {
    "local-zfs": {"type": "zfspool"},
    "thick": {"type": "lvm"},
    "san": {"type": "iscsi"},
    "nas": {"type": "nfs"},
    "local": {"type": "dir"},
}


def test_snapshot_blockers_name_every_disk_proxmox_refuses():
    from antigrafa.platforms.proxmox.capture import snapshot_blockers

    config = {
        "scsi0": "local-zfs:vm-650-disk-0,size=32G",
        "scsi1": "thick:vm-650-disk-1,backup=0,size=1T",  # not backed up: still counts
        "scsi2": "nas:650/vm-650-disk-2.raw,size=10G",
        "scsi3": "nas:650/vm-650-disk-3.qcow2,size=10G",  # qcow2: Proxmox can snapshot it
        "scsi4": "san:0.0.1.scsi-36001,size=100G",
        "scsi5": "/dev/disk/by-id/ata-XYZ,backup=0",
        "virtio0": "unknown-storage:vm-650-disk-5",  # not visible: left to Proxmox
        "ide2": "local:iso/debian.iso,media=cdrom",
    }
    blockers = snapshot_blockers("qemu", config, STORAGES.get)
    assert blockers == [
        "scsi1 (thick:vm-650-disk-1): storage type lvm has no snapshots",
        "scsi2 (nas:650/vm-650-disk-2.raw): on storage type nfs, only qcow2 files have snapshots",
        "scsi4 (san:0.0.1.scsi-36001): storage type iscsi has no snapshots",
        "scsi5 (/dev/disk/by-id/ata-XYZ): passthrough disk, no snapshot possible",
    ]


def test_snapshot_blockers_of_a_container():
    from antigrafa.platforms.proxmox.capture import snapshot_blockers

    config = {
        "rootfs": "local-zfs:subvol-101-disk-0,size=8G",
        "mp0": "/srv/share,mp=/share",  # bind mount: never part of a snapshot
        "mp1": "local:101/vm-101-disk-1.raw,mp=/data",  # not backed up: still counts
    }
    assert snapshot_blockers("lxc", config, STORAGES.get) == [
        "mp1 (local:101/vm-101-disk-1.raw): on storage type dir, only qcow2 files have snapshots"
    ]
    assert snapshot_blockers("lxc", {"rootfs": "local-zfs:subvol-101-disk-0"}, STORAGES.get) == []


def _zvol(ssh):
    return ZfsZvolSource(ssh, {"type": "zfspool", "pool": "rpool/data"}, Disk("scsi0", "local-zfs:vm-100-disk-0"), "antigrafa_1")


def test_zfs_storage_snapshot():
    ssh = FakeSSH({"zfs list -H -o name -t snapshot -d 1": "rpool/data/vm-100-disk-0@antigrafa_0\nrpool/data/vm-100-disk-0@daily\n"})
    for src in (_zvol(ssh), ZfsDatasetSource(ssh, {"type": "zfspool", "pool": "rpool/data"},
                                             Disk("rootfs", "local-zfs:vm-100-disk-0"), "antigrafa_1")):
        ssh.commands.clear()
        src.take_snapshot()
        src.drop_snapshot()
        assert ssh.commands == [
            "zfs snapshot rpool/data/vm-100-disk-0@antigrafa_1",
            # Deleting twice (a retry) is not an error
            "if zfs list -H -o name -t snapshot rpool/data/vm-100-disk-0@antigrafa_1 >/dev/null 2>&1; "
            "then zfs destroy rpool/data/vm-100-disk-0@antigrafa_1; fi",
        ]
        assert src.snapshots() == ["antigrafa_0", "daily"]


def test_rbd_storage_snapshot_on_an_external_cluster():
    ssh = FakeSSH({"rbd -m": '[{"id": 4, "name": "antigrafa_0"}, {"id": 5, "name": "before"}]'})
    cfg = {"type": "rbd", "pool": "vms", "namespace": "prod", "monhost": "10.0.0.1", "username": "pve"}
    src = RbdSource(ssh, cfg, Disk("scsi0", "ceph-ext:vm-100-disk-0"), "antigrafa_1")
    src.take_snapshot()
    src.drop_snapshot()
    rbd = "rbd -m 10.0.0.1 --keyring /etc/pve/priv/ceph/ceph-ext.keyring --id pve"
    assert ssh.commands == [
        f"{rbd} snap create vms/prod/vm-100-disk-0@antigrafa_1",
        f"if {rbd} info vms/prod/vm-100-disk-0@antigrafa_1 >/dev/null 2>&1; "
        f"then {rbd} snap rm --no-progress vms/prod/vm-100-disk-0@antigrafa_1; fi",
    ]
    assert src.snapshots() == ["antigrafa_0", "before"]
    assert ssh.commands[-1] == f"{rbd} snap ls --format json vms/prod/vm-100-disk-0"


def test_lvmthin_storage_snapshot_named_like_proxmox():
    ssh = FakeSSH({"lvs --noheadings": "  vm-100-disk-0\n  snap_vm-100-disk-0_antigrafa_0\n  snap_vm-100-disk-1_x\n"})
    src = LvmThinSource(ssh, {"type": "lvmthin", "vgname": "pve"}, Disk("scsi0", "local-lvm:vm-100-disk-0"), "antigrafa_1")
    src.take_snapshot()
    src.drop_snapshot()
    assert ssh.commands == [
        # The volume the reader activates (prepare) is this one
        "lvcreate -s -n snap_vm-100-disk-0_antigrafa_1 pve/vm-100-disk-0",
        "if lvs pve/snap_vm-100-disk-0_antigrafa_1 >/dev/null 2>&1; then lvremove -f pve/snap_vm-100-disk-0_antigrafa_1; fi",
    ]
    assert src.device == "/dev/pve/snap_vm-100-disk-0_antigrafa_1"
    assert src.snapshots() == ["antigrafa_0"]  # only this volume's


# --------------------------------------------------------------------------- stopped VM: live volumes


def _live(cfg, volid, key="scsi0", ssh=None, **replies):
    ssh = ssh or StreamingSSH(replies)
    return ssh, make_source(ssh, cfg, Disk(key, volid), "antigrafa_1", live=True)


def test_live_zvol_reads_the_volume_itself():
    ssh, src = _live({"type": "zfspool", "pool": "rpool/data"}, "local-zfs:vm-100-disk-0")
    src.prepare()
    src.open_stream()
    assert src.device == "/dev/zvol/rpool/data/vm-100-disk-0"
    assert not any("snapdev" in c for c in ssh.commands)  # no snapshot device to show
    assert ssh.streamed[0].endswith("dd if=/dev/zvol/rpool/data/vm-100-disk-0 bs=4M iflag=direct status=none")


def test_live_dataset_tars_the_mountpoint():
    ssh = StreamingSSH({"zfs get -H -o value mountpoint": "/rpool/data/subvol-101-disk-0\n"})
    _, src = _live({"type": "zfspool", "pool": "rpool/data"}, "local-zfs:subvol-101-disk-0", "rootfs", ssh=ssh)
    src.prepare()
    assert src.stream_command().endswith("-cf - -C /rpool/data/subvol-101-disk-0 .")
    src.estimated_size()
    assert ssh.commands[-1] == "zfs get -Hp -o value logicalreferenced rpool/data/subvol-101-disk-0"


def test_live_rbd_exports_the_image():
    _, src = _live({"type": "rbd", "pool": "vms"}, "ceph:vm-100-disk-0")
    assert src.stream_command() == "rbd export --no-progress vms/vm-100-disk-0 -"


@pytest.mark.parametrize(("attr", "activated"), [("  -wi-ao----\n", False), ("  -wi-------\n", True)])
def test_live_lvm_volumes_are_activated_only_when_inactive(attr, activated):
    for cfg, volid in (({"type": "lvmthin", "vgname": "pve"}, "local-lvm:vm-100-disk-0"),
                       ({"type": "lvm", "vgname": "san"}, "thick:vm-100-disk-0")):
        vg = cfg["vgname"]
        ssh, src = _live(cfg, volid, **{"lvs --noheadings -o lv_attr": attr})
        src.prepare()
        assert src.device == f"/dev/{vg}/vm-100-disk-0"
        assert (f"lvchange -ay {vg}/vm-100-disk-0" in ssh.commands) is activated
        src.cleanup()
        # Put back as found: a shared LVM volume stays active only where the guest runs
        assert (ssh.commands[-1] == f"lvchange -an {vg}/vm-100-disk-0") is activated


def test_live_iscsi_lun_is_read_by_id():
    _, src = _live({"type": "iscsi", "portal": "10.0.0.5"}, "ISCSI-QNAP:0.0.8.scsi-36e843b694fe4fe4df4b9d4d16d832bd3")
    assert src.device == "/dev/disk/by-id/scsi-36e843b694fe4fe4df4b9d4d16d832bd3"
    with pytest.raises(UnsupportedStorage, match="not an iSCSI LUN name"):
        _live({"type": "iscsi"}, "ISCSI-QNAP:weird")


def test_live_raw_file_on_a_directory_storage():
    ssh, src = _live({"type": "nfs", "path": "/mnt/pve/nas/"}, "nas:100/vm-100-disk-0.raw", **{"stat": "1024\n"})
    src.prepare()
    assert src.size() == 1024
    assert ssh.commands == ["test -f /mnt/pve/nas/images/100/vm-100-disk-0.raw",
                            "stat -L -c %s /mnt/pve/nas/images/100/vm-100-disk-0.raw"]
    assert src.snapshots() == []
    with pytest.raises(UnsupportedStorage, match="has no snapshots"):
        src.take_snapshot()
    # O_DIRECT (no page cache) when the filesystem accepts it, tested on one block first
    image = "/mnt/pve/nas/images/100/vm-100-disk-0.raw"
    assert src.stream_command() == (
        f"if dd if={image} bs=4096 count=1 iflag=direct of=/dev/null status=none 2>/dev/null; "
        f"then dd if={image} bs=4M iflag=direct status=none; else dd if={image} bs=4M status=none; fi"
    )


@pytest.mark.parametrize(
    ("cfg", "volid"),
    [({"type": "lvm", "vgname": "san"}, "thick:vm-1-disk-0"),
     ({"type": "iscsi"}, "lun:0.0.1.scsi-abc"),
     ({"type": "dir", "path": "/var/lib/vz"}, "local:1/vm-1-disk-0.raw")],
)
def test_storages_without_snapshots_need_the_qemu_mode(cfg, volid):
    with pytest.raises(UnsupportedStorage, match='snapshot_mode = "qemu"'):
        make_source(FakeSSH(), cfg, Disk("scsi0", volid), "s")


def test_qcow2_stays_unsupported_even_stopped():
    with pytest.raises(UnsupportedStorage, match="qcow2"):
        make_source(FakeSSH(), {"type": "dir", "path": "/x"}, Disk("scsi0", "local:1/vm-1-disk-0.qcow2"), "s", live=True)
