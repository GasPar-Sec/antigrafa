"""Running commands on remote hosts through the system ``ssh`` client."""

from __future__ import annotations

import logging

# subprocess runs the system ssh client (argument lists, never a shell)
import subprocess  # nosec B404
import tempfile
import time
from dataclasses import dataclass, field
from typing import BinaryIO

log = logging.getLogger(__name__)


class RemoteCommandError(Exception):
    def __init__(self, host: str, command: str, returncode: int, stderr: str) -> None:
        super().__init__(f"{host}: '{_short(command)}' failed (exit {returncode}): {stderr.strip()}")
        self.returncode = returncode
        self.stderr = stderr


class RemoteStream:
    """Output stream of a remote command (reading a snapshot)."""

    def __init__(self, host: str, command: str, proc: subprocess.Popen, stderr_file) -> None:
        self.host = host
        self.command = command
        self.proc = proc
        self._stderr = stderr_file

    @property
    def stdout(self) -> BinaryIO:
        return self.proc.stdout

    def close(self, abort: bool = False) -> None:
        if abort and self.proc.poll() is None:
            self.proc.kill()
        if self.proc.stdout:
            self.proc.stdout.close()
        rc = self.proc.wait()
        self._stderr.seek(0)
        err = self._stderr.read().decode(errors="replace")
        self._stderr.close()
        if rc != 0 and not abort:
            raise RemoteCommandError(self.host, self.command, rc, err)

    def __enter__(self) -> "RemoteStream":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(abort=exc_type is not None)


@dataclass
class SSHHost:
    host: str
    user: str = "root"
    port: int = 22
    identity_file: str | None = None
    options: list[str] = field(default_factory=list)

    def _argv(self, command: str) -> list[str]:
        argv = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "Compression=no",  # we compress ourselves, after deduplication
            "-p", str(self.port),
        ]
        if self.identity_file:
            argv += ["-i", self.identity_file]
        for opt in self.options:
            argv += ["-o", opt]
        return argv + [f"{self.user}@{self.host}", command]

    def run(self, command: str, timeout: float = 120.0) -> str:
        # Argument list without a shell: the remote command is a single ssh argument
        log.debug("ssh %s@%s: %s", self.user, self.host, command)
        t0 = time.monotonic()
        res = subprocess.run(self._argv(command), capture_output=True, timeout=timeout)  # nosec B603
        log.debug("ssh %s: exit %d in %.2f s", self.host, res.returncode, time.monotonic() - t0)
        if res.returncode != 0:
            raise RemoteCommandError(self.host, command, res.returncode, res.stderr.decode(errors="replace"))
        return res.stdout.decode()

    def stream(self, command: str, input: bytes | None = None) -> RemoteStream:
        """Start ``command``; ``input`` is written to its standard input first, then closed.

        The remote command must read all of ``input`` before writing much output.
        """
        stderr_file = tempfile.TemporaryFile()
        argv = self._argv(command)
        log.debug("ssh %s@%s (stream): %s", self.user, self.host, _short(command))
        stdin = subprocess.DEVNULL if input is None else subprocess.PIPE
        pipes = {"stdin": stdin, "stdout": subprocess.PIPE, "stderr": stderr_file}
        # Argument list without a shell, as in run()
        proc = subprocess.Popen(argv, bufsize=0, **pipes)  # nosec B603
        if input is not None:
            _send(proc.stdin, input)
        return RemoteStream(self.host, command, proc, stderr_file)


def _send(pipe, data: bytes) -> None:
    """Write everything (an unbuffered pipe may take part of it), then close."""
    view = memoryview(data)
    try:
        while view:
            view = view[pipe.write(view) :]
    except BrokenPipeError:
        pass  # the remote command already ended: its exit status tells why
    finally:
        try:
            pipe.close()
        except BrokenPipeError:
            pass


def _short(command: str, limit: int = 300) -> str:
    """Commands with an inline script are long: keep the log readable."""
    return command if len(command) <= limit else f"{command[:limit]}... ({len(command)} characters)"
