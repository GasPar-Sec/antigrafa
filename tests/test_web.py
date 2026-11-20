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
from antigrafa.platforms.base import AccessDenied, Guest
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
    page = client.get("/").content.decode()
    assert "<option>lab</option>" in page and "<option>nas</option>" in page
    assert 'name="csrf-token"' in page
    client.fake.error = ConfigError("bad config")
    assert "Configuration: bad config" in client.get("/").content.decode()


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
