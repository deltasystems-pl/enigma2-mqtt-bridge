"""`prerm` takes the compiled plugin with it when the package is removed.

`opkg remove` deletes the files it installed, which are the `.py` ones. The
image byte-compiles a plugin after opkg has installed it, so the `.pyc` files
are the receiver's and are in nobody's file list — and a legacy-location `.pyc`,
sitting where its source used to be rather than under `__pycache__`, is a
complete importable module in Python 3. On an OpenViX 6.6 receiver forty files
survived a removal, enigma2's plugin loader found them by module name at the
next GUI restart, and the plugin that `opkg status` said was not installed
reconnected to the broker.

These tests run the real maintainer script, under every POSIX shell on the
machine, against a fixture tree — the same arrangement as
`tests/test_postinst_sweep.py`, and for the same reason: the shell is the unit,
and rewriting the sweep in Python would test something the receiver never runs.

Half of what is asserted below is about what the script refuses to do. It runs
as root, over paths somebody else chose the names of, and it is the one script
here that removes directories — so „it deleted the right files" is only half the
question.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PRERM = REPO_ROOT / "CONTROL" / "prerm"

# A space in the path on purpose: an unquoted expansion in the script would
# split it, and every one of these tests would then be run against nothing.
PLUGIN_PARENT = "an installed plugin"

# The directory basename is not decoration. The script refuses to act on
# anything not called MQTTBridge, so that the environment variable below cannot
# be turned into a way of pointing it at somebody else's files.
PLUGIN_NAME = "MQTTBridge"

# The OpenWebif hook lives beside the plugin, not inside it: opkg installs one
# file into a directory that belongs to another package.
WEBIF_TAIL = ("WebInterface", "WebChilds", "External")

# What the receiver had compiled in the plugin directory when the removal that
# started all this was measured. `setup` is in the list because somebody had
# opened the setup screen once — which is the whole reason the sweep matches a
# pattern instead of a list of modules.
COMPILED_MODULES = (
    "__init__ plugin bridge commands discovery mqttclient keys setup version config log i18n"
    " publisher publishers bouquet boxinfo cam channels diagnostics enigma2 epggrid hdd oscam"
    " osd power recording remote screen service volume webif"
).split()

VENDORED_MODULES = (
    "__init__ client enums matcher packettypes properties publish reasoncodes subscribe"
).split()


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


def run(shell, plugin_dir, argument="remove", cwd=None, environment=None):
    """The real prerm, with the plugin directory pointed at a fixture."""
    env = dict(os.environ)
    # An offline rootfs build sets these, and this is not one. Cleared rather
    # than assumed absent, so the suite cannot be steered by whoever runs it.
    env.pop("D", None)
    env.pop("OPKG_OFFLINE_ROOT", None)
    env["MQTTBRIDGE_PLUGIN_DIR"] = str(plugin_dir)
    env["MQTTBRIDGE_PRERM_TEST"] = "1"
    env.update(environment or {})
    return subprocess.run(
        shell + [str(PRERM)] + ([] if argument is None else [argument]),
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
    """The plugin directory, with the right name and nothing in it yet."""
    directory = tmp_path / PLUGIN_PARENT / PLUGIN_NAME
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def external_dir(tmp_path):
    """OpenWebif's `External/`, which this package puts one file into."""
    directory = tmp_path.joinpath(PLUGIN_PARENT, *WEBIF_TAIL)
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def removed_package(plugin_dir, external_dir):
    """What opkg leaves behind: every `.py` gone, every `.pyc` still there.

    The shape measured on a receiver — legacy same-directory bytecode, the
    vendored MQTT client included, and the compiled OpenWebif hook beside where
    opkg's own `MQTTBridge.py` was.
    """
    for module in COMPILED_MODULES:
        write(plugin_dir / f"{module}.pyc")
    for module in VENDORED_MODULES:
        write(plugin_dir / "_vendor" / "paho" / "mqtt" / f"{module}.pyc")
    write(plugin_dir / "_vendor" / "paho" / "__init__.pyc")
    write(external_dir / "MQTTBridge.pyc")
    return plugin_dir


def test_there_is_a_shell_to_run_this_under():
    # A machine with no POSIX shell would silently collect zero of these.
    assert SHELLS, "no shell found to run the maintainer script under"


# ------------------------------------------------------------- the removal --


def test_a_removed_package_leaves_no_plugin_directory(shell, removed_package, external_dir):
    """(a) The whole point: nothing importable, and nothing to import it from."""
    result = run(shell, removed_package)

    assert result.returncode == 0, result.stderr
    assert not removed_package.exists()
    assert not (external_dir / "MQTTBridge.pyc").exists()
    assert external_dir.is_dir()


def test_every_compiled_file_the_receiver_wrote_is_gone(shell, removed_package):
    assert run(shell, removed_package).returncode == 0
    assert not list(removed_package.parent.rglob("*.pyc"))


def test_the_count_of_what_was_removed_is_reported(shell, removed_package):
    # The plugin directory only: the hook beside it is reported by name.
    expected = len(COMPILED_MODULES) + len(VENDORED_MODULES) + 1  # + `_vendor/paho`

    result = run(shell, removed_package)

    assert f"removed {expected} compiled files from the plugin directory" in result.stdout


def test_one_compiled_file_is_reported_in_the_singular(shell, plugin_dir):
    write(plugin_dir / "bridge.pyc")

    result = run(shell, plugin_dir)

    assert "removed 1 compiled file from the plugin directory" in result.stdout


def test_a_pyo_is_swept_too(shell, plugin_dir):
    orphan = write(plugin_dir / "legacy.pyo")

    assert run(shell, plugin_dir).returncode == 0
    assert not orphan.exists()
    assert not plugin_dir.exists()


def test_a_pycache_directory_goes_with_its_contents(shell, plugin_dir):
    write(plugin_dir / "__pycache__" / "bridge.cpython-312.pyc")
    write(plugin_dir / "__pycache__" / "bridge.cpython-312.opt-1.pyc")

    assert run(shell, plugin_dir).returncode == 0
    assert not plugin_dir.exists()


def test_a_directory_that_was_already_empty_goes_too(shell, plugin_dir):
    """Where this parts company with the upgrade sweep.

    `postinst` gives back only the directories it emptied itself, because on an
    upgrade an empty `screenshots/` is one the plugin means to keep. Here the
    plugin is going away and there is nothing left to keep it for.
    """
    (plugin_dir / "screenshots").mkdir()
    (plugin_dir / "data" / "cache").mkdir(parents=True)
    write(plugin_dir / "bridge.pyc")

    assert run(shell, plugin_dir).returncode == 0
    assert not plugin_dir.exists()


# ------------------------------------------------------- what it never takes --


def test_a_source_file_is_never_deleted(shell, plugin_dir, external_dir):
    """(d) opkg removes its own files, and this script runs before it does.

    So during an ordinary removal every `.py` is still on disk while this runs,
    and the same is true of a `prerm` somebody ran by hand at the wrong moment.
    """
    source = write(plugin_dir / "bridge.py", "# the source")
    compiled = write(plugin_dir / "bridge.pyc")
    hook = write(external_dir / "MQTTBridge.py", "# the hook")

    result = run(shell, plugin_dir)

    assert result.returncode == 0, result.stderr
    assert source.read_text(encoding="utf-8") == "# the source"
    assert hook.read_text(encoding="utf-8") == "# the hook"
    assert not compiled.exists()
    assert plugin_dir.is_dir()


def test_a_directory_holding_a_source_file_survives(shell, plugin_dir):
    write(plugin_dir / "_vendor" / "paho" / "mqtt" / "client.py", "# vendored")
    write(plugin_dir / "_vendor" / "paho" / "mqtt" / "client.pyc")

    assert run(shell, plugin_dir).returncode == 0
    assert (plugin_dir / "_vendor" / "paho" / "mqtt" / "client.py").exists()
    assert plugin_dir.is_dir()


def test_nothing_is_said_about_leftovers_while_opkg_still_has_files_to_remove(
    shell, plugin_dir
):
    """A count of what remains, taken before opkg deletes its own files, would
    be a number about files that are about to go — printed on every removal."""
    write(plugin_dir / "bridge.py", "# the source")
    write(plugin_dir / "bridge.pyc")

    result = run(shell, plugin_dir)

    assert "not this package's" not in result.stdout


def test_a_stray_file_keeps_the_directory_and_is_counted(shell, plugin_dir):
    """(b) Somebody's own file is not this package's to delete or to ignore."""
    write(plugin_dir / "bridge.pyc")
    stray = write(plugin_dir / "notes.txt", "mine")
    also_stray = write(plugin_dir / "captures" / "screen.png", "mine")

    result = run(shell, plugin_dir)

    assert result.returncode == 0, result.stderr
    assert stray.read_text(encoding="utf-8") == "mine"
    assert also_stray.exists()
    assert plugin_dir.is_dir()
    assert "2 files in it are not this package's" in result.stdout


def test_one_stray_file_is_counted_in_the_singular(shell, plugin_dir):
    write(plugin_dir / "bridge.pyc")
    write(plugin_dir / "notes.txt", "mine")

    result = run(shell, plugin_dir)

    assert "1 file in it is not this package's" in result.stdout


# ------------------------------------------------------ the upgrade argument --


def test_the_removal_half_of_an_upgrade_sweeps_nothing(shell, removed_package, external_dir):
    """(e) `postinst` sweeps an upgrade, after the new tree is unpacked.

    That is the only moment at which „this bytecode has no source" is true.
    Sweeping here would delete every compiled file the receiver has, including
    the ones the new package is about to reuse.
    """
    before = snapshot(removed_package)
    hook = external_dir / "MQTTBridge.pyc"

    result = run(shell, removed_package, argument="upgrade")

    assert result.returncode == 0, result.stderr
    assert snapshot(removed_package) == before
    assert hook.exists()


def test_an_unrecognised_argument_sweeps_nothing(shell, removed_package):
    """The safe half of the guess is the one that leaves files alone."""
    before = snapshot(removed_package)

    assert run(shell, removed_package, argument="failed-upgrade").returncode == 0
    assert snapshot(removed_package) == before


def test_no_argument_at_all_is_a_removal(shell, removed_package):
    """A `./prerm` run by hand is somebody taking this off their box."""
    assert run(shell, removed_package, argument=None).returncode == 0
    assert not removed_package.exists()


# ------------------------------------------------- nothing outside the root --


def test_nothing_outside_the_plugin_directory_is_touched(
    shell, removed_package, external_dir, tmp_path
):
    """(f) Siblings full of bytecode, which is what `Extensions/` is."""
    sibling = write(tmp_path / PLUGIN_PARENT / "SomeOtherPlugin" / "plugin.pyc")
    sibling_cache = write(
        tmp_path / PLUGIN_PARENT / "SomeOtherPlugin" / "__pycache__" / "x.cpython-312.pyc"
    )
    above = write(tmp_path / "orphan.pyc")
    empty_sibling = tmp_path / PLUGIN_PARENT / "an empty neighbour"
    empty_sibling.mkdir()

    assert run(shell, removed_package).returncode == 0
    assert sibling.exists()
    assert sibling_cache.exists()
    assert above.exists()
    assert empty_sibling.is_dir()
    assert external_dir.is_dir()


def test_only_this_packages_file_goes_from_the_shared_external_directory(
    shell, removed_package, external_dir
):
    """`External/` belongs to OpenWebif; this package puts one file in it."""
    init = write(external_dir / "__init__.py", "# OpenWebif's")
    init_compiled = write(external_dir / "__init__.pyc")
    neighbour = write(external_dir / "SomeOtherPlugin.pyc")
    neighbour_cached = write(
        external_dir / "__pycache__" / "SomeOtherPlugin.cpython-312.pyc"
    )

    result = run(shell, removed_package)

    assert result.returncode == 0, result.stderr
    assert init.read_text(encoding="utf-8") == "# OpenWebif's"
    assert init_compiled.exists()
    assert neighbour.exists()
    assert neighbour_cached.exists()
    assert not (external_dir / "MQTTBridge.pyc").exists()


def test_the_pycache_form_of_the_hook_goes_as_well(shell, plugin_dir, external_dir):
    cached = write(external_dir / "__pycache__" / "MQTTBridge.cpython-312.pyc")
    optimised = write(external_dir / "__pycache__" / "MQTTBridge.cpython-312.opt-1.pyc")

    result = run(shell, plugin_dir)

    assert result.returncode == 0, result.stderr
    assert not cached.exists()
    assert not optimised.exists()
    assert (external_dir / "__pycache__").is_dir()


def test_a_newline_in_a_directory_name_cannot_reach_a_file_outside(
    shell, plugin_dir, tmp_path
):
    """🔴 `read` splits on newlines, and a filename may contain one.

    `find` prints `<plugin>/evil\\netc/passwd.pyc` as two lines, and the second
    is the *relative* path `etc/passwd.pyc`. opkg never chdir()s, so for a
    GUI-driven removal that resolves against `/`. Here the working directory
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
    compiled = write(plugin_dir / "a package" / "__pycache__" / "a module.cpython-312.pyc")

    result = run(shell, plugin_dir)

    assert result.returncode == 0, result.stderr
    assert not compiled.exists()
    assert not (plugin_dir / "a package" / "__pycache__").exists()
    assert (plugin_dir / "a package" / "a module.py").exists()


# ------------------------------------------------------------- the refusals --


def test_a_missing_plugin_directory_is_not_an_error(shell, tmp_path):
    absent = tmp_path / PLUGIN_PARENT / PLUGIN_NAME

    result = run(shell, absent)

    assert result.returncode == 0, result.stderr
    assert "retained topics are not retracted" in result.stdout


def test_a_directory_by_another_name_is_refused(shell, tmp_path):
    """The environment variable is a seam, not a way to point this anywhere."""
    elsewhere = tmp_path / "somebody elses files"
    compiled = write(elsewhere / "orphan.pyc")

    assert run(shell, elsewhere).returncode == 0
    assert compiled.exists()
    assert elsewhere.is_dir()


def test_the_override_is_ignored_without_the_test_variable(shell, removed_package):
    """opkg hands a maintainer script the caller's environment verbatim."""
    before = snapshot(removed_package)

    result = run(shell, removed_package, environment={"MQTTBRIDGE_PRERM_TEST": ""})

    assert result.returncode == 0, result.stderr
    assert snapshot(removed_package) == before


def test_a_trailing_slash_on_the_override_is_tolerated(shell, removed_package):
    result = run(shell, str(removed_package) + "/")

    assert result.returncode == 0, result.stderr
    assert not removed_package.exists()


@pytest.mark.parametrize("variable", ["D", "OPKG_OFFLINE_ROOT"])
def test_an_offline_rootfs_build_sweeps_nothing(shell, removed_package, variable):
    """do_rootfs runs maintainer scripts on the build host, with paths of its own."""
    before = snapshot(removed_package)

    result = run(
        shell, removed_package, environment={variable: "/build/tmp/work/image-rootfs"}
    )

    assert result.returncode == 0, result.stderr
    assert snapshot(removed_package) == before


# ------------------------------------------------------------- the symlink --


def test_a_symlinked_plugin_directory_is_refused(shell, tmp_path):
    """(c) 🔴 Where this parts company with `postinst` a second time.

    That sweep resolves a symlinked plugin directory and sweeps what it points
    at, which is safe because it only ever deletes files. This one removes the
    directories too, and the last of them is the plugin directory itself — so
    following the link would empty and remove a directory somebody deliberately
    put somewhere else, and leave the link dangling behind it.
    """
    real = tmp_path / "the real place" / PLUGIN_NAME
    compiled = write(real / "bridge.pyc")
    write(real / "somebody elses notes.txt", "mine")
    link = tmp_path / PLUGIN_PARENT / PLUGIN_NAME
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)

    result = run(shell, link)

    assert result.returncode == 0, result.stderr
    assert compiled.exists()
    assert real.is_dir()
    assert link.is_symlink()


def test_a_symlink_inside_the_tree_is_neither_followed_nor_removed(
    shell, plugin_dir, tmp_path
):
    outside = write(tmp_path / "outside" / "orphan.pyc")
    link = plugin_dir / "orphan.pyc"
    link.symlink_to(outside)

    result = run(shell, plugin_dir)

    assert result.returncode == 0, result.stderr
    assert outside.exists()
    assert link.is_symlink()
    # A link is not bytecode this script may delete, so the directory holding it
    # stays — and it is counted as somebody else's file.
    assert plugin_dir.is_dir()


def test_a_symlinked_external_directory_is_refused(shell, plugin_dir, tmp_path):
    """The hook's directory is OpenWebif's, and a link on it is not ours to follow."""
    real = tmp_path / "the real openwebif" / "External"
    hook = write(real / "MQTTBridge.pyc")
    link = tmp_path.joinpath(PLUGIN_PARENT, *WEBIF_TAIL)
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)

    assert run(shell, plugin_dir).returncode == 0
    assert hook.exists()


def test_a_symlinked_hook_is_not_followed(shell, plugin_dir, external_dir, tmp_path):
    target = write(tmp_path / "outside" / "something.pyc")
    link = external_dir / "MQTTBridge.pyc"
    link.symlink_to(target)

    assert run(shell, plugin_dir).returncode == 0
    assert target.exists()
    assert link.is_symlink()


# ------------------------------------------------------------- the script ---


def test_the_retained_topics_message_still_comes_out(shell, removed_package):
    """It is the one thing removing the package cannot do for you."""
    result = run(shell, removed_package)

    assert "retained topics are not retracted by uninstalling" in result.stdout
    assert "cmd/reset" in result.stdout


def test_the_script_does_not_restart_enigma2():
    """A restart during a recording loses the recording; opkg cannot know."""
    body = PRERM.read_text(encoding="utf-8")
    for forbidden in ("init 3", "init 4", "systemctl", "killall", "reboot"):
        assert forbidden not in body, forbidden


def test_the_script_never_touches_the_settings_file():
    """A reinstall has to find its configuration, the broker password included."""
    body = PRERM.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in body.splitlines() if not line.lstrip().startswith("#")
    )
    assert "/etc/enigma2" not in code


def test_the_real_plugin_directory_is_the_default():
    body = PRERM.read_text(encoding="utf-8")
    assert "PLUGIN_DIR=/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge\n" in body


def test_the_removal_is_gated_on_the_argument_opkg_passes():
    """A source assertion: the gate is one `case`, and deleting it would turn
    every upgrade into a full recompile without failing a single test that runs
    with an argument."""
    body = PRERM.read_text(encoding="utf-8")
    assert 'case "${1:-remove}" in' in body
    assert "    remove) ;;" in body


def test_the_script_has_no_bashisms_that_a_shell_check_would_find():
    body = PRERM.read_text(encoding="utf-8")
    assert body.startswith("#!/bin/sh\n")
    for bashism in ("[[", "==", "local ", "function ", "$'", "<<<", "&>"):
        assert bashism not in body, bashism
