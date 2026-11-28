# Antigrafa

[![Version](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2FGasPar-Sec%2Fantigrafa%2Fmain%2Fpyproject.toml&query=%24.project.version&label=version&prefix=v)](pyproject.toml)
[![License](https://img.shields.io/github/license/GasPar-Sec/antigrafa)](LICENSE)

Antigrafa backs up virtual machines (VMs) and containers. It runs on its own machine,
not on the servers it backs up. It first freezes the disks with a snapshot, then copies
them slowly enough that **the VMs never slow down**.

| Platform | Status |
|---|---|
| [Proxmox VE](src/antigrafa/platforms/proxmox/README.md) | supported |
| VMware, Hyper-V | planned |

## Installation

On Linux, with Python 3.11 or newer:

```sh
sudo deploy/install.sh
```

The script installs Antigrafa, creates an SSH key and a default configuration, then prints
the next steps. Follow~ them, then start the services:

```sh
sudo systemctl start antigrafa-scheduler antigrafa-web
```

## Configuration

Edit `/etc/antigrafa/antigrafa.toml`. Every option is explained in the file itself
(a copy of [antigrafa.example.toml](antigrafa.example.toml)).

| Section | What it sets |
|---|---|
| `[source.<name>]` | what to back up (a cluster) |
| `[target.<name>]` | where backups go (a folder, an NFS share) |
| `[job.<name>]` | backups that run on a schedule |

## Usage

```sh
antigrafa guests                        # list the VMs and containers
antigrafa backup --all --target nas     # back up everything now
antigrafa list --target nas             # list the backups
antigrafa verify --target nas           # check the backups can be read
antigrafa restore-guest --target nas <backup>   # <backup>: a name shown by 'list'
antigrafa job list                      # scheduled backups
```

- `-v`: more details, `-q`: errors only, `--help`: all the options of a command.
- Web page: create a user with `antigrafa web adduser admin`, then open `https://<machine>:8443/`.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

| Topic | Where |
|---|---|
| Add a platform | [platforms/README.md](src/antigrafa/platforms/README.md) |
| Code analysis | [tools/sonarqube/README.md](tools/sonarqube/README.md) |
| Known limits and plans | [ROADMAP.md](ROADMAP.md) |
