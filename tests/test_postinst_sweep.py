"""`postinst` clears out bytecode whose source the upgrade removed.

The image byte-compiles a plugin after opkg has installed it, so the `.pyc`
files in the plugin directory are not in opkg's file list and opkg will never
remove them. An upgrade that moves a module - `paho/` to `_vendor/paho/` - takes
the `.py` away and leaves the compiled copy, and a legacy-location `.pyc` with
no source beside it is still importable in Python 3. The old module survives the
upgrade that was meant to remove it.

These tests run the real maintainer script, under every POSIX shell on the
machine, against a fixture tree. `busybox sh` is the one that matters - it is
what a receiver has - and `dash` is the standard-issue one that catches a
bashism. The shell is the unit here: rewriting the sweep in Python and testing
that would test something the receiver never runs.

Half of what is asserted below is about what the script refuses to do. A
maintainer script runs as root, with whatever environment and whatever working
directory the caller happened to have, over paths somebody else chose the names
of - so „it deleted the right files" is only half the question, and „it touched
nothing else" is the other half.
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
# not called MQTTBridge, so that the environment variable below cannot be turned
# into a way of pointing it at somebody else's files.
PLUGIN_NAME = "MQTTBridge"

# The sweep refuses a tree with no `plugin.py`, so every fixture that expects it
# to do anything has one.
PLUGIN_PY = "# the plugin entry point\n"


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


def run(shell, plugin_dir, cwd=None, environment=None):
    """The real postinst, with the plugin directory pointed at a fixture."""
    env = dict(os.environ)
    # An offline rootfs build sets these, and this is not one. Cleared rather
    # than assumed absent, so the suite cannot be steered by whoever runs it.
    env.pop("D", None)
    env.pop("OPKG_OFFLINE_ROOT", None)
    env["MQTTBRIDGE_PLUGIN_DIR"] = str(plugin_dir)
    env["MQTTBRIDGE_POSTINST_TEST"] = "1"
    env.update(environment or {})
    return subprocess.run(
        shell + [str(POSTINST)],
        env=env,
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )


def write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def snapshot(root):
    """Every path under `root`, with the bytes of each file."""
    found = {}
    for path in sorted(Path(root).rglob("*")):
        key = str(path.relative_to(root))
        found[key] = path.read_bytes() if path.is_file() else None
    return found


@pytest.fixture
def plugin_dir(tmp_path):
    """An installed plugin: the right directory name, and a `plugin.py` in it."""
    directory = tmp_path / PLUGIN_PARENT / PLUGIN_NAME
    write(directory / "plugin.py", PLUGIN_PY)
    return directory


def test_there_is_a_shell_to_run_this_under():
    # A machine with no POSIX shell would silently collect zero of these.
    assert SHELLS, "no shell found to run the maintainer script under"


# --------------------------------------------------------------- the sweep --


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


def test_the_plugins_own_compiled_copy_is_kept(shell, plugin_dir):
    """`plugin.pyc` beside `plugin.py` is ordinary, and deleting it is not."""
    compiled = write(plugin_dir / "plugin.pyc")

    assert run(shell, plugin_dir).returncode == 0
    assert compiled.exists()
    assert (plugin_dir / "plugin.py").exists()


# ------------------------------------------- only the directories it emptied --


def test_a_directory_left_empty_is_removed(shell, plugin_dir):
    write(plugin_dir / "paho" / "mqtt" / "client.pyc")

    assert run(shell, plugin_dir).returncode == 0
    assert not (plugin_dir / "paho").exists()
    assert plugin_dir.is_dir()
    assert (plugin_dir / "plugin.py").exists()


def test_a_directory_that_still_holds_something_survives(shell, plugin_dir):
    write(plugin_dir / "locale" / "pl" / "LC_MESSAGES" / "MQTTBridge.mo", "compiled")
    orphan = write(plugin_dir / "locale" / "pl" / "LC_MESSAGES" / "old.pyc")

    assert run(shell, plugin_dir).returncode == 0
    assert not orphan.exists()
    assert (plugin_dir / "locale" / "pl" / "LC_MESSAGES" / "MQTTBridge.mo").exists()


def test_a_directory_that_was_already_empty_survives(shell, plugin_dir):
    """The sweep gives back what it emptied, not whatever it finds empty.

    A plugin may keep an empty directory on purpose - somewhere to put captures,
    a cache that is cleared - and an upgrade that deletes it is an upgrade that
    broke something for a reason nobody will ever find.
    """
    kept = plugin_dir / "screenshots"
    kept.mkdir()
    nested = plugin_dir / "data" / "cache"
    nested.mkdir(parents=True)
    orphan = write(plugin_dir / "paho" / "client.pyc")

    assert run(shell, plugin_dir).returncode == 0
    assert not orphan.exists()
    assert not (plugin_dir / "paho").exists()
    assert kept.is_dir()
    assert nested.is_dir()


def test_only_the_emptied_branch_is_pruned(shell, plugin_dir):
    """The walk upward stops the moment a directory still holds something."""
    write(plugin_dir / "a" / "keep.mo", "compiled")
    write(plugin_dir / "a" / "b" / "c" / "gone.pyc")

    assert run(shell, plugin_dir).returncode == 0
    assert not (plugin_dir / "a" / "b").exists()
    assert (plugin_dir / "a" / "keep.mo").exists()


# ----------------------------------------------- nothing outside the plugin --


def test_nothing_outside_the_plugin_directory_is_touched(shell, plugin_dir, tmp_path):
    sibling = write(tmp_path / PLUGIN_PARENT / "WebInterface" / "orphan.pyc")
    above = write(tmp_path / "orphan.pyc")
    empty_sibling = tmp_path / PLUGIN_PARENT / "an empty neighbour"
    empty_sibling.mkdir(parents=True)

    assert run(shell, plugin_dir).returncode == 0
    assert sibling.exists()
    assert above.exists()
    assert empty_sibling.is_dir()


def test_a_newline_in_a_directory_name_cannot_reach_a_file_outside(
    shell, plugin_dir, tmp_path
):
    """🔴 `read` splits on newlines, and a filename may contain one.

    `find` prints `<plugin>/evil\\netc/passwd.pyc` as two lines, and the second
    is the *relative* path `etc/passwd.pyc`. opkg never chdir()s, so for a
    GUI-driven install that resolves against `/`. Here the working directory
    holds the decoy instead.
    """
    trap = plugin_dir / "evil\netc"
    trap.mkdir()
    write(trap / "passwd.pyc")
    elsewhere = tmp_path / "a working directory"
    decoy = write(elsewhere / "etc" / "passwd.pyc", "the real one")

    result = run(shell, plugin_dir, cwd=elsewhere)

    assert result.returncode == 0, result.stderr
    assert decoy.exists()
    assert decoy.read_text(encoding="utf-8") == "the real one"


def test_a_newline_in_a_directory_name_cannot_rmdir_a_directory_outside(
    shell, plugin_dir, tmp_path
):
    """The same split, aimed at the directory removal rather than at a file."""
    trap = plugin_dir / "evil\nempty"
    trap.mkdir()
    write(trap / "orphan.pyc")
    elsewhere = tmp_path / "a working directory"
    decoy = elsewhere / "empty"
    decoy.mkdir(parents=True)

    result = run(shell, plugin_dir, cwd=elsewhere)

    assert result.returncode == 0, result.stderr
    assert decoy.is_dir()


def test_a_path_with_spaces_survives(shell, plugin_dir):
    """Every path here has a space in it, including the one being deleted."""
    write(plugin_dir / "a package" / "a module.py", "# the source")
    kept = write(plugin_dir / "a package" / "__pycache__" / "a module.cpython-312.pyc")
    orphan = write(plugin_dir / "a package" / "__pycache__" / "a gone module.cpython-312.pyc")

    result = run(shell, plugin_dir)

    assert result.returncode == 0, result.stderr
    assert kept.exists()
    assert not orphan.exists()


# ------------------------------------------------------- the refusals --------


def test_a_tree_with_no_plugin_py_is_left_alone(shell, tmp_path):
    """🔴 A bytecode-only plugin directory is a build-time packaging, not an orphan.

    oe-alliance-core's `python3-compileall.inc` moves `.py` out of a package
    into `${PN}-src`, which is not in the feed. In such a tree „every `.pyc`
    whose `.py` is missing" is every file the plugin has - and a plugin
    directory with no `plugin.py`/`.pyc`/`.pyo` left is one that enigma2's own
    PluginComponent deletes outright.
    """
    compiled = tmp_path / PLUGIN_PARENT / PLUGIN_NAME
    write(compiled / "plugin.pyc")
    write(compiled / "hdd.pyc")
    write(compiled / "__pycache__" / "bridge.cpython-312.pyc")
    (compiled / "locale").mkdir()
    before = snapshot(compiled)

    result = run(shell, compiled)

    assert result.returncode == 0, result.stderr
    assert snapshot(compiled) == before
    assert "removing orphaned bytecode" not in result.stdout


def test_the_plugin_directory_cannot_be_emptied_by_the_sweep(shell, plugin_dir):
    """Which is why the refusal above is the load-bearing one.

    `plugin.py` has to be there for the sweep to run at all, and the sweep never
    deletes a `.py`, so the plugin directory always still holds one afterwards.
    """
    write(plugin_dir / "everything.pyc")
    write(plugin_dir / "__pycache__" / "else.cpython-312.pyc")

    assert run(shell, plugin_dir).returncode == 0
    assert plugin_dir.is_dir()
    assert list(plugin_dir.iterdir()) == [plugin_dir / "plugin.py"]


def test_a_missing_plugin_directory_is_not_an_error(shell, tmp_path):
    absent = tmp_path / PLUGIN_PARENT / PLUGIN_NAME

    result = run(shell, absent)

    assert result.returncode == 0, result.stderr
    assert "MQTT Bridge installed." in result.stdout


def test_a_directory_by_another_name_is_refused(shell, tmp_path):
    """The environment variable is a seam, not a way to point this anywhere.

    A complete plugin, `plugin.py` and all, so that the only thing refusing it
    is its name.
    """
    elsewhere = tmp_path / "somebody elses files"
    write(elsewhere / "plugin.py", PLUGIN_PY)
    orphan = write(elsewhere / "orphan.pyc")

    assert run(shell, elsewhere).returncode == 0
    assert orphan.exists()


def test_the_override_is_ignored_without_the_test_variable(shell, plugin_dir):
    """opkg hands a maintainer script the caller's environment verbatim."""
    orphan = write(plugin_dir / "paho" / "client.pyc")

    result = run(shell, plugin_dir, environment={"MQTTBRIDGE_POSTINST_TEST": ""})

    assert result.returncode == 0, result.stderr
    assert orphan.exists()


def test_a_trailing_slash_on_the_override_is_tolerated(shell, plugin_dir):
    orphan = write(plugin_dir / "paho" / "client.pyc")

    result = run(shell, str(plugin_dir) + "/")

    assert result.returncode == 0, result.stderr
    assert not orphan.exists()


@pytest.mark.parametrize("variable", ["D", "OPKG_OFFLINE_ROOT"])
def test_an_offline_rootfs_build_sweeps_nothing(shell, plugin_dir, variable):
    """do_rootfs runs postinsts on the build host, with absolute paths that are its own."""
    orphan = write(plugin_dir / "paho" / "client.pyc")

    result = run(shell, plugin_dir, environment={variable: "/build/tmp/work/image-rootfs"})

    assert result.returncode == 0, result.stderr
    assert orphan.exists()


# ------------------------------------------------------------- the symlink --


def test_a_symlinked_plugin_directory_is_resolved_and_swept(shell, tmp_path):
    """`find` does not descend a symlink operand, so this would sweep nothing.

    Silently: it would print no file, delete nothing and report success, which
    is the worst shape a bug can have in a maintainer script.
    """
    real = tmp_path / "the real place" / PLUGIN_NAME
    write(real / "plugin.py", PLUGIN_PY)
    orphan = write(real / "paho" / "client.pyc")
    link = tmp_path / PLUGIN_PARENT / PLUGIN_NAME
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)

    result = run(shell, link)

    assert result.returncode == 0, result.stderr
    assert not orphan.exists()
    assert not (real / "paho").exists()
    assert link.is_symlink()


def test_a_symlink_resolving_to_another_name_is_refused(shell, tmp_path):
    """The basename check is applied to what the link points at, not just to the link."""
    real = tmp_path / "the real place" / "somebody elses files"
    write(real / "plugin.py", PLUGIN_PY)
    orphan = write(real / "orphan.pyc")
    link = tmp_path / PLUGIN_PARENT / PLUGIN_NAME
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)

    assert run(shell, link).returncode == 0
    assert orphan.exists()


def test_a_symlink_inside_the_tree_is_neither_followed_nor_removed(shell, plugin_dir, tmp_path):
    outside = write(tmp_path / "outside" / "orphan.pyc")
    link = plugin_dir / "orphan.pyc"
    link.symlink_to(outside)

    assert run(shell, plugin_dir).returncode == 0
    assert outside.exists()
    assert link.is_symlink()


# ------------------------------------------------------------- the script ---


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
    assert "PLUGIN_DIR=/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge\n" in body


def test_the_upward_walk_is_bounded_by_the_plugin_directory():
    """A source assertion, because after the `plugin.py` refusal it cannot be
    reached by behaviour: the plugin directory always still holds a `.py`, so
    `rmdir` on it would fail anyway. It is kept as the second of two guards on
    the walk and asserted here so that removing it is a deliberate act."""
    body = POSTINST.read_text(encoding="utf-8")
    assert 'while [ "$_pu_holder" != "$_pu_root" ]; do' in body
    assert '"$_pu_root"/*) ;;' in body


def test_the_script_has_no_bashisms_that_a_shell_check_would_find():
    body = POSTINST.read_text(encoding="utf-8")
    assert body.startswith("#!/bin/sh\n")
    for bashism in ("[[", "==", "local ", "function ", "$'", "<<<", "&>"):
        assert bashism not in body, bashism
