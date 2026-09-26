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
    # The same enumeration test_discovery_templates.every_capability() makes, which its own
    # test keeps complete: one per publisher, plus the three no publisher stands behind.
    from MQTTBridge import bridge, zaphistory
    from MQTTBridge.publishers import PUBLISHER_CLASSES

    names = set(bridge.CORE_CAPABILITIES)
    names.update(publisher.name for publisher in PUBLISHER_CLASSES if publisher.name)
    names.update({bridge.MESSAGE_CAPABILITY, bridge.UNINSTALL_CAPABILITY})
    names.add(zaphistory.CLEAR_CAPABILITY)
    assert sorted(names) == contract["capabilities"]


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
