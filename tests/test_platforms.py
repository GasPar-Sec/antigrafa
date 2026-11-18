import pytest

from antigrafa import platforms
from antigrafa.platforms.base import BackupPlan, DiskSource, Guest, Platform, check_name
from antigrafa.platforms.proxmox import ProxmoxPlatform, ProxmoxSettings


def test_registry_builds_proxmox():
    settings = platforms.parse_settings("proxmox", "c1", {"url": "https://pve:8006", "token_id": "a@pve!b", "token_secret": "s"})
    platform = platforms.create_platform("proxmox", "c1", settings)
    assert isinstance(platform, ProxmoxPlatform)
    assert (platform.type, platform.name) == ("proxmox", "c1")
    assert platform.api.base == "https://pve:8006/api2/json"


def test_registry_rejects_unknown_type():
    with pytest.raises(ValueError, match="unknown platform type 'hyperv' \(supported: proxmox\)"):
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


def test_proxmox_platform_passes_verify_tls_to_the_api(caplog):
    import ssl

    insecure = ProxmoxSettings(url="https://pve", token_id="a@pve!b", token_secret="s", verify_tls=False)
    assert ProxmoxPlatform("lab", insecure).api._ctx.verify_mode == ssl.CERT_NONE
    secure = ProxmoxSettings(url="https://pve", token_id="a@pve!b", token_secret="s")
    assert ProxmoxPlatform("prod", secure).api._ctx.verify_mode == ssl.CERT_REQUIRED
