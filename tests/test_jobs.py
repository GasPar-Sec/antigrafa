import io
import os

import pytest

from antigrafa.config import Tuning
from antigrafa.index import read_index, read_manifest
from antigrafa.jobs import PHASES, BackupRunner
from antigrafa.pipeline import restore_stream
from antigrafa.platforms.base import BackupPlan, DiskSource, Guest, Platform
from antigrafa.platforms.proxmox import ProxmoxPlatform, ProxmoxSettings
from antigrafa.targets import Repository, TargetConfig

DISK_SIZE = 3 * 1024 * 1024 + 4096


class FakeStream:
    def __init__(self, data, fail=False):
        self.stdout = io.BytesIO(data)
        self.fail = fail

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        if self.fail and exc[0] is None:
            raise RuntimeError("dd: read error")


class FakeSSH:
    def __init__(self, disks, fail_stream=False):
        self.disks = disks
        self.fail_stream = fail_stream
        self.commands = []

    def run(self, cmd, timeout=120):
        self.commands.append(cmd)
        if cmd.startswith("zfs get -H -o value,source snapdev"):
            return "hidden\tdefault\n"
        if cmd.startswith("zfs get -Hp -o value volsize"):
            name = cmd.rsplit("/", 1)[1]
            return f"{len(self.disks[name])}\n"
        return ""

    def stream(self, cmd):
        name = cmd.split("/dev/zvol/rpool/data/")[1].split("@")[0]
        return FakeStream(self.disks[name], fail=self.fail_stream)


class FakeAPI:
    def __init__(self, guests, configs):
        self._guests = guests
        self.configs = configs
        self.snapshots = {}  # vmid -> set
        self.log = []

    def guests(self):
        return self._guests

    def guest_config(self, g):
        return self.configs[g.id]

    def storage(self, sid):
        return {"type": "zfspool", "pool": "rpool/data"}

    def list_snapshots(self, g):
        return [{"name": n} for n in self.snapshots.get(g.id, ())]

    def create_snapshot(self, g, name, description=""):
        self.log.append(("create", g.id, name))
        self.snapshots.setdefault(g.id, set()).add(name)

    def delete_snapshot(self, g, name):
        self.log.append(("delete", g.id, name))
        self.snapshots[g.id].remove(name)

    def node_iowait(self, node):
        return 0.0


@pytest.fixture
def repo(tmp_path):
    r = Repository(TargetConfig(name="t", path=str(tmp_path)))
    r.open()
    return r


def _setup():
    common = os.urandom(1024 * 1024)
    disks = {
        "vm-100-disk-0": common + os.urandom(DISK_SIZE - len(common)),
        "vm-101-disk-0": common + bytes(DISK_SIZE - len(common)),  # shares a chunk with 100
    }
    guests = [Guest("100", "vm", "web", "pve1", "running"), Guest("101", "vm", "db", "pve2", "running")]
    configs = {
        "100": {"scsi0": "local-zfs:vm-100-disk-0,size=4M"},
        "101": {"virtio0": "local-zfs:vm-101-disk-0,size=4M"},
    }
    return disks, guests, configs


def _proxmox(api, ssh_for_host):
    settings = ProxmoxSettings(url="https://pve1:8006", token_id="antigrafa@pve!backup")
    return ProxmoxPlatform("cluster1", settings, api=api, ssh_for_host=ssh_for_host)


def test_backup_several_guests_end_to_end(repo, tmp_path):
    disks, guests, configs = _setup()
    api = FakeAPI(guests, configs)
    ssh = FakeSSH(disks)
    platform = _proxmox(api, lambda node: ssh)
    runner = BackupRunner(Tuning(chunk_size_mib=1, max_parallel_guests=2), platform, repo)
    try:
        results = runner.run(guests)
    finally:
        runner.close()

    assert all(r.ok for r in results), [r.error for r in results]
    # Snapshots created then deleted, snapdev restored.
    assert all(not s for s in api.snapshots.values())
    assert sum(1 for c in ssh.commands if c.startswith("zfs inherit snapdev")) == 2

    snaps = repo.list_snapshots()
    assert [m["guest"]["id"] for _, m in snaps] == ["100", "101"]
    assert all(path.startswith("cluster1") for path, _ in snaps)
    assert snaps[0][1]["platform"] == "proxmox" and snaps[0][1]["source"] == "cluster1"
    assert snaps[0][1]["disks"][0]["volume"] == "local-zfs:vm-100-disk-0"
    # The shared chunk is stored only once.
    assert results[0].disks[0].stats.new_chunks + results[1].disks[0].stats.new_chunks < 8

    for (path, m), name in zip(snaps, ["vm-100-disk-0", "vm-101-disk-0"]):
        d = m["disks"][0]
        size, _, digests = read_index(repo.resolve(path) / d["index"])
        out = tmp_path / f"{name}.raw"
        with open(out, "wb") as f:
            restore_stream(repo.store, digests, f, size)
        assert out.read_bytes() == disks[name]


def test_failed_read_still_removes_snapshot_and_partial_backup(repo):
    disks, guests, configs = _setup()
    api = FakeAPI(guests[:1], configs)
    platform = _proxmox(api, lambda node: FakeSSH(disks, fail_stream=True))
    runner = BackupRunner(Tuning(chunk_size_mib=1), platform, repo)
    try:
        [result] = runner.run(guests[:1])
    finally:
        runner.close()
    assert not result.ok and "dd" in result.error
    assert api.snapshots["100"] == set()
    assert repo.list_snapshots() == []
    assert not any((repo.root / "cluster1" / "vm" / "100").iterdir())


def test_unsupported_storage_fails_before_snapshot(repo):
    disks, guests, configs = _setup()
    api = FakeAPI(guests[:1], {"100": {"scsi0": "nfs:100/vm-100-disk-0.qcow2,size=4M"}})
    api.storage = lambda sid: {"type": "nfs", "path": "/mnt/pve/nfs"}
    runner = BackupRunner(Tuning(), _proxmox(api, lambda node: FakeSSH(disks)), repo)
    try:
        [result] = runner.run(guests[:1])
    finally:
        runner.close()
    assert not result.ok and "qcow2" in result.error
    assert api.log == []  # no snapshot attempted


# --------------------------------------------------------------------------- any platform
# The runner only talks to the Platform interface: a platform that is not Proxmox,
# with string ids and no load measure, must work the same way.


class MemoryDisk(DiskSource):
    def __init__(self, key, data, size_known=True):
        self.key = key
        self.volume = f"memory:{key}"
        self.data = data
        self.size_known = size_known
        self.events = []

    def prepare(self):
        self.events.append("prepare")

    def size(self):
        return len(self.data) if self.size_known else None

    def open_stream(self):
        return FakeStream(self.data)

    def cleanup(self):
        self.events.append("cleanup")


class MemoryPlatform(Platform):
    type = "memory"

    def __init__(self, guests, disks, leftovers=()):
        super().__init__("lab")
        self._guests = guests
        self.disks = disks  # guest id -> [MemoryDisk]
        self.leftovers = list(leftovers)
        self.snapshots = set()
        self.log = []

    def guests(self):
        return self._guests

    def plan_backup(self, guest, snapshot, low_priority=True):
        self.log.append(("plan", guest.id, low_priority))
        return BackupPlan(config={"cpus": 2}, disks=self.disks[guest.id])

    def create_snapshot(self, guest, snapshot):
        self.snapshots.add((guest.id, snapshot))

    def delete_snapshot(self, guest, snapshot):
        self.snapshots.remove((guest.id, snapshot))

    def leftover_snapshots(self, guest, prefix):
        return [s for s in self.leftovers if s.startswith(prefix)]


def _run(platform, repo, guests, **tuning):
    runner = BackupRunner(Tuning(chunk_size_mib=1, **tuning), platform, repo)
    try:
        return runner.run(guests)
    finally:
        runner.close()


def test_any_platform_end_to_end(repo, tmp_path, caplog):
    disk = MemoryDisk("disk0", os.urandom(DISK_SIZE))
    tree = MemoryDisk("root", os.urandom(5000), size_known=False)
    tree.format = "tar"
    guests = [Guest("vm-42", "vm", "web", "esx1"), Guest("ct-7", "ct", "dns", "esx2")]
    platform = MemoryPlatform(guests, {"vm-42": [disk], "ct-7": [tree]}, leftovers=["antigrafa_old", "manual"])

    results = _run(platform, repo, guests, low_priority=False)

    assert all(r.ok for r in results), [r.error for r in results]
    assert platform.snapshots == set()  # every snapshot deleted
    assert ("plan", "vm-42", False) in platform.log  # low_priority passed through
    assert disk.events == ["prepare", "cleanup"]
    assert "antigrafa_old" in caplog.text and "manual" not in caplog.text

    snaps = dict(repo.list_snapshots())
    vm_path = next(p for p in snaps if "vm-42" in p)
    assert vm_path.split(os.sep)[:3] == ["lab", "vm", "vm-42"]
    m = snaps[vm_path]
    assert m["format_version"] == 2
    assert m["platform"] == "memory" and m["source"] == "lab"
    assert m["guest"] == {"id": "vm-42", "kind": "vm", "name": "web", "host": "esx1"}
    assert m["config"] == {"cpus": 2}
    assert m["disks"][0]["volume"] == "memory:disk0"
    assert m["snapshot"].startswith("antigrafa_")

    ct_path = next(p for p in snaps if "ct-7" in p)
    [ct_disk] = snaps[ct_path]["disks"]
    assert ct_disk["index"] == "root.tar.fidx" and ct_disk["size"] == 5000
    size, _, digests = read_index(repo.resolve(ct_path) / ct_disk["index"])
    out = tmp_path / "root.tar"
    with open(out, "wb") as f:
        restore_stream(repo.store, digests, f, size)
    assert out.read_bytes() == tree.data


def test_guest_without_disk_fails_before_snapshot(repo):
    guest = Guest("1", "vm", "empty", "h1")
    platform = MemoryPlatform([guest], {"1": []})
    [result] = _run(platform, repo, [guest])
    assert not result.ok and "no disk" in result.error
    assert platform.snapshots == set()
    assert repo.list_snapshots() == []


def test_short_read_fails_and_keeps_nothing(repo):
    class Lying(MemoryDisk):
        def size(self):
            return len(self.data) + 1  # announces one byte more than it streams

    guest = Guest("1", "vm", "web", "h1")
    platform = MemoryPlatform([guest], {"1": [Lying("disk0", b"x" * 1000)]})
    [result] = _run(platform, repo, [guest])
    assert not result.ok and "read 1000 bytes instead of 1001" in result.error
    assert platform.snapshots == set()
    assert repo.list_snapshots() == []


def test_failed_cleanup_is_logged_not_fatal(repo, caplog):
    class Sticky(MemoryDisk):
        def cleanup(self):
            raise RuntimeError("device busy")

    guest = Guest("1", "vm", "web", "h1")
    platform = MemoryPlatform([guest], {"1": [Sticky("disk0", b"y" * 1000)]})
    [result] = _run(platform, repo, [guest])
    assert result.ok
    assert "cleanup failed: device busy" in caplog.text


def test_failed_snapshot_deletion_fails_the_backup(repo, caplog):
    guest = Guest("1", "vm", "web", "h1")

    class Stuck(MemoryPlatform):
        def delete_snapshot(self, guest, snapshot):
            raise RuntimeError("snapshot locked")

    platform = Stuck([guest], {"1": [MemoryDisk("disk0", b"z" * 1000)]})
    [result] = _run(platform, repo, [guest])
    assert not result.ok and "snapshot locked" in result.error
    assert "remove it by hand" in caplog.text
    assert repo.list_snapshots() == []


def test_leftover_listing_errors_are_ignored(repo):
    guest = Guest("1", "vm", "web", "h1")

    class Flaky(MemoryPlatform):
        def leftover_snapshots(self, guest, prefix):
            raise RuntimeError("API timeout")

    platform = Flaky([guest], {"1": [MemoryDisk("disk0", b"w" * 1000)]})
    [result] = _run(platform, repo, [guest])
    assert result.ok


def test_host_load_drives_the_throttle(repo):
    class Busy(MemoryPlatform):
        def host_iowait(self, host):
            return 0.5

    guest = Guest("1", "vm", "web", "h1")
    runner = BackupRunner(Tuning(), Busy([guest], {}), repo)
    quiet = BackupRunner(Tuning(), MemoryPlatform([guest], {}), repo)
    try:
        assert runner._iowait("h1") == 0.5
        assert quiet._iowait("h1") == 0.0  # no measure: never throttled by load
        assert runner._throttle("h1") is runner._throttle("h1")  # one throttle per host
        assert runner._host_slot("h1") is not runner._host_slot("h2")
    finally:
        runner.close()
        quiet.close()


def test_backup_progress_and_debug_details(repo, caplog):
    import io as _io
    import logging

    from antigrafa.progress import Progress

    caplog.set_level(logging.DEBUG)
    a = MemoryDisk("disk0", os.urandom(DISK_SIZE))
    b = MemoryDisk("root", os.urandom(3000), size_known=False)
    guests = [Guest("1", "vm", "web", "h1"), Guest("2", "ct", "dns", "h2")]
    platform = MemoryPlatform(guests, {"1": [a], "2": [b]})
    progress = Progress(label="backup", enabled=True, stream=_io.StringIO(), interval=0)
    runner = BackupRunner(Tuning(chunk_size_mib=1), platform, repo, progress=progress)
    try:
        results = runner.run(guests)
    finally:
        runner.close()
    assert all(r.ok for r in results)
    # Every byte read is counted; only the disk of known size is in the total
    assert progress.done == DISK_SIZE + 3000
    assert progress.total == progress.done_known == DISK_SIZE
    assert progress.unknown_open == 0
    assert progress.status == "2/2 guests"
    assert "0/2 guests" in progress.stream.getvalue()
    # -v details
    assert "disk0: memory:disk0, read by MemoryDisk (raw)" in caplog.text
    assert "root: size unknown (archive)" in caplog.text
    assert f"disk0: size {DISK_SIZE} bytes" in caplog.text
    assert "destination flushed in" in caplog.text


def test_progress_lists_the_disk_being_read_with_the_guest_id(repo):
    import io as _io

    from antigrafa.progress import Progress

    seen = []

    class Watched(MemoryDisk):
        def open_stream(self):
            seen.append([t.name for t in progress.running])  # while this disk is read
            return super().open_stream()

    guest = Guest("vm-42", "vm", "web", "esx1")
    platform = MemoryPlatform([guest], {"vm-42": [Watched("disk0", b"a" * 5000), Watched("disk1", b"b" * 3000)]})
    progress = Progress(label="backup", enabled=True, stream=_io.StringIO(), interval=0)
    runner = BackupRunner(Tuning(chunk_size_mib=1), platform, repo, progress=progress)
    try:
        [result] = runner.run([guest])
    finally:
        runner.close()
    assert result.ok
    assert seen == [["vm-42/disk0"], ["vm-42/disk1"]]  # one disk after the other
    assert progress.running == []  # all finished
    assert "vm-42/disk0 100%" in progress.stream.getvalue()


def test_every_phase_is_timed(repo):
    guest = Guest("1", "vm", "web", "h1")
    platform = MemoryPlatform([guest], {"1": [MemoryDisk("disk0", b"a" * 5000), MemoryDisk("disk1", b"b" * 3000)]})
    [result] = _run(platform, repo, [guest])
    assert result.ok
    assert list(result.timings) == list(PHASES)  # in the order they ran
    assert all(t >= 0 for t in result.timings.values())
    assert sum(result.timings.values()) <= result.duration
    assert [d.key for d in result.disks] == ["disk0", "disk1"]


def test_failed_backup_keeps_the_timings_of_the_phases_reached(repo):
    guest = Guest("1", "vm", "web", "h1")

    class Broken(MemoryDisk):
        def open_stream(self):
            raise RuntimeError("dd: read error")

    platform = MemoryPlatform([guest], {"1": [Broken("disk0", b"x" * 1000)]})
    [result] = _run(platform, repo, [guest])
    assert not result.ok
    # Transfer failed, the snapshot was still removed, nothing flushed
    assert list(result.timings) == ["wait", "prepare", "snapshot", "transfer", "cleanup"]

    empty = MemoryPlatform([guest], {"1": []})
    [result] = _run(empty, repo, [guest])
    assert list(result.timings) == ["wait", "prepare"]  # failed before any snapshot


def test_waiting_for_the_host_is_timed_and_the_slot_released(repo):
    import threading

    release = threading.Event()

    class Slow(MemoryDisk):
        def open_stream(self):
            release.wait(5)
            return super().open_stream()

    guests = [Guest("1", "vm", "a", "h1"), Guest("2", "vm", "b", "h1")]  # same host
    platform = MemoryPlatform(guests, {"1": [Slow("disk0", b"a" * 100)], "2": [Slow("disk0", b"b" * 100)]})
    timer = threading.Timer(0.3, release.set)
    timer.start()
    results = _run(platform, repo, guests, max_parallel_guests=2, max_parallel_per_host=1)
    timer.join()
    assert all(r.ok for r in results)
    # One guest per host at a time: the second waited for the first
    assert max(r.timings["wait"] for r in results) >= 0.2


def test_archive_estimate_drives_the_progress(repo):
    import io as _io

    from antigrafa.progress import Progress

    class Archive(MemoryDisk):
        format = "tar"

        def estimated_size(self):
            return 4000  # the real archive is 5000 bytes

    seen = []

    class Watched(Archive):
        def open_stream(self):
            [task] = progress.running
            seen.append((task.total, task.approximate))
            return super().open_stream()

    guest = Guest("150", "ct", "dns", "pve1")
    platform = MemoryPlatform([guest], {"150": [Watched("rootfs", b"t" * 5000, size_known=False)]})
    progress = Progress(label="backup", enabled=True, stream=_io.StringIO(), interval=0)
    runner = BackupRunner(Tuning(chunk_size_mib=1), platform, repo, progress=progress)
    try:
        [result] = runner.run([guest])
    finally:
        runner.close()
    assert result.ok
    assert seen == [(4000, True)]  # estimated while reading
    assert progress.total == progress.done == 5000  # corrected to the real size at the end
    assert "150/rootfs ~99%" in progress.stream.getvalue()
    # The estimate never replaces the exact size check: the manifest has the real size
    [(path, manifest)] = repo.list_snapshots()
    assert manifest["disks"][0]["size"] == 5000
