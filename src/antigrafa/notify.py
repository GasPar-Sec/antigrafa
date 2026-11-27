"""Notifications by email, Discord and Microsoft Teams: the ``[notify.<name>]`` sections.

Each section is one channel (an address list, a Discord webhook, a Teams workflow) with
the events it receives:

- ``backup-failed``: a backup that failed (one guest or the whole run) or was cancelled;
- ``backup-ok``: a backup where every guest succeeded;
- ``restore``: the end of a restore, successful or not;
- ``system``: what the scheduler's checks find (a destination unreachable or nearly
  full, a node offline, the scheduler stopped), and when it is over.

A notification that cannot be sent is logged and never fails what it reports: a backup
is not lost because the mail server is down. Each channel gets a few seconds at most.

Discord: a webhook (Server settings > Integrations > Webhooks). Teams: a workflow
"Send webhook alerts to a channel" (the Workflows app), which takes an Adaptive Card;
the old Office 365 connectors are retired.
"""

from __future__ import annotations

import json
import logging
import smtplib
import socket
import ssl
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

EVENTS = ("backup-failed", "backup-ok", "restore", "system")
DEFAULT_EVENTS = ["backup-failed", "restore", "system"]  # everything but successes
TYPES = ("email", "discord", "teams")
SECURITY = ("starttls", "ssl", "none")
TIMEOUT = 10.0  # seconds for each channel
MAX_TEXT = 3500  # characters of details: Discord takes 4096 per message, Teams about 28 KB

COLORS = {"ok": 0x1F8A4C, "failed": 0xC4352B, "warning": 0xB26A00}  # Discord
TEAMS_COLORS = {"ok": "Good", "failed": "Attention", "warning": "Warning"}
ICONS = {"ok": "✅", "failed": "❌", "warning": "⚠️"}


class NotifyError(Exception):
    pass


@dataclass
class NotifyConfig:
    """One ``[notify.<name>]`` section."""

    name: str
    type: str
    events: list[str] = field(default_factory=lambda: list(DEFAULT_EVENTS))
    # discord, teams: the webhook address. A secret (anyone who has it can post): in a file
    url: str = ""
    url_file: str = ""
    # email
    smtp_host: str = ""
    smtp_port: int = 0  # 0: 587 with starttls, 465 with ssl, 25 without
    security: str = "starttls"
    username: str = ""  # "": no login
    password: str = ""
    password_file: str = ""
    sender: str = ""  # From
    to: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.type not in TYPES:
            raise ValueError(f"type must be one of {', '.join(TYPES)}")
        if not isinstance(self.events, list) or not all(e in EVENTS for e in self.events):
            raise ValueError(f"events must be a list of {', '.join(EVENTS)}")
        for name in ("url", "url_file", "smtp_host", "security", "username", "password", "password_file", "sender"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        if self.type == "email":
            if not self.smtp_host or not self.sender:
                raise ValueError("smtp_host and sender are required for email")
            if not isinstance(self.to, list) or not self.to or not all(isinstance(t, str) and "@" in t for t in self.to):
                raise ValueError("to must be a list of email addresses")
            if self.security not in SECURITY:
                raise ValueError(f"security must be one of {', '.join(SECURITY)}")
            if isinstance(self.smtp_port, bool) or not isinstance(self.smtp_port, int) or not 0 <= self.smtp_port <= 65535:
                raise ValueError("smtp_port must be a port number")
        elif not (self.url or self.url_file):
            raise ValueError(f"url or url_file is required for {self.type}")
        elif self.url and not self.url.startswith("https://"):
            raise ValueError("url must start with https://")

    @property
    def port(self) -> int:
        return self.smtp_port or {"starttls": 587, "ssl": 465, "none": 25}[self.security]

    def webhook(self) -> str:
        url = Path(self.url_file).read_text().strip() if self.url_file else self.url
        if not url.startswith("https://"):
            raise NotifyError(f"{self.url_file or 'url'}: the webhook address must start with https://")
        return url

    def login_password(self) -> str:
        return Path(self.password_file).read_text().strip() if self.password_file else self.password


@dataclass
class Notification:
    event: str  # one of EVENTS, or "test"
    status: str  # ok, failed, warning
    title: str  # one line
    lines: list[str] = field(default_factory=list)  # the details, the first one a summary

    def text(self, limit: int = MAX_TEXT) -> str:
        """The details, cut to ``limit`` characters (whole lines) with what was left out."""
        kept, size = [], 0
        for i, line in enumerate(self.lines):
            if size + len(line) + 1 > limit:
                kept.append(f"... {len(self.lines) - i} more line(s) in the log")
                break
            kept.append(line)
            size += len(line) + 1
        return "\n".join(kept)


def _footer() -> str:
    return f"Antigrafa {__version__} on {socket.gethostname()}"


# --------------------------------------------------------------------------- messages


def discord_payload(n: Notification) -> dict:
    description = n.text()
    if n.lines[1:]:  # the summary, then the details as they are (a report lines up in columns)
        description = f"{n.lines[0]}\n```\n{Notification('', '', '', n.lines[1:]).text()}\n```"
    return {
        "username": "Antigrafa",
        "embeds": [{
            "title": f"{ICONS.get(n.status, '')} {n.title}".strip()[:256],
            "description": description,
            "color": COLORS.get(n.status, COLORS["warning"]),
            "footer": {"text": _footer()},
        }],
    }


def teams_payload(n: Notification) -> dict:
    """An Adaptive Card, as a Teams workflow ("Send webhook alerts to a channel") takes it."""
    body = [{"type": "TextBlock", "text": n.title, "weight": "Bolder", "size": "Medium", "wrap": True,
             "color": TEAMS_COLORS.get(n.status, "Warning")}]
    if n.lines:
        body.append({"type": "TextBlock", "text": n.lines[0], "wrap": True})
    if n.lines[1:]:
        details = Notification("", "", "", n.lines[1:]).text()
        body.append({"type": "TextBlock", "text": details, "wrap": True, "fontType": "Monospace", "size": "Small"})
    body.append({"type": "TextBlock", "text": _footer(), "isSubtle": True, "size": "Small", "wrap": True})
    card = {"$schema": "https://adaptivecards.io/schemas/1.2.0/adaptive-card.json", "type": "AdaptiveCard",
            "version": "1.4", "body": body}
    return {"type": "message",
            "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": card}]}


def email_message(channel: NotifyConfig, n: Notification) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[Antigrafa] {n.title}"
    msg["From"] = channel.sender
    msg["To"] = ", ".join(channel.to)
    msg.set_content(f"{n.text(limit=200_000)}\n\n-- \n{_footer()}\n")
    return msg


# --------------------------------------------------------------------------- sending


def post_json(url: str, payload: dict, timeout: float = TIMEOUT) -> None:
    data = json.dumps(payload).encode()
    request = urllib.request.Request(  # https only: checked by NotifyConfig.webhook
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": f"Antigrafa/{__version__}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310 - https:// checked
        if not 200 <= response.status < 300:
            raise NotifyError(f"HTTP {response.status}")


def send_email(channel: NotifyConfig, msg: EmailMessage, timeout: float = TIMEOUT) -> None:
    context = ssl.create_default_context()
    if channel.security == "ssl":
        server = smtplib.SMTP_SSL(channel.smtp_host, channel.port, timeout=timeout, context=context)
    else:
        server = smtplib.SMTP(channel.smtp_host, channel.port, timeout=timeout) #NOSONAR
    with server:
        if channel.security == "starttls":
            server.starttls(context=context)
        if channel.username:
            server.login(channel.username, channel.login_password())
        server.send_message(msg)


def deliver(channel: NotifyConfig, n: Notification) -> None:
    """Send ``n`` to one channel; raises on failure."""
    if channel.type == "email":
        send_email(channel, email_message(channel, n))
    elif channel.type == "discord":
        post_json(channel.webhook(), discord_payload(n))
    else:
        post_json(channel.webhook(), teams_payload(n))


def send(channels: dict[str, NotifyConfig], n: Notification, only: str | None = None) -> dict[str, str]:
    """Send ``n`` to the channels that take its event (``only``: that channel, whatever its
    events: a test). Never raises: the error of each channel that failed, by name."""
    errors = {}
    for name, channel in channels.items():
        if only is not None:
            if name != only:
                continue
        elif n.event not in channel.events:
            continue
        try:
            deliver(channel, n)
            log.info("notification sent to %s (%s): %s", name, channel.type, n.title)
        except Exception as exc:  # never fails the backup it reports
            errors[name] = str(exc) or type(exc).__name__
            log.warning("notification to %s (%s) not sent: %s", name, channel.type, errors[name])
    return errors


# --------------------------------------------------------------------------- events


def backup_notification(
    state: str, *, source: str, target: str, results, report: list[str], error: str = "",
    job: str = "", by: str = "", user: str = "",
) -> Notification:
    """The end of a backup: ``state`` ok, failed or cancelled; ``report``: its report lines."""
    what = f"job {job}" if job else "backup"
    ok = sum(1 for r in results if r.ok)
    failed = [r for r in results if not r.ok]
    summary = f"{source} -> {target}: {ok} guest(s) OK, {len(failed)} failed"
    if by:
        summary += f" (started {'by the scheduler' if by == 'scheduler' else 'from the ' + by}"
        summary += f" by {user})" if user else ")"
    lines = [summary]
    if error:
        lines.append(f"Error: {error}")
    lines += [f"FAILED {r.guest.label}: {r.error}" for r in failed]
    lines += [line for line in report if line.strip()]
    if state == "ok":
        return Notification("backup-ok", "ok", f"{what.capitalize()} succeeded: {source} -> {target}", lines)
    word = "was cancelled" if state == "cancelled" else "failed"
    return Notification("backup-failed", "failed" if state == "failed" else "warning",
                        f"{what.capitalize()} {word}: {source} -> {target}", lines)


def restore_notification(state: str, *, backup: str, target: str, guest: str, error: str = "",
                         message: str = "", user: str = "") -> Notification:
    lines = [f"{backup} (from {target}) -> {guest}" + (f", started by {user}" if user else "")]
    if error:
        lines.append(f"Error: {error}")
    if message:
        lines.append(message)
    if state == "ok":
        return Notification("restore", "ok", f"Restore succeeded: {guest}", lines)
    word = "was cancelled" if state == "cancelled" else "failed"
    return Notification("restore", "failed" if state == "failed" else "warning", f"Restore {word}: {guest}", lines)


def test_notification(name: str) -> Notification:
    return Notification("test", "ok", f"Test notification to {name}",
                        [f"Antigrafa can reach this channel ({name}).", "Nothing to do."])
