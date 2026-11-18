"""Loading the TOML configuration (see ``antigrafa.example.toml``)."""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .targets import TargetConfig


class ConfigError(Exception):
    pass


@dataclass
class ProxmoxConfig:
    url: str
    token_id: str
    token_secret: str = ""
    token_secret_file: str | None = None
    ca_file: str | None = None

    def secret(self) -> str:
        if self.token_secret_file:
            return Path(self.token_secret_file).read_text().strip()
        if not self.token_secret:
            raise ConfigError("[proxmox]: token_secret or token_secret_file is required")
        return self.token_secret


@dataclass
class SSHConfig:
    user: str = "root"
    port: int = 22
    identity_file: str | None = None
    options: list[str] = field(default_factory=list)


@dataclass
class NodeConfig:
    host: str


@dataclass
class Tuning:
    chunk_size_mib: int = 4
    max_parallel_guests: int = 4
    max_parallel_per_node: int = 1
    # Destination
    writer_min_concurrency: int = 1
    writer_max_concurrency: int = 8
    writer_start_concurrency: int = 2
    writer_target_latency_ms: int = 250
    compression_level: int = 3
    compression_min_level: int = 1
    compression_max_level: int = 9
    # Source (Proxmox nodes)
    source_max_mib_s: float = 0  # 0 = no fixed cap
    source_min_mib_s: float = 10
    node_iowait_high: float = 0.10
    node_iowait_low: float = 0.03
    low_priority: bool = True  # ionice/nice on the node


@dataclass
class Config:
    proxmox: ProxmoxConfig | None
    ssh: SSHConfig
    nodes: dict[str, NodeConfig]
    targets: dict[str, TargetConfig]
    tuning: Tuning

    def target(self, name: str | None) -> TargetConfig:
        if name is None:
            if len(self.targets) != 1:
                raise ConfigError(f"several targets configured, use --target ({', '.join(self.targets)})")
            return next(iter(self.targets.values()))
        try:
            return self.targets[name]
        except KeyError:
            raise ConfigError(f"unknown target: {name}") from None

    def node_host(self, node: str) -> str:
        return self.nodes[node].host if node in self.nodes else node


def _build(cls, data: dict, section: str, **extra):
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError(f"[{section}]: unknown keys {', '.join(sorted(unknown))}")
    try:
        return cls(**data, **extra)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"[{section}]: {exc}") from None


def _proxmox(data: dict) -> ProxmoxConfig:
    if "verify_tls" in data:
        # Removed: an unverified connection would hand the API token to anyone on
        # the path. The cluster's own CA makes verification work out of the box.
        raise ConfigError(
            "[proxmox]: verify_tls is no longer supported, the certificate is always "
            "verified: set ca_file = \"/etc/antigrafa/pve-root-ca.pem\" (copy of "
            "/etc/pve/pve-root-ca.pem from a node)"
        )
    return _build(ProxmoxConfig, data, "proxmox")


def load_config(path: str | Path) -> Config:
    try:
        raw = tomllib.loads(Path(path).read_text())
    except FileNotFoundError:
        raise ConfigError(f"configuration file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from None

    unknown = set(raw) - {"proxmox", "ssh", "nodes", "target", "tuning"}
    if unknown:
        raise ConfigError(f"unknown sections: {', '.join(sorted(unknown))}")

    targets = {
        name: _build(TargetConfig, data, f"target.{name}", name=name) for name, data in raw.get("target", {}).items()
    }
    if not targets:
        raise ConfigError("at least one [target.<name>] section is required")

    return Config(
        proxmox=_proxmox(raw["proxmox"]) if "proxmox" in raw else None,
        ssh=_build(SSHConfig, raw.get("ssh", {}), "ssh"),
        nodes={n: _build(NodeConfig, d, f"nodes.{n}") for n, d in raw.get("nodes", {}).items()},
        targets=targets,
        tuning=_build(Tuning, raw.get("tuning", {}), "tuning"),
    )
