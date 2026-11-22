import subprocess
import sys
import tempfile

import pytest

from antigrafa.ssh import RemoteCommandError, RemoteStream, SSHHost

# The RemoteStream tests run a local shell in place of ssh: sh on Linux and macOS,
# cmd.exe on Windows. Every test runs on every system, with the commands of its shell.
WINDOWS = sys.platform == "win32"


def _local(posix: str, windows: str) -> RemoteStream:
    err = tempfile.TemporaryFile()
    if WINDOWS:
        # A command line string: cmd.exe parses its own quoting, unlike sh -c
        cmd = windows
        proc = subprocess.Popen(f"cmd.exe /d /c {cmd}", stdout=subprocess.PIPE, stderr=err, bufsize=0)
    else:
        cmd = posix
        proc = subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=err, bufsize=0)
    return RemoteStream("local", cmd, proc, err)


def test_stream_ok():
    with _local("printf abc", "echo abc") as s:
        assert s.stdout.read() == (b"abc\r\n" if WINDOWS else b"abc")


def test_stream_failure_is_reported_with_stderr():
    with pytest.raises(RemoteCommandError, match="vanished") as exc:
        with _local("echo vanished >&2; exit 3", "echo vanished 1>&2 & exit 3") as s:
            s.stdout.read()
    assert exc.value.returncode == 3


def test_stream_abort_kills_reader():
    with pytest.raises(ValueError):
        # Endless output: only the kill can stop it
        with _local("yes", "for /l %i in (0,0,1) do @echo y") as s:
            s.stdout.read(10)
            raise ValueError("destination down")
    assert s.proc.returncode is not None


def test_ssh_argv():
    argv = SSHHost("10.0.0.1", identity_file="/k", options=["Ciphers=aes128-gcm@openssh.com"])._argv("ls")
    assert argv[-2:] == ["root@10.0.0.1", "ls"]
    assert "BatchMode=yes" in argv and "/k" in argv and "Ciphers=aes128-gcm@openssh.com" in argv


def test_run_returns_stdout(monkeypatch):
    from antigrafa import ssh

    seen = {}

    def fake_run(argv, capture_output, timeout):
        seen.update(argv=argv, timeout=timeout)
        return subprocess.CompletedProcess(argv, 0, b"hello\n", b"")

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    assert SSHHost("pve1", port=2222).run("hostname", timeout=5) == "hello\n"
    assert seen["argv"][-2:] == ["root@pve1", "hostname"]
    assert "2222" in seen["argv"] and seen["timeout"] == 5


def test_run_failure_raises(monkeypatch):
    from antigrafa import ssh

    def fake_run(argv, capture_output, timeout):
        return subprocess.CompletedProcess(argv, 255, b"", b"Permission denied (publickey)\n")

    monkeypatch.setattr(ssh.subprocess, "run", fake_run)
    with pytest.raises(RemoteCommandError, match="Permission denied") as exc:
        SSHHost("pve1").run("true")
    assert exc.value.returncode == 255


def test_stream_runs_the_ssh_command(monkeypatch):
    import sys

    host = SSHHost("pve1")
    monkeypatch.setattr(host, "_argv", lambda cmd: [sys.executable, "-c", "print('chunk', end='')"])
    with host.stream("dd if=/dev/zvol/x") as s:
        assert s.host == "pve1" and s.command == "dd if=/dev/zvol/x"
        assert s.stdout.read() == b"chunk"


def test_run_logs_command_at_debug(monkeypatch, caplog):
    import logging

    from antigrafa import ssh

    caplog.set_level(logging.DEBUG, logger="antigrafa.ssh")
    monkeypatch.setattr(
        ssh.subprocess, "run", lambda argv, capture_output, timeout: subprocess.CompletedProcess(argv, 0, b"", b"")
    )
    SSHHost("pve1", user="backup").run("zfs list")
    assert "ssh backup@pve1: zfs list" in caplog.text
    assert "ssh pve1: exit 0 in" in caplog.text


def test_stream_sends_input_first(monkeypatch):
    # A local python in place of ssh: the same on every platform
    echo = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])"]
    monkeypatch.setattr(SSHHost, "_argv", lambda self, command: echo)
    with SSHHost("pve1").stream("reverse", input=b"abc" * 100000) as s:
        assert s.stdout.read() == (b"abc" * 100000)[::-1]


def test_send_writes_everything_even_in_small_pieces():
    from antigrafa.ssh import _send

    class Slow:
        def __init__(self):
            self.data, self.closed = b"", False

        def write(self, view):
            self.data += bytes(view[:3])
            return min(3, len(view))

        def close(self):
            self.closed = True

    pipe = Slow()
    _send(pipe, b"0123456789")
    assert pipe.data == b"0123456789" and pipe.closed


def test_send_to_a_command_that_already_ended():
    from antigrafa.ssh import _send

    class Gone:
        def write(self, view):
            raise BrokenPipeError

        def close(self):
            raise BrokenPipeError

    _send(Gone(), b"data")  # no error here: the exit status of the command reports it


def test_long_commands_are_shortened_in_messages():
    from antigrafa.ssh import _short

    assert _short("zfs list") == "zfs list"
    long = "python3 -c '" + "x" * 1000 + "'"
    assert _short(long).endswith(f"... ({len(long)} characters)") and len(_short(long)) < 340
    err = RemoteCommandError("pve1", long, 1, "boom")
    assert len(str(err)) < 400 and "boom" in str(err)


def test_stream_remembers_it_was_aborted():
    with _local("printf abc", "echo abc") as s:
        s.stdout.read()
    assert s.aborted is False
    with pytest.raises(ValueError):
        with _local("printf abc", "echo abc") as s:
            raise ValueError("cancelled")
    assert s.aborted is True


# --------------------------------------------------------------------------- feeding a remote command (restore)
# A local python3 in place of ssh: the same on every system.


def _sink(code: str):
    from antigrafa.ssh import RemoteSink

    out, err = tempfile.TemporaryFile(), tempfile.TemporaryFile()
    proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=out, stderr=err, bufsize=0)
    return RemoteSink("local", "python3", proc, out, err)


COUNT = "import sys; data = sys.stdin.buffer.read(); print(len(data))"


def test_sink_feeds_the_command_and_returns_its_output():
    sink = _sink(COUNT)
    for _ in range(64):
        sink.write(b"x" * 65536)  # more than a pipe holds: written as the command reads
    assert sink.close().strip() == str(64 * 65536)
    assert sink.close() == ""  # closed once


def test_sink_failure_is_reported_with_stderr():
    sink = _sink("import sys; sys.stdin.buffer.read(); sys.stderr.write('no space left'); sys.exit(4)")
    sink.write(b"data")
    with pytest.raises(RemoteCommandError, match="no space left") as exc:
        sink.close()
    assert exc.value.returncode == 4


def test_sink_command_that_stops_reading():
    sink = _sink("import sys; sys.stderr.write('device gone'); sys.exit(5)")
    with pytest.raises(RemoteCommandError):
        for _ in range(1000):  # sooner or later the pipe is broken
            sink.write(b"y" * 65536)


def test_sink_abort_kills_the_command():
    with pytest.raises(ValueError):
        with _sink("import time; time.sleep(60)") as sink:
            raise ValueError("cancelled")
    assert sink.proc.returncode is not None


def test_sink_runs_the_ssh_command(monkeypatch):
    from antigrafa import ssh

    seen = {}

    class Proc:
        def __init__(self, argv, **kwargs):
            seen.update(argv=argv, kwargs=kwargs)

    monkeypatch.setattr(ssh.subprocess, "Popen", Proc)
    sink = SSHHost("pve1", port=2222).sink("tar -xf -")
    assert seen["argv"][-2:] == ["root@pve1", "tar -xf -"] and "2222" in seen["argv"]
    assert seen["kwargs"]["stdin"] == subprocess.PIPE
    assert sink.command == "tar -xf -"
