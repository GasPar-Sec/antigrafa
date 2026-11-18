"""Running commands on Proxmox nodes through the system ``ssh`` client."""

from __future__ import annotations

# subprocess runs the system ssh client (argument lists, never a shell)
import subprocess  # nosec B404
import tempfile
from dataclasses import dataclass, field
from typing import BinaryIO


class RemoteCommandError(Exception):
    def __init__(self, host: str, command: str, returncode: int, stderr: str) -> None:
        super().__init__(f"{host}: '{command}' failed (exit {returncode}): {stderr.strip()}")
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
        res = subprocess.run(self._argv(command), capture_output=True, timeout=timeout)  # nosec B603
        if res.returncode != 0:
            raise RemoteCommandError(self.host, command, res.returncode, res.stderr.decode(errors="replace"))
        return res.stdout.decode()

    def stream(self, command: str) -> RemoteStream:
        stderr_file = tempfile.TemporaryFile()
        argv = self._argv(command)
        pipes = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": stderr_file}
        # Argument list without a shell, as in run()
        proc = subprocess.Popen(argv, bufsize=0, **pipes)  # nosec B603
        return RemoteStream(self.host, command, proc, stderr_file)
