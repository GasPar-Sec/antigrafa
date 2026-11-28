# SonarQube (local, throwaway)

[SonarQube](https://www.sonarsource.com/products/sonarqube/) checks code quality: bugs,
security flaws, duplicated code, test coverage. Back to the [main README](../../README.md).

`scan.py` starts a **new** SonarQube in Docker for each analysis, then **deletes it**.
Nothing to install or configure, no password to remember. Works on Linux and on Windows
(Docker Desktop + WSL2).

## Contents

- [Running an analysis](#running-an-analysis)
- [What the script does](#what-the-script-does)
- [The Quality Gate](#the-quality-gate)
- [Good to know](#good-to-know)

## Running an analysis

You need Docker running and the project venv with the dev tools (`pip install -e '.[dev]'`).

```sh
python tools/sonarqube/scan.py          # analyze, print a summary, delete everything
python tools/sonarqube/scan.py --keep   # same, but keep the web page open until Enter
```

| Exit code | Meaning |
|---|---|
| 0 | the Quality Gate passes |
| 1 | it fails |

## What the script does

1. Runs the tests (`pytest`, coverage) and the security check (`bandit`). Stops if a test fails.
2. Downloads the latest SonarQube images, if any.
3. Starts SonarQube on `127.0.0.1`, on a random port, with a random password.
4. Analyzes the code (read-only) and prints the result: verdict, issues with file and line.
5. Deletes the containers, network and volumes, **even on an error or Ctrl+C**.

## The Quality Gate

The Quality Gate is the pass/fail rule. SonarQube's default one only checks **new** code,
and on a fresh instance no code is new, so it would always pass. The script uses its own
gate, which checks **all** the code:

| Condition | Threshold |
|---|---|
| Open issues | 0 |
| Security Hotspots reviewed | 100 % |
| Test coverage | ≥ 80 % |
| Duplicated lines | ≤ 3 % |

The thresholds are in `QUALITY_GATE_CONDITIONS`, at the top of `scan.py`.

## Good to know

| Topic | Detail |
|---|---|
| Duration | 3 to 4 minutes, 1 to 2 of them to start SonarQube |
| History | none: each analysis starts from zero. Use `--keep` to browse the results |
| Offline | the script uses the images already downloaded |
| Coverage | `compress.py` has one branch for Python 3.14+ and one for older versions: one interpreter covers only one |
| Not for a server | embedded H2 database and `SONAR_ES_BOOTSTRAP_CHECKS_DISABLE`: fine for a throwaway instance only |
