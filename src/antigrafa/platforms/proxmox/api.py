"""Minimal Proxmox VE API client (API token authentication)."""

from __future__ import annotations

import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from ..base import Guest, PlatformError

log = logging.getLogger(__name__)

# Generic guest kind -> Proxmox guest type
PVE_TYPES = {"vm": "qemu", "ct": "lxc"}
_KINDS = {pve: kind for kind, pve in PVE_TYPES.items()}


class ProxmoxError(PlatformError):
    pass


class ProxmoxAPI:
    def __init__(
        self,
        url: str,
        token_id: str,
        token_secret: str,
        ca_file: str | None = None,
        timeout: float = 30.0,
        verify_tls: bool = True,
    ) -> None:
        # HTTPS only: the API token travels in every request. This also rules out
        # file:// and other schemes urllib would otherwise accept.
        if urllib.parse.urlsplit(url).scheme != "https":
            raise ProxmoxError(f"the Proxmox URL must start with https:// (got {url!r})")
        self.base = url.rstrip("/") + "/api2/json"
        self._auth = f"PVEAPIToken={token_id}={token_secret}"
        self.timeout = timeout
        # For the self-signed certificate of a Proxmox cluster, point ca_file at its
        # CA (/etc/pve/pve-root-ca.pem): the connection is then verified.
        self._ctx = ssl.create_default_context(cafile=ca_file)
        if not verify_tls:
            # Explicit opt-out (verify_tls = false, labs only): anyone on the network
            # path can impersonate the API and capture the token. Loudly reported.
            log.warning(
                "TLS certificate of %s is NOT verified (verify_tls = false): "
                "the API token can be intercepted, use ca_file instead",
                url,
            )
            self._ctx.check_hostname = False  # NOSONAR: opt-in, see verify_tls
            self._ctx.verify_mode = ssl.CERT_NONE  # NOSONAR: opt-in, see verify_tls

    def _request(self, method: str, path: str, params: dict | None = None):
        url = self.base + path
        body = None
        if params:
            encoded = urllib.parse.urlencode(params)
            if method in ("GET", "DELETE"):
                url += "?" + encoded
            else:
                body = encoded.encode()
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self._auth)
        if body is not None:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        t0 = time.monotonic()
        try:
            # Scheme checked in __init__: always https:// (see above)
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:  # nosec B310
                data = json.load(resp).get("data")
            log.debug("API %s %s: %.0f ms", method, path, (time.monotonic() - t0) * 1000)
            return data
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace").strip()
            if exc.code == 401:
                detail += " (API token rejected: check token_id and its secret)"
            raise ProxmoxError(f"{method} {path}: HTTP {exc.code} {exc.reason} {detail}") from None
        except urllib.error.URLError as exc:
            raise ProxmoxError(f"{method} {path}: {exc.reason}") from None

    def get(self, path: str, params: dict | None = None):
        return self._request("GET", path, params)

    def post(self, path: str, params: dict | None = None):
        return self._request("POST", path, params)

    def delete(self, path: str, params: dict | None = None):
        return self._request("DELETE", path, params)

    # --- Guests ------------------------------------------------------------

    def guests(self) -> list[Guest]:
        """Every VM and container in the cluster, except templates."""
        out = []
        for r in self.get("/cluster/resources", {"type": "vm"}) or []:
            if r.get("template") or r.get("type") not in _KINDS:
                continue
            out.append(
                Guest(
                    id=str(r["vmid"]),
                    kind=_KINDS[r["type"]],
                    name=r.get("name", ""),
                    host=r["node"],
                    status=r.get("status", ""),
                )
            )
        return sorted(out, key=lambda g: int(g.id))

    def _guest_path(self, g: Guest) -> str:
        return f"/nodes/{urllib.parse.quote(g.host)}/{PVE_TYPES[g.kind]}/{urllib.parse.quote(g.id)}"

    def guest_config(self, g: Guest) -> dict:
        return self.get(self._guest_path(g) + "/config")

    def list_snapshots(self, g: Guest) -> list[dict]:
        return self.get(self._guest_path(g) + "/snapshot") or []

    def create_snapshot(self, g: Guest, name: str, description: str = "") -> None:
        """Consistent snapshot of every disk. Proxmox freezes the filesystems through
        the qemu-guest-agent when it is enabled; RAM is not saved."""
        params = {"snapname": name, "description": description}
        if g.kind == "vm":
            params["vmstate"] = 0
        self.wait_task(g.host, self.post(self._guest_path(g) + "/snapshot", params))

    def delete_snapshot(self, g: Guest, name: str) -> None:
        upid = self.delete(self._guest_path(g) + f"/snapshot/{urllib.parse.quote(name)}")
        self.wait_task(g.host, upid)

    # --- Storage and nodes -------------------------------------------------

    def storage(self, storage_id: str) -> dict:
        # The list, not GET /storage/<id>: that one requires Datastore.Allocate (the
        # right to change storages), the list only Datastore.Audit (read-only).
        for entry in self.get("/storage") or []:
            if entry.get("storage") == storage_id:
                return entry
        raise ProxmoxError(f"storage {storage_id}: not found, or not visible to the token (Datastore.Audit)")

    # --- Permissions -------------------------------------------------------

    def permissions(self, path: str) -> set[str]:
        """Privileges the token has on ``path``, inheritance and privilege separation included."""
        data = self.get("/access/permissions", {"path": path}) or {}
        return set(data.get(path) or {})

    def node_iowait(self, node: str) -> float:
        """Fraction of CPU time spent waiting on disks (0.0 to 1.0)."""
        return float(self.get(f"/nodes/{urllib.parse.quote(node)}/status").get("wait", 0.0))

    def wait_task(self, node: str, upid: str | None, timeout: float = 900.0, poll: float = 1.0) -> None:
        if not upid:
            return
        path = f"/nodes/{urllib.parse.quote(node)}/tasks/{urllib.parse.quote(upid, safe='')}/status"
        deadline = time.monotonic() + timeout
        while True:
            status = self.get(path)
            if status.get("status") == "stopped":
                if status.get("exitstatus") != "OK":
                    raise ProxmoxError(f"task {upid}: {status.get('exitstatus')}")
                return
            if time.monotonic() > deadline:
                raise ProxmoxError(f"task {upid}: timed out")
            time.sleep(poll)
