# Antigrafa

> [!WARNING]
> **Work in progress — not ready for production.** Antigrafa is under active development
> and has not yet been tested against a real cluster. Do not rely on it as your only
> backup. See [ROADMAP.md](ROADMAP.md) for known limitations.

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
   only the backup slows down: the guest is never in the loop.
3. **Cleanup**: the snapshot is deleted in every case, success or failure.

The trade-off: the snapshot takes space on the source storage for as long as the
transfer lasts.

## Adaptive control

| Side | Measured | Adjusted |
|---|---|---|
| Destination | chunk write latency | number of parallel writes (AIMD: +1 when it keeps up, ×0.7 when it saturates) |
| Destination | write time vs. compression time | zstd level: compress harder when the disk is slow, less when the CPU is the bottleneck |
| Source | iowait of the host running the guest (when the platform reports it) | read rate cap: halved when iowait rises, +25% when it drops |
| Source | — | reads run at low priority (`ionice -c2 -n7 nice -n10` on Proxmox VE) |

The destination is shared: a single controller drives every concurrent backup that
writes to it.

## Proxmox VE

### Why not just PBS?

With Proxmox Backup Server, a VM backup runs inside QEMU using *copy-before-write*:
when the VM writes to a block that has not been backed up yet, QEMU must first send
the old content to PBS. If the destination is slow, **the VM's writes wait for the
destination** and the VM stops responding. *Fleecing* (Proxmox 8.2+) mitigates this
without removing it. Antigrafa snapshots through the API instead (filesystems frozen
through the qemu-guest-agent when it is enabled) and reads the snapshot afterwards.

### Supported storage

| Proxmox storage | VM | Container |
|---|---|---|
| ZFS (`zfspool`) | zvol, raw image | `subvol-*` dataset, tar archive |
| Ceph RBD (hyperconverged or external) | `rbd export`, raw image | raw image |
| LVM-thin | `snap_<vol>_<snap>` snapshot, raw image | raw image |
| qcow2 on directory, NFS or CIFS | not yet | — |

Disks flagged `backup=0` are skipped. For containers, only `rootfs` and mount points
flagged `backup=1` are backed up, matching Proxmox's behaviour.

### Requirements

- an API token with at least `VM.Audit`, `VM.Snapshot` and `Sys.Audit`, plus read
  access to the storage configuration (`/storage`). These permissions still need to be
  validated on a real cluster;
- the cluster's CA certificate (`/etc/pve/pve-root-ca.pem`, copied from a node) for
  `ca_file`, so that the API certificate is verified. `verify_tls = false` skips the
  verification, for labs only: the API token can then be intercepted;
- an SSH key from the Antigrafa machine in `/root/.ssh/authorized_keys` on every node.

See the `[source.cluster1]` section of [antigrafa.example.toml](antigrafa.example.toml).

## Installation

```sh
python3 -m venv /opt/antigrafa && /opt/antigrafa/bin/pip install .
cp antigrafa.example.toml /etc/antigrafa/antigrafa.toml
```

Requires Python 3.11 or later. On Python 3.14+, zstd comes from the standard library
and the project has no dependencies.

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
