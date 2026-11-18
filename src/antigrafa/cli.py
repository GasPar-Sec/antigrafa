"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys

from . import platforms
from .config import Config, ConfigError, load_config
from .index import read_index, read_manifest
from .pipeline import restore_stream
from .platforms.base import Platform, PlatformError
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


def _human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return str(n)


def cmd_guests(cfg: Config, args) -> int:
    for g in _platform(cfg, args.source).guests():
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
            raise ConfigError(f"guests not found: {', '.join(sorted(missing))}")
        guests = [g for g in guests if g.id in wanted]
    excluded = set(args.exclude or [])
    guests = [g for g in guests if g.id not in excluded]

    runner = BackupRunner(cfg.tuning, platform, repo)
    try:
        results = runner.run(guests)
    finally:
        runner.close()

    print()
    for r in results:
        status = "OK    " if r.ok else "FAILED"
        detail = r.snapshot if r.ok else r.error
        print(f"{status} {r.guest.label:<30} {r.duration:>7.0f} s  {detail}")
    s = runner.controller.stats()
    if s["raw_bytes"]:
        print(
            f"\nNew data: {_human(s['raw_bytes'])} -> {_human(s['stored_bytes'])} written "
            f"(final zstd level {s['level']}, {s['concurrency']} parallel writes)"
        )
    return 0 if all(r.ok for r in results) else 1


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
    checked: dict[bytes, bool] = {}
    bad = 0
    for snap in snaps:
        snap_dir = repo.resolve(snap)
        m = read_manifest(snap_dir)
        snap_bad = 0
        for d in m["disks"]:
            _, _, digests = read_index(snap_dir / d["index"])
            for digest in digests:
                if digest not in checked:
                    try:
                        repo.store.read(digest)
                        checked[digest] = True
                    except Exception as exc:
                        log.error("%s: chunk %s: %s", snap, digest.hex(), exc)
                        checked[digest] = False
                snap_bad += not checked[digest]
        print(f"{'OK   ' if not snap_bad else 'ERROR'} {snap}" + (f" ({snap_bad} bad chunks)" if snap_bad else ""))
        bad += snap_bad
    return 1 if bad else 0


def cmd_restore(cfg: Config, args) -> int:
    repo = _repo(cfg, args.target)
    snap_dir = repo.resolve(args.snapshot)
    m = read_manifest(snap_dir)
    disk = next((d for d in m["disks"] if d["key"] == args.disk), None)
    if disk is None:
        raise ConfigError(f"no disk {args.disk}; available: {', '.join(d['key'] for d in m['disks'])}")
    size, _, digests = read_index(snap_dir / disk["index"])
    with open(args.output, "xb") as out:  # "x": never overwrite an existing file
        restore_stream(repo.store, digests, out, size)
    print(f"{args.output}: {_human(size)} restored ({disk['format']})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="antigrafa", description="Backups for virtual machines and containers")
    p.add_argument("-c", "--config", default="/etc/antigrafa/antigrafa.toml")
    p.add_argument("-v", "--verbose", action="store_true")
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
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return args.func(load_config(args.config), args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except (TargetUnavailable, PlatformError, FileExistsError, FileNotFoundError) as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        log.error("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
