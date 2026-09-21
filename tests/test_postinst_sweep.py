"""`postinst` clears out bytecode whose source the upgrade removed.

The image byte-compiles a plugin after opkg has installed it, so the `.pyc`
files in the plugin directory are not in opkg's file list and opkg will never
remove them. An upgrade that moves a module — `paho/` to `_vendor/paho/` — takes
the `.py` away and leaves the compiled copy, and a legacy-location `.pyc` with
no source beside it is still importable in Python 3. The old module survives the
upgrade that was meant to remove it.

These tests run the real maintainer script, under every POSIX shell on the
machine, against a fixture tree. `busybox sh` is the one that matters — it is
what a receiver has — and `dash` is the standard-issue one that catches a
bashism. The shell is the unit here: rewriting the sweep in Python and testing
that would test something the receiver never runs.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
POSTINST = REPO_ROOT / "CONTROL" / "postinst"

# A space in the path on purpose: an unquoted expansion in the script would
# split it, and every one of these tests would then be run against nothing.
PLUGIN_PARENT = "an installed plugin"

# The directory basename is not decoration. The script refuses to sweep anything
# not called MQTTBridge, so that the test-only environment variable cannot be
# turned into a way of pointing it at somebody else's files.
PLUGIN_NAME = "MQTTBridge"


def _shells():
    found = []
    for name, command in (
        ("busybox sh", [shutil.which("busybox") or "", "sh"]),
        ("dash", [shutil.which("dash") or ""]),
        ("sh", [shutil.which("sh") or "/bin/sh"]),
    ):
        if command[0] and Path(command[0]).exists():
            found.append(pytest.param(command, id=name))
    return found


SHELLS = _shells()


@pytest.fixture(params=SHELLS)
def shell(request):
    return request.param


def run(shell, plugin_dir):
    """The real postinst, with the plugin directory pointed at a fixture."""
    environment = dict(os.environ, MQTTBRIDGE_PLUGIN_DIR=str(plugin_dir))
    return subprocess.run(
        shell + [str(POSTINST)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
    )


def write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def plugin_dir(tmp_path):
    return tmp_path / PLUGIN_PARENT / PLUGIN_NAME


def test_there_is_a_shell_to_run_this_under():
    # A machine with no POSIX shell would silently collect zero of these.
    assert SHELLS, "no shell found to run the maintainer script under"


def test_an_orphaned_legacy_pyc_is_removed(shell, plugin_dir):
    """The dangerous one: no `__pycache__`, and importable in Python 3 as it is."""
    orphan = write(plugin_dir / "paho" / "client.pyc")

    result = run(shell, plugin_dir)

    assert result.returncode == 0, result.stderr
    assert not orphan.exists()


def test_an_orphaned_pycache_pyc_is_removed(shell, plugin_dir):
    orphan = write(plugin_dir / "__pycache__" / "paho.cpython-312.pyc")
    optimised = write(plugin_dir / "__pycache__" / "paho.cpython-312.opt-1.pyc")

    result = run(shell, plugin_dir)

    assert result.returncode == 0, result.stderr
    assert not orphan.exists()
    assert not optimised.exists()


def test_a_pyo_is_swept_too(shell, plugin_dir):
    orphan = write(plugin_dir / "legacy.pyo")

    assert run(shell, plugin_dir).returncode == 0
    assert not orphan.exists()


def test_bytecode_whose_source_is_present_is_kept(shell, plugin_dir):
    write(plugin_dir / "hdd.py", "# the source")
    legacy = write(plugin_dir / "hdd.pyc")
    cached = write(plugin_dir / "__pycache__" / "hdd.cpython-312.pyc")
    write(plugin_dir / "_vendor" / "paho" / "mqtt" / "client.py", "# vendored")
    vendored = write(
        plugin_dir / "_vendor" / "paho" / "mqtt" / "__pycache__" / "client.cpython-39.pyc"
    )

    assert run(shell, plugin_dir).returncode == 0
    assert legacy.exists()
    assert cached.exists()
    assert vendored.exists()


def test_a_directory_left_empty_is_removed(shell, plugin_dir):
    write(plugin_dir / "paho" / "mqtt" / "client.pyc")
    write(plugin_dir / "keep.py", "# the source")

    assert run(shell, plugin_dir).returncode == 0
    assert not (plugin_dir / "paho").exists()
    # Not the plugin directory itself, and not a directory with anything in it.
    assert plugin_dir.is_dir()
    assert (plugin_dir / "keep.py").exists()


def test_a_directory_that_still_holds_something_survives(shell, plugin_dir):
    write(plugin_dir / "locale" / "pl" / "LC_MESSAGES" / "MQTTBridge.mo", "compiled")
    orphan = write(plugin_dir / "locale" / "pl" / "LC_MESSAGES" / "old.pyc")

    assert run(shell, plugin_dir).returncode == 0
    assert not orphan.exists()
    assert (plugin_dir / "locale" / "pl" / "LC_MESSAGES" / "MQTTBridge.mo").exists()


def test_nothing_outside_the_plugin_directory_is_touched(shell, plugin_dir, tmp_path):
    plugin_dir.mkdir(parents=True)
    sibling = write(tmp_path / PLUGIN_PARENT / "WebInterface" / "orphan.pyc")
    above = write(tmp_path / "orphan.pyc")
    empty_sibling = tmp_path / PLUGIN_PARENT / "an empty neighbour"
    empty_sibling.mkdir(parents=True)

    assert run(shell, plugin_dir).returncode == 0
    assert sibling.exists()
    assert above.exists()
    assert empty_sibling.is_dir()


def test_a_path_with_spaces_survives(shell, plugin_dir):
    """Every path here has a space in it, including the one being deleted."""
    write(plugin_dir / "a package" / "a module.py", "# the source")
    kept = write(plugin_dir / "a package" / "__pycache__" / "a module.cpython-312.pyc")
    orphan = write(plugin_dir / "a package" / "__pycache__" / "a gone module.cpython-312.pyc")

    result = run(shell, plugin_dir)

    assert result.returncode == 0, result.stderr
    assert kept.exists()
    assert not orphan.exists()


def test_a_missing_plugin_directory_is_not_an_error(shell, tmp_path):
    absent = tmp_path / PLUGIN_PARENT / PLUGIN_NAME

    result = run(shell, absent)

    assert result.returncode == 0, result.stderr
    assert "MQTT Bridge installed." in result.stdout


def test_a_directory_by_another_name_is_refused(shell, tmp_path):
    """The environment variable is a test seam, not a way to point this anywhere."""
    elsewhere = tmp_path / "somebody elses files"
    orphan = write(elsewhere / "orphan.pyc")

    assert run(shell, elsewhere).returncode == 0
    assert orphan.exists()


def test_the_install_message_still_comes_out_after_a_sweep(shell, plugin_dir):
    write(plugin_dir / "paho" / "client.pyc")

    result = run(shell, plugin_dir)

    assert "removing orphaned bytecode client.pyc" in result.stdout
    assert "Menu -> Plugins -> MQTT Bridge" in result.stdout


def test_the_script_does_not_restart_enigma2():
    """A restart during a recording loses the recording; opkg cannot know."""
    body = POSTINST.read_text(encoding="utf-8")
    for forbidden in ("init 3", "init 4", "systemctl", "killall", "reboot"):
        assert forbidden not in body, forbidden


def test_the_real_plugin_directory_is_the_default():
    body = POSTINST.read_text(encoding="utf-8")
    assert (
        "PLUGIN_DIR=${MQTTBRIDGE_PLUGIN_DIR:-"
        "/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge}" in body
    )


def test_the_script_has_no_bashisms_that_a_shell_check_would_find():
    body = POSTINST.read_text(encoding="utf-8")
    assert body.startswith("#!/bin/sh\n")
    for bashism in ("[[", "==", "local ", "function ", "$'", "<<<", "&>"):
        assert bashism not in body, bashism
