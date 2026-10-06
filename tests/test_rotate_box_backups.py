"""`deploy-to-box.sh` removes its own old backups from a receiver, and only those.

The first rotation was `ls -1d MQTTBridge.bak-* | sort -r | tail -n +4`: every
entry with that prefix, ordered by name, all but the first three removed. A
backup somebody makes by hand before a risky change is named for the reason -
`MQTTBridge.bak-pre-<reason>-<date>` - and `p` sorts after every digit, so in
reverse order the named ones came first and filled the three places. On a
receiver with seven of them and three dated ones a deploy would have removed
seven of ten: four of the named backups and every dated one, the backup it had
just made included.

These tests run the real script, under every POSIX shell on the machine,
against a fixture directory - the same arrangement as
`tests/test_prerm_sweep.py`, and for the same reason: the shell is the unit, and
the receiver's shell is busybox. `deploy-to-box.sh` runs the script's text
inside the command it sends, so that form is run here too.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ROTATE = REPO_ROOT / "tools" / "rotate-box-backups.sh"
DEPLOY = REPO_ROOT / "tools" / "deploy-to-box.sh"

# A space in the path on purpose: an unquoted expansion in the script would
# split it.
BACKUP_PARENT = "a receiver"

NAMED = (
    "MQTTBridge.bak-pre-0.2.0-20260101",
    "MQTTBridge.bak-pre-0.3.0-20260201",
    "MQTTBridge.bak-pre-0.3.1-20260301",
    "MQTTBridge.bak-pre-0.4.0-20260401",
    "MQTTBridge.bak-pre-relay-20260501",
    "MQTTBridge.bak-pre-rollback-20260601",
    "MQTTBridge.bak-pre-uninstall-20260701",
)
DATED = (
    "MQTTBridge.bak-20260810-101500",
    "MQTTBridge.bak-20260811-090000",
    "MQTTBridge.bak-20260811-090001",
)


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


@pytest.fixture
def backups(tmp_path):
    directory = tmp_path / BACKUP_PARENT / "mqttbridge-backups"
    directory.mkdir(parents=True)
    return directory


def make(directory, *names):
    for name in names:
        backup = directory / name
        backup.mkdir()
        (backup / "plugin.py").write_text(name, encoding="utf-8")


def run(shell, *arguments, cwd=None):
    return subprocess.run(
        shell + [str(ROTATE)] + [str(argument) for argument in arguments],
        cwd=None if cwd is None else str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
    )


def names(directory):
    return sorted(path.name for path in directory.iterdir())


def snapshot(root):
    found = {}
    for path in sorted(Path(root).rglob("*")):
        key = str(path.relative_to(root))
        found[key] = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    return found


def test_there_is_a_shell_to_run_this_under():
    assert SHELLS, "no shell found to run the rotation under"


# ------------------------------------------------------------ what is kept --


def test_seven_named_backups_and_the_one_just_made_all_survive(shell, backups):
    """The receiver this was found on: seven named backups, three dated ones."""
    make(backups, *NAMED)
    make(backups, *DATED)

    result = run(shell, backups)

    assert result.returncode == 0, result.stderr
    assert names(backups) == sorted(NAMED + DATED)


def test_the_newest_three_of_its_own_are_kept(shell, backups):
    make(backups, *NAMED)
    make(backups, *DATED)
    make(backups, "MQTTBridge.bak-20260812-120000", "MQTTBridge.bak-20251231-235959")

    assert run(shell, backups).returncode == 0

    assert names(backups) == sorted(NAMED + (
        "MQTTBridge.bak-20260811-090000",
        "MQTTBridge.bak-20260811-090001",
        "MQTTBridge.bak-20260812-120000",
    ))


def test_a_kept_backup_is_left_whole(shell, backups):
    make(backups, *DATED)
    make(backups, "MQTTBridge.bak-20260101-000000")
    kept = {name: snapshot(backups / name) for name in DATED}

    assert run(shell, backups).returncode == 0

    assert {name: snapshot(backups / name) for name in DATED} == kept


def test_the_order_is_the_stamp_in_the_name_and_not_the_file_time(shell, backups):
    """`cp -a` gives a backup the time of the directory it copied, so that says nothing."""
    stamps = [f"MQTTBridge.bak-2026090{day}-120000" for day in range(1, 6)]
    make(backups, *stamps)
    # The oldest by name is the newest by modification time, and the other way round.
    for age, name in enumerate(stamps):
        os.utime(backups / name, (1900000000 - age * 86400, 1900000000 - age * 86400))

    assert run(shell, backups).returncode == 0

    assert names(backups) == stamps[2:]


def test_three_or_fewer_are_all_kept(shell, backups):
    make(backups, *DATED)
    assert run(shell, backups).returncode == 0
    assert names(backups) == sorted(DATED)


def test_an_empty_directory_is_no_error(shell, backups):
    result = run(shell, backups)
    assert result.returncode == 0, result.stderr
    assert names(backups) == []


def test_the_number_kept_can_be_given(shell, backups):
    make(backups, *DATED)
    make(backups, NAMED[0])

    assert run(shell, backups, 1).returncode == 0

    assert names(backups) == sorted((NAMED[0], DATED[-1]))


# ------------------------------------------------------ what is never touched --


def test_only_a_directory_with_exactly_the_stamp_is_one_of_its_own(shell, backups, tmp_path):
    """Everything here is older by name than the dated four, and none of it is counted."""
    make(backups, *DATED)
    make(backups, "MQTTBridge.bak-20260812-120000")
    strangers = (
        "MQTTBridge.bak-20200101-000000-keep",
        "MQTTBridge.bak-20200101-00000",
        "MQTTBridge.bak-2020010-000000",
        "MQTTBridge.bak-20200101_000000",
        "MQTTBridge.bak-",
        "MQTTBridge.bak-$(date +%Y%m%d-%H%M%S)",
        "MQTTBridge.bak-20200101-000000 copy",
        "OtherPlugin.bak-20200101-000000",
        "xMQTTBridge.bak-20200101-000000",
    )
    make(backups, *strangers)
    # A file and a symlink that carry the name of a backup are not backups.
    (backups / "MQTTBridge.bak-20200102-000000").write_text("a note", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    make(tmp_path, "elsewhere")
    (backups / "MQTTBridge.bak-20200103-000000").symlink_to(elsewhere, target_is_directory=True)

    assert run(shell, backups).returncode == 0

    assert names(backups) == sorted(strangers + (
        "MQTTBridge.bak-20200102-000000",
        "MQTTBridge.bak-20200103-000000",
        "MQTTBridge.bak-20260811-090000",
        "MQTTBridge.bak-20260811-090001",
        "MQTTBridge.bak-20260812-120000",
    ))
    assert (backups / "MQTTBridge.bak-20200103-000000").is_symlink()
    assert (elsewhere / "plugin.py").read_text(encoding="utf-8") == "elsewhere"


def test_nothing_outside_the_directory_is_touched(shell, backups):
    make(backups, *DATED)
    make(backups, "MQTTBridge.bak-20260101-000000")
    # The same names one level up, and in the directory the script is run from.
    make(backups.parent, *DATED)
    make(backups.parent, "MQTTBridge.bak-20260101-000000", "MQTTBridge.bak-20260102-000000")
    before = snapshot(backups.parent)

    assert run(shell, backups, cwd=backups.parent).returncode == 0

    after = snapshot(backups.parent)
    gone = sorted(set(before) - set(after))
    assert gone == [
        "mqttbridge-backups/MQTTBridge.bak-20260101-000000",
        "mqttbridge-backups/MQTTBridge.bak-20260101-000000/plugin.py",
    ]


@pytest.mark.parametrize("arguments", [(), ("",), ("missing",), ("{dir}", "three"),
                                       ("{dir}", "-1"), ("{dir}", "3 4")])
def test_a_bad_call_removes_nothing(shell, backups, arguments):
    make(backups, *DATED)
    make(backups, "MQTTBridge.bak-20260101-000000", "MQTTBridge.bak-20260102-000000")
    before = snapshot(backups)
    given = [argument.replace("{dir}", str(backups)) for argument in arguments]

    result = run(shell, *given, cwd=backups)

    assert result.returncode == 2
    assert result.stderr.startswith("rotate-box-backups.sh: ")
    assert snapshot(backups) == before


# ------------------------------------------------- the form the receiver runs --


def test_the_text_runs_inside_the_command_the_deploy_script_sends(shell, backups):
    """As `deploy-to-box.sh` sends it: under `set -e`, in a subshell, arguments in front."""
    make(backups, *NAMED)
    make(backups, *DATED)
    make(backups, "MQTTBridge.bak-20260812-120000")
    text = ROTATE.read_text(encoding="utf-8")
    command = f"set -e\n( set -- '{backups}' '3'\n{text}\n)\ncd '{backups}'\necho carried on\n"

    result = subprocess.run(shell + ["-c", command], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "carried on\n"
    assert names(backups) == sorted(NAMED + DATED[1:] + ("MQTTBridge.bak-20260812-120000",))


def test_the_deploy_script_sends_this_text_and_rotates_no_other_way():
    text = DEPLOY.read_text(encoding="utf-8")
    assert 'ROTATE=$(cat "$REPO_ROOT/tools/rotate-box-backups.sh")' in text
    assert "( set -- '$BACKUP_DIR' '$BACKUPS_KEPT'\n$ROTATE\n" in text
    assert "BACKUPS_KEPT=3\n" in text
    # The backup it makes is the name the rotation counts.
    assert "'$BACKUP_DIR/MQTTBridge.bak-$STAMP'" in text
    assert "STAMP=$(date +%Y%m%d-%H%M%S)\n" in text
    backing_up = text.split('say "backing up the installed plugin"')[1].split('say "copying')[0]
    assert "rm " not in backing_up
    assert "tail -n +4" not in text


def test_both_scripts_parse():
    assert subprocess.run(["bash", "-n", str(DEPLOY)], capture_output=True).returncode == 0
    assert subprocess.run(["sh", "-n", str(ROTATE)], capture_output=True).returncode == 0
