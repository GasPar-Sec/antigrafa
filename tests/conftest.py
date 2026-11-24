import sys
import textwrap

import pytest


@pytest.fixture(autouse=True)
def _log_file_in_tmp(tmp_path, monkeypatch):
    """The default log file (/var/log/antigrafa, %PROGRAMDATA%) is never written by tests."""
    from antigrafa import config

    real = config.default_log_file

    def default_log_file(*args, **kwargs):
        # With arguments: a test of the default paths themselves
        return real(*args, **kwargs) if args or kwargs else str(tmp_path / "log" / "antigrafa.log")

    monkeypatch.setattr(config, "default_log_file", default_log_file)


@pytest.fixture(autouse=True)
def _scheduler_dir_in_tmp(tmp_path, monkeypatch):
    """The default scheduler directory (run lock, job states) is never the system one in tests."""
    from antigrafa import config

    real = config.default_scheduler_dir

    def default_scheduler_dir(*args, **kwargs):
        return real(*args, **kwargs) if args or kwargs else str(tmp_path / "scheduler")

    monkeypatch.setattr(config, "default_scheduler_dir", default_scheduler_dir)


# Stands in for the rar and 7z programs, rarely installed (rar is not even free): it
# takes their arguments and writes a zip (readable by the tests), with the level as
# comment. FAKE_ARCHIVER_FAIL set: fails the way they do.
FAKE_ARCHIVER = textwrap.dedent(
    """
    import os, sys, zipfile
    args = sys.argv[1:]
    assert args[0] == "a" and "-y" in args, args
    archive = args[args.index("--") + 1]
    if "-t7z" in args:  # 7z a -t7z -mx<level> -bso0 -bsp0 -y -si<name> -- archive < data
        assert "-bso0" in args and "-bsp0" in args, args
        level = next(a for a in args if a.startswith("-mx"))[3:]
        member = next(a for a in args if a.startswith("-si"))[3:]
        data = sys.stdin.buffer.read()
    else:  # rar a -m<level> -ep -idq -y -- archive file
        assert "-ep" in args and "-idq" in args, args
        level = next(a for a in args if a.startswith("-m"))[2:]
        source = args[args.index("--") + 2]
        member = os.path.basename(source)
        data = open(source, "rb").read()
    if os.environ.get("FAKE_ARCHIVER_FAIL"):
        sys.stderr.write("ERROR: cannot create " + archive)
        sys.exit(9)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, data)
        zf.comment = ("level=" + level).encode()
    """
)


@pytest.fixture
def fake_archivers(tmp_path, monkeypatch):
    """find_program() finds the fake rar and 7z; returns the calls made to them."""
    from antigrafa import logfile

    script = tmp_path / "fake_archiver.py"
    script.write_text(FAKE_ARCHIVER)
    asked = []

    def find_program(compression):
        asked.append(compression)
        return [sys.executable, str(script)]

    monkeypatch.setattr(logfile, "find_program", find_program)
    return asked
