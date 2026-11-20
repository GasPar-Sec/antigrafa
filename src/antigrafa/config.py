"""Loading the TOML configuration (see ``antigrafa.example.toml``).

Generic sections: ``[source.<name>]`` (one per cluster or host, with its ``type``),
``[target.<name>]``, ``[tuning]`` and ``[web]`` (the web interface). The rest of a source section belongs to its
platform, which validates it (see ``antigrafa.platforms``).
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


@dataclass
class Config:
    sources: dict[str, SourceConfig]
    targets: dict[str, TargetConfig]
    tuning: Tuning
    web: WebConfig = field(default_factory=WebConfig)

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
    unknown = set(raw) - {"source", "target", "tuning", "web"}
    if unknown:
        raise ConfigError(f"unknown sections: {', '.join(sorted(unknown))}")

    targets = {
        name: build_section(TargetConfig, data, f"target.{name}", name=name)
        for name, data in _tables(raw, "target").items()
    }
    if not targets:
        raise ConfigError("at least one [target.<name>] section is required")

    return Config(
        sources={name: _source(name, data) for name, data in _tables(raw, "source").items()},
        targets=targets,
        tuning=build_section(Tuning, raw.get("tuning", {}), "tuning"),
        web=build_section(WebConfig, raw.get("web", {}), "web"),
    )
