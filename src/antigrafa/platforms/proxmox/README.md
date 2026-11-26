# Antigrafa for Proxmox VE

Back to the [main README](../../../../README.md).

## Installation

Antigrafa talks to Proxmox in two ways: the **API** (to list guests and take snapshots)
and **SSH** (to read the disks).

1. On a Proxmox node, create a user and an API token with the rights Antigrafa needs:

   ```sh
   pveum user add antigrafa@pve
   pveum user token add antigrafa@pve backup      # prints the secret once: keep it
   pveum role add AntigrafaBackup --privs "VM.Audit VM.Snapshot Sys.Audit Datastore.Audit"
   pveum acl modify / --roles AntigrafaBackup --tokens 'antigrafa@pve!backup'
   ```

2. On the Antigrafa machine:

   | File | Content |
   |---|---|
   | `/etc/antigrafa/token` | the token secret |
   | `/etc/antigrafa/pve-root-ca.pem` | a copy of `/etc/pve/pve-root-ca.pem` from a node |

3. On every node, add the Antigrafa public key (`/etc/antigrafa/id_ed25519.pub`) to
   `/root/.ssh/authorized_keys`.

## Configuration

Fill in the `[source.cluster1]` section of
[antigrafa.example.toml](../../../../antigrafa.example.toml). Each option is explained there.

## Usage

Put a backup back into Proxmox:

```sh
antigrafa restore-guest --target nas cluster1/vm/100/2026-11-17T22-30-00Z
```

By default, the guest goes back to its original id, node and storage.
`antigrafa restore-guest --help` lists the options to change that.
