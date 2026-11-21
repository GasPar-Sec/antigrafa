# Antigrafa

> [!WARNING]
> **Work in progress — not ready for production.** Antigrafa is under active development
> and has not yet been tested against a real Proxmox cluster. Do not rely on it as your
> only backup. See [ROADMAP.md](ROADMAP.md) for known limitations.

Backups of virtual machines and containers, run from a separate machine, that
**never slow down the guests**, with a throughput that adapts to the destination and
to the load on the hosts.

## Supported platforms

| Platform | Status |
|---|---|
| [Proxmox VE](#proxmox-ve) | supported (VMs and containers) |
| VMware vSphere | planned |
| Microsoft Hyper-V | planned |

The backup engine, the destinations and the repository format are the same for every
platform. Each platform only provides the list of guests, the snapshot and a way to
read the disks: see [Adding a platform](#adding-a-platform).

## How Antigrafa works: capture and transfer are separate

```
 Source host                           Antigrafa machine                  Destination
+---------------------------+         +----------------------------+     +--------------+
| VM --writes--> disk       |         | 4 MiB chunks --> SHA-256   |     | local disk   |
|                 |         |         | already known? --> skip    |     | or NFS       |
|          snapshot (~1 s)  |         | new --> zstd --> write     | --> |              |
|                 |         |   SSH   | (adaptive concurrency)     |     | .chunks/     |
| read at low priority:     | ------> |                            |     | vm/  ct/     |
| dd, rbd export or tar     |         |                            |     |              |
+---------------------------+         +----------------------------+     +--------------+
   ^ host iowait:                        ^ destination write latency:
     throttles reads                       throttles writes
```

1. **Capture**: snapshot through the platform, consistent across all disks of the
   guest. The guest resumes right away.
2. **Transfer**: Antigrafa reads the snapshot (over SSH for Proxmox VE: `dd`,
   `rbd export` or `tar`, depending on the storage). If the destination slows down,
   only the backup slows down: the guest is never in the loop. Only what changed
   crosses the network (see [Only changes are sent](#only-changes-are-sent)).
3. **Cleanup**: the snapshot is deleted in every case, success or failure. After a
   stopped read (cancel, error), the reader still running on the host is killed first:
   a snapshot in use cannot be deleted. The deletion is tried again for about 30 s, and
   on Proxmox VE a guest left locked (`snapshot-delete`) by a failed deletion of the
   Antigrafa snapshot is unlocked (`qm unlock` / `pct unlock`, over SSH).

The trade-off: the snapshot takes space on the source storage for as long as the
transfer lasts.

## Adaptive control

| Side | Measured | Adjusted |
|---|---|---|
| Destination | chunk write latency | number of parallel writes (AIMD: +1 when it keeps up, ×0.7 when it saturates) |
| Destination | write time vs. compression time | zstd level: compress harder when the disk is slow, less when the CPU is the bottleneck |
| Source | iowait of the host running the guest (when the platform reports it) | read rate cap: halved when iowait rises, +25% when it drops |
| Source | — | reads run at low priority (`ionice -c2 -n7 nice -n10` on Proxmox VE) |
| Source | — | disks are read in direct I/O (`O_DIRECT`, like vzdump), so the read does not fill the node's page cache (or, on a zvol, the cache and the ZFS ARC) and push the guests' memory to swap. Not container archives (`tar` reads the files) |

The destination is shared: a single controller drives every concurrent backup that
writes to it.

## Only changes are sent

For a slow link (a remote site over the internet) or a slow destination, the chunks
are hashed on the source host, before anything is sent:

1. Antigrafa sends the digests of the previous backup of the disk to a small Python
   script run on the host (standard library only, nothing to install);
2. the script reads the snapshot chunk by chunk and hashes each chunk;
3. a chunk with the same digest as before at the same position is not sent: only a
   5-byte "unchanged" record is. A changed chunk is sent with its digest, checked
   again on arrival;
4. Antigrafa writes a complete index: every backup is still a full image, restored
   on its own.

| | Without (`remote_hash = false`) | With (default) |
|---|---|---|
| Network | every chunk | changed chunks only |
| Destination | lookup of every chunk, writes of new ones | writes of changed chunks only |
| Source host | reads the snapshot | reads the snapshot and hashes it (CPU, at low priority) |

The first backup of a disk sends everything. The report shows what was sent
(`Sent over the network: 1.2 GiB (3% of what was read)`). On Proxmox VE, without
`python3` on a node, its chunks are all sent (a warning says so). Container archives
gain less: a file that changes size shifts everything after it in the archive.

## Proxmox VE

### Why not just PBS?

With Proxmox Backup Server, a VM backup runs inside QEMU using *copy-before-write*:
when the VM writes to a block that has not been backed up yet, QEMU must first send
the old content to PBS. If the destination is slow, **the VM's writes wait for the
destination** and the VM stops responding. *Fleecing* (Proxmox 8.2+) mitigates this
without removing it. Antigrafa snapshots through the API instead (filesystems frozen
through the qemu-guest-agent when it is enabled) and reads the snapshot afterwards.

### Supported storage

| Proxmox storage | VM, `snapshot_mode = "qemu"` | VM, other modes | Container |
|---|---|---|---|
| ZFS (`zfspool`) | yes | zvol snapshot, raw image | `subvol-*` dataset, tar archive |
| Ceph RBD (hyperconverged or external) | yes | `rbd export` of a snapshot, raw image | raw image |
| LVM-thin | yes | `snap_<vol>_<snap>` snapshot, raw image | raw image |
| LVM (thick), iSCSI LUN, raw files on directory, NFS or CIFS | yes | no | no |
| qcow2 on directory, NFS or CIFS | yes, while the VM runs | no | — |
| Passthrough disks | no | no | no |

In `"qemu"` mode, a running VM is read through QEMU whatever the storage; the storage
only matters when the VM is stopped (its volume is then read directly: every storage
above except qcow2). Disks flagged `backup=0` are skipped. For containers, only
`rootfs` and mount points flagged `backup=1` are backed up, matching Proxmox's
behaviour.

What each storage implies for a backup:

| Storage | Advantages | Drawbacks |
|---|---|---|
| **ZFS zvol** (VM) | Instant snapshot that costs nothing to create. Unallocated blocks read fast. | The whole volume is read at every backup (only changed chunks are sent, but everything is read). The snapshot keeps the old blocks while the backup runs, so the pool must have room for what the VM writes meanwhile. `snapdev` is switched to `visible` during the read, then restored. |
| **ZFS dataset** (container `subvol-*`) | Only the files are read, not the free space: fast for a mostly empty container. Ownership, ACLs and extended attributes are kept (`tar --numeric-owner --xattrs --acls`). | No exact size in advance: the progress is estimated (`~`). A tar archive cut into fixed-size chunks deduplicates poorly once files change size: everything after a change shifts (content-defined chunking is on the roadmap). |
| **Ceph RBD** | Instant snapshots, hyperconverged or external clusters (`monhost`, keyring). The read can run on any node that reaches the cluster. | `rbd export` reads the whole image over the Ceph network at every backup, which loads the cluster network and OSDs. The snapshot keeps the old objects until it is deleted. |
| **LVM-thin** | Instant thin snapshot, no size to reserve. | The snapshot lives in the same thin pool as the volumes. If the pool fills up while the backup runs, **every volume of the pool** stops writing: keep free room in the pool. The snapshot must be activated to be read (`lvchange -ay -K`). The whole volume is read at every backup. |
| **LVM thick, iSCSI LUN, raw files** | Simple and often shared (SAN). With `"qemu"`, VMs are backed up without downtime. | No storage snapshot: VMs only with `snapshot_mode = "qemu"`, containers not at all. With the default mode, a single such disk on a guest, even flagged `backup=0`, makes Proxmox refuse the snapshot of the whole guest (`snapshot feature is not available`); `"storage"` and `"qemu"` ignore it. |
| **qcow2** (directory, NFS, CIFS) | With `"qemu"`, read by QEMU like any other disk. | Not readable otherwise (reading an internal snapshot of a qcow2 file in use is not safe), and not when the VM is stopped. |
| **Passthrough disks** | — | Never backed up: not a Proxmox volume. Flag them `backup=0` (refused by Proxmox snapshots even then; fine with `"storage"` and `"qemu"`). |

### Snapshot modes

`snapshot_mode`, in the `[source.<name>]` section, chooses how the disks are captured.
No mode stops a guest. A guest can have its own mode, by id:

```toml
[source.cluster1]
snapshot_mode = "auto"        # every guest of the source...

[source.cluster1.guests.650]
snapshot_mode = "qemu"        # ...except this one
```

**`"auto"`** decides for each guest, at each backup, and logs its choice
(`VM 650 (nextcloud): captured by storage (Proxmox cannot snapshot virtio2 ...)`):

1. `"proxmox"` if Proxmox can snapshot every disk of the guest and the token has
   `VM.Snapshot` on it: the most standard way;
2. else `"storage"` if every disk backed up is on ZFS, Ceph RBD or LVM-thin: same
   result, without the disks flagged `backup=0` getting in the way;
3. else `"qemu"` for a VM (any storage). A container that fits neither 1 nor 2 cannot
   be backed up.

The default stays `"proxmox"`.

| | `"proxmox"` (default) | `"storage"` | `"qemu"` |
|---|---|---|---|
| How | Proxmox snapshots the guest, through the API | Antigrafa snapshots each volume, over SSH, on its storage (`zfs snapshot`, `rbd snap create`, `lvcreate -s`) | running VMs: QEMU keeps a frozen view of each disk (copy-before-write, as PBS does); stopped VMs: their volumes are read directly; containers: as `"storage"` |
| Storages (VMs) | with snapshots only | with snapshots only | all of them, qcow2 included |
| Disks involved | every disk of the guest, `backup=0` ones included | only the disks backed up | only the disks backed up |
| Consistency | filesystems frozen by Proxmox through the qemu-guest-agent | the same way: `qm guest cmd <id> fsfreeze-freeze` (honours `freeze-fs-on-backup=0`). Containers: `lxc-freeze`, then `sync`. Thawed right after the snapshots | the same freeze, thawed as soon as QEMU has started the backup of every disk (one transaction: the same instant for all disks) |
| Cost while the backup runs | the storage snapshot keeps the old blocks | the same | before the VM overwrites a block not read yet, QEMU copies it to a temporary image in `fleecing_dir` (`/var/lib/antigrafa/fleecing` on the node by default, root only): VM writes are a bit slower, and the image grows with what the VM writes |
| Visible in Proxmox | yes, in the guest's snapshot list | no. A snapshot left by an interrupted run is reported at the next backup of the guest | the VM is locked (`backup`), as with vzdump |
| API rights | `VM.Audit`, `VM.Snapshot` | `VM.Audit` (SSH as root does the rest) | `VM.Audit` (SSH as root does the rest) |
| Proxmox lock | while the snapshot is created and deleted | none: moving or deleting a disk during the backup makes that backup fail | `lock=backup` for the whole read: no migration, snapshot, disk move or start meanwhile |

`"auto"` picks among these three for you. To force one: `"qemu"` for VMs with disks
on storages without snapshots (iSCSI, LVM thick, NFS files); `"storage"` when the
disks backed up are on ZFS, Ceph RBD or LVM-thin and only a disk flagged `backup=0`
blocks Proxmox's snapshot.

`"qemu"` mode in detail:

- the temporary images only grow with what the VM writes during its backup, but if
  the filesystem of `fleecing_dir` fills up, **QEMU fails the VM's writes**: put it
  where there is room for a busy VM's writes during a full read;
- `python3` and `qemu-img` are needed on the nodes (both present on Proxmox VE);
- `tpmstate` disks are used by swtpm, not by QEMU: they are read from their volume
  (a few KiB, written only by TPM operations);
- a VM already locked (another backup, a migration) is not backed up;
- a run killed outright (power cut, `kill -9`) leaves the VM locked, and its temporary
  image keeps growing with the VM's writes: run `qm unlock <id>`, then back it up
  again, which first removes what the killed run left in QEMU. A cancelled run
  (Ctrl+C, web interface) cleans up by itself.

With snapshots, the capture is consistent across the disks of the guest: they are all
taken while the filesystems are frozen. Without the guest agent, or when the freeze
fails (agent not running), the capture is crash-consistent, as after a power cut.
Antigrafa logs it and goes on. If a guest cannot be thawed after several tries, the
backup fails with the command to run by hand, since the guest cannot write until then.
A stopped guest is not frozen: it writes nothing.

### Requirements

- an API token with these privileges (checked before anything is done, see below):

  | Privilege | Path | Used for |
  |---|---|---|
  | `VM.Audit` | `/vms/<id>` | listing the guests, reading their configuration |
  | `VM.Snapshot` | `/vms/<id>` | creating and deleting the temporary snapshot (not needed with `snapshot_mode = "storage"` or `"qemu"`; optional with `"auto"`, which then avoids Proxmox snapshots) |
  | `Sys.Audit` | `/nodes/<node>` | host load (iowait), to throttle reads |
  | `Datastore.Audit` | `/storage/<id>` | how to read each disk (storage type, ZFS pool, Ceph pool...) |

  ```sh
  pveum role add AntigrafaBackup --privs "VM.Audit VM.Snapshot Sys.Audit Datastore.Audit"
  pveum acl modify / --roles AntigrafaBackup --tokens 'antigrafa@pve!backup'
  ```

  With privilege separation (the default for a token), the token's user needs them too;
- the cluster's CA certificate (`/etc/pve/pve-root-ca.pem`, copied from a node) for
  `ca_file`, so that the API certificate is verified. `verify_tls = false` skips the
  verification, for labs only: the API token can then be intercepted;
- an SSH key from the Antigrafa machine in `/root/.ssh/authorized_keys` on every node.

See the `[source.cluster1]` section of [antigrafa.example.toml](antigrafa.example.toml).

Before the first snapshot, `backup` asks Proxmox for the token's effective rights
(`/access/permissions`) on every selected guest, its node and its storages. If one is
missing, nothing is done: the program lists every missing privilege and its path, and
exits with code 3. `guests` does the same when it lists nothing, since Proxmox hides
the guests a token cannot see.

## Installation

```sh
python3 -m venv /opt/antigrafa && /opt/antigrafa/bin/pip install .
cp antigrafa.example.toml /etc/antigrafa/antigrafa.toml
```

Requires Python 3.11 or later. On Python 3.14+, zstd comes from the standard library
and the project has no dependencies.

On Windows, the configuration goes to `%PROGRAMDATA%\antigrafa\antigrafa.toml`
(`C:\ProgramData\antigrafa\antigrafa.toml`) instead of `/etc/antigrafa/antigrafa.toml`;
`-c` picks another file.

Declare one `[source.<name>]` section per cluster or host (with its `type`), and one
`[target.<name>]` per destination. Each platform has its own requirements: see
[Proxmox VE](#requirements).

## Usage

```sh
antigrafa guests                          # VMs and CTs of the source
antigrafa backup --all --target nas       # back up everything
antigrafa backup --guest 100 101 --target local
antigrafa backup --source cluster2 --all  # --source: when several sources are configured
antigrafa list --target nas
antigrafa verify --target nas             # re-read every chunk and check its digest
antigrafa restore --target nas cluster1/vm/100/2026-11-17T22-30-00Z scsi0 /tmp/scsi0.raw
```

Output, for every command (options go before the command name):

| Option | Effect |
|---|---|
| `-q` | warnings and errors only (cron) |
| *(none)* | main steps |
| `-v` | details: SSH commands, API calls and their duration, disks found and how each one is read, adaptive control adjustments (parallel writes and their latency, zstd level, read cap from the host's iowait) |
| `-vv` | as `-v`, with milliseconds, thread (one per guest backed up in parallel) and module |
| `--progress` / `--no-progress` | progress line for `backup`, `restore` and `verify`: percentage, amount, rate, ETA, and each disk being read with its guest id and its own percentage (`100/scsi0 63%`). Container archives (ZFS datasets) have no exact size: their percentage is estimated from the snapshot (`150/rootfs ~63%`) and stays below 100 % until the end. Shown by default on a terminal only, never with `-q` unless `--progress` |

```sh
antigrafa -v backup --all --target nas    # details, with the progress line
antigrafa -q backup --all --target nas    # cron: silent unless something goes wrong
```

When several guests are backed up at once (`max_parallel_guests`), each disk being
read gets its own bar, with its rate and ETA, above the overall line:

```
  100/scsi0   [############--------]  63%  12.6 GiB/20.0 GiB  140.2 MiB/s  ETA 54s
  150/rootfs  [####----------------]~ 22%  1.2 GiB/~5.4 GiB  72.0 MiB/s  ETA 59s
backup  [########------------]~ 54%  13.8 GiB/25.4 GiB  212.2 MiB/s  ETA 56s  1m 06s  0/3 guests
```

This needs a terminal that understands ANSI sequences (Linux, macOS, Windows 10 and
later); otherwise, and when a single disk is being read, everything stays on one line.

At the end, `backup` prints a report of where the time went: start and end, then
for each guest its total, its phases and each disk (size, time, rate, chunks):

```
Backup report: 2026-11-19 22:30:00 -> 22:41:12 (11m 12s)

OK     VM 100 (web)                    10m 02s  cluster1/vm/100/2026-11-19T22-30-00Z
       waiting for host 0.0s, preparation 0.4s, snapshot 1.2s, transfer 9m 50s, snapshot removal 2.0s, flush 8.3s
       scsi0          20.0 GiB    9m 48s    34.8 MiB/s  1200 new chunks, 3920 reused
       efidisk0      528.0 KiB      0.1s     5.2 MiB/s  1 new chunks, 0 reused

FAILED CT 150 (dns)                       6.8s  tar: read error
       waiting for host 0.0s, preparation 0.3s, snapshot 0.5s, transfer 4.8s, snapshot removal 1.2s

Total: 2 guest(s), 1 OK, 1 failed, in 11m 12s
Read: 20.0 GiB, 30.5 MiB/s on average
Longest: VM 100 (web) (10m 02s)
New data: 4.7 GiB -> 2.1 GiB written (final zstd level 5, 6 parallel writes)
```

*Waiting for host*: time spent waiting for other guests on the same host
(`max_parallel_per_host`). *Flush*: forcing the data onto the destination's disk,
then writing the manifest. A failed guest shows the phases it reached. The report is
printed even with `-q`.

### Log file

Every command, the web interface included, also writes its log to a file. The file
always gets INFO, and DEBUG with `-v`: `-q` only quiets the terminal. Each line
carries the date, the process and the thread, because a backup started by cron and
the web interface may write the same file. Everything is set in the `[log]` section:

| Key | Default | Meaning |
|---|---|---|
| `file` | `/var/log/antigrafa/antigrafa.log`, `%PROGRAMDATA%\antigrafa\antigrafa.log` | the `.log` file; `"none"`: no log file |
| `max_size_mib` | `1024` | past this size, the file is archived and a new one starts |
| `max_archives` | `5` | archives kept; the oldest ones are removed first |
| `compression` | `zip` | `zip`, `tar.gz`, `rar` or `7z` |
| `compression_level` | `6` (zip, tar.gz), `3` (rar), `5` (7z) | `0` (none) to `9` (smallest, slowest); `0` to `5` for rar (`-m0` to `-m5`), 7z uses `-mx0` to `-mx9` |

`zip` and `tar.gz` need nothing else. Python cannot write `rar` or `7z` archives, so
those two need their program:

- **rar**: the `rar` program, looked up in the `PATH`, then in `Program Files\WinRAR\Rar.exe`
  on Windows. It is not free software, and on Debian it is in the `non-free` section.
- **7z**: `7z`, `7zz` or `7za` (`7zip` or `p7zip-full` package), looked up in the `PATH`,
  then in `Program Files\7-Zip\7z.exe` on Windows.

If the program is missing, the archives are written in zip and a warning is shown.
This way the log is never lost.

The archives sit next to the file, one per rotation, named after the time the file was
archived: `antigrafa-2026-11-20T10-15-00.log.zip`. Each archive holds one file with
the same name, so several archives can be extracted side by side. Compression runs in
the background, so the backups keep logging meanwhile. If it fails, the file stays
uncompressed next to the log and the error is written to the log. The file is opened
for each line and closed right after, so any process can archive it, Windows
included. When the file cannot be written (a user without the rights of the service
account), the command still runs and prints a warning.

## Web interface

A web page to start backups, follow them live and cancel them, served over HTTPS only,
on Linux and Windows:

```sh
pip install '.[web]'                 # Django, cheroot (HTTPS server), cryptography
antigrafa web adduser admin          # asks for the password (12 characters or more)
antigrafa web serve                  # https://<this machine>:8443/
```

- **Start**: source, destination, then every guest (with exclusions) or the chosen
  ones. The configuration, the destination, the guests and the token's rights are
  checked before the page says it started: an error shows up at once.
- **Follow**: one bar per disk being read and an overall bar (percentage, rate, ETA),
  each guest's result as it ends, the log, then the full report.
- **Cancel**: the disks being read stop at the next chunk, the snapshots are deleted
  and the unfinished backups removed; guests not started yet are skipped.
- One backup at a time. The last 20 runs stay listed until the server stops.

**Certificate.** Without one, a self-signed certificate is created on the first start
(ECDSA P-256, valid 397 days, renewed by Antigrafa 30 days before it expires) for the
machine's names and addresses. The browser warns on the first visit: check that it shows
the SHA-256 fingerprint printed at startup. To use your own certificate, set `cert_file`
and `key_file`; Antigrafa never replaces a certificate it did not create.

**Data.** Users (passwords hashed by Django), sessions, the secret key and the generated
certificate live in `data_dir`: `/var/lib/antigrafa/web` on Linux (mode 0700),
`%PROGRAMDATA%\antigrafa\web` on Windows (rights limited to SYSTEM, Administrators and
the account running Antigrafa).

**Stopping.** Ctrl+C, or SIGTERM (`systemctl stop`): a running backup is cancelled
first, so that its snapshots are deleted.

| Command | Effect |
|---|---|
| `antigrafa web serve` | start the web interface; `antigrafa -v web serve`: the details (`-v` above) in the console and in the page's log |
| `antigrafa web adduser <name>` | create a user, or change its password (`--password-stdin` for scripts) |
| `antigrafa web deluser <name>` | remove a user |

Running as a service on Linux (systemd), `/etc/systemd/system/antigrafa-web.service`:

```ini
[Unit]
Description=Antigrafa web interface
After=network-online.target

[Service]
ExecStart=/opt/antigrafa/bin/antigrafa web serve
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

On Windows, `antigrafa web serve` runs in a console, or at startup through the Task
Scheduler (run whether the user is logged on or not, with the account that has the SSH
key). See the `[web]` section of [antigrafa.example.toml](antigrafa.example.toml).

## Repository format

```
<destination>/
  .chunks/<4 hex>/<sha256>          chunk (header + raw or zstd), shared by every backup
  <source>/vm/<guest id>/<timestamp>/
    manifest.json                    written last: its presence means the backup is complete
    scsi0.img.fidx                   index: size + ordered list of digests
  <source>/ct/<guest id>/<timestamp>/
    rootfs.tar.fidx
```

`<source>` is the name of the `[source.<name>]` section: guests of two clusters never
mix, even with the same id, while their chunks are still deduplicated together. The
manifest records the platform, the guest (id, kind, name, host) and its configuration.

## Adding a platform

A platform lives in `src/antigrafa/platforms/<name>/` and provides:

- `parse_settings(name, data)`: validates its `[source.<name>]` section;
- `create(name, settings)`: returns a `Platform` (see `platforms/base.py`) that lists
  the guests, plans a backup (one `DiskSource` per disk, before any snapshot), creates
  and deletes the snapshot, and optionally reports the host's iowait.

Then register its `type` in `PLATFORMS` (`platforms/__init__.py`). The Proxmox VE
implementation (`platforms/proxmox/`) is the reference.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                        # tests + coverage (coverage.xml)
.venv/bin/bandit -c pyproject.toml -r src   # security analysis
```

A full SonarQube analysis (tests, bandit, quality and security) runs in a throwaway
Docker instance: see [tools/sonarqube/README.md](tools/sonarqube/README.md).

## Roadmap

See [ROADMAP.md](ROADMAP.md).
