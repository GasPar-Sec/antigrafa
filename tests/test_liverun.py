"""Jobs run by the scheduler or ``antigrafa job run``: followed, and cancelled, from the web page."""

import json
import logging
import os
import threading
import time
from datetime import timedelta
from types import SimpleNamespace

import pytest

from antigrafa import cli, liverun, operations
from antigrafa import scheduler as sched
from antigrafa.config import Config, JobConfig, SchedulerConfig, Tuning
from antigrafa.liverun import LiveRun
from antigrafa.platforms.base import Guest
from antigrafa.targets import TargetConfig
from antigrafa.web import manager as manager_module
from antigrafa.web.manager import BackupManager
from test_jobs import MemoryDisk, MemoryPlatform

GUESTS = [Guest("1", "vm", "web", "h1", "running"), Guest("2", "ct", "dns", "h2", "stopped")]


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(liverun, "PUBLISH", 0.02)


@pytest.fixture
def lab(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)  # the log lines the page shows
    (tmp_path / "repo").mkdir()
    platform = MemoryPlatform(GUESTS, {"1": [MemoryDisk("disk0", os.urandom(200_000))],
                                       "2": [MemoryDisk("rootfs", b"r" * 5000)]})
    platform.check_access = lambda guests: None
    cfg = Config(
        sources={"lab": SimpleNamespace(name="lab")},
        targets={"local": TargetConfig(name="local", path=str(tmp_path / "repo"))},
        tuning=Tuning(chunk_size_mib=1),
        jobs={"nightly": JobConfig(name="nightly", all=True, schedule="0 2 * * *")},
        scheduler=SchedulerConfig(data_dir=str(tmp_path / "sched")),
    )
    monkeypatch.setattr(operations, "open_platform", lambda c, name: platform)
    monkeypatch.setattr(manager_module, "open_platform", lambda c, name: platform)
    m = BackupManager("antigrafa.toml", load=lambda path: cfg)
    yield SimpleNamespace(cfg=cfg, platform=platform, dir=tmp_path / "sched", manager=m)
    m.shutdown(timeout=10)


def _files(lab):
    return sorted((lab.dir / "runs").glob("*.json"))


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _wait(condition, timeout=10.0):
    end = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)


def test_job_writes_its_guests_results_report_and_log(lab):
    with LiveRun(lab.dir, "nightly", by="scheduler") as live:
        result, lines = sched.execute_job(lab.cfg, lab.cfg.jobs["nightly"], by="scheduler", cancel=live.cancel,
                                          progress=live.progress, live=live)
    assert result["state"] == "ok"
    [path] = _files(lab)
    assert path == live.path and path.stem.endswith("-nightly")
    data = _read(path)
    assert (data["state"], data["job"], data["user"], data["kind"]) == ("ok", "nightly", "scheduler", "backup")
    assert (data["source"], data["target"]) == ("lab", "local")
    assert [g["id"] for g in data["guests"]] == ["1", "2"]
    assert (data["guests_total"], data["guests_done"], data["guests_ok"], data["guests_failed"]) == (2, 2, 2, 0)
    assert [r["guest"]["id"] for r in sorted(data["results"], key=lambda r: r["guest"]["id"])] == ["1", "2"]
    assert data["progress"] is None and data["finished"]  # ended: no bar any more
    assert data["report"] == lines and any(line.startswith("Total: 2 guest(s)") for line in lines)
    assert any("job nightly ended: ok" in line for line in data["logs"])
    assert data["log_total"] == len(data["logs"])
    assert not list((lab.dir / "runs").glob("*.cancel"))


def test_scheduler_job_followed_and_cancelled_from_the_page(lab):
    started = threading.Event()

    def execute(cfg, job, *, cancel, progress, live, **kwargs):
        live.begin("lab", "local", GUESTS)
        logging.getLogger("antigrafa.jobs").info("reading disk0")
        started.set()
        assert cancel.wait(10)  # the backup stops when the page cancels it
        live.finish("cancelled")
        return sched.job_result(sched._now(), "cancelled", 2, [], by="scheduler"), []

    s = sched.Scheduler("x", load=lambda path: lab.cfg, execute=execute, lock_poll=0.05)
    thread = threading.Thread(target=s.run_job, args=("nightly",))
    thread.start()
    try:
        assert started.wait(10)
        m = lab.manager
        _wait(lambda: m.status()["runs"] and m.status()["runs"][0]["guests_total"] == 2)
        status = m.status()
        [run] = status["runs"]
        assert (run["job"], run["user"], run["state"], run["external"]) == ("nightly", "scheduler", "running", True)
        assert status["busy"] and status["external"]["run"] == run["id"]  # shown as a run, not a banner
        _wait(lambda: any("reading disk0" in line for line in m.run(run["id"]).details()["logs"]))
        details = m.run(run["id"]).details()
        assert details["progress"] is not None and [g["id"] for g in details["guests"]] == ["1", "2"]
        assert "pid" not in details and "heartbeat" not in details
        assert m.run(run["id"]).details(details["log_total"])["logs"] == []  # only the new lines
        assert m.cancel(run["id"], "alice")
    finally:
        thread.join(10)
    assert not thread.is_alive()
    data = lab.manager.run(run["id"]).details()
    assert data["state"] == "cancelled" and data["cancelled_by"] == "alice"
    assert any("job nightly: cancel requested by alice" in line for line in data["logs"])
    assert not list((lab.dir / "runs").glob("*.cancel"))
    assert not lab.manager.cancel(run["id"], "bob")  # ended
    status = lab.manager.status()
    assert status["external"] is None and not status["busy"]
    assert [r["id"] for r in status["runs"]] == [run["id"]]  # same id on every look


def test_stopping_the_scheduler_cancels_its_job(lab):
    stop = threading.Event()
    with LiveRun(lab.dir, "nightly", by="scheduler", stop=stop) as live:
        stop.set()
        assert live.cancel.wait(10)
        assert (live.state, live.cancelled_by) == ("cancelling", "scheduler stopping")
    assert _read(live.path)["state"] == "cancelled"


def test_run_ended_without_a_result(lab):
    with pytest.raises(RuntimeError), LiveRun(lab.dir, "a", by="cli") as failed:
        raise RuntimeError("boom")
    data = _read(failed.path)
    assert (data["state"], data["error"], data["user"]) == ("failed", "boom", "command line")
    with pytest.raises(KeyboardInterrupt), LiveRun(lab.dir, "b", by="cli") as interrupted:
        raise KeyboardInterrupt
    assert _read(interrupted.path)["state"] == "cancelled"


def test_only_the_most_recent_runs_are_kept(lab):
    folder = lab.dir / "runs"
    folder.mkdir(parents=True)
    for i in range(25):
        (folder / f"20260101T0000{i:02d}000000-1-old.json").write_text("{}")
    (folder / "20260101T000000000000-1-old.cancel").write_text("{}")
    with LiveRun(lab.dir, "new", by="scheduler") as live:
        live.finish("ok")
    names = [p.name for p in _files(lab)]
    assert len(names) == liverun.HISTORY and names[-1] == live.path.name
    assert names[0] == "20260101T000006000000-1-old.json"  # the oldest went first
    assert not list(folder.glob("*.cancel"))


def test_a_run_whose_process_is_gone_shows_as_failed(lab):
    now = sched._now()
    data = {"state": "running", "heartbeat": (now - timedelta(seconds=liverun.STALE + 5)).isoformat(),
            "progress": {"done": 1}, "error": ""}
    lost = liverun.shown(data, now)
    assert lost["state"] == "failed" and lost["progress"] is None
    assert "the process running it stopped before the end (no news since" in lost["error"]
    fresh = {**data, "heartbeat": now.isoformat()}
    assert liverun.shown(fresh, now) is fresh
    ended = {"state": "ok"}
    assert liverun.shown(ended, now) is ended
    assert liverun.shown({"state": "cancelling"}, now)["error"] == "the process running it stopped before the end"
    # On the page: failed, and nothing to cancel
    (lab.dir / "runs").mkdir(parents=True)
    (lab.dir / "runs" / "20260101T000000000000-1-nightly.json").write_text(json.dumps(
        {**data, "kind": "backup", "job": "nightly", "started": now.isoformat(), "logs": [], "log_total": 0}))
    [run] = lab.manager.status()["runs"]
    assert run["state"] == "failed" and not lab.manager.cancel(run["id"], "alice")


def test_files_read_again_only_when_they_change(tmp_path):
    folder = tmp_path / "runs"
    folder.mkdir()
    path = folder / "a.json"
    path.write_text(json.dumps({"state": "running"}))
    (folder / "b.json").write_text("half a fi")  # never read whole: left out
    cache = {}
    assert liverun.read_runs(tmp_path, cache) == [("a", {"state": "running"})]
    path.write_text("half a fi")  # being replaced: the previous content
    os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    assert liverun.read_runs(tmp_path, cache) == [("a", {"state": "running"})]
    path.unlink()
    assert liverun.read_runs(tmp_path, cache) == [] and cache == {}
    assert liverun.read_runs(tmp_path / "none") == []


def test_unwritable_directory_is_said_once_and_the_job_runs(tmp_path, caplog):
    (tmp_path / "file").write_text("x")
    with LiveRun(tmp_path / "file", "j", by="scheduler") as live:
        live.publish()
        live.finish("ok")
    assert caplog.text.count("the web page cannot show it") == 1


def test_job_run_command_is_followed_on_the_page(lab, monkeypatch):
    monkeypatch.setattr(cli, "load_config", lambda path: lab.cfg)
    assert cli.main(["--no-progress", "job", "run", "nightly"]) == 0
    [path] = _files(lab)
    data = _read(path)
    assert (data["user"], data["state"], data["guests_ok"]) == ("command line", "ok", 2)


def test_page_lists_scheduler_runs_with_its_own_newest_first(lab):
    m = lab.manager
    earlier = (sched._now() - timedelta(hours=1)).isoformat()
    (lab.dir / "runs").mkdir(parents=True)
    (lab.dir / "runs" / "20260101T000000000000-1-nightly.json").write_text(json.dumps(
        {"kind": "backup", "job": "nightly", "user": "scheduler", "state": "ok", "started": earlier,
         "report": ["Total: 2 guest(s), 2 OK"], "logs": ["a", "b"], "log_total": 2, "pid": 1}))
    run = m.start(source="lab", target=None, every=True, user="alice")
    _wait(lambda: not run.active)
    runs = m.status()["runs"]
    assert [r["id"] for r in runs][0] == run.id and runs[1]["job"] == "nightly" and runs[1]["external"]
    details = m.run(runs[1]["id"]).details()
    assert details["report"] == ["Total: 2 guest(s), 2 OK"] and details["logs"] == ["a", "b"]
    assert m.status()["runs"][1]["id"] == runs[1]["id"]
    assert m.run(999) is None
