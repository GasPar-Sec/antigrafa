import io
import os

import pytest

from antigrafa.config import Tuning
from antigrafa.index import read_index, read_manifest
from antigrafa.jobs import BackupRunner
from antigrafa.pipeline import restore_stream
from antigrafa.proxmox import Guest
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
        return self.configs[g.vmid]

    def storage(self, sid):
        return {"type": "zfspool", "pool": "rpool/data"}

    def list_snapshots(self, g):
        return [{"name": n} for n in self.snapshots.get(g.vmid, ())]

    def create_snapshot(self, g, name, description=""):
        self.log.append(("create", g.vmid, name))
        self.snapshots.setdefault(g.vmid, set()).add(name)

    def delete_snapshot(self, g, name):
        self.log.append(("delete", g.vmid, name))
        self.snapshots[g.vmid].remove(name)

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
    guests = [Guest(100, "pve1", "qemu", "web", "running"), Guest(101, "pve2", "qemu", "db", "running")]
    configs = {
        100: {"scsi0": "local-zfs:vm-100-disk-0,size=4M"},
        101: {"virtio0": "local-zfs:vm-101-disk-0,size=4M"},
    }
    return disks, guests, configs


def test_backup_several_guests_end_to_end(repo, tmp_path):
    disks, guests, configs = _setup()
    api = FakeAPI(guests, configs)
    ssh = FakeSSH(disks)
    runner = BackupRunner(Tuning(chunk_size_mib=1, max_parallel_guests=2), api, repo, lambda node: ssh)
    try:
        results = runner.run(guests)
    finally:
        runner.close()

    assert all(r.ok for r in results), [r.error for r in results]
    # Snapshots created then deleted, snapdev restored.
    assert all(not s for s in api.snapshots.values())
    assert sum(1 for c in ssh.commands if c.startswith("zfs inherit snapdev")) == 2

    snaps = repo.list_snapshots()
    assert [m["vmid"] for _, m in snaps] == [100, 101]
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
    runner = BackupRunner(Tuning(chunk_size_mib=1), api, repo, lambda node: FakeSSH(disks, fail_stream=True))
    try:
        [result] = runner.run(guests[:1])
    finally:
        runner.close()
    assert not result.ok and "dd" in result.error
    assert api.snapshots[100] == set()
    assert repo.list_snapshots() == []
    assert not any((repo.root / "vm" / "100").iterdir())


def test_unsupported_storage_fails_before_snapshot(repo):
    disks, guests, configs = _setup()
    api = FakeAPI(guests[:1], {100: {"scsi0": "nfs:100/vm-100-disk-0.qcow2,size=4M"}})
    api.storage = lambda sid: {"type": "nfs", "path": "/mnt/pve/nfs"}
    runner = BackupRunner(Tuning(), api, repo, lambda node: FakeSSH(disks))
    try:
        [result] = runner.run(guests[:1])
    finally:
        runner.close()
    assert not result.ok and "qcow2" in result.error
    assert api.log == []  # no snapshot attempted
