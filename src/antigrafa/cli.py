"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime

from . import operations
from .config import Config, ConfigError, default_config_path, load_config
from .index import read_index, read_manifest
from .pipeline import restore_stream
from .operations import new_data_line, open_platform, open_repository, report_lines, select_guests
from .platforms.base import AccessDenied, PlatformError
from .progress import Progress, ProgressLogHandler, human_bytes, human_duration
from .targets import TargetUnavailable

log = logging.getLogger("antigrafa")


# Kept under their old names (tests and older code)
_platform = open_platform
_repo = open_repository


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
    guests = select_guests(platform.guests(), args.guest, args.all, args.exclude)
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
    new_data = new_data_line(runner.controller.stats())
    if new_data:
        print(new_data)
    return 0 if all(r.ok for r in results) else 1


PHASE_LABELS = operations.PHASE_LABELS
_secs = operations.secs


def _backup_report(results, started: datetime, elapsed: float) -> None:
    print("\n".join(report_lines(results, started, elapsed)))


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


WEB_MISSING = "the web interface needs its extra dependencies: pip install 'antigrafa[web]'"


def _web(module: str):
    """A module of the web interface, or a clear error when Django & co. are missing."""
    import importlib

    try:
        return importlib.import_module(f"antigrafa.web.{module}")
    except ImportError as exc:
        if exc.name and exc.name.split(".")[0] in ("django", "cheroot", "cryptography"):
            raise ConfigError(WEB_MISSING) from None
        raise


def cmd_web_serve(cfg: Config, args) -> int:
    return _web("server").serve(cfg, args.config)


def _read_password(args) -> str:
    if args.password_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    import getpass

    password = getpass.getpass(f"Password for {args.name}: ")
    if getpass.getpass("Again: ") != password:
        raise ConfigError("the two passwords differ")
    return password


def cmd_web_adduser(cfg: Config, args) -> int:
    server = _web("server")
    created = server.add_user(cfg, args.config, args.name, _read_password(args))
    print(f"user {args.name} {'created' if created else 'updated (new password)'}")
    return 0


def cmd_web_deluser(cfg: Config, args) -> int:
    if not _web("server").remove_user(cfg, args.config, args.name):
        raise ConfigError(f"no user {args.name}")
    print(f"user {args.name} removed")
    return 0


def _open_log_file(cfg: Config) -> logging.Handler | None:
    """The [log] file on the root logger; None when disabled or not writable."""
    from .logfile import ArchivingFileHandler

    if not cfg.log.enabled:
        return None
    try:
        handler = ArchivingFileHandler(
            cfg.log.path, cfg.log.max_bytes, cfg.log.max_archives, cfg.log.compression, cfg.log.compression_level
        )
        handler.check()  # fails here, not on the first record
    except OSError as exc:
        # A user without the rights of the service account: the terminal still has it all
        log.warning("no log file (%s): %s", cfg.log.path, exc)
        return None
    logging.getLogger().addHandler(handler)
    log.debug("log file: %s", cfg.log.path)
    if handler.warning:  # rar or 7z missing: on the terminal and in the file
        log.warning("log file: %s", handler.warning)
    return handler


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="antigrafa", description="Backups for virtual machines and containers")
    p.add_argument("-c", "--config", default=default_config_path(), help="default: %(default)s")
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

    w = sub.add_parser("web", help="web interface (HTTPS): start and follow backups")
    wsub = w.add_subparsers(dest="web_cmd", required=True)
    ws = wsub.add_parser("serve", help="start the web interface (see the [web] section)")
    ws.set_defaults(func=cmd_web_serve)
    wa = wsub.add_parser("adduser", help="create a user of the web interface, or change its password")
    wa.add_argument("name")
    wa.add_argument("--password-stdin", action="store_true", help="read the password from the standard input")
    wa.set_defaults(func=cmd_web_adduser)
    wd = wsub.add_parser("deluser", help="remove a user of the web interface")
    wd.add_argument("name")
    wd.set_defaults(func=cmd_web_deluser)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    verbosity = -1 if args.quiet else min(args.verbose, 2)
    # Shown on a terminal unless --no-progress; -q hides it unless --progress
    enabled = args.progress if args.progress is not None else (None if verbosity >= 0 else False)
    args.progress_bar = Progress(label=args.cmd, enabled=enabled)
    console = ProgressLogHandler(args.progress_bar)
    console.setLevel(LOG_LEVELS[min(verbosity, 1)])
    logging.basicConfig(
        format=LOG_FORMAT_DETAILED if verbosity >= 2 else LOG_FORMAT,
        datefmt="%H:%M:%S",
        handlers=[console],
    )
    # -q only quiets the terminal: the log file always gets INFO (DEBUG with -v)
    logging.getLogger().setLevel(logging.DEBUG if verbosity >= 1 else logging.INFO)
    file_handler = None
    try:
        cfg = load_config(args.config)
        file_handler = _open_log_file(cfg)
        return args.func(cfg, args)
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
    finally:
        if file_handler is not None:
            logging.getLogger().removeHandler(file_handler)
            file_handler.close()  # waits for an archive being compressed


if __name__ == "__main__":
    sys.exit(main())
