import io
import json
import urllib.error
import urllib.parse

import pytest

from antigrafa import proxmox
from antigrafa.proxmox import Guest, ProxmoxAPI, ProxmoxError

URL = "https://pve1.example.lan:8006"


class FakeServer:
    """Stands in for urlopen: records requests, answers from a list of replies."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, req, timeout=None, context=None):
        self.requests.append(req)
        self.context = context
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return io.BytesIO(json.dumps({"data": reply}).encode())


@pytest.fixture
def server(monkeypatch):
    def install(*replies):
        fake = FakeServer(*replies)
        monkeypatch.setattr(proxmox.urllib.request, "urlopen", fake)
        return fake

    return install


@pytest.fixture
def api():
    return ProxmoxAPI(URL + "/", "antigrafa@pve!backup", "s3cret")


def _query(req):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)


@pytest.mark.parametrize("url", ["http://pve1:8006", "file:///etc/passwd", "pve1:8006"])
def test_only_https_urls_are_accepted(url):
    with pytest.raises(ProxmoxError, match="https://"):
        ProxmoxAPI(url, "id", "secret")


def test_tls_certificate_is_always_verified(api):
    import ssl

    assert api._ctx.verify_mode == ssl.CERT_REQUIRED
    assert api._ctx.check_hostname is True


def test_get_sends_token_and_query(server, api):
    fake = server({"wait": 0.25})
    assert api.node_iowait("pve 1") == 0.25
    [req] = fake.requests
    assert req.full_url == f"{URL}/api2/json/nodes/pve%201/status"
    assert req.get_header("Authorization") == "PVEAPIToken=antigrafa@pve!backup=s3cret"
    assert req.get_method() == "GET"
    assert fake.context is api._ctx


def test_guests_skips_templates_and_sorts(server, api):
    fake = server(
        [
            {"vmid": 200, "node": "pve2", "type": "lxc", "name": "dns", "status": "running"},
            {"vmid": 100, "node": "pve1", "type": "qemu", "name": "web", "status": "stopped"},
            {"vmid": 9000, "node": "pve1", "type": "qemu", "template": 1},
            {"vmid": "150", "node": "pve1", "type": "qemu"},
        ]
    )
    guests = api.guests()
    assert [g.vmid for g in guests] == [100, 150, 200]
    assert guests[1] == Guest(150, "pve1", "qemu", "", "")
    assert _query(fake.requests[0]) == {"type": ["vm"]}


def test_guests_empty_cluster(server, api):
    server(None)
    assert api.guests() == []


def test_guest_label():
    assert Guest(100, "pve1", "qemu", "web", "running").label == "VM 100 (web)"
    assert Guest(200, "pve1", "lxc", "dns", "running").label == "CT 200 (dns)"


def test_guest_config_and_snapshots(server, api):
    g = Guest(100, "pve1", "qemu", "web", "running")
    fake = server({"scsi0": "local-zfs:vm-100-disk-0"}, [{"name": "current"}], None)
    assert api.guest_config(g) == {"scsi0": "local-zfs:vm-100-disk-0"}
    assert api.list_snapshots(g) == [{"name": "current"}]
    assert api.list_snapshots(g) == []
    assert fake.requests[0].full_url.endswith("/nodes/pve1/qemu/100/config")
    assert fake.requests[1].full_url.endswith("/nodes/pve1/qemu/100/snapshot")


def test_create_snapshot_qemu_without_ram_and_waits(server, api, monkeypatch):
    monkeypatch.setattr(proxmox.time, "sleep", lambda s: None)
    g = Guest(100, "pve1", "qemu", "web", "running")
    upid = "UPID:pve1:0001:snap"
    fake = server(upid, {"status": "running"}, {"status": "stopped", "exitstatus": "OK"})
    api.create_snapshot(g, "antigrafa_1", "temporary")

    post = fake.requests[0]
    assert post.get_method() == "POST"
    assert post.get_header("Content-type") == "application/x-www-form-urlencoded"
    body = urllib.parse.parse_qs(post.data.decode())
    assert body == {"snapname": ["antigrafa_1"], "description": ["temporary"], "vmstate": ["0"]}
    assert urllib.parse.quote(upid, safe="") in fake.requests[1].full_url
    assert len(fake.requests) == 3


def test_create_snapshot_container_has_no_vmstate(server, api):
    g = Guest(200, "pve1", "lxc", "dns", "running")
    fake = server(None)  # no task id: nothing to wait for
    api.create_snapshot(g, "antigrafa_1")
    body = urllib.parse.parse_qs(fake.requests[0].data.decode())
    assert "vmstate" not in body
    assert fake.requests[0].full_url.endswith("/nodes/pve1/lxc/200/snapshot")


def test_delete_snapshot(server, api):
    g = Guest(100, "pve1", "qemu", "web", "running")
    fake = server("UPID:x", {"status": "stopped", "exitstatus": "OK"})
    api.delete_snapshot(g, "antigrafa 1")
    assert fake.requests[0].get_method() == "DELETE"
    assert fake.requests[0].full_url.endswith("/snapshot/antigrafa%201")


def test_delete_with_params_uses_query_string(server, api):
    fake = server(None)
    api.delete("/x", {"force": 1})
    assert _query(fake.requests[0]) == {"force": ["1"]}
    assert fake.requests[0].data is None


def test_storage(server, api):
    fake = server({"type": "zfspool", "pool": "rpool/data"})
    assert api.storage("local-zfs")["type"] == "zfspool"
    assert fake.requests[0].full_url.endswith("/storage/local-zfs")


def test_failed_task_raises(server, api):
    server({"status": "stopped", "exitstatus": "snapshot feature is not available"})
    with pytest.raises(ProxmoxError, match="not available"):
        api.wait_task("pve1", "UPID:x")


def test_task_timeout(server, api):
    server({"status": "running"})
    with pytest.raises(ProxmoxError, match="timed out"):
        api.wait_task("pve1", "UPID:x", timeout=-1)


def test_http_error_includes_detail(server, api):
    error = urllib.error.HTTPError(URL, 403, "Forbidden", {}, io.BytesIO(b"permission check failed"))
    server(error)
    with pytest.raises(ProxmoxError, match="HTTP 403 Forbidden permission check failed"):
        api.get("/cluster/resources")


def test_network_error(server, api):
    server(urllib.error.URLError("connection refused"))
    with pytest.raises(ProxmoxError, match="GET /version: connection refused"):
        api.get("/version")
