"""snapshot_mode = "qemu": the scripts run on the node, with fakes of QEMU (QMP, NBD)."""

import io
import json
import os
import shlex
import socket
import struct
import threading

import pytest

from antigrafa.platforms.proxmox import qemu
from antigrafa.platforms.proxmox.capture import Disk, UnsupportedStorage


def _script(source):
    namespace = {"__name__": "script"}
    exec(compile(source, "remote", "exec"), namespace)  # noqa: S102 - our own script
    return namespace


QMP_NS = _script(qemu.QMP_SCRIPT)
NBD_NS = _script(qemu.NBD_SCRIPT)
QMPError = QMP_NS["QMPError"]


# --------------------------------------------------------------------------- QMP


class FakeQMP:
    """QEMU as seen through QMP: block devices, nodes, jobs, exports, NBD server."""

    def __init__(self, blocks=None, nodes=(), jobs=(), exports=(), server=False, fail=None):
        self.blocks = blocks if blocks is not None else [
            {"device": "drive-scsi0", "qdev": "scsi0", "inserted": {"node-name": "#block123", "image": {"virtual-size": 1024}}},
            {"device": "drive-ide2", "qdev": "ide2"},  # empty CD-ROM drive
            {"device": "", "qdev": "/machine/peripheral/virtio2/virtio-backend",
             "inserted": {"node-name": "drive-virtio2", "image": {"virtual-size": 4096}}},
        ]
        self.nodes = ["#block123", "drive-virtio2", *nodes]
        self.jobs, self.exports, self.server = list(jobs), list(exports), server
        self.fail = fail or {}
        self.calls = []

    def cmd(self, name, args=None):
        self.calls.append((name, args))
        if name in self.fail:
            raise QMPError(f"{name}: {self.fail[name]}")
        a = args or {}
        if name == "query-block":
            return self.blocks
        if name == "query-named-block-nodes":
            return [{"node-name": n} for n in self.nodes]
        if name == "query-block-jobs":
            return [{"device": j} for j in self.jobs]
        if name == "query-block-exports":
            return [{"id": e} for e in self.exports]
        if name == "blockdev-add":
            self.nodes.append(a["node-name"])
        elif name == "blockdev-del":
            self.nodes.remove(a["node-name"])
        elif name == "transaction":
            self.jobs += [action["data"]["job-id"] for action in a["actions"]]
        elif name == "block-job-cancel":
            self.jobs.remove(a["device"])
        elif name == "block-export-add":
            self.exports.append(a["id"])
        elif name == "block-export-del":
            self.exports.remove(a["id"])
        elif name == "nbd-server-start":
            self.server = True
        elif name == "nbd-server-stop":
            if not self.server:
                raise QMPError("nbd-server-stop: NBD server not running")
            self.server = False
        return {}

    def names(self):
        return [c[0] for c in self.calls if not c[0].startswith("query-")]


def _params(tmp_path, keys=("scsi0", "virtio2")):
    return {"qmp": "/q", "socket": str(tmp_path / "nbd.sock"), "dir": str(tmp_path), "vmid": "650", "keys": list(keys)}


def _qemu_img(created):
    def run(argv):
        created.append(argv)
        open(argv[-2], "wb").close()

    return run


def test_setup_start_and_teardown_leave_qemu_as_found(tmp_path):
    fake, created = FakeQMP(), []
    p = _params(tmp_path)
    answer = QMP_NS["setup"](fake, p, run=_qemu_img(created))
    assert answer == {"sizes": {"scsi0": 1024, "virtio2": 4096}, "removed": []}
    image = str(tmp_path) + "/antigrafa-650-scsi0.qcow2"
    assert created[0] == ["qemu-img", "create", "-q", "-f", "qcow2", image, "1024"]
    add = [a for n, a in fake.calls if n == "blockdev-add"]
    # The temporary image reads what was not copied yet from the disk itself
    assert add[0] == {"driver": "qcow2", "node-name": "antigrafa-scsi0", "backing": "#block123",
                      "file": {"driver": "file", "filename": image}, "cache": {"direct": True}}
    assert add[1]["backing"] == "drive-virtio2"  # found by its device (Proxmox with -blockdev)
    assert fake.server

    QMP_NS["start"](fake, p)
    transaction = next(a for n, a in fake.calls if n == "transaction")
    # Every disk in one transaction: the same instant for all of them
    assert transaction["actions"] == [
        {"type": "blockdev-backup",
         "data": {"device": "#block123", "target": "antigrafa-scsi0", "sync": "none", "job-id": "antigrafa-scsi0-job"}},
        {"type": "blockdev-backup",
         "data": {"device": "drive-virtio2", "target": "antigrafa-virtio2", "sync": "none",
                  "job-id": "antigrafa-virtio2-job"}},
    ]
    exports = [a for n, a in fake.calls if n == "block-export-add"]
    assert exports[0] == {"type": "nbd", "id": "antigrafa-scsi0-exp", "node-name": "antigrafa-scsi0", "name": "scsi0",
                          "writable": False}

    fake.calls.clear()
    removed = QMP_NS["teardown"](fake, p)
    assert fake.names() == [
        "block-export-del", "block-export-del", "block-job-cancel", "block-job-cancel",
        "blockdev-del", "blockdev-del", "nbd-server-stop",
    ]
    assert ("block-export-del", {"id": "antigrafa-scsi0-exp", "mode": "hard"}) in fake.calls
    assert ("block-job-cancel", {"device": "antigrafa-scsi0-job", "force": True}) in fake.calls
    assert fake.nodes == ["#block123", "drive-virtio2"] and not fake.jobs and not fake.exports and not fake.server
    assert "antigrafa-650-scsi0.qcow2" in {os.path.basename(r) for r in removed}
    assert not list(tmp_path.glob("*.qcow2"))


def test_setup_first_removes_what_an_interrupted_run_left(tmp_path):
    (tmp_path / "antigrafa-650-scsi0.qcow2").write_bytes(b"old")
    (tmp_path / "antigrafa-651-scsi0.qcow2").write_bytes(b"another VM")
    fake = FakeQMP(nodes=["antigrafa-scsi0"], jobs=["antigrafa-scsi0-job"], exports=["antigrafa-scsi0-exp"],
                   server=True)
    answer = QMP_NS["setup"](fake, _params(tmp_path, ["scsi0"]), run=_qemu_img([]))
    assert {"antigrafa-scsi0-exp", "antigrafa-scsi0-job", "antigrafa-scsi0"} <= set(answer["removed"])
    assert (tmp_path / "antigrafa-651-scsi0.qcow2").exists()  # not this VM's


def test_teardown_with_nothing_to_remove_touches_nothing(tmp_path):
    fake = FakeQMP()
    assert QMP_NS["teardown"](fake, _params(tmp_path)) == []
    assert fake.names() == []  # not even nbd-server-stop: it may not be ours


def test_teardown_of_a_vm_stopped_meanwhile_only_removes_files(tmp_path):
    (tmp_path / "antigrafa-650-scsi0.qcow2").write_bytes(b"x")
    removed = QMP_NS["teardown"](None, _params(tmp_path))
    assert [os.path.basename(r) for r in removed] == ["antigrafa-650-scsi0.qcow2"]
    assert not (tmp_path / "antigrafa-650-scsi0.qcow2").exists()


def test_teardown_tries_everything_and_reports_every_error(tmp_path, monkeypatch):
    monkeypatch.setitem(QMP_NS, "SLEEP", lambda s: None)
    monkeypatch.setitem(QMP_NS, "WAIT_TIMEOUT", -1.0)  # the job never goes: give up at once
    fake = FakeQMP(nodes=["antigrafa-scsi0"], jobs=["antigrafa-scsi0-job"], server=True,
                   fail={"block-job-cancel": "refused"})
    with pytest.raises(QMPError) as exc:
        QMP_NS["teardown"](fake, _params(tmp_path))
    assert "block-job-cancel: refused" in str(exc.value) and "jobs still there" in str(exc.value)
    assert "blockdev-del" in fake.names() and "nbd-server-stop" in fake.names()  # went on anyway


@pytest.mark.parametrize(
    ("mode", "uid", "accepted"),
    [(0o40700, 0, True), (0o40755, 0, True), (0o41777, 0, False), (0o40770, 0, False), (0o40700, 1000, False),
     (0o120777, 0, False)],  # world-writable (/var/tmp), group-writable, not root's, a symbolic link
)
def test_fleecing_dir_must_be_writable_by_root_only(monkeypatch, mode, uid, accepted):
    from types import SimpleNamespace

    made = []
    fake_os = SimpleNamespace(
        name="posix", getuid=lambda: 0, makedirs=lambda path, m, exist_ok: made.append((path, oct(m), exist_ok)),
        lstat=lambda path: SimpleNamespace(st_mode=mode, st_uid=uid),
    )
    monkeypatch.setitem(QMP_NS, "os", fake_os)
    if accepted:
        QMP_NS["check_dir"]("/var/lib/antigrafa/fleecing")
    else:
        with pytest.raises(QMPError, match="must be a directory owned by root, writable by root only"):
            QMP_NS["check_dir"]("/var/lib/antigrafa/fleecing")
    assert made == [("/var/lib/antigrafa/fleecing", "0o700", True)]


def test_a_disk_not_attached_to_qemu_is_named(tmp_path):
    with pytest.raises(QMPError, match="not attached to the running VM: sata1"):
        QMP_NS["setup"](FakeQMP(), _params(tmp_path, ["scsi0", "sata1"]), run=_qemu_img([]))


class Lines(io.BytesIO):
    """Answers of QEMU, one JSON per line; what is written is kept."""

    def __init__(self, answers):
        super().__init__(b"".join(json.dumps(a).encode() + b"\n" for a in answers))
        self.sent = []

    def write(self, data):
        self.sent.append(json.loads(data))
        return len(data)


def test_qmp_protocol_skips_events_and_raises_errors():
    stream = Lines([
        {"QMP": {"version": {}}}, {"return": {}},  # greeting, capabilities
        {"event": "JOB_STATUS_CHANGE"}, {"return": [1]},
        {"error": {"class": "GenericError", "desc": "Node 'x' not found"}},
    ])
    qmp = QMP_NS["QMP"](stream)
    assert qmp.cmd("query-block") == [1]
    with pytest.raises(QMPError, match="blockdev-del: Node 'x' not found"):
        qmp.cmd("blockdev-del", {"node-name": "x"})
    assert stream.sent == [{"execute": "qmp_capabilities"}, {"execute": "query-block"},
                           {"execute": "blockdev-del", "arguments": {"node-name": "x"}}]
    with pytest.raises(QMPError, match="closed"):
        qmp.cmd("query-block")


def test_qemu_names_fit_in_31_characters():
    assert all(len(n) <= 31 for n in QMP_NS["names"]("tpmstate0"))


# --------------------------------------------------------------------------- NBD


def _nbd_server(sock, data, **options):
    try:
        _serve_nbd(sock, data, **options)
    except OSError:
        pass  # the client gave up (read error): nothing more to answer


def _serve_nbd(sock, data, name=b"scsi0", no_zeroes=True, reorder=True, error_at=None):
    """A newstyle NBD server for one export; answers pairs of requests in reverse order."""

    def recv(n):
        buf = b""
        while len(buf) < n:
            buf += sock.recv(n - len(buf))
        return buf

    sock.sendall(b"NBDMAGIC" + struct.pack(">QH", 0x49484156454F5054, 1 | (2 if no_zeroes else 0)))
    recv(4)
    _, opt, length = struct.unpack(">QII", recv(16))
    assert opt == 1 and recv(length) == name
    sock.sendall(struct.pack(">QH", len(data), 0) + (b"" if no_zeroes else b"\0" * 124))
    pending = []
    while True:
        magic, _, kind, handle, offset, length = struct.unpack(">IHHQQI", recv(28))
        assert magic == 0x25609513
        if kind == 2:
            sock.close()
            return
        pending.append((handle, offset, length))
        if len(pending) == 2 or offset + length == len(data) or not reorder:
            for h, off, ln in reversed(pending):
                if error_at is not None and off == error_at:
                    sock.sendall(struct.pack(">IIQ", 0x67446698, 5, h))
                else:
                    sock.sendall(struct.pack(">IIQ", 0x67446698, 0, h) + data[off:off + ln])
            pending.clear()


def _read_export(data, **server):
    client, server_sock = socket.socketpair()
    thread = threading.Thread(target=_nbd_server, args=(server_sock, data), kwargs=server, daemon=True)
    thread.start()
    out = io.BytesIO()
    try:
        size = NBD_NS["handshake"](client, "scsi0")
        NBD_NS["copy"](client, size, out, block=1000, depth=3)
    finally:
        client.close()
        thread.join(5)
    return out.getvalue()


def test_nbd_reads_the_export_in_order_whatever_the_reply_order():
    data = bytes(range(256)) * 41  # 10496 bytes: 11 requests, the last one short
    assert _read_export(data) == data
    assert _read_export(data, no_zeroes=False, reorder=False) == data
    assert _read_export(b"") == b""


def test_nbd_read_error_is_reported():
    with pytest.raises(NBD_NS["NBDError"], match="read of 1000 bytes at 2000 failed: error 5"):
        _read_export(b"x" * 5000, error_at=2000)


# --------------------------------------------------------------------------- over SSH


class SSH:
    def __init__(self, reply=""):
        self.reply, self.commands = reply, []

    def run(self, cmd, timeout=120):
        self.commands.append(cmd)
        return self.reply


def test_fleecing_runs_the_script_with_its_parameters(caplog):
    ssh = SSH(json.dumps({"sizes": {"scsi0": "1024"}, "removed": ["antigrafa-scsi0"]}))
    fleecing = qemu.QemuFleecing(ssh, "650", ["scsi0"], "/var/tmp")
    assert fleecing.setup() == {"scsi0": 1024}
    argv = shlex.split(ssh.commands[0])
    assert argv[:3] == ["python3", "-c", qemu.QMP_SCRIPT] and argv[3] == "setup"
    assert json.loads(argv[4]) == {"qmp": "/var/run/qemu-server/650.qmp", "socket": "/run/antigrafa-650.nbd",
                                   "dir": "/var/tmp", "vmid": "650", "keys": ["scsi0"]}
    assert "interrupted backup left in QEMU: antigrafa-scsi0" in caplog.text
    ssh.reply = ""
    fleecing.start()
    fleecing.teardown()
    assert [shlex.split(c)[3] for c in ssh.commands] == ["setup", "start", "teardown"]


class Direct:
    """The volume reader used when the VM is stopped."""

    def __init__(self):
        self.calls = []

    def prepare(self):
        self.calls.append("prepare")

    def size(self):
        return 7

    def stream_command(self):
        return "dd if=/dev/x"

    def reader_marker(self):
        return "/dev/x"

    def cleanup(self):
        self.calls.append("cleanup")


def test_disk_source_reads_through_qemu_or_from_the_volume():
    ssh = SSH()
    disk = Disk("scsi0", "san:0.0.8.scsi-36e8")
    src = qemu.QemuDiskSource(ssh, disk, "snap", Direct(), low_priority=False)
    src.use_qemu("/run/antigrafa-650.nbd", 1024)
    src.prepare()
    assert src.size() == 1024
    assert shlex.split(src.stream_command()) == ["python3", "-c", qemu.NBD_SCRIPT, "/run/antigrafa-650.nbd", "scsi0"]
    assert src.reader_marker() == "/run/antigrafa-650.nbd scsi0"
    src.cleanup()
    assert src.volume_source.calls == []  # never touched

    stopped = qemu.QemuDiskSource(ssh, disk, "snap", Direct())
    stopped.prepare()
    assert stopped.size() == 7 and stopped.stream_command() == "dd if=/dev/x"
    stopped.cleanup()
    assert stopped.volume_source.calls == ["prepare", "cleanup"]
    assert stopped.snapshots() == []


def test_stopped_vm_disk_without_a_direct_reader_says_why():
    src = qemu.QemuDiskSource(SSH(), Disk("scsi0", "nfs:1/vm-1-disk-0.qcow2"), "snap", None, "qcow2 is not supported")
    with pytest.raises(UnsupportedStorage, match="qcow2 is not supported"):
        src.prepare()


def test_fleecing_image_without_direct_io_when_the_filesystem_refuses_it(tmp_path):
    """O_DIRECT keeps the copied data out of the page cache; tmpfs and a few others refuse it."""

    class NoDirectIO(FakeQMP):
        def cmd(self, name, args=None):
            if name == "blockdev-add" and "cache" in args:
                self.calls.append((name, args))
                raise QMPError("blockdev-add: Could not open: Invalid argument")
            return super().cmd(name, args)

    fake = NoDirectIO()
    QMP_NS["setup"](fake, _params(tmp_path, keys=("scsi0",)), run=_qemu_img([]))
    add = [a for n, a in fake.calls if n == "blockdev-add"]
    assert [a.get("cache") for a in add] == [{"direct": True}, None]
    assert "antigrafa-scsi0" in fake.nodes


def test_only_a_disk_read_from_its_volume_restarts_mid_way():
    class Volume(Direct):
        def stream_command_from(self, offset):
            return f"dd if=/dev/x skip={offset}"

    class Streams(SSH):
        def stream(self, cmd, input=None):
            self.commands.append(cmd)
            return "stream"

    ssh = Streams()
    disk = Disk("scsi0", "san:0.0.8.scsi-36e8")
    through_qemu = qemu.QemuDiskSource(ssh, disk, "snap", Volume(), low_priority=False)
    through_qemu.use_qemu("/run/antigrafa-650.nbd", 1024)
    through_qemu.open_stream()
    assert through_qemu.reopener(4096) is None  # the NBD reader always starts at 0

    stopped = qemu.QemuDiskSource(ssh, disk, "snap", Volume(), low_priority=False)
    stopped.open_stream()
    stopped.reopener(4096)(2)
    assert ssh.commands[-1] == "dd if=/dev/x skip=8192"
