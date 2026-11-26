import os
import sys
import threading

import pytest

from antigrafa import overview
from antigrafa.overview import SizeCache, directory_size, disk_usage, machine_info


def test_machine_info():
    m = machine_info()
    assert m["hostname"] and m["os"] and m["version"] and m["python"]
    assert m["cpus"] is None or m["cpus"] >= 1


def test_memory_and_uptime_from_proc(tmp_path):
    meminfo = tmp_path / "meminfo"
    meminfo.write_text("MemTotal:        4028464 kB\nMemFree:  100 kB\nMemAvailable:    2014232 kB\n")
    assert overview._memory(str(meminfo)) == {"total": 4028464 * 1024, "available": 2014232 * 1024}
    (tmp_path / "partial").write_text("MemTotal: 10 kB\n")
    assert overview._memory(str(tmp_path / "partial")) is None
    assert overview._memory(str(tmp_path / "none")) is None
    (tmp_path / "uptime").write_text("7322.51 28000.10\n")
    assert overview._uptime(str(tmp_path / "uptime")) == pytest.approx(7322.51)
    assert overview._uptime(str(tmp_path / "none")) is None


def test_disk_usage_and_root(tmp_path):
    usage = disk_usage(str(tmp_path))
    assert usage["total"] >= usage["used"] and usage["free"] > 0
    assert usage["device"] == disk_usage(str(tmp_path))["device"]
    assert os.path.isdir(overview.root_path())


def test_directory_size_counts_the_backups(tmp_path):
    (tmp_path / ".chunks" / "ab12").mkdir(parents=True)
    (tmp_path / ".chunks" / "ab12" / "ab12cd").write_bytes(b"x" * 10_000)
    snap = tmp_path / "lab" / "vm" / "100" / "2026-11-26T10-00-00Z"
    snap.mkdir(parents=True)
    (snap / "manifest.json").write_text("{}")
    (snap / "scsi0.img.fidx").write_bytes(b"i" * 100)
    size, backups = directory_size(tmp_path)
    assert size >= 10_000 and backups == 1
    if sys.platform != "win32":
        (tmp_path / "link").symlink_to("/")  # never followed: only Antigrafa's own files
        assert directory_size(tmp_path) == (size, 1)
    assert directory_size(tmp_path / "missing") == (0, 0)


def test_size_cache_measures_in_the_background_and_again_when_old():
    now = [0.0]
    started, release = threading.Event(), threading.Event()
    calls = []

    def measure(path):
        calls.append(path)
        started.set()
        release.wait(5)
        return 1234, 3

    cache = SizeCache(max_age=600, measure=measure, clock=lambda: now[0])
    first = cache.get("/backups")
    assert first == {"bytes": None, "backups": None, "age": None, "measuring": True}
    started.wait(5)
    assert cache.get("/backups")["measuring"] and calls == ["/backups"]  # one measure at a time
    release.set()
    cache.wait(5)
    now[0] = 100
    assert cache.get("/backups") == {"bytes": 1234, "backups": 3, "age": 100, "measuring": False}
    now[0] = 700  # too old: measured again, the old value shown meanwhile
    assert cache.get("/backups")["bytes"] == 1234
    cache.wait(5)
    assert calls == ["/backups", "/backups"]


def test_size_cache_survives_a_failed_measure(caplog):
    def broken(path):
        raise OSError("stale NFS handle")

    cache = SizeCache(measure=broken)
    cache.get("/nas")
    cache.wait(5)
    assert "size of /nas: stale NFS handle" in caplog.text
    assert cache.get("/nas")["measuring"]  # tried again
    cache.wait(5)


def test_proxmox_hosts():
    from antigrafa.platforms.proxmox import NodeConfig, ProxmoxPlatform, ProxmoxSettings

    class API:
        def nodes(self):
            return [{"node": "pve2", "status": "offline"}, {"node": "pve1", "status": "online"}, {"node": "pve3"}]

    settings = ProxmoxSettings(url="https://pve1:8006", token_id="a@pve!b", nodes={"pve1": NodeConfig(host="10.0.0.11")})
    platform = ProxmoxPlatform("cluster1", settings, api=API())
    assert platform.hosts() == [
        {"name": "pve1", "status": "online", "address": "10.0.0.11"},
        {"name": "pve2", "status": "offline", "address": "pve2"},
        {"name": "pve3", "status": "unknown", "address": "pve3"},
    ]
