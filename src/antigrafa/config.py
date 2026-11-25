"""Loading the TOML configuration (see ``antigrafa.example.toml``).

Generic sections: ``[source.<name>]`` (one per cluster or host, with its ``type``),
``[target.<name>]``, ``[tuning]``, ``[web]`` (the web interface), ``[log]`` (the log file),
``[job.<name>]`` (recurring backups) and ``[scheduler]`` (the service that runs them). The
rest of a source section belongs to its platform, which validates it (see
``antigrafa.platforms``).
"""

from __future__ import annotations

import dataclasses
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any

from .platforms.base import check_name
from .schedule import Schedule, parse_schedule
from .targets import TargetConfig


class ConfigError(Exception):
    pass


def _system_dir(*parts: str, platform: str = sys.platform, environ=os.environ) -> PurePath:
    """Linux: ``parts`` as given (/etc/antigrafa/...); Windows: %PROGRAMDATA%/antigrafa/<the rest>."""
    if platform == "win32":
        return PureWindowsPath(environ.get("PROGRAMDATA", r"C:\ProgramData"), "antigrafa", *parts[1:])
    return PurePosixPath(*parts)


def default_config_path(platform: str = sys.platform, environ=os.environ) -> str:
    return str(_system_dir("/etc/antigrafa", "antigrafa.toml", platform=platform, environ=environ))


def default_web_dir(platform: str = sys.platform, environ=os.environ) -> str:
    return str(_system_dir("/var/lib/antigrafa", "web", platform=platform, environ=environ))


def default_scheduler_dir(platform: str = sys.platform, environ=os.environ) -> str:
    return str(_system_dir("/var/lib/antigrafa", "scheduler", platform=platform, environ=environ))


def default_log_file(platform: str = sys.platform, environ=os.environ) -> str:
    return str(_system_dir("/var/log/antigrafa", "antigrafa.log", platform=platform, environ=environ))


@dataclass
class SourceConfig:
    name: str
    type: str  # platform: "proxmox", ...
    settings: Any  # validated by the platform


@dataclass
class Tuning:
    chunk_size_mib: int = 4
    max_parallel_guests: int = 4
    max_parallel_per_host: int = 1
    # Destination
    writer_min_concurrency: int = 1
    writer_max_concurrency: int = 8
    writer_start_concurrency: int = 2
    writer_target_latency_ms: int = 250
    compression_level: int = 3
    compression_min_level: int = 1
    compression_max_level: int = 9
    # Source (hosts running the guests)
    source_max_mib_s: float = 0  # 0 = no fixed cap
    source_min_mib_s: float = 10
    host_iowait_high: float = 0.10
    host_iowait_low: float = 0.03
    low_priority: bool = True  # read at low I/O and CPU priority on the host


@dataclass
class WebConfig:
    """The web interface (``antigrafa web serve``): HTTPS only, with a login."""

    # Every interface: the page is used from other machines (HTTPS only, with a login)
    listen: str = "0.0.0.0"  # nosec B104
    port: int = 8443
    data_dir: str = ""  # users, sessions, secret key, certificate; "": default_web_dir()
    # "": <data_dir>/tls/; created (self-signed) when neither file exists
    cert_file: str = ""
    key_file: str = ""
    # Host names the browser may use; []: this machine's names and addresses
    allowed_hosts: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        for name in ("listen", "data_dir", "cert_file", "key_file"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        if not self.listen:
            raise ValueError("listen must not be empty")
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not 1 <= self.port <= 65535:
            raise ValueError(f"port must be between 1 and 65535, not {self.port!r}")
        if not isinstance(self.allowed_hosts, list) or not all(isinstance(h, str) and h for h in self.allowed_hosts):
            raise ValueError("allowed_hosts must be a list of host names")
        if bool(self.cert_file) != bool(self.key_file):
            raise ValueError("cert_file and key_file go together")

    @property
    def directory(self) -> Path:
        return Path(self.data_dir or default_web_dir())

    @property
    def certificate(self) -> Path:
        return Path(self.cert_file) if self.cert_file else self.directory / "tls" / "cert.pem"

    @property
    def private_key(self) -> Path:
        return Path(self.key_file) if self.key_file else self.directory / "tls" / "key.pem"


# Compression of the log archives: (highest level, default level). rar: -m0 to -m5,
# 7z: -mx0 to -mx9
LOG_COMPRESSIONS = {"zip": (9, 6), "tar.gz": (9, 6), "rar": (5, 3), "7z": (9, 5)}


@dataclass
class LogConfig:
    """The log file: archived and compressed when it reaches ``max_size_mib``."""

    file: str = ""  # "": default_log_file(); "none": no log file
    max_size_mib: int = 1024
    max_archives: int = 5
    compression: str = "zip"  # "zip", "tar.gz", "rar" or "7z"
    # 0 (stored) to 9 (smallest), 0 to 5 for rar; None: the default of the compression
    compression_level: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.file, str):
            raise ValueError("file must be a string")
        for name, low in (("max_size_mib", 1), ("max_archives", 0)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < low:
                raise ValueError(f"{name} must be an integer >= {low}, not {value!r}")
        if self.compression not in LOG_COMPRESSIONS:
            raise ValueError(f"compression must be one of {', '.join(LOG_COMPRESSIONS)}, not {self.compression!r}")
        highest, default = LOG_COMPRESSIONS[self.compression]
        if self.compression_level is None:
            self.compression_level = default
        level = self.compression_level
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= highest:
            raise ValueError(f"compression_level must be between 0 and {highest} for {self.compression}, not {level!r}")

    @property
    def enabled(self) -> bool:
        return self.file.lower() != "none"

    @property
    def path(self) -> Path:
        return Path(self.file or default_log_file())

    @property
    def max_bytes(self) -> int:
        return self.max_size_mib * 1024 * 1024


@dataclass
class SchedulerConfig:
    """The service that runs the jobs (``antigrafa scheduler``), and what it shares with the
    web interface: the lock that keeps one run at a time, and the state of each job."""

    data_dir: str = ""  # "": default_scheduler_dir(); writable by the scheduler and the web interface

    def __post_init__(self) -> None:
        if not isinstance(self.data_dir, str):
            raise ValueError("data_dir must be a string")

    @property
    def directory(self) -> Path:
        return Path(self.data_dir or default_scheduler_dir())


def _ids(value, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in value):
        raise ValueError(f"{name} must be a list of guest ids")
    return [str(v).strip() for v in value]


@dataclass
class JobConfig:
    """A recurring backup: which guests, from which source to which target, and when."""

    name: str
    source: str = ""  # "": the only source
    target: str = ""  # "": the only target
    guests: list[str] = field(default_factory=list)  # these ids...
    all: bool = False  # ...or every guest of the source (new ones included)...
    exclude: list[str] = field(default_factory=list)  # ...but these
    schedule: str = ""  # cron: "0 2 * * *"
    every: str = ""  # or an interval: "6h"
    at: str | list[str] = ""  # or times of the day: "02:00", ["02:00", "14:00"]
    days: list[str] = field(default_factory=list)  # with at: ["mon", "fri"]; []: every day
    enabled: bool = True
    timing: Schedule = field(init=False, repr=False)

    def __post_init__(self) -> None:
        for name in ("source", "target", "schedule", "every"):
            if not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")
        for name in ("all", "enabled"):
            if not isinstance(getattr(self, name), bool):
                raise ValueError(f"{name} must be true or false")
        self.guests = _ids(self.guests, "guests")
        self.exclude = _ids(self.exclude, "exclude")
        if self.all == bool(self.guests):
            raise ValueError("set all = true, or the guests to back up (guests = [...])")
        self.timing = parse_schedule(self.schedule, self.every, self.at, self.days)


@dataclass
class Config:
    sources: dict[str, SourceConfig]
    targets: dict[str, TargetConfig]
    tuning: Tuning
    web: WebConfig = field(default_factory=WebConfig)
    log: LogConfig = field(default_factory=LogConfig)
    jobs: dict[str, JobConfig] = field(default_factory=dict)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)

    def source(self, name: str | None) -> SourceConfig:
        return _pick(self.sources, name, "source")

    def target(self, name: str | None) -> TargetConfig:
        return _pick(self.targets, name, "target")


def _pick(items: dict, name: str | None, what: str):
    if name is None:
        if len(items) != 1:
            raise ConfigError(f"several {what}s configured, use --{what} ({', '.join(items)})")
        return next(iter(items.values()))
    try:
        return items[name]
    except KeyError:
        raise ConfigError(f"unknown {what}: {name}") from None


def build_section(cls, data: dict, section: str, /, **extra):
    """Build dataclass ``cls`` from a TOML table, with errors that name the section."""
    if not isinstance(data, dict):
        raise ConfigError(f"[{section}] must be a table")
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"[{section}]: unknown keys {', '.join(sorted(unknown))}")
    try:
        return cls(**data, **extra)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[{section}]: {exc}") from None


# Sections of the Proxmox-only format, before [source.<name>]
_OLD_SECTIONS = {"proxmox", "ssh", "nodes"}


def _source(name: str, data: dict) -> SourceConfig:
    from . import platforms  # here: platform modules import this one

    section = f"source.{name}"
    try:
        check_name(name, "source name")
    except ValueError as exc:
        raise ConfigError(f"[{section}]: {exc}") from None
    if not isinstance(data, dict) or "type" not in data:
        raise ConfigError(f"[{section}]: type is required ({', '.join(sorted(platforms.PLATFORMS))})")
    settings = dict(data)
    platform_type = settings.pop("type")
    try:
        parsed = platforms.parse_settings(platform_type, name, settings)
    except ValueError as exc:
        raise ConfigError(f"[{section}]: {exc}") from None
    return SourceConfig(name=name, type=platform_type, settings=parsed)


def _tables(raw: dict, section: str) -> dict:
    """The [<section>.<name>] tables of the file (empty if there are none)."""
    tables = raw.get(section, {})
    if not isinstance(tables, dict):
        raise ConfigError(f"[{section}] must be a table of [{section}.<name>] sections")
    return tables


def load_config(path: str | Path) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None

    old = _OLD_SECTIONS & set(raw)
    if old:
        raise ConfigError(
            f"[{'], ['.join(sorted(old))}]: these sections moved into [source.<name>] "
            'with type = "proxmox" (see antigrafa.example.toml)'
        )
    unknown = set(raw) - {"source", "target", "tuning", "web", "log", "job", "scheduler"}
    if unknown:
        raise ConfigError(f"unknown sections: {', '.join(sorted(unknown))}")

    targets = {
        name: build_section(TargetConfig, data, f"target.{name}", name=name)
        for name, data in _tables(raw, "target").items()
    }
    if not targets:
        raise ConfigError("at least one [target.<name>] section is required")

    sources = {name: _source(name, data) for name, data in _tables(raw, "source").items()}
    return Config(
        sources=sources,
        targets=targets,
        tuning=build_section(Tuning, raw.get("tuning", {}), "tuning"),
        web=build_section(WebConfig, raw.get("web", {}), "web"),
        log=build_section(LogConfig, raw.get("log", {}), "log"),
        jobs={name: _job(name, data, sources, targets) for name, data in _tables(raw, "job").items()},
        scheduler=build_section(SchedulerConfig, raw.get("scheduler", {}), "scheduler"),
    )


def _job(name: str, data: dict, sources: dict, targets: dict) -> JobConfig:
    section = f"job.{name}"
    try:
        check_name(name, "job name")
    except ValueError as exc:
        raise ConfigError(f"[{section}]: {exc}") from None
    job = build_section(JobConfig, data, section, name=name)
    # The source and the target exist: a job never fails at 2 a.m. on a typo
    for what, items in (("source", sources), ("target", targets)):
        chosen = getattr(job, what)
        if chosen and chosen not in items:
            raise ConfigError(f"[{section}]: unknown {what} {chosen!r} ({', '.join(sorted(items)) or 'none configured'})")
        if not chosen and len(items) != 1:
            raise ConfigError(f"[{section}]: several {what}s configured, set {what} ({', '.join(sorted(items))})"
                              if items else f"[{section}]: a [{what}.<name>] section is required")
    return job
