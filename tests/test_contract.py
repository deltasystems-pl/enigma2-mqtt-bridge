"""`docs/contract.json` says what `docs/TOPICS.md` says, and a release keeps its promises.

The topic contract is prose, written for the people who build consumers against it. The same
contract is also written as data, `docs/contract.json`, so that a machine can answer two questions
a reviewer answers badly by eye: does the data still say what the prose says, and did this change
remove or retype something a consumer relies on without a new contract major. The rule is in the
"Contract version" section of `docs/TOPICS.md`; the checker is `tools/check-contract.py`.

Most of these tests break one side of the real pair on purpose and expect the checker to name the
break. A checker that passes the real files and nothing else would pass an empty contract too.
"""

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHECKER = REPO_ROOT / "tools" / "check-contract.py"
TOPICS = REPO_ROOT / "docs" / "TOPICS.md"
CONTRACT = REPO_ROOT / "docs" / "contract.json"


@pytest.fixture(scope="module")
def checker():
    spec = importlib.util.spec_from_file_location("check_contract", CHECKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def topics_text():
    return TOPICS.read_text(encoding="utf-8")


@pytest.fixture()
def contract():
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def _mentions(problems, *words):
    """True when one problem line carries every word - the checker has to say what broke."""
    return any(all(word in line for word in words) for line in problems)


# --------------------------------------------------------------- the real pair --


def test_the_repository_pair_agrees(checker, topics_text, contract):
    assert checker.check_consistency(topics_text, contract) == []


def test_the_contract_major_is_one(contract):
    assert contract["contract"] == 1


def test_the_parse_is_not_empty(checker, topics_text):
    # Guards the guard: a parser that silently found nothing would agree with an empty file.
    parsed = checker.parse_topics(topics_text)
    assert {"availability", "info", "last_error", "zap_history"} <= set(parsed["state_topics"])
    assert {"zap", "config", "uninstall", "history_clear"} <= set(parsed["commands"])
    assert parsed["info_members"]["capabilities"] == "list of strings"
    assert parsed["settings"]["uninstall_allowed"] == {
        "type": "bool",
        "writable": False,
        "since": "0.3.0",
    }
    assert parsed["settings"]["screenshot"]["type"] == "enum(off|on_zap|interval)"
    assert "uninstall" in parsed["capabilities"]
    assert {"kind": "command", "name": "update"} in parsed["planned"]


# ------------------------------------------------------------- and the code --
#
# The prose and the data can agree with each other and both be wrong about the plugin. These
# three read the plugin's own tables, so a command, a setting or a capability added to the code
# alone fails here until the contract says it too.


def test_the_commands_are_the_dispatchers(contract):
    from MQTTBridge.commands import CommandDispatcher

    assert sorted(CommandDispatcher(None).handlers) == sorted(contract["commands"])


def test_the_settings_are_the_configs(contract):
    from MQTTBridge import config

    settings = contract["settings"]
    assert sorted(n for n, s in settings.items() if s["writable"]) == sorted(
        config.REMOTE_SETTING_NAMES
    )
    assert sorted(n for n, s in settings.items() if not s["writable"]) == sorted(
        config.READ_ONLY_SETTING_NAMES
    )


def test_the_capabilities_are_the_bridges(contract):
    # Imported, not copied: test_discovery_templates keeps this enumeration complete - every
    # `*_CAPABILITY` constant must be in it - so one list is guarded, and this reads that list.
    from test_discovery_templates import every_capability

    assert every_capability() == contract["capabilities"]


def test_the_setting_types_are_the_configs(contract):
    from MQTTBridge import config

    for name, entry in contract["settings"].items():
        kind = config.SETTING_KINDS[name]
        if kind == "choice":
            expected = "enum(" + "|".join(config.CHOICES[name]) + ")"
        else:
            expected = kind
        assert entry["type"] == expected, name


def test_the_info_members_are_build_infos(contract):
    # Read from the source rather than by running it: build_info needs a whole bridge, and its
    # member list is a literal. A member added there alone fails here.
    import ast

    tree = ast.parse((REPO_ROOT / "src" / "MQTTBridge" / "bridge.py").read_text(encoding="utf-8"))
    builds = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "build_info"
    ]
    assert len(builds) == 1
    returned = [node.value for node in ast.walk(builds[0]) if isinstance(node, ast.Return)]
    assert len(returned) == 1 and isinstance(returned[0], ast.Dict)
    keys = [key.value for key in returned[0].keys]
    assert sorted(keys) == sorted(contract["info_members"])


def test_the_topics_that_are_not_json_are_the_raw_ones(contract):
    # `availability` is the bridge's own; every other topic published as raw bytes is named in
    # its publisher's `raw`. Topic names and retain flags have no such list in the code - they
    # are held to the prose only.
    from MQTTBridge.publishers import PUBLISHER_CLASSES

    raw = {"availability"}
    for publisher in PUBLISHER_CLASSES:
        raw.update(getattr(publisher, "raw", ()))
    not_json = {
        name for name, entry in contract["state_topics"].items() if entry["payload"] != "json"
    }
    assert not_json == raw


# ------------------------------------------------- a deliberately broken pair --


def test_a_command_missing_from_the_data_is_named(checker, topics_text, contract):
    del contract["commands"]["uninstall"]
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "command", "uninstall", "contract.json")


def test_a_command_missing_from_the_prose_is_named(checker, topics_text, contract):
    contract["commands"]["self_destruct"] = {}
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "command", "self_destruct", "TOPICS.md")


def test_a_state_topic_missing_from_the_data_is_named(checker, topics_text, contract):
    del contract["state_topics"]["zap_history"]
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "state topic", "zap_history")


def test_a_payload_kind_that_differs_is_named(checker, topics_text, contract):
    contract["state_topics"]["power"]["payload"] = "json"
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "state topic", "power", "payload")


def test_a_retain_flag_that_differs_is_named(checker, topics_text, contract):
    contract["state_topics"]["key"]["retained"] = True
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "state topic", "key", "retained")


def test_a_since_that_differs_is_named(checker, topics_text, contract):
    contract["commands"]["uninstall"]["since"] = "0.2.0"
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "command", "uninstall", "since")


def test_an_info_member_retyped_is_named(checker, topics_text, contract):
    contract["info_members"]["uptime"] = "string"
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "info member", "uptime")


def test_a_wol_member_missing_is_named(checker, topics_text, contract):
    del contract["info_wol_members"]["mechanism"]
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "info.wol member", "mechanism")


def test_a_build_member_missing_is_named(checker, topics_text, contract):
    del contract["info_build_members"]["on_disk"]
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "info.build member", "on_disk")


def test_a_build_member_retyped_in_the_prose_is_named(checker, topics_text, contract):
    row = "| `dirty` | bool or `null` |"
    assert topics_text.count(row) == 1
    problems = checker.check_consistency(topics_text.replace(row, "| `dirty` | bool |"), contract)
    assert _mentions(problems, "info.build member", "dirty", "differs")


def test_a_removed_build_member_is_refused(checker, contract):
    newer = copy.deepcopy(contract)
    del newer["info_build_members"]["time"]
    assert _mentions(checker.compare(contract, newer), "info.build member", "time", "removed")


def test_a_setting_made_writable_in_the_data_is_named(checker, topics_text, contract):
    contract["settings"]["uninstall_allowed"]["writable"] = True
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "setting", "uninstall_allowed")


def test_an_extra_capability_is_named(checker, topics_text, contract):
    contract["capabilities"].append("teleport")
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "capability", "teleport")


def test_a_contract_major_that_differs_is_named(checker, topics_text, contract):
    contract["contract"] = 2
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "contract major")


def test_a_planned_row_missing_from_the_data_is_named(checker, topics_text, contract):
    contract["planned"] = [
        row for row in contract["planned"] if row != {"kind": "command", "name": "update"}
    ]
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "planned", "update")


def test_a_command_added_to_the_prose_only_is_named(checker, topics_text, contract):
    row = "| `restart_gui` | any |"
    assert row in topics_text
    broken = topics_text.replace(row, "| `teleport` | any | Moves the box | - |\n" + row, 1)
    problems = checker.check_consistency(broken, contract)
    assert _mentions(problems, "command", "teleport", "contract.json")


def test_a_planned_item_that_is_already_implemented_is_refused(checker, topics_text, contract):
    # A plan that is also in the implemented tables is a feature pretending to be a promise,
    # or a promise pretending to be a feature: either way a consumer cannot tell which.
    contract["planned"].append({"kind": "command", "name": "zap"})
    broken_text = topics_text.replace(
        "| Command | `update` |", "| Command | `zap` |\n| Command | `update` |", 1
    )
    problems = checker.check_consistency(broken_text, contract)
    assert _mentions(problems, "planned", "zap", "already")


def test_topics_disagreeing_with_itself_on_cmd_config_is_named(checker, topics_text, contract):
    # The `settings` row and `cmd/config`'s payload both spell out the writable keys.
    assert '"screenshot_delay": 4, ' in topics_text
    broken = topics_text.replace('"screenshot_delay": 4, ', "", 1)
    problems = checker.check_consistency(broken, contract)
    assert _mentions(problems, "disagrees with itself", "cmd/config")


def test_topics_disagreeing_with_itself_on_read_only_members_is_named(
    checker, topics_text, contract
):
    # The `settings` row and the read-only table both name the read-only members.
    lines = topics_text.splitlines(keepends=True)
    row = [line for line in lines if line.startswith("| `uninstall_allowed` | 0.3.0 |")]
    assert len(row) == 1
    broken = "".join(line for line in lines if line is not row[0])
    problems = checker.check_consistency(broken, contract)
    assert _mentions(problems, "disagrees with itself", "read-only")


def test_an_unknown_part_of_the_data_is_named(checker, topics_text, contract):
    contract["hidden_promises"] = {"x": 1}
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "has a part", "hidden_promises")


def test_an_exception_missing_from_the_data_is_named(checker, topics_text, contract):
    contract["exceptions"] = [
        row for row in contract["exceptions"] if row["name"] != "timers-lists-finished"
    ]
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "exception", "timers-lists-finished", "contract.json")


def test_an_exception_missing_from_the_prose_is_named(checker, topics_text, contract):
    contract["exceptions"].append({"name": "quietly-different", "release": "unreleased"})
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "exception", "quietly-different", "TOPICS.md")


def test_an_exception_dated_differently_is_named(checker, topics_text, contract):
    for row in contract["exceptions"]:
        if row["name"] == "timers-lists-finished":
            row["release"] = "0.3.0"
    problems = checker.check_consistency(topics_text, contract)
    assert _mentions(problems, "exception", "timers-lists-finished", "release")


# ------------------------------------------------ against the previous release --


def test_an_addition_keeps_the_major(checker, contract):
    newer = copy.deepcopy(contract)
    newer["commands"]["teleport"] = {"since": "9.9.9"}
    newer["capabilities"].append("teleport")
    assert checker.compare(contract, newer) == []


def test_a_removal_without_a_new_major_is_refused(checker, contract):
    newer = copy.deepcopy(contract)
    del newer["commands"]["reboot"]
    problems = checker.compare(contract, newer)
    assert _mentions(problems, "command", "reboot", "removed")


def test_a_retype_without_a_new_major_is_refused(checker, contract):
    newer = copy.deepcopy(contract)
    newer["info_members"]["uptime"] = "string"
    problems = checker.compare(contract, newer)
    assert _mentions(problems, "info member", "uptime")


def test_a_setting_that_changes_side_is_refused(checker, contract):
    newer = copy.deepcopy(contract)
    newer["settings"]["publish_keys"]["writable"] = False
    problems = checker.compare(contract, newer)
    assert _mentions(problems, "setting", "publish_keys")


def test_a_removed_capability_is_refused(checker, contract):
    newer = copy.deepcopy(contract)
    newer["capabilities"].remove("volume")
    problems = checker.compare(contract, newer)
    assert _mentions(problems, "capability", "volume", "removed")


def test_a_new_major_allows_a_removal(checker, contract):
    newer = copy.deepcopy(contract)
    newer["contract"] = contract["contract"] + 1
    del newer["commands"]["reboot"]
    assert checker.compare(contract, newer) == []


def test_a_lower_major_is_refused(checker, contract):
    newer = copy.deepcopy(contract)
    newer["contract"] = contract["contract"] - 1
    assert _mentions(checker.compare(contract, newer), "contract major")


def test_a_plan_may_be_dropped(checker, contract):
    # A planned row is a promise about a shape, not a feature anybody can already rely on.
    newer = copy.deepcopy(contract)
    newer["planned"] = []
    assert checker.compare(contract, newer) == []


def test_a_setting_retyped_is_refused(checker, contract):
    newer = copy.deepcopy(contract)
    newer["settings"]["publish_keys"]["type"] = "int"
    problems = checker.compare(contract, newer)
    assert _mentions(problems, "setting", "publish_keys", "changed")


def test_a_new_setting_choice_is_an_addition(checker, contract):
    newer = copy.deepcopy(contract)
    newer["settings"]["screenshot"]["type"] = "enum(off|on_zap|interval|on_record)"
    assert checker.compare(contract, newer) == []


def test_a_setting_choice_taken_away_is_refused(checker, contract):
    newer = copy.deepcopy(contract)
    newer["settings"]["screenshot"]["type"] = "enum(off|on_zap)"
    problems = checker.compare(contract, newer)
    assert _mentions(problems, "setting", "screenshot", "interval")


def test_a_removed_outside_topic_is_refused(checker, contract):
    newer = copy.deepcopy(contract)
    newer["other_topics"] = [
        topic for topic in newer["other_topics"] if not topic.startswith("enigma2mqtt/")
    ]
    problems = checker.compare(contract, newer)
    assert _mentions(problems, "topic", "enigma2mqtt/discovery/<node>/config", "removed")


def test_a_dropped_exception_is_refused(checker, contract):
    newer = copy.deepcopy(contract)
    newer["exceptions"] = [
        row for row in newer["exceptions"] if row["name"] != "zap-moves-channel-list"
    ]
    assert _mentions(checker.compare(contract, newer), "exception", "zap-moves-channel-list")


def test_an_unreleased_exception_may_be_dated_once(checker, contract):
    newer = copy.deepcopy(contract)
    for row in newer["exceptions"]:
        if row["release"] == "unreleased":
            row["release"] = "0.4.0"
    assert checker.compare(contract, newer) == []
    redated = copy.deepcopy(newer)
    for row in redated["exceptions"]:
        if row["release"] == "0.4.0":
            row["release"] = "0.4.1"
    assert _mentions(checker.compare(newer, redated), "exception", "re-dated")


# ------------------------------------------------------ the command line, in git --
#
# The CI job runs `tools/check-contract.py --previous tag`. Everything the job relies on - the
# exit status, finding the tag, refusing to call "I could not compare" a pass - is exercised
# here through the real script in a throwaway repository, as a separate process.

GIT = ("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main")


def _git(repo, *args):
    subprocess.run([*GIT, *args], cwd=repo, check=True, capture_output=True)


def _write(repo, topics, contract_data, version="1.0.0"):
    (repo / "docs").mkdir(exist_ok=True)
    (repo / "docs" / "TOPICS.md").write_text(topics, encoding="utf-8")
    target = repo / "docs" / "contract.json"
    if contract_data is None:
        target.unlink(missing_ok=True)
    else:
        target.write_text(json.dumps(contract_data, indent=2) + "\n", encoding="utf-8")
    source = repo / "src" / "MQTTBridge"
    source.mkdir(parents=True, exist_ok=True)
    (source / "version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def _with_exceptions(topics_text, contract, rows):
    """The real pair, with its exception record replaced by `rows` - (name, release) - on both
    sides, so the two still agree."""
    lines = topics_text.splitlines(keepends=True)
    start = next(i for i, line in enumerate(lines) if line.startswith("| Exception | Release |"))
    end = start + 2
    while lines[end].startswith("|"):
        end += 1
    table = [f"| `{name}` | {release} | a change | nobody reads it |\n" for name, release in rows]
    changed = copy.deepcopy(contract)
    changed["exceptions"] = [{"name": name, "release": release} for name, release in rows]
    return "".join(lines[: start + 2] + table + lines[end:]), changed


# The record as the first release that carries the file would have it: history dated when it
# happened (at or before that release), nothing left "unreleased".
FIRST_RECORD = [("zap-moves-channel-list", "0.3.0"), ("timers-lists-finished", "1.0.0")]


def _commit(repo, message, tag=None):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    if tag:
        _git(repo, "tag", tag)


def _run(repo, *args):
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--root", str(repo), *args],
        capture_output=True, text=True,
    )
    return result.returncode, result.stdout + result.stderr


@pytest.fixture()
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    return tmp_path


def _without_reboot(topics_text, contract):
    lines = topics_text.splitlines(keepends=True)
    kept = [line for line in lines if not line.startswith("| `reboot` |")]
    assert len(kept) == len(lines) - 1
    smaller = copy.deepcopy(contract)
    del smaller["commands"]["reboot"]
    return "".join(kept), smaller


@pytest.fixture()
def released(topics_text, contract):
    return _with_exceptions(topics_text, contract, FIRST_RECORD)


def test_cli_a_removal_against_the_release_tag_fails(repo, released):
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    _write(repo, *_without_reboot(topics_text, contract))
    _commit(repo, "drop reboot")
    status, output = _run(repo, "--previous", "tag")
    assert status == 1, output
    assert "command 'reboot' was removed inside contract major 1" in output
    assert "against v1.0.0" in output


def test_cli_an_unchanged_contract_passes_against_the_tag(repo, released):
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    _commit(repo, "later")
    status, output = _run(repo, "--previous", "tag")
    assert status == 0, output
    assert "keeps every promise v1.0.0 made" in output


def test_cli_an_unknown_ref_cannot_be_a_pass(repo, topics_text, contract):
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    status, output = _run(repo, "--previous", "v9.9.9-typo")
    assert status == 2, output
    assert "v9.9.9-typo" in output and "not a commit" in output


def test_cli_no_tags_cannot_be_a_pass(repo, topics_text, contract):
    # What a checkout without `fetch-depth: 0` looks like to the job.
    _write(repo, topics_text, contract)
    _commit(repo, "untagged")
    status, output = _run(repo, "--previous", "tag")
    assert status == 2, output
    assert "no release tag is reachable" in output


def test_cli_nothing_to_compare_only_before_any_tag_carries_the_file(
    repo, topics_text, contract
):
    # version.py stays at the last release on main: only a release pull request bumps it.
    _write(repo, topics_text, None, version="0.3.0")
    _commit(repo, "before the contract file", tag="v0.3.0")
    _write(repo, topics_text, contract, version="0.3.0")
    _commit(repo, "the contract file")
    status, output = _run(repo, "--previous", "tag")
    assert status == 0, output
    assert "no release tag carries docs/contract.json yet (checked v0.3.0)" in output


def test_cli_a_newer_tag_without_the_file_fails(repo, released):
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    _write(repo, topics_text, None)
    _commit(repo, "the file went missing", tag="v1.1.0")
    _write(repo, topics_text, contract)
    _commit(repo, "the file is back")
    status, output = _run(repo, "--previous", "tag")
    assert status == 2, output
    assert "v1.1.0" in output and "although v1.0.0 did" in output


def test_cli_an_explicit_ref_without_the_file_fails(repo, topics_text, contract):
    _write(repo, topics_text, None)
    _commit(repo, "old", tag="v0.3.0")
    _write(repo, topics_text, contract)
    status, output = _run(repo, "--previous", "v0.3.0")
    assert status == 2, output
    assert "carries no docs/contract.json" in output


def test_cli_a_disagreement_alone_exits_one(repo, topics_text, contract):
    broken = copy.deepcopy(contract)
    del broken["commands"]["uninstall"]
    _write(repo, topics_text, broken)
    status, output = _run(repo)
    assert status == 1, output
    assert "command 'uninstall' is in docs/TOPICS.md but not in docs/contract.json" in output


def test_cli_an_unknown_option_exits_two(repo):
    status, _output = _run(repo, "--previus", "tag")
    assert status == 2


# ----------------------------------------------- the exception record keeps its dates --
#
# Criterion 4 of TOPICS.md's exceptions: a change of meaning is decided in the pull request that
# makes it, and the record says which release shipped it. A record that can be back-dated says
# nothing. The rule: the first release that carries contract.json dates its history when it
# happened; after that, a new exception says "unreleased" until the release that ships it, which
# dates it to itself; a release tag never says "unreleased"; a date never changes.


# This project's own history, rebuilt: releases without contract.json, then the pull request
# that brings it, then the release pull request that dates the record, then that release's tag
# on HEAD - the first release carrying the file. The release workflow runs the check there.


def _real_record(topics_text, contract, unreleased_as):
    rows = [
        (row["name"], unreleased_as if row["release"] == "unreleased" else row["release"])
        for row in contract["exceptions"]
    ]
    assert any(row["release"] == "unreleased" for row in contract["exceptions"])
    return _with_exceptions(topics_text, contract, rows)


def _history_before_the_first_carrying_release(repo, topics_text, contract):
    for version in ("0.1.0", "0.2.0", "0.3.0"):
        _write(repo, topics_text, None, version=version)
        _commit(repo, version, tag=f"v{version}")
    _write(repo, topics_text, contract, version="0.3.0")
    _commit(repo, "the contract lands, unreleased exceptions and all")
    status, output = _run(repo, "--previous", "tag")
    assert status == 0, output


@pytest.mark.parametrize("annotated", [False, True], ids=["lightweight", "annotated"])
def test_cli_the_first_release_carrying_the_file_can_be_released(
    repo, topics_text, contract, annotated
):
    _history_before_the_first_carrying_release(repo, topics_text, contract)
    _write(repo, *_real_record(topics_text, contract, "0.4.0"), version="0.4.0")
    _commit(repo, "the release pull request")
    status, output = _run(repo, "--previous", "tag")
    assert status == 0, output
    if annotated:
        _git(repo, "tag", "-a", "v0.4.0", "-m", "0.4.0")
    else:
        _git(repo, "tag", "v0.4.0")
    status, output = _run(repo, "--previous", "tag")
    assert status == 0, output
    _commit(repo, "main after the release")
    status, output = _run(repo, "--previous", "tag")
    assert status == 0, output
    assert "keeps every promise v0.4.0 made" in output


def test_cli_the_first_carrying_release_still_saying_unreleased_is_refused(
    repo, topics_text, contract
):
    _history_before_the_first_carrying_release(repo, topics_text, contract)
    _write(repo, topics_text, contract, version="0.4.0")
    _commit(repo, "a release pull request that forgot the dates")
    _git(repo, "tag", "v0.4.0")
    status, output = _run(repo, "--previous", "tag")
    assert status == 1, output
    assert "release v0.4.0 carries exception 'timers-lists-finished' still marked unreleased" in (
        output
    )


def test_cli_a_release_pull_request_must_date_its_exceptions(repo, topics_text, contract):
    # version.py above the previous release tag is a release on its way: its exceptions are
    # dated now, in the pull request, not discovered when the tag is pushed.
    _history_before_the_first_carrying_release(repo, topics_text, contract)
    _write(repo, topics_text, contract, version="0.4.0")
    _commit(repo, "a release pull request that forgot the dates")
    status, output = _run(repo, "--previous", "tag")
    assert status == 1, output
    assert "timers-lists-finished" in output and "0.4.0" in output


def test_cli_a_release_pull_request_after_a_carrying_release_must_date_too(repo, released):
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    later = FIRST_RECORD + [("next-meaning-change", "unreleased")]
    _write(repo, *_with_exceptions(topics_text, contract, later), version="1.1.0")
    _commit(repo, "the 1.1.0 release pull request")
    status, output = _run(repo, "--previous", "tag")
    assert status == 1, output
    assert "next-meaning-change" in output and "1.1.0" in output


def test_cli_the_first_carrying_release_cannot_date_after_itself(repo, topics_text, contract):
    record = [("zap-moves-channel-list", "0.3.0"), ("from-the-future", "2.0.0")]
    _write(repo, *_with_exceptions(topics_text, contract, record))
    _commit(repo, "release", tag="v1.0.0")
    _commit(repo, "later")
    status, output = _run(repo, "--previous", "tag")
    assert status == 1, output
    assert "'from-the-future' in v1.0.0, the first release carrying the record" in output


def test_cli_a_tag_on_head_that_is_the_only_tag_has_nothing_earlier(repo, released):
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "the very first release", tag="v1.0.0")
    status, output = _run(repo, "--previous", "tag")
    assert status == 0, output
    assert "nothing earlier to compare" in output


def test_cli_a_new_exception_back_dated_is_refused(repo, released):
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    later = FIRST_RECORD + [("sneaky-meaning-change", "0.3.0")]
    _write(repo, *_with_exceptions(topics_text, contract, later), version="1.1.0")
    _commit(repo, "a change of meaning")
    status, output = _run(repo, "--previous", "tag")
    assert status == 1, output
    assert "sneaky-meaning-change" in output


def test_cli_a_new_exception_dated_to_the_previous_release_is_refused(repo, released):
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    later = FIRST_RECORD + [("sneaky-meaning-change", "1.0.0")]
    _write(repo, *_with_exceptions(topics_text, contract, later), version="1.1.0")
    _commit(repo, "a change of meaning")
    status, output = _run(repo, "--previous", "tag")
    assert status == 1, output
    assert "sneaky-meaning-change" in output


def test_cli_a_release_that_forgot_its_version_bump_cannot_date_back(repo, released):
    # version.py still says 1.0.0 after the 1.0.0 tag: "the shipping version" is no guide then,
    # and the previous release is the one that refuses the date.
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    later = FIRST_RECORD + [("sneaky-meaning-change", "1.0.0")]
    _write(repo, *_with_exceptions(topics_text, contract, later), version="1.0.0")
    _commit(repo, "a change of meaning, no bump")
    status, output = _run(repo, "--previous", "tag")
    assert status == 1, output
    assert "not later than the previous release 1.0.0" in output


def test_cli_an_exception_dated_before_the_tag_it_first_appears_in_is_refused(repo, released):
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    later = FIRST_RECORD + [("late-meaning-change", "1.0.0")]
    _write(repo, *_with_exceptions(topics_text, contract, later), version="1.1.0")
    _commit(repo, "release", tag="v1.1.0")
    _commit(repo, "later")
    status, output = _run(repo, "--previous", "tag")
    assert status == 1, output
    assert "late-meaning-change" in output and "v1.1.0" in output


def test_cli_a_release_tag_still_saying_unreleased_is_refused(repo, released):
    topics_text, contract = released
    left_open = FIRST_RECORD + [("forgotten-date", "unreleased")]
    _write(repo, *_with_exceptions(topics_text, contract, left_open))
    _commit(repo, "release", tag="v1.0.0")
    dated = FIRST_RECORD + [("forgotten-date", "1.1.0")]
    _write(repo, *_with_exceptions(topics_text, contract, dated), version="1.1.0")
    _commit(repo, "the next release")
    status, output = _run(repo, "--previous", "tag")
    assert status == 1, output
    assert "carries exception 'forgotten-date' still marked unreleased" in output


def test_cli_unreleased_dated_to_an_old_release_is_refused(repo, released):
    # Against a commit that is not a release, where "unreleased" is legitimate.
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    work = FIRST_RECORD + [("pending-change", "unreleased")]
    _write(repo, *_with_exceptions(topics_text, contract, work), version="1.1.0")
    _commit(repo, "work in progress")
    _git(repo, "branch", "work")
    dated = FIRST_RECORD + [("pending-change", "0.1.0")]
    _write(repo, *_with_exceptions(topics_text, contract, dated), version="1.1.0")
    status, output = _run(repo, "--previous", "work")
    assert status == 1, output
    assert "pending-change" in output


def test_cli_a_tagged_checkout_saying_unreleased_is_refused(repo, released):
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    left_open = FIRST_RECORD + [("forgotten-date", "unreleased")]
    _write(repo, *_with_exceptions(topics_text, contract, left_open), version="1.1.0")
    _commit(repo, "release", tag="v1.1.0")
    status, output = _run(repo, "--previous", "tag")
    assert status == 1, output
    assert "carries exception 'forgotten-date' still marked unreleased" in output


def test_cli_a_new_exception_unreleased_passes(repo, released):
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    later = FIRST_RECORD + [("next-meaning-change", "unreleased")]
    _write(repo, *_with_exceptions(topics_text, contract, later), version="1.0.0")
    _commit(repo, "a change of meaning")
    status, output = _run(repo, "--previous", "tag")
    assert status == 0, output


def test_cli_the_release_dates_its_exceptions_to_itself(repo, released):
    topics_text, contract = released
    _write(repo, topics_text, contract)
    _commit(repo, "release", tag="v1.0.0")
    shipped = FIRST_RECORD + [("next-meaning-change", "1.1.0")]
    _write(repo, *_with_exceptions(topics_text, contract, shipped), version="1.1.0")
    _commit(repo, "the release pull request")
    # The release pull request, before the tag exists...
    status, output = _run(repo, "--previous", "tag")
    assert status == 0, output
    # ...and the tagged release itself, compared with the release before it.
    _git(repo, "tag", "v1.1.0")
    status, output = _run(repo, "--previous", "tag")
    assert status == 0, output
    assert "against v1.0.0" in output or "every promise v1.0.0 made" in output
