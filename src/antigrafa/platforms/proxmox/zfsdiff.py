"""Which chunks of a ZFS volume changed between two snapshots.

``zfs send -i <base> <new>`` only reads and sends the blocks written since ``base``
(ZFS knows the birth of every block); ``zstream dump -v`` prints each record of that
stream. The script below, run on the node, turns the records of the volume's data
(object 1 of a zvol) into the chunks they touch: only those are read again.

Never too few: a record it cannot place (spill, redaction, objects freed) marks the
whole volume as changed, and the counts it parsed must match the summary
``zstream dump`` prints at the end, otherwise it fails and the volume is read in full.
Too many is only slower: ZFS also sends holes, and a chunk touched by a write of the
same data is read and found unchanged.
"""

from __future__ import annotations

import json
import shlex

from ...ssh import SSHHost

# argv: chunk size, size of the volume, base snapshot, new snapshot (full names).
# Prints {"ranges": [[first, last], ...], "records": n}; fails with a message.
CHANGES_SCRIPT = r'''
import json, re, shutil, subprocess, sys, tempfile

ZVOL_OBJ = 1  # data of a zvol (object 2: its properties)
FIELD = {name: re.compile(r"\b%s = (-?\d+)" % name)
         for name in ("object", "offset", "logical_size", "length", "firstobj", "numobjs")}
TOTAL = re.compile(r"Total DRR_([A-Z_]+) records = (\d+)")
# Records that write or free a range of an object, and which field gives its length
RANGES = {"WRITE": "logical_size", "WRITE_EMBEDDED": "length", "WRITE_BYREF": "length", "FREE": "length"}
COUNTED = ("WRITE", "WRITE_EMBEDDED", "WRITE_BYREF", "FREE", "FREEOBJECTS", "SPILL")


class StreamError(Exception):
    pass


def field(line, name):
    m = FIELD[name].search(line)
    if not m:
        raise StreamError("no %s in: %s" % (name, line.strip()))
    return int(m.group(1))


def changed_chunks(lines, chunk_size, size):
    """Sorted, merged (first, last) chunk ranges touched by the stream, and its records."""
    total = (size + chunk_size - 1) // chunk_size
    chunks, counts, totals = set(), {}, {}
    everything, ended, records = False, False, 0
    for line in lines:
        m = TOTAL.search(line)
        if m:
            totals[m.group(1)] = int(m.group(2))
            continue
        word = line.split(" ", 1)[0].strip()
        if word == "END":
            ended = True
        if word not in COUNTED:
            continue
        counts[word] = counts.get(word, 0) + 1
        if word == "FREEOBJECTS":
            first = field(line, "firstobj")
            everything |= first <= ZVOL_OBJ < first + field(line, "numobjs")
            continue
        if field(line, "object") != ZVOL_OBJ:
            continue
        records += 1
        if word not in RANGES:  # spill block of the volume: cannot be placed
            everything = True
            continue
        offset, length = field(line, "offset"), field(line, RANGES[word])
        if length < 0 or offset + length > size:  # -1: up to the end
            length = size - offset
        if offset >= size or length <= 0:
            continue
        chunks.update(range(offset // chunk_size, (offset + length - 1) // chunk_size + 1))
    if not ended:
        raise StreamError("incomplete stream (no END record)")
    for word in ("WRITE", "FREE"):
        if word not in totals:
            raise StreamError("no DRR_%s total in the summary: unknown zstream output" % word)
    for word, count in totals.items():
        if word in COUNTED and counts.get(word, 0) != count:
            raise StreamError("%d %s records parsed, the summary says %d" % (counts.get(word, 0), word, count))
    if everything:
        chunks = set(range(total))
    ranges = []
    for index in sorted(chunks):
        if ranges and ranges[-1][1] == index - 1:
            ranges[-1][1] = index
        else:
            ranges.append([index, index])
    return ranges, records


def dump_command():
    """zstream dump -v; zstreamdump before OpenZFS 2.0."""
    if shutil.which("zstream"):
        return ["zstream", "dump", "-v"]
    if shutil.which("zstreamdump"):
        return ["zstreamdump", "-v"]
    raise StreamError("zstream not found (zfsutils-linux)")


def main(chunk_size, size, base, snapshot):
    dumper = dump_command()
    errors = tempfile.TemporaryFile()
    send = subprocess.Popen(["zfs", "send", "-i", base, snapshot], stdout=subprocess.PIPE, stderr=errors)
    dump = subprocess.Popen(dumper, stdin=send.stdout, stdout=subprocess.PIPE, stderr=errors)
    send.stdout.close()
    try:
        lines = (line.decode("utf-8", "replace") for line in dump.stdout)
        ranges, records = changed_chunks(lines, chunk_size, size)
    finally:
        dump.stdout.close()
        failed = [(name, p.wait()) for name, p in (("zfs send", send), ("zstream dump", dump))]
    for name, rc in failed:
        if rc:
            errors.seek(0)
            raise StreamError("%s failed (exit %d): %s" % (name, rc, errors.read().decode("utf-8", "replace").strip()))
    return {"ranges": ranges, "records": records}


if __name__ == "__main__":
    try:
        result = main(int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4])
    except (StreamError, OSError) as exc:
        sys.stderr.write("%s\n" % exc)
        sys.exit(1)
    sys.stdout.write(json.dumps(result))
'''

# zfs send reads every changed block: allow it far more than an ordinary command
TIMEOUT = 4 * 3600.0


def changed_ranges(
    ssh: SSHHost, base: str, snapshot: str, size: int, chunk_size: int, prefix: str = ""
) -> tuple[list[tuple[int, int]], int]:
    """Chunk ranges of ``snapshot`` that may differ from ``base``, and the ZFS records seen.

    Raises (``RemoteCommandError``, ``ValueError``) when it cannot tell: read everything.
    """
    args = " ".join(shlex.quote(a) for a in (str(int(chunk_size)), str(int(size)), base, snapshot))
    out = ssh.run(f"{prefix}python3 -c {shlex.quote(CHANGES_SCRIPT)} {args}", timeout=TIMEOUT)
    answer = json.loads(out)
    ranges = [(int(first), int(last)) for first, last in answer["ranges"]]
    return ranges, int(answer["records"])
