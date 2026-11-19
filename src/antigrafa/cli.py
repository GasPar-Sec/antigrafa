"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta

from . import platforms
from .config import Config, ConfigError, load_config
from .index import read_index, read_manifest
from .pipeline import restore_stream
from .platforms.base import AccessDenied, Platform, PlatformError
from .progress import Progress, ProgressLogHandler, human_bytes, human_duration
from .targets import Repository, TargetUnavailable

log = logging.getLogger("antigrafa")


def _platform(cfg: Config, name: str | None) -> Platform:
    if not cfg.sources:
        raise ConfigError("a [source.<name>] section is required for this command")
    source = cfg.source(name)
    return platforms.create_platform(source.type, source.name, source.settings)


def _repo(cfg: Config, name: str | None) -> Repository:
    repo = Repository(cfg.target(name))
    repo.open()
    return repo


_human = human_bytes

# -q: -1, default: 0, -v: 1, -vv: 2
LOG_LEVELS = {-1: logging.WARNING, 0: logging.INFO, 1: logging.DEBUG}
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"
# -vv: milliseconds, thread (one per guest backed up in parallel) and module
LOG_FORMAT_DETAILED = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"


def cmd_guests(cfg: Config, args) -> int:
    platform = _platform(cfg, args.source)
    guests = platform.guests()
    if not guests:
        platform.check_access([])  # nothing visible: no guest, or no right?
    for g in guests:
        print(f"{g.id:>6}  {g.kind:<3} {g.host:<12} {g.status:<8} {g.name}")
    return 0


def cmd_backup(cfg: Config, args) -> int:
    from .jobs import BackupRunner

    platform = _platform(cfg, args.source)
    repo = _repo(cfg, args.target)
    guests = platform.guests()
    if not args.all:
        wanted = set(args.guest or [])
        if not wanted:
            raise ConfigError("use --all or --guest")
        missing = wanted - {g.id for g in guests}
        if missing:
            raise ConfigError(f"guests not found: {', '.join(sorted(missing))} (or not visible with these credentials)")
        guests = [g for g in guests if g.id in wanted]
    excluded = set(args.exclude or [])
    guests = [g for g in guests if g.id not in excluded]
    # Every right checked before the first snapshot: missing one stops everything
    platform.check_access(guests)

    progress = args.progress_bar
    runner = BackupRunner(cfg.tuning, platform, repo, progress=progress)
    started = datetime.now().astimezone()
    start = time.monotonic()
    try:
        results = runner.run(guests)
    finally:
        runner.close()
        progress.finish()
    elapsed = time.monotonic() - start

    _backup_report(results, started, elapsed)
    s = runner.controller.stats()
    if s["raw_bytes"]:
        print(
            f"New data: {_human(s['raw_bytes'])} -> {_human(s['stored_bytes'])} written "
            f"(final zstd level {s['level']}, {s['concurrency']} parallel writes)"
        )
    return 0 if all(r.ok for r in results) else 1


PHASE_LABELS = {
    "wait": "waiting for host",
    "prepare": "preparation",
    "snapshot": "snapshot",
    "transfer": "transfer",
    "cleanup": "snapshot removal",
    "flush": "flush",
}


def _secs(seconds: float) -> str:
    """Short durations to the tenth of a second, longer ones as 1m 05s."""
    return f"{seconds:.1f}s" if seconds < 10 else human_duration(seconds)


def _backup_report(results, started: datetime, elapsed: float) -> None:
    """Everything about the time a backup took: overall, per guest, per phase, per disk."""
    ended = started + timedelta(seconds=elapsed)
    end_format = "%H:%M:%S" if ended.date() == started.date() else "%Y-%m-%d %H:%M:%S"
    print(f"\nBackup report: {started:%Y-%m-%d %H:%M:%S} -> {ended.strftime(end_format)} ({_secs(elapsed)})")
    for r in results:
        status = "OK    " if r.ok else "FAILED"
        detail = r.snapshot if r.ok else r.error
        print(f"\n{status} {r.guest.label:<30} {_secs(r.duration):>8}  {detail}")
        phases = [f"{PHASE_LABELS[k]} {_secs(v)}" for k, v in r.timings.items() if k in PHASE_LABELS]
        if phases:
            print("       " + ", ".join(phases))
        for d in r.disks:
            s = d.stats
            rate = f"{_human(d.size / s.elapsed)}/s" if s.elapsed else "-"
            print(
                f"       {d.key:<12} {_human(d.size):>10}  {_secs(s.elapsed):>8}  {rate:>12}  "
                f"{s.new_chunks} new chunks, {s.reused_chunks} reused"
            )

    ok = sum(1 for r in results if r.ok)
    print(f"\nTotal: {len(results)} guest(s), {ok} OK, {len(results) - ok} failed, in {_secs(elapsed)}")
    busy = sum(r.duration for r in results)
    if busy > elapsed * 1.05:  # several guests at once
        print(f"Sum of guest times: {_secs(busy)} (guests backed up in parallel)")
    read = sum(d.size for r in results for d in r.disks)
    if read and elapsed:
        print(f"Read: {_human(read)}, {_human(read / elapsed)}/s on average")
    if len(results) > 1:
        longest = max(results, key=lambda r: r.duration)
        print(f"Longest: {longest.guest.label} ({_secs(longest.duration)})")


def cmd_list(cfg: Config, args) -> int:
    repo = _repo(cfg, args.target)
    for path, m in repo.list_snapshots():
        total = sum(d["size"] for d in m["disks"])
        name = m.get("guest", {}).get("name", "")
        print(f"{path:<48} {name:<20} {len(m['disks'])} disk(s)  {_human(total)}")
    return 0


def cmd_verify(cfg: Config, args) -> int:
    repo = _repo(cfg, args.target)
    snaps = [args.snapshot] if args.snapshot else [p for p, _ in repo.list_snapshots()]
    # Indexes first (small): the number of distinct chunks gives the progress total
    plan = []
    for snap in snaps:
        snap_dir = repo.resolve(snap)
        m = read_manifest(snap_dir)
        plan.append((snap, [read_index(snap_dir / d["index"])[2] for d in m["disks"]]))
    progress = args.progress_bar
    progress.unit = "chunks"
    unique = {digest for _, disks in plan for digests in disks for digest in digests}
    checked: dict[bytes, bool] = {}
    bad = 0
    with progress.task(len(unique)) as task:
        for snap, disks in plan:
            snap_bad = 0
            for digests in disks:
                for digest in digests:
                    if digest not in checked:
                        try:
                            repo.store.read(digest)
                            checked[digest] = True
                        except Exception as exc:
                            log.error("%s: chunk %s: %s", snap, digest.hex(), exc)
                            checked[digest] = False
                        task.advance(1)
                    snap_bad += not checked[digest]
            with progress.paused():
                print(f"{'OK   ' if not snap_bad else 'ERROR'} {snap}" + (f" ({snap_bad} bad chunks)" if snap_bad else ""))
            bad += snap_bad
    progress.finish()
    return 1 if bad else 0


def cmd_restore(cfg: Config, args) -> int:
    repo = _repo(cfg, args.target)
    snap_dir = repo.resolve(args.snapshot)
    m = read_manifest(snap_dir)
    disk = next((d for d in m["disks"] if d["key"] == args.disk), None)
    if disk is None:
        raise ConfigError(f"no disk {args.disk}; available: {', '.join(d['key'] for d in m['disks'])}")
    size, _, digests = read_index(snap_dir / disk["index"])
    progress = args.progress_bar
    task = progress.task(size, name=args.disk)
    with open(args.output, "xb") as out, task:  # "x": never overwrite
        restore_stream(repo.store, digests, out, size, on_chunk=task.advance)
    progress.finish()
    print(f"{args.output}: {_human(size)} restored ({disk['format']})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="antigrafa", description="Backups for virtual machines and containers")
    p.add_argument("-c", "--config", default="/etc/antigrafa/antigrafa.toml")
    talk = p.add_mutually_exclusive_group()
    talk.add_argument(
        "-v", "--verbose", action="count", default=0, help="more details: -v debug, -vv also threads and milliseconds"
    )
    talk.add_argument("-q", "--quiet", action="store_true", help="only warnings and errors")
    p.add_argument(
        "--progress",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="progress line on the terminal (default: shown when the output is a terminal, never with -q)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("guests", help="list the VMs and containers of a source")
    g.add_argument("--source", help="name of a [source.<name>] section")
    g.set_defaults(func=cmd_guests)

    b = sub.add_parser("backup", help="back up guests")
    b.add_argument("--source", help="name of a [source.<name>] section")
    b.add_argument("--all", action="store_true", help="every guest of the source")
    b.add_argument("--guest", nargs="+", metavar="ID", help="guest ids, as shown by 'guests'")
    b.add_argument("--exclude", nargs="+", metavar="ID")
    b.add_argument("--target")
    b.set_defaults(func=cmd_backup)

    ls = sub.add_parser("list", help="list backups")
    ls.add_argument("--target")
    ls.set_defaults(func=cmd_list)

    v = sub.add_parser("verify", help="re-read chunks and check their digests")
    v.add_argument("--target")
    v.add_argument("snapshot", nargs="?")
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("restore", help="restore one disk to a file (raw image or tar)")
    r.add_argument("--target")
    r.add_argument("snapshot", help="e.g. cluster1/vm/100/2026-11-17T22-30-00Z")
    r.add_argument("disk", help="e.g. scsi0, rootfs")
    r.add_argument("output")
    r.set_defaults(func=cmd_restore)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    verbosity = -1 if args.quiet else min(args.verbose, 2)
    # Shown on a terminal unless --no-progress; -q hides it unless --progress
    enabled = args.progress if args.progress is not None else (None if verbosity >= 0 else False)
    args.progress_bar = Progress(label=args.cmd, enabled=enabled)
    logging.basicConfig(
        level=LOG_LEVELS[min(verbosity, 1)],
        format=LOG_FORMAT_DETAILED if verbosity >= 2 else LOG_FORMAT,
        datefmt="%H:%M:%S",
        handlers=[ProgressLogHandler(args.progress_bar)],
    )
    try:
        return args.func(load_config(args.config), args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except AccessDenied as exc:
        log.error("%s", exc)
        return 3
    except (TargetUnavailable, PlatformError, FileExistsError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
