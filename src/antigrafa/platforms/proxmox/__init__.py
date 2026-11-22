"""Proxmox VE: guests listed and snapshotted through the API, disks read over SSH.

Configuration (``[source.<name>]`` with ``type = "proxmox"``)::

    [source.cluster1]
    type = "proxmox"
    url = "https://pve1.example.lan:8006"
    token_id = "antigrafa@pve!backup"
    token_secret_file = "/etc/antigrafa/token"
    ca_file = "/etc/antigrafa/pve-root-ca.pem"
    # verify_tls = false                  # labs only: do not verify the certificate
    # remote_hash = false                 # send every chunk (default: only changed ones)
    # snapshot_mode = "storage"           # snapshot the volumes ourselves (default: "proxmox")
    #                                     # "qemu": running VMs backed up by QEMU, as PBS does
    #                                     # "auto": the best of the three, per guest
    # fleecing_dir = "/var/lib/antigrafa/fleecing"  # "qemu": temporary images on the nodes

    [source.cluster1.ssh]                 # how to reach the nodes
    user = "root"
    identity_file = "/etc/antigrafa/id_ed25519"

    [source.cluster1.nodes.pve1]          # SSH address of a node (default: its name)
    host = "10.0.0.11"
"""

from __future__ import annotations

import contextlib
import logging
import shlex
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ...config import ConfigError, build_section
from ...ssh import SSHHost
from ..base import AccessDenied, BackupPlan, Guest, Platform, PlatformError
from .api import PVE_TYPES, ProxmoxAPI, ProxmoxError
from .capture import (
    UnsupportedStorage,
    VolumeSource,
    ZfsZvolSource,
    guest_disks,
    make_source,
    parse_size,
    snapshot_blockers,
)
from .qemu import QemuDiskSource, QemuFleecing

log = logging.getLogger(__name__)

# Always succeeds: an empty answer means python3 is missing (remote hashing, see delta)
PYTHON3_CHECK = "command -v python3 || true"
# What Proxmox answers while a failed snapshot deletion still holds the guest
DELETE_LOCK = "locked (snapshot-delete)"
# "proxmox": snapshot through the API (Proxmox snapshots every disk of the guest);
# "storage": Antigrafa snapshots only the volumes it backs up, directly on their storage;
# "qemu": running VMs backed up by QEMU (any storage, see .qemu), containers as "storage";
# "auto": per guest, the first of these three that works for it
SNAPSHOT_MODES = ("proxmox", "storage", "qemu", "auto")
# Seconds between the tries to thaw a guest (storage snapshots)
THAW_RETRY_DELAYS = (1.0, 2.0, 4.0)

__all__ = ["GuestSettings", "ProxmoxAPI", "ProxmoxError", "ProxmoxPlatform", "ProxmoxSettings", "UnsupportedStorage"]

# Privileges needed by a backup, and why (what fails without them)
GUEST_PRIVS = ("VM.Audit", "VM.Snapshot")  # /vms/<id>: read the configuration, snapshot
# snapshot_mode = "storage" or "qemu": done over SSH, not through the API
STORAGE_MODE_GUEST_PRIVS = ("VM.Audit",)
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
class GuestSettings:
    """``[source.<name>.guests.<id>]``: what differs for one guest."""

    snapshot_mode: str | None = None  # None: the source's

    def __post_init__(self) -> None:
        if self.snapshot_mode is not None and self.snapshot_mode not in SNAPSHOT_MODES:
            raise ValueError(f"snapshot_mode must be one of {', '.join(SNAPSHOT_MODES)}, not {self.snapshot_mode!r}")


@dataclass
class ProxmoxSettings:
    url: str
    token_id: str
    token_secret: str = ""
    token_secret_file: str | None = None
    ca_file: str | None = None
    # false: do not verify the API certificate (labs only, the token can be intercepted)
    verify_tls: bool = True
    # Hash the chunks on the node (python3): only the chunks that changed since the
    # previous backup cross the network. Costs CPU on the node, at low priority.
    remote_hash: bool = True
    # "proxmox" (default), "storage", "qemu" or "auto": see SNAPSHOT_MODES
    snapshot_mode: str = "proxmox"
    # ZFS volumes (zvols) captured by storage snapshots: the snapshot of a backup stays
    # on the node as the base of the next one, which then reads only what ZFS says
    # changed. "auto" picks storage snapshots for guests with zvols.
    incremental: bool = True
    # "qemu" mode: directory, on each node, of the temporary images that keep the old
    # blocks the VM overwrites during its backup (created 0700; refused if others can
    # write to it: the images are created by root under predictable names)
    fleecing_dir: str = "/var/lib/antigrafa/fleecing"
    ssh: SSHConfig = field(default_factory=SSHConfig)
    nodes: dict[str, NodeConfig] = field(default_factory=dict)
    # Guest id -> its own settings (snapshot_mode)
    guests: dict[str, GuestSettings] = field(default_factory=dict)
    section: str = "source"  # for error messages

    def __post_init__(self) -> None:
        if self.snapshot_mode not in SNAPSHOT_MODES:
            raise ValueError(f"snapshot_mode must be one of {', '.join(SNAPSHOT_MODES)}, not {self.snapshot_mode!r}")
        if not self.fleecing_dir.startswith("/"):
            raise ValueError(f"fleecing_dir must be an absolute path on the nodes, not {self.fleecing_dir!r}")

    def secret(self) -> str:
        if self.token_secret_file:
            return Path(self.token_secret_file).read_text().strip()
        if not self.token_secret:
            raise ConfigError(f"[{self.section}]: token_secret or token_secret_file is required")
        return self.token_secret

    def node_host(self, node: str) -> str:
        return self.nodes[node].host if node in self.nodes else node

    def mode_for(self, guest_id: str) -> str:
        """snapshot_mode of a guest: its own, else the source's."""
        own = self.guests.get(guest_id)
        return own.snapshot_mode if own is not None and own.snapshot_mode else self.snapshot_mode


def parse_settings(name: str, data: dict) -> ProxmoxSettings:
    section = f"source.{name}"
    for flag in ("verify_tls", "remote_hash", "incremental"):
        if not isinstance(data.get(flag, True), bool):
            raise ConfigError(f"[{section}]: {flag} must be true or false")
    verify = data.get("verify_tls", True)
    if not verify and data.get("ca_file"):
        # Contradictory: a CA to verify against, and no verification
        raise ConfigError(f"[{section}]: ca_file and verify_tls = false cannot be used together")
    data = dict(data)
    ssh = build_section(SSHConfig, data.pop("ssh", {}), f"{section}.ssh")
    nodes_data = data.pop("nodes", {})
    if not isinstance(nodes_data, dict):
        raise ConfigError(f"[{section}.nodes] must be a table")
    nodes = {n: build_section(NodeConfig, d, f"{section}.nodes.{n}") for n, d in nodes_data.items()}
    guests_data = data.pop("guests", {})
    if not isinstance(guests_data, dict) or not all(isinstance(d, dict) for d in guests_data.values()):
        raise ConfigError(f"[{section}.guests.<id>] must be tables")
    guests = {}
    for gid, d in guests_data.items():
        if not str(gid).isdigit():
            raise ConfigError(f"[{section}.guests.{gid}]: a guest is named by its id (650), not {gid!r}")
        guests[str(gid)] = build_section(GuestSettings, d, f"{section}.guests.{gid}")
    return build_section(ProxmoxSettings, data, section, ssh=ssh, nodes=nodes, guests=guests, section=section)


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
        self._python3: dict[str, bool] = {}  # node -> python3 available (remote hashing)
        # Guest id -> (configuration, disk readers) of its current backup: the storage
        # snapshots are taken and deleted volume by volume
        self._plans: dict[str, tuple[dict, list[VolumeSource]]] = {}
        # "proxmox", "storage" or "qemu" for each guest planned (snapshot_mode = "auto")
        self._methods: dict[str, str] = {}
        # "qemu" mode: VMs locked by Antigrafa, and the QEMU side of those running
        self._locked: set[str] = set()
        self._fleecings: dict[str, QemuFleecing] = {}

    def _ssh(self, node: str) -> SSHHost:
        s = self.settings.ssh
        host = self.settings.node_host(node)
        return SSHHost(host, user=s.user, port=s.port, identity_file=s.identity_file, options=s.options)

    def guests(self) -> list[Guest]:
        return self.api.guests()

    def disk_sizes(self, guest: Guest) -> dict[str, int]:
        """The ``size=`` Proxmox records for each disk (container: the disk's size, not its data)."""
        disks = guest_disks(PVE_TYPES[guest.kind], self.api.guest_config(guest))
        sizes = {d.key: parse_size(d.options.get("size", "")) for d in disks}
        return {key: size for key, size in sizes.items() if size is not None}

    def plan_backup(
        self, guest: Guest, snapshot: str, low_priority: bool = True, previous: dict | None = None
    ) -> BackupPlan:
        config = self.api.guest_config(guest)
        disks = guest_disks(PVE_TYPES[guest.kind], config)
        ssh = self.ssh_for_host(guest.host)
        storages = {d.storage: self.api.storage(d.storage) for d in disks}
        if self.settings.mode_for(guest.id) == "auto":
            method = self._choose_method(guest, config, disks, storages)
        else:
            method = self._configured_method(guest)
        self._methods[guest.id] = method
        if method == "qemu":
            if not self._has_python3(guest.host, ssh):
                raise PlatformError(f'{guest.host}: snapshot_mode = "qemu" needs python3 on the node '
                                    "(backup through QEMU)")
            remote_hash = self.settings.remote_hash
            sources: list[VolumeSource] = [
                self._qemu_source(ssh, storages[d.storage], d, snapshot, low_priority, remote_hash) for d in disks
            ]
        else:
            remote_hash = self.settings.remote_hash and self._has_python3(guest.host, ssh)
            sources = [make_source(ssh, storages[d.storage], d, snapshot, low_priority, remote_hash) for d in disks]
        if method == "proxmox":
            self._check_snapshot_possible(guest, config, storages)
        self._attach_bases(method, sources, previous)
        self._plans[guest.id] = (config, sources)
        return BackupPlan(config=config, disks=sources)

    def _attach_bases(self, method: str, sources: list[VolumeSource], previous: dict | None) -> None:
        """Incremental reads: the base each zvol kept at the previous backup, and whether
        this backup keeps one (only its own storage snapshots can stay)."""
        entries = {}
        for d in (previous or {}).get("disks") or []:
            if isinstance(d, dict) and "key" in d:
                entries[d["key"]] = d
        for src in sources:
            if not isinstance(src, ZfsZvolSource) or src.live:
                continue
            src.keeps_base = self.settings.incremental and method == "storage"
            entry = entries.get(src.key) or {}
            base = entry.get("source_snapshot")
            if entry.get("volume") != src.volume or entry.get("format") != "raw" or not isinstance(base, dict):
                continue
            if base.get("name") and base.get("guid"):
                src.base = (str(base["name"]), str(base["guid"]))
                src.base_size = entry.get("size")

    def _configured_method(self, guest: Guest) -> str:
        """How the guest is captured: "proxmox", "storage" or "qemu"."""
        mode = self.settings.mode_for(guest.id)
        if mode == "auto":
            return self._methods.get(guest.id, "proxmox")  # chosen by plan_backup
        if mode == "qemu" and guest.kind != "vm":
            return "storage"  # QEMU runs VMs only
        return mode

    def _choose_method(self, guest: Guest, config: dict, disks, storages: dict[str, dict]) -> str:
        """snapshot_mode = "auto": the first way that works for this guest.

        1. "proxmox": Proxmox can snapshot every disk, and the token may ask it to;
        2. "storage": every disk backed up has a storage snapshot Antigrafa can read;
        3. "qemu" (VMs): any storage, QEMU keeps the frozen view.
        """
        unreadable = ""
        for d in disks:
            try:
                make_source(None, storages[d.storage], d, "")  # type: ignore[arg-type]  # only checks
            except UnsupportedStorage as exc:
                unreadable = str(exc)
                break
        blockers = snapshot_blockers(PVE_TYPES[guest.kind], config, self._storage_lookup(dict(storages)))
        zvols = [d.key for d in disks if storages[d.storage].get("type") == "zfspool"
                 and not d.volname.startswith("subvol-")]
        if not unreadable and zvols and self.settings.incremental:
            method, why = "storage", f"incremental: the snapshot of {', '.join(zvols)} stays as the next base"
        elif not unreadable and not blockers and self._may_snapshot(guest):
            method, why = "proxmox", "Proxmox can snapshot every disk"
        elif not unreadable:
            why = f"Proxmox cannot snapshot {blockers[0]}" if blockers else "the token has no VM.Snapshot right"
            method = "storage"
        elif guest.kind == "vm":
            method, why = "qemu", unreadable
        else:
            raise UnsupportedStorage(f"{unreadable} (containers need a storage with snapshots)")
        log.info("%s: captured by %s (%s)", guest.label, method, why)
        return method

    def _may_snapshot(self, guest: Guest) -> bool:
        try:
            return "VM.Snapshot" in self.api.permissions(f"/vms/{guest.id}")
        except Exception as exc:
            log.debug("%s: snapshot right unknown: %s", guest.label, exc)
            return False

    def _storage_lookup(self, known: dict[str, dict]) -> Callable[[str], dict | None]:
        def storage_of(storage_id: str) -> dict | None:
            if storage_id not in known:
                try:
                    known[storage_id] = self.api.storage(storage_id)
                except ProxmoxError:
                    return None  # not visible to the token: Proxmox will judge
            return known[storage_id]

        return storage_of

    def _check_snapshot_possible(self, guest: Guest, config: dict, known: dict[str, dict]) -> None:
        """Before the snapshot: name the disks Proxmox would refuse to snapshot.

        Otherwise Proxmox only says "snapshot feature is not available", without which disk.
        """
        blockers = snapshot_blockers(PVE_TYPES[guest.kind], config, self._storage_lookup(known))
        if blockers:
            raise UnsupportedStorage(
                "Proxmox cannot snapshot this guest (every disk counts, backup=0 ones included): "
                + "; ".join(blockers)
                + '. Set snapshot_mode = "auto" (Antigrafa picks a way that works), or "qemu" '
                "(VMs: QEMU keeps the frozen view, any storage), "
                'or flag these disks backup=0 and set snapshot_mode = "storage", or move them '
                "to a storage with snapshots"
            )

    def _qemu_source(
        self, ssh: SSHHost, storage_cfg: dict, disk, snapshot: str, low_priority: bool, remote_hash: bool
    ) -> QemuDiskSource:
        """Read through QEMU while the VM runs; from the volume itself if it is stopped."""
        try:
            direct: VolumeSource | None = make_source(
                ssh, storage_cfg, disk, "", low_priority, remote_hash, live=True
            )
            why = ""
        except UnsupportedStorage as exc:
            direct, why = None, f"cannot be read while the VM is stopped: {exc}"
        # tpmstate is used by swtpm, not by QEMU: always read from its volume (a few KiB,
        # written only by TPM operations)
        through_qemu = not disk.key.startswith("tpmstate")
        if not through_qemu and direct is None:
            raise UnsupportedStorage(f"{disk.key} ({disk.volid}): {why}")
        return QemuDiskSource(ssh, disk, snapshot, direct, why, low_priority, remote_hash, through_qemu)

    def _has_python3(self, node: str, ssh: SSHHost) -> bool:
        """Checked once per node. Without python3, every chunk is sent (as before)."""
        if node not in self._python3:
            try:
                # "|| true": an empty answer means missing; an error means the check itself failed
                found = bool(ssh.run(PYTHON3_CHECK).strip())
            except Exception as exc:
                log.warning("%s: python3 check failed, every chunk is sent: %s", node, exc)
                found = False
            else:
                if not found:
                    log.warning(
                        "%s: python3 not found, chunks cannot be hashed there: every chunk is sent "
                        "(install it on the node: apt install python3)",
                        node,
                    )
            self._python3[node] = found
        return self._python3[node]

    def create_snapshot(self, guest: Guest, snapshot: str) -> None:
        method = self._configured_method(guest)
        if method == "qemu":
            self._start_qemu_backup(guest, snapshot)
            return
        if method == "storage":
            self._create_storage_snapshots(guest, snapshot)
            return
        self.api.create_snapshot(guest, snapshot, "Temporary Antigrafa snapshot, deleted after the backup")

    def _plan(self, guest: Guest, snapshot: str) -> tuple[dict, list[VolumeSource]]:
        config, sources = self._plans.get(guest.id, ({}, []))
        if not sources or any(s.snapname != snapshot for s in sources):
            raise PlatformError(f"{guest.label}: no backup planned for snapshot {snapshot}")
        return config, sources

    def _create_storage_snapshots(self, guest: Guest, snapshot: str) -> None:
        """Freeze the guest, snapshot each volume backed up, thaw: a second or two."""
        config, sources = self._plan(guest, snapshot)
        ssh = self.ssh_for_host(guest.host)
        taken: list[VolumeSource] = []
        try:
            with self._frozen(guest, config, ssh):
                for src in sources:
                    src.take_snapshot()
                    taken.append(src)
        except BaseException:
            for src in taken:  # all or nothing: never leave a snapshot behind
                try:
                    src.drop_snapshot()
                except Exception as exc:
                    log.error("%s: FAILED to delete snapshot %s of %s, remove it by hand: %s",
                              guest.label, snapshot, src.volume, exc)
            raise

    @contextlib.contextmanager
    def _frozen(self, guest: Guest, config: dict, ssh: SSHHost) -> Iterator[None]:
        """Filesystems frozen, as Proxmox does for its own snapshots.

        VM: through the qemu-guest-agent, when it is enabled. Container: its processes
        frozen, then its writes flushed. A stopped guest writes nothing: nothing to do.
        Without a freeze, the snapshot is crash-consistent (like a power cut).
        """
        gid = shlex.quote(guest.id)
        if guest.status != "running":
            yield
            return
        if guest.kind == "vm":
            if not freeze_enabled(config):
                log.info("%s: no qemu-guest-agent freeze: crash-consistent snapshot", guest.label)
                yield
                return
            freeze, thaw = f"qm guest cmd {gid} fsfreeze-freeze", f"qm guest cmd {gid} fsfreeze-thaw"
        else:
            freeze, thaw = f"lxc-freeze -n {gid} && sync", f"lxc-unfreeze -n {gid}"
        try:
            ssh.run(freeze)
        except Exception as exc:
            log.warning("%s: filesystems not frozen, crash-consistent snapshot: %s", guest.label, exc)
            with contextlib.suppress(Exception):
                ssh.run(thaw)  # a freeze that timed out may still have happened
            yield
            return
        log.debug("%s: frozen", guest.label)
        try:
            yield
        finally:
            self._thaw(guest, ssh, thaw)

    def _thaw(self, guest: Guest, ssh: SSHHost, thaw: str) -> None:
        for delay in (*THAW_RETRY_DELAYS, None):
            try:
                ssh.run(thaw)
                log.debug("%s: thawed", guest.label)
                return
            except Exception as exc:
                if delay is None:
                    # The guest cannot write until then: say it loudly, with the fix
                    log.error("%s: STILL FROZEN, thaw it by hand on %s: %s (%s)", guest.label, guest.host, thaw, exc)
                    raise PlatformError(f"{guest.label}: still frozen, run on {guest.host}: {thaw}") from exc
                log.warning("%s: thaw failed, new try in %g s: %s", guest.label, delay, exc)
                time.sleep(delay)

    def _start_qemu_backup(self, guest: Guest, snapshot: str) -> None:
        """Lock the VM; if it runs, let QEMU keep a frozen view of its disks (see .qemu)."""
        config, sources = self._plan(guest, snapshot)
        ssh = self.ssh_for_host(guest.host)
        gid = shlex.quote(guest.id)
        # Like vzdump: no migration, snapshot, disk move or start while it is read
        ssh.run(f"qm set {gid} --lock backup")
        self._locked.add(guest.id)
        try:
            if "status: running" not in ssh.run(f"qm status {gid}"):
                log.info("%s: stopped, its volumes are read directly", guest.label)
                return
            qemu_sources = [s for s in sources if isinstance(s, QemuDiskSource) and s.through_qemu]
            if not qemu_sources:
                return
            fleecing = QemuFleecing(ssh, guest.id, [s.key for s in qemu_sources], self.settings.fleecing_dir)
            self._fleecings[guest.id] = fleecing
            sizes = fleecing.setup()
            running = Guest(guest.id, guest.kind, guest.name, guest.host, "running")  # frozen by _frozen
            with self._frozen(running, config, ssh):
                fleecing.start()
            for src in qemu_sources:
                src.use_qemu(fleecing.socket, sizes[src.key])
            log.debug("%s: QEMU keeps the view of %s", guest.label, ", ".join(sizes))
        except BaseException:
            try:  # nothing left in QEMU, and the VM unlocked
                self._end_qemu_backup(guest)
            except Exception as exc:
                log.error("%s: %s", guest.label, exc)
            raise

    def _end_qemu_backup(self, guest: Guest) -> None:
        """QEMU cleaned up first, then the VM unlocked (kept locked while QEMU is not clean)."""
        fleecing = self._fleecings.get(guest.id)
        if fleecing is not None:
            try:
                fleecing.teardown()
            except Exception as exc:
                raise PlatformError(f"{guest.label}: QEMU cleanup failed, the VM stays locked: {exc}") from exc
            del self._fleecings[guest.id]
        if guest.id in self._locked:
            try:
                self.ssh_for_host(guest.host).run(f"qm unlock {shlex.quote(guest.id)}")
            except Exception as exc:
                raise PlatformError(f"{guest.label}: still locked, run on {guest.host}: qm unlock {guest.id}") from exc
            self._locked.discard(guest.id)

    def delete_snapshot(self, guest: Guest, snapshot: str) -> None:
        method = self._configured_method(guest)
        if method == "qemu":
            self._end_qemu_backup(guest)
            return
        if method == "storage":
            errors = []
            for src in self._plan(guest, snapshot)[1]:
                try:
                    src.drop_snapshot()
                except Exception as exc:  # try the other volumes anyway
                    errors.append(f"{src.volume}: {exc}")
            if errors:
                raise PlatformError("; ".join(errors))
            return
        try:
            self.api.delete_snapshot(guest, snapshot)
        except ProxmoxError as exc:
            if DELETE_LOCK not in str(exc):
                raise
            # A previous try failed half-way (snapshot still in use): Proxmox left the guest
            # locked, and every new try is refused. Only root can lift it (not through a token).
            tool = "qm" if guest.kind == "vm" else "pct"
            log.warning("%s: locked by the failed deletion of %s: %s unlock, then a new try", guest.label,
                        snapshot, tool)
            self.ssh_for_host(guest.host).run(f"{tool} unlock {shlex.quote(guest.id)}")
            self.api.delete_snapshot(guest, snapshot)

    def release_snapshot(self, guest: Guest, snapshot: str) -> bool:
        """Storage snapshots: those of zvols stay as the base of the next backup."""
        if self._configured_method(guest) != "storage":
            self.delete_snapshot(guest, snapshot)
            return False
        kept, errors = [], []
        for src in self._plan(guest, snapshot)[1]:
            if getattr(src, "keeps_base", False):
                kept.append(src.volume)
                continue
            try:
                src.drop_snapshot()
            except Exception as exc:  # try the other volumes anyway
                errors.append(f"{src.volume}: {exc}")
        if errors:
            raise PlatformError("; ".join(errors))
        if kept:
            log.info("%s: snapshot %s kept as the base of the next backup: %s", guest.label, snapshot, ", ".join(kept))
        return bool(kept)

    def drop_previous_bases(self, guest: Guest, snapshot: str) -> None:
        errors = []
        for src in self._plans.get(guest.id, ({}, []))[1]:
            if isinstance(src, ZfsZvolSource) and src.base is not None:
                try:
                    src.drop_base()
                except Exception as exc:
                    errors.append(f"{src.volume}@{src.base[0]}: {exc}")
        if errors:
            raise PlatformError("; ".join(errors))

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
            # "auto" uses Proxmox snapshots only where the token has VM.Snapshot
            privs = GUEST_PRIVS if self.settings.mode_for(g.id) == "proxmox" else STORAGE_MODE_GUEST_PRIVS
            need(f"/vms/{g.id}", privs, g.label)
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
        names = {s["name"] for s in self.api.list_snapshots(guest) if s.get("name", "").startswith(prefix)}
        sources = self._plans.get(guest.id, ({}, []))[1]
        if self._configured_method(guest) == "storage":
            # Invisible to Proxmox: looked for on each volume (once per name; none via QEMU)
            for src in sources:
                names.update(n for n in src.snapshots() if n.startswith(prefix))
        # The bases kept for incremental reads are expected
        names -= {src.base[0] for src in sources if isinstance(src, ZfsZvolSource) and src.base}
        return sorted(names)

    def restore_guest(self, manifest: dict, disks, request, progress, cancel) -> str:
        from .restore import ProxmoxRestore

        return ProxmoxRestore(self, manifest, disks, request, progress, cancel).run()

    def host_iowait(self, host: str) -> float:
        return self.api.node_iowait(host)


def freeze_enabled(config: dict) -> bool:
    """The qemu-guest-agent is enabled, and allowed to freeze (``freeze-fs-on-backup``)."""
    enabled, freeze = "0", "1"
    for part in str(config.get("agent", "0")).split(","):
        key, sep, value = part.partition("=")
        if not sep:
            enabled = key
        elif key == "enabled":
            enabled = value
        elif key == "freeze-fs-on-backup":
            freeze = value
    return enabled.strip() in ("1", "yes", "on", "true") and freeze.strip() not in ("0", "no", "off", "false")


def create(name: str, settings: ProxmoxSettings) -> ProxmoxPlatform:
    return ProxmoxPlatform(name, settings)
