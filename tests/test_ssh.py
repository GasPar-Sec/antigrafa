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
