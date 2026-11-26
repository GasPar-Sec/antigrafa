# Antigrafa

[![Version](https://img.shields.io/badge/dynamic/toml?url=https%3A%2F%2Fraw.githubusercontent.com%2FGasPar-Sec%2Fantigrafa%2Frelease%2Fpyproject.toml&query=%24.project.version&label=version&prefix=v)](pyproject.toml)
[![License](https://img.shields.io/github/license/GasPar-Sec/antigrafa)](LICENSE)

Antigrafa backs up virtual machines and containers from a separate machine. It takes a
snapshot, then copies it slowly enough that **the guests never slow down**.

Supported: [Proxmox VE](src/antigrafa/platforms/proxmox/README.md). Planned: VMware, Hyper-V.

## Contents

- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Development](#development)

## Installation

On Linux with Python 3.11 or newer:

```sh
sudo deploy/install.sh
sudo systemctl start antigrafa-scheduler antigrafa-web
```

## Configuration

Edit `/etc/antigrafa/antigrafa.toml`. Each option is explained in [antigrafa.example.toml](antigrafa.example.toml).

| Section | What it sets |
|---|---|
| `[source.<name>]` | what to back up |
| `[target.<name>]` | where backups go |
| `[job.<name>]` | scheduled backups |

## Usage

```sh
antigrafa guests                        # list the VMs and containers
antigrafa backup --all --target nas     # back up everything
antigrafa list --target nas             # list the backups
antigrafa verify --target nas           # check the backups are readable
antigrafa restore-guest --target nas <backup>
antigrafa job list                      # scheduled backups
```

Add `-v` for more details, `-q` for errors only.

Web page: `antigrafa web adduser admin`, then open `https://<machine>:8443/`.

## Development

```sh
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

- New platform: [platforms/README.md](src/antigrafa/platforms/README.md)
- Code analysis: [tools/sonarqube/README.md](tools/sonarqube/README.md)
- Known limits: [ROADMAP.md](ROADMAP.md)
