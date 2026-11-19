import logging
import os
from pathlib import Path
from datetime import datetime, timezone

import pytest

from antigrafa import cli, jobs
from antigrafa.chunkstore import encode_chunk
from antigrafa.index import write_index, write_manifest
from antigrafa.pipeline import StreamStats
from antigrafa.platforms import proxmox as pve_platform
from antigrafa.platforms.base import Guest
from antigrafa.platforms.proxmox import ProxmoxError
from antigrafa.targets import Repository, TargetConfig

CHUNK = 64 * 1024
WHEN = datetime(2026, 11, 17, 22, 30, tzinfo=timezone.utc)
SNAP = "cluster1/vm/100/2026-11-17T22-30-00Z"
GUESTS = [
    Guest("100", "vm", "web", "pve1", "running"),
    Guest("101", "vm", "db", "pve2", "running"),
    Guest("200", "ct", "dns", "pve1", "stopped"),
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
[source.cluster1]
type = "proxmox"
url = "https://pve1:8006"
token_id = "antigrafa@pve!backup"
token_secret = "s3cret"

[source.cluster1.nodes.pve1]
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
    snap = repo.create_snapshot_dir("cluster1", "vm", "100", WHEN)
    write_index(snap / "scsi0.img.fidx", len(data), CHUNK, digests)
    write_manifest(
        snap,
        {
            "guest": {"id": "100", "kind": "vm", "name": "web", "host": "pve1"},
            "disks": [{"key": "scsi0", "format": "raw", "size": len(data), "index": "scsi0.img.fidx"}],
        },
    )
    return repo, data, digests


class FakeAPI:
    instances = []

    def __init__(self, url, token_id, token_secret, ca_file=None, verify_tls=True):
        self.args = (url, token_id, token_secret, ca_file, verify_tls)
        FakeAPI.instances.append(self)

    denied = {}  # path -> privileges missing there
    visible = GUESTS

    def guests(self):
        return FakeAPI.visible

    def permissions(self, path):
        return {"VM.Audit", "VM.Snapshot", "Sys.Audit", "Datastore.Audit"} - set(FakeAPI.denied.get(path, ()))

    def guest_config(self, g):
        if g.kind == "ct":
            return {"rootfs": f"local-zfs:subvol-{g.id}-disk-0"}
        return {"scsi0": f"local-zfs:vm-{g.id}-disk-0"}


@pytest.fixture
def fake_api(monkeypatch):
    FakeAPI.instances = []
    FakeAPI.denied = {}
    FakeAPI.visible = GUESTS
    monkeypatch.setattr(pve_platform, "ProxmoxAPI", FakeAPI)
    return FakeAPI


class FakeController:
    def __init__(self, raw_bytes):
        self.raw_bytes = raw_bytes

    def stats(self):
        return {"raw_bytes": self.raw_bytes, "stored_bytes": self.raw_bytes // 2, "level": 3, "concurrency": 2}


class FakeRunner:
    """Replaces BackupRunner: records the platform and the guests it would back up."""

    last = None
    fail = set()
    raw_bytes = 3 * 1024 * 1024

    def __init__(self, tuning, platform, repo, progress=None):
        self.platform = platform
        self.controller = FakeController(FakeRunner.raw_bytes)
        self.closed = False
        FakeRunner.last = self

    def run(self, guests):
        self.guests = guests
        return [
            jobs.GuestResult(
                guest=g,
                ok=g.id not in FakeRunner.fail,
                error="dd: read error" if g.id in FakeRunner.fail else "",
                snapshot=f"cluster1/vm/{g.id}/x",
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


def test_command_needing_a_source_without_section(tmp_path, repo_dir, caplog):
    path = tmp_path / "antigrafa.toml"
    path.write_text(f'[target.local]\npath = "{repo_dir.as_posix()}"\n')
    assert cli.main(["-c", str(path), "guests"]) == 2
    assert "[source.<name>] section is required" in caplog.text


def test_several_sources_need_source_flag(tmp_path, repo_dir, fake_api, caplog, capsys):
    path = tmp_path / "antigrafa.toml"
    two = "".join(
        f'[source.{name}]\ntype = "proxmox"\nurl = "https://{name}:8006"\ntoken_id = "a@pve!b"\ntoken_secret = "s"\n'
        for name in ("east", "west")
    )
    path.write_text(two + f'[target.local]\npath = "{repo_dir.as_posix()}"\n')
    assert cli.main(["-c", str(path), "guests"]) == 2
    assert "several sources configured, use --source (east, west)" in caplog.text
    assert cli.main(["-c", str(path), "guests", "--source", "west"]) == 0
    assert fake_api.instances[-1].args[0] == "https://west:8006"
    assert cli.main(["-c", str(path), "guests", "--source", "north"]) == 2
    assert "unknown source: north" in caplog.text


def test_proxmox_error(config, monkeypatch, caplog):
    def unreachable(*args, **kwargs):
        raise ProxmoxError("GET /cluster/resources: connection refused")

    monkeypatch.setattr(pve_platform, "ProxmoxAPI", unreachable)
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
    assert out[0].split() == ["100", "vm", "pve1", "running", "web"]
    assert out[2].split() == ["200", "ct", "pve1", "stopped", "dns"]
    assert fake_api.instances[0].args == ("https://pve1:8006", "antigrafa@pve!backup", "s3cret", None, True)


# --------------------------------------------------------------------------- backup


def test_backup_all_with_exclusion(config, fake_runner, capsys):
    assert cli.main(["-c", config, "backup", "--all", "--exclude", "101"]) == 0
    runner = fake_runner.last
    assert [g.id for g in runner.guests] == ["100", "200"]
    assert runner.closed
    out = capsys.readouterr().out
    assert "OK" in out and "VM 100 (web)" in out and "cluster1/vm/100/x" in out
    assert "New data: 3.0 MiB -> 1.5 MiB written (final zstd level 3, 2 parallel writes)" in out


def test_backup_ssh_hosts_come_from_config(config, fake_runner):
    assert cli.main(["-c", config, "backup", "--guest", "100"]) == 0
    platform = fake_runner.last.platform
    assert (platform.type, platform.name) == ("proxmox", "cluster1")
    ssh = platform.ssh_for_host("pve1")
    assert (ssh.host, ssh.user, ssh.port) == ("10.0.0.11", "root", 22)
    assert platform.ssh_for_host("pve2").host == "pve2"


def test_backup_selected_guests_with_failure(config, fake_runner, capsys):
    fake_runner.fail = {"101"}
    fake_runner.raw_bytes = 0
    assert cli.main(["-c", config, "backup", "--guest", "101", "100"]) == 1
    assert [g.id for g in fake_runner.last.guests] == ["100", "101"]
    out = capsys.readouterr().out
    assert "FAILED VM 101 (db)" in out and "dd: read error" in out
    assert "New data" not in out  # nothing new written


def test_backup_report_shows_every_duration(config, fake_runner, capsys):
    assert cli.main(["-c", config, "backup", "--guest", "100", "200"]) == 0
    out = capsys.readouterr().out
    assert "Backup report: " in out
    assert "OK     VM 100 (web)" in out and "12s" in out
    assert "Total: 2 guest(s), 2 OK, 0 failed, in " in out
    assert "Sum of guest times: 24s (guests backed up in parallel)" in out
    assert "Longest: VM 100 (web) (12s)" in out


def _disk(key, size, elapsed, new=3, reused=1, sent=None):
    sent = size if sent is None else sent
    stats = StreamStats(bytes_read=size, new_chunks=new, reused_chunks=reused, elapsed=elapsed, sent_bytes=sent)
    return jobs.DiskResult(key=key, volume=f"local:{key}", format="raw", size=size, stats=stats)


def test_backup_report_details(capsys):
    vm = jobs.GuestResult(
        guest=Guest("100", "vm", "web", "pve1"),
        ok=True,
        snapshot="cluster1/vm/100/x",
        disks=[_disk("scsi0", 20 * 1024**3, 90.0, sent=1024**3), _disk("efidisk0", 4096, 0.0)],
        duration=125.0,
        timings={"wait": 0.0, "prepare": 0.4, "snapshot": 1.2, "transfer": 120.0, "cleanup": 2.0, "flush": 1.4},
    )
    ct = jobs.GuestResult(
        guest=Guest("150", "ct", "dns", "pve1"),
        ok=False,
        error="tar: read error",
        duration=5.0,
        timings={"wait": 0.0, "prepare": 0.3, "snapshot": 0.5, "transfer": 6.0, "cleanup": 1.2},
    )
    started = datetime(2026, 11, 19, 23, 58, 0)
    cli._backup_report([vm, ct], started, 125.0)
    out = capsys.readouterr().out
    assert "Backup report: 2026-11-19 23:58:00 -> 2026-11-20 00:00:05 (2m 05s)" in out  # crosses midnight
    vm_line = next(line for line in out.splitlines() if "VM 100" in line)
    assert vm_line.startswith("OK     VM 100 (web)") and vm_line.split()[-3:] == ["2m", "05s", "cluster1/vm/100/x"]
    assert (
        "waiting for host 0.0s, preparation 0.4s, snapshot 1.2s, transfer 2m 00s, snapshot removal 2.0s, flush 1.4s"
    ) in out
    assert "scsi0" in out and "20.0 GiB" in out and "1m 30s" in out and "227.6 MiB/s" in out
    assert "3 new chunks, 1 reused, 1.0 GiB sent" in out
    assert "Sent over the network: 1.0 GiB (5% of what was read)" in out
    efi = next(line for line in out.splitlines() if "efidisk0" in line)
    assert " - " in efi  # too fast to measure a rate
    assert "FAILED CT 150 (dns)" in out and "tar: read error" in out
    assert "transfer 6.0s, snapshot removal 1.2s" in out
    assert "Total: 2 guest(s), 1 OK, 1 failed, in 2m 05s" in out
    assert "Sum of guest times" not in out  # 130 s over 125 s: not really parallel
    assert "Read: 20.0 GiB, 163.8 MiB/s on average" in out
    assert "Longest: VM 100 (web) (2m 05s)" in out


def test_backup_report_same_day_and_single_guest(capsys):
    r = jobs.GuestResult(guest=Guest("1", "vm", "a", "h"), ok=True, snapshot="s", duration=3.0)
    cli._backup_report([r], datetime(2026, 11, 19, 10, 0, 0), 3.25)
    out = capsys.readouterr().out
    assert "Backup report: 2026-11-19 10:00:00 -> 10:00:03 (3.2s)" in out
    assert "Longest" not in out and "Read:" not in out


def test_backup_stops_before_anything_without_the_rights(config, fake_runner, caplog):
    FakeAPI.denied = {"/vms/101": ["VM.Snapshot"], "/storage/local-zfs": ["Datastore.Audit"]}
    assert cli.main(["-c", config, "backup", "--all"]) == 3
    assert fake_runner.last is None  # no runner: no snapshot, nothing written
    assert "VM.Snapshot on /vms/101 (VM 101 (db))" in caplog.text
    assert "nothing was done" in caplog.text


def test_backup_checks_only_the_selected_guests(config, fake_runner):
    FakeAPI.denied = {"/vms/101": ["VM.Snapshot"]}
    assert cli.main(["-c", config, "backup", "--all", "--exclude", "101"]) == 0  # 101 not backed up
    assert cli.main(["-c", config, "backup", "--guest", "101"]) == 3


def test_guests_without_any_right(config, fake_api, caplog, capsys):
    fake_api.visible = []  # the API hides what the token cannot see
    fake_api.denied = {"/vms": ["VM.Audit"]}
    assert cli.main(["-c", config, "guests"]) == 3
    assert "VM.Audit on /vms (list the guests)" in caplog.text
    fake_api.denied = {}
    assert cli.main(["-c", config, "guests"]) == 0  # really no guest
    assert capsys.readouterr().out == ""


def test_backup_needs_a_selection(config, fake_runner, caplog):
    assert cli.main(["-c", config, "backup"]) == 2
    assert "use --all or --guest" in caplog.text
    assert fake_runner.last is None


def test_backup_unknown_guest(config, fake_runner, caplog):
    assert cli.main(["-c", config, "backup", "--guest", "100", "999", "998"]) == 2
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
    assert cli.main(["-c", config, "restore", "cluster1/vm/100/nope", "scsi0", str(tmp_path / "x")]) == 1


def test_verify_tls_reaches_the_api(config, fake_api):
    path = Path(config)
    path.write_text(path.read_text().replace('token_secret = "s3cret"', 'token_secret = "s3cret"\nverify_tls = false'))
    assert cli.main(["-c", config, "guests"]) == 0
    assert fake_api.instances[-1].args[-1] is False


# --------------------------------------------------------------------------- verbosity / progress


@pytest.fixture
def logging_config(monkeypatch):
    seen = {}
    monkeypatch.setattr(logging, "basicConfig", lambda **kw: seen.update(kw))
    return seen


@pytest.mark.parametrize(
    ("flags", "level", "detailed"),
    [([], logging.INFO, False), (["-q"], logging.WARNING, False), (["-v"], logging.DEBUG, False),
     (["-vv"], logging.DEBUG, True), (["-vvv"], logging.DEBUG, True)],
)
def test_verbosity_levels(config, logging_config, flags, level, detailed):
    assert cli.main(["-c", config, *flags, "list"]) == 0
    assert logging_config["level"] == level
    assert ("threadName" in logging_config["format"]) is detailed
    assert isinstance(logging_config["handlers"][0], cli.ProgressLogHandler)


def test_quiet_and_verbose_are_exclusive(config, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["-c", config, "-q", "-v", "list"])
    assert exc.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flags", "terminal", "shown"),
    [
        ([], True, True),  # terminal: shown
        ([], False, False),  # redirected / cron: hidden
        (["--no-progress"], True, False),
        (["--progress"], False, True),
        (["-q"], True, False),  # quiet hides it...
        (["-q", "--progress"], True, True),  # ...unless asked for
    ],
)
def test_progress_line_activation(config, monkeypatch, logging_config, flags, terminal, shown):
    seen = {}
    real = cli.cmd_list

    def spy(cfg, args):
        seen["enabled"] = args.progress_bar.enabled
        return real(cfg, args)

    monkeypatch.setattr(cli, "cmd_list", spy)
    monkeypatch.setattr(cli.Progress, "__init__", _progress_init(terminal))
    assert cli.main(["-c", config, *flags, "list"]) == 0
    assert seen["enabled"] is shown


def _progress_init(terminal):
    original = cli.Progress.__init__

    def init(self, *args, enabled=None, **kwargs):
        if enabled is None:
            enabled = terminal  # what the auto-detection would find
        original(self, *args, enabled=enabled, **kwargs)

    return init


def test_restore_with_progress(config, backup, tmp_path, capsys, logging_config):
    _, data, _ = backup
    out = tmp_path / "disk.raw"
    assert cli.main(["-c", config, "--progress", "restore", SNAP, "scsi0", str(out)]) == 0
    assert out.read_bytes() == data
    captured = capsys.readouterr()
    assert "restore  [####################] 100%" in captured.err
    assert "| scsi0 " in captured.err.replace("|  ", "| ")  # the disk being restored
    assert captured.err.endswith("\r")  # erased before the summary
    assert "restored (raw)" in captured.out


def test_verify_with_progress_counts_distinct_chunks(config, backup, capsys, logging_config):
    assert cli.main(["-c", config, "--progress", "verify"]) == 0
    captured = capsys.readouterr()
    assert "verify  [####################] 100%  3/3" in captured.err
    assert captured.out.startswith("OK")


def test_backup_hands_the_progress_line_to_the_runner(config, fake_runner, logging_config):
    seen = {}
    original = fake_runner.__init__

    def init(self, tuning, platform, repo, progress=None):
        seen["progress"] = progress
        original(self, tuning, platform, repo, progress)

    fake_runner.__init__ = init
    try:
        assert cli.main(["-c", config, "--progress", "backup", "--all"]) == 0
    finally:
        fake_runner.__init__ = original
    assert isinstance(seen["progress"], cli.Progress)
    assert seen["progress"].enabled is False  # finished before the summary
