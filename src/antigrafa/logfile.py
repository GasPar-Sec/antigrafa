"""Log file with size-based archiving (the ``[log]`` section).

The log goes to one ``.log`` file. When it reaches ``max_size_mib``, it is renamed
and compressed in the background (zip, tar.gz, rar or 7z) next to it, under a timestamped
name: ``antigrafa-2026-11-20T10-15-00.log.zip``. Only the ``max_archives`` most recent
archives are kept.

Several processes (a backup started by cron, the web interface) may write the same
file. Each record is appended with the file opened and closed again: no process keeps
it open, so any of them can archive it (Windows refuses to rename an open file), and
the others simply write to the new file from their next record.
"""

from __future__ import annotations

import logging
import os
import shutil
# subprocess runs the rar command (argument list, never a shell)
import subprocess  # nosec B404
import tarfile
import threading
import time
import zipfile
from datetime import datetime
from pathlib import Path

COMPRESSIONS = {"zip": ".zip", "tar.gz": ".tar.gz", "rar": ".rar", "7z": ".7z"}
# Highest level of each compression (zip, gzip and 7z -mx: 9, rar -m: 5)
MAX_LEVELS = {"zip": 9, "tar.gz": 9, "rar": 5, "7z": 9}
DEFAULT_LEVELS = {"zip": 6, "tar.gz": 6, "rar": 3, "7z": 5}
# Compressions written by an outside program (Python cannot write them):
# names looked up in the PATH, folder under Program Files on Windows, what to install
PROGRAMS = {
    "rar": (("rar",), ("WinRAR", "Rar.exe"), "rar, or WinRAR on Windows"),
    "7z": (("7z", "7zz", "7za"), ("7-Zip", "7z.exe"), "7zip or p7zip, or 7-Zip on Windows"),
}
STAMP = "%Y-%m-%dT%H-%M-%S"
# File records: the date, the process (several may share the file) and the thread
FILE_FORMAT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(process)d %(threadName)s] %(name)s: %(message)s"
FILE_DATEFMT = "%Y-%m-%d %H:%M:%S"
# Seconds before trying again when the file could not be renamed (Windows: another
# process was writing a record at that very moment)
RETRY_DELAY = 5.0

log = logging.getLogger(__name__)


def find_program(
    compression: str, windows: bool = os.name == "nt", environ=os.environ, which=shutil.which
) -> list[str] | None:
    """The program that writes ``compression`` (rar, 7z), None if not installed.

    Looked up in the PATH, then in its usual folder under Program Files on Windows.
    """
    names, folder, _ = PROGRAMS[compression]
    for name in names:
        found = which(name)
        if found:
            return [found]
    if windows:
        for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            base = environ.get(var)
            if base and os.path.isfile(os.path.join(base, *folder)):
                return [os.path.join(base, *folder)]
    return None


def _run(argv: list[str], what: str, stdin=None) -> None:
    # Argument list, never a shell; the paths are the handler's own
    done = subprocess.run(argv, stdin=stdin, capture_output=True)  # nosec B603
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).decode(errors="replace").strip()
        raise OSError(f"{what} failed (exit {done.returncode}): {detail}")


class ArchivingFileHandler(logging.Handler):
    def __init__(
        self,
        path: str | os.PathLike,
        max_bytes: int,
        max_archives: int,
        compression: str = "zip",
        level: int = 6,
    ) -> None:
        if compression not in COMPRESSIONS:
            raise ValueError(f"unknown compression {compression!r} ({', '.join(COMPRESSIONS)})")
        if not 0 <= level <= MAX_LEVELS[compression]:
            raise ValueError(f"{compression}: level must be between 0 and {MAX_LEVELS[compression]}, not {level}")
        super().__init__()
        self.path = Path(path).absolute()
        self.max_bytes = max_bytes
        self.max_archives = max_archives
        self.compression = compression
        self.compresslevel = level
        self._program: list[str] | None = None
        self.warning = ""  # for the caller to log, once the handler is in place
        if compression in PROGRAMS:
            self._program = find_program(compression)
            if self._program is None:
                # Never lose the log for want of a program: zip is always there
                self.warning = f"{compression} not found (install {PROGRAMS[compression][2]}): archives in zip"
                self.compression = "zip"
        self._worker: threading.Thread | None = None
        self._next_try = 0.0
        # Errors of the archiving thread, written with the next record: that thread
        # never logs itself (close() waits for it while holding the handler lock)
        self._failures: list[str] = []
        self.setFormatter(logging.Formatter(FILE_FORMAT, datefmt=FILE_DATEFMT))

    def check(self) -> None:
        """Create the directory and the file now: an error shows at start, not later."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8"):
            pass

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            if self._should_roll():
                self._roll()
            lines = []
            while self._failures:
                message = self._failures.pop(0)
                lines.append(self.format(log.makeRecord(log.name, logging.ERROR, __file__, 0, message, None, None)))
            lines.append(line)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("".join(f"{text}\n" for text in lines))
        except Exception:
            self.handleError(record)

    def _should_roll(self) -> bool:
        try:
            size = os.stat(self.path).st_size
        except FileNotFoundError:
            return False  # archived by another process: this record starts a new file
        return size >= self.max_bytes and time.monotonic() >= self._next_try

    def _roll(self) -> None:
        self.wait()  # one archive at a time
        stamp = datetime.now().strftime(STAMP)
        pending = self.path.with_name(f"{self.path.name}.{os.getpid()}.{stamp}.rotating")
        try:
            os.replace(self.path, pending)
        except FileNotFoundError:
            return  # another process archived it first
        except PermissionError:
            # Windows: another process is writing a record. The file grows a little
            # past the limit; try again shortly rather than on every line.
            self._next_try = time.monotonic() + RETRY_DELAY
            return
        # Compressing 1 GiB takes a while: the backups keep logging meanwhile
        self._worker = threading.Thread(target=self._archive, args=(pending, stamp), name="log-archive")
        self._worker.start()

    def _archive_name(self, stamp: str) -> Path:
        stem, suffix = self.path.stem, self.path.suffix or ".log"
        ext = COMPRESSIONS[self.compression]
        name = self.path.with_name(f"{stem}-{stamp}{suffix}{ext}")
        n = 1
        while name.exists():
            n += 1
            name = self.path.with_name(f"{stem}-{stamp}-{n}{suffix}{ext}")
        return name

    def _archive(self, pending: Path, stamp: str) -> None:
        try:
            target = self._archive_name(stamp)
            member = target.name[: -len(COMPRESSIONS[self.compression])]
            # Same extension as the archive: rar and 7z add theirs to a name without one
            tmp = target.with_name(f".tmp-{target.name}")
            try:
                if self.compression == "zip":
                    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=self.compresslevel) as zf:
                        zf.write(pending, arcname=member)
                elif self.compression == "tar.gz":
                    with tarfile.open(tmp, "w:gz", compresslevel=self.compresslevel) as tf:
                        tf.add(pending, arcname=member)
                elif self.compression == "7z":
                    self._7z_archive(pending, member, tmp)
                else:
                    self._rar_archive(pending, member, tmp)
                os.replace(tmp, target)  # an archive is either complete or absent
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            pending.unlink(missing_ok=True)  # rar already took it
            self.prune()
        except Exception as exc:
            # The uncompressed file stays next to the log: nothing is lost
            self._failures.append(f"log file not archived ({pending.name} stays uncompressed): {exc}")

    def _rar_archive(self, pending: Path, member: str, tmp: Path) -> None:
        # rar stores a file under its own name: give it the member's name first, in a
        # folder of its own, and put it back if rar fails
        work = tmp.with_name(f"{tmp.name}.d")
        work.mkdir()
        staged = work / member
        os.replace(pending, staged)
        try:
            # -ep: no folder in the archive, -idq: quiet, -y: never ask
            argv = [*self._program, "a", f"-m{self.compresslevel}", "-ep", "-idq", "-y", "--", str(tmp), str(staged)]
            _run(argv, "rar")
            staged.unlink()
        finally:
            if staged.exists():
                os.replace(staged, pending)
            work.rmdir()

    def _7z_archive(self, pending: Path, member: str, tmp: Path) -> None:
        # -si<name>: the data comes from the standard input, stored under that name;
        # -bso0 -bsp0: no output, no progress; -y: never ask
        argv = [*self._program, "a", "-t7z", f"-mx{self.compresslevel}", "-bso0", "-bsp0", "-y", f"-si{member}"]
        with open(pending, "rb") as data:
            _run([*argv, "--", str(tmp)], "7z", stdin=data)

    def archives(self) -> list[Path]:
        """Archives of this log, oldest first (every compression)."""
        stem, suffix = self.path.stem, self.path.suffix or ".log"
        found = [
            p
            for ext in COMPRESSIONS.values()
            for p in self.path.parent.glob(f"{stem}-*{suffix}{ext}")
            if p.is_file()
        ]

        def key(p: Path):
            try:
                return (p.stat().st_mtime_ns, p.name)
            except FileNotFoundError:
                return (0, p.name)

        return sorted(found, key=key)

    def prune(self) -> None:
        """Remove the oldest archives beyond ``max_archives``."""
        archives = self.archives()
        for old in archives[: max(0, len(archives) - self.max_archives)]:
            try:
                old.unlink()
            except FileNotFoundError:
                pass  # another process removed it

    def wait(self) -> None:
        """Wait for the archive being compressed, if any."""
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join()
        self._worker = None

    def close(self) -> None:
        self.wait()  # never leave an archive half written at exit
        super().close()
