"""A command's id: how it arrives, what it may be, and which refusal says it back.

The id comes in an envelope around the command's own payload and goes out on
`last_error`, on a refusal only. Two things are held here. A command without an
envelope is handled exactly as it was before there was one. And a refusal that
is published after its command returned - a zap that never landed, a `grab`
that failed - carries the id of that command or none, whichever command is
being handled at that moment.

The paths that need a softcam, a package manager or an update helper are in
`test_softcam.py`, `test_uninstall.py` and `test_selfupdate.py`.
"""

import json

import pytest
from conftest import POLSAT, TVN, TVP1, ConsoleAppContainer, MainLoop

from MQTTBridge import commands, log
from MQTTBridge import service as service_module
from MQTTBridge.origin import PAGE

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
INFO = ROOT + "/info"
LAST_ERROR = ROOT + "/last_error"
ANNOUNCEMENT = "enigma2mqtt/discovery/" + NODE + "/config"


def wrapped(command_id, payload=""):
    return json.dumps({"id": command_id, "payload": payload}).encode("utf-8")


def send(factory, name, payload=b"PRESS"):
    factory.client.fire_message(ROOT + "/cmd/" + name, payload)


def refusal(factory):
    entry = factory.client.last(LAST_ERROR)
    return None if entry is None or entry.text == "" else entry.json()


# ---------------------------------------------------------------- the envelope --


@pytest.mark.parametrize("text", [
    "",
    "PRESS",
    "  Integration  ",
    "42",
    '{"sref": "1:0:1:1:1:1:1:0:0:0:"}',
    '{"id": "a1"}',
    '{"payload": "on"}',
    '{"id": "a1", "payload": "on", "more": 1}',
    '{"ID": "a1", "payload": "on"}',
    '["a1", "on"]',
    '{"id": "a1", "payload": "on"',
    '"{\\"id\\": \\"a1\\", \\"payload\\": \\"on\\"}"',
])
def test_a_payload_that_is_not_an_envelope_reaches_its_handler_untouched(text):
    inner, command_id, problem = commands.unwrap(text)
    assert inner is text
    assert command_id is None
    assert problem is None


def test_an_envelopes_string_is_the_commands_text_as_it_was_written():
    text = json.dumps({"id": "a1", "payload": "  Integration \n"})
    assert commands.unwrap(text) == ("  Integration \n", "a1", None)


@pytest.mark.parametrize("payload, text", [
    (None, ""),
    (40, "40"),
    (True, "true"),
    ({"name": "TVN", "long": False}, '{"name": "TVN", "long": false}'),
    ({"text": "za\u017c\u00f3\u0142\u0107"}, '{"text": "za\u017c\u00f3\u0142\u0107"}'),
])
def test_an_envelopes_json_value_is_handed_on_as_its_json_text(payload, text):
    inner, command_id, problem = commands.unwrap(json.dumps({"id": "a1", "payload": payload}))
    assert (inner, command_id, problem) == (text, "a1", None)


def test_whitespace_around_an_envelope_does_not_hide_it():
    assert commands.unwrap(' \n{"payload": "on", "id": "a1"}\n ') == ("on", "a1", None)


def test_an_envelope_is_opened_once():
    inside = {"id": "inner", "payload": "on"}
    inner, command_id, _problem = commands.unwrap(json.dumps({"id": "outer", "payload": inside}))
    assert command_id == "outer"
    assert json.loads(inner) == inside


def test_an_envelope_nested_past_what_the_parser_follows_is_the_handlers_to_judge():
    deep = '{"id": "a1", "payload": ' + "[" * 100000 + "]" * 100000 + "}"
    assert commands.unwrap(deep) == (deep, None, None)


def test_a_value_the_encoder_cannot_follow_is_the_handlers_to_judge(monkeypatch):
    text = json.dumps({"id": "a1", "payload": {"a": 1}})

    def too_deep(*args, **kwargs):
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(commands.json, "dumps", too_deep)
    assert commands.unwrap(text) == (text, None, None)


# ---------------------------------------------------------- what an id may be --


@pytest.mark.parametrize("command_id", [
    "a",
    "0",
    "a1b2c3d4e5f6",
    "7f9c2e4a-3b1d-4c8e-9f60-2d5a1b7c8e90",
    "01JABCDEFGHJKMNPQRSTVWXYZ0",
    "ha:living_room.zap-17",
    "x" * 64,
])
def test_an_id_of_the_documented_shape_is_taken(command_id):
    assert commands.id_problem(command_id) is None
    assert commands.clean_id(command_id) == command_id
    assert commands.unwrap(json.dumps({"id": command_id, "payload": "on"})) == (
        "on", command_id, None)


@pytest.mark.parametrize("command_id", [
    "",
    "x" * 65,
    "two words",
    " a1",
    "a1\n",
    "a\tb",
    'a"b',
    "a\\b",
    "a/b",
    "a+b=",
    "<b>a</b>",
    "a%sb",
    "z\u00f3\u0142w",
    "\uff41\uff11",
    "a" + chr(0x200B) + "b",
    17,
    1.5,
    True,
    None,
    ["a1"],
    {"a": 1},
])
def test_an_id_of_another_shape_is_dropped_and_the_command_is_still_opened(command_id):
    assert commands.id_problem(command_id) is not None
    assert commands.clean_id(command_id) is None
    inner, taken, problem = commands.unwrap(json.dumps({"id": command_id, "payload": "on"}))
    assert inner == "on"
    assert taken is None
    assert problem == commands.id_problem(command_id)


def test_the_reason_an_id_is_dropped_never_holds_the_id():
    for command_id in ("two words", "x" * 65, "<b>a</b>", "z\u00f3\u0142w"):
        assert command_id not in commands.id_problem(command_id)


def test_an_id_that_holds_a_registered_secret_is_dropped(isolated_log):
    log.register_secret("hunter2")
    for command_id in ("hunter2", "a-hunter2", "hunter2.b", "a:hunter2:b"):
        assert commands.clean_id(command_id) is None
    # A piece of the secret is not looked for: that test would spell the
    # secret out, a character at a time, to whoever kept asking.
    assert commands.clean_id("hunter") == "hunter"
    assert commands.clean_id("unter2") == "unter2"


# ------------------------------------------------------------- the capability --


def test_the_capability_is_claimed_by_a_bridge_that_has_bound_nothing(connected_bridge, factory):
    assert connected_bridge.capabilities() == ["command_id"]
    assert factory.client.last(INFO).json()["capabilities"] == ["command_id"]
    assert factory.client.last(ANNOUNCEMENT).json()["capabilities"] == ["command_id"]


def test_the_capability_is_claimed_next_to_every_feature_area(live_bridge):
    names = live_bridge.capabilities()
    assert names.count("command_id") == 1
    assert "power" in names


# ------------------------------------------------- refusals the dispatcher makes --


def test_a_refused_command_says_its_id_back(connected_bridge, factory):
    send(factory, "ha_mode", wrapped("a1b2c3", "sometimes"))

    payload = refusal(factory)
    assert payload["id"] == "a1b2c3"
    assert payload["cmd"] == "ha_mode"
    assert "sometimes" in payload["error"]
    assert sorted(payload) == ["cmd", "error", "id", "ts"]


def test_a_refusal_without_an_id_is_the_payload_it_always_was(connected_bridge, factory,
                                                              monkeypatch):
    monkeypatch.setattr("MQTTBridge.bridge.time.time", lambda: 1789459213)
    send(factory, "ha_mode", b"sometimes")

    assert factory.client.last(LAST_ERROR).text == (
        '{"cmd":"ha_mode","error":"unknown ha_mode \'sometimes\'; expected one of '
        'discovery, integration, off","ts":1789459213}')


def test_an_unknown_command_says_its_id_back(connected_bridge, factory):
    send(factory, "teleport", wrapped("a1b2c3", "PRESS"))

    payload = refusal(factory)
    assert (payload["cmd"], payload["error"], payload["id"]) == (
        "teleport", "unknown command", "a1b2c3")


def test_a_handler_that_raises_says_the_id_back(connected_bridge, factory, monkeypatch):
    def explode(mode):
        raise RuntimeError("enigma2 said no")

    monkeypatch.setattr(connected_bridge, "set_ha_mode", explode)
    send(factory, "ha_mode", wrapped("a1b2c3", "integration"))

    payload = refusal(factory)
    assert "RuntimeError" in payload["error"]
    assert payload["id"] == "a1b2c3"
    assert connected_bridge.command_id is None


def test_a_refusal_with_a_reason_code_keeps_it_next_to_the_id(connected_bridge, factory):
    send(factory, "update_check", wrapped("a1b2c3", "PRESS"))

    payload = refusal(factory)
    assert payload["reason"] == "not_permitted"
    assert payload["id"] == "a1b2c3"


def test_an_enveloped_command_does_what_the_bare_one_does(connected_bridge, factory, settings):
    factory.client.clear()
    send(factory, "ha_mode", wrapped("a1b2c3", "  Integration  "))

    assert settings.ha_mode.value == "integration"
    assert factory.client.last(INFO).json()["ha_mode"] == "integration"
    # No acknowledgement: a command that worked says its id nowhere.
    assert LAST_ERROR not in factory.client.topics()
    assert all("a1b2c3" not in entry.text for entry in factory.client.published)


def test_a_command_that_ignores_its_payload_takes_an_envelope(connected_bridge, factory):
    send(factory, "teleport", b"PRESS")
    factory.client.clear()

    send(factory, "discovery", wrapped("a1b2c3", "PRESS"))

    assert factory.client.last(ANNOUNCEMENT) is not None
    assert factory.client.last(LAST_ERROR).text == ""


def test_an_envelopes_object_reaches_a_handler_that_reads_json(live_bridge, factory):
    send(factory, "zap", wrapped("a1b2c3", {"name": "nonexistent"}))

    payload = refusal(factory)
    assert "no channel called 'nonexistent'" in payload["error"]
    assert payload["id"] == "a1b2c3"


def test_an_envelopes_number_reaches_a_handler_that_reads_text(live_bridge, factory, receiver):
    send(factory, "volume", wrapped("a1b2c3", 40))
    assert refusal(factory) is None
    assert receiver.volume.getVolume() == 40


def test_a_command_with_an_id_that_cannot_be_used_runs_and_its_refusal_has_none(
        connected_bridge, factory, settings, plugin_log):
    send(factory, "ha_mode", wrapped("not an id", "integration"))
    assert settings.ha_mode.value == "integration"

    send(factory, "ha_mode", wrapped("not an id", "sometimes"))
    payload = refusal(factory)
    assert "sometimes" in payload["error"]
    assert "id" not in payload
    assert "cmd/ha_mode: the command's id is dropped" in plugin_log()
    assert "not an id" not in plugin_log()


def test_an_id_that_is_the_broker_password_is_not_said_back(connected_bridge, factory,
                                                            plugin_log):
    log.register_secret("Tr0ub4dor-3")
    send(factory, "ha_mode", wrapped("Tr0ub4dor-3", "sometimes"))

    assert "id" not in refusal(factory)
    assert "Tr0ub4dor-3" not in factory.client.last(LAST_ERROR).text
    assert "Tr0ub4dor-3" not in plugin_log()


def test_the_id_is_in_the_log_next_to_its_command(connected_bridge, factory, plugin_log):
    send(factory, "ha_mode", wrapped("a1b2c3", "sometimes"))

    text = plugin_log()
    assert "cmd/ha_mode (id a1b2c3)" in text
    assert "cmd/ha_mode (id a1b2c3) refused" in text


def test_the_size_limit_counts_the_envelope(connected_bridge, factory, settings, plugin_log):
    factory.client.clear()
    send(factory, "ha_mode", wrapped("a1b2c3", "integration".ljust(4090)))

    assert settings.ha_mode.value == "discovery"
    assert factory.client.published == []
    assert "over the 4096 byte limit" in plugin_log()


def test_a_command_from_the_page_is_never_read_as_an_envelope(connected_bridge, factory):
    text = wrapped("a1b2c3", "integration").decode("utf-8")
    sentence = connected_bridge.run_command("ha_mode", text, PAGE)

    assert "unknown ha_mode" in sentence
    assert "id" not in refusal(factory)


def test_the_relays_answer_is_never_read_as_an_envelope(connected_bridge, factory, monkeypatch):
    taken = []
    monkeypatch.setattr(connected_bridge.self_update, "on_relay", taken.append)
    answer = wrapped("a1b2c3", {"id": "a1b2c3d4e5f6", "version": "0.4.1"})
    send(factory, "relay", answer)

    assert taken == [answer.decode("utf-8")]


def test_the_id_is_the_commands_only_while_its_handler_runs(connected_bridge, factory,
                                                           monkeypatch):
    seen = []
    monkeypatch.setattr(connected_bridge, "set_ha_mode",
                        lambda mode: seen.append(connected_bridge.command_id))
    assert connected_bridge.command_id is None

    send(factory, "ha_mode", wrapped("a1b2c3", "integration"))
    send(factory, "ha_mode", b"integration")
    send(factory, "ha_mode", wrapped("d4e5f6", "integration"))

    assert seen == ["a1b2c3", None, "d4e5f6"]
    assert connected_bridge.command_id is None


def test_a_page_command_run_inside_a_broker_command_has_no_id(connected_bridge, factory,
                                                             monkeypatch):
    """One handler calling into another is not how the plugin works; the id still nests."""
    seen = []

    def nested(mode):
        seen.append(connected_bridge.command_id)
        seen.append(connected_bridge.run_command("ha_mode", "sometimes", PAGE) is None)
        seen.append(connected_bridge.command_id)

    monkeypatch.setattr(connected_bridge, "set_ha_mode", nested)
    send(factory, "ha_mode", wrapped("a1b2c3", "integration"))

    assert seen == ["a1b2c3", False, "a1b2c3"]
    assert "id" not in [entry.json() for entry in factory.client.all_for(LAST_ERROR)
                        if entry.text][-1]


# ------------------------------------------------- where the id meets the broker --


def test_last_error_takes_an_id_only_of_the_documented_shape(connected_bridge, factory):
    """An id that crossed a restart was read from a file: it is held to the rule again."""
    connected_bridge.publish_last_error("update", "it failed", command_id="a1b2c3")
    assert refusal(factory)["id"] == "a1b2c3"

    for command_id in ("not an id", "x" * 65, "", 17, ["a1b2c3"], None):
        connected_bridge.publish_last_error("update", "it failed", command_id=command_id)
        assert "id" not in refusal(factory)


def test_last_error_does_not_take_an_id_that_holds_a_secret(connected_bridge, factory):
    log.register_secret("hunter2")
    connected_bridge.publish_last_error("update", "it failed", command_id="a-hunter2")
    assert "id" not in refusal(factory)


def test_a_refusal_kept_for_the_next_connect_keeps_its_id(connected_bridge, factory):
    connected_bridge.say_at_connect("update", "the previous version 0.4.0 is back", "a1b2c3")
    send(factory, "ha_mode", wrapped("d4e5f6", "integration"))
    factory.client.fire_disconnect(7)
    factory.client.clear()

    factory.client.fire_connect()

    payload = refusal(factory)
    assert payload["id"] == "a1b2c3"
    assert payload["cmd"] == "update"


def test_a_refusal_kept_for_the_next_connect_without_an_id_has_none(connected_bridge, factory):
    connected_bridge.say_at_connect("update", "the previous version 0.4.0 is back")
    factory.client.fire_disconnect(7)
    factory.client.clear()

    factory.client.fire_connect()

    assert sorted(refusal(factory)) == ["cmd", "error", "ts"]


# -------------------------------------------- refusals that come after the command --


def test_a_zap_that_never_lands_says_the_id_of_its_own_command(live_bridge, factory, receiver):
    send(factory, "zap", wrapped("zap-1", TVN))
    # Two commands later, one of them with an id of its own.
    send(factory, "discovery", wrapped("other-2", "PRESS"))
    send(factory, "discovery", b"PRESS")
    receiver.nav.sref = TVP1

    live_bridge.publisher("service")._verify.timer.fire()

    payload = refusal(factory)
    assert "did not tune to" in payload["error"]
    assert payload["id"] == "zap-1"


def test_a_zap_without_an_id_that_never_lands_takes_nobody_elses(live_bridge, factory, receiver):
    send(factory, "zap", TVN.encode("utf-8"))
    send(factory, "discovery", wrapped("other-2", "PRESS"))
    receiver.nav.sref = TVP1

    live_bridge.publisher("service")._verify.timer.fire()

    payload = refusal(factory)
    assert "did not tune to" in payload["error"]
    assert "id" not in payload


def test_a_late_refusal_without_an_id_does_not_fall_back_on_the_command_being_handled(
        live_bridge, factory, receiver):
    """Timers do not fire inside a handler. Were one to, the answer is still the zap's own."""
    send(factory, "zap", TVN.encode("utf-8"))
    receiver.nav.sref = TVP1
    verify = live_bridge.publisher("service")._verify.timer
    live_bridge._commands.handlers["discovery"] = lambda text, origin=None: verify.fire()

    send(factory, "discovery", wrapped("other-2", "PRESS"))

    payloads = [entry.json() for entry in factory.client.all_for(LAST_ERROR) if entry.text]
    assert "did not tune to" in payloads[-1]["error"]
    assert "id" not in payloads[-1]


def test_a_newer_zap_takes_the_watch_and_its_id_with_it(live_bridge, factory, receiver):
    send(factory, "zap", wrapped("zap-1", TVN))
    send(factory, "zap", wrapped("zap-2", POLSAT))
    receiver.nav.sref = TVN

    live_bridge.publisher("service")._verify.timer.fire()

    payload = refusal(factory)
    assert "did not tune to " + POLSAT in payload["error"]
    assert payload["id"] == "zap-2"


def test_a_newer_zap_without_an_id_leaves_the_older_zaps_id_behind(live_bridge, factory,
                                                                  receiver):
    send(factory, "zap", wrapped("zap-1", TVN))
    send(factory, "zap", POLSAT.encode("utf-8"))
    receiver.nav.sref = TVN

    live_bridge.publisher("service")._verify.timer.fire()

    payload = refusal(factory)
    assert "did not tune to " + POLSAT in payload["error"]
    assert "id" not in payload


def test_a_standby_that_never_ends_says_the_id_of_the_zap_that_waited(live_bridge, factory,
                                                                     receiver):
    receiver.with_channel_list([TVP1, TVN])
    receiver.enter_standby(restoring=True)
    send(factory, "zap", wrapped("zap-1", TVN))
    send(factory, "discovery", wrapped("other-2", "PRESS"))

    MainLoop.advance(service_module.WAKE_WAIT_MILLISECONDS)

    payload = refusal(factory)
    assert payload["error"] == "the receiver did not leave standby"
    assert (payload["cmd"], payload["id"]) == ("zap", "zap-1")


def test_a_zap_refused_after_the_wake_says_the_id_of_its_own_command(live_bridge, factory,
                                                                    receiver):
    receiver.with_channel_list([TVP1, TVN])
    screen = receiver.enter_standby(restoring=True)
    send(factory, "zap", wrapped("zap-1", TVN))
    send(factory, "discovery", wrapped("other-2", "PRESS"))
    receiver.session.nav = None
    screen.finish_close()

    MainLoop.advance(0)

    payload = refusal(factory)
    assert payload["error"] == service_module.NO_SESSION
    assert payload["id"] == "zap-1"


def test_a_zap_made_after_the_wake_is_watched_under_its_own_id(live_bridge, factory, receiver):
    receiver.with_channel_list([TVP1, TVN])
    screen = receiver.enter_standby(restoring=True)
    send(factory, "zap", wrapped("zap-1", TVN))
    screen.finish_close()
    # The zap is made on the next turn, and another command is handled first.
    send(factory, "discovery", wrapped("other-2", "PRESS"))
    MainLoop.advance(0)
    receiver.nav.sref = TVP1

    live_bridge.publisher("service")._verify.timer.fire()

    payload = refusal(factory)
    assert "did not tune to" in payload["error"]
    assert payload["id"] == "zap-1"


def screenshots(bridge, tmp_path):
    found = bridge.publisher("screenshot")
    found.path = str(tmp_path / "mqttbridge.jpg")
    return found


def test_a_grab_that_fails_says_the_id_of_the_command_that_asked(live_bridge, factory,
                                                                tmp_path):
    screenshots(live_bridge, tmp_path)
    send(factory, "screenshot", wrapped("shot-1", "PRESS"))
    send(factory, "discovery", wrapped("other-2", "PRESS"))

    ConsoleAppContainer.instances[-1].finish(1)

    payload = refusal(factory)
    assert "exited with code 1" in payload["error"]
    assert (payload["cmd"], payload["id"]) == ("screenshot", "shot-1")


def test_a_grab_that_wrote_nothing_says_the_id_of_the_command_that_asked(live_bridge, factory,
                                                                        tmp_path):
    screenshots(live_bridge, tmp_path)
    send(factory, "screenshot", wrapped("shot-1", "PRESS"))

    ConsoleAppContainer.instances[-1].finish(0)

    payload = refusal(factory)
    assert "no image" in payload["error"]
    assert payload["id"] == "shot-1"


def test_a_picture_over_the_limit_says_the_id_of_the_command_that_asked(live_bridge, factory,
                                                                       tmp_path, monkeypatch):
    from MQTTBridge import screen as screen_module

    monkeypatch.setattr(screen_module, "MAX_BYTES", 8)
    found = screenshots(live_bridge, tmp_path)
    send(factory, "screenshot", wrapped("shot-1", "PRESS"))
    with open(found.path, "wb") as handle:
        handle.write(b"\xff\xd8\xff\xe0" + b"picture" * 100)

    ConsoleAppContainer.instances[-1].finish(0)

    payload = refusal(factory)
    assert "over the" in payload["error"]
    assert payload["id"] == "shot-1"


def test_a_grab_without_an_id_that_fails_takes_nobody_elses(live_bridge, factory, tmp_path):
    screenshots(live_bridge, tmp_path)
    send(factory, "screenshot", b"PRESS")
    send(factory, "discovery", wrapped("other-2", "PRESS"))

    ConsoleAppContainer.instances[-1].finish(1)

    assert sorted(refusal(factory)) == ["cmd", "error", "ts"]


def test_the_next_grab_does_not_inherit_the_id_of_the_one_before(live_bridge, factory,
                                                                tmp_path, monkeypatch):
    from MQTTBridge import screen as screen_module

    now = [1000.0]
    monkeypatch.setattr(screen_module.time, "time", lambda: now[0])
    found = screenshots(live_bridge, tmp_path)
    send(factory, "screenshot", wrapped("shot-1", "PRESS"))
    ConsoleAppContainer.instances[-1].finish(1)
    assert refusal(factory)["id"] == "shot-1"
    now[0] += screen_module.MINIMUM_INTERVAL_SECONDS + 1

    assert found.capture(commanded=True) is None
    ConsoleAppContainer.instances[-1].finish(1)

    assert "id" not in refusal(factory)
