import pytest

from antigrafa import platforms
from antigrafa.platforms.base import AccessDenied, BackupPlan, DiskSource, Guest, Platform, check_name
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

    def plan_backup(self, guest, snapshot, low_priority=True):
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

        def plan_backup(self, guest, snapshot, low_priority=True):
            return BackupPlan(config={}, disks=[])

        def create_snapshot(self, guest, snapshot):
            pass

        def delete_snapshot(self, guest, snapshot):
            pass

    assert Bare("x").disk_sizes(Guest("1", "vm", "a", "h")) == {}
