"""SonarQube analysis of antigrafa in a throwaway instance.

Each run:
  1. runs the tests (pytest -> coverage.xml) and bandit (-> bandit-report.json);
  2. pulls the latest Docker images (SonarQube and the scanner);
  3. starts fresh SonarQube containers with a random password and token;
  4. analyzes the code and prints the result (Quality Gate, metrics, issues);
  5. removes everything: containers, network, volumes (and the old image if it was replaced).

Usage (project venv activated, from any directory):
    python tools/sonarqube/scan.py           # analyze, then remove the instance
    python tools/sonarqube/scan.py --keep    # keep the web UI open until Enter is pressed

Exit code: 0 if the Quality Gate passes, 1 otherwise.
Works the same way on Windows and Linux (standard library only).
"""

import argparse
import base64
import importlib.util
import json
import os
import secrets
import shutil

# subprocess is needed to drive Docker (see run())
import subprocess  # nosec B404
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = Path(__file__).resolve().with_name("docker-compose.yml")
PROJECT_PREFIX = "antigrafa-sonar-"

# Quality Gate applied to ALL the code.
# The default gate ("Sonar way") only judges "new code": on a fresh instance this is
# always the first analysis, so no code is new and it would always pass. We keep its
# thresholds, but apply them to the whole code base.
QUALITY_GATE_NAME = "antigrafa"
QUALITY_GATE_CONDITIONS = [
    # (metric, operator, threshold): the gate fails if "metric operator threshold"
    ("violations", "GT", "0"),  # no open issue (bug, vulnerability, code smell)
    ("security_hotspots_reviewed", "LT", "100"),  # every Security Hotspot reviewed
    ("coverage", "LT", "80"),  # at least 80 % test coverage
    ("duplicated_lines_density", "GT", "3"),  # at most 3 % duplicated lines
]


# --------------------------------------------------------------------------- helpers


def step(message: str) -> None:
    print(f"\n==> {message}", flush=True)


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command (argument list, never through a shell)."""
    # Fixed commands written in this script, no shell and no external input
    return subprocess.run(command, check=False, **kwargs)  # nosec B603


def compose(project: str, *args: str, **kwargs) -> subprocess.CompletedProcess:
    base = [
        "docker",
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "-p",
        project,
        "--profile",
        "scan",
    ]
    return run([*base, *args], **kwargs)


def read_project_key() -> str:
    for line in (ROOT / "sonar-project.properties").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("sonar.projectKey="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("sonar.projectKey not found in sonar-project.properties")


class SonarApi:
    """Small client for the SonarQube web API."""

    def __init__(self, base_url: str, login: str, password: str) -> None:
        self.base_url = base_url
        self.password = password
        credentials = base64.b64encode(f"{login}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {credentials}"}

    def call(self, method: str, path: str, **params: str) -> dict:
        query = urllib.parse.urlencode(params)
        url = f"{self.base_url}{path}"
        data = None
        if method == "GET" and query:
            url = f"{url}?{query}"
        elif query:
            data = query.encode()
        request = urllib.request.Request(url, data=data, headers=self.headers, method=method)
        # URL built from 127.0.0.1 only: never an external address
        with urllib.request.urlopen(request, timeout=30) as response:  # nosec B310
            body = response.read()
        return json.loads(body) if body else {}


# --------------------------------------------------------------------------- steps


def find_docker() -> bool:
    """Find docker, even when a terminal opened before installing it has a stale PATH."""
    if shutil.which("docker"):
        return True
    if os.name != "nt":
        return False
    # Docker Desktop install folders on Windows (per user, then machine-wide)
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs/DockerDesktop/resources/bin",
        Path(os.environ.get("PROGRAMFILES", "")) / "Docker/Docker/resources/bin",
    ]
    for folder in candidates:
        if (folder / "docker.exe").is_file():
            # Added to this script's PATH only: docker and its helpers (including the
            # credential helper) are then found by the subcommands
            os.environ["PATH"] = f"{folder}{os.pathsep}{os.environ.get('PATH', '')}"
            print(f"Docker found in {folder} (missing from this terminal's PATH:")
            print("  reopen your terminal or VS Code so it is found directly)")
            return True
    return False


def check_prerequisites() -> None:
    if not find_docker():
        raise SystemExit("Docker not found. Install Docker Desktop (or Docker on Linux).")
    if run(["docker", "info"], capture_output=True).returncode != 0:
        raise SystemExit("Docker is not responding. Start Docker Desktop and try again.")
    for module in ("pytest", "pytest_cov", "bandit"):
        if importlib.util.find_spec(module) is None:
            raise SystemExit(
                f"Module '{module}' not found: activate the project venv first\n"
                "  Windows: .venv\\Scripts\\activate\n"
                "  Linux:   source .venv/bin/activate\n"
                "then install the dev tools: pip install -e '.[dev]'"
            )


def remove_leftovers() -> None:
    """Remove instances left behind by a previous run that was killed."""
    result = run(["docker", "compose", "ls", "--all", "--format", "json"], capture_output=True)
    projects = json.loads(result.stdout or "[]") if result.returncode == 0 else []
    for project in projects:
        if project["Name"].startswith(PROJECT_PREFIX):
            print(f"Removing a forgotten instance: {project['Name']}")
            compose(project["Name"], "down", "-v", "--remove-orphans", capture_output=True)


def generate_reports() -> bool:
    step("Tests (pytest + coverage)")
    if run([sys.executable, "-m", "pytest"], cwd=ROOT).returncode != 0:
        print("\nTests are failing: fix them before running the SonarQube analysis.")
        return False

    step("Security (bandit -> bandit-report.json)")
    bandit = [sys.executable, "-m", "bandit", "-q", "-c", "pyproject.toml", "-r", "src"]
    report = ["-f", "json", "-o", "bandit-report.json", "--exit-zero"]
    run([*bandit, *report], cwd=ROOT)
    return True


def image_ids(images: list[str]) -> dict[str, str | None]:
    ids = {}
    for image in images:
        result = run(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image],
            capture_output=True,
        )
        ids[image] = result.stdout.decode().strip() if result.returncode == 0 else None
    return ids


def update_images(project: str) -> dict[str, str]:
    """Pull the latest images. Return the old images that were replaced."""
    step("Checking for new Docker image versions")
    listing = compose(project, "config", "--images", capture_output=True)
    images = listing.stdout.decode().split()
    before = image_ids(images)

    if compose(project, "pull", "--quiet").returncode != 0:
        if None in before.values():
            raise SystemExit("Pull failed and images are missing: check your Internet access.")
        print("Pull failed (offline?): using the images already present.")
        return {}

    after = image_ids(images)
    replaced = {}
    for image in images:
        if before[image] is None:
            print(f"  {image}: downloaded")
        elif before[image] != after[image]:
            print(f"  {image}: NEW VERSION downloaded")
            replaced[image] = before[image]
        else:
            print(f"  {image}: up to date")
    return replaced


def start_sonarqube(project: str) -> SonarApi:
    step("Starting a fresh SonarQube (1 to 2 minutes)")
    started = compose(project, "up", "-d", "--wait", "--wait-timeout", "300", "sonarqube")
    if started.returncode != 0:
        raise SystemExit("SonarQube did not start correctly.")

    address = compose(project, "port", "sonarqube", "9000", capture_output=True)
    base_url = f"http://{address.stdout.decode().strip()}"

    # Fresh instance: replace the default password with a random one
    password = f"Ag-{secrets.token_urlsafe(18)}-9a!"
    SonarApi(base_url, "admin", "admin").call(
        "POST",
        "/api/users/change_password",
        login="admin",
        previousPassword="admin",
        password=password,
    )
    api = SonarApi(base_url, "admin", password)

    version = urllib.request.urlopen(f"{base_url}/api/server/version", timeout=10)  # nosec B310
    print(f"SonarQube {version.read().decode()} ready at {base_url}")
    return api


def configure_quality_gate(api: SonarApi) -> None:
    """Create the project Quality Gate and make it the default."""
    api.call("POST", "/api/qualitygates/create", name=QUALITY_GATE_NAME)
    for metric, operator, threshold in QUALITY_GATE_CONDITIONS:
        api.call(
            "POST",
            "/api/qualitygates/create_condition",
            gateName=QUALITY_GATE_NAME,
            metric=metric,
            op=operator,
            error=threshold,
        )
    api.call("POST", "/api/qualitygates/set_as_default", name=QUALITY_GATE_NAME)
    print(f"Quality Gate '{QUALITY_GATE_NAME}': {len(QUALITY_GATE_CONDITIONS)} conditions")


def analyze(project: str, api: SonarApi) -> int:
    step("Analyzing the code (scanner)")
    token = api.call("POST", "/api/user_tokens/generate", name="scan")["token"]
    env = {**os.environ, "SONAR_TOKEN": token}
    return compose(project, "run", "--rm", "scanner", env=env).returncode


def print_summary(api: SonarApi, project_key: str) -> bool:
    step("Result")
    status = api.call("GET", "/api/qualitygates/project_status", projectKey=project_key)
    gate = status["projectStatus"]
    passed = gate["status"] == "OK"
    print(f"Quality Gate: {'PASSED (green)' if passed else 'FAILED (red)'}")
    for condition in gate.get("conditions", []):
        if condition["status"] != "OK":
            print(
                f"  - {condition['metricKey']}: {condition.get('actualValue', '?')} "
                f"(threshold {condition['comparator']} {condition['errorThreshold']})"
            )

    metrics = ",".join(
        [
            "ncloc",
            "coverage",
            "bugs",
            "vulnerabilities",
            "code_smells",
            "security_hotspots",
            "duplicated_lines_density",
        ]
    )
    measures = api.call("GET", "/api/measures/component", component=project_key, metricKeys=metrics)
    print("\nMetrics:")
    for measure in sorted(measures["component"]["measures"], key=lambda m: m["metric"]):
        print(f"  {measure['metric']:<26} {measure['value']}")

    issues = api.call(
        "GET",
        "/api/issues/search",
        componentKeys=project_key,
        resolved="false",
        ps="100",
    )
    print(f"\nOpen issues: {issues['total']}")
    for issue in issues["issues"]:
        location = issue["component"].removeprefix(f"{project_key}:")
        print(f"  [{issue['severity']}] {location}:{issue.get('line', '-')}  {issue['message']}")
        print(f"      rule {issue['rule']}")

    try:
        hotspots = api.call("GET", "/api/hotspots/search", project=project_key, status="TO_REVIEW")
    except urllib.error.HTTPError as error:
        # The Quality Gate above remains the reference: do not abort the summary
        print(f"\n(Security Hotspots list unavailable: HTTP {error.code})")
        return passed
    if hotspots["paging"]["total"]:
        print(f"\nSecurity Hotspots to review: {hotspots['paging']['total']}")
        for hotspot in hotspots["hotspots"]:
            location = hotspot["component"].removeprefix(f"{project_key}:")
            print(f"  {location}:{hotspot.get('line', '-')}  {hotspot['message']}")
    return passed


def clean_up(project: str, replaced_images: dict[str, str]) -> None:
    step("Removing the instance (containers, network, volumes)")
    compose(project, "down", "-v", "--remove-orphans", capture_output=True)
    for image, old_id in replaced_images.items():
        # Fails harmlessly if the old image is still used elsewhere
        if run(["docker", "image", "rm", old_id], capture_output=True).returncode == 0:
            print(f"Old version of {image} removed")
    print("Done: no container left.")


# --------------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description="Throwaway SonarQube analysis of antigrafa.")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the web UI open after the analysis, until you press Enter",
    )
    args = parser.parse_args()
    sys.stdout.reconfigure(errors="replace")

    started_at = time.monotonic()
    check_prerequisites()
    remove_leftovers()
    if not generate_reports():
        return 1

    project = f"{PROJECT_PREFIX}{datetime.now():%Y%m%d-%H%M%S}"
    replaced_images: dict[str, str] = {}
    try:
        replaced_images = update_images(project)
        api = start_sonarqube(project)
        configure_quality_gate(api)
        scanner_code = analyze(project, api)
        # Exit code 3 = Quality Gate failed: still print the summary
        if scanner_code not in (0, 3):
            print(f"\nThe scanner failed (code {scanner_code}): see the log above.")
            return 1
        passed = print_summary(api, read_project_key())

        if args.keep:
            print(f"\nWeb UI: {api.base_url}  (admin / {api.password})")
            try:
                input("Press Enter to remove everything... ")
            except EOFError:  # no keyboard (redirected input): carry on
                print()
        return 0 if passed else 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    finally:
        clean_up(project, replaced_images)
        print(f"Total time: {time.monotonic() - started_at:.0f} s")


if __name__ == "__main__":
    sys.exit(main())
