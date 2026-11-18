import errno
import os
from datetime import datetime, timezone

import pytest

from antigrafa import targets
from antigrafa.index import write_manifest
from antigrafa.targets import (
    Repository,
    TargetConfig,
    TargetUnavailable,
    filesystem_sync_supported,
    mount_fstype,
    syncfs,
)

WHEN = datetime(2026, 11, 17, 22, 30, tzinfo=timezone.utc)

MOUNTS = """\
sysfs /sys sysfs rw 0 0
/dev/sda1 / ext4 rw 0 0
nas:/export /mnt/nas nfs4 rw 0 0
/dev/sdb1 /mnt/nas/local\\040disk xfs rw 0 0
broken-line
"""


@pytest.fixture
def mounts(tmp_path):
    path = tmp_path / "mounts"
    path.write_text(MOUNTS)
    return str(path)


@pytest.fixture
def repo(tmp_path):
    r = Repository(TargetConfig(name="t", path=str(tmp_path / "repo")))
    r.root.mkdir()
    r.open()
    return r


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [({"kind": "smb"}, "unknown kind 'smb'"), ({"sync": "always"}, "unknown sync mode 'always'")],
)
def test_invalid_target_config(kwargs, message):
    with pytest.raises(ValueError, match=message):
        TargetConfig(name="t", path="/x", **kwargs)


def test_mount_fstype_longest_match(mounts, monkeypatch):
    # Paths are compared as given: realpath would turn them into C:\... on Windows
    monkeypatch.setattr(targets.os.path, "realpath", lambda p: p)
    assert mount_fstype("/mnt/nas/antigrafa", mounts) == "nfs4"
    assert mount_fstype("/mnt/nas", mounts) == "nfs4"
    assert mount_fstype("/mnt/nas/local disk/x", mounts) == "xfs"  # \040 = space
    assert mount_fstype("/mnt/nasty", mounts) == "ext4"  # not under /mnt/nas
    assert mount_fstype("/srv", mounts) == "ext4"


def test_open_refuses_missing_directory(tmp_path):
    repo = Repository(TargetConfig(name="usb", path=str(tmp_path / "absent")))
    with pytest.raises(TargetUnavailable, match="usb: .* does not exist"):
        repo.open()


@pytest.mark.parametrize("fstype", ["ext4", None])
def test_open_nfs_refuses_local_disk(tmp_path, monkeypatch, fstype):
    monkeypatch.setattr(targets, "mount_fstype", lambda path: fstype)
    repo = Repository(TargetConfig(name="nas", path=str(tmp_path), kind="nfs"))
    with pytest.raises(TargetUnavailable, match=f"not on an NFS mount \\(type: {fstype}\\)"):
        repo.open()
    assert not (tmp_path / ".chunks").exists()  # nothing written


def test_open_nfs_mounted(tmp_path, monkeypatch):
    monkeypatch.setattr(targets, "mount_fstype", lambda path: "nfs4")
    repo = Repository(TargetConfig(name="nas", path=str(tmp_path), kind="nfs"))
    repo.open()
    assert (tmp_path / ".chunks").is_dir()


def test_snapshot_dirs_and_listing(repo):
    vm = repo.create_snapshot_dir("qemu", 100, WHEN)
    ct = repo.create_snapshot_dir("lxc", 200, WHEN)
    repo.create_snapshot_dir("qemu", 101, WHEN)  # no manifest: incomplete backup
    assert vm == repo.root / "vm" / "100" / "2026-11-17T22-30-00Z"
    with pytest.raises(FileExistsError):
        repo.create_snapshot_dir("qemu", 100, WHEN)

    write_manifest(vm, {"vmid": 100})
    write_manifest(ct, {"vmid": 200})
    assert repo.list_snapshots() == [
        ("vm/100/2026-11-17T22-30-00Z".replace("/", os.sep), {"vmid": 100}),
        ("ct/200/2026-11-17T22-30-00Z".replace("/", os.sep), {"vmid": 200}),
    ]


def test_list_snapshots_empty(repo):
    assert repo.list_snapshots() == []


def test_resolve_stays_inside_repository(repo):
    assert repo.resolve("vm/100/x") == (repo.root / "vm" / "100" / "x").resolve()
    for evil in ("../outside", "vm/../../outside", "."):
        with pytest.raises(ValueError, match="invalid backup path"):
            repo.resolve(evil)


# --------------------------------------------------------------------------- sync


def test_sync_modes(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(targets, "syncfs", calls.append)
    monkeypatch.setattr(targets, "filesystem_sync_supported", lambda: True)

    none = Repository(TargetConfig(name="t", path=str(tmp_path), sync="none"))
    none.sync()
    assert calls == [] and not none.store.sync_each

    fs = Repository(TargetConfig(name="t", path=str(tmp_path)))
    fs.sync()
    assert calls == [fs.root] and not fs.store.sync_each

    per_file = Repository(TargetConfig(name="t", path=str(tmp_path), sync="file"))
    per_file.sync()
    assert calls == [fs.root, per_file.root] and per_file.store.sync_each


def test_without_filesystem_sync_chunks_are_fsynced(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(targets, "syncfs", calls.append)
    monkeypatch.setattr(targets, "filesystem_sync_supported", lambda: False)
    repo = Repository(TargetConfig(name="t", path=str(tmp_path)))
    assert repo.store.sync_each  # durability kept: fsync per chunk instead
    repo.sync()
    assert calls == []


class FakeLibc:
    def __init__(self, result):
        self.result = result
        self.fds = []

    def syncfs(self, fd):
        self.fds.append(fd)
        return self.result


def test_syncfs_uses_libc(tmp_path, monkeypatch):
    target = tmp_path / "f"
    target.write_bytes(b"x")
    libc = FakeLibc(0)
    monkeypatch.setattr(targets, "_libc", lambda: libc)
    syncfs(target)
    assert len(libc.fds) == 1


def test_syncfs_reports_libc_error(tmp_path, monkeypatch):
    target = tmp_path / "f"
    target.write_bytes(b"x")
    monkeypatch.setattr(targets, "_libc", lambda: FakeLibc(-1))
    monkeypatch.setattr(targets.ctypes, "get_errno", lambda: errno.EIO)
    with pytest.raises(OSError) as exc:
        syncfs(target)
    assert exc.value.errno == errno.EIO and exc.value.filename == str(target)


def test_syncfs_falls_back_to_sync(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(targets, "_libc", lambda: object())  # libc without syncfs (macOS)
    monkeypatch.setattr(targets.os, "sync", lambda: calls.append("sync"), raising=False)
    syncfs(tmp_path)
    assert calls == ["sync"]


def test_syncfs_unsupported_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(targets, "_libc", lambda: None)
    monkeypatch.delattr(targets.os, "sync", raising=False)
    assert not filesystem_sync_supported()
    with pytest.raises(OSError) as exc:
        syncfs(tmp_path)
    assert exc.value.errno == errno.ENOSYS


def test_syncfs_real_call(tmp_path):
    (tmp_path / "f").write_bytes(b"x")
    if filesystem_sync_supported():
        syncfs(tmp_path / "f")
    else:
        with pytest.raises(OSError):
            syncfs(tmp_path / "f")


def test_libc_loading(monkeypatch):
    monkeypatch.setattr(targets.os, "name", "nt")
    assert targets._libc() is None

    monkeypatch.setattr(targets.os, "name", "posix")
    monkeypatch.setattr(targets.ctypes.util, "find_library", lambda name: "libc.so.6")
    monkeypatch.setattr(targets.ctypes, "CDLL", lambda name, use_errno: ("loaded", name, use_errno))
    assert targets._libc() == ("loaded", "libc.so.6", True)

    def missing(name, use_errno):
        raise OSError("cannot open shared object file")

    monkeypatch.setattr(targets.ctypes, "CDLL", missing)
    assert targets._libc() is None
