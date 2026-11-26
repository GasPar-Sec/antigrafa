"""``snapshot_mode = "qemu"``: running VMs backed up by QEMU itself, as PBS does.

No storage snapshot: QEMU keeps a frozen view of each disk while the VM runs on
("image fleecing"), on any storage (iSCSI, LVM thick, raw or qcow2 files included):

1. for each disk, a temporary qcow2 image on the node (``fleecing_dir``, root only)
   whose backing is the disk itself;
2. filesystems frozen (guest agent), then one ``transaction`` of
   ``blockdev-backup sync=none``, one per disk, then thawed: from that instant, QEMU
   copies a block to the temporary image before the VM overwrites it;
3. the temporary images are exported over NBD (unix socket on the node): reading one
   gives the disk as it was at the transaction, blocks not yet overwritten coming
   from the disk itself;
4. after the read: exports, jobs and images removed.

The temporary image only grows with what the VM writes during the backup. If its
filesystem fills up, QEMU fails the VM's writes: put ``fleecing_dir`` where there is
room.

Both scripts below run on the node with its python3 (standard library only): one
drives QEMU through its QMP socket, the other reads an NBD export to its output.
"""

from __future__ import annotations

import json
import logging
import shlex

from ...ssh import SSHHost
from .capture import Disk, UnsupportedStorage, VolumeSource

log = logging.getLogger(__name__)

QMP_SOCKET = "/var/run/qemu-server/{vmid}.qmp"
NBD_SOCKET = "/run/antigrafa-{vmid}.nbd"

# argv: action (setup, start, teardown), JSON parameters. Prints a JSON answer.
QMP_SCRIPT = r'''
import glob, json, os, socket, stat, subprocess, sys, time

PREFIX = "antigrafa-"  # every node, job and export Antigrafa adds to QEMU
SLEEP = time.sleep
WAIT_TIMEOUT = 30.0  # seconds for QEMU to drop an export or end a job


class QMPError(Exception):
    pass


class QMP:
    """QMP over a binary stream (readline, write, flush)."""

    def __init__(self, stream):
        self.stream = stream
        self._reply()  # greeting
        self.cmd("qmp_capabilities")

    def _reply(self):
        while True:
            line = self.stream.readline()
            if not line:
                raise QMPError("QMP connection closed")
            msg = json.loads(line.decode())
            if "event" not in msg:
                return msg

    def cmd(self, name, args=None):
        msg = {"execute": name}
        if args:
            msg["arguments"] = args
        self.stream.write(json.dumps(msg).encode() + b"\n")
        self.stream.flush()
        reply = self._reply()
        if "error" in reply:
            raise QMPError("%s: %s" % (name, reply["error"].get("desc", reply["error"])))
        return reply.get("return")


def names(key):
    """Temporary node, job and export of a disk (QEMU names: 31 characters at most)."""
    return PREFIX + key, PREFIX + key + "-job", PREFIX + key + "-exp"


def image_path(p, key):
    return "%s/antigrafa-%s-%s.qcow2" % (p["dir"].rstrip("/"), p["vmid"], key)


def find_drives(qmp, keys):
    """key -> (node of the disk, size). Proxmox names drives drive-<key>."""
    found = {}
    for block in qmp.cmd("query-block"):
        inserted = block.get("inserted")
        if not inserted:
            continue
        qdev = block.get("qdev", "")
        for key in keys:
            ids = ("/machine/peripheral/" + key, "/machine/peripheral/%s/virtio-backend" % key, key)
            if block.get("device") == "drive-" + key or qdev in ids:
                found[key] = (inserted["node-name"], inserted["image"]["virtual-size"])
    missing = [k for k in keys if k not in found]
    if missing:
        raise QMPError("not attached to the running VM: %s" % ", ".join(missing))
    return found


def check_dir(path):
    """Images are created as root under predictable names: nobody else may write there."""
    os.makedirs(path, 0o700, exist_ok=True)
    st = os.lstat(path)
    if os.name == "posix" and (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022):
        raise QMPError("fleecing_dir %s must be a directory owned by root, writable by root only" % path)


def setup(qmp, p, run=subprocess.check_call):
    check_dir(p["dir"])
    removed = teardown(qmp, p)
    sizes = {}
    for key, (src, size) in sorted(find_drives(qmp, p["keys"]).items()):
        node = names(key)[0]
        path = image_path(p, key)
        run(["qemu-img", "create", "-q", "-f", "qcow2", path, str(size)])
        image = {"driver": "qcow2", "node-name": node, "backing": src,
                 "file": {"driver": "file", "filename": path}}
        try:
            # O_DIRECT: the old data copied there must not fill the node's page cache
            qmp.cmd("blockdev-add", dict(image, cache={"direct": True}))
        except QMPError:
            qmp.cmd("blockdev-add", image)  # filesystem without O_DIRECT (tmpfs...)
        sizes[key] = size
    qmp.cmd("nbd-server-start", {"addr": {"type": "unix", "data": {"path": p["socket"]}}})
    return {"sizes": sizes, "removed": removed}


def start(qmp, p):
    """Every disk at the same instant (the caller freezes the filesystems around it)."""
    drives = find_drives(qmp, p["keys"])
    actions = []
    for key in sorted(drives):
        node, job, _ = names(key)
        actions.append({"type": "blockdev-backup",
                        "data": {"device": drives[key][0], "target": node, "sync": "none", "job-id": job}})
    qmp.cmd("transaction", {"actions": actions})
    for key in sorted(drives):
        node, _, export = names(key)
        qmp.cmd("block-export-add", {"type": "nbd", "id": export, "node-name": node, "name": key,
                                     "writable": False})
    return {}


def _ours(items, field):
    return [i[field] for i in items or [] if str(i.get(field, "")).startswith(PREFIX)]


def _wait_gone(query, field, errors, what):
    deadline = time.time() + WAIT_TIMEOUT
    while True:
        left = _ours(query(), field)
        if not left:
            return
        if time.time() > deadline:
            errors.append("%s still there: %s" % (what, ", ".join(left)))
            return
        SLEEP(0.2)


def teardown(qmp, p):
    """Remove everything Antigrafa added to this VM, also after an interrupted run."""
    removed, errors = [], []

    def attempt(name, args):
        try:
            qmp.cmd(name, args)
        except QMPError as exc:
            errors.append(str(exc))

    if qmp is not None:
        exports = _ours(qmp.cmd("query-block-exports"), "id")
        for export in exports:
            attempt("block-export-del", {"id": export, "mode": "hard"})
        _wait_gone(lambda: qmp.cmd("query-block-exports"), "id", errors, "exports")
        jobs = _ours(qmp.cmd("query-block-jobs"), "device")
        for job in jobs:
            attempt("block-job-cancel", {"device": job, "force": True})
        _wait_gone(lambda: qmp.cmd("query-block-jobs"), "device", errors, "jobs")
        nodes = _ours(qmp.cmd("query-named-block-nodes", {"flat": True}), "node-name")
        for node in nodes:
            attempt("blockdev-del", {"node-name": node})
        if exports or jobs or nodes or os.path.exists(p["socket"]):
            try:
                qmp.cmd("nbd-server-stop")
            except QMPError as exc:
                if "not running" not in str(exc):
                    errors.append(str(exc))
        removed += exports + jobs + nodes
    for path in glob.glob(image_path(p, "*")) + [p["socket"]]:
        if os.path.exists(path):
            try:
                os.remove(path)
                removed.append(path)
            except OSError as exc:
                errors.append("%s: %s" % (path, exc))
    if errors:
        raise QMPError("; ".join(errors))
    return removed


ACTIONS = {"setup": setup, "start": start, "teardown": lambda qmp, p: {"removed": teardown(qmp, p)}}


def connect(path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(60)
    sock.connect(path)
    return sock, QMP(sock.makefile("rwb"))


if __name__ == "__main__":
    action, params = sys.argv[1], json.loads(sys.argv[2])
    sock = qmp = None
    try:
        try:
            sock, qmp = connect(params["qmp"])
        except OSError as exc:
            if action != "teardown":
                raise QMPError("QMP socket %s: %s (is the VM running?)" % (params["qmp"], exc))
            # VM stopped meanwhile: QEMU is gone with what was added to it, files remain
        result = ACTIONS[action](qmp, params)
    except QMPError as exc:
        sys.stderr.write("%s\n" % exc)
        sys.exit(1)
    finally:
        if sock is not None:
            sock.close()
    sys.stdout.write(json.dumps(result))
'''

# argv: unix socket, export name. Writes the whole export to its standard output.
NBD_SCRIPT = r'''
import socket, struct, sys

NBD_MAGIC = b"NBDMAGIC"
IHAVEOPT = 0x49484156454F5054
REQUEST_MAGIC = 0x25609513
REPLY_MAGIC = 0x67446698
OPT_EXPORT_NAME, CMD_READ, CMD_DISC = 1, 0, 2
FIXED_NEWSTYLE, NO_ZEROES = 1, 2
REQUEST = struct.Struct(">IHHQQI")


class NBDError(Exception):
    pass


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(min(n - len(buf), 1 << 20))
        if not part:
            raise NBDError("NBD connection closed")
        buf += part
    return bytes(buf)


def handshake(sock, name):
    """Fixed newstyle negotiation; returns the size of the export."""
    if recv_exact(sock, 8) != NBD_MAGIC:
        raise NBDError("not an NBD server")
    magic, flags = struct.unpack(">QH", recv_exact(sock, 10))
    if magic != IHAVEOPT:
        raise NBDError("old style NBD server")
    sock.sendall(struct.pack(">I", FIXED_NEWSTYLE | (flags & NO_ZEROES)))
    data = name.encode()
    sock.sendall(struct.pack(">QII", IHAVEOPT, OPT_EXPORT_NAME, len(data)) + data)
    size, _ = struct.unpack(">QH", recv_exact(sock, 10))
    if not flags & NO_ZEROES:
        recv_exact(sock, 124)
    return size


def copy(sock, size, out, block=4 << 20, depth=4):
    """Read the export in order, ``depth`` requests in flight (replies may come out of order)."""
    sent = written = 0
    handle = 0
    inflight, done = {}, {}
    while written < size:
        while len(inflight) < depth and sent < size:
            length = min(block, size - sent)
            sock.sendall(REQUEST.pack(REQUEST_MAGIC, 0, CMD_READ, handle, sent, length))
            inflight[handle] = (sent, length)
            sent += length
            handle += 1
        magic, error, h = struct.unpack(">IIQ", recv_exact(sock, 16))
        if magic != REPLY_MAGIC or h not in inflight:
            raise NBDError("unexpected NBD reply")
        offset, length = inflight.pop(h)
        if error:
            raise NBDError("read of %d bytes at %d failed: error %d" % (length, offset, error))
        done[offset] = recv_exact(sock, length)
        while written in done:
            data = done.pop(written)
            out.write(data)
            written += len(data)
    sock.sendall(REQUEST.pack(REQUEST_MAGIC, 0, CMD_DISC, handle, 0, 0))


if __name__ == "__main__":
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.connect(sys.argv[1])
    try:
        copy(s, handshake(s, sys.argv[2]), sys.stdout.buffer)
    except NBDError as exc:
        sys.stderr.write("%s\n" % exc)
        sys.exit(1)
    sys.stdout.buffer.flush()
'''


class QemuFleecing:
    """The QEMU side of the backup of one running VM, driven over SSH."""

    def __init__(self, ssh: SSHHost, vmid: str, keys: list[str], fleecing_dir: str) -> None:
        self.ssh = ssh
        self.params = {
            "qmp": QMP_SOCKET.format(vmid=vmid),
            "socket": NBD_SOCKET.format(vmid=vmid),
            "dir": fleecing_dir,
            "vmid": vmid,
            "keys": keys,
        }

    @property
    def socket(self) -> str:
        return self.params["socket"]

    def _call(self, action: str) -> dict:
        args = shlex.quote(json.dumps(self.params, separators=(",", ":")))
        out = self.ssh.run(f"python3 -c {shlex.quote(QMP_SCRIPT)} {action} {args}", timeout=300)
        return json.loads(out) if out.strip() else {}

    def setup(self) -> dict[str, int]:
        """Temporary images and NBD server; returns the size of each disk."""
        answer = self._call("setup")
        if answer.get("removed"):
            log.warning("VM %s: removed what an interrupted backup left in QEMU: %s",
                        self.params["vmid"], ", ".join(answer["removed"]))
        return {key: int(size) for key, size in answer["sizes"].items()}

    def start(self) -> None:
        self._call("start")

    def teardown(self) -> None:
        self._call("teardown")


class QemuDiskSource(VolumeSource):
    """A VM disk read through QEMU (running VM), or from its volume (stopped VM)."""

    def __init__(
        self,
        ssh: SSHHost,
        disk: Disk,
        snapname: str,
        volume_source: VolumeSource | None,
        why_not_direct: str = "",
        low_priority: bool = True,
        remote_hash: bool = False,
        through_qemu: bool = True,  # False: never a QEMU drive (tpmstate)
    ) -> None:
        super().__init__(ssh, {}, disk, snapname, low_priority, remote_hash)
        self.volume_source = volume_source
        self.why_not_direct = why_not_direct
        self.through_qemu = through_qemu
        self._socket: str | None = None
        self._size = 0

    def use_qemu(self, socket: str, size: int) -> None:
        self._socket, self._size = socket, size

    def _direct(self) -> VolumeSource:
        if self.volume_source is None:
            raise UnsupportedStorage(f"{self.key} ({self.volume}): {self.why_not_direct}")
        return self.volume_source

    def prepare(self) -> None:
        if self._socket is None:
            self._direct().prepare()

    def size(self) -> int | None:
        return self._size if self._socket else self._direct().size()

    def stream_command(self) -> str:
        if self._socket is None:
            return self._direct().stream_command()
        return f"python3 -c {shlex.quote(NBD_SCRIPT)} {shlex.quote(self._socket)} {shlex.quote(self.key)}"

    def stream_command_from(self, offset: int) -> str | None:
        if self._socket is None:
            return self._direct().stream_command_from(offset)
        return None  # through QEMU: the NBD reader always starts at 0 (for now)

    def reader_marker(self) -> str:
        if self._socket is None:
            return self._direct().reader_marker()
        return f"{self._socket} {self.key}"

    def cleanup(self) -> None:
        super().cleanup()
        if self._socket is None and self.volume_source is not None:
            self.volume_source.cleanup()

    # Not used: QEMU keeps the frozen view, there is no storage snapshot

    def take_snapshot(self) -> None:
        pass

    def drop_snapshot(self) -> None:
        pass

    def snapshots(self) -> list[str]:
        return []
