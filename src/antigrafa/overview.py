"""What the overview at the top of the web page shows: this machine and its disks.

Nothing here depends on Django. The space Antigrafa takes on a destination is the size
of its directory: walking every chunk takes from a second to minutes (NFS), so it is
measured in the background and kept for a while (``SizeCache``); the page shows
"measuring" until the first measure ends.
"""

from __future__ import annotations

import logging
import os
import platform
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .index import MANIFEST_NAME

log = logging.getLogger(__name__)

SIZE_MAX_AGE = 600.0  # seconds a measure of a destination stays valid


def machine_info() -> dict:
    """The machine Antigrafa runs on. Values a system does not give are None."""
    return {
        "hostname": socket.gethostname(),
        "os": _os_name(),
        "version": __version__,
        "python": platform.python_version(),
        "cpus": os.cpu_count(),
        "uptime": _uptime(),
        "load": os.getloadavg()[0] if hasattr(os, "getloadavg") else None,
        "memory": _memory(),
    }


def _os_name() -> str:
    try:
        return platform.freedesktop_os_release()["PRETTY_NAME"]
    except (OSError, KeyError, AttributeError):
        return f"{platform.system()} {platform.release()}".strip()


def _uptime(path: str = "/proc/uptime") -> float | None:
    try:
        with open(path) as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _memory(path: str = "/proc/meminfo") -> dict | None:
    """Total and available memory, in bytes (Linux)."""
    values = {}
    try:
        with open(path) as f:
            for line in f:
                name, _, rest = line.partition(":")
                if name in ("MemTotal", "MemAvailable"):
                    values[name] = int(rest.split()[0]) * 1024  # kB
    except (OSError, ValueError, IndexError):
        return None
    if len(values) != 2:
        return None
    return {"total": values["MemTotal"], "available": values["MemAvailable"]}


def root_path() -> str:
    """The system disk: / or, on Windows, the drive Windows is on."""
    if sys.platform == "win32":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


def disk_usage(path: str) -> dict:
    """Total, used and free bytes of the filesystem holding ``path``, and its device id
    (two paths on the same filesystem have the same one)."""
    usage = shutil.disk_usage(path)
    return {"total": usage.total, "used": usage.used, "free": usage.free, "device": os.stat(path).st_dev}


def directory_size(path: str | os.PathLike) -> tuple[int, int]:
    """Bytes the files under ``path`` take on disk, and the number of complete backups
    (manifests) among them. Files that vanish meanwhile (a backup running) are skipped."""
    total = backups = 0
    pending = [Path(path)]
    while pending:
        try:
            entries = os.scandir(pending.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        # Space really used: blocks where the system gives them (sparse files)
                        blocks = getattr(st, "st_blocks", None)
                        total += blocks * 512 if blocks is not None else st.st_size
                        backups += entry.name == MANIFEST_NAME
                except OSError:
                    continue
    return total, backups


class SizeCache:
    """Sizes of directories, measured in the background, each at most every ``max_age``."""

    def __init__(self, max_age: float = SIZE_MAX_AGE, measure=directory_size, clock=time.monotonic) -> None:
        self.max_age = max_age
        self._measure = measure
        self._clock = clock
        self._lock = threading.Lock()
        self._sizes: dict[str, tuple[int, int, float]] = {}  # path -> bytes, backups, when
        self._running: dict[str, threading.Thread] = {}

    def get(self, path: str) -> dict:
        """The last measure of ``path`` (``bytes`` and ``backups`` None until the first one
        ends); starts a new one when there is none or it is too old."""
        with self._lock:
            known = self._sizes.get(path)
            stale = known is None or self._clock() - known[2] > self.max_age
            if stale and path not in self._running:
                thread = threading.Thread(target=self._run, args=(path,), name="measure-target", daemon=True)
                self._running[path] = thread
                thread.start()
            measuring = path in self._running
        if known is None:
            return {"bytes": None, "backups": None, "age": None, "measuring": measuring}
        return {"bytes": known[0], "backups": known[1], "age": self._clock() - known[2], "measuring": measuring}

    def wait(self, timeout: float | None = None) -> None:
        """Until the measures in progress end (tests)."""
        with self._lock:
            threads = list(self._running.values())
        for thread in threads:
            thread.join(timeout)

    def _run(self, path: str) -> None:
        try:
            size, backups = self._measure(path)
        except Exception as exc:  # never kills the page: the next call tries again
            log.warning("size of %s: %s", path, exc)
            with self._lock:
                self._running.pop(path, None)
            return
        with self._lock:
            self._sizes[path] = (size, backups, self._clock())
            self._running.pop(path, None)
