"""Minimal Proxmox VE API client (API token authentication)."""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass


class ProxmoxError(Exception):
    pass


@dataclass(frozen=True)
class Guest:
    vmid: int
    node: str
    type: str  # "qemu" or "lxc"
    name: str
    status: str

    @property
    def label(self) -> str:
        kind = "VM" if self.type == "qemu" else "CT"
        return f"{kind} {self.vmid} ({self.name})"


class ProxmoxAPI:
    def __init__(
        self,
        url: str,
        token_id: str,
        token_secret: str,
        verify_tls: bool = True,
        ca_file: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.base = url.rstrip("/") + "/api2/json"
        self._auth = f"PVEAPIToken={token_id}={token_secret}"
        self.timeout = timeout
        if not verify_tls:
            self._ctx = ssl._create_unverified_context()
        else:
            self._ctx = ssl.create_default_context(cafile=ca_file)

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
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx) as resp:
                return json.load(resp).get("data")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace").strip()
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
            if r.get("template"):
                continue
            out.append(
                Guest(
                    vmid=int(r["vmid"]),
                    node=r["node"],
                    type=r["type"],
                    name=r.get("name", ""),
                    status=r.get("status", ""),
                )
            )
        return sorted(out, key=lambda g: g.vmid)

    def _guest_path(self, g: Guest) -> str:
        return f"/nodes/{urllib.parse.quote(g.node)}/{g.type}/{g.vmid}"

    def guest_config(self, g: Guest) -> dict:
        return self.get(self._guest_path(g) + "/config")

    def list_snapshots(self, g: Guest) -> list[dict]:
        return self.get(self._guest_path(g) + "/snapshot") or []

    def create_snapshot(self, g: Guest, name: str, description: str = "") -> None:
        """Consistent snapshot of every disk. Proxmox freezes the filesystems through
        the qemu-guest-agent when it is enabled; RAM is not saved."""
        params = {"snapname": name, "description": description}
        if g.type == "qemu":
            params["vmstate"] = 0
        self.wait_task(g.node, self.post(self._guest_path(g) + "/snapshot", params))

    def delete_snapshot(self, g: Guest, name: str) -> None:
        upid = self.delete(self._guest_path(g) + f"/snapshot/{urllib.parse.quote(name)}")
        self.wait_task(g.node, upid)

    # --- Storage and nodes -------------------------------------------------

    def storage(self, storage_id: str) -> dict:
        return self.get(f"/storage/{urllib.parse.quote(storage_id)}")

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
