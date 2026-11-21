"""Incremental reads checked against a real ZFS. Optional: skipped unless configured.

Needs a Linux machine with ZFS, python3 and zstream, reachable over SSH as root:

    ANTIGRAFA_TEST_ZFS_SSH=root@zfs-host        who to connect as
    ANTIGRAFA_TEST_ZFS_POOL=tank/scratch        where to create the test volume
    ANTIGRAFA_TEST_ZFS_SSH_KEY=~/.ssh/id_test   optional: identity file
    ANTIGRAFA_TEST_ZFS_SSH_PORT=22              optional
    ANTIGRAFA_TEST_ZFS_SSH_OPTIONS="UserKnownHostsFile=/tmp/kh"   optional: -o options

Each test creates a sparse 64 MiB volume named ``antigrafa-selftest-<random>`` under
that dataset, and destroys it (with its snapshots) at the end, even on failure. No
other dataset is touched.
"""

import hashlib
import os
import secrets
import shlex

import pytest

from antigrafa.delta import NOT_READ, read_records
from antigrafa.platforms.proxmox.capture import Disk, ZfsZvolSource
from antigrafa.ssh import SSHHost

TARGET = os.environ.get("ANTIGRAFA_TEST_ZFS_SSH", "")
POOL = os.environ.get("ANTIGRAFA_TEST_ZFS_POOL", "")

pytestmark = pytest.mark.skipif(
    not (TARGET and POOL),
    reason="optional: no ZFS host to test against (set ANTIGRAFA_TEST_ZFS_SSH and ANTIGRAFA_TEST_ZFS_POOL)",
)

MiB = 1 << 20
CHUNK = MiB
SIZE = 64 * MiB


def _ssh() -> SSHHost:
    user, _, host = TARGET.rpartition("@")
    return SSHHost(
        host,
        user=user or "root",
        port=int(os.environ.get("ANTIGRAFA_TEST_ZFS_SSH_PORT", "22")),
        identity_file=os.path.expanduser(os.environ["ANTIGRAFA_TEST_ZFS_SSH_KEY"])
        if os.environ.get("ANTIGRAFA_TEST_ZFS_SSH_KEY") else None,
        options=shlex.split(os.environ.get("ANTIGRAFA_TEST_ZFS_SSH_OPTIONS", "")),
    )


class Volume:
    def __init__(self, ssh: SSHHost) -> None:
        self.ssh = ssh
        self.name = f"antigrafa-selftest-{secrets.token_hex(4)}"
        self.dataset = f"{POOL}/{self.name}"
        self.device = f"/dev/zvol/{self.dataset}"

    def run(self, command: str) -> str:
        return self.ssh.run(command, timeout=300)

    def write(self, offset: int, length: int, source: str = "/dev/urandom") -> None:
        block = 4096
        self.run(f"dd if={source} of={shlex.quote(self.device)} bs={block} seek={offset // block} "
                 f"count={length // block} oflag=direct conv=notrunc status=none")

    def discard(self, offset: int, length: int) -> None:
        self.run(f"blkdiscard -o {offset} -l {length} {shlex.quote(self.device)}")

    def snapshot(self, name: str) -> str:
        self.run(f"sync; zfs snapshot {shlex.quote(f'{self.dataset}@{name}')}")
        return self.run(f"zfs get -Hp -o value guid {shlex.quote(f'{self.dataset}@{name}')}").strip()

    def digests(self, snapshot: str) -> list[bytes]:
        """Every chunk of the snapshot, read in full: what the backup must find."""
        out = []
        with self.ssh.stream(f"dd if={shlex.quote(f'{self.device}@{snapshot}')} bs=1M status=none") as stream:
            while True:
                data = stream.stdout.read(CHUNK)
                while data and len(data) < CHUNK:
                    more = stream.stdout.read(CHUNK - len(data))
                    if not more:
                        break
                    data += more
                if not data:
                    return out
                out.append(hashlib.sha256(data).digest())

    def source(self, snapshot: str, base: tuple[str, str] | None) -> ZfsZvolSource:
        src = ZfsZvolSource(self.ssh, {"type": "zfspool", "pool": POOL}, Disk("scsi0", f"test:{self.name}"),
                            snapshot, False, True)
        src.base, src.base_size = base, SIZE
        return src


@pytest.fixture
def volume():
    vol = Volume(_ssh())
    vol.run(f"zfs create -s -V {SIZE} -o volblocksize=16K -o snapdev=visible {shlex.quote(vol.dataset)}")
    try:
        vol.run(f"udevadm settle; for i in $(seq 1 50); do [ -e {shlex.quote(vol.device)} ] && exit 0; sleep 0.2; "
                "done; exit 1")
        vol.write(0, SIZE)
        yield vol
    finally:
        vol.run(f"zfs destroy -r {shlex.quote(vol.dataset)}")


def _read(src: ZfsZvolSource, reference: list[bytes]) -> list[tuple[bytes, object, int]]:
    src.prepare()
    try:
        with src.open_hashed_stream(reference, CHUNK) as stream:
            return list(read_records(stream.stdout, reference))
    finally:
        src.cleanup()


def test_incremental_read_finds_exactly_the_new_image(volume):
    guid_a = volume.snapshot("antigrafa_a")
    before = volume.digests("antigrafa_a")
    volume.write(5 * MiB + 4096, 4096)  # inside chunk 5
    volume.write(21 * MiB - 4096, 8192)  # across chunks 20 and 21
    volume.write(50 * MiB, 64 * 1024, source="/dev/zero")  # zeros in chunk 50
    volume.discard(40 * MiB, 2 * MiB)  # freed: chunks 40 and 41
    volume.snapshot("antigrafa_b")
    after = volume.digests("antigrafa_b")
    changed = {i for i, (a, b) in enumerate(zip(before, after)) if a != b}
    assert changed == {5, 20, 21, 40, 41, 50}

    src = volume.source("antigrafa_b", ("antigrafa_a", guid_a))
    records = _read(src, before)
    assert src.read_mode.startswith("incremental since antigrafa_a: ")
    # The backup is the new image, chunk for chunk
    assert [digest for digest, _, _ in records] == after
    read = {i for i, (_, data, _) in enumerate(records) if data is not NOT_READ}
    assert changed <= read  # every changed chunk was read
    assert len(read) <= len(changed) + 4  # and little else (the whole volume is 64)

    src.drop_base()
    assert volume.run(f"zfs list -H -t snapshot -o name -d 1 {shlex.quote(volume.dataset)}").split() == [
        f"{volume.dataset}@antigrafa_b"
    ]


def test_nothing_written_means_nothing_read(volume):
    guid_a = volume.snapshot("antigrafa_a")
    before = volume.digests("antigrafa_a")
    volume.snapshot("antigrafa_b")
    src = volume.source("antigrafa_b", ("antigrafa_a", guid_a))
    records = _read(src, before)
    assert src.read_mode.startswith("incremental since antigrafa_a: 0 of 64 chunks changed")
    assert [data for _, data, _ in records] == [NOT_READ] * 64
    assert [digest for digest, _, _ in records] == before


def test_base_snapshot_gone_means_a_full_read(volume):
    guid_a = volume.snapshot("antigrafa_a")
    before = volume.digests("antigrafa_a")
    volume.run(f"zfs destroy {shlex.quote(f'{volume.dataset}@antigrafa_a')}")
    volume.write(3 * MiB, 4096)
    volume.snapshot("antigrafa_b")
    src = volume.source("antigrafa_b", ("antigrafa_a", guid_a))
    records = _read(src, before)
    assert src.read_mode == "full read: base snapshot antigrafa_a is gone"
    assert NOT_READ not in [data for _, data, _ in records]
    assert [digest for digest, _, _ in records] == volume.digests("antigrafa_b")
