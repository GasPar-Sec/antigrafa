"""Hashing on the source host: only the chunks that changed cross the network.

Antigrafa sends the digests of the previous backup of a disk (the *reference*) to a
small Python script run on the source host. The script reads the snapshot chunk by
chunk, hashes each chunk and compares it with the reference at the same position:

- same digest: only a short record is sent ("unchanged, n bytes");
- different (or no reference): the digest and the chunk are sent.

Antigrafa rebuilds a complete index from it, so every backup is still a full image:
the repository format, ``verify`` and ``restore`` do not change. The digest of every
chunk received is checked again on arrival.

What saves time: the network carries only what changed, and the destination only
receives the new chunks (an unchanged chunk is not even looked up in the store). The
source still reads the whole snapshot, at low priority, and hashes it.

Wire format (integers big-endian)::

    to the script:   count (8 bytes), then ``count`` digests (32 bytes each)
    from the script: MAGIC, then records, then b"E"
        b"S" + length (4)                          unchanged
        b"D" + digest (32) + length (4) + data     sent
"""

from __future__ import annotations

import shlex
import struct
from collections.abc import Iterator, Sequence
from typing import BinaryIO

from .chunkstore import digest_of

MAGIC = b"ANTIGRAFA-DELTA-1\n"
DIGEST_SIZE = 32
_LENGTH = struct.Struct(">I")
_COUNT = struct.Struct(">Q")

# Runs on the source host with its own python3 (standard library only, Python 3.5+).
# argv: chunk size, command that writes the snapshot to its standard output.
REMOTE_SCRIPT = r'''
import hashlib, struct, subprocess, sys

MAGIC = b"ANTIGRAFA-DELTA-1\n"


def read_full(stream, size):
    parts = []
    while size:
        buf = stream.read(size)
        if not buf:
            break
        parts.append(buf)
        size -= len(buf)
    return b"".join(parts)


def relay(source, reference, out, chunk_size):
    count = struct.unpack(">Q", read_full(reference, 8))[0]
    ref = read_full(reference, 32 * count)
    out.write(MAGIC)
    index = 0
    while True:
        data = read_full(source, chunk_size)
        if not data:
            break
        digest = hashlib.sha256(data).digest()
        if ref[32 * index:32 * index + 32] == digest:
            out.write(b"S" + struct.pack(">I", len(data)))
        else:
            out.write(b"D" + digest + struct.pack(">I", len(data)) + data)
        index += 1
    out.write(b"E")
    out.flush()


if __name__ == "__main__":
    chunk_size, command = int(sys.argv[1]), sys.argv[2]
    proc = subprocess.Popen(command, shell=True, stdout=subprocess.PIPE)
    relay(proc.stdout, sys.stdin.buffer, sys.stdout.buffer, chunk_size)
    proc.stdout.close()
    sys.exit(proc.wait())
'''


class DeltaError(Exception):
    """The answer of the source host is not a valid delta stream."""


def remote_command(read_command: str, chunk_size: int, prefix: str = "") -> str:
    """Shell command for the source host: ``read_command`` piped through the script.

    ``prefix`` (``nice``, ``ionice``...) applies to the script and to the read command.
    """
    return f"{prefix}python3 -c {shlex.quote(REMOTE_SCRIPT)} {int(chunk_size)} {shlex.quote(read_command)}"


def encode_reference(digests: Sequence[bytes]) -> bytes:
    """What the script reads on its standard input."""
    return _COUNT.pack(len(digests)) + b"".join(digests)


def _exact(stream: BinaryIO, size: int, index: int) -> bytes:
    parts = []
    remaining = size
    while remaining:
        buf = stream.read(remaining)
        if not buf:
            raise DeltaError(f"chunk {index}: stream ended early")
        parts.append(buf)
        remaining -= len(buf)
    return b"".join(parts)


def read_records(stream: BinaryIO, reference: Sequence[bytes]) -> Iterator[tuple[bytes, bytes | None, int]]:
    """(digest, data or None when unchanged, length) for each chunk, in order."""
    try:
        magic = _exact(stream, len(MAGIC), 0)
    except DeltaError:
        magic = b""
    if magic != MAGIC:
        raise DeltaError("unexpected answer from the source host (is python3 installed?)")
    index = 0
    while True:
        kind = _exact(stream, 1, index)
        if kind == b"E":
            return
        if kind == b"S":
            (length,) = _LENGTH.unpack(_exact(stream, _LENGTH.size, index))
            if index >= len(reference):
                raise DeltaError(f"chunk {index}: marked unchanged, but the reference has only {len(reference)}")
            yield reference[index], None, length
        elif kind == b"D":
            digest = _exact(stream, DIGEST_SIZE, index)
            (length,) = _LENGTH.unpack(_exact(stream, _LENGTH.size, index))
            data = _exact(stream, length, index)
            if digest_of(data) != digest:
                raise DeltaError(f"chunk {index}: digest mismatch, corrupted in transit")
            yield digest, data, length
        else:
            raise DeltaError(f"chunk {index}: invalid record {kind!r}")
        index += 1
