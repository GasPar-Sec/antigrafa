"""Recurring jobs: the run lock shared between processes, job states, the scheduler and
the ``scheduler`` / ``job`` commands."""

import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from antigrafa import cli, operations
from antigrafa import scheduler as sched
from antigrafa.config import Config, ConfigError, JobConfig, SchedulerConfig, Tuning
from antigrafa.platforms.base import Guest
from antigrafa.runlock import RunLock, describe
from antigrafa.targets import TargetConfig
from test_jobs import MemoryDisk, MemoryPlatform

GUESTS = [Guest("1", "vm", "web", "h1", "running"), Guest("2", "ct", "dns", "h2", "stopped")]


def local(*args):
    return datetime(*args).astimezone()


# --------------------------------------------------------------------------- run lock


def test_run_lock_one_holder_at_a_time(tmp_path):
    first, second = RunLock(tmp_path / "s"), RunLock(tmp_path / "s")
    assert first.holder() is None and second.holder() is None  # nothing there yet
    assert first.try_acquire({"by": "scheduler", "job": "nightly", "what": "backup"})
    assert first.held and (tmp_path / "s" / "run.lock").exists()
    assert not second.try_acquire({"by": "web", "what": "backup"})  # another holder, same process
    holder = second.holder()
    assert holder["job"] == "nightly" and holder["pid"] == os.getpid() and holder["since"]
    assert first.holder()["job"] == "nightly"
    with pytest.raises(RuntimeError, match="already held"):
        first.try_acquire({})
    first.release()
    first.release()  # twice: nothing
    assert second.holder() is None and not (tmp_path / "s" / "run.json").exists()
    assert second.try_acquire({"by": "web", "what": "restore"})
    second.release()


def test_run_lock_waits_for_the_holder(tmp_path):
    holder, waiter = RunLock(tmp_path), RunLock(tmp_path)
    holder.try_acquire({"by": "web", "what": "restore", "run": 4})
    waits = []
    threading.Timer(0.3, holder.release).start()
    assert waiter.acquire({"by": "scheduler", "job": "j"}, threading.Event(), poll=0.05, on_wait=waits.append)
    assert [w["what"] for w in waits] == ["restore"]  # said once, not at every poll
    stop = threading.Event()
    threading.Timer(0.2, stop.set).start()
    assert not RunLock(tmp_path).acquire({}, stop, poll=0.05)  # stopping while it waits
    waiter.release()


def test_run_lock_across_processes_and_released_when_the_process_dies(tmp_path):
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(os.path.join(os.path.dirname(__file__), "..", "src"))!r})
        from antigrafa.runlock import RunLock
        lock = RunLock(sys.argv[1])
        assert lock.try_acquire({{"by": "cli", "job": "other"}})
        print("locked", flush=True)
        sys.stdin.readline()
    """)
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "locked"
        ours = RunLock(tmp_path)
        assert not ours.try_acquire({"by": "web"})
        holder = ours.holder()
        # Not child.pid: on Windows, a venv's python.exe starts the real interpreter
        assert holder["pid"] != os.getpid() and holder["job"] == "other"
    finally:
        child.kill()  # no release: the system frees the lock
        child.wait(10)
        child.stdin.close()
    # Windows: the venv's python.exe is a launcher; the interpreter it started, which
    # holds the lock, ends a moment later (its standard input closed)
    deadline = time.monotonic() + 10
    while not ours.try_acquire({"by": "web"}):
        assert time.monotonic() < deadline, "the lock of a dead process was never freed"
        time.sleep(0.05)
    ours.release()


def test_unwritable_lock_directory_raises(tmp_path):
    (tmp_path / "file").write_text("x")
    with pytest.raises(OSError):
        RunLock(tmp_path / "file" / "s").try_acquire({})
    assert RunLock(tmp_path / "file" / "s").holder() is None


def test_read_json_tries_again_while_the_file_is_replaced(tmp_path, monkeypatch):
    from pathlib import Path

    import antigrafa.runlock as runlock

    path = tmp_path / "s.json"
    runlock.write_json(path, {"a": 1})
    real = Path.read_text
    refusals = [PermissionError("being replaced")] * 2

    def read_text(self, *args, **kwargs):
        if refusals:
            raise refusals.pop()
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    assert runlock.read_json(path) == {"a": 1}  # Windows: its writer replaces it at that moment
    refusals.extend([PermissionError("held")] * 5)
    assert runlock.read_json(path) is None  # held all along: as missing
    refusals.clear()
    path.write_text("[1]")
    assert runlock.read_json(path) is None and runlock.read_json(tmp_path / "none.json") is None


def test_describe_the_holder():
    assert describe(None) == "another backup or restore"
    assert describe({"by": "scheduler", "job": "nightly"}) == "job nightly (started by the scheduler)"
    assert describe({"by": "cli", "job": "n"}) == "job n (started by the command line)"
    assert describe({"by": "web", "job": "n"}) == "job n (started by the web interface)"
    assert describe({"by": "web", "what": "restore", "run": 3}) == "restore #3 of the web interface"
    assert describe({"by": "web", "what": "backup"}) == "backup of the web interface"
    assert describe({}) == "another backup or restore"


# --------------------------------------------------------------------------- jobs and their state


def _job(name="nightly", **keys):
    keys.setdefault("all", True)
    if not {"schedule", "every", "at"} & set(keys):
        keys["schedule"] = "0 2 * * *"
    return JobConfig(name=name, **keys)


@pytest.fixture
def lab(tmp_path, monkeypatch):
    (tmp_path / "repo").mkdir()
    platform = MemoryPlatform(GUESTS, {"1": [MemoryDisk("disk0", os.urandom(200_000))],
                                       "2": [MemoryDisk("rootfs", b"r" * 5000)]})
    platform.check_access = lambda guests: platform.log.append(("checked", [g.id for g in guests]))
    cfg = Config(
        sources={"lab": SimpleNamespace(name="lab")},
        targets={"local": TargetConfig(name="local", path=str(tmp_path / "repo"))},
        tuning=Tuning(chunk_size_mib=1),
        jobs={"nightly": _job(), "web-only": _job("web-only", all=False, guests=["1"], every="6h")},
        scheduler=SchedulerConfig(data_dir=str(tmp_path / "sched")),
    )
    monkeypatch.setattr(operations, "open_platform", lambda c, name: platform)
    return SimpleNamespace(cfg=cfg, platform=platform, dir=tmp_path / "sched")


def test_execute_job_backs_up_and_records_the_result(lab, caplog):
    caplog.set_level(logging.INFO)
    result, lines = sched.execute_job(lab.cfg, lab.cfg.jobs["nightly"], by="cli", user="")
    assert result["state"] == "ok" and result["by"] == "cli"
    assert (result["guests_total"], result["guests_ok"], result["guests_failed"]) == (2, 2, 0)
    assert ("checked", ["1", "2"]) in lab.platform.log  # every right checked first
    assert any(line.startswith("Total: 2 guest(s), 2 OK") for line in lines)
    state = json.loads((lab.dir / "jobs" / "nightly.json").read_text())
    assert state["last"] == result and state["last_start"] == result["started"]
    assert "job nightly: 2 guest(s) of lab to local" in caplog.text and "job nightly ended: ok" in caplog.text


def test_execute_job_that_fails_records_why(lab, caplog):
    job = _job("gone", all=False, guests=["99"])
    result, lines = sched.execute_job(lab.cfg, job, by="scheduler")
    assert result["state"] == "failed" and "guests not found: 99" in result["error"] and lines == []
    assert sched.read_job_state(lab.dir, "gone")["last"]["state"] == "failed"
    assert "job gone failed: guests not found: 99" in caplog.text


def test_cancelled_job(lab):
    cancel = threading.Event()
    cancel.set()
    result, _ = sched.execute_job(lab.cfg, lab.cfg.jobs["nightly"], by="scheduler", cancel=cancel)
    assert result["state"] == "cancelled"


def test_state_that_cannot_be_written_is_only_a_warning(lab, caplog, monkeypatch):
    def refuse(*args):
        raise PermissionError("read-only")

    monkeypatch.setattr(sched, "write_job_state", refuse)
    result, _ = sched.execute_job(lab.cfg, lab.cfg.jobs["nightly"], by="cli")
    assert result["state"] == "ok" and "cannot write its state" in caplog.text


def test_job_summaries_and_scheduler_status(lab):
    now = local(2026, 11, 22, 10, 0)
    sched.record_start(lab.dir, "web-only", now - timedelta(hours=1))
    nightly, web_only = sched.job_summaries(lab.cfg, now)
    assert nightly["name"] == "nightly" and nightly["what"] == "every guest of the source -> the target"
    assert nightly["schedule"] == "cron 0 2 * * *" and nightly["last"] is None
    assert datetime.fromisoformat(nightly["next"]) == local(2026, 11, 23, 2, 0)
    assert web_only["what"] == "guest 1 of the source -> the target"
    assert datetime.fromisoformat(web_only["next"]) == now + timedelta(hours=5)
    lab.cfg.jobs["nightly"].enabled = False
    assert sched.job_summaries(lab.cfg, now)[0]["next"] is None

    assert sched.scheduler_status(lab.dir, now)["alive"] is False  # never started
    (lab.dir / "scheduler.json").write_text(json.dumps(
        {"pid": 5, "started": now.isoformat(), "heartbeat": (now - timedelta(seconds=20)).isoformat(),
         "queue": ["a"], "running": "nightly"}))
    status = sched.scheduler_status(lab.dir, now)
    assert status["alive"] and status["running"] == "nightly" and status["queue"] == ["a"]
    assert not sched.scheduler_status(lab.dir, now + timedelta(minutes=5))["alive"]  # no heartbeat since


def test_describe_job():
    assert sched.describe_job(_job(source="lab", target="nas", exclude=["9", "10"])) == \
        "every guest of lab, except 9, 10 -> nas"
    assert sched.describe_job(_job(all=False, guests=["1", "2"], source="lab", target="nas")) == \
        "guests 1, 2 of lab -> nas"


# --------------------------------------------------------------------------- scheduler


class Clock:
    def __init__(self, moment):
        self.moment = moment

    def __call__(self):
        return self.moment


@pytest.fixture
def scheduler(lab):
    clock = Clock(local(2026, 11, 22, 1, 0))
    ran = []

    def execute(cfg, job, **kwargs):
        ran.append((job.name, kwargs["by"]))
        result = sched.job_result(clock(), "ok", 1, [], by=kwargs["by"])
        sched.record_start(cfg.scheduler.directory, job.name, clock())
        sched.record_end(cfg.scheduler.directory, job.name, result)
        return result, []

    s = sched.Scheduler("antigrafa.toml", load=lambda path: lab.cfg, execute=execute, now=clock, tick=0.05,
                        lock_poll=0.05)
    return SimpleNamespace(s=s, clock=clock, ran=ran, lab=lab)


def test_cron_job_is_queued_at_its_time_only(scheduler, caplog):
    caplog.set_level(logging.INFO)
    s, clock = scheduler.s, scheduler.clock
    del scheduler.lab.cfg.jobs["web-only"]
    assert s.check() == []  # 01:00: next is 02:00
    clock.moment = local(2026, 11, 22, 1, 59)
    assert s.check() == []
    clock.moment = local(2026, 11, 22, 2, 0, 10)
    assert s.check() == ["nightly"] and s.queue() == ["nightly"]
    assert "job nightly is due (cron 0 2 * * *): queued" in caplog.text
    clock.moment = local(2026, 11, 22, 2, 1)
    assert s.check() == []  # next: tomorrow
    clock.moment = local(2026, 11, 23, 2, 0)
    assert s.check() == [] and s.queue() == ["nightly"]  # still waiting: never queued twice
    assert "still waits in the queue: not queued twice" in caplog.text
    status = json.loads((scheduler.lab.dir / "scheduler.json").read_text())
    assert status["queue"] == ["nightly"] and status["pid"] == os.getpid()


def test_missed_times_are_not_caught_up_and_schedule_changes_apply(scheduler):
    s, clock, cfg = scheduler.s, scheduler.clock, scheduler.lab.cfg
    del cfg.jobs["web-only"]
    s.check()
    clock.moment = local(2026, 11, 25, 12, 0)  # the scheduler was not running for days
    assert s.check() == ["nightly"]  # once, not once per missed day
    cfg.jobs["nightly"] = _job(schedule="0 13 * * *")  # changed: counts from now
    s._queue.clear()
    assert s.check() == []
    clock.moment = local(2026, 11, 25, 13, 0)
    assert s.check() == ["nightly"]
    cfg.jobs["nightly"].enabled = False
    s._queue.clear()
    clock.moment = local(2026, 11, 26, 13, 0)
    assert s.check() == [] and "nightly" not in s._next


def test_every_job_runs_at_once_then_from_its_last_start(scheduler):
    s, clock, cfg = scheduler.s, scheduler.clock, scheduler.lab.cfg
    del cfg.jobs["nightly"]
    assert s.check() == ["web-only"]  # never ran
    assert s.check() == []  # queued
    s._queue.clear()
    s.run_job("web-only")
    assert scheduler.ran == [("web-only", "scheduler")]
    clock.moment += timedelta(hours=5, minutes=59)
    assert s.check() == []
    clock.moment += timedelta(minutes=1)
    assert s.check() == ["web-only"]


def test_every_job_whose_state_cannot_be_written_does_not_loop(scheduler, monkeypatch):
    s, clock, cfg = scheduler.s, scheduler.clock, scheduler.lab.cfg
    del cfg.jobs["nightly"]
    monkeypatch.setattr(sched, "read_job_state", lambda directory, name: {})  # nothing recorded
    s.run_job("web-only")
    assert s.check() == []  # the start kept in memory counts
    clock.moment += timedelta(hours=6)
    assert s.check() == ["web-only"]


def test_job_waits_for_the_run_in_progress(scheduler, caplog):
    caplog.set_level(logging.INFO)
    other = RunLock(scheduler.lab.dir)
    other.try_acquire({"by": "web", "what": "restore", "run": 3})
    threading.Timer(0.3, other.release).start()
    scheduler.s.run_job("nightly")
    assert scheduler.ran == [("nightly", "scheduler")]
    assert "job nightly waits for restore #3 of the web interface to end" in caplog.text


def test_removed_disabled_or_unreadable_jobs_are_not_run(scheduler, caplog):
    caplog.set_level(logging.INFO)
    s, cfg = scheduler.s, scheduler.lab.cfg
    assert s.run_job("unknown") is None
    cfg.jobs["nightly"].enabled = False
    assert s.run_job("nightly") is None
    assert scheduler.ran == [] and "job nightly not run: disabled since it was queued" in caplog.text
    s._load = lambda path: (_ for _ in ()).throw(ConfigError("bad file"))
    assert s.run_job("nightly") is None and "configuration: bad file" in caplog.text
    assert s.check() == []
    assert caplog.text.count("jobs are not checked until it is fixed") == 1
    s.check()
    assert caplog.text.count("jobs are not checked until it is fixed") == 1  # said once
    s._load = lambda path: cfg
    s.check()
    assert "configuration fixed" in caplog.text


def test_unwritable_lock_directory_skips_the_job(scheduler, caplog):
    (scheduler.lab.dir.parent / "file").write_text("x")
    scheduler.lab.cfg.scheduler = SchedulerConfig(data_dir=str(scheduler.lab.dir.parent / "file" / "s"))
    assert scheduler.s.run_job("nightly") is None
    assert "cannot take the run lock" in caplog.text and scheduler.ran == []


def test_serve_runs_the_queue_and_stops(scheduler):
    s, clock, cfg = scheduler.s, scheduler.clock, scheduler.lab.cfg
    del cfg.jobs["nightly"]  # web-only: due at once
    thread = threading.Thread(target=s.serve)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not scheduler.ran and time.monotonic() < deadline:
            time.sleep(0.02)
        assert scheduler.ran == [("web-only", "scheduler")]
        status = sched.scheduler_status(scheduler.lab.dir, clock())
        assert status["alive"]
    finally:
        s.stop.set()
        thread.join(10)
    assert not thread.is_alive()
    status = sched.scheduler_status(scheduler.lab.dir, clock())
    assert not status["alive"] and status["stopped"]


def test_serve_cancels_the_running_job_on_stop(lab):
    started = threading.Event()

    def execute(cfg, job, cancel, **kwargs):
        started.set()
        cancel.wait(10)  # the backup stops when the scheduler stops
        return sched.job_result(sched._now(), "cancelled", 1, [], by="scheduler"), []

    s = sched.Scheduler("x", load=lambda path: lab.cfg, execute=execute, tick=0.05)
    thread = threading.Thread(target=s.serve)
    thread.start()
    assert started.wait(10)
    assert RunLock(lab.dir).holder()["job"] == "web-only"
    s.stop.set()
    thread.join(10)
    assert not thread.is_alive() and RunLock(lab.dir).holder() is None


# --------------------------------------------------------------------------- commands


@pytest.fixture
def cli_lab(lab, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda path: lab.cfg)
    return lab


def test_job_list_command(cli_lab, capsys):
    assert cli.main(["job", "list"]) == 0
    out = capsys.readouterr().out
    assert "scheduler: not running" in out
    assert "nightly\n  what:  every guest of the source -> the target\n  when:  cron 0 2 * * *" in out
    assert "last:  never ran" in out
    sched.record_end(cli_lab.dir, "nightly", {"state": "failed", "started": local(2026, 11, 22, 2, 0).isoformat(),
                                              "guests_ok": 1, "guests_total": 2, "error": "boom"})
    cli_lab.cfg.jobs["web-only"].enabled = False
    (cli_lab.dir / "scheduler.json").write_text(json.dumps(
        {"pid": 7, "started": sched._now().isoformat(), "heartbeat": sched._now().isoformat(),
         "queue": ["web-only"], "running": "nightly"}))
    assert cli.main(["job", "list"]) == 0
    out = capsys.readouterr().out
    assert "running nightly, queue: web-only" in out and "(pid 7)" in out
    assert "last:  failed 2026-11-22 02:00, 1/2 OK: boom" in out
    assert "web-only (disabled)" in out
    cli_lab.cfg.jobs.clear()
    assert cli.main(["job", "list"]) == 0
    assert "no job configured" in capsys.readouterr().out


def test_job_run_command(cli_lab, caplog):
    assert cli.main(["--no-progress", "job", "run", "nightly"]) == 0
    assert sched.read_job_state(cli_lab.dir, "nightly")["last"]["by"] == "cli"
    assert RunLock(cli_lab.dir).holder() is None  # released
    assert cli.main(["--no-progress", "job", "run", "nope"]) == 2
    assert "unknown job nope (nightly, web-only)" in caplog.text
    cli_lab.cfg.jobs["bad"] = _job("bad", all=False, guests=["99"])
    assert cli.main(["--no-progress", "job", "run", "bad"]) == 1


def test_job_run_waits_for_the_lock(cli_lab, caplog):
    caplog.set_level(logging.INFO)
    other = RunLock(cli_lab.dir)
    other.try_acquire({"by": "scheduler", "job": "nightly"})
    threading.Timer(0.3, other.release).start()
    import antigrafa.runlock as runlock

    real = runlock.RunLock.acquire
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(runlock.RunLock, "acquire", lambda self, info, stop, poll=5.0, on_wait=None:
                   real(self, info, stop, 0.05, on_wait))
        assert cli.main(["--no-progress", "job", "run", "web-only"]) == 0
    assert "waiting for job nightly (started by the scheduler) to end" in caplog.text


def test_job_run_with_an_unwritable_lock_directory(cli_lab, caplog, tmp_path):
    (tmp_path / "file").write_text("x")
    cli_lab.cfg.scheduler = SchedulerConfig(data_dir=str(tmp_path / "file" / "s"))
    assert cli.main(["--no-progress", "job", "run", "nightly"]) == 2
    assert "cannot take the run lock" in caplog.text


def test_scheduler_command_stops_on_interrupt(cli_lab, monkeypatch):
    served = []

    def serve(self):
        served.append(self.config_path)
        return 0

    monkeypatch.setattr(sched.Scheduler, "serve", serve)
    assert cli.main(["-c", "my.toml", "scheduler"]) == 0
    assert served == ["my.toml"]
