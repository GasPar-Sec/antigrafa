"""One run at a time across processes: the scheduler and the web interface.

``run.lock`` in the scheduler's directory is locked (an OS lock: released by the system
if the process dies) by whoever runs a backup or a restore; ``run.json`` next to it says
what runs, for the other side's messages. A job due while another run is in progress
waits for the lock; the web interface refuses to start (it says what runs).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

LOCK_FILE = "run.lock"
INFO_FILE = "run.json"


def _try_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def write_json(path: Path, data: dict) -> None:
    """Replace ``path`` at once: a reader never sees half a file."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader has it open for a moment
            if attempt == 4:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.05)


def _read_text(path: Path) -> str | None:
    for _ in range(4):
        try:
            return path.read_text(encoding="utf-8")
        except PermissionError:  # Windows: being replaced by its writer at this very moment
            time.sleep(0.02)
        except OSError:
            return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def read_json(path: Path) -> dict | None:
    text = _read_text(path)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def describe(info: dict | None) -> str:
    """'scheduled job nightly', 'backup #3 of the web interface'..."""
    if not info:
        return "another backup or restore"
    if info.get("job"):
        where = "the scheduler" if info.get("by") == "scheduler" else "the web interface" \
            if info.get("by") == "web" else "the command line"
        return f"job {info['job']} (started by {where})"
    what = info.get("what") or "run"
    run = f" #{info['run']}" if info.get("run") else ""
    return f"{what}{run} of the web interface" if info.get("by") == "web" else what


class RunLock:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self._fd: int | None = None
        self._info: dict | None = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def try_acquire(self, info: dict) -> bool:
        """Take the lock if it is free; ``info`` says what runs. Raises OSError if the
        directory cannot be created or written."""
        if self._fd is not None:
            raise RuntimeError("run lock already held")
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.directory / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
        if not _try_lock(fd):
            os.close(fd)
            return False
        self._fd = fd
        self._info = {"pid": os.getpid(), "since": datetime.now().astimezone().isoformat(), **info}
        try:
            write_json(self.directory / INFO_FILE, self._info)
        except BaseException:
            self.release()
            raise
        return True

    def acquire(self, info: dict, stop: threading.Event, poll: float = 5.0, on_wait=None) -> bool:
        """Wait for the lock; False if ``stop`` is set first. ``on_wait(holder)`` is called
        once, when it has to wait."""
        waited = False
        while not stop.is_set():
            if self.try_acquire(info):
                return True
            if not waited and on_wait is not None:
                on_wait(self.holder())
            waited = True
            stop.wait(poll)
        return False

    def release(self) -> None:
        fd, self._fd, self._info = self._fd, None, None
        if fd is None:
            return
        try:
            (self.directory / INFO_FILE).unlink(missing_ok=True)
        except OSError:  # pragma: no cover - the lock is released anyway
            pass
        try:
            _unlock(fd)
        finally:
            os.close(fd)

    def holder(self) -> dict | None:
        """What runs (here or in another process); None when nothing does."""
        if self._info is not None:
            return dict(self._info)
        path = self.directory / LOCK_FILE
        if not path.exists():
            return None
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            return None
        try:
            if _try_lock(fd):  # free: nobody runs
                _unlock(fd)
                return None
        finally:
            os.close(fd)
        return read_json(self.directory / INFO_FILE) or {}
