import pytest

from antigrafa import platforms
from antigrafa.platforms.base import AccessDenied, BackupPlan, DiskSource, Guest, Platform, PlatformError, check_name
from antigrafa.platforms.proxmox import ProxmoxPlatform, ProxmoxSettings


def test_registry_builds_proxmox():
    settings = platforms.parse_settings("proxmox", "c1", {"url": "https://pve:8006", "token_id": "a@pve!b", "token_secret": "s"})
    platform = platforms.create_platform("proxmox", "c1", settings)
    assert isinstance(platform, ProxmoxPlatform)
    assert (platform.type, platform.name) == ("proxmox", "c1")
    assert platform.api.base == "https://pve:8006/api2/json"


def test_registry_rejects_unknown_type():
    with pytest.raises(ValueError, match=r"unknown platform type 'hyperv' \(supported: proxmox\)"):
        platforms.parse_settings("hyperv", "h", {})
    with pytest.raises(ValueError, match="unknown platform type"):
        platforms.create_platform("vmware", "v", None)


@pytest.mark.parametrize("value", ["100", "vm-42", "a.b_c-d", "6F9619FF-8B86-D011-B42D-00C04FC964FF"])
def test_safe_names(value):
    assert check_name(value, "guest id") == value


@pytest.mark.parametrize("value", ["", ".", "..", ".x", "a/b", "a\b", "a b", "é"])
def test_unsafe_names(value):
    with pytest.raises(ValueError, match="invalid guest id"):
        check_name(value, "guest id")


def test_guest_model():
    vm = Guest("vm-42", "vm", "web", "esx1", "poweredOn")
    assert vm.label == "VM vm-42 (web)"
    assert Guest("7", "ct", "dns", "h").label == "CT 7 (dns)"
    assert Guest("7", "ct", "dns", "h").status == ""
    with pytest.raises(ValueError, match="unknown guest kind 'pod'"):
        Guest("1", "pod", "x", "h")
    with pytest.raises(ValueError, match="invalid guest id"):
        Guest("../1", "vm", "x", "h")


class Minimal(Platform):
    """Only the required methods: the optional ones keep their defaults."""

    type = "minimal"

    def guests(self):
        return []

    def plan_backup(self, guest, snapshot, low_priority=True, previous=None):
        return BackupPlan(config={})

    def create_snapshot(self, guest, snapshot):
        pass

    def delete_snapshot(self, guest, snapshot):
        pass


class Disk(DiskSource):
    key = "d0"
    volume = "v"

    def size(self):
        return None

    def open_stream(self):
        raise NotImplementedError


def test_platform_defaults():
    platform = Minimal("lab")
    guest = Guest("1", "vm", "x", "h")
    assert platform.name == "lab"
    assert platform.leftover_snapshots(guest, "antigrafa_") == []
    assert platform.host_iowait("h") is None  # cannot measure: no load throttling
    assert platform.plan_backup(guest, "s").disks == []
    platform.check_access([guest])  # default: no rights to check

    disk = Disk()
    assert disk.format == "raw"
    disk.prepare()  # default: nothing to do
    disk.cleanup()


def test_proxmox_platform_maps_calls_to_the_api():
    calls = []

    class API:
        def create_snapshot(self, g, name, description=""):
            calls.append(("create", g.id, name, description))

        def delete_snapshot(self, g, name):
            calls.append(("delete", g.id, name))

        def list_snapshots(self, g):
            return [{"name": "antigrafa_1"}, {"name": "before-upgrade"}, {"description": "no name"}]

        def node_iowait(self, node):
            return 0.2

        def guests(self):
            return ["listed"]

    settings = ProxmoxSettings(url="https://pve", token_id="a@pve!b")
    platform = ProxmoxPlatform("c1", settings, api=API())
    g = Guest("100", "vm", "web", "pve1")
    platform.create_snapshot(g, "antigrafa_2")
    platform.delete_snapshot(g, "antigrafa_2")
    assert calls[0][:3] == ("create", "100", "antigrafa_2") and "Temporary" in calls[0][3]
    assert calls[1] == ("delete", "100", "antigrafa_2")
    assert platform.leftover_snapshots(g, "antigrafa_") == ["antigrafa_1"]
    assert platform.host_iowait("pve1") == 0.2
    assert platform.guests() == ["listed"]


class LockedAPI:
    """Refuses the deletion while the guest is locked by a failed one."""

    def __init__(self, error="VM is locked (snapshot-delete)"):
        self.locked = True
        self.error = error
        self.deleted = []

    def delete_snapshot(self, g, name):
        if self.locked:
            from antigrafa.platforms.proxmox import ProxmoxError

            raise ProxmoxError(f"task UPID:pve1:qmdelsnapshot:{g.id}: {self.error}")
        self.deleted.append((g.id, name))


class UnlockSSH:
    def __init__(self, api):
        self.api, self.commands = api, []

    def run(self, cmd, timeout=120):
        self.commands.append(cmd)
        self.api.locked = False
        return ""


@pytest.mark.parametrize(("kind", "tool"), [("vm", "qm"), ("ct", "pct")])
def test_guest_left_locked_by_a_failed_deletion_is_unlocked(kind, tool, caplog):
    api = LockedAPI()
    ssh = UnlockSSH(api)
    platform = ProxmoxPlatform("c1", ProxmoxSettings(url="https://pve", token_id="a@pve!b"), api=api,
                               ssh_for_host=lambda node: ssh)
    g = Guest("111", kind, "win", "pve1")
    platform.delete_snapshot(g, "antigrafa_1")
    assert ssh.commands == [f"{tool} unlock 111"]
    assert api.deleted == [("111", "antigrafa_1")]
    assert f"locked by the failed deletion of antigrafa_1: {tool} unlock" in caplog.text


def test_other_deletion_errors_are_not_unlocked():
    from antigrafa.platforms.proxmox import ProxmoxError

    api = LockedAPI(error="zfs error: cannot destroy snapshot: dataset is busy")
    ssh = UnlockSSH(api)
    platform = ProxmoxPlatform("c1", ProxmoxSettings(url="https://pve", token_id="a@pve!b"), api=api,
                               ssh_for_host=lambda node: ssh)
    with pytest.raises(ProxmoxError, match="dataset is busy"):
        platform.delete_snapshot(Guest("111", "vm", "win", "pve1"), "antigrafa_1")
    assert ssh.commands == []  # never unlocks a guest for another reason


def test_proxmox_platform_passes_verify_tls_to_the_api(caplog):
    import ssl

    insecure = ProxmoxSettings(url="https://pve", token_id="a@pve!b", token_secret="s", verify_tls=False)
    assert ProxmoxPlatform("lab", insecure).api._ctx.verify_mode == ssl.CERT_NONE
    secure = ProxmoxSettings(url="https://pve", token_id="a@pve!b", token_secret="s")
    assert ProxmoxPlatform("prod", secure).api._ctx.verify_mode == ssl.CERT_REQUIRED


class PermAPI:
    """Fake API with per-path privileges (every privilege everywhere by default)."""

    ALL = {"VM.Audit", "VM.Snapshot", "Sys.Audit", "Datastore.Audit"}

    def __init__(self, configs=None, denied=None):
        self.configs = configs or {}
        self.denied = denied or {}  # path -> privileges missing there
        self.asked = []

    def permissions(self, path):
        self.asked.append(path)
        return self.ALL - set(self.denied.get(path, ()))

    def guest_config(self, g):
        return self.configs[g.id]


def _checked(api):
    return ProxmoxPlatform("c1", ProxmoxSettings(url="https://pve", token_id="antigrafa@pve!backup"), api=api)


VM = Guest("100", "vm", "web", "pve1")
CT = Guest("200", "ct", "dns", "pve2")
CONFIGS = {
    "100": {"scsi0": "local-zfs:vm-100-disk-0", "scsi1": "ceph:vm-100-disk-1"},
    "200": {"rootfs": "local-zfs:subvol-200-disk-0"},
}


def test_check_access_all_rights():
    api = PermAPI(CONFIGS)
    _checked(api).check_access([VM, CT])
    assert api.asked == ["/vms/100", "/vms/200", "/nodes/pve1", "/nodes/pve2", "/storage/ceph", "/storage/local-zfs"]


def test_check_access_lists_everything_missing():
    api = PermAPI(CONFIGS, denied={"/vms/100": ["VM.Snapshot"], "/nodes/pve2": ["Sys.Audit"]})
    with pytest.raises(AccessDenied) as err:
        _checked(api).check_access([VM, CT])
    text = str(err.value)
    assert "API token antigrafa@pve!backup lacks permissions, nothing was done" in text
    assert "  - VM.Snapshot on /vms/100 (VM 100 (web))" in text
    assert "  - Sys.Audit on /nodes/pve2 (host load)" in text
    assert "/vms/200" not in text  # that guest has every right
    assert 'pveum role add AntigrafaBackup --privs "VM.Audit VM.Snapshot Sys.Audit Datastore.Audit"' in text
    assert "--tokens 'antigrafa@pve!backup'" in text
    # Configurations are not read without VM.Audit: storages checked only once that passed
    assert not any(path.startswith("/storage") for path in api.asked)


def test_check_access_storage_and_audit():
    api = PermAPI(CONFIGS, denied={"/storage/ceph": ["Datastore.Audit"], "/vms/200": ["VM.Audit", "VM.Snapshot"]})
    with pytest.raises(AccessDenied, match=r"VM.Audit, VM.Snapshot on /vms/200 \(CT 200 \(dns\)\)"):
        _checked(api).check_access([VM, CT])
    api = PermAPI(CONFIGS, denied={"/storage/ceph": ["Datastore.Audit"]})
    with pytest.raises(AccessDenied, match=r"Datastore.Audit on /storage/ceph \(storage configuration\)"):
        _checked(api).check_access([VM])


def test_check_access_skips_unsupported_disks():
    # A passthrough disk is that guest's own failure (with its reason), not a rights problem
    api = PermAPI({"100": {"scsi0": "/dev/sdb"}, "200": CONFIGS["200"]})
    _checked(api).check_access([VM, CT])
    assert "/storage/local-zfs" in api.asked


def test_check_access_without_guests_checks_listing():
    api = PermAPI(denied={"/vms": ["VM.Audit"]})
    with pytest.raises(AccessDenied, match=r"VM.Audit on /vms \(list the guests\)"):
        _checked(api).check_access([])
    _checked(PermAPI()).check_access([])  # rights, simply no guest


class PlanAPI:
    def guest_config(self, g):
        return {"scsi0": "local-zfs:vm-100-disk-0"}

    def storage(self, sid):
        return {"type": "zfspool", "pool": "rpool/data"}


class PythonSSH:
    def __init__(self, reply="/usr/bin/python3\n", error=None):
        self.reply, self.error, self.calls = reply, error, 0

    def run(self, cmd, timeout=120):
        assert cmd == "command -v python3 || true"
        self.calls += 1
        if self.error:
            raise self.error
        return self.reply


def _planner(ssh, **settings):
    s = ProxmoxSettings(url="https://pve", token_id="a@pve!b", **settings)
    return ProxmoxPlatform("c1", s, api=PlanAPI(), ssh_for_host=lambda node: ssh)


def test_remote_hash_when_python3_is_on_the_node():
    ssh = PythonSSH()
    platform = _planner(ssh)
    for _ in range(3):
        [disk] = platform.plan_backup(VM, "s").disks
        assert disk.remote_hash is True
    assert ssh.calls == 1  # checked once per node


def test_no_python3_on_the_node_sends_everything(caplog):
    [disk] = _planner(PythonSSH(reply="")).plan_backup(VM, "s").disks
    assert disk.remote_hash is False
    assert "pve1: python3 not found" in caplog.text and "apt install python3" in caplog.text
    assert "check failed" not in caplog.text


def test_failed_python3_check_says_why(caplog):
    from antigrafa.ssh import RemoteCommandError

    error = RemoteCommandError("pve1", "command -v python3 || true", 255, "Permission denied (publickey)")
    [disk] = _planner(PythonSSH(error=error)).plan_backup(VM, "s").disks
    assert disk.remote_hash is False
    assert "pve1: python3 check failed, every chunk is sent" in caplog.text
    assert "Permission denied (publickey)" in caplog.text  # shown without -v: a warning
    assert "not found" not in caplog.text


def test_remote_hash_turned_off():
    ssh = PythonSSH()
    [disk] = _planner(ssh, remote_hash=False).plan_backup(VM, "s").disks
    assert disk.remote_hash is False and ssh.calls == 0


def test_disk_sizes_from_the_proxmox_configuration():
    class API:
        def guest_config(self, g):
            return {
                "scsi0": "local-zfs:vm-100-disk-0,size=32G",
                "scsi1": "local-zfs:vm-100-disk-1,backup=0,size=100G",  # not backed up
                "efidisk0": "local-lvm:vm-100-disk-2,efitype=4m,size=528K",
                "sata0": "local-lvm:vm-100-disk-3",  # no size recorded
                "ide2": "local:iso/debian.iso,media=cdrom,size=600M",
            }

    platform = ProxmoxPlatform("c1", ProxmoxSettings(url="https://pve", token_id="a@pve!b"), api=API())
    assert platform.disk_sizes(Guest("100", "vm", "web", "pve1")) == {"scsi0": 32 * 1024**3, "efidisk0": 528 * 1024}


def test_platform_without_disk_sizes():
    class Bare(Platform):
        type = "bare"

        def guests(self):
            return []

        def plan_backup(self, guest, snapshot, low_priority=True, previous=None):
            return BackupPlan(config={}, disks=[])

        def create_snapshot(self, guest, snapshot):
            pass

        def delete_snapshot(self, guest, snapshot):
            pass

    assert Bare("x").disk_sizes(Guest("1", "vm", "a", "h")) == {}


# --------------------------------------------------------------------------- snapshot modes


class ModeAPI:
    """A VM with a ZFS disk and an LVM-thin disk, and a disk on thick LVM not backed up."""

    def __init__(self, config=None):
        self.config = config or {
            "agent": "1",
            "scsi0": "local-zfs:vm-650-disk-0,size=32G",
            "scsi1": "local-lvm:vm-650-disk-1,size=8G",
            "scsi2": "thick:vm-650-disk-2,backup=0,size=1T",
        }
        self.snapshots = []
        self.api_calls = []

    def guest_config(self, g):
        return self.config

    def storage(self, sid):
        return {"local-zfs": {"type": "zfspool", "pool": "rpool/data"}, "local-lvm": {"type": "lvmthin", "vgname": "pve"},
                "thick": {"type": "lvm", "vgname": "data"}}[sid]

    def create_snapshot(self, g, name, description=""):
        self.api_calls.append(("create", name))

    def delete_snapshot(self, g, name):
        self.api_calls.append(("delete", name))

    def list_snapshots(self, g):
        return [{"name": n} for n in self.snapshots]


class NodeSSH:
    """Records the commands; those starting with a key of ``fail`` fail (n times, or always)."""

    def __init__(self, fail=None, replies=None):
        self.commands, self.fail, self.replies = [], dict(fail or {}), replies or {}

    def run(self, cmd, timeout=120):
        from antigrafa.ssh import RemoteCommandError

        self.commands.append(cmd)
        for prefix, times in self.fail.items():
            if cmd.startswith(prefix) and times:
                self.fail[prefix] = times - 1
                raise RemoteCommandError("pve1", cmd, 255, f"{prefix}: refused")
        return next((reply for prefix, reply in self.replies.items() if cmd.startswith(prefix)), "")


RUNNING_VM = Guest("650", "vm", "nextcloud", "pve1", "running")
SNAP = "antigrafa_20261120T100000"
ZFS_SNAP = f"zfs snapshot rpool/data/vm-650-disk-0@{SNAP}"
LVM_SNAP = f"lvcreate -s -n snap_vm-650-disk-1_{SNAP} pve/vm-650-disk-1"


def _platform(api, ssh, mode="storage"):
    settings = ProxmoxSettings(url="https://pve", token_id="a@pve!b", remote_hash=False, snapshot_mode=mode)
    return ProxmoxPlatform("c1", settings, api=api, ssh_for_host=lambda node: ssh)


def test_proxmox_mode_names_the_disk_that_blocks_the_snapshot():
    from antigrafa.platforms.proxmox import UnsupportedStorage

    platform = _platform(ModeAPI(), NodeSSH(), mode="proxmox")
    with pytest.raises(UnsupportedStorage) as exc:
        platform.plan_backup(RUNNING_VM, SNAP)
    message = str(exc.value)
    assert "scsi2 (thick:vm-650-disk-2): storage type lvm has no snapshots" in message
    assert 'snapshot_mode = "storage"' in message and "backup=0 ones included" in message


def test_proxmox_mode_leaves_unknown_storages_to_proxmox():
    class Hidden(ModeAPI):
        def storage(self, sid):
            if sid == "thick":
                from antigrafa.platforms.proxmox import ProxmoxError

                raise ProxmoxError("storage thick: not visible to the token")
            return super().storage(sid)

    plan = _platform(Hidden(), NodeSSH(), mode="proxmox").plan_backup(RUNNING_VM, SNAP)
    assert [d.key for d in plan.disks] == ["scsi0", "scsi1"]


def test_storage_mode_freezes_snapshots_the_backed_up_volumes_and_thaws():
    api, ssh = ModeAPI(), NodeSSH()
    platform = _platform(api, ssh)
    plan = platform.plan_backup(RUNNING_VM, SNAP)  # the thick LVM disk does not block
    assert [d.key for d in plan.disks] == ["scsi0", "scsi1"]
    platform.create_snapshot(RUNNING_VM, SNAP)
    assert ssh.commands == [
        "qm guest cmd 650 fsfreeze-freeze",
        ZFS_SNAP,
        LVM_SNAP,
        "qm guest cmd 650 fsfreeze-thaw",
    ]
    ssh.commands.clear()
    platform.delete_snapshot(RUNNING_VM, SNAP)
    assert [c.split(" then ")[1] for c in ssh.commands] == [
        f"zfs destroy rpool/data/vm-650-disk-0@{SNAP}; fi",
        f"lvremove -f pve/snap_vm-650-disk-1_{SNAP}; fi",
    ]
    assert api.api_calls == []  # Proxmox is not asked for anything


def test_proxmox_mode_still_snapshots_through_the_api():
    api = ModeAPI({"scsi0": "local-zfs:vm-650-disk-0"})
    ssh = NodeSSH()
    platform = _platform(api, ssh, mode="proxmox")
    platform.plan_backup(RUNNING_VM, SNAP)
    platform.create_snapshot(RUNNING_VM, SNAP)
    platform.delete_snapshot(RUNNING_VM, SNAP)
    assert api.api_calls == [("create", SNAP), ("delete", SNAP)] and ssh.commands == []


@pytest.mark.parametrize(
    ("agent", "frozen"),
    [("1", True), ("enabled=1,fstrim_cloned_disks=1", True), ("0", False), ("enabled=0", False),
     ("1,freeze-fs-on-backup=0", False), (None, False)],
)
def test_vm_freeze_follows_the_agent_setting(agent, frozen, caplog):
    import logging

    caplog.set_level(logging.INFO)
    config = {"scsi0": "local-zfs:vm-650-disk-0"}
    if agent is not None:
        config["agent"] = agent
    ssh = NodeSSH()
    platform = _platform(ModeAPI(config), ssh)
    platform.plan_backup(RUNNING_VM, SNAP)
    platform.create_snapshot(RUNNING_VM, SNAP)
    assert ("qm guest cmd 650 fsfreeze-freeze" in ssh.commands) is frozen
    assert ("crash-consistent" in caplog.text) is not frozen


def test_stopped_guest_is_not_frozen():
    ssh = NodeSSH()
    platform = _platform(ModeAPI(), ssh)
    stopped = Guest("650", "vm", "nextcloud", "pve1", "stopped")
    platform.plan_backup(stopped, SNAP)
    platform.create_snapshot(stopped, SNAP)
    assert ssh.commands == [ZFS_SNAP, LVM_SNAP]


def test_running_container_is_frozen_and_flushed():
    api = ModeAPI({"rootfs": "local-zfs:subvol-210-disk-0,size=8G"})
    ssh = NodeSSH()
    platform = _platform(api, ssh)
    ct = Guest("210", "ct", "dns", "pve1", "running")
    platform.plan_backup(ct, SNAP)
    platform.create_snapshot(ct, SNAP)
    assert ssh.commands == [
        "lxc-freeze -n 210 && sync",
        f"zfs snapshot rpool/data/subvol-210-disk-0@{SNAP}",
        "lxc-unfreeze -n 210",
    ]


def test_failed_freeze_gives_a_crash_consistent_snapshot(caplog):
    ssh = NodeSSH(fail={"qm guest cmd 650 fsfreeze-freeze": 1})
    platform = _platform(ModeAPI(), ssh)
    platform.plan_backup(RUNNING_VM, SNAP)
    platform.create_snapshot(RUNNING_VM, SNAP)
    # A freeze that failed (agent not running, timeout) may still have happened: thawed anyway
    assert ssh.commands == ["qm guest cmd 650 fsfreeze-freeze", "qm guest cmd 650 fsfreeze-thaw", ZFS_SNAP, LVM_SNAP]
    assert "filesystems not frozen, crash-consistent snapshot" in caplog.text


def test_failed_volume_snapshot_removes_the_others_and_thaws():
    from antigrafa.ssh import RemoteCommandError

    ssh = NodeSSH(fail={"lvcreate": 1})
    platform = _platform(ModeAPI(), ssh)
    platform.plan_backup(RUNNING_VM, SNAP)
    with pytest.raises(RemoteCommandError, match="lvcreate: refused"):
        platform.create_snapshot(RUNNING_VM, SNAP)
    assert ssh.commands[:4] == ["qm guest cmd 650 fsfreeze-freeze", ZFS_SNAP, LVM_SNAP, "qm guest cmd 650 fsfreeze-thaw"]
    assert f"zfs destroy rpool/data/vm-650-disk-0@{SNAP}" in ssh.commands[4]  # all or nothing
    assert len(ssh.commands) == 5


def test_thaw_is_tried_again(monkeypatch, caplog):
    from antigrafa.platforms import proxmox

    monkeypatch.setattr(proxmox, "THAW_RETRY_DELAYS", (0, 0, 0))
    ssh = NodeSSH(fail={"qm guest cmd 650 fsfreeze-thaw": 2})
    platform = _platform(ModeAPI(), ssh)
    platform.plan_backup(RUNNING_VM, SNAP)
    platform.create_snapshot(RUNNING_VM, SNAP)
    assert ssh.commands.count("qm guest cmd 650 fsfreeze-thaw") == 3
    assert "thaw failed, new try" in caplog.text


def test_guest_still_frozen_fails_loudly_without_leaving_snapshots(monkeypatch, caplog):
    from antigrafa.platforms import proxmox

    monkeypatch.setattr(proxmox, "THAW_RETRY_DELAYS", (0, 0, 0))
    ssh = NodeSSH(fail={"qm guest cmd 650 fsfreeze-thaw": 99})
    platform = _platform(ModeAPI(), ssh)
    platform.plan_backup(RUNNING_VM, SNAP)
    with pytest.raises(PlatformError, match="still frozen, run on pve1: qm guest cmd 650 fsfreeze-thaw"):
        platform.create_snapshot(RUNNING_VM, SNAP)
    assert ssh.commands.count("qm guest cmd 650 fsfreeze-thaw") == 4
    assert "STILL FROZEN" in caplog.text
    assert sum("zfs destroy" in c or "lvremove" in c for c in ssh.commands) == 2


def test_storage_deletion_tries_every_volume():
    ssh = NodeSSH(fail={"if zfs list": 1})
    platform = _platform(ModeAPI(), ssh)
    platform.plan_backup(RUNNING_VM, SNAP)
    with pytest.raises(PlatformError, match="local-zfs:vm-650-disk-0: .*refused"):
        platform.delete_snapshot(RUNNING_VM, SNAP)
    assert any("lvremove" in c for c in ssh.commands)  # the other one deleted anyway
    platform.delete_snapshot(RUNNING_VM, SNAP)  # the retry of the backup engine


def test_storage_snapshot_needs_a_plan():
    platform = _platform(ModeAPI(), NodeSSH())
    with pytest.raises(PlatformError, match="no backup planned for snapshot"):
        platform.create_snapshot(RUNNING_VM, SNAP)
    platform.plan_backup(RUNNING_VM, SNAP)
    with pytest.raises(PlatformError, match="no backup planned for snapshot other"):
        platform.delete_snapshot(RUNNING_VM, "other")


def test_storage_mode_finds_leftovers_on_the_volumes_and_in_proxmox():
    api = ModeAPI()
    api.snapshots = ["antigrafa_A", "manual"]
    ssh = NodeSSH(replies={
        "zfs list -H -o name -t snapshot -d 1": "rpool/data/vm-650-disk-0@antigrafa_B\nrpool/data/vm-650-disk-0@antigrafa_A\n",
        "lvs --noheadings": "snap_vm-650-disk-1_antigrafa_B\n",
    })
    platform = _platform(api, ssh)
    platform.plan_backup(RUNNING_VM, SNAP)
    assert platform.leftover_snapshots(RUNNING_VM, "antigrafa_") == ["antigrafa_A", "antigrafa_B"]
    proxmox_mode = _platform(api, NodeSSH(), mode="proxmox")
    assert proxmox_mode.leftover_snapshots(RUNNING_VM, "antigrafa_") == ["antigrafa_A"]


def test_storage_mode_does_not_need_the_snapshot_right():
    api = PermAPI(CONFIGS, denied={"/vms/100": {"VM.Snapshot"}})
    settings = ProxmoxSettings(url="https://pve", token_id="a@pve!b", snapshot_mode="storage")
    ProxmoxPlatform("c1", settings, api=api).check_access([VM])  # no VM.Snapshot: fine
    with pytest.raises(AccessDenied, match="VM.Snapshot on /vms/100"):
        _checked(api).check_access([VM])  # the Proxmox snapshot needs it


# --------------------------------------------------------------------------- "qemu" mode

PYTHON3 = {"command -v python3": "/usr/bin/python3\n"}
QMP_ANSWER = '{"sizes": {"scsi0": 34359738368, "scsi1": 8589934592}, "removed": []}'


def _qemu_ssh(status="running", **kwargs):
    replies = {**PYTHON3, "qm status 650": f"status: {status}\n", "python3 -c": QMP_ANSWER}
    return NodeSSH(replies=replies, **kwargs)


def _steps(commands):
    """The QMP script calls shortened to "qmp <action>"."""
    import shlex

    return [f"qmp {shlex.split(c)[3]}" if c.startswith("python3 -c") else c for c in commands]


def _storages(extra):
    def storage(sid):
        return extra[sid] if sid in extra else ModeAPI.storage(None, sid)

    return storage


def test_qemu_mode_backs_up_a_running_vm_through_qemu():
    api, ssh = ModeAPI(), _qemu_ssh()
    platform = _platform(api, ssh, mode="qemu")
    plan = platform.plan_backup(RUNNING_VM, SNAP)
    assert [d.key for d in plan.disks] == ["scsi0", "scsi1"]
    ssh.commands.clear()
    platform.create_snapshot(RUNNING_VM, SNAP)
    assert _steps(ssh.commands) == [
        "qm set 650 --lock backup",  # nothing may move or snapshot it meanwhile
        "qm status 650",
        "qmp setup",
        "qm guest cmd 650 fsfreeze-freeze",
        "qmp start",  # every disk at the same instant, frozen
        "qm guest cmd 650 fsfreeze-thaw",
    ]
    assert [(d.size(), d.reader_marker()) for d in plan.disks] == [
        (34359738368, "/run/antigrafa-650.nbd scsi0"),
        (8589934592, "/run/antigrafa-650.nbd scsi1"),
    ]
    ssh.commands.clear()
    platform.delete_snapshot(RUNNING_VM, SNAP)
    platform.delete_snapshot(RUNNING_VM, SNAP)  # a retry does nothing more
    assert _steps(ssh.commands) == ["qmp teardown", "qm unlock 650"]
    assert api.api_calls == []  # no Proxmox snapshot


def test_qemu_mode_backs_up_disks_on_storages_without_snapshots():
    api = ModeAPI({"agent": "1", "virtio2": "san:0.0.8.scsi-36e8,size=1T", "scsi1": "thick:vm-650-disk-2,size=8G",
                   "scsi3": "nfs:650/vm-650-disk-3.qcow2,size=8G"})
    api.storage = _storages({"san": {"type": "iscsi"}, "nfs": {"type": "nfs", "path": "/mnt/pve/nfs"}})
    plan = _platform(api, _qemu_ssh(), mode="qemu").plan_backup(RUNNING_VM, SNAP)
    assert [d.key for d in plan.disks] == ["scsi1", "scsi3", "virtio2"]  # qcow2 too: QEMU reads it


def test_qemu_mode_reads_a_stopped_vm_from_its_volumes():
    ssh = _qemu_ssh(status="stopped")
    platform = _platform(ModeAPI(), ssh, mode="qemu")
    plan = platform.plan_backup(RUNNING_VM, SNAP)  # listed as running, stopped since
    ssh.commands.clear()
    platform.create_snapshot(RUNNING_VM, SNAP)
    assert ssh.commands == ["qm set 650 --lock backup", "qm status 650"]  # locked: nobody starts it
    assert plan.disks[0].stream_command() == "dd if=/dev/zvol/rpool/data/vm-650-disk-0 bs=4M iflag=direct status=none"
    ssh.commands.clear()
    platform.delete_snapshot(RUNNING_VM, SNAP)
    assert ssh.commands == ["qm unlock 650"]


def test_qemu_mode_stopped_vm_on_a_storage_it_cannot_read_directly():
    from antigrafa.platforms.proxmox import UnsupportedStorage

    api = ModeAPI({"scsi0": "nfs:650/vm-650-disk-0.qcow2,size=8G"})
    api.storage = _storages({"nfs": {"type": "nfs", "path": "/mnt/pve/nfs"}})
    platform = _platform(api, _qemu_ssh(status="stopped"), mode="qemu")
    plan = platform.plan_backup(RUNNING_VM, SNAP)
    platform.create_snapshot(RUNNING_VM, SNAP)
    with pytest.raises(UnsupportedStorage, match="cannot be read while the VM is stopped: .*qcow2"):
        plan.disks[0].prepare()


def test_qemu_mode_reads_tpmstate_from_its_volume():
    import json
    import shlex

    from antigrafa.platforms.proxmox import UnsupportedStorage

    config = {"scsi0": "local-zfs:vm-650-disk-0", "tpmstate0": "local-lvm:vm-650-disk-1,size=4M,version=v2.0"}
    ssh = _qemu_ssh()
    platform = _platform(ModeAPI(config), ssh, mode="qemu")
    plan = platform.plan_backup(RUNNING_VM, SNAP)
    platform.create_snapshot(RUNNING_VM, SNAP)
    setup = next(c for c in ssh.commands if c.startswith("python3 -c"))
    assert json.loads(shlex.split(setup)[4])["keys"] == ["scsi0"]  # swtpm uses it, not QEMU
    tpm = plan.disks[1]
    assert tpm.key == "tpmstate0" and tpm.stream_command() == "dd if=/dev/pve/vm-650-disk-1 bs=4M iflag=direct status=none"
    bad = ModeAPI({"tpmstate0": "nfs:650/vm-650-disk-1.qcow2"})
    bad.storage = _storages({"nfs": {"type": "nfs", "path": "/mnt/pve/nfs"}})
    with pytest.raises(UnsupportedStorage, match="tpmstate0"):
        _platform(bad, _qemu_ssh(), mode="qemu").plan_backup(RUNNING_VM, SNAP)


def test_qemu_setup_failure_cleans_qemu_and_unlocks():
    from antigrafa.ssh import RemoteCommandError

    ssh = _qemu_ssh(fail={"python3 -c": 1})  # setup refused, the cleanup works
    platform = _platform(ModeAPI(), ssh, mode="qemu")
    platform.plan_backup(RUNNING_VM, SNAP)
    ssh.commands.clear()
    with pytest.raises(RemoteCommandError):
        platform.create_snapshot(RUNNING_VM, SNAP)
    assert _steps(ssh.commands) == ["qm set 650 --lock backup", "qm status 650", "qmp setup", "qmp teardown",
                                    "qm unlock 650"]


def test_qemu_cleanup_failure_keeps_the_vm_locked_until_a_retry_works():
    ssh = _qemu_ssh()
    platform = _platform(ModeAPI(), ssh, mode="qemu")
    platform.plan_backup(RUNNING_VM, SNAP)
    platform.create_snapshot(RUNNING_VM, SNAP)
    ssh.commands.clear()
    ssh.fail = {"python3 -c": 1}
    with pytest.raises(PlatformError, match="QEMU cleanup failed, the VM stays locked"):
        platform.delete_snapshot(RUNNING_VM, SNAP)
    assert "qm unlock 650" not in ssh.commands
    platform.delete_snapshot(RUNNING_VM, SNAP)  # the retry of the backup engine
    assert _steps(ssh.commands)[-2:] == ["qmp teardown", "qm unlock 650"]


def test_qemu_mode_does_nothing_on_a_vm_already_locked():
    from antigrafa.ssh import RemoteCommandError

    ssh = _qemu_ssh(fail={"qm set 650 --lock backup": 1})
    platform = _platform(ModeAPI(), ssh, mode="qemu")
    platform.plan_backup(RUNNING_VM, SNAP)
    with pytest.raises(RemoteCommandError):
        platform.create_snapshot(RUNNING_VM, SNAP)
    ssh.commands.clear()
    platform.delete_snapshot(RUNNING_VM, SNAP)
    assert ssh.commands == []  # not our lock: left alone


def test_qemu_mode_needs_python3_on_the_node():
    platform = _platform(ModeAPI(), NodeSSH(), mode="qemu")
    with pytest.raises(PlatformError, match='snapshot_mode = "qemu" needs python3'):
        platform.plan_backup(RUNNING_VM, SNAP)


def test_qemu_mode_snapshots_containers_on_their_storage():
    api = ModeAPI({"rootfs": "local-zfs:subvol-210-disk-0,size=8G"})
    ssh = _qemu_ssh()
    platform = _platform(api, ssh, mode="qemu")
    ct = Guest("210", "ct", "dns", "pve1", "running")
    platform.plan_backup(ct, SNAP)
    ssh.commands.clear()
    platform.create_snapshot(ct, SNAP)
    assert ssh.commands == [
        "lxc-freeze -n 210 && sync",
        f"zfs snapshot rpool/data/subvol-210-disk-0@{SNAP}",
        "lxc-unfreeze -n 210",
    ]


# --------------------------------------------------------------------------- "auto" mode, per guest


class AutoAPI(ModeAPI):
    """ModeAPI with the token's rights: VM.Snapshot unless ``no_snapshot_right``."""

    def __init__(self, config=None, no_snapshot_right=False):
        super().__init__(config)
        self.no_snapshot_right = no_snapshot_right
        self.storage = _storages({"san": {"type": "iscsi"}, "nfs": {"type": "nfs", "path": "/mnt/pve/nfs"}})

    def permissions(self, path):
        return {"VM.Audit"} if self.no_snapshot_right else {"VM.Audit", "VM.Snapshot"}


def _auto(api, ssh, guests=None, incremental=False):
    """incremental=False: the choice between Proxmox, storage and QEMU snapshots alone."""
    from antigrafa.platforms.proxmox import GuestSettings

    settings = ProxmoxSettings(url="https://pve", token_id="a@pve!b", remote_hash=False, snapshot_mode="auto",
                               incremental=incremental,
                               guests={g: GuestSettings(m) for g, m in (guests or {}).items()})
    return ProxmoxPlatform("c1", settings, api=api, ssh_for_host=lambda node: ssh)


ZFS_ONLY = {"agent": "1", "scsi0": "local-zfs:vm-650-disk-0"}


@pytest.mark.parametrize(
    ("config", "no_right", "method"),
    [
        (ZFS_ONLY, False, "proxmox"),  # Proxmox can do it: its own snapshot
        (ZFS_ONLY, True, "storage"),  # the token may not ask Proxmox
        ({**ZFS_ONLY, "virtio2": "san:0.0.8.scsi-36e8,backup=0"}, False, "storage"),  # VM 650: iSCSI disk backup=0
        ({**ZFS_ONLY, "scsi1": "thick:vm-650-disk-2"}, False, "qemu"),  # a disk to back up without snapshots
        ({"agent": "1", "scsi0": "nfs:650/vm-650-disk-0.qcow2"}, False, "qemu"),
    ],
)
def test_auto_picks_the_best_way_for_each_vm(config, no_right, method, caplog):
    import logging

    caplog.set_level(logging.INFO)
    api, ssh = AutoAPI(config, no_snapshot_right=no_right), _qemu_ssh()
    platform = _auto(api, ssh)
    platform.plan_backup(RUNNING_VM, SNAP)
    assert f"captured by {method}" in caplog.text
    ssh.commands.clear()
    platform.create_snapshot(RUNNING_VM, SNAP)
    platform.delete_snapshot(RUNNING_VM, SNAP)
    steps = _steps(ssh.commands)
    assert (api.api_calls == [("create", SNAP), ("delete", SNAP)]) is (method == "proxmox")
    assert ("qmp start" in steps) is (method == "qemu")
    assert any(c.startswith("zfs snapshot") for c in steps) is (method == "storage")


def test_auto_explains_why_it_did_not_use_proxmox(caplog):
    import logging

    caplog.set_level(logging.INFO)
    api = AutoAPI({**ZFS_ONLY, "virtio2": "san:0.0.8.scsi-36e8,backup=0"})
    _auto(api, _qemu_ssh()).plan_backup(RUNNING_VM, SNAP)
    assert "captured by storage (Proxmox cannot snapshot virtio2 (san:0.0.8.scsi-36e8): storage type iscsi" in caplog.text


def test_auto_cannot_back_up_a_container_without_storage_snapshots():
    from antigrafa.platforms.proxmox import UnsupportedStorage

    api = AutoAPI({"rootfs": "thick:vm-210-disk-0,size=8G"})
    ct = Guest("210", "ct", "dns", "pve1", "running")
    with pytest.raises(UnsupportedStorage, match="containers need a storage with snapshots"):
        _auto(api, _qemu_ssh()).plan_backup(ct, SNAP)


def test_each_guest_can_have_its_own_snapshot_mode():
    api, ssh = AutoAPI(ZFS_ONLY), _qemu_ssh()
    platform = _auto(api, ssh, guests={"650": "qemu"})  # auto for the others
    other = Guest("651", "vm", "other", "pve1", "running")
    platform.plan_backup(RUNNING_VM, SNAP)
    platform.plan_backup(other, SNAP)
    ssh.commands.clear()
    platform.create_snapshot(RUNNING_VM, SNAP)
    platform.create_snapshot(other, SNAP)
    assert "qmp start" in _steps(ssh.commands)  # 650 through QEMU
    assert api.api_calls == [("create", SNAP)]  # 651: auto picked a Proxmox snapshot


def test_rights_follow_the_mode_of_each_guest():
    from antigrafa.platforms.proxmox import GuestSettings

    api = PermAPI(CONFIGS, denied={"/vms/100": {"VM.Snapshot"}})
    settings = ProxmoxSettings(url="https://pve", token_id="a@pve!b", guests={"100": GuestSettings("auto")})
    ProxmoxPlatform("c1", settings, api=api).check_access([VM])  # auto: VM.Snapshot not required
    settings = ProxmoxSettings(url="https://pve", token_id="a@pve!b", snapshot_mode="auto",
                               guests={"100": GuestSettings("proxmox")})
    with pytest.raises(AccessDenied, match="VM.Snapshot on /vms/100"):
        ProxmoxPlatform("c1", settings, api=api).check_access([VM])


def test_qemu_mode_settings_and_rights():
    with pytest.raises(ValueError, match="fleecing_dir must be an absolute path"):
        ProxmoxSettings(url="https://pve", token_id="a@pve!b", fleecing_dir="tmp")
    with pytest.raises(ValueError, match="snapshot_mode must be one of proxmox, storage, qemu"):
        ProxmoxSettings(url="https://pve", token_id="a@pve!b", snapshot_mode="stop")
    api = PermAPI(CONFIGS, denied={"/vms/100": {"VM.Snapshot"}})
    settings = ProxmoxSettings(url="https://pve", token_id="a@pve!b", snapshot_mode="qemu")
    ProxmoxPlatform("c1", settings, api=api).check_access([VM])  # no VM.Snapshot: fine


# --------------------------------------------------------------------------- incremental: ZFS base snapshots

PREVIOUS = {
    "snapshot": "antigrafa_A",
    "disks": [
        {"key": "scsi0", "volume": "local-zfs:vm-650-disk-0", "format": "raw", "size": 1024,
         "source_snapshot": {"name": "antigrafa_A", "guid": "42"}},
        {"key": "scsi1", "volume": "local-lvm:vm-650-disk-1", "format": "raw", "size": 8},
    ],
}
BASE_GUID = {"zfs get -Hp -o value guid rpool/data/vm-650-disk-0@antigrafa_A": "42\n"}


def test_auto_with_incremental_takes_storage_snapshots_of_zvols(caplog):
    import logging

    caplog.set_level(logging.INFO)
    plan = _auto(AutoAPI(ZFS_ONLY), _qemu_ssh(), incremental=True).plan_backup(RUNNING_VM, SNAP)
    assert "captured by storage (incremental: the snapshot of scsi0 stays as the next base)" in caplog.text
    assert plan.disks[0].keeps_base


def test_auto_with_incremental_and_no_zvol_keeps_the_usual_choice(caplog):
    import logging

    caplog.set_level(logging.INFO)
    api = AutoAPI({"agent": "1", "scsi0": "local-lvm:vm-650-disk-1"})
    _auto(api, _qemu_ssh(), incremental=True).plan_backup(RUNNING_VM, SNAP)
    assert "captured by proxmox" in caplog.text


def test_storage_mode_keeps_zvol_snapshots_as_bases_and_drops_the_previous_one():
    ssh = NodeSSH(replies=BASE_GUID)
    platform = _platform(ModeAPI(), ssh)
    zvol, thin = platform.plan_backup(RUNNING_VM, SNAP, previous=PREVIOUS).disks
    assert zvol.keeps_base and zvol.base == ("antigrafa_A", "42") and zvol.base_size == 1024
    assert not getattr(thin, "keeps_base", False)  # LVM-thin: no incremental reads

    assert platform.release_snapshot(RUNNING_VM, SNAP) is True
    assert not any(f"zfs destroy rpool/data/vm-650-disk-0@{SNAP}" in c for c in ssh.commands)  # kept
    assert any(f"lvremove -f pve/snap_vm-650-disk-1_{SNAP}" in c for c in ssh.commands)  # deleted

    platform.drop_previous_bases(RUNNING_VM, SNAP)
    assert "zfs destroy rpool/data/vm-650-disk-0@antigrafa_A" in ssh.commands
    platform.delete_snapshot(RUNNING_VM, SNAP)  # the backup failed after all: the new base goes too
    assert any(f"zfs destroy rpool/data/vm-650-disk-0@{SNAP}" in c for c in ssh.commands)


def test_base_not_reused_for_another_volume_or_without_incremental():
    moved = {"disks": [{**PREVIOUS["disks"][0], "volume": "other-zfs:vm-650-disk-0"}]}
    platform = _platform(ModeAPI(), NodeSSH())
    assert platform.plan_backup(RUNNING_VM, SNAP, previous=moved).disks[0].base is None

    settings = ProxmoxSettings(url="https://pve", token_id="a@pve!b", snapshot_mode="storage", incremental=False)
    ssh = NodeSSH()
    plain = ProxmoxPlatform("c1", settings, api=ModeAPI(), ssh_for_host=lambda node: ssh)
    zvol = plain.plan_backup(RUNNING_VM, SNAP, previous=PREVIOUS).disks[0]
    assert not zvol.keeps_base and zvol.base == ("antigrafa_A", "42")  # still read since it, and dropped
    assert plain.release_snapshot(RUNNING_VM, SNAP) is False
    assert any(f"zfs destroy rpool/data/vm-650-disk-0@{SNAP}" in c for c in ssh.commands)


def test_proxmox_snapshots_are_never_kept():
    api = ModeAPI({"agent": "1", "scsi0": "local-zfs:vm-650-disk-0"})
    platform = _platform(api, NodeSSH(), mode="proxmox")
    plan = platform.plan_backup(RUNNING_VM, SNAP)
    assert not plan.disks[0].keeps_base
    assert platform.release_snapshot(RUNNING_VM, SNAP) is False
    assert api.api_calls == [("delete", SNAP)]


def test_kept_bases_are_not_reported_as_leftovers():
    ssh = NodeSSH(replies={
        "zfs list -H -o name -t snapshot -d 1": "rpool/data/vm-650-disk-0@antigrafa_A\nrpool/data/vm-650-disk-0@antigrafa_C\n",
    })
    platform = _platform(ModeAPI(), ssh)
    platform.plan_backup(RUNNING_VM, SNAP, previous=PREVIOUS)
    assert platform.leftover_snapshots(RUNNING_VM, "antigrafa_") == ["antigrafa_C"]


def test_previous_base_deletion_reports_failures():
    ssh = NodeSSH(replies=BASE_GUID, fail={"zfs destroy": 1})
    platform = _platform(ModeAPI(), ssh)
    platform.plan_backup(RUNNING_VM, SNAP, previous=PREVIOUS)
    with pytest.raises(PlatformError, match="vm-650-disk-0@antigrafa_A: .*refused"):
        platform.drop_previous_bases(RUNNING_VM, SNAP)


def test_incremental_must_be_true_or_false():
    from antigrafa.config import ConfigError
    from antigrafa.platforms.proxmox import parse_settings

    with pytest.raises(ConfigError, match="incremental must be true or false"):
        parse_settings("c1", {"url": "https://pve", "token_id": "a@pve!b", "incremental": "yes"})
