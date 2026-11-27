import http.server
import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from antigrafa import cli, monitor, notify
from antigrafa import scheduler as sched
from antigrafa.config import Config, ConfigError, SchedulerConfig, Tuning, load_config
from antigrafa.notify import Notification, NotifyConfig
from antigrafa.targets import TargetConfig

EMAIL = dict(type="email", smtp_host="smtp.lan", sender="ag@lan", to=["admin@lan"])


def _channel(name="ops", **kwargs):
    return NotifyConfig(name=name, **kwargs)


# --------------------------------------------------------------------------- configuration


def test_channel_settings_are_checked():
    assert _channel(**EMAIL).events == ["backup-failed", "restore", "system"]  # all but successes
    assert _channel(**EMAIL).port == 587 and _channel(**EMAIL, security="ssl").port == 465
    assert _channel(**EMAIL, security="none", smtp_port=2525).port == 2525
    for bad, match in [
        (dict(type="sms"), "type must be one of"),
        (dict(EMAIL, events=["backup-failed", "always"]), "events must be a list of"),
        (dict(EMAIL, to=[]), "to must be a list of email addresses"),
        (dict(EMAIL, to=["nobody"]), "to must be a list of email addresses"),
        (dict(EMAIL, smtp_host=""), "smtp_host and sender are required"),
        (dict(EMAIL, security="tls"), "security must be one of"),
        (dict(EMAIL, smtp_port=70000), "smtp_port must be a port number"),
        (dict(type="discord"), "url or url_file is required for discord"),
        (dict(type="teams", url="http://example.org/hook"), "url must start with https://"),
    ]:
        with pytest.raises(ValueError, match=match):
            _channel(**bad)


def test_sections_in_the_configuration(tmp_path):
    path = tmp_path / "antigrafa.toml"
    path.write_text('[target.local]\npath = "/backups"\n\n[notify.chat]\ntype = "discord"\n'
                    'url = "https://discord.com/api/webhooks/1/x"\nevents = ["backup-ok"]\n')
    cfg = load_config(path)
    assert cfg.notify["chat"].type == "discord" and cfg.notify["chat"].events == ["backup-ok"]
    path.write_text('[target.local]\npath = "/b"\n[notify.chat]\ntype = "discord"\n')
    with pytest.raises(ConfigError, match=r"\[notify.chat\]: url or url_file is required"):
        load_config(path)
    path.write_text('[target.local]\npath = "/b"\n[scheduler]\ndisk_alert_percent = 120\n')
    with pytest.raises(ConfigError, match="disk_alert_percent must be a whole number from 0 to 100"):
        load_config(path)


def test_example_configuration_has_a_channel():
    from pathlib import Path

    cfg = load_config(Path(__file__).resolve().parents[1] / "antigrafa.example.toml")
    assert cfg.notify["admins"].type == "email" and cfg.notify["admins"].to == ["admin@example.lan"]


def test_secrets_from_files(tmp_path):
    (tmp_path / "hook").write_text("https://example.org/hook\n")
    (tmp_path / "pw").write_text("s3cret\n")
    assert _channel(type="teams", url_file=str(tmp_path / "hook")).webhook() == "https://example.org/hook"
    assert _channel(**EMAIL, password_file=str(tmp_path / "pw")).login_password() == "s3cret"
    (tmp_path / "hook").write_text("http://example.org/hook")
    with pytest.raises(notify.NotifyError, match="must start with https://"):
        _channel(type="teams", url_file=str(tmp_path / "hook")).webhook()


# --------------------------------------------------------------------------- messages

FAILED = Notification("backup-failed", "failed", "Job nightly failed: pvx1 -> qnap",
                      ["pvx1 -> qnap: 8 guest(s) OK, 1 failed", "FAILED VM 701 (win): timeout", "Total: 9 guest(s)"])


def test_discord_message():
    [embed] = notify.discord_payload(FAILED)["embeds"]
    assert embed["title"] == "❌ Job nightly failed: pvx1 -> qnap" and embed["color"] == 0xC4352B
    assert embed["description"].startswith("pvx1 -> qnap: 8 guest(s) OK, 1 failed\n```\nFAILED VM 701")
    assert embed["footer"]["text"].startswith("Antigrafa ")


def test_long_details_are_cut_on_whole_lines():
    n = Notification("backup-ok", "ok", "t", ["summary"] + [f"line {i} " + "x" * 90 for i in range(200)])
    [embed] = notify.discord_payload(n)["embeds"]
    assert len(embed["description"]) < 4096 and "more line(s) in the log" in embed["description"]


def test_teams_card():
    payload = notify.teams_payload(FAILED)
    [attachment] = payload["attachments"]
    assert payload["type"] == "message" and attachment["contentType"] == "application/vnd.microsoft.card.adaptive"
    title, summary, details, footer = attachment["content"]["body"]
    assert title["text"] == "Job nightly failed: pvx1 -> qnap" and title["color"] == "Attention"
    assert summary["text"] == "pvx1 -> qnap: 8 guest(s) OK, 1 failed"
    assert details["fontType"] == "Monospace" and "FAILED VM 701" in details["text"]
    assert footer["isSubtle"]


def test_email_message():
    msg = notify.email_message(_channel(**dict(EMAIL, to=["a@lan", "b@lan"])), FAILED)
    assert msg["Subject"] == "[Antigrafa] Job nightly failed: pvx1 -> qnap"
    assert msg["From"] == "ag@lan" and msg["To"] == "a@lan, b@lan"
    assert "FAILED VM 701 (win): timeout" in msg.get_content()


def _result(label, ok, error=""):
    return SimpleNamespace(ok=ok, error=error, guest=SimpleNamespace(label=label))


def test_backup_and_restore_events():
    ok = notify.backup_notification("ok", source="pvx1", target="qnap", results=[_result("VM 1", True)],
                                    report=["", "Total: 1 guest(s)"], job="nightly", by="scheduler")
    assert (ok.event, ok.status, ok.title) == ("backup-ok", "ok", "Job nightly succeeded: pvx1 -> qnap")
    assert ok.lines == ["pvx1 -> qnap: 1 guest(s) OK, 0 failed (started by the scheduler)", "Total: 1 guest(s)"]
    failed = notify.backup_notification("failed", source="pvx1", target="qnap",
                                        results=[_result("VM 1", True), _result("VM 2", False, "no space")],
                                        report=[], by="web", user="alice")
    assert failed.event == "backup-failed" and failed.title == "Backup failed: pvx1 -> qnap"
    assert "FAILED VM 2: no space" in failed.lines and "started from the web by alice" in failed.lines[0]
    cancelled = notify.backup_notification("cancelled", source="a", target="b", results=[], report=[], error="stop")
    assert (cancelled.event, cancelled.status) == ("backup-failed", "warning") and "Error: stop" in cancelled.lines
    restored = notify.restore_notification("failed", backup="x/vm/1/t", target="qnap", guest="VM 1", error="taken")
    assert (restored.event, restored.status, restored.title) == ("restore", "failed", "Restore failed: VM 1")


# --------------------------------------------------------------------------- sending


class Hook(http.server.BaseHTTPRequestHandler):
    received: list = []
    status = 202

    def do_POST(self):
        Hook.received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        self.send_response(Hook.status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def hook():
    server = http.server.HTTPServer(("127.0.0.1", 0), Hook)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Hook.received, Hook.status = [], 202
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


def test_post_json(hook):
    notify.post_json(hook, {"a": 1})
    assert Hook.received == [{"a": 1}]
    Hook.status = 500
    with pytest.raises(Exception, match="500"):
        notify.post_json(hook, {"a": 2})


def test_send_picks_channels_by_event_and_never_raises(monkeypatch, caplog):
    sent = []

    def deliver(channel, n):
        if channel.name == "broken":
            raise OSError("connection refused")
        sent.append(channel.name)

    monkeypatch.setattr(notify, "deliver", deliver)
    channels = {
        "all": _channel("all", **EMAIL, events=list(notify.EVENTS)),
        "failures": _channel("failures", **EMAIL, events=["backup-failed"]),
        "broken": _channel("broken", **EMAIL, events=["backup-ok"]),
    }
    ok = Notification("backup-ok", "ok", "fine")
    assert notify.send(channels, ok) == {"broken": "connection refused"}
    assert sent == ["all"] and "notification to broken (email) not sent: connection refused" in caplog.text
    sent.clear()
    assert notify.send(channels, notify.test_notification("failures"), only="failures") == {}
    assert sent == ["failures"]  # a test goes to the channel named, whatever its events


def test_email_through_smtp(monkeypatch):
    calls = []

    class SMTP:
        def __init__(self, host, port, timeout, **kwargs):
            calls.append(("connect", type(self).__name__, host, port))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            calls.append(("quit",))

        def starttls(self, context):
            calls.append(("starttls",))

        def login(self, user, password):
            calls.append(("login", user, password))

        def send_message(self, msg):
            calls.append(("send", msg["To"]))

    class SMTP_SSL(SMTP):
        pass

    monkeypatch.setattr(notify.smtplib, "SMTP", SMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", SMTP_SSL)
    notify.deliver(_channel(**EMAIL, username="ag", password="pw"), FAILED)
    assert calls == [("connect", "SMTP", "smtp.lan", 587), ("starttls",), ("login", "ag", "pw"),
                     ("send", "admin@lan"), ("quit",)]
    calls.clear()
    notify.deliver(_channel(**EMAIL, security="ssl"), FAILED)  # no login without a user name
    assert calls == [("connect", "SMTP_SSL", "smtp.lan", 465), ("send", "admin@lan"), ("quit",)]


def test_webhooks_get_their_own_format(monkeypatch):
    posted = []
    monkeypatch.setattr(notify, "post_json", lambda url, payload: posted.append((url, payload)))
    notify.deliver(_channel(type="discord", url="https://discord.example/1"), FAILED)
    notify.deliver(_channel(type="teams", url="https://teams.example/2"), FAILED)
    assert posted[0][0] == "https://discord.example/1" and "embeds" in posted[0][1]
    assert posted[1][0] == "https://teams.example/2" and posted[1][1]["type"] == "message"


def test_notify_test_command(monkeypatch, capsys):
    monkeypatch.setattr(notify, "deliver", lambda c, n: (_ for _ in ()).throw(OSError("refused")) if c.name == "b" else None)
    cfg = SimpleNamespace(notify={"a": _channel("a", **EMAIL), "b": _channel("b", type="discord", url="https://x/y")})
    assert cli.cmd_notify_test(cfg, SimpleNamespace(name=None)) == 1
    assert capsys.readouterr().out == "a (email): sent\nb (discord): not sent: refused\n"
    assert cli.cmd_notify_test(cfg, SimpleNamespace(name="a")) == 0
    with pytest.raises(ConfigError, match="unknown channel c"):
        cli.cmd_notify_test(cfg, SimpleNamespace(name="c"))
    assert cli.cmd_notify_test(SimpleNamespace(notify={}), SimpleNamespace(name=None)) == 1


# --------------------------------------------------------------------------- system checks


def _cfg(tmp_path, **targets):
    return Config(sources={"pvx": SimpleNamespace(name="pvx")}, tuning=Tuning(),
                  targets={n: TargetConfig(name=n, path=p) for n, p in targets.items()},
                  scheduler=SchedulerConfig(data_dir=str(tmp_path / "sched")),
                  notify={"ops": _channel(**EMAIL, events=["system"])})


def test_find_problems(tmp_path, monkeypatch):
    (tmp_path / "full").mkdir()
    cfg = _cfg(tmp_path, gone=str(tmp_path / "missing"), full=str(tmp_path / "full"))
    monkeypatch.setattr(monitor, "target_usage", lambda t: (_ for _ in ()).throw(OSError(f"{t.path} does not exist"))
                        if t.name == "gone" else {"total": 100, "used": 95, "free": 5})
    platform = SimpleNamespace(hosts=lambda: [{"name": "pvx1", "status": "online"}, {"name": "pvx3", "status": "offline"}])
    monkeypatch.setattr(monitor, "open_platform", lambda cfg, name: platform)
    problems = monitor.find_problems(cfg)
    assert problems == {
        "target:gone": f"destination gone: {tmp_path / 'missing'} does not exist",
        "target-full:full": "destination full: 95% full, 5 B free",
        "node:pvx:pvx3": "node pvx3 of pvx: offline",
    }
    monkeypatch.setattr(monitor, "open_platform", lambda cfg, name: (_ for _ in ()).throw(OSError("refused")))
    assert monitor.find_problems(cfg)["source:pvx"] == "source pvx: refused"


def test_a_problem_is_notified_when_it_appears_and_when_it_is_gone(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    now = [datetime(2026, 11, 27, 10, 0, tzinfo=timezone.utc)]
    watch = monitor.SystemWatch(send=lambda channels, n: sent.append(n), now=lambda: now[0])
    first = watch.check(cfg, {"node:pvx:pvx3": "node pvx3 of pvx: offline"})
    assert first.event == "system" and first.status == "warning" and first.title.startswith("1 new problem(s)")
    assert "NEW     node pvx3 of pvx: offline" in first.lines
    # Known: nothing more, even from a scheduler started again
    assert monitor.SystemWatch(send=lambda c, n: sent.append(n)).check(cfg, {"node:pvx:pvx3": "x"}) is None
    now[0] = datetime(2026, 11, 27, 11, 0, tzinfo=timezone.utc)
    solved = watch.check(cfg, {})
    assert solved.status == "ok" and solved.title.startswith("Problem(s) solved")
    assert solved.lines == ["Nothing wrong now.", "SOLVED  x (since 2026-11-27 10:00)"]
    assert len(sent) == 2


def test_scheduler_tells_it_stopped_and_an_unexpected_stop(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    events = []
    monkeypatch.setattr(monitor, "scheduler_event", lambda cfg, title, lines: events.append(title))
    s = sched.Scheduler("x", load=lambda path: cfg, tick=0.05,
                        watch=SimpleNamespace(check=lambda cfg: events.append("checked")))
    cfg.scheduler.directory.mkdir(parents=True)
    (cfg.scheduler.directory / sched.STATUS_FILE).write_text(json.dumps({"heartbeat": "2026-11-27T03:12:00+00:00"}))
    thread = threading.Thread(target=s.serve)
    thread.start()
    try:
        for _ in range(200):
            if "checked" in events:
                break
            threading.Event().wait(0.01)
    finally:
        s.stop.set()
        thread.join(10)
    assert events == ["The scheduler started again after an unexpected stop", "checked", "The scheduler stopped"]
    events.clear()
    s2 = sched.Scheduler("x", load=lambda path: cfg)
    s2._previous_stop(cfg)  # it stopped properly this time: nothing to say
    assert events == []


def test_job_end_is_notified(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, local=str(tmp_path / "repo"))
    sent = []
    monkeypatch.setattr(notify, "send", lambda channels, n: sent.append(n))
    monkeypatch.setattr(sched, "prepare_backup", lambda *a: (_ for _ in ()).throw(OSError("pvx1 unreachable")))
    job = SimpleNamespace(name="nightly", source="pvx", target="local", guests=[], all=True, exclude=[])
    result, _ = sched.execute_job(cfg, job, by="scheduler")
    assert result["state"] == "failed"
    [n] = sent
    assert n.event == "backup-failed" and n.title == "Job nightly failed: pvx -> local"
    assert "Error: pvx1 unreachable" in n.lines
