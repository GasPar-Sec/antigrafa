# SonarQube (local, throwaway)

[SonarQube Community Build](https://www.sonarsource.com/products/sonarqube/) checks code quality: bugs, security flaws, duplicated code, test coverage.

`scan.py` creates a **fresh** SonarQube instance in Docker for each analysis, then **removes it entirely**. Nothing to install or configure, no password to remember. It works the same way on Windows (Docker Desktop + WSL2) and on Linux.

## Running an analysis

Requirements: Docker running, and the project venv activated with the dev tools installed (`pip install -e '.[dev]'`).

```sh
python tools/sonarqube/scan.py          # analyze, print a summary, then remove everything
python tools/sonarqube/scan.py --keep   # same, but keep the web UI open until Enter
```

It can be run from any directory. It exits with code **0** if the Quality Gate passes, **1** otherwise.

## What the script does

1. **Tests**: runs `pytest`, which writes `coverage.xml`. If a test fails, the script stops there.
2. **Security**: runs `bandit` on `src/`, which writes `bandit-report.json`.
3. **Update**: pulls the latest `sonarqube:community` and `sonar-scanner-cli:latest` images and prints "up to date" or "NEW VERSION".
4. **Starts** a fresh SonarQube:
   - on `127.0.0.1` only, on a random port (no conflict possible);
   - with a random admin password and an analysis token created for the occasion;
   - with the project Quality Gate (see below).
5. **Analyzes** the code with the `scanner` container (the repository is mounted read-only).
6. **Summary** in the terminal: Quality Gate verdict, metrics, list of issues with file and line, Security Hotspots to review.
7. **Removes** the containers, network and volumes. If an image was updated, the old version is removed too.

Removal happens **in every case**: success, failure, error or Ctrl+C. If an analysis was killed abruptly, the leftover instance is removed on the next run.

## The Quality Gate

SonarQube's default Quality Gate ("Sonar way") only judges **new code**. A fresh instance always runs its first analysis, so no code is "new" and the gate would always pass, even with a flaw. The script therefore creates its own gate, `antigrafa`, which applies the same thresholds to **all the code**:

| Condition | Threshold |
|---|---|
| Open issues (bugs, vulnerabilities, code smells, including bandit's) | 0 |
| Security Hotspots reviewed | 100 % |
| Test coverage | ≥ 80 % |
| Duplicated lines | ≤ 3 % |

The thresholds are in `QUALITY_GATE_CONDITIONS`, at the top of `scan.py`.

## Good to know

- **Coverage depends on the Python version**: `compress.py` uses the standard library's zstd on Python 3.14+ and the `zstandard` package before, so only one of the two branches is covered by a given interpreter.
- **No history**: each analysis starts from scratch. The terminal summary is the result. To explore in detail in the web UI, use `--keep`.
- **Duration**: about 3 to 4 minutes, 1 to 2 of which to start SonarQube.
- **Embedded database**: SonarQube uses its embedded H2 database instead of PostgreSQL. It is not recommended for a permanent instance, but fine for a throwaway one.
- **Offline**: if pulling the images fails, the script carries on with the images already present.
- **`SONAR_ES_BOOTSTRAP_CHECKS_DISABLE`**: lets SonarQube start without tuning the system (`vm.max_map_count`). Acceptable for a local throwaway instance, not for a real server.
