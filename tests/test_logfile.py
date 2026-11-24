import logging
import os
import tarfile
import time
import zipfile

import pytest

from antigrafa import logfile
from antigrafa.logfile import ArchivingFileHandler


@pytest.fixture
def logger():
    lg = logging.getLogger("antigrafa.test_logfile")
    lg.propagate = False
    lg.setLevel(logging.INFO)
    yield lg
    for h in lg.handlers[:]:
        lg.removeHandler(h)
        h.close()


def _handler(logger, path, **kwargs):
    options = dict(max_bytes=200, max_archives=5, compression="zip", level=6)
    options.update(kwargs)
    h = ArchivingFileHandler(path, **options)
    logger.addHandler(h)
    return h


def _rename(src, dst):
    # Windows: an antivirus or the indexer may hold the file just written for a moment
    for _ in range(50):
        try:
            return os.replace(src, dst)
        except PermissionError:
            time.sleep(0.05)
    return os.replace(src, dst)


def _member(archive):
    if archive.name.endswith((".zip", ".rar", ".7z")):  # the fake rar and 7z write a zip
        with zipfile.ZipFile(archive) as zf:
            (name,) = zf.namelist()
            return name, zf.read(name).decode()
    with tarfile.open(archive, "r:gz") as tf:
        (info,) = tf.getmembers()
        return info.name, tf.extractfile(info).read().decode()


def test_the_file_is_archived_in_a_zip_past_the_limit(tmp_path, logger):
    h = _handler(logger, tmp_path / "logs" / "antigrafa.log")  # the directory is created
    logger.info("first " + "x" * 250)  # past 200 bytes after this line
    logger.info("second")
    h.wait()
    (archive,) = h.archives()
    assert archive.name.startswith("antigrafa-") and archive.name.endswith(".log.zip")
    name, text = _member(archive)
    assert name == archive.name[: -len(".zip")]  # one name per archive: they extract side by side
    assert "first" in text and "second" not in text
    current = (tmp_path / "logs" / "antigrafa.log").read_text(encoding="utf-8")
    assert "second" in current and "first" not in current
    assert not list((tmp_path / "logs").glob("*.rotating"))  # the uncompressed copy is gone


def test_tar_gz_archives(tmp_path, logger):
    h = _handler(logger, tmp_path / "antigrafa.log", compression="tar.gz")
    logger.info("y" * 300)
    logger.info("after")
    h.wait()
    (archive,) = h.archives()
    assert archive.name.endswith(".log.tar.gz")
    name, text = _member(archive)
    assert name == archive.name[: -len(".tar.gz")] and "y" * 300 in text


def test_only_the_most_recent_archives_are_kept(tmp_path, logger):
    h = _handler(logger, tmp_path / "antigrafa.log", max_archives=3)
    for i in range(6):
        logger.info("line %d %s", i, "z" * 250)
        h.wait()
    archives = h.archives()
    assert len(archives) == 3
    # The oldest went first: the kept ones hold the last lines archived
    texts = [_member(a)[1] for a in archives]
    assert ["line 2" in texts[0], "line 3" in texts[1], "line 4" in texts[2]] == [True, True, True]
    assert "line 5" in (tmp_path / "antigrafa.log").read_text(encoding="utf-8")


def test_no_archive_kept_with_max_archives_zero(tmp_path, logger):
    h = _handler(logger, tmp_path / "antigrafa.log", max_archives=0)
    logger.info("w" * 300)
    logger.info("next")
    h.wait()
    assert h.archives() == []


def test_archives_of_both_compressions_count_together(tmp_path, logger):
    # The compression changed in the configuration: older zips still count, and go first
    for i in range(3):
        old = tmp_path / f"antigrafa-2020-01-0{i + 1}T00-00-00.log.zip"
        old.write_bytes(b"old")
        os.utime(old, (1_000_000 + i, 1_000_000 + i))
    h = _handler(logger, tmp_path / "antigrafa.log", max_archives=2, compression="tar.gz")
    logger.info("v" * 300)
    logger.info("next")
    h.wait()
    names = [a.name for a in h.archives()]
    assert len(names) == 2
    assert names[0] == "antigrafa-2020-01-03T00-00-00.log.zip" and names[1].endswith(".log.tar.gz")


def test_compression_level(tmp_path, logger):
    sizes = {}
    for level in (0, 9):
        h = _handler(logger, tmp_path / str(level) / "antigrafa.log", max_bytes=10**9, level=level)
        for i in range(2000):
            logger.info("backup of guest %d: 4096 MiB read, 12 new chunks", i % 7)
        h.max_bytes = 1  # one archive, of all the lines above
        logger.info("next")
        h.wait()
        (archive,) = h.archives()
        sizes[level] = archive.stat().st_size
        logger.removeHandler(h)
        h.close()
    assert sizes[9] < sizes[0] / 5


def test_two_archives_in_the_same_second_get_distinct_names(tmp_path, logger, monkeypatch):
    monkeypatch.setattr(logfile, "STAMP", "fixed")
    h = _handler(logger, tmp_path / "antigrafa.log")
    for _ in range(3):
        logger.info("u" * 300)
        h.wait()
    names = sorted(a.name for a in h.archives())
    assert names == ["antigrafa-fixed-2.log.zip", "antigrafa-fixed.log.zip"]


def test_file_archived_by_another_process(tmp_path, logger):
    path = tmp_path / "antigrafa.log"
    _handler(logger, path, max_bytes=10_000)
    logger.info("before")
    # Another process (the web interface, a cron backup) archived it: never held open
    # here, so the rename works on every system, and the next record starts a new file
    _rename(path, tmp_path / "elsewhere.log")
    logger.info("after")
    assert "after" in path.read_text(encoding="utf-8")
    assert "after" not in (tmp_path / "elsewhere.log").read_text(encoding="utf-8")
    # A new file of that other process: this one appends to it
    path.write_text("other process\n", encoding="utf-8")
    logger.info("shared")
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "other process" and lines[1].endswith("shared")


def test_check_creates_the_file_or_fails_at_once(tmp_path):
    h = ArchivingFileHandler(tmp_path / "a" / "b" / "antigrafa.log", 100, 5)
    h.check()
    assert (tmp_path / "a" / "b" / "antigrafa.log").is_file()
    (tmp_path / "file").write_text("")
    with pytest.raises(OSError):
        ArchivingFileHandler(tmp_path / "file" / "antigrafa.log", 100, 5).check()


def test_rename_refused_is_tried_again_later(tmp_path, logger, monkeypatch):
    # Windows: the file is open in another process, it cannot be renamed
    path = tmp_path / "antigrafa.log"
    h = _handler(logger, path)
    real_replace = os.replace
    calls = []

    def refuse(src, dst):
        if str(src) == str(path):
            calls.append(src)
            raise PermissionError("in use")
        return real_replace(src, dst)

    monkeypatch.setattr(logfile.os, "replace", refuse)
    monkeypatch.setattr(logfile, "RENAME_PAUSE", 0)
    clock = [1000.0]
    monkeypatch.setattr(logfile.time, "monotonic", lambda: clock[0])
    logger.info("t" * 300)
    logger.info("one")
    logger.info("two")
    assert len(calls) == logfile.RENAME_ATTEMPTS  # a few quick tries, not on every line
    assert "two" in path.read_text(encoding="utf-8")  # nothing lost meanwhile
    clock[0] += logfile.RETRY_DELAY
    monkeypatch.setattr(logfile.os, "replace", real_replace)
    logger.info("three")
    h.wait()
    assert len(h.archives()) == 1


def test_rename_refused_for_a_moment_is_retried_at_once(tmp_path, logger, monkeypatch):
    # Windows: an antivirus reads the file just written, for a few milliseconds
    path = tmp_path / "antigrafa.log"
    h = _handler(logger, path)
    real_replace = os.replace
    refused = []

    def once(src, dst):
        if str(src) == str(path) and not refused:
            refused.append(src)
            raise PermissionError("scanned")
        return real_replace(src, dst)

    monkeypatch.setattr(logfile.os, "replace", once)
    monkeypatch.setattr(logfile, "RENAME_PAUSE", 0)
    logger.info("q" * 300)
    logger.info("next")
    h.wait()
    assert len(refused) == 1
    assert len(h.archives()) == 1  # archived now, not RETRY_DELAY later


def test_failed_archive_keeps_the_text_and_is_reported(tmp_path, logger, monkeypatch):
    path = tmp_path / "antigrafa.log"
    h = _handler(logger, path)

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(logfile.zipfile, "ZipFile", broken)
    logger.info("s" * 300)
    logger.info("next")
    h.wait()
    assert h.archives() == []
    (kept,) = tmp_path.glob("*.rotating")
    assert "s" * 300 in kept.read_text(encoding="utf-8")  # nothing lost
    assert not list(tmp_path.glob(".tmp-*"))  # no half-written archive
    logger.info("later")
    text = path.read_text(encoding="utf-8")
    assert "log file not archived" in text and "disk full" in text and "later" in text


def test_close_waits_for_the_archive(tmp_path, logger):
    h = _handler(logger, tmp_path / "antigrafa.log")
    logger.info("r" * 300)
    logger.info("next")
    logger.removeHandler(h)
    h.close()
    assert len(h.archives()) == 1


def test_record_format_has_the_date_process_and_module(tmp_path, logger):
    path = tmp_path / "antigrafa.log"
    _handler(logger, path, max_bytes=10_000)
    logger.info("hello")
    line = path.read_text(encoding="utf-8").strip()
    assert line.endswith(f"[{os.getpid()} MainThread] antigrafa.test_logfile: hello")
    assert line[4] == "-" and line[10] == " "  # 2026-11-20 10:15:00.123


def test_unknown_compression():
    with pytest.raises(ValueError, match="unknown compression"):
        ArchivingFileHandler("x.log", 100, 5, compression="arj")


def test_level_out_of_range_for_the_compression():
    with pytest.raises(ValueError, match="rar: level must be between 0 and 5"):
        ArchivingFileHandler("x.log", 100, 5, compression="rar", level=6)
    with pytest.raises(ValueError, match="7z: level must be between 0 and 9"):
        ArchivingFileHandler("x.log", 100, 5, compression="7z", level=10)
    with pytest.raises(ValueError, match="zip: level must be between 0 and 9"):
        ArchivingFileHandler("x.log", 100, 5, compression="zip", level=10)


# ------------------------------------------------------------- rar and 7z (outside programs)

OUTSIDE = pytest.mark.parametrize(("compression", "level"), [("rar", 5), ("7z", 9)])


@OUTSIDE
def test_archives_through_the_outside_program(tmp_path, logger, fake_archivers, compression, level):
    h = _handler(logger, tmp_path / "antigrafa.log", compression=compression, level=level)
    assert h.compression == compression and h.warning == "" and fake_archivers == [compression]
    logger.info("q" * 300)
    logger.info("after")
    h.wait()
    (archive,) = h.archives()
    assert archive.name.endswith(f".log.{compression}")
    name, text = _member(archive)
    # Stored under the name of the archive, without its extension
    assert name == archive.name[: -len(compression) - 1] and "q" * 300 in text and "after" not in text
    with zipfile.ZipFile(archive) as zf:
        assert zf.comment == f"level={level}".encode()  # rar -m5, 7z -mx9
    # Nothing left over: no uncompressed copy, no temporary archive or folder
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["antigrafa.log", archive.name, "fake_archiver.py"])


@OUTSIDE
def test_outside_program_failure_keeps_the_text(tmp_path, logger, fake_archivers, monkeypatch, compression, level):
    monkeypatch.setenv("FAKE_ARCHIVER_FAIL", "1")
    path = tmp_path / "antigrafa.log"
    h = _handler(logger, path, compression=compression, level=level)
    logger.info("p" * 300)
    logger.info("next")
    h.wait()
    assert h.archives() == []
    (kept,) = tmp_path.glob("*.rotating")  # under its own name (rar: out of its folder)
    assert "p" * 300 in kept.read_text(encoding="utf-8")
    assert not list(tmp_path.glob(".tmp-*"))
    logger.info("later")
    assert f"{compression} failed (exit 9): ERROR: cannot create" in path.read_text(encoding="utf-8")


@OUTSIDE
def test_outside_program_missing_falls_back_to_zip(tmp_path, logger, monkeypatch, compression, level):
    monkeypatch.setattr(logfile, "find_program", lambda name: None)
    h = _handler(logger, tmp_path / "antigrafa.log", compression=compression, level=min(level, 4))
    assert h.compression == "zip" and f"{compression} not found" in h.warning
    logger.info("o" * 300)
    logger.info("next")
    h.wait()
    (archive,) = h.archives()
    assert archive.name.endswith(".log.zip")


@pytest.mark.parametrize(
    ("compression", "names", "folder", "exe"),
    [("rar", ["rar"], "WinRAR", "Rar.exe"), ("7z", ["7z", "7zz", "7za"], "7-Zip", "7z.exe")],
)
def test_find_program(tmp_path, compression, names, folder, exe):
    # Every usual name is tried in the PATH, in order
    for name in names:
        found = logfile.find_program(compression, which=lambda n, want=name: f"/usr/bin/{n}" if n == want else None)
        assert found == [f"/usr/bin/{name}"]
    # Windows: rarely in the PATH, its usual folders under Program Files are looked at
    (tmp_path / "Programs" / folder).mkdir(parents=True)
    (tmp_path / "Programs" / folder / exe).write_bytes(b"")
    env = {"ProgramFiles": str(tmp_path / "missing"), "ProgramFiles(x86)": str(tmp_path / "Programs")}
    found = logfile.find_program(compression, windows=True, environ=env, which=lambda n: None)
    assert found == [str(tmp_path / "Programs" / folder / exe)]
    assert logfile.find_program(compression, windows=False, environ=env, which=lambda n: None) is None
    assert logfile.find_program(compression, windows=True, environ={}, which=lambda n: None) is None
