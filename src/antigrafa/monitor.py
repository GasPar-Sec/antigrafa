"""The checks behind the "system" notifications, run by the scheduler.

Every ``check_minutes`` (``[scheduler]``), the scheduler looks at:

- each destination: it can be reached (an NFS one is mounted), and is not fuller than
  ``disk_alert_percent``;
- each source: its API answers, and every node is online.

A problem is notified once, when it appears, and once more when it is gone: what is
known stays in ``system.json`` in the scheduler's directory, so a restart of the
scheduler does not notify again what is already known. The scheduler adds its own
events: stopped, and started again after an unexpected stop.
"""

from __future__ import annotations

import logging
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import notify
from .config import Config
from .operations import open_platform
from .overview import target_usage
from .progress import human_bytes
from .runlock import read_json, write_json

log = logging.getLogger(__name__)

STATE_FILE = "system.json"


def find_problems(cfg: Config) -> dict[str, str]:
    """What is wrong now: a message by key (the key names the thing, the message says what)."""
    problems: dict[str, str] = {}
    limit = cfg.scheduler.disk_alert_percent
    for name in sorted(cfg.targets):
        try:
            usage = target_usage(cfg.targets[name])
        except OSError as exc:
            problems[f"target:{name}"] = f"destination {name}: {exc}"
            continue
        percent = usage["used"] * 100 / usage["total"] if usage["total"] else 0.0
        if limit and percent >= limit:
            problems[f"target-full:{name}"] = (
                f"destination {name}: {percent:.0f}% full, {human_bytes(usage['free'])} free"
            )
    for name in sorted(cfg.sources):
        try:
            hosts = open_platform(cfg, name).hosts()
        except Exception as exc:  # an unreachable API is a problem to report, not a crash
            problems[f"source:{name}"] = f"source {name}: {exc}"
            continue
        for host in hosts:
            if host["status"] != "online":
                problems[f"node:{name}:{host['name']}"] = f"node {host['name']} of {name}: {host['status']}"
    return problems


class SystemWatch:
    """Compares what is wrong with what was already notified, and notifies the difference."""

    def __init__(self, send: Callable = notify.send, now: Callable[[], datetime] | None = None) -> None:
        self._send = send
        self._now = now or (lambda: datetime.now(timezone.utc))

    def check(self, cfg: Config, problems: dict[str, str] | None = None) -> notify.Notification | None:
        """Look (or take ``problems``), notify what changed; the notification sent, if any."""
        problems = find_problems(cfg) if problems is None else problems
        path = Path(cfg.scheduler.directory) / STATE_FILE
        known = (read_json(path) or {}).get("problems", {})
        now = self._now().isoformat()
        new = {k: m for k, m in problems.items() if k not in known}
        gone = {k: v for k, v in known.items() if k not in problems}
        state = {k: {"message": m, "since": known.get(k, {}).get("since", now)} for k, m in problems.items()}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json(path, {"checked": now, "problems": state})
        except OSError as exc:  # notified anyway: a full disk must not hide itself
            log.warning("cannot write %s: %s", path, exc)
        if not new and not gone:
            return None
        message = self.notification(new, gone, state)
        self._send(cfg.notify, message)
        return message

    @staticmethod
    def notification(new: dict[str, str], gone: dict[str, dict], state: dict[str, dict]) -> notify.Notification:
        host = socket.gethostname()
        if new:
            title = f"{len(new)} new problem(s) on {host}"
        else:
            title = f"Problem(s) solved on {host}"
        lines = [f"{len(state)} problem(s) now." if state else "Nothing wrong now."]
        lines += [f"NEW     {m}" for m in new.values()]
        lines += [f"SOLVED  {v['message']} (since {v['since'][:16].replace('T', ' ')})" for v in gone.values()]
        lines += [f"STILL   {v['message']}" for k, v in state.items() if k not in new]
        return notify.Notification("system", "warning" if state else "ok", title, lines)


def scheduler_event(cfg: Config, title: str, lines: list[str], send: Callable = notify.send) -> None:
    """The scheduler itself: stopped, or started again after an unexpected stop."""
    send(cfg.notify, notify.Notification("system", "warning", title, lines))
