"""Web interface: certificate, private files, backup manager, pages and API, HTTPS server."""

import io
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.request
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from antigrafa import cli
from antigrafa.config import Config, ConfigError, Tuning, WebConfig
from antigrafa.platforms.base import AccessDenied, Guest, PlatformError
from antigrafa.targets import TargetConfig
from antigrafa.web import certs, files, manager as manager_module, server, settings as web_settings, views
from antigrafa.web.manager import BackupManager, Busy
from test_jobs import FakeStream, MemoryDisk, MemoryPlatform

PASSWORD = "correct-horse-battery-42"


# --------------------------------------------------------------------------- private files


class Done:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_windows_rights_go_to_system_administrators_and_the_account(tmp_path):
    calls = []
    ok = files.restrict(
        tmp_path, directory=True, platform="win32", run=lambda argv, **kw: calls.append(argv) or Done(),
        environ={"USERDOMAIN": "LAB"},
    )
    assert ok
    [argv] = calls
    assert argv[:3] == ["icacls", str(tmp_path), "/inheritance:r"]
    grants = [argv[i + 1] for i, a in enumerate(argv) if a == "/grant:r"]
    assert grants[:2] == ["*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F"]
    assert grants[2].startswith("LAB\\") and grants[2].endswith(":(OI)(CI)F")
    assert "/T" not in argv  # children inherit; /T would empty the rights of existing files


def test_windows_file_rights_and_failures_are_only_warnings(tmp_path, caplog):
    path = tmp_path / "key"
    calls = []
    assert files.restrict(path, platform="win32", run=lambda argv, **kw: calls.append(argv) or Done(), environ={})
    assert calls[0][-1].endswith(":F") and "\\" not in calls[0][-1]  # no domain: the account alone

    assert not files.restrict(path, platform="win32", run=lambda argv, **kw: Done(5, "", "Access is denied."))
    assert "Access is denied." in caplog.text

    def missing(argv, **kw):
        raise FileNotFoundError("icacls")

    assert not files.restrict(path, platform="win32", run=missing)
    assert "could not restrict access (icacls)" in caplog.text


def test_linux_rights_are_0700_and_0600(tmp_path, monkeypatch):
    modes = {}
    monkeypatch.setattr(os, "chmod", lambda path, mode: modes.__setitem__(Path(path).name, mode))
    files.restrict(tmp_path / "dir", directory=True, platform="linux")
    files.restrict(tmp_path / "file", platform="linux")
    assert modes == {"dir": 0o700, "file": 0o600}


def test_linux_rights_failure_is_a_warning(tmp_path, caplog):
    assert not files.restrict(tmp_path / "absent", platform="linux")
    assert "could not restrict access" in caplog.text


def test_write_private_replaces_the_file(tmp_path):
    path = tmp_path / "sub" / "secret"
    files.write_private(path, b"one")
    files.write_private(path, b"two")
    assert path.read_bytes() == b"two"


# --------------------------------------------------------------------------- certificate


def test_self_signed_certificate_created_when_missing(tmp_path):
    cert, key = tmp_path / "tls" / "cert.pem", tmp_path / "tls" / "key.pem"
    assert certs.ensure_certificate(cert, key, ["backup.lab", "10.0.0.5", "backup.lab"]) == "missing"
    info = certs.describe(cert)
    assert info.self_signed
    assert info.names == ["backup.lab", "10.0.0.5"]  # duplicates removed, addresses as IP entries
    assert len(info.fingerprint.split(":")) == 32
    loaded = x509.load_pem_x509_certificate(cert.read_bytes())
    assert loaded.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value == "backup.lab"
    assert loaded.not_valid_after_utc - loaded.not_valid_before_utc > timedelta(days=390)
    private = serialization.load_pem_private_key(key.read_bytes(), None)
    assert private.public_key().public_numbers() == loaded.public_key().public_numbers()

    assert certs.ensure_certificate(cert, key, ["backup.lab"]) is None  # kept as is
    assert certs.describe(cert).fingerprint == info.fingerprint


def test_self_signed_certificate_renewed_before_it_expires(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    long_ago = datetime.now(timezone.utc) - timedelta(days=380)
    certs.create_self_signed(cert, key, ["backup.lab"], now=long_ago)
    old = certs.describe(cert).fingerprint
    assert certs.ensure_certificate(cert, key, ["backup.lab"]) == "expiring"
    assert certs.describe(cert).fingerprint != old


def _foreign_certificate(cert: Path, key: Path, days: int) -> None:
    private = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "backup.example.com")])
    now = datetime.now(timezone.utc)
    built = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(private.public_key())
        .serial_number(1).not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=days))
        .sign(private, hashes.SHA256())
    )
    cert.write_bytes(built.public_bytes(serialization.Encoding.PEM))
    key.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))


def test_a_certificate_antigrafa_did_not_make_is_never_replaced(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    _foreign_certificate(cert, key, days=3)  # about to expire: still the admin's business
    before = cert.read_bytes()
    assert certs.ensure_certificate(cert, key, ["backup.lab"]) is None
    assert cert.read_bytes() == before
    info = certs.describe(cert)
    assert not info.self_signed and info.names == []


def test_certificate_without_its_key_is_an_error(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("x")
    with pytest.raises(certs.CertificateError, match="key.pem is missing"):
        certs.ensure_certificate(cert, key, ["a"])
    cert.unlink()
    key.write_text("x")
    with pytest.raises(certs.CertificateError, match="cert.pem is missing"):
        certs.ensure_certificate(cert, key, ["a"])


def test_invalid_certificate_and_no_host_name(tmp_path):
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("not a certificate")
    key.write_text("x")
    with pytest.raises(certs.CertificateError, match="not a PEM certificate"):
        certs.ensure_certificate(cert, key, ["a"])
    with pytest.raises(certs.CertificateError, match="at least one host name"):
        certs.create_self_signed(tmp_path / "c", tmp_path / "k", [])


# --------------------------------------------------------------------------- settings


def test_local_names(monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "Backup")
    monkeypatch.setattr(socket, "getfqdn", lambda host: "backup.lab")
    monkeypatch.setattr(socket, "gethostbyname_ex", lambda host: ("backup", [], ["10.0.0.5", "127.0.0.1"]))
    assert web_settings.local_names("0.0.0.0") == ["backup.lab", "backup", "localhost", "127.0.0.1", "::1", "10.0.0.5"]
    assert web_settings.local_names("10.0.0.9")[2] == "10.0.0.9"

    def unresolved(host):
        raise OSError("no DNS")

    monkeypatch.setattr(socket, "gethostbyname_ex", unresolved)
    assert web_settings.local_names("::") == ["backup.lab", "backup", "localhost", "127.0.0.1", "::1"]


def test_settings_are_https_only(tmp_path):
    web = WebConfig(data_dir=str(tmp_path / "web"), allowed_hosts=["backup.lab"])
    built = web_settings.build(web, "/etc/antigrafa/antigrafa.toml")
    assert built["DEBUG"] is False
    assert built["ALLOWED_HOSTS"] == ["backup.lab"]
    assert built["SESSION_COOKIE_SECURE"] and built["CSRF_COOKIE_SECURE"]
    assert built["DATABASES"]["default"]["NAME"] == str(tmp_path / "web" / "web.sqlite3")
    assert built["ANTIGRAFA_CONFIG"] == "/etc/antigrafa/antigrafa.toml"
    # The secret key is made once and kept
    assert web_settings.build(web, "x")["SECRET_KEY"] == built["SECRET_KEY"]
    assert len(built["SECRET_KEY"]) > 50


# --------------------------------------------------------------------------- backup manager


class Gate:
    """Holds the read of a disk until the test lets it go."""

    def __init__(self):
        self.reading = threading.Event()
        self.go = threading.Event()


class HeldDisk(MemoryDisk):
    def __init__(self, key, data, gate):
        super().__init__(key, data)
        self.gate = gate

    def open_stream(self):
        gate = self.gate

        class Held(io.BytesIO):
            def read(self, n=-1):
                gate.reading.set()
                gate.go.wait(10)
                return super().read(n)

        stream = FakeStream(b"")
        stream.stdout = Held(self.data)
        return stream


class LabPlatform(MemoryPlatform):
    def __init__(self, guests, disks, denied=False):
        super().__init__(guests, disks)
        self.denied = denied
        self.checked = []

    def check_access(self, guests):
        self.checked.append([g.id for g in guests])
        if self.denied:
            raise AccessDenied("the token lacks VM.Snapshot on /vms/1")


GUESTS = [Guest("1", "vm", "web", "h1", "running"), Guest("2", "ct", "dns", "h2", "stopped")]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A manager over an in-memory platform and a real repository."""
    (tmp_path / "repo").mkdir()
    gate = Gate()
    platform = LabPlatform(
        GUESTS, {"1": [HeldDisk("disk0", os.urandom(300_000), gate)], "2": [MemoryDisk("rootfs", b"r" * 5000)]}
    )
    cfg = Config(
        sources={"lab": SimpleNamespace(name="lab")},
        targets={"local": TargetConfig(name="local", path=str(tmp_path / "repo"))},
        tuning=Tuning(chunk_size_mib=1, max_parallel_guests=2),
    )
    opened = []
    monkeypatch.setattr(manager_module, "open_platform", lambda c, name: opened.append(name) or platform)
    m = BackupManager("antigrafa.toml", load=lambda path: cfg)
    yield SimpleNamespace(manager=m, platform=platform, gate=gate, cfg=cfg, opened=opened)
    gate.go.set()
    m.shutdown(timeout=10)


def _wait(condition, timeout=10.0):
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_manager_runs_a_backup_and_reports_it(lab, caplog):
    caplog.set_level(logging.INFO)  # as the server does: the page shows the main steps
    m = lab.manager
    assert m.choices() == {"sources": ["lab"], "targets": ["local"]}
    assert [g["label"] for g in m.guests("lab")] == ["VM 1 (web)", "CT 2 (dns)"]

    run = m.start(source="lab", target=None, every=True, user="alice")
    assert lab.platform.checked == [["1", "2"]]  # rights checked before anything
    lab.gate.reading.wait(10)
    busy = m.status()
    assert busy["busy"] and busy["runs"][0]["state"] == "running"
    details = run.details()
    assert details["user"] == "alice" and details["source"] == "lab" and details["target"] == "local"
    assert "1/disk0" in [t["name"] for t in details["progress"]["tasks"]]  # held: still being read

    with pytest.raises(Busy):
        m.start(source="lab", target=None, every=True, user="bob")

    _wait(lambda: len(run.results) == 1)  # the container ends while the VM is held
    lab.gate.go.set()
    _wait(lambda: not run.active)
    assert run.state == "ok"
    details = run.details()
    assert details["progress"] is None
    # In the order the guests ended: the small container first
    assert [r["guest"]["id"] for r in details["results"]] == ["2", "1"]
    assert all(r["ok"] for r in details["results"])
    assert details["results"][0]["disks"][0]["key"] in ("disk0", "rootfs")
    assert any(line.startswith("Total: 2 guest(s), 2 OK") for line in details["report"])
    assert any("New data" in line for line in details["report"])
    assert any("backup complete" in line for line in details["logs"])
    assert not m.status()["busy"]
    assert m.run(run.id) is run and m.run(999) is None


def test_manager_selection_exclusion_and_errors(lab):
    m = lab.manager
    run = m.start(source="lab", target="local", every=True, exclude=["1"], user="alice")
    _wait(lambda: not run.active)
    assert [g.id for g in run.guests] == ["2"] and run.state == "ok"

    run = m.start(source="lab", target="local", guests=["2"], user="alice")
    _wait(lambda: not run.active)
    assert [g.id for g in run.guests] == ["2"]

    with pytest.raises(ConfigError, match="guests not found: 9"):
        m.start(source="lab", target="local", guests=["9"])
    with pytest.raises(ConfigError, match="no guest to back up"):
        m.start(source="lab", target="local", every=True, exclude=["1", "2"])
    with pytest.raises(ConfigError, match="unknown target"):
        m.start(source="lab", target="nope", every=True)
    lab.platform.denied = True
    with pytest.raises(AccessDenied):
        m.start(source="lab", target="local", every=True)
    assert not m.status()["busy"]  # a refused start never blocks the next one


def test_manager_cancel(lab, caplog):
    m = lab.manager
    run = m.start(source="lab", target=None, guests=["1"], user="alice")
    lab.gate.reading.wait(10)
    assert not m.cancel(run.id + 1, "bob")  # not that one
    assert m.cancel(run.id, "bob")
    assert run.state == "cancelling" and not m.cancel(run.id, "bob")  # already asked
    lab.gate.go.set()
    _wait(lambda: not run.active)
    assert run.state == "cancelled" and run.cancelled_by == "bob"
    assert run.results[0].error == "cancelled"
    assert lab.platform.snapshots == set()
    assert any("cancel requested by bob" in line for line in run.logs)


def test_manager_failed_guest_and_engine_failure(lab):
    m = lab.manager
    lab.platform.disks["2"] = []  # no disk: that guest fails
    run = m.start(source="lab", target=None, guests=["2"])
    _wait(lambda: not run.active)
    assert run.state == "failed" and run.results[0].error == "no disk to back up"

    class Broken:
        def __init__(self, *args, **kwargs):
            self.controller = SimpleNamespace(stats=lambda: {"raw_bytes": 0})

        def run(self, guests):
            raise RuntimeError("engine exploded")

        def close(self):
            pass

    m._runner_class = Broken
    run = m.start(source="lab", target=None, guests=["2"])
    _wait(lambda: not run.active)
    assert run.state == "failed" and run.error == "engine exploded"


def test_manager_log_paging_survives_the_line_limit(monkeypatch):
    monkeypatch.setattr(manager_module, "LOG_LINES", 3)
    run = manager_module.Run(1, "alice", "lab", "local", [])
    for n in range(5):
        run.add_log(f"line {n}")
    assert run.details()["logs"] == ["line 2", "line 3", "line 4"]
    assert run.details(4)["logs"] == ["line 4"]
    assert run.details(5)["logs"] == []
    assert run.details(1)["logs"] == ["line 2", "line 3", "line 4"]  # older lines are gone
    assert run.details(99)["logs"] == ["line 2", "line 3", "line 4"]  # another run's count: everything


def test_manager_log_ignores_the_web_server_itself(lab):
    run = manager_module.Run(1, "alice", "lab", "local", [])
    lab.manager.current = run
    logging.getLogger("antigrafa.jobs").warning("engine")
    logging.getLogger("antigrafa.web.views").warning("web")
    lab.manager.current = None
    assert [line.split()[-1] for line in run.logs] == ["engine"]


def test_manager_guests_checks_rights_when_nothing_is_visible(lab):
    lab.platform._guests = []
    lab.platform.denied = True
    with pytest.raises(AccessDenied):
        lab.manager.guests("lab")


def test_manager_refuses_to_start_while_the_scheduler_runs(lab, monkeypatch):
    from antigrafa.runlock import RunLock

    m = lab.manager
    scheduler = RunLock(lab.cfg.scheduler.directory)
    assert scheduler.try_acquire({"by": "scheduler", "job": "nightly", "what": "backup"})
    try:
        status = m.status()
        assert status["busy"] and status["external"]["label"] == "job nightly (started by the scheduler)"
        with pytest.raises(Busy, match="job nightly \\(started by the scheduler\\) is running: wait for it to end"):
            m.start(source="lab", target=None, every=True, user="alice")
        with pytest.raises(Busy, match="job nightly"):
            m.start_restore(target=None, backup="x", user="alice")
        assert not m._busy  # refused: nothing claimed here
    finally:
        scheduler.release()
    assert m.status() == {"busy": False, "runs": [], "external": None}


def test_manager_holds_the_run_lock_while_it_runs(lab):
    from antigrafa.runlock import RunLock

    m = lab.manager
    run = m.start(source="lab", target=None, guests=["1"], user="alice")
    lab.gate.reading.wait(10)
    holder = RunLock(lab.cfg.scheduler.directory).holder()
    assert holder["by"] == "web" and holder["what"] == "backup" and holder["user"] == "alice"
    assert m.status()["external"] is None  # its own run: not "external"
    lab.gate.go.set()
    _wait(lambda: not run.active)
    assert RunLock(lab.cfg.scheduler.directory).holder() is None
    with pytest.raises(ConfigError, match="no guest to back up"):
        m.start(source="lab", target=None, every=True, exclude=["1", "2"])
    assert RunLock(lab.cfg.scheduler.directory).holder() is None  # refused: released


def test_manager_unwritable_lock_directory(lab, tmp_path):
    from antigrafa.config import SchedulerConfig

    (tmp_path / "file").write_text("x")
    lab.cfg.scheduler = SchedulerConfig(data_dir=str(tmp_path / "file" / "s"))
    with pytest.raises(ConfigError, match="cannot take the run lock in .*\\[scheduler\\] data_dir"):
        lab.manager.start(source="lab", target=None, every=True)
    assert not lab.manager.status()["busy"]


def test_manager_runs_a_job_now_and_records_it(lab):
    from antigrafa import scheduler as sched
    from antigrafa.config import JobConfig

    lab.cfg.jobs["nightly"] = JobConfig(name="nightly", all=True, exclude=["1"], schedule="0 2 * * *")
    data = lab.manager.jobs()
    assert data["scheduler"]["alive"] is False
    [job] = data["jobs"]
    assert job["name"] == "nightly" and job["schedule"] == "cron 0 2 * * *" and job["last"] is None
    run = lab.manager.start_job("nightly", user="alice")
    assert run.job == "nightly" and run.summary()["job"] == "nightly" and [g.id for g in run.guests] == ["2"]
    _wait(lambda: not run.active)
    state = sched.read_job_state(lab.cfg.scheduler.directory, "nightly")
    assert state["last_start"] == run.started.isoformat()
    assert state["last"]["state"] == "ok" and state["last"]["by"] == "web" and state["last"]["user"] == "alice"
    assert lab.manager.jobs()["jobs"][0]["last"]["guests_ok"] == 1
    with pytest.raises(ConfigError, match="unknown job nope"):
        lab.manager.start_job("nope")


def _backed_up(lab):
    """A backup of the container in the lab repository; its listing entry."""
    run = lab.manager.start(source="lab", target=None, guests=["2"], user="alice")
    _wait(lambda: not run.active)
    [backup] = lab.manager.backups("local")
    return backup


class FakeRestore:
    """Stands for the engine's restore_guest: holds until the test lets it go."""

    def __init__(self, error=None):
        self.calls, self.error = [], error
        self.started, self.go = threading.Event(), threading.Event()

    def __call__(self, platform, repo, backup, request, progress, cancel):
        self.calls.append((platform, backup, request))
        with progress.task(100, name="rootfs") as task:
            task.advance(40)
            self.started.set()
            self.go.wait(10)
        if cancel.is_set():
            raise PlatformError("cancelled")
        if self.error:
            raise self.error
        return f"CT {request.guest_id} restored on h2 (1 disk(s)); not started"


def test_manager_lists_the_backups_of_a_target(lab):
    backup = _backed_up(lab)
    assert backup["path"].startswith("lab/ct/2/") and "\\" not in backup["path"]
    assert {k: backup[k] for k in ("source", "kind", "id", "name", "host", "size")} == {
        "source": "lab", "kind": "ct", "id": "2", "name": "dns", "host": "h2", "size": 5000}
    assert backup["time"] and backup["disks"] == [{"key": "rootfs", "volume": "memory:rootfs", "size": 5000}]
    with pytest.raises(ConfigError, match="unknown target"):
        lab.manager.backups("nope")


def test_manager_restores_a_backup_and_reports_it(lab, monkeypatch):
    backup = _backed_up(lab)
    fake = FakeRestore()
    monkeypatch.setattr(manager_module, "restore_guest", fake)
    m = lab.manager
    run = m.start_restore(target=None, backup=backup["path"], user="bob")  # no id: the original one
    fake.started.wait(10)
    status = m.status()
    assert status["busy"] and status["runs"][0]["kind"] == "restore" and status["runs"][0]["guest"] == "CT 2 (dns)"
    with pytest.raises(Busy, match="a restore is already running"):
        m.start(source="lab", target=None, every=True)
    details = run.details()
    assert details["backup"] == backup["path"] and details["user"] == "bob"
    assert details["restore"] == {"guest_id": "2", "host": "", "storage": "", "mode": "new", "stop": "",
                                  "start": False}
    assert details["progress"]["tasks"][0]["name"] == "rootfs"
    fake.go.set()
    _wait(lambda: not run.active)
    assert run.state == "ok" and run.message == "CT 2 restored on h2 (1 disk(s)); not started"
    assert run.summary()["guests_ok"] == 1 and not m.status()["busy"]
    platform, path, request = fake.calls[0]
    assert platform is lab.platform and path == backup["path"] and lab.opened[-1] == "lab"  # its own source

    fake.go.clear()
    fake.started.clear()
    run = m.start_restore(target="local", backup=backup["path"], guest_id="902", host="h1", storage="ZFS",
                          mode="overwrite", stop="force-stop", start=True, user="bob")
    fake.go.set()
    _wait(lambda: not run.active)
    request = fake.calls[-1][2]
    assert (request.guest_id, request.host, request.storage, request.overwrite, request.disk_only,
            request.stop, request.start) == ("902", "h1", "ZFS", True, False, "force-stop", True)
    assert run.guests[0].host == "h1"


def test_manager_restore_cancel_and_failure(lab, monkeypatch):
    backup = _backed_up(lab)
    fake = FakeRestore()
    monkeypatch.setattr(manager_module, "restore_guest", fake)
    m = lab.manager
    run = m.start_restore(target=None, backup=backup["path"], mode="disk-only", user="bob")
    fake.started.wait(10)
    assert m.cancel(run.id, "carol")
    fake.go.set()
    _wait(lambda: not run.active)
    assert run.state == "cancelled" and run.cancelled_by == "carol" and run.error == "cancelled"
    assert any("restore %d: cancel requested by carol" % run.id in line for line in run.logs)

    fake = FakeRestore(PlatformError("id 2 is taken"))
    fake.go.set()
    monkeypatch.setattr(manager_module, "restore_guest", fake)
    run = m.start_restore(target=None, backup=backup["path"], user="bob")
    _wait(lambda: not run.active)
    assert run.state == "failed" and run.error == "id 2 is taken" and run.summary()["guests_failed"] == 1
    assert any("restore %d failed: id 2 is taken" % run.id in line for line in run.logs)


def test_manager_runs_stay_listed_after_a_restart(lab, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="antigrafa")  # as the server does: the run's log is kept
    backup = _backed_up(lab)  # a backup by alice
    fake = FakeRestore(PlatformError("id 2 is taken"))
    fake.go.set()
    monkeypatch.setattr(manager_module, "restore_guest", fake)
    run = lab.manager.start_restore(target=None, backup=backup["path"], user="bob")
    _wait(lambda: not run.active)
    before = lab.manager.status()["runs"]
    assert [(r["kind"], r["state"]) for r in before] == [("restore", "failed"), ("backup", "ok")]

    restarted = BackupManager("antigrafa.toml", load=lambda path: lab.cfg)  # the server started again
    try:
        after = restarted.status()["runs"]
        keys = ("kind", "user", "source", "target", "state", "started", "finished", "guests_ok", "guests_failed")
        assert [{k: r[k] for k in keys} for r in after] == [{k: r[k] for k in keys} for r in before]
        details = restarted.run(after[1]["id"]).details()
        assert details["results"][0]["guest"]["id"] == "2" and details["progress"] is None
        assert any(line.startswith("Total: 1 guest(s), 1 OK") for line in details["report"])
        assert details["logs"]  # the log of the run, too
        details = restarted.run(after[0]["id"]).details()
        assert details["error"] == "id 2 is taken" and details["backup"] == backup["path"]
        assert details["restore"]["guest_id"] == "2"
    finally:
        restarted.close()


def test_manager_run_cut_by_a_crash_shows_as_failed_after_a_restart(lab):
    run = lab.manager.start(source="lab", target=None, every=True, user="alice")
    lab.gate.reading.wait(10)
    [own] = lab.manager.status()["runs"]  # its file is there, but this server shows its own run
    assert own["state"] == "running" and own["id"] == run.id and "external" not in own

    # The machine stops: the file still says "running", and nothing writes it any more
    path = lab.cfg.scheduler.directory / "runs" / f"{run.uid}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["heartbeat"] = (datetime.now().astimezone() - timedelta(minutes=5)).isoformat()
    restarted = BackupManager("antigrafa.toml", load=lambda p: lab.cfg)
    try:
        path.write_text(json.dumps(data), encoding="utf-8")
        [shown] = restarted.status()["runs"]
        assert shown["state"] == "failed" and shown["user"] == "alice"
        assert "stopped before the end" in restarted.run(shown["id"]).details()["error"]
    finally:
        restarted.close()


def test_manager_cannot_write_its_runs_but_still_runs(lab, tmp_path, caplog):
    from antigrafa.config import SchedulerConfig

    blocker = tmp_path / "s"
    blocker.mkdir()
    (blocker / "runs").write_text("not a directory")
    lab.cfg.scheduler = SchedulerConfig(data_dir=str(blocker))
    lab.gate.go.set()
    run = lab.manager.start(source="lab", target=None, every=True, user="alice")
    _wait(lambda: not run.active)
    assert run.state == "ok"
    assert "not listed after a restart" in caplog.text


def test_manager_restore_locations(lab, monkeypatch):
    with pytest.raises(PlatformError, match="not supported on memory"):  # the base platform
        lab.manager.restore_locations("lab", "vm")
    monkeypatch.setattr(lab.platform, "restore_locations", lambda kind: [{"host": "h1", "kind": kind}], raising=False)
    assert lab.manager.restore_locations("lab", "ct") == [{"host": "h1", "kind": "ct"}]
    assert lab.opened[-1] == "lab"
    with pytest.raises(ConfigError, match="unknown guest kind"):
        lab.manager.restore_locations("lab", "")


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"mode": "replace"}, "unknown restore mode"),
        ({"mode": "overwrite", "stop": "now"}, "unknown stop mode"),
        ({"stop": "stop"}, "only applies when replacing it"),
        ({"mode": "disk-only", "start": True}, "nothing to start"),
        ({"guest_id": "abc"}, "is not a guest id"),
        ({"backup": "../outside"}, "invalid backup path"),
        ({"backup": "lab/ct/2/2000-01-01T00-00-00Z"}, "no backup lab/ct/2/2000-01-01T00-00-00Z in target local"),
        ({"target": "nope"}, "unknown target"),
    ],
)
def test_manager_restore_refused_before_it_starts(lab, monkeypatch, options, message):
    backup = _backed_up(lab)
    monkeypatch.setattr(manager_module, "restore_guest", lambda *a: pytest.fail("never started"))
    with pytest.raises(ConfigError, match=message):
        lab.manager.start_restore(**{"target": None, "backup": backup["path"], **options})
    assert not lab.manager.status()["busy"]  # a refused restore never blocks the next run


# --------------------------------------------------------------------------- pages and API


@pytest.fixture(scope="session")
def web_config(tmp_path_factory):
    base = tmp_path_factory.mktemp("web")
    cfg = Config(
        sources={},
        targets={"local": TargetConfig(name="local", path=str(base / "repo"))},
        tuning=Tuning(),
        web=WebConfig(listen="127.0.0.1", port=1, data_dir=str(base / "data"),
                      allowed_hosts=["testserver", "127.0.0.1", "localhost"]),
    )
    server.setup(cfg, str(base / "antigrafa.toml"))  # Django: once per process
    from django.test import override_settings

    # Password hashing is slow on purpose; the tests do not need it to be
    fast = override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"])
    fast.enable()
    yield cfg
    fast.disable()


class FakeManager:
    def __init__(self):
        self.started = []
        self.cancelled = []
        self.error = None
        self.run_obj = manager_module.Run(7, "alice", "lab", "local", [GUESTS[0]])

    def choices(self):
        if self.error:
            raise self.error
        return {"sources": ["lab"], "targets": ["local", "nas"]}

    def guests(self, source):
        if self.error:
            raise self.error
        return [manager_module.guest_dict(g) for g in GUESTS]

    def status(self):
        return {"busy": True, "runs": [self.run_obj.summary()]}

    def running(self):
        return [{**self.run_obj.summary(), "progress": None}]

    def overview(self):
        if self.error:
            raise self.error
        return {"machine": {"hostname": "pbs"}, "backups": {"runs": 0}, "sources": [], "disks": []}

    def run(self, run_id):
        return self.run_obj if run_id == 7 else None

    def start(self, **kwargs):
        if self.error:
            raise self.error
        self.started.append(kwargs)
        return self.run_obj

    def cancel(self, run_id, user):
        self.cancelled.append((run_id, user))
        return run_id == 7

    def backups(self, target):
        if self.error:
            raise self.error
        self.listed = target
        return [{"path": "lab/ct/2/2026-11-21T20-40-09Z", "source": "lab", "kind": "ct", "id": "2"}]

    def restore_locations(self, source, kind):
        if self.error:
            raise self.error
        return [{"host": "h1", "online": True, "storages": [], "source": source, "kind": kind}]

    def start_restore(self, **kwargs):
        if self.error:
            raise self.error
        self.restores = getattr(self, "restores", []) + [kwargs]
        return self.run_obj

    def jobs(self):
        if self.error:
            raise self.error
        return {"scheduler": {"alive": False, "queue": [], "running": None},
                "jobs": [{"name": "nightly", "what": "every guest of lab -> local", "schedule": "cron 0 2 * * *"}]}

    def start_job(self, name, user=""):
        if self.error:
            raise self.error
        self.jobs_run = getattr(self, "jobs_run", []) + [(name, user)]
        return self.run_obj


@pytest.fixture
def client(web_config):
    from django.contrib.auth import get_user_model
    from django.test import Client

    model = get_user_model()
    if not model.objects.filter(username="alice").exists():
        server.add_user(web_config, "x", "alice", PASSWORD)
    fake = FakeManager()
    views.set_manager(fake)
    c = Client()
    c.fake = fake
    yield c
    views.set_manager(None)


def _login(c):
    assert c.login(username="alice", password=PASSWORD)


def test_pages_require_a_login(client):
    response = client.get("/")
    assert response.status_code == 302 and response["Location"].startswith("/login/")
    assert client.get("/login/").status_code == 200
    for url in ("/api/status", "/api/guests"):
        response = client.get(url)
        assert response.status_code == 401 and response.json() == {"error": "not logged in"}
    assert client.post("/api/backups", "{}", content_type="application/json").status_code == 401


def test_index_page_lists_sources_and_targets(client):
    _login(client)
    page = client.get("/backup/").content.decode()
    assert "<option>lab</option>" in page and "<option>nas</option>" in page
    assert 'name="csrf-token"' in page
    client.fake.error = ConfigError("bad config")
    assert "Configuration: bad config" in client.get("/backup/").content.decode()


def test_status_and_guests(client):
    _login(client)
    client.fake.run_obj.add_log("hello")
    data = client.get("/api/status").json()
    assert data["busy"] and data["run"]["id"] == 7 and data["run"]["logs"] == ["hello"]
    assert client.get("/api/status?run=7&logs_after=1").json()["run"]["logs"] == []
    assert client.get("/api/status?run=bad").json()["run"]["id"] == 7  # unknown: the latest
    assert [g["id"] for g in client.get("/api/guests?source=lab").json()["guests"]] == ["1", "2"]
    client.fake.error = AccessDenied("no right")
    assert client.get("/api/guests").status_code == 403


def test_start_and_cancel(client):
    _login(client)
    body = {"source": "lab", "target": "", "all": True, "exclude": ["2"]}
    response = client.post("/api/backups", json.dumps(body), content_type="application/json")
    assert response.status_code == 202 and response.json() == {"run": 7}
    assert client.fake.started == [
        {"source": "lab", "target": None, "guests": [], "every": True, "exclude": ["2"], "user": "alice"}
    ]
    assert client.post("/api/backups/7/cancel").json() == {"run": 7, "state": "cancelling"}
    assert client.post("/api/backups/8/cancel").status_code == 409
    assert client.fake.cancelled == [(7, "alice"), (8, "alice")]
    assert client.get("/api/backups/7/cancel").status_code == 405  # never by a simple link


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ("not json", 400),
        ("[1]", 400),
        (json.dumps({"source": 3}), 400),
        (json.dumps({"guests": "1"}), 400),
    ],
)
def test_start_rejects_bad_requests(client, body, status):
    _login(client)
    assert client.post("/api/backups", body, content_type="application/json").status_code == status
    assert client.fake.started == []


@pytest.mark.parametrize(
    ("error", "status"),
    [(Busy("running"), 409), (AccessDenied("rights"), 403), (ConfigError("config"), 400), (OSError("nfs"), 502)],
)
def test_start_errors_become_json(client, error, status):
    _login(client)
    client.fake.error = error
    response = client.post("/api/backups", "{}", content_type="application/json")
    assert response.status_code == status and response.json() == {"error": str(error)}


def test_backups_listed_for_the_restore(client):
    assert client.get("/api/backups?target=nas").status_code == 401
    _login(client)
    data = client.get("/api/backups?target=nas").json()
    assert data["backups"][0]["path"] == "lab/ct/2/2026-11-21T20-40-09Z" and client.fake.listed == "nas"
    client.get("/api/backups")
    assert client.fake.listed is None  # the default target
    assert client.put("/api/backups").status_code == 405
    client.fake.error = OSError("nas gone")
    assert client.get("/api/backups").status_code == 502


def test_restore_locations_listed(client):
    assert client.get("/api/restore-locations?source=lab&kind=ct").status_code == 401
    _login(client)
    [location] = client.get("/api/restore-locations?source=lab&kind=ct").json()["locations"]
    assert (location["host"], location["source"], location["kind"]) == ("h1", "lab", "ct")
    assert client.get("/api/restore-locations?kind=vm").json()["locations"][0]["source"] is None  # default source
    assert client.post("/api/restore-locations").status_code == 405
    client.fake.error = PlatformError("pve unreachable")
    assert client.get("/api/restore-locations?kind=vm").status_code == 502


def test_restore_has_its_own_page(client):
    response = client.get("/restore/")
    assert response.status_code == 302 and response["Location"].startswith("/login/")
    _login(client)
    page = client.get("/restore/").content.decode()
    assert "Restore a guest" in page and 'id="restore-target"' in page and "/api/restores" in page
    assert "<option>nas</option>" in page and 'const PAGE = "restore"' in page
    assert "Start a backup" not in page and "Recent restores" in page
    assert '<a href="/restore/" aria-current="page">Restore</a>' in page
    backup = client.get("/backup/").content.decode()
    assert "Start a backup" in backup and 'const PAGE = "backup"' in backup and "Recent backups" in backup
    assert "Restore a guest" not in backup and 'id="restore-target"' not in backup
    assert '<a href="/backup/" aria-current="page">Backup</a>' in backup and 'href="/restore/"' in backup
    client.fake.error = ConfigError("bad config")
    assert "Configuration: bad config" in client.get("/restore/").content.decode()
    assert client.post("/restore/").status_code == 405


def test_status_of_one_kind_of_run(client):
    _login(client)
    restore = manager_module.Run(8, "bob", "lab", "local", [GUESTS[1]], kind="restore", state="failed")
    backup = client.fake.run_obj  # 7, running
    client.fake.status = lambda: {"busy": True, "runs": [restore.summary(), backup.summary()]}
    client.fake.run = lambda run_id: {7: backup, 8: restore}.get(run_id)
    data = client.get("/api/status").json()
    assert [r["id"] for r in data["runs"]] == [8, 7] and data["run"]["id"] == 8
    data = client.get("/api/status?kind=backup").json()
    assert [r["id"] for r in data["runs"]] == [7] and data["run"]["id"] == 7
    data = client.get("/api/status?kind=restore").json()
    # Its own runs, and the most recent of them; the running backup is pointed to
    assert [r["id"] for r in data["runs"]] == [8] and data["run"]["id"] == 8
    assert data["active"]["id"] == 7 and data["active"]["kind"] == "backup"
    backup.state = "cancelling"  # still running
    assert client.get("/api/status?kind=restore").json()["active"]["id"] == 7
    backup.state = "failed"
    assert client.get("/api/status?kind=restore").json()["active"] is None
    assert client.get("/api/status?kind=restore&run=7").json()["run"]["id"] == 7  # asked for: shown
    assert client.get("/api/status?kind=other").status_code == 400


def test_start_a_restore(client):
    body = {"target": "local", "backup": "lab/ct/2/2026-11-21T20-40-09Z", "guest_id": " 902 ", "mode": "overwrite",
            "stop": "stop", "start": True}
    assert client.post("/api/restores", json.dumps(body), content_type="application/json").status_code == 401
    _login(client)
    response = client.post("/api/restores", json.dumps(body), content_type="application/json")
    assert response.status_code == 202 and response.json() == {"run": 7}
    assert client.fake.restores == [{
        "target": "local", "backup": "lab/ct/2/2026-11-21T20-40-09Z", "guest_id": "902", "host": "", "storage": "",
        "mode": "overwrite", "stop": "stop", "start": True, "user": "alice"}]
    # The defaults: the original id, node and storages, a new guest
    client.post("/api/restores", json.dumps({"backup": "x"}), content_type="application/json")
    assert client.fake.restores[-1] == {"target": None, "backup": "x", "guest_id": "", "host": "", "storage": "",
                                        "mode": "new", "stop": "", "start": False, "user": "alice"}
    assert client.get("/api/restores").status_code == 405


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"backup": ""},
        {"backup": 3},
        {"backup": "x", "guest_id": 902},
        {"backup": "x", "mode": ["new"]},
        {"backup": "x", "start": "yes"},
        {"backup": "x", "target": 1},
    ],
)
def test_restore_rejects_bad_requests(client, body):
    _login(client)
    response = client.post("/api/restores", json.dumps(body), content_type="application/json")
    assert response.status_code == 400
    assert getattr(client.fake, "restores", []) == []


@pytest.mark.parametrize(
    ("error", "status"),
    [(Busy("running"), 409), (ConfigError("config"), 400), (PlatformError("pve"), 502)],
)
def test_restore_errors_become_json(client, error, status):
    _login(client)
    client.fake.error = error
    response = client.post("/api/restores", json.dumps({"backup": "x"}), content_type="application/json")
    assert response.status_code == status and response.json() == {"error": str(error)}


def test_jobs_listed_and_run_now(client):
    assert client.get("/api/jobs").status_code == 401
    assert client.post("/api/jobs/nightly/run").status_code == 401
    _login(client)
    data = client.get("/api/jobs").json()
    assert data["jobs"][0]["name"] == "nightly" and data["scheduler"]["alive"] is False
    response = client.post("/api/jobs/nightly/run")
    assert response.status_code == 202 and response.json() == {"run": 7}
    assert client.fake.jobs_run == [("nightly", "alice")]
    assert client.get("/api/jobs/nightly/run").status_code == 405  # never by a simple link
    assert client.post("/api/jobs").status_code == 405
    client.fake.error = Busy("job nightly (started by the scheduler) is running")
    response = client.post("/api/jobs/nightly/run")
    assert response.status_code == 409 and "started by the scheduler" in response.json()["error"]
    client.fake.error = ConfigError("unknown job x")
    assert client.post("/api/jobs/x/run").status_code == 400
    assert client.get("/api/jobs").status_code == 400


def test_backup_page_has_the_jobs(client):
    _login(client)
    page = client.get("/backup/").content.decode()
    assert "Scheduled jobs" in page and "/api/jobs" in page and 'id="jobs"' in page
    assert "Scheduled jobs" not in client.get("/restore/").content.decode()


def test_status_says_what_runs_in_the_scheduler(client):
    _login(client)
    external = {"by": "scheduler", "job": "nightly", "label": "job nightly (started by the scheduler)"}
    client.fake.status = lambda: {"busy": True, "runs": [], "external": external}
    data = client.get("/api/status?kind=restore").json()
    assert data["external"] == external and data["busy"] and data["run"] is None and data["active"] is None


def test_posts_need_the_csrf_token(client):
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    assert strict.login(username="alice", password=PASSWORD)
    assert strict.post("/api/backups", "{}", content_type="application/json").status_code == 403
    page = strict.get("/").content.decode()
    token = page.split('name="csrf-token" content="')[1].split('"')[0]
    # Over HTTPS, Django also wants the request to come from this site (browsers send Origin)
    headers = {"X-CSRFToken": token, "Origin": "https://testserver"}
    response = strict.post("/api/backups", "{}", content_type="application/json", headers=headers, secure=True)
    assert response.status_code == 202


def test_unknown_host_is_refused(client):
    assert client.get("/login/", headers={"Host": "evil.example"}).status_code == 400


def test_default_manager_reads_the_configured_file(client, web_config):
    views.set_manager(None)
    m = views.get_manager()
    try:
        assert m.config_path.endswith("antigrafa.toml") and views.get_manager() is m
    finally:
        m.close()


# --------------------------------------------------------------------------- users


def test_add_change_and_remove_a_user(web_config):
    assert server.add_user(web_config, "x", "carol", PASSWORD)
    assert not server.add_user(web_config, "x", "carol", PASSWORD + "!")  # new password
    with pytest.raises(ConfigError, match="password refused: This password is too short"):
        server.add_user(web_config, "x", "carol", "short")
    with pytest.raises(ConfigError, match="password refused"):
        server.add_user(web_config, "x", "dave", "481516234200")  # digits only
    assert server.remove_user(web_config, "x", "carol")
    assert not server.remove_user(web_config, "x", "carol")


def test_cli_web_users(web_config, monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda path: web_config)
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\r\n"))
    assert cli.main(["web", "adduser", "erin", "--password-stdin"]) == 0
    assert "user erin created" in capsys.readouterr().out
    answers = iter([PASSWORD, PASSWORD])
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))
    assert cli.main(["web", "adduser", "erin"]) == 0
    assert "updated (new password)" in capsys.readouterr().out
    answers = iter([PASSWORD, "other"])
    assert cli.main(["web", "adduser", "erin"]) == 2  # typed twice, differently
    assert cli.main(["web", "deluser", "erin"]) == 0
    assert cli.main(["web", "deluser", "erin"]) == 2


def test_cli_web_without_its_dependencies(monkeypatch, caplog):
    import importlib

    def missing(name):
        raise ModuleNotFoundError("No module named 'django'", name="django")

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(ConfigError, match=r"pip install 'antigrafa\[web\]'"):
        cli._web("server")

    def broken(name):
        raise ImportError("something else", name="antigrafa.web.server")

    monkeypatch.setattr(importlib, "import_module", broken)
    with pytest.raises(ImportError, match="something else"):
        cli._web("server")


# --------------------------------------------------------------------------- HTTPS server


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_server_refuses_to_start_without_a_user(web_config, monkeypatch):
    from django.contrib.auth import get_user_model

    monkeypatch.setattr(get_user_model().objects, "exists", lambda: False)
    with pytest.raises(ConfigError, match="antigrafa web adduser"):
        server.make_server(web_config, "x")


def test_https_end_to_end(web_config, tmp_path, caplog):
    """A real server: self-signed certificate created, TLS 1.2+, login page served."""
    import dataclasses

    from django.contrib.auth import get_user_model

    if not get_user_model().objects.filter(username="alice").exists():
        server.add_user(web_config, "x", "alice", PASSWORD)
    web = dataclasses.replace(web_config.web, port=_free_port(), data_dir=str(tmp_path / "tls-test"))
    cfg = dataclasses.replace(web_config, web=web)
    caplog.set_level(logging.INFO)
    httpd = server.make_server(cfg, "x")
    assert "self-signed certificate created" in caplog.text
    fingerprint = certs.describe(web.certificate).fingerprint
    assert fingerprint in caplog.text

    views.set_manager(FakeManager())
    httpd.prepare()
    thread = threading.Thread(target=httpd.serve, daemon=True)
    thread.start()
    try:
        context = ssl.create_default_context(cafile=str(web.certificate))  # trusts only this certificate
        context.check_hostname = False
        with urllib.request.urlopen(f"https://127.0.0.1:{web.port}/login/", context=context, timeout=10) as r:
            assert r.status == 200 and b"Sign in" in r.read()
        old = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        old.check_hostname, old.verify_mode = False, ssl.CERT_NONE
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)  # TLS 1.1: deprecated, which is the point
            old.maximum_version = ssl.TLSVersion.TLSv1_1
        with pytest.raises((ssl.SSLError, OSError)):
            urllib.request.urlopen(f"https://127.0.0.1:{web.port}/login/", context=old, timeout=10)
    finally:
        httpd.stop()
        thread.join(10)
        views.set_manager(None)


def test_serve_stops_cleanly_and_cancels_the_backup(web_config, caplog):
    stopped = []

    class Interrupted:
        def start(self):
            raise KeyboardInterrupt

        def stop(self):
            stopped.append(True)

    shut = []
    fake = SimpleNamespace(shutdown=lambda: shut.append(True))
    views.set_manager(fake)
    try:
        caplog.set_level(logging.INFO)
        assert server.serve(web_config, "x", server=Interrupted()) == 0
    finally:
        views.set_manager(None)
    assert stopped == [True] and shut == [True]
    assert "web interface: https://127.0.0.1:1/" in caplog.text
    with pytest.raises(KeyboardInterrupt):
        server._interrupt(15, None)  # SIGTERM stops like Ctrl+C


def test_manager_shutdown_waits_for_the_cleanup(lab):
    run = lab.manager.start(source="lab", target=None, guests=["1"], user="alice")
    lab.gate.reading.wait(10)
    threading.Timer(0.2, lab.gate.go.set).start()
    lab.manager.shutdown(timeout=10)
    assert run.state == "cancelled" and run.cancelled_by == "server shutdown"
    assert lab.platform.snapshots == set()


@pytest.mark.parametrize(
    ("listen", "expected"),
    [("10.0.0.5", "https://10.0.0.5:8443/"), ("fd00::5", "https://[fd00::5]:8443/")],
)
def test_address_shown(listen, expected):
    cfg = SimpleNamespace(web=WebConfig(listen=listen))
    assert server.address(cfg) == expected


def test_address_with_every_interface(monkeypatch):
    monkeypatch.setattr(server, "local_names", lambda: ["backup.lab"])
    assert server.address(SimpleNamespace(web=WebConfig())) == "https://backup.lab:8443/"
    monkeypatch.setattr(server, "local_names", lambda: [])
    assert server.address(SimpleNamespace(web=WebConfig())) == "https://localhost:8443/"


def test_certificate_errors_name_the_section(web_config, tmp_path):
    import dataclasses

    cert = tmp_path / "c.pem"
    cert.write_text("x")
    web = dataclasses.replace(web_config.web, cert_file=str(cert), key_file=str(tmp_path / "missing.pem"))
    with pytest.raises(ConfigError, match=r"^\[web\]: .*missing.pem is missing"):
        server.certificate(dataclasses.replace(web_config, web=web))


def test_expiring_self_signed_certificate_is_renewed_at_start(web_config, tmp_path, caplog):
    import dataclasses

    web = dataclasses.replace(web_config.web, data_dir=str(tmp_path))
    certs.create_self_signed(web.certificate, web.private_key, ["a"],
                             now=datetime.now(timezone.utc) - timedelta(days=390))
    server.certificate(dataclasses.replace(web_config, web=web))
    assert "self-signed certificate renewed" in caplog.text


def test_foreign_certificate_is_used_as_is(web_config, tmp_path, caplog):
    import dataclasses

    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    _foreign_certificate(cert, key, days=200)
    web = dataclasses.replace(web_config.web, cert_file=str(cert), key_file=str(key))
    caplog.set_level(logging.INFO)
    server.certificate(dataclasses.replace(web_config, web=web))
    assert "self-signed" not in caplog.text and "valid until" in caplog.text


# --------------------------------------------------------------------------- home page


def test_home_page_shows_what_runs_and_the_machine(client):
    response = client.get("/")
    assert response.status_code == 302 and response["Location"].startswith("/login/")
    response = client.post("/login/", {"username": "alice", "password": PASSWORD})
    assert response.status_code == 302 and response["Location"] == "/"  # signed in: the home page
    page = client.get("/").content.decode()
    assert "Running now" in page and "/api/running" in page and "/api/overview" in page
    # The Home tab is a house icon, named for screen readers
    assert 'href="/" class="home-tab" aria-label="Home" title="Home" aria-current="page"' in page
    assert "<svg" in page and 'href="/backup/"' in page
    # Each run leads to the page of its kind, opened on that run
    assert 'const RUN_PAGES = {backup: "/backup/", restore: "/restore/"}' in page and "?run=" in page
    assert "Start a backup" not in page and 'id="run"' not in page


def test_running_and_overview_api(client):
    for url in ("/api/running", "/api/overview"):
        assert client.get(url).status_code == 401
    _login(client)
    [run] = client.get("/api/running").json()["running"]
    assert run["id"] == 7 and run["progress"] is None
    assert client.get("/api/overview").json()["machine"] == {"hostname": "pbs"}
    client.fake.error = ConfigError("bad config")
    assert client.get("/api/overview").status_code == 400


def test_manager_running_backup_and_overview(lab):
    m = lab.manager
    assert m.running() == []
    run = m.start(source="lab", target=None, guests=["1"], user="alice")
    lab.gate.reading.wait(10)
    [shown] = m.running()
    assert shown["id"] == run.id and shown["guests_total"] == 1 and shown["progress"]["total"] == 300_000
    lab.gate.go.set()
    _wait(lambda: not run.active)
    assert m.running() == []

    data = m.overview()
    assert data["machine"]["hostname"] and data["machine"]["version"]
    assert data["backups"]["guests_ok"] == 1 and data["backups"]["guests_failed"] == 0
    assert data["backups"]["runs_ok"] == 1 and data["backups"]["last_ok"]
    assert data["sources"] == [{"name": "lab", "type": "", "hosts": [], "error": ""}]
    system = data["disks"][0]
    assert system["system"] and system["total"] >= system["used"] > 0
    m._sizes.wait(10)  # the space of the destination, measured in the background
    disk = next(d for d in m.overview()["disks"] if any(t["name"] == "local" for t in d["targets"]))
    [target] = [t for t in disk["targets"] if t["name"] == "local"]
    assert target["bytes"] > 0 and target["backups"] == 1 and disk["antigrafa"] >= target["bytes"]
    assert disk["error"] == "" and disk["free"] > 0


def test_manager_overview_unreachable_destinations(lab, tmp_path):
    (tmp_path / "plain").mkdir()
    lab.cfg.targets["gone"] = TargetConfig(name="gone", path=str(tmp_path / "missing"))
    lab.cfg.targets["share"] = TargetConfig(name="share", path=str(tmp_path / "plain"), kind="nfs")
    disks = {d["targets"][0]["name"]: d for d in lab.manager.overview()["disks"] if d["error"]}
    assert disks["gone"]["error"].endswith("missing does not exist")
    assert "not on an NFS mount" in disks["share"]["error"]  # never the local disk under it


def test_manager_overview_reports_a_source_it_cannot_reach(lab, monkeypatch):
    def unreachable(cfg, name):
        raise PlatformError("pve1: connection refused")

    monkeypatch.setattr(manager_module, "open_platform", unreachable)
    [source] = lab.manager.overview()["sources"]
    assert source["error"] == "pve1: connection refused" and source["hosts"] == []


def test_run_pages_scroll_to_the_run_they_are_opened_on(client):
    _login(client)
    for url in ("/backup/", "/restore/"):
        page = client.get(url).content.decode()
        assert "scrollToRun = true" in page and '$("run").scrollIntoView' in page


def test_manager_running_lists_every_kind(lab, monkeypatch):
    backup = _backed_up(lab)
    fake = FakeRestore()  # held until the test lets it go
    monkeypatch.setattr(manager_module, "restore_guest", fake)
    run = lab.manager.start_restore(target=None, backup=backup["path"], user="bob")
    fake.started.wait(10)
    [shown] = lab.manager.running()
    assert shown["kind"] == "restore" and shown["id"] == run.id and shown["guest"]
    fake.go.set()
    _wait(lambda: not run.active)
    assert lab.manager.running() == []


# --------------------------------------------------------------------------- account


def test_account_page_from_the_user_name(client):
    assert client.get("/account/").status_code == 302  # signed in only
    _login(client)
    home = client.get("/").content.decode()
    assert 'href="/account/" class="account-link"' in home and ">alice</a>" in home
    page = client.get("/account/").content.decode()
    assert "Your details" in page and 'name="first_name"' in page and 'name="email"' in page
    assert 'name="old_password"' in page and 'name="new_password2"' in page
    assert "at least 12 characters" in page  # the password rules, shown before typing
    # Each form brings the page back to itself, errors included
    assert 'action="/account/#details"' in page and 'action="/account/#password"' in page
    assert 'href="/account/" class="account-link" title="Your details and password" aria-current="page"' in page


def test_account_details_are_saved(client):
    from django.contrib.auth import get_user_model

    _login(client)
    response = client.post("/account/", {"form": "profile", "first_name": "Alice", "last_name": "Martin",
                                         "email": "alice@example.org"})
    assert response.status_code == 302 and response["Location"] == "/account/?saved=profile#details"
    user = get_user_model().objects.get(username="alice")
    assert (user.first_name, user.last_name, user.email) == ("Alice", "Martin", "alice@example.org")
    page = client.get(response["Location"]).content.decode()
    # The confirmation shows in the section the page scrolls to (#details), under its title
    assert '<section id="details">' in page
    assert page.index("Your details are saved.") < page.index('<section id="password">')
    assert ">Alice Martin</a>" in client.get("/").content.decode()  # the header shows the name

    page = client.post("/account/", {"form": "profile", "first_name": "A", "last_name": "", "email": "not-an-email"})
    assert page.status_code == 200 and "Enter a valid email address." in page.content.decode()
    assert get_user_model().objects.get(username="alice").email == "alice@example.org"  # unchanged
    client.post("/account/", {"form": "profile", "first_name": "", "last_name": "", "email": ""})  # back


def test_password_change(client):
    from django.contrib.auth import get_user_model
    from django.test import Client

    _login(client)
    other = Client()
    assert other.login(username="alice", password=PASSWORD)  # another session of the same user
    new = "a-much-longer-pass-phrase-42"
    wrong = client.post("/account/", {"form": "password", "old_password": "nope",
                                      "new_password1": new, "new_password2": new})
    assert "Your old password was entered incorrectly" in wrong.content.decode()
    short = client.post("/account/", {"form": "password", "old_password": PASSWORD,
                                      "new_password1": "short", "new_password2": "short"})
    assert "too short" in short.content.decode()
    response = client.post("/account/", {"form": "password", "old_password": PASSWORD,
                                         "new_password1": new, "new_password2": new})
    assert response["Location"] == "/account/?saved=password#password"
    page = client.get(response["Location"]).content.decode()
    assert page.index('<section id="password">') < page.index("Your password is changed.")
    assert client.get("/").status_code == 200  # this session stays signed in
    assert other.get("/").status_code == 302  # the other one is signed out
    assert get_user_model().objects.get(username="alice").check_password(new)
    user = get_user_model().objects.get(username="alice")
    user.set_password(PASSWORD)  # back, for the other tests
    user.save()


def test_account_ignores_an_unknown_form(client):
    _login(client)
    assert client.post("/account/", {"form": "other"}).status_code == 200
    assert "is changed" not in client.get("/account/?saved=nonsense").content.decode()
    assert client.delete("/account/").status_code == 405


def test_manager_notifies_the_end_of_a_run(lab, monkeypatch):
    from antigrafa import notify

    sent = []
    monkeypatch.setattr(notify, "send", lambda channels, n: sent.append((channels, n)))
    lab.cfg.notify = {"ops": notify.NotifyConfig(name="ops", type="discord", url="https://x/y")}
    backup = _backed_up(lab)  # a backup by alice, from the page
    _wait(lambda: sent)  # sent right after the run shows as ended
    [(channels, n)] = sent
    assert channels is lab.cfg.notify and n.event == "backup-ok" and n.title == "Backup succeeded: lab -> local"
    assert "started from the web by alice" in n.lines[0]
    fake = FakeRestore(PlatformError("id 2 is taken"))
    fake.go.set()
    monkeypatch.setattr(manager_module, "restore_guest", fake)
    run = lab.manager.start_restore(target=None, backup=backup["path"], user="bob")
    _wait(lambda: not run.active)
    _wait(lambda: len(sent) == 2)
    restore = sent[1][1]
    assert restore.event == "restore" and restore.status == "failed" and "Error: id 2 is taken" in restore.lines
