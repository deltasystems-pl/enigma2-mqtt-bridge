"""The control template must stay packageable.

A missing field or a stray architecture is the kind of mistake that only shows
up when a feed rejects the package or a receiver refuses to install it, long
after the commit. The template is cheap to assert here instead.
"""

import io
import os
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTROL = REPO_ROOT / "CONTROL" / "control"
MAINTAINER_SCRIPTS = ("postinst", "prerm")
WEBIF_SHIM = (
    REPO_ROOT / "src" / "WebInterface" / "WebChilds" / "External" / "MQTTBridge.py"
)

REQUIRED_FIELDS = (
    "Package",
    "Version",
    "Architecture",
    "Section",
    "Priority",
    "Depends",
    "Maintainer",
    "Homepage",
    "License",
    "Description",
)


def read_control_fields():
    fields = {}
    key = None
    for line in CONTROL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        if line[0] in " \t" and key is not None:
            fields[key] += "\n" + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip()
    return fields


def test_all_required_fields_present():
    fields = read_control_fields()
    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    assert not missing, f"CONTROL/control is missing: {', '.join(missing)}"


def test_package_name_and_architecture():
    fields = read_control_fields()
    assert fields["Package"] == "enigma2-plugin-extensions-mqttbridge"
    # Pure Python: one package installs on every receiver.
    assert fields["Architecture"] == "all"
    # The code is GPL-2.0-or-later; the package also carries the menu's Apache-2.0 icons, which
    # GPLv2 cannot take, so the package is distributed under GPLv3 or later (NOTICE).
    assert fields["License"] == "GPL-3.0-or-later AND Apache-2.0"


def test_version_is_a_build_placeholder():
    # The build substitutes this from version.py; a literal version here would
    # silently go stale.
    assert read_control_fields()["Version"] == "@VERSION@"


def test_depends_are_image_packages():
    depends = [d.strip() for d in read_control_fields()["Depends"].split(",")]
    assert "python3-core" in depends
    # Anything not in an image feed would have to be vendored instead.
    assert all(d.startswith("python3-") for d in depends), depends


def test_description_has_a_synopsis_and_a_body():
    description = read_control_fields()["Description"]
    synopsis, _, body = description.partition("\n")
    assert synopsis.strip(), "the first Description line is the synopsis"
    assert body.strip(), "the extended description is what a plugin browser shows"


def test_maintainer_scripts_are_executable_in_the_checkout():
    """A maintainer script opkg cannot execute is a maintainer script that never runs.

    The mode is carried by git, and it is the only thing between `postinst` and
    being a file opkg unpacks and ignores - which on an upgrade would mean the
    orphaned-bytecode sweep silently not happening.
    """
    for name in MAINTAINER_SCRIPTS:
        script = REPO_ROOT / "CONTROL" / name
        assert script.is_file(), name
        assert script.stat().st_mode & stat.S_IXUSR, f"CONTROL/{name} is not executable"


def test_maintainer_scripts_are_packaged_and_their_mode_is_asserted_from_the_archive():
    build = (REPO_ROOT / "tools" / "build-ipk.sh").read_text(encoding="utf-8")
    assert "cp CONTROL/postinst CONTROL/prerm" in build
    # Staged with an explicit mode, then read back out of the built archive:
    # the check that survives a change of staging filesystem.
    assert 'chmod 755 "$STAGE/control/postinst" "$STAGE/control/prerm"' in build
    assert "for script in ./postinst ./prerm; do" in build


LICENCE_FILES = ("LICENSE", "LICENSE-GPL-3.0.txt", "NOTICE")


def test_the_licence_texts_and_the_notice_are_packaged_beside_the_plugin():
    """The package says GPL-3.0-or-later AND Apache-2.0: it carries both texts and the notice."""
    for name in LICENCE_FILES:
        assert (REPO_ROOT / name).is_file(), name
    for name in ("LICENSE-Apache-2.0.txt", "NOTICE"):
        assert (REPO_ROOT / "src" / "MQTTBridge" / "icons" / name).is_file(), name
    build = (REPO_ROOT / "tools" / "build-ipk.sh").read_text(encoding="utf-8")
    assert 'cp LICENSE LICENSE-GPL-3.0.txt NOTICE "$STAGE/data/$PLUGIN_DIR/"' in build
    # And read back out of the built archive, where a build cannot pass without them.
    assert ("for licence in LICENSE LICENSE-GPL-3.0.txt NOTICE icons/LICENSE-Apache-2.0.txt "
            "icons/NOTICE; do") in build


def test_the_built_package_carries_the_licence_files(tmp_path):
    """The whole build, on a copy of this tree, where the build tools are installed."""
    missing = [tool for tool in ("bash", "msgfmt", "ar", "tar", "gzip")
               if shutil.which(tool) is None]
    if missing:
        if os.environ.get("MQTTBRIDGE_REQUIRE_BUILD_TOOLS"):
            pytest.fail("the package build needs " + ", ".join(missing))
        pytest.skip("the package build needs " + ", ".join(missing))
    tree = tmp_path / "tree"
    shutil.copytree(REPO_ROOT, tree, ignore=shutil.ignore_patterns(
        ".git", "dist", "__pycache__", ".venv", "venv", "*.pyc"))
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["SOURCE_DATE_EPOCH"] = "1790000000"
    result = subprocess.run(["bash", "tools/build-ipk.sh", "--allow-unreleased"], cwd=tree,
                            capture_output=True, text=True, env=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    (ipk,) = (tree / "dist").glob("*.ipk")
    members = subprocess.run(["ar", "p", str(ipk), "data.tar.gz"], capture_output=True,
                             check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(members), mode="r:gz") as data:
        names = set(data.getnames())
    plugin = "./usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/"
    for name in LICENCE_FILES:
        assert plugin + name in names, name
        packaged = data_text(members, plugin + name)
        assert packaged == (REPO_ROOT / name).read_text(encoding="utf-8")
    assert plugin + "icons/LICENSE-Apache-2.0.txt" in names


def data_text(data_tar_gz, member):
    with tarfile.open(fileobj=io.BytesIO(data_tar_gz), mode="r:gz") as data:
        return data.extractfile(member).read().decode("utf-8")


def test_optional_openwebif_shim_is_packaged_from_its_unique_external_path():
    build = (REPO_ROOT / "tools" / "build-ipk.sh").read_text(encoding="utf-8")
    assert WEBIF_SHIM.is_file()
    assert "src/WebInterface/WebChilds/External/MQTTBridge.py" in build
    assert "Plugins/Extensions/WebInterface/WebChilds/External" in build
