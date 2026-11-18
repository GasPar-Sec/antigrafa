"""Proxmox VE: guests listed and snapshotted through the API, disks read over SSH.

Configuration (``[source.<name>]`` with ``type = "proxmox"``)::

    [source.cluster1]
    type = "proxmox"
    url = "https://pve1.example.lan:8006"
    token_id = "antigrafa@pve!backup"
    token_secret_file = "/etc/antigrafa/token"
    ca_file = "/etc/antigrafa/pve-root-ca.pem"
    # verify_tls = false                  # labs only: do not verify the certificate

    [source.cluster1.ssh]                 # how to reach the nodes
    user = "root"
    identity_file = "/etc/antigrafa/id_ed25519"

    [source.cluster1.nodes.pve1]          # SSH address of a node (default: its name)
    host = "10.0.0.11"
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ...config import ConfigError, build_section
from ...ssh import SSHHost
from ..base import BackupPlan, Guest, Platform
from .api import PVE_TYPES, ProxmoxAPI, ProxmoxError
from .capture import UnsupportedStorage, guest_disks, make_source

__all__ = ["ProxmoxAPI", "ProxmoxError", "ProxmoxPlatform", "ProxmoxSettings", "UnsupportedStorage"]


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
class ProxmoxSettings:
    url: str
    token_id: str
    token_secret: str = ""
    token_secret_file: str | None = None
    ca_file: str | None = None
    # false: do not verify the API certificate (labs only, the token can be intercepted)
    verify_tls: bool = True
    ssh: SSHConfig = field(default_factory=SSHConfig)
    nodes: dict[str, NodeConfig] = field(default_factory=dict)
    section: str = "source"  # for error messages

    def secret(self) -> str:
        if self.token_secret_file:
            return Path(self.token_secret_file).read_text().strip()
        if not self.token_secret:
            raise ConfigError(f"[{self.section}]: token_secret or token_secret_file is required")
        return self.token_secret

    def node_host(self, node: str) -> str:
        return self.nodes[node].host if node in self.nodes else node


def parse_settings(name: str, data: dict) -> ProxmoxSettings:
    section = f"source.{name}"
    verify = data.get("verify_tls", True)
    if not isinstance(verify, bool):
        raise ConfigError(f"[{section}]: verify_tls must be true or false")
    if not verify and data.get("ca_file"):
        # Contradictory: a CA to verify against, and no verification
        raise ConfigError(f"[{section}]: ca_file and verify_tls = false cannot be used together")
    data = dict(data)
    ssh = build_section(SSHConfig, data.pop("ssh", {}), f"{section}.ssh")
    nodes_data = data.pop("nodes", {})
    if not isinstance(nodes_data, dict):
        raise ConfigError(f"[{section}.nodes] must be a table")
    nodes = {n: build_section(NodeConfig, d, f"{section}.nodes.{n}") for n, d in nodes_data.items()}
    return build_section(ProxmoxSettings, data, section, ssh=ssh, nodes=nodes, section=section)


class ProxmoxPlatform(Platform):
    type = "proxmox"

    def __init__(
        self,
        name: str,
        settings: ProxmoxSettings,
        api: ProxmoxAPI | None = None,
        ssh_for_host: Callable[[str], SSHHost] | None = None,
    ) -> None:
        super().__init__(name)
        self.settings = settings
        if api is None:
            api = ProxmoxAPI(
                settings.url,
                settings.token_id,
                settings.secret(),
                ca_file=settings.ca_file,
                verify_tls=settings.verify_tls,
            )
        self.api = api
        self.ssh_for_host = ssh_for_host or self._ssh

    def _ssh(self, node: str) -> SSHHost:
        s = self.settings.ssh
        host = self.settings.node_host(node)
        return SSHHost(host, user=s.user, port=s.port, identity_file=s.identity_file, options=s.options)

    def guests(self) -> list[Guest]:
        return self.api.guests()

    def plan_backup(self, guest: Guest, snapshot: str, low_priority: bool = True) -> BackupPlan:
        config = self.api.guest_config(guest)
        disks = guest_disks(PVE_TYPES[guest.kind], config)
        ssh = self.ssh_for_host(guest.host)
        storages = {d.storage: self.api.storage(d.storage) for d in disks}
        sources = [make_source(ssh, storages[d.storage], d, snapshot, low_priority) for d in disks]
        return BackupPlan(config=config, disks=sources)

    def create_snapshot(self, guest: Guest, snapshot: str) -> None:
        self.api.create_snapshot(guest, snapshot, "Temporary Antigrafa snapshot, deleted after the backup")

    def delete_snapshot(self, guest: Guest, snapshot: str) -> None:
        self.api.delete_snapshot(guest, snapshot)

    def leftover_snapshots(self, guest: Guest, prefix: str) -> list[str]:
        return [s["name"] for s in self.api.list_snapshots(guest) if s.get("name", "").startswith(prefix)]

    def host_iowait(self, host: str) -> float:
        return self.api.node_iowait(host)


def create(name: str, settings: ProxmoxSettings) -> ProxmoxPlatform:
    return ProxmoxPlatform(name, settings)
