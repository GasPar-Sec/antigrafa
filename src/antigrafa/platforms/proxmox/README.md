# Antigrafa for Proxmox VE

Back to the [main README](../../../../README.md).

## Contents

- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)

## Installation

1. Create an API token with the rights Antigrafa needs:

   ```sh
   pveum role add AntigrafaBackup --privs "VM.Audit VM.Snapshot Sys.Audit Datastore.Audit"
   pveum acl modify / --roles AntigrafaBackup --tokens 'antigrafa@pve!backup'
   ```

2. Copy `/etc/pve/pve-root-ca.pem` from a node to the Antigrafa machine.
3. Add the Antigrafa SSH key to `/root/.ssh/authorized_keys` on every node.

## Configuration

Fill in the `[source.cluster1]` section of [antigrafa.example.toml](../../../../antigrafa.example.toml): each option is explained there.

Storage that cannot take snapshots (LVM, iSCSI, qcow2) only works for VMs, with `snapshot_mode = "qemu"`.

## Usage

Put a backup back into Proxmox:

```sh
antigrafa restore-guest --target nas cluster1/vm/100/2026-11-17T22-30-00Z
```

| Option | What it does |
|---|---|
| `--id`, `--host`, `--storage` | where to restore (the original place by default) |
| `--overwrite` | replace the existing guest |
| `--disk-only` | add the disks to the existing guest, without changing it |
| `--stop`, `--start` | stop the guest before, start it after |
