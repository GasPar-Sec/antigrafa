import subprocess
import sys
import tempfile

import pytest

from antigrafa.ssh import RemoteCommandError, RemoteStream, SSHHost

# The RemoteStream tests run a local shell in place of ssh: sh on Linux, cmd on Windows.
linux_only = pytest.mark.skipif(sys.platform != "linux", reason="needs a POSIX sh")
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs cmd.exe")


def _local(cmd):
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=err, bufsize=0)
    return RemoteStream("local", cmd, proc, err)


def _local_windows(cmd):
    err = tempfile.TemporaryFile()
    # A command line string: cmd.exe parses its own quoting, unlike sh -c
    proc = subprocess.Popen(f"cmd.exe /d /c {cmd}", stdout=subprocess.PIPE, stderr=err, bufsize=0)
    return RemoteStream("local", cmd, proc, err)


@linux_only
def test_stream_ok():
    with _local("printf abc") as s:
        assert s.stdout.read() == b"abc"


@windows_only
def test_stream_ok_windows():
    with _local_windows("echo abc") as s:
        assert s.stdout.read() == b"abc\r\n"


@linux_only
def test_stream_failure_is_reported_with_stderr():
    with pytest.raises(RemoteCommandError, match="vanished"):
        with _local("echo vanished >&2; exit 3") as s:
            s.stdout.read()


@windows_only
def test_stream_failure_is_reported_with_stderr_windows():
    with pytest.raises(RemoteCommandError, match="vanished") as exc:
        with _local_windows("echo vanished 1>&2 & exit 3") as s:
            s.stdout.read()
    assert exc.value.returncode == 3


@linux_only
def test_stream_abort_kills_reader():
    with pytest.raises(ValueError):
        with _local("yes") as s:
            s.stdout.read(10)
            raise ValueError("destination down")
    assert s.proc.returncode is not None


@windows_only
def test_stream_abort_kills_reader_windows():
    with pytest.raises(ValueError):
        # Endless output, like yes: only the kill can stop it
        with _local_windows("for /l %i in (0,0,1) do @echo y") as s:
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
