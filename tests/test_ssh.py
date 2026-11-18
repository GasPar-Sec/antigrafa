import subprocess
import tempfile

import pytest

from antigrafa.ssh import RemoteCommandError, RemoteStream, SSHHost


def _local(cmd):
    err = tempfile.TemporaryFile()
    proc = subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=err, bufsize=0)
    return RemoteStream("local", cmd, proc, err)


def test_stream_ok():
    with _local("printf abc") as s:
        assert s.stdout.read() == b"abc"


def test_stream_failure_is_reported_with_stderr():
    with pytest.raises(RemoteCommandError, match="vanished"):
        with _local("echo vanished >&2; exit 3") as s:
            s.stdout.read()


def test_stream_abort_kills_reader():
    with pytest.raises(ValueError):
        with _local("yes") as s:
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
