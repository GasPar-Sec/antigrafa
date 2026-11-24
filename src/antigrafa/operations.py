"""What the command line and the web interface share: opening a source and a target,
choosing the guests of a backup and describing its results."""

from __future__ import annotations

from datetime import datetime, timedelta

from . import platforms
from .config import Config, ConfigError
from .platforms.base import Guest, Platform
from .progress import human_bytes, human_duration
from .targets import Repository


def open_platform(cfg: Config, name: str | None) -> Platform:
    if not cfg.sources:
        raise ConfigError("a [source.<name>] section is required for this command")
    source = cfg.source(name)
    return platforms.create_platform(source.type, source.name, source.settings)


def open_repository(cfg: Config, name: str | None) -> Repository:
    repo = Repository(cfg.target(name))
    repo.open()
    return repo


def select_guests(
    guests: list[Guest], wanted: list[str] | None, every: bool = False, exclude: list[str] | None = None
) -> list[Guest]:
    """The guests to back up: ``every`` one, or the ``wanted`` ids, minus ``exclude``."""
    if not every:
        ids = set(wanted or [])
        if not ids:
            raise ConfigError("use --all or --guest")
        missing = ids - {g.id for g in guests}
        if missing:
            raise ConfigError(f"guests not found: {', '.join(sorted(missing))} (or not visible with these credentials)")
        guests = [g for g in guests if g.id in ids]
    excluded = set(exclude or [])
    return [g for g in guests if g.id not in excluded]


def prepare_backup(
    cfg: Config,
    source: str | None,
    target: str | None,
    guests: list[str] | None = None,
    every: bool = False,
    exclude: list[str] | None = None,
) -> tuple[Platform, Repository, list[Guest]]:
    """Everything checked before the first snapshot: the source, the target, the guests and
    every right of the token (missing one stops everything)."""
    platform = open_platform(cfg, source or None)
    repo = open_repository(cfg, target or None)
    selected = select_guests(platform.guests(), guests, every, exclude)
    if not selected:
        raise ConfigError("no guest to back up")
    platform.check_access(selected)
    return platform, repo, selected


def backup_state(guests: int, results, error: str = "", cancelled: bool = False) -> str:
    """How a backup ended: ok, failed (a guest failed, or the run itself) or cancelled."""
    if cancelled:
        return "cancelled"
    if error or len(results) < guests or not all(r.ok for r in results):
        return "failed"
    return "ok"


PHASE_LABELS = {
    "wait": "waiting for host",
    "prepare": "preparation",
    "snapshot": "snapshot",
    "transfer": "transfer",
    "cleanup": "snapshot removal",
    "flush": "flush",
}


def secs(seconds: float) -> str:
    """Short durations to the tenth of a second, longer ones as 1m 05s."""
    return f"{seconds:.1f}s" if seconds < 10 else human_duration(seconds)


def report_lines(results, started: datetime, elapsed: float) -> list[str]:
    """Everything about the time a backup took: overall, per guest, per phase, per disk."""
    ended = started + timedelta(seconds=elapsed)
    end_format = "%H:%M:%S" if ended.date() == started.date() else "%Y-%m-%d %H:%M:%S"
    out = ["", f"Backup report: {started:%Y-%m-%d %H:%M:%S} -> {ended.strftime(end_format)} ({secs(elapsed)})"]
    for r in results:
        status = "OK    " if r.ok else "FAILED"
        detail = r.snapshot if r.ok else r.error
        out += ["", f"{status} {r.guest.label:<30} {secs(r.duration):>8}  {detail}"]
        phases = [f"{PHASE_LABELS[k]} {secs(v)}" for k, v in r.timings.items() if k in PHASE_LABELS]
        if phases:
            out.append("       " + ", ".join(phases))
        for d in r.disks:
            s = d.stats
            rate = f"{human_bytes(d.size / s.elapsed)}/s" if s.elapsed else "-"
            out.append(
                f"       {d.key:<12} {human_bytes(d.size):>10}  {secs(s.elapsed):>8}  {rate:>12}  "
                f"{s.new_chunks} new chunks, {s.reused_chunks} reused, {human_bytes(s.sent_bytes)} sent"
            )

    ok = sum(1 for r in results if r.ok)
    out += ["", f"Total: {len(results)} guest(s), {ok} OK, {len(results) - ok} failed, in {secs(elapsed)}"]
    busy = sum(r.duration for r in results)
    if busy > elapsed * 1.05:  # several guests at once
        out.append(f"Sum of guest times: {secs(busy)} (guests backed up in parallel)")
    read = sum(d.size for r in results for d in r.disks)
    if read and elapsed:
        out.append(f"Read: {human_bytes(read)}, {human_bytes(read / elapsed)}/s on average")
        sent = sum(d.stats.sent_bytes for r in results for d in r.disks)
        out.append(f"Sent over the network: {human_bytes(sent)} ({sent / read:.0%} of what was read)")
    if len(results) > 1:
        longest = max(results, key=lambda r: r.duration)
        out.append(f"Longest: {longest.guest.label} ({secs(longest.duration)})")
    return out


def new_data_line(stats: dict) -> str | None:
    """What the destination received (``WriteController.stats()``); None: nothing new."""
    if not stats["raw_bytes"]:
        return None
    return (
        f"New data: {human_bytes(stats['raw_bytes'])} -> {human_bytes(stats['stored_bytes'])} written "
        f"(final zstd level {stats['level']}, {stats['concurrency']} parallel writes)"
    )
