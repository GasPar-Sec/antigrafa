"""Files only the account running Antigrafa may read: secret key, TLS key, user database.

Linux: mode 0700 for the directory, 0600 for the files. Windows: the inherited rights
are removed (ProgramData is readable by every local user) and only SYSTEM, the
Administrators group and the current account keep access, through ``icacls``.
"""

from __future__ import annotations

import getpass
import logging
import os

# subprocess runs icacls on Windows (argument list, never a shell)
import subprocess  # nosec B404
import sys
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Well-known SIDs: the same in every language of Windows
SYSTEM = "*S-1-5-18"
ADMINISTRATORS = "*S-1-5-32-544"


def _account(environ=os.environ) -> str:
    domain = environ.get("USERDOMAIN")
    user = getpass.getuser()
    return f"{domain}\\{user}" if domain else user


def restrict(
    path: Path,
    directory: bool = False,
    platform: str = sys.platform,
    run: Callable = subprocess.run,
    environ=os.environ,
) -> bool:
    """Leave ``path`` to the current account (and, on Windows, the administrators).

    False when it could not be done (a warning says so): the files still work.
    """
    if platform != "win32":
        try:
            os.chmod(path, 0o700 if directory else 0o600)
        except OSError as exc:
            log.warning("%s: could not restrict access: %s", path, exc)
            return False
        return True
    inherit = "(OI)(CI)F" if directory else "F"
    grants = [SYSTEM, ADMINISTRATORS, _account(environ)]
    argv = ["icacls", str(path), "/inheritance:r"]
    for who in grants:
        argv += ["/grant:r", f"{who}:{inherit}"]
    # A directory: Windows passes the new rights on to what it contains (inheritance)
    try:
        res = run(argv, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("%s: could not restrict access (icacls): %s", path, exc)
        return False
    if res.returncode != 0:
        log.warning("%s: could not restrict access (icacls): %s", path, (res.stdout + res.stderr).strip())
        return False
    return True


def private_dir(path: Path, **kwargs) -> Path:
    """Create ``path`` if needed and leave it to the current account."""
    path.mkdir(parents=True, exist_ok=True)
    restrict(path, directory=True, **kwargs)
    return path


def write_private(path: Path, data: bytes, **kwargs) -> None:
    """Write a new file that only the current account can read (replaces an old one)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    # 0600 from the start on Linux: never readable by others, even for a moment
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    restrict(path, **kwargs)
