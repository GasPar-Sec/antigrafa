import hashlib
import io
import os
import shlex
import struct
import subprocess
import sys

import pytest

from antigrafa import delta
from antigrafa.delta import MAGIC, DeltaError, encode_reference, read_records, remote_command

CHUNK = 1024


def _sha(data):
    return hashlib.sha256(data).digest()


def _script():
    """The remote script, loaded without running its main part."""
    namespace = {"__name__": "antigrafa_remote"}
    exec(compile(delta.REMOTE_SCRIPT, "remote", "exec"), namespace)  # noqa: S102 - our own script
    return namespace


def _relay(data, reference, chunk_size=CHUNK):
    out = io.BytesIO()
    _script()["relay"](io.BytesIO(data), io.BytesIO(encode_reference(reference)), out, chunk_size)
    out.seek(0)
    return out


def _chunks(data, size=CHUNK):
    return [data[i : i + size] for i in range(0, len(data), size)]


def test_only_changed_chunks_are_sent():
    old = os.urandom(CHUNK * 4 + 100)
    new = bytearray(old)
    new[CHUNK + 5] ^= 0xFF  # chunk 1 changed
    new = bytes(new) + os.urandom(CHUNK)  # disk grew: tail and a new chunk differ
    reference = [_sha(c) for c in _chunks(old)]

    out = _relay(new, reference)
    raw = out.getvalue()
    assert raw.startswith(MAGIC) and raw.endswith(b"E")
    # 3 unchanged chunks: 5 bytes each instead of 1024
    assert len(raw) < len(MAGIC) + 3 * CHUNK + 300 + 3 * 37 + 3 * 5 + 1

    records = list(read_records(out, reference))
    expected = _chunks(new)
    assert [length for _, _, length in records] == [len(c) for c in expected]
    assert [d for d, _, _ in records] == [_sha(c) for c in expected]
    sent = [i for i, (_, data, _) in enumerate(records) if data is not None]
    assert sent == [1, 4, 5]  # the changed chunk, the old tail (now longer), the new chunk
    assert records[1][1] == expected[1]


def test_without_reference_everything_is_sent():
    data = os.urandom(CHUNK * 2)
    records = list(read_records(_relay(data, []), []))
    assert [r[1] for r in records] == _chunks(data)


def test_empty_disk():
    assert list(read_records(_relay(b"", []), [])) == []


def test_script_runs_on_a_real_interpreter(tmp_path):
    """The whole script, as the source host runs it: its own process, reading a command."""
    image = tmp_path / "disk.img"
    data = os.urandom(CHUNK * 3)
    image.write_bytes(data)
    reference = [_sha(c) for c in _chunks(data)]
    reference[2] = bytes(32)  # pretend chunk 2 changed
    # Reading command, run by the script through the platform's shell
    read = f'type "{image}"' if sys.platform == "win32" else f"cat {shlex.quote(str(image))}"
    res = subprocess.run(
        [sys.executable, "-c", delta.REMOTE_SCRIPT, str(CHUNK), read],
        input=encode_reference(reference),
        capture_output=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr
    records = list(read_records(io.BytesIO(res.stdout), reference))
    assert [r[1] is None for r in records] == [True, True, False]
    assert records[2][1] == data[2 * CHUNK :]


def test_script_reports_a_failed_read():
    res = subprocess.run(
        [sys.executable, "-c", delta.REMOTE_SCRIPT, str(CHUNK), "exit 3"],
        input=encode_reference([]),
        capture_output=True,
        timeout=60,
    )
    assert res.returncode == 3  # the exit status of the read command: the backup fails
    assert list(read_records(io.BytesIO(res.stdout), [])) == []


def test_remote_command_quotes_everything():
    cmd = remote_command("dd if='/dev/zvol/rpool/vm 1' bs=4M", 4194304, prefix="nice -n 10 ")
    assert cmd.startswith("nice -n 10 python3 -c ")
    argv = shlex.split(cmd)
    assert argv[:4] == ["nice", "-n", "10", "python3"]
    assert argv[5] == delta.REMOTE_SCRIPT  # the script reaches python3 unchanged
    assert argv[6:] == ["4194304", "dd if='/dev/zvol/rpool/vm 1' bs=4M"]


def test_encode_reference():
    raw = encode_reference([b"a" * 32, b"b" * 32])
    assert struct.unpack(">Q", raw[:8]) == (2,) and raw[8:] == b"a" * 32 + b"b" * 32


def _stream(*parts):
    return io.BytesIO(MAGIC + b"".join(parts))


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "is python3 installed"),
        (b"bash: python3: command not found\n", "is python3 installed"),
        (MAGIC, "stream ended early"),
        (MAGIC + b"X", "invalid record"),
        (MAGIC + b"D" + bytes(10), "stream ended early"),
        (MAGIC + b"D" + _sha(b"abc") + struct.pack(">I", 3) + b"abd" + b"E", "corrupted in transit"),
        (MAGIC + b"S" + struct.pack(">I", 10) + b"E", "the reference has only 0"),
    ],
)
def test_damaged_streams_are_rejected(raw, message):
    with pytest.raises(DeltaError, match=message):
        list(read_records(io.BytesIO(raw), []))


def test_unchanged_record_uses_the_reference_digest():
    ref = [_sha(b"x")]
    records = list(read_records(_stream(b"S" + struct.pack(">I", 1), b"E"), ref))
    assert records == [(ref[0], None, 1)]
