"""Loading the TOML configuration (see ``antigrafa.example.toml``).

Generic sections: ``[source.<name>]`` (one per cluster or host, with its ``type``),
``[target.<name>]`` and ``[tuning]``. The rest of a source section belongs to its
platform, which validates it (see ``antigrafa.platforms``).
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .platforms.base import check_name
from .targets import TargetConfig


class ConfigError(Exception):
    pass


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
class Config:
    sources: dict[str, SourceConfig]
    targets: dict[str, TargetConfig]
    tuning: Tuning

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
    unknown = set(raw) - {"source", "target", "tuning"}
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
    )
