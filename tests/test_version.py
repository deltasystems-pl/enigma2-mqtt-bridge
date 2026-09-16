"""The version is single-sourced, and the build agrees with the import.

`version.py` is read two ways: Python imports it, and `tools/build-ipk.sh`
scrapes it with a regular expression. A refactor that keeps one of those working
and breaks the other would produce an IPK stamped with a version the plugin does
not report, so both paths are asserted here.
"""

import re
from pathlib import Path

from MQTTBridge.version import __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
VERSION_PY = REPO_ROOT / "src" / "MQTTBridge" / "version.py"

# The same expression tools/build-ipk.sh uses.
BUILD_REGEX = re.compile(r'^__version__\s*=\s*["\']([^"\']+)["\']', re.M)

# Semantic versioning, which is also a valid PEP 440 release: MAJOR.MINOR.PATCH
# with an optional pre-release and build metadata.
SEMVER = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)


def test_version_is_semver_shaped():
    assert SEMVER.match(__version__), f"not a semantic version: {__version__!r}"


def test_build_script_scrapes_the_same_version():
    source = VERSION_PY.read_text(encoding="utf-8")
    match = BUILD_REGEX.search(source)
    assert match, "tools/build-ipk.sh would find no __version__ in version.py"
    assert match.group(1) == __version__


def test_version_is_defined_only_once():
    source = VERSION_PY.read_text(encoding="utf-8")
    assert len(BUILD_REGEX.findall(source)) == 1
