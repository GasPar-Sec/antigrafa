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

## Features

- [ ] Native incrementals (`zfs send -i`, `rbd export-diff`) to read only changed blocks
- [ ] Retention (prune) and garbage collection of orphaned chunks
- [ ] Content-defined chunking (CDC) for container tar archives
- [ ] qcow2 (through `qemu-nbd` on the snapshot)
- [ ] Automatic abort when the snapshot grows too large on the source storage
- [ ] Scheduling, notifications, client-side encryption
- [ ] Direct restore to Proxmox
- [ ] Replication between 2 antigrafa servers

## Validation

- [ ] Test against a real Proxmox cluster (ZFS, Ceph RBD, LVM-thin)
- [ ] Confirm the minimum API token permissions
