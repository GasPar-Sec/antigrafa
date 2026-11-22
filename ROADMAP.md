# Roadmap

## Reliability (I/O errors)

- [ ] Quarantine corrupted chunks: `verify` renames them to `.bad`, and a backup rewrites
      an existing chunk that is empty or has an invalid header
- [ ] Per-disk timeout and stall detection: kill the read, delete the snapshot and stop the
      batch (a `hard` NFS mount can otherwise hang forever)
- [ ] Stop the whole batch on the first destination error (`ENOSPC`, `EIO`, `ESTALE`)
      instead of snapshotting the remaining guests
- [ ] Bounded retries for transient errors (SSH, Proxmox API)
- [ ] Keep the original error when snapshot deletion also fails
- [ ] Clean error messages in `restore`/`verify`; remove the partial output file of a
      failed `restore`

## Configurable chunk size

The size is currently a single global setting (`chunk_size_mib`, 4 MiB). Each index
already records its own chunk size, so restoring works with mixed sizes. However, chunks
of different sizes never deduplicate against each other.

### Step 1: choose the chunk size

- [ ] Set the chunk size per target, with an optional per-guest override
- [ ] Validate the value (power of two, e.g. 64 KiB to 64 MiB) and record it in the manifest
- [ ] Warn when a guest is backed up with a different size than its previous backup
      (deduplication with older backups is lost)
- [ ] Document the trade-off: smaller chunks deduplicate better but create more files and
      a larger index; larger chunks are faster on slow or high-latency destinations

### Step 2: convert existing data to the new chunk size

- [ ] `antigrafa rechunk` command: rebuild the stream from an existing index, cut it at the
      new size, write the new chunks and a new index
- [ ] Atomic swap: keep the old index until the new one is written and verified
- [ ] Scope: one backup, one guest or a whole target; resumable after interruption
- [ ] Throttled like a backup (uses the destination `WriteController`)
- [ ] Let garbage collection remove the old chunks once no index references them
      (depends on retention / GC)

## Backup speed

Today every backup reads each disk in full on the source host. With `remote_hash`, only
changed chunks cross the network, but the whole snapshot is still read and hashed. Before
picking an item, measure a real run (phase timings and per-disk rate in the report, `-v`):
the bottleneck can be the read on the node, the network or the destination.

### Step 1: cheap wins (no repository format change)

- [ ] All-zero chunks detected on the source host: send a short record instead of the
      data (first backups and thin disks: unallocated space no longer crosses SSH, and is
      no longer hashed and compressed at the destination)
- [ ] Read the disks of one guest in parallel, with a limit (disks are read one after the
      other today)
- [ ] Hash in parallel in the script on the source host (`hashlib` releases the GIL): the
      single-threaded hash at low priority can cap the rate on a busy node
- [ ] Containers: read their files without filling the node's page cache (`tar` reads
      them buffered; disks are already read with `O_DIRECT`)
- [ ] Revisit the default `max_parallel_per_host = 1` (one guest at a time per node), now
      that the iowait throttle protects the guests

### Step 2: native incrementals (read only the changed blocks)

- [x] Keep the last Antigrafa snapshot on the source as the base of the next backup
      (`incremental`, ZFS volumes; costs space on the source storage)
- [ ] Ceph RBD: `rbd diff --from-snap` gives the changed extents directly
- [x] ZFS zvol: changed blocks from `zfs send -i` (read by `zstream dump -v`)
- [ ] LVM-thin: changed blocks from `thin_delta`
- [x] Fall back to a full read when the base snapshot is missing or does not match the
      previous backup
- [ ] A command to remove the bases of a guest, before a Proxmox rollback to an older
      snapshot (refused while a base exists)

## Features

- [x] Chunks hashed on the source host (`remote_hash`): only changed chunks cross the network
- [x] Storage snapshots (`snapshot_mode = "storage"`): guests with a disk on a storage
      without snapshots (flagged `backup=0`) can be backed up
- [x] VMs backed up by QEMU (`snapshot_mode = "qemu"`, image fleecing): no storage
      snapshot, no downtime, any storage (iSCSI, LVM thick, raw and qcow2 files)
- [ ] `"qemu"` mode: keep a QEMU dirty bitmap between backups and read only the
      changed blocks (as PBS does, while the VM is not restarted)
- [ ] `"qemu"` mode: stop the backup when `fleecing_dir` runs low on space, before the
      VM's writes fail
- [ ] `"qemu"` mode: containers on storages without snapshots (copy, then a short
      suspend, like vzdump's `suspend` mode)
- [x] `snapshot_mode` per guest (`[source.<name>.guests.<id>]`), and `"auto"`: the best
      way for each guest (Proxmox snapshot, storage snapshot, then QEMU)
- [ ] Storage snapshots: lock the guest in Proxmox during the backup (`lock=backup`, like
      vzdump), so that a disk cannot be moved or deleted meanwhile
- [ ] Repair: `verify` removes a damaged chunk and the next backup sends it again (with
      `remote_hash`, a chunk unchanged on the source is never sent again, so a chunk
      damaged at the destination stays damaged until then)
- [ ] Retention (prune) and garbage collection of orphaned chunks
- [ ] Content-defined chunking (CDC) for container tar archives
- [ ] qcow2 (through `qemu-nbd` on the snapshot)
- [ ] Automatic abort when the snapshot grows too large on the source storage
- [x] Web interface (HTTPS, login): start, follow and cancel backups
- [x] Web interface: list the backups and restore a guest (new, replace, or disks only)
- [ ] Web interface: limit login attempts, keep the run history across restarts, schedules
- [ ] Scheduling, notifications, client-side encryption
- [x] Direct restore to Proxmox (`restore-guest`): VM disks to ZFS, LVM-thin, LVM and
      directory storages, containers to ZFS
- [ ] Restore: containers to LVM, LVM-thin and directory storages (file system created,
      then extracted), VM disks to Ceph RBD
- [ ] Restore checked against a real Proxmox VE (optional test, like `test_zfs_live.py`)
- [ ] Replication between 2 antigrafa servers

## Validation

- [ ] Test against a real Proxmox cluster (ZFS, Ceph RBD, LVM-thin)
- [ ] Confirm the minimum API token permissions
