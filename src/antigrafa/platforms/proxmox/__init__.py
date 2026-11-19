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
from ..base import AccessDenied, BackupPlan, Guest, Platform, PlatformError
from .api import PVE_TYPES, ProxmoxAPI, ProxmoxError
from .capture import UnsupportedStorage, guest_disks, make_source

__all__ = ["ProxmoxAPI", "ProxmoxError", "ProxmoxPlatform", "ProxmoxSettings", "UnsupportedStorage"]

# Privileges needed by a backup, and why (what fails without them)
GUEST_PRIVS = ("VM.Audit", "VM.Snapshot")  # /vms/<id>: read the configuration, snapshot
HOST_PRIVS = ("Sys.Audit",)  # /nodes/<node>: host load (iowait), to throttle reads
STORAGE_PRIVS = ("Datastore.Audit",)  # /storage/<id>: how to read the disks
ROLE_PRIVS = " ".join(GUEST_PRIVS + HOST_PRIVS + STORAGE_PRIVS)


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

    def check_access(self, guests: list[Guest]) -> None:
        missing: list[str] = []

        def need(path: str, privs: tuple[str, ...], why: str) -> None:
            have = self.api.permissions(path)  # one API call per path
            lacking = [p for p in privs if p not in have]
            if lacking:
                missing.append(f"{', '.join(lacking)} on {path} ({why})")

        if not guests:
            # The guest list silently hides what the token cannot see: an empty list
            # may just mean "no right at all"
            need("/vms", ("VM.Audit",), "list the guests")
        for g in guests:
            need(f"/vms/{g.id}", GUEST_PRIVS, g.label)
        for host in sorted({g.host for g in guests}):
            need(f"/nodes/{host}", HOST_PRIVS, "host load")
        if not missing:  # the configurations can be read: check their storages
            storages = set()
            for g in guests:
                try:
                    storages.update(d.storage for d in guest_disks(PVE_TYPES[g.kind], self.api.guest_config(g)))
                except PlatformError:
                    continue  # unsupported disk: that guest fails on its own, with its reason
            for storage in sorted(storages):
                need(f"/storage/{storage}", STORAGE_PRIVS, "storage configuration")
        if missing:
            token = self.settings.token_id
            raise AccessDenied(
                f"API token {token} lacks permissions, nothing was done:\n"
                + "".join(f"  - {m}\n" for m in missing)
                + "Grant them on the cluster, for example:\n"
                f'  pveum role add AntigrafaBackup --privs "{ROLE_PRIVS}"\n'
                f"  pveum acl modify / --roles AntigrafaBackup --tokens '{token}'\n"
                "With privilege separation, the token's user needs them too."
            )

    def leftover_snapshots(self, guest: Guest, prefix: str) -> list[str]:
        return [s["name"] for s in self.api.list_snapshots(guest) if s.get("name", "").startswith(prefix)]

    def host_iowait(self, host: str) -> float:
        return self.api.node_iowait(host)


def create(name: str, settings: ProxmoxSettings) -> ProxmoxPlatform:
    return ProxmoxPlatform(name, settings)
