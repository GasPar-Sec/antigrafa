"""Incremental reads: only the chunks ZFS says changed since the previous backup are read.

Everything here runs on any OS, without ZFS: the node scripts are run on local files
and on recorded ``zstream dump`` output. ``test_zfs_live.py`` checks them against a
real ZFS, when one is configured.
"""

import hashlib
import io
import os
import subprocess
import sys

import pytest

from antigrafa import delta
from antigrafa.delta import NOT_READ, changed_command, encode_changed, read_records
from antigrafa.pipeline import backup_records
from antigrafa.platforms.proxmox import zfsdiff
from antigrafa.platforms.proxmox.capture import Disk, ZfsZvolSource
from antigrafa.ssh import RemoteCommandError

CHUNK = 1024
MiB = 1 << 20


def _sha(data):
    return hashlib.sha256(data).digest()


def _chunks(data, size=CHUNK):
    return [data[i : i + size] for i in range(0, len(data), size)]


def _namespace(script):
    namespace = {"__name__": "antigrafa_remote"}
    exec(compile(script, "remote", "exec"), namespace)  # noqa: S102 - our own script
    return namespace


# --------------------------------------------------------------------------- reading the changed chunks


def _versions():
    """The disk at the previous backup, and now: chunks 1 and 4 changed, 3 did not."""
    old = os.urandom(CHUNK * 5 + 100)  # a short last chunk
    new = bytearray(old)
    new[CHUNK + 7] ^= 0xFF
    new[4 * CHUNK] ^= 0xFF
    return old, bytes(new)


def test_only_the_changed_chunks_are_read(tmp_path):
    old, new = _versions()
    image = tmp_path / "disk.img"
    image.write_bytes(new)
    reference = [_sha(c) for c in _chunks(old)]
    ns = _namespace(delta.CHANGED_SCRIPT)
    out = io.BytesIO()
    # Chunk 3 listed although unchanged (ZFS also sends rewrites of the same data)
    ranges = [(1, 1), (3, 4)]
    ns["relay_changed"](ns["Device"](str(image), CHUNK), io.BytesIO(encode_changed(reference, ranges)), out, CHUNK,
                        len(new))
    out.seek(0)
    records = list(read_records(out, reference))

    expected = _chunks(new)
    assert [d for d, _, _ in records] == [_sha(c) for c in expected]
    assert [length for _, _, length in records] == [len(c) for c in expected]
    kinds = [NOT_READ if data is NOT_READ else None if data is None else "sent" for _, data, _ in records]
    assert kinds == [NOT_READ, "sent", NOT_READ, None, "sent", NOT_READ]
    assert records[1][1] == expected[1] and records[4][1] == expected[4]


def test_nothing_changed_reads_nothing(tmp_path):
    data = os.urandom(CHUNK * 3)
    image = tmp_path / "disk.img"
    image.write_bytes(b"")  # never read
    reference = [_sha(c) for c in _chunks(data)]
    ns = _namespace(delta.CHANGED_SCRIPT)
    out = io.BytesIO()
    ns["relay_changed"](ns["Device"](str(image), CHUNK), io.BytesIO(encode_changed(reference, [])), out, CHUNK,
                        len(data))
    out.seek(0)
    assert [r[1] for r in read_records(out, reference)] == [NOT_READ] * 3


def test_changed_script_runs_on_a_real_interpreter(tmp_path):
    old, new = _versions()
    image = tmp_path / "disk.img"
    image.write_bytes(new)
    reference = [_sha(c) for c in _chunks(old)]
    res = subprocess.run(
        [sys.executable, "-c", delta.CHANGED_SCRIPT, str(CHUNK), str(len(new)), str(image)],
        input=encode_changed(reference, [(0, 5)]),
        capture_output=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    records = list(read_records(io.BytesIO(res.stdout), reference))
    assert [d for d, _, _ in records] == [_sha(c) for c in _chunks(new)]
    assert [r[1] is None for r in records] == [True, False, True, True, False, True]


def test_changed_script_fails_on_a_short_device(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"x" * 10)
    res = subprocess.run(
        [sys.executable, "-c", delta.CHANGED_SCRIPT, str(CHUNK), str(CHUNK * 2), str(image)],
        input=encode_changed([bytes(32)] * 2, [(1, 1)]),
        capture_output=True,
        timeout=60,
    )
    assert res.returncode != 0 and b"instead of" in res.stderr


def test_device_falls_back_when_direct_io_is_refused(tmp_path):
    image = tmp_path / "disk.img"
    image.write_bytes(b"abcdefgh" * 256)
    ns = _namespace(delta.CHANGED_SCRIPT)
    device = ns["Device"](str(image), CHUNK)

    class Refusing:
        def seek(self, offset):
            pass

        def readinto(self, view):
            raise OSError(22, "Invalid argument")

        def close(self):
            pass

    device.file, device.direct = Refusing(), True
    assert bytes(device.read(8, 8)) == b"abcdefgh"
    assert device.direct is False  # the normal way from now on


def test_changed_command_and_input():
    cmd = changed_command("/dev/zvol/rpool/data/vm-1-disk-0@s", 4 * MiB, 32 * MiB, prefix="nice -n 10 ")
    assert cmd.startswith("nice -n 10 python3 -c ")
    assert cmd.endswith(f" {4 * MiB} {32 * MiB} /dev/zvol/rpool/data/vm-1-disk-0@s")
    raw = encode_changed([b"a" * 32], [(5, 6), (1, 2)])
    assert raw[8 + 32 :] == (2).to_bytes(8, "big") + b"".join(
        a.to_bytes(8, "big") + b.to_bytes(8, "big") for a, b in [(1, 2), (5, 6)]  # sorted
    )


def test_unread_chunks_do_not_count_for_the_read_throttle(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from antigrafa.adaptive import WriteController
    from antigrafa.chunkstore import ChunkStore

    class Throttle:
        consumed = 0

        def consume(self, n):
            self.consumed += n

    store = ChunkStore(tmp_path)
    store.init()
    stored = store.insert(b"x" * 10)
    throttle, progress = Throttle(), []
    records = [(stored, NOT_READ, 10), (stored, None, 10), (_sha(b"y" * 10), b"y" * 10, 10)]
    with ThreadPoolExecutor(2) as pool:
        digests, stats = backup_records(records, store, WriteController(), pool, throttle, progress.append)
    assert throttle.consumed == 20  # the chunk not read on the node does not slow it down
    assert progress == [10, 10, 10]  # every chunk counts for the progress
    assert (stats.skipped_chunks, stats.unchanged_chunks, stats.new_chunks) == (1, 2, 1)


# --------------------------------------------------------------------------- which chunks changed (zstream dump)

WRITE = (
    "WRITE object = {obj} type = 23 checksum type = 7 compression type = 0 flags = 0 offset = {off} "
    "logical_size = {size} compressed_size = 0 payload_size = {size} props = 8200000001 salt = 0000000000000000 "
    "iv = 000000000000000000000000 mac = 00000000000000000000000000000000"
)


def _dump(*records, totals=None, end=True):
    """``zstream dump -v`` output with these records, and its summary."""
    lines = ["BEGIN record", "\thdrtype = 1", "\tfeatures = 4", "\tmagic = 2f5bacbac", "\ttoname = lab/vol@b",
             "\tpayloadlen = 0",
             "OBJECT object = 1 type = 23 bonustype = 0 blksz = 16384 bonuslen = 0 dn_slots = 1 raw_bonuslen = 0 "
             "flags = 0 maxblkid = 4095 indblkshift = 17 nlevels = 2 nblkptr = 1"]
    lines += records
    if end:
        lines.append("END checksum = 1/2/3/4")
    counts = {"WRITE": 0, "FREE": 0, "FREEOBJECTS": 0, "SPILL": 0, "WRITE_EMBEDDED": 0, "WRITE_BYREF": 0}
    for r in records:
        word = r.split(" ", 1)[0]
        counts[word] = counts.get(word, 0) + 1
    counts.update(totals or {})
    lines += ["SUMMARY:"] + [f"\tTotal DRR_{k} records = {v} (0 bytes)" for k, v in counts.items()]
    lines += ["\tTotal records = 9", "\tTotal payload size = 0 (0x0)"]
    return [line + "\n" for line in lines]


def _changed(lines, chunk=4 * MiB, size=64 * MiB):
    return _namespace(zfsdiff.CHANGES_SCRIPT)["changed_chunks"](lines, chunk, size)


def test_records_of_the_volume_give_the_changed_chunks():
    lines = _dump(
        WRITE.format(obj=1, off=5 * MiB, size=16384),  # chunk 1
        WRITE.format(obj=2, off=0, size=512),  # the volume's properties: not data
        WRITE.format(obj=1, off=8 * MiB - 8192, size=16384),  # across chunks 1 and 2
        "FREE object = 1 offset = 33554432 length = 1048576",  # chunk 8
        "WRITE_EMBEDDED object = 1 offset = 41943040 length = 512 toguid = 1 comp = 1 etype = 0 lsize = 512 psize = 20",
        "FREE object = 1 offset = 67108864 length = -1",  # past the end of the volume
        "FREEOBJECTS firstobj = 3 numobjs = 29",
    )
    ranges, records = _changed(lines)
    assert ranges == [[1, 2], [8, 8], [10, 10]]
    assert records == 5  # the records of object 1


def test_a_free_to_the_end_inside_the_volume():
    ranges, _ = _changed(_dump("FREE object = 1 offset = 58720256 length = -1"))
    assert ranges == [[14, 15]]


def test_nothing_changed():
    assert _changed(_dump()) == ([], 0)


@pytest.mark.parametrize(
    "record",
    [
        "SPILL block for object = 1 length = 512 flags = 0 compression type = 0 payload_size = 512",
        "FREEOBJECTS firstobj = 0 numobjs = 2",
    ],
)
def test_records_that_cannot_be_placed_mark_everything(record):
    ranges, _ = _changed(_dump(record), size=40 * MiB)
    assert ranges == [[0, 9]]


@pytest.mark.parametrize(
    ("lines", "message"),
    [
        (_dump(WRITE.format(obj=1, off=0, size=512), end=False), "no END record"),
        (_dump(WRITE.format(obj=1, off=0, size=512), totals={"WRITE": 2}), "1 WRITE records parsed, the summary says 2"),
        ([line for line in _dump() if "DRR_FREE " not in line], "no DRR_FREE total"),
        (_dump("WRITE object = 1 offset = 0"), "no logical_size"),
    ],
)
def test_an_output_it_does_not_understand_fails(lines, message):
    ns = _namespace(zfsdiff.CHANGES_SCRIPT)
    with pytest.raises(ns["StreamError"], match=message):
        ns["changed_chunks"](lines, 4 * MiB, 64 * MiB)


def test_zstream_is_required(monkeypatch):
    ns = _namespace(zfsdiff.CHANGES_SCRIPT)
    monkeypatch.setattr(ns["shutil"], "which", lambda name: "/usr/sbin/zstreamdump" if name == "zstreamdump" else None)
    assert ns["dump_command"]() == ["zstreamdump", "-v"]  # before OpenZFS 2.0
    monkeypatch.setattr(ns["shutil"], "which", lambda name: None)
    with pytest.raises(ns["StreamError"], match="zstream not found"):
        ns["dump_command"]()


class ChangesSSH:
    """The node: answers by command prefix, records the commands and the streams opened."""

    def __init__(self, replies=None, fail=()):
        self.replies, self.fail = dict(replies or {}), tuple(fail)
        self.commands, self.streams = [], []

    def run(self, cmd, timeout=120):
        self.commands.append((cmd, timeout))
        for prefix in self.fail:
            if prefix in cmd:
                raise RemoteCommandError("pve1", cmd, 1, "zfs send failed (exit 1): no such snapshot")
        return next((reply for key, reply in self.replies.items() if key in cmd), "")

    def stream(self, command, input=None):
        self.streams.append((command, input))
        return object()


def test_changed_ranges_runs_the_script_on_the_node():
    ssh = ChangesSSH({"python3 -c": '{"ranges": [[1, 2], [8, 8]], "records": 4}'})
    ranges, records = zfsdiff.changed_ranges(ssh, "pool/v@a", "pool/v@b", 64 * MiB, 4 * MiB, prefix="nice -n 10 ")
    assert ranges == [(1, 2), (8, 8)] and records == 4
    cmd, timeout = ssh.commands[0]
    assert cmd.startswith("nice -n 10 python3 -c ") and cmd.endswith(f" {4 * MiB} {64 * MiB} pool/v@a pool/v@b")
    assert timeout == zfsdiff.TIMEOUT  # zfs send reads every changed block


# --------------------------------------------------------------------------- the zvol source

SIZE = 8 * CHUNK
DATASET = "rpool/data/vm-100-disk-0"


def _zvol(ssh, base=("antigrafa_A", "111"), base_size=SIZE, remote_hash=True):
    src = ZfsZvolSource(ssh, {"type": "zfspool", "pool": "rpool/data"}, Disk("scsi0", "local-zfs:vm-100-disk-0"),
                        "antigrafa_B", True, remote_hash)
    src.base, src.base_size = base, base_size
    return src


def _node(guid="111", changes='{"ranges": [[1, 2]], "records": 3}', fail=()):
    return ChangesSSH({
        "volsize": f"{SIZE}\n",
        f"guid {DATASET}@antigrafa_A": f"{guid}\n",
        f"guid {DATASET}@antigrafa_B": "222\n",
        "python3 -c": changes,
        "snapdev": "visible\tlocal\n",
    }, fail=fail)


REFERENCE = [bytes([i]) * 32 for i in range(8)]


def test_zvol_with_its_base_reads_only_the_changes():
    ssh = _node()
    src = _zvol(ssh)
    src.open_hashed_stream(REFERENCE, CHUNK)
    assert src.read_mode == "incremental since antigrafa_A: 2 of 8 chunks changed (3 ZFS records)"
    changes = next(c for c, _ in ssh.commands if "python3 -c" in c)
    assert changes.endswith(f" {CHUNK} {SIZE} {DATASET}@antigrafa_A {DATASET}@antigrafa_B")
    command, sent = ssh.streams[0]
    assert command == changed_command(f"/dev/zvol/{DATASET}@antigrafa_B", CHUNK, SIZE, src.prefix)
    assert sent == encode_changed(REFERENCE, [(1, 2)])


@pytest.mark.parametrize(
    ("kwargs", "node", "reference", "why"),
    [
        ({}, {"guid": ""}, REFERENCE, "base snapshot antigrafa_A is gone"),
        ({}, {"guid": "999"}, REFERENCE, "base snapshot antigrafa_A is gone"),  # same name, not ours
        ({"base_size": SIZE // 2}, {}, REFERENCE, f"the volume changed size ({SIZE // 2} -> {SIZE} bytes)"),
        ({}, {}, REFERENCE[:7], "the previous backup has another number of chunks"),
        ({"remote_hash": False}, {}, REFERENCE, "needs python3 on the node and remote_hash"),
        ({}, {"fail": ("python3 -c",)}, REFERENCE, "changes since antigrafa_A unknown: "),
        ({}, {"changes": "not json"}, REFERENCE, "changes since antigrafa_A unknown: "),
    ],
)
def test_zvol_reads_everything_when_the_base_cannot_be_trusted(kwargs, node, reference, why):
    ssh = _node(**node)
    src = _zvol(ssh, **kwargs)
    src.open_hashed_stream(reference, CHUNK)
    assert src.read_mode.startswith(f"full read: {why}")
    # The usual hashed read of the whole snapshot (or nothing without python3)
    assert all(c == delta.remote_command(src.stream_command(), CHUNK, src.prefix) for c, _ in ssh.streams)
    assert len(ssh.streams) == (0 if kwargs.get("remote_hash") is False else 1)


def test_zvol_without_base_is_read_in_full_silently():
    ssh = _node()
    src = _zvol(ssh, base=None)
    src.open_hashed_stream(REFERENCE, CHUNK)
    assert src.read_mode == "" and len(ssh.streams) == 1


def test_zvol_kept_as_base_is_recorded_in_the_manifest():
    ssh = _node()
    src = _zvol(ssh, base=None)
    src.keeps_base = True
    src.prepare()
    assert src.manifest_extra() == {"source_snapshot": {"name": "antigrafa_B", "guid": "222"}}
    src.keeps_base = False
    assert src.manifest_extra() == {}


@pytest.mark.parametrize(("guid", "destroyed"), [("111", True), ("999", False), ("", False)])
def test_previous_base_deleted_only_if_it_is_the_one_kept(guid, destroyed):
    ssh = _node(guid=guid)
    _zvol(ssh).drop_base()
    assert any(c == f"zfs destroy {DATASET}@antigrafa_A" for c, _ in ssh.commands) is destroyed


def test_the_snapshot_of_this_backup_is_never_dropped_as_a_base():
    ssh = _node()
    _zvol(ssh, base=("antigrafa_B", "222")).drop_base()
    assert ssh.commands == []
