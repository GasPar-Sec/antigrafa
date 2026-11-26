"""The version is the same everywhere it appears.

``pyproject.toml`` holds it; ``antigrafa.__version__`` repeats it (backup manifests,
the web page); the README badge reads it from ``pyproject.toml`` itself, so the README
never writes a version by hand. Run on its own by .github/workflows/version.yml.
"""

import ast
import re
import tomllib
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
# A version written in a text: 1.2.3 or v1.2.3, not part of an address (10.0.0.11)
WRITTEN_VERSION = re.compile(r"(?<![\w.])v?(\d+\.\d+\.\d+)(?![\w.]*\d)")


def _pyproject_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def _init_version() -> str:
    """``__version__`` read from the source, without importing the package."""
    tree = ast.parse((ROOT / "src" / "antigrafa" / "__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "__version__" for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("__version__ not found in src/antigrafa/__init__.py")


def test_version_is_major_minor_patch():
    assert SEMVER.match(_pyproject_version()), _pyproject_version()


def test_init_has_the_pyproject_version():
    assert _init_version() == _pyproject_version()


def test_readme_badge_reads_the_pyproject_version():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    badge = re.search(r"\[!\[Version\]\((?P<image>[^)]+)\)\]\((?P<link>[^)]+)\)", readme)
    assert badge, "README.md: the Version badge is missing"
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(badge["image"]).query)
    assert query["url"][0].endswith("/pyproject.toml") and query["query"] == ["$.project.version"]
    assert badge["link"] == "pyproject.toml"


def test_readme_writes_no_other_version():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    written = set(WRITTEN_VERSION.findall(readme))
    assert written <= {_pyproject_version()}, f"README.md: versions written by hand: {sorted(written)}"
