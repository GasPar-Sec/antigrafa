"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import notify, operations
from .config import Config, ConfigError, default_config_path, load_config
from .index import read_index, read_manifest
from .pipeline import READ_AHEAD, in_order, restore_stream
from .operations import new_data_line, open_platform, open_repository, report_lines, select_guests
from .platforms.base import AccessDenied, PlatformError
from .progress import Progress, ProgressLogHandler, human_bytes, human_duration
from .ssh import RemoteCommandError
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
    results, error = [], ""
    try:
        results = runner.run(guests)
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        raise
    finally:
        runner.close()
        progress.finish()
        lines = report_lines(results, started, time.monotonic() - start)
        new_data = new_data_line(runner.controller.stats())
        state = operations.backup_state(len(guests), results, error)
        notify.send(cfg.notify, notify.backup_notification(
            state, source=platform.name, target=repo.cfg.name, results=results, report=lines[1:], error=error,
            by="command line"))

    print("\n".join(lines))
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
    # Sorted: chunks of the same directory are read together
    unique = sorted({digest for _, disks in plan for digests in disks for digest in digests})

    def check(digest: bytes) -> str:
        try:
            repo.store.read(digest)
            return ""
        except Exception as exc:
            return str(exc) or type(exc).__name__

    errors: dict[bytes, str] = {}
    jobs = max(1, args.jobs)
    with progress.task(len(unique)) as task, ThreadPoolExecutor(jobs, thread_name_prefix="verify") as pool:
        for digest, error in zip(unique, in_order(pool, check, unique, jobs * READ_AHEAD)):
            if error:
                errors[digest] = error
            task.advance(1)
    bad = 0
    for snap, disks in plan:
        damaged = [d for digests in disks for d in digests if d in errors]
        for digest in dict.fromkeys(damaged):
            log.error("%s: chunk %s: %s", snap, digest.hex(), errors[digest])
        with progress.paused():
            print(f"{'OK   ' if not damaged else 'ERROR'} {snap}" + (f" ({len(damaged)} bad chunks)" if damaged else ""))
        bad += len(damaged)
    progress.finish()
    return 1 if bad else 0


def cmd_restore(cfg: Config, args) -> int:
    repo = _repo(cfg, args.target)
    snap_dir = repo.resolve(args.snapshot)
    m = read_manifest(snap_dir)
    disk = next((d for d in m["disks"] if d["key"] == args.disk), None)
    if disk is None:
        raise ConfigError(f"no disk {args.disk}; available: {', '.join(d['key'] for d in m['disks'])}")
    size, chunk_size, digests = read_index(snap_dir / disk["index"])
    progress = args.progress_bar
    task = progress.task(size, name=args.disk)
    with open(args.output, "xb") as out, task:  # "x": never overwrite
        restore_stream(repo.store, digests, out, size, on_chunk=task.advance, workers=args.jobs, chunk_size=chunk_size)
    progress.finish()
    print(f"{args.output}: {_human(size)} restored ({disk['format']})")
    return 0


def _ask_guest_id(default: str, ask=input) -> str:
    """The id to restore as: asked on a terminal, the original one by default."""
    if not sys.stdin.isatty():
        return default
    answer = ask(f"Restore as guest id [{default}]: ").strip()
    return answer or default


def cmd_restore_guest(cfg: Config, args) -> int:
    from .platforms.base import RestoreRequest
    from .restore import restore_guest

    repo = _repo(cfg, args.target)
    manifest = read_manifest(repo.resolve(args.backup))
    original = str((manifest.get("guest") or {}).get("id", ""))
    guest_id = args.id or _ask_guest_id(original)
    platform = _platform(cfg, args.source or manifest.get("source"))
    request = RestoreRequest(guest_id=guest_id, host=args.host or "", storage=args.storage or "",
                             overwrite=args.overwrite, disk_only=args.disk_only, stop=args.stop or "",
                             start=args.start)
    progress = args.progress_bar
    guest = f"{str(manifest.get('guest', {}).get('kind', '')).upper()} {guest_id}".strip()
    try:
        done = restore_guest(platform, repo, args.backup, request, progress, jobs=args.jobs)
    except BaseException as exc:
        progress.finish()
        notify.send(cfg.notify, notify.restore_notification(
            "cancelled" if isinstance(exc, KeyboardInterrupt) else "failed", backup=args.backup,
            target=repo.cfg.name, guest=guest, error=str(exc) or type(exc).__name__))
        raise
    progress.finish()
    notify.send(cfg.notify, notify.restore_notification("ok", backup=args.backup, target=repo.cfg.name,
                                                        guest=guest, message=str(done)))
    print(done)
    return 0


WEB_MISSING ="the web interface needs its extra dependencies: pip install 'antigrafa[web]'"


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


def cmd_scheduler(cfg: Config, args) -> int:
    import signal
    import threading

    from .scheduler import Scheduler

    scheduler = Scheduler(args.config)
    previous = None
    if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGTERM"):
        # systemd stops services with SIGTERM: stop like Ctrl+C, the running backup cleaned up
        previous = signal.signal(signal.SIGTERM, _interrupt)
    try:
        return scheduler.serve()
    finally:
        if previous is not None:
            signal.signal(signal.SIGTERM, previous)


def _interrupt(signum, frame) -> None:
    raise KeyboardInterrupt


def _when(text: str | None) -> str:
    if not text:
        return "-"
    return datetime.fromisoformat(text).astimezone().strftime("%Y-%m-%d %H:%M")


def cmd_notify_test(cfg: Config, args) -> int:
    """Send a test message to every [notify.<name>] channel (or the one named), whatever its events."""
    names = [args.name] if args.name else sorted(cfg.notify)
    if not names:
        print("no notification channel configured ([notify.<name>] sections)")
        return 1
    unknown = [n for n in names if n not in cfg.notify]
    if unknown:
        raise ConfigError(f"unknown channel {unknown[0]} ({', '.join(sorted(cfg.notify)) or 'none configured'})")
    failed = 0
    for name in names:
        errors = notify.send(cfg.notify, notify.test_notification(name), only=name)
        print(f"{name} ({cfg.notify[name].type}): {'not sent: ' + errors[name] if name in errors else 'sent'}")
        failed += name in errors
    return 1 if failed else 0


def cmd_job_list(cfg: Config, args) -> int:
    if cfg.jobs:
        _print_jobs(cfg)
    else:
        print("no job configured ([job.<name>] sections)")
    return 0


def _print_jobs(cfg: Config) -> None:
    from .scheduler import job_summaries, scheduler_status

    status = scheduler_status(cfg.scheduler.directory)
    if status["alive"]:
        print(f"scheduler: running since {_when(status['started'])} (pid {status['pid']})"
              + (f", running {status['running']}" if status["running"] else "")
              + (f", queue: {', '.join(status['queue'])}" if status["queue"] else ""))
    else:
        print("scheduler: not running (antigrafa scheduler): jobs only run with it")
    for job in job_summaries(cfg):
        last = job["last"]
        if last:
            outcome = f"{last['state']} {_when(last['started'])}, {last['guests_ok']}/{last['guests_total']} OK"
            if last.get("error"):
                outcome += f": {last['error']}"
        else:
            outcome = "never ran"
        print()
        print(f"{job['name']}{'' if job['enabled'] else ' (disabled)'}")
        print(f"  what:  {job['what']}")
        print(f"  when:  {job['schedule']}")
        print(f"  next:  {_when(job['next']) if job['enabled'] else '-'}")
        print(f"  last:  {outcome}")


def cmd_job_run(cfg: Config, args) -> int:
    import threading

    from .liverun import LiveRun
    from .runlock import RunLock, describe
    from .scheduler import execute_job

    job = cfg.jobs.get(args.name)
    if job is None:
        raise ConfigError(f"unknown job {args.name} ({', '.join(sorted(cfg.jobs)) or 'none configured'})")
    lock = RunLock(cfg.scheduler.directory)
    stop = threading.Event()
    try:
        lock.acquire({"by": "cli", "job": job.name, "what": "backup"}, stop,
                     on_wait=lambda holder: log.info("waiting for %s to end", describe(holder)))
    except OSError as exc:
        raise ConfigError(f"cannot take the run lock in {cfg.scheduler.directory}: {exc}") from None
    progress = args.progress_bar
    try:
        # Followed on the web page too, which may cancel it
        with LiveRun(cfg.scheduler.directory, job.name, by="cli", progress=progress) as live:
            result, _ = execute_job(cfg, job, by="cli", progress=progress, cancel=live.cancel, live=live)
    finally:
        progress.finish()
        lock.release()
    return 0 if result["state"] == "ok" else 1


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

    jobs_help = "chunks read, decompressed and checked at the same time (default: %(default)s)"
    v = sub.add_parser("verify", help="re-read chunks and check their digests")
    v.add_argument("--target")
    v.add_argument("--jobs", type=int, default=4, help=jobs_help)
    v.add_argument("snapshot", nargs="?")
    v.set_defaults(func=cmd_verify)

    r = sub.add_parser("restore", help="restore one disk to a file (raw image or tar)")
    r.add_argument("--target")
    r.add_argument("--jobs", type=int, default=4, help=jobs_help)
    r.add_argument("snapshot", help="e.g. cluster1/vm/100/2026-11-17T22-30-00Z")
    r.add_argument("disk", help="e.g. scsi0, rootfs")
    r.add_argument("output")
    r.set_defaults(func=cmd_restore)

    rg = sub.add_parser("restore-guest", help="recreate a guest from its backup on its platform (Proxmox VE)")
    rg.add_argument("backup", help="e.g. cluster1/vm/100/2026-11-17T22-30-00Z (see 'list')")
    rg.add_argument("--target")
    rg.add_argument("--source", help="default: the source the backup came from")
    rg.add_argument("--id", help="guest id to restore as (asked on a terminal; default: the original one)")
    rg.add_argument("--host", help="node to restore on (default: the one the guest was on)")
    rg.add_argument("--storage", help="storage for every disk (default: each disk's original storage)")
    existing = rg.add_mutually_exclusive_group()
    existing.add_argument("--overwrite", action="store_true",
                          help="replace the guest with that id, disks included (its previous disks are deleted)")
    existing.add_argument("--disk-only", action="store_true",
                          help="only add the backed-up disks to the guest with that id, as unused disks")
    rg.add_argument("--start", action="store_true", help="start the guest once restored")
    stop = rg.add_mutually_exclusive_group()
    stop.add_argument("--stop", action="store_const", const="stop", dest="stop",
                      help="with --overwrite: shut the running guest down before the restore starts "
                           "(fails after 5 minutes); by default it runs until its disks are copied")
    stop.add_argument("--force-stop", action="store_const", const="force-stop", dest="stop",
                      help="with --overwrite: shut it down before the restore starts, then stop it hard "
                           "if it is still running after 5 minutes (fails after 2 more)")
    rg.add_argument("--jobs", type=int, default=4, help=jobs_help)
    rg.set_defaults(func=cmd_restore_guest)

    sc = sub.add_parser("scheduler", help="run the jobs ([job.<name>] sections) at their times, until stopped")
    sc.set_defaults(func=cmd_scheduler)
    j = sub.add_parser("job", help="recurring backups: list the jobs, or run one now")
    jsub = j.add_subparsers(dest="job_cmd", required=True)
    jl = jsub.add_parser("list", help="the jobs, their next run and their last result")
    jl.set_defaults(func=cmd_job_list)
    jr = jsub.add_parser("run", help="run a job now (waits for a backup or restore in progress)")
    jr.add_argument("name")
    jr.set_defaults(func=cmd_job_run)

    n = sub.add_parser("notify", help="notifications by email, Discord or Teams ([notify.<name>] sections)")
    nsub = n.add_subparsers(dest="notify_cmd", required=True)
    nt = nsub.add_parser("test", help="send a test message to every channel, or to one")
    nt.add_argument("name", nargs="?", help="one channel: the <name> of its [notify.<name>] section")
    nt.set_defaults(func=cmd_notify_test)

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
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "stop", None) and not args.overwrite:
        parser.error(f"--{args.stop} only applies with --overwrite")
    if getattr(args, "start", False) and getattr(args, "disk_only", False):
        parser.error("--start does not apply with --disk-only (the guest is not changed)")
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
    except (TargetUnavailable, PlatformError, RemoteCommandError, FileExistsError, FileNotFoundError) as exc:
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
