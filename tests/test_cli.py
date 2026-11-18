import logging
import os
from datetime import datetime, timezone

import pytest

from antigrafa import cli, jobs
from antigrafa.chunkstore import encode_chunk
from antigrafa.index import write_index, write_manifest
from antigrafa.pipeline import StreamStats
from antigrafa.proxmox import Guest, ProxmoxError
from antigrafa.targets import Repository, TargetConfig

CHUNK = 64 * 1024
WHEN = datetime(2026, 11, 17, 22, 30, tzinfo=timezone.utc)
SNAP = "vm/100/2026-11-17T22-30-00Z"
GUESTS = [
    Guest(100, "pve1", "qemu", "web", "running"),
    Guest(101, "pve2", "qemu", "db", "running"),
    Guest(200, "pve1", "lxc", "dns", "stopped"),
]


@pytest.fixture(autouse=True)
def _reset_logging():
    # main() configures the root logger: undo it so each test starts clean
    root = logging.getLogger()
    handlers = root.handlers[:]
    yield
    root.handlers[:] = handlers


@pytest.fixture
def repo_dir(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    return path


@pytest.fixture
def config(tmp_path, repo_dir):
    path = tmp_path / "antigrafa.toml"
    path.write_text(
        f"""
[proxmox]
url = "https://pve1:8006"
token_id = "antigrafa@pve!backup"
token_secret = "s3cret"

[nodes.pve1]
host = "10.0.0.11"

[target.local]
path = "{repo_dir.as_posix()}"
"""
    )
    return str(path)


@pytest.fixture
def backup(repo_dir):
    """A complete backup of VM 100: one zero chunk, one random chunk, a short tail."""
    data = bytes(CHUNK) + os.urandom(CHUNK) + b"tail"
    repo = Repository(TargetConfig(name="local", path=str(repo_dir)))
    repo.open()
    digests = [repo.store.insert(data[i : i + CHUNK]) for i in range(0, len(data), CHUNK)]
    snap = repo.create_snapshot_dir("qemu", 100, WHEN)
    write_index(snap / "scsi0.img.fidx", len(data), CHUNK, digests)
    write_manifest(
        snap,
        {
            "name": "web",
            "disks": [{"key": "scsi0", "format": "raw", "size": len(data), "index": "scsi0.img.fidx"}],
        },
    )
    return repo, data, digests


class FakeAPI:
    instances = []

    def __init__(self, url, token_id, token_secret, ca_file=None):
        self.args = (url, token_id, token_secret, ca_file)
        FakeAPI.instances.append(self)

    def guests(self):
        return GUESTS


@pytest.fixture
def fake_api(monkeypatch):
    FakeAPI.instances = []
    monkeypatch.setattr(cli, "ProxmoxAPI", FakeAPI)
    return FakeAPI


class FakeController:
    def __init__(self, raw_bytes):
        self.raw_bytes = raw_bytes

    def stats(self):
        return {"raw_bytes": self.raw_bytes, "stored_bytes": self.raw_bytes // 2, "level": 3, "concurrency": 2}


class FakeRunner:
    """Replaces BackupRunner: records the guests and the SSH hosts it would use."""

    last = None
    fail = set()
    raw_bytes = 3 * 1024 * 1024

    def __init__(self, tuning, api, repo, ssh_for_node):
        self.ssh_for_node = ssh_for_node
        self.controller = FakeController(FakeRunner.raw_bytes)
        self.closed = False
        FakeRunner.last = self

    def run(self, guests):
        self.guests = guests
        return [
            jobs.GuestResult(
                guest=g,
                ok=g.vmid not in FakeRunner.fail,
                error="dd: read error" if g.vmid in FakeRunner.fail else "",
                snapshot=f"vm/{g.vmid}/x",
                duration=12.0,
            )
            for g in guests
        ]

    def close(self):
        self.closed = True


@pytest.fixture
def fake_runner(monkeypatch, fake_api):
    FakeRunner.last = None
    FakeRunner.fail = set()
    FakeRunner.raw_bytes = 3 * 1024 * 1024
    monkeypatch.setattr(jobs, "BackupRunner", FakeRunner)
    return FakeRunner


# --------------------------------------------------------------------------- helpers


@pytest.mark.parametrize(
    ("size", "text"),
    [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KiB"), (5 * 1024**2, "5.0 MiB"), (3 * 1024**5, "3072.0 TiB")],
)
def test_human_sizes(size, text):
    assert cli._human(size) == text


# --------------------------------------------------------------------------- errors


def test_missing_config_file(tmp_path, caplog):
    assert cli.main(["-c", str(tmp_path / "nope.toml"), "list"]) == 2
    assert "not found" in caplog.text


def test_unavailable_target(tmp_path, caplog):
    path = tmp_path / "antigrafa.toml"
    path.write_text(f'[target.usb]\npath = "{(tmp_path / "absent").as_posix()}"\n')
    assert cli.main(["-c", str(path), "list"]) == 1
    assert "does not exist" in caplog.text


def test_command_needing_proxmox_without_section(tmp_path, repo_dir, caplog):
    path = tmp_path / "antigrafa.toml"
    path.write_text(f'[target.local]\npath = "{repo_dir.as_posix()}"\n')
    assert cli.main(["-c", str(path), "guests"]) == 2
    assert "[proxmox] section is required" in caplog.text


def test_proxmox_error(config, monkeypatch, caplog):
    def unreachable(*args, **kwargs):
        raise ProxmoxError("GET /cluster/resources: connection refused")

    monkeypatch.setattr(cli, "ProxmoxAPI", unreachable)
    assert cli.main(["-c", config, "guests"]) == 1
    assert "connection refused" in caplog.text


def test_interrupted(config, monkeypatch, caplog):
    def interrupted(cfg, args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_list", interrupted)
    assert cli.main(["-c", config, "list"]) == 130
    assert "interrupted" in caplog.text


def test_verbose_flag_enables_debug(config, monkeypatch):
    seen = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: seen.update(kw))
    assert cli.main(["-c", config, "-v", "list"]) == 0
    assert seen["level"] == logging.DEBUG


# --------------------------------------------------------------------------- guests


def test_guests(config, fake_api, capsys):
    assert cli.main(["-c", config, "guests"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 3
    assert out[0].split() == ["100", "qemu", "pve1", "running", "web"]
    assert fake_api.instances[0].args == ("https://pve1:8006", "antigrafa@pve!backup", "s3cret", None)


# --------------------------------------------------------------------------- backup


def test_backup_all_with_exclusion(config, fake_runner, capsys):
    assert cli.main(["-c", config, "backup", "--all", "--exclude", "101"]) == 0
    runner = fake_runner.last
    assert [g.vmid for g in runner.guests] == [100, 200]
    assert runner.closed
    out = capsys.readouterr().out
    assert "OK" in out and "VM 100 (web)" in out and "vm/100/x" in out
    assert "New data: 3.0 MiB -> 1.5 MiB written (final zstd level 3, 2 parallel writes)" in out


def test_backup_ssh_hosts_come_from_config(config, fake_runner):
    assert cli.main(["-c", config, "backup", "--vmid", "100"]) == 0
    ssh = fake_runner.last.ssh_for_node("pve1")
    assert (ssh.host, ssh.user, ssh.port) == ("10.0.0.11", "root", 22)
    assert fake_runner.last.ssh_for_node("pve2").host == "pve2"


def test_backup_selected_guests_with_failure(config, fake_runner, capsys):
    fake_runner.fail = {101}
    fake_runner.raw_bytes = 0
    assert cli.main(["-c", config, "backup", "--vmid", "101", "100"]) == 1
    assert [g.vmid for g in fake_runner.last.guests] == [100, 101]
    out = capsys.readouterr().out
    assert "FAILED VM 101 (db)" in out and "dd: read error" in out
    assert "New data" not in out  # nothing new written


def test_backup_needs_a_selection(config, fake_runner, caplog):
    assert cli.main(["-c", config, "backup"]) == 2
    assert "use --all or --vmid" in caplog.text
    assert fake_runner.last is None


def test_backup_unknown_guest(config, fake_runner, caplog):
    assert cli.main(["-c", config, "backup", "--vmid", "100", "999", "998"]) == 2
    assert "guests not found: 998, 999" in caplog.text


# --------------------------------------------------------------------------- list / verify


def test_list(config, backup, capsys):
    assert cli.main(["-c", config, "list"]) == 0
    out = capsys.readouterr().out
    assert SNAP.replace("/", os.sep) in out and "web" in out and "1 disk(s)" in out and "128.0 KiB" in out


def test_verify_ok(config, backup, capsys):
    assert cli.main(["-c", config, "verify"]) == 0
    assert capsys.readouterr().out.startswith("OK")


def test_verify_detects_corrupted_chunk(config, backup, capsys, caplog):
    repo, _, digests = backup
    repo.store.path_for(digests[1]).write_bytes(encode_chunk(b"tampered", 3))
    assert cli.main(["-c", config, "verify", SNAP]) == 1
    out = capsys.readouterr().out
    assert out.startswith("ERROR") and "(1 bad chunks)" in out
    assert "digest mismatch" in caplog.text


def test_verify_counts_missing_chunk(config, backup, capsys):
    repo, _, digests = backup
    repo.store.path_for(digests[0]).unlink()
    assert cli.main(["-c", config, "verify", SNAP]) == 1
    assert "(1 bad chunks)" in capsys.readouterr().out


# --------------------------------------------------------------------------- restore


def test_restore(config, backup, tmp_path, capsys):
    _, data, _ = backup
    out = tmp_path / "disk.raw"
    assert cli.main(["-c", config, "restore", SNAP, "scsi0", str(out)]) == 0
    assert out.read_bytes() == data
    assert "restored (raw)" in capsys.readouterr().out


def test_restore_never_overwrites(config, backup, tmp_path, caplog):
    out = tmp_path / "disk.raw"
    out.write_bytes(b"precious")
    assert cli.main(["-c", config, "restore", SNAP, "scsi0", str(out)]) == 1
    assert out.read_bytes() == b"precious"


def test_restore_unknown_disk(config, backup, tmp_path, caplog):
    assert cli.main(["-c", config, "restore", SNAP, "scsi9", str(tmp_path / "x")]) == 2
    assert "no disk scsi9; available: scsi0" in caplog.text


def test_restore_unknown_snapshot(config, backup, tmp_path):
    assert cli.main(["-c", config, "restore", "vm/100/nope", "scsi0", str(tmp_path / "x")]) == 1
