"""The menu creator's receiver half: the catalogue, the edits and their answers.

The companion integration publishes a catalogue on `enigma2mqtt/menu_catalogue/<node>`; the
OpenWebif page's save is published on `<base>/<node>/menu_edit`; the integration answers on
`enigma2mqtt/menu_edit_result/<node>` (docs/TOPICS.md, section 3, "The menu creator"). These tests
drive the receiver half through the fake broker, as the integration would.
"""

import json
import re

import pytest
from conftest import MainLoop
from test_menu import NODE, World

from MQTTBridge import menueditor, webif
from MQTTBridge.menueditor import (
    InvalidCatalogue,
    NewerCatalogue,
    encode_edit,
    parse_answer,
    parse_catalogue,
)

CATALOGUE_TOPIC = "enigma2mqtt/menu_catalogue/" + NODE
RESULT_TOPIC = "enigma2mqtt/menu_edit_result/" + NODE
EDIT_TOPIC = "enigma2/" + NODE + "/menu_edit"
CONFIG_TOPIC = "enigma2/" + NODE + "/cmd/config"
LAST_ERROR = "enigma2/" + NODE + "/last_error"
REVISION = "3f9c2e4a1b7d5c60"
NEW_REVISION = "a1b2c3d4e5f60718"


def catalogue(**changes):
    """The contract's example, as the integration publishes it."""
    raw = {
        "v": 1,
        "revision": REVISION,
        "areas": ["Kitchen", "Living room"],
        "left_out": 0,
        "menu": [
            {"type": "group", "id": "g_0a1b2c3d4e", "label": "Living room", "icon": "sofa",
             "rows": [
                 {"type": "row", "id": "r_5f6a7b8c9d", "entity": "light.living_room",
                  "control": "slider", "label": None, "icon": None}]},
            {"type": "row", "id": "r_1a2b3c4d5e", "entity": "scene.movie_night",
             "control": "button", "label": "Movie", "icon": "movie"},
        ],
        "entities": [
            {"e": "light.living_room", "n": "Living room lamp", "a": 1,
             "c": ["toggle", "slider", "indicator"]},
            {"e": "sensor.outdoor_temperature", "n": "Outdoor temperature", "a": None,
             "c": ["indicator"], "h": True, "k": "diagnostic"},
        ],
    }
    raw.update(changes)
    return raw


def encoded(raw):
    return json.dumps(raw, ensure_ascii=False).encode("utf-8")


def a_menu():
    return [{"type": "group", "id": None, "label": "Garden", "icon": "garden", "rows": []}]


@pytest.fixture
def world(make_bridge, factory, settings):
    webif.mounted()
    return World(make_bridge, factory, settings)


def editor(world):
    return world.publisher.editor


def edits(world):
    return [json.loads(entry.text) for entry in world.client.all_for(EDIT_TOPIC)]


def answer(world, ident, result="saved", retain=False, **fields):
    payload = {"id": ident, "result": result, "revision": NEW_REVISION, "path": None,
               "detail": None}
    payload.update(fields)
    world.client.fire_message(RESULT_TOPIC, json.dumps(payload).encode("utf-8"), retain=retain)


def submit(world, menu=None, base=REVISION):
    answers = []
    refusal = editor(world).submit(base, a_menu() if menu is None else menu, answers.append)
    return refusal, answers


# --------------------------------------------------------------- subscriptions --


def test_the_catalogue_and_the_answers_are_subscribed_with_the_menu_at_qos_1(world):
    assert (CATALOGUE_TOPIC, 1) in world.client.subscriptions
    assert (RESULT_TOPIC, 1) in world.client.subscriptions


def test_nothing_of_the_creator_is_subscribed_with_the_menu_off(make_bridge, factory, settings):
    webif.mounted()
    world = World(make_bridge, factory, settings, menu_allowed=False)
    topics = [topic for topic, _qos in world.client.subscriptions]
    assert CATALOGUE_TOPIC not in topics and RESULT_TOPIC not in topics
    assert "menu_editor" not in world.info()["capabilities"]


def test_the_capability_is_claimed_once_openwebif_has_the_page(make_bridge, factory, settings,
                                                               monkeypatch):
    world = World(make_bridge, factory, settings)
    # The running bridge, as `plugin.get_bridge` hands it to the hook.
    monkeypatch.setattr(webif, "_bridge", lambda: world.bridge)
    assert "menu" in world.info()["capabilities"]
    assert "menu_editor" not in world.info()["capabilities"]
    assert editor(world).submit(REVISION, a_menu(), lambda answer: None) == "not_allowed"

    # The hook runs after the connect: `info` is said again with the name in it.
    webif.mounted()

    assert "menu_editor" in world.info()["capabilities"]


def test_a_mounted_page_is_claimed_from_the_first_info(world):
    assert "menu_editor" in world.info()["capabilities"]


# ----------------------------------------------------------------- the catalogue --


def test_the_contract_example_is_a_catalogue_and_its_bytes_are_kept_exactly():
    raw = encoded(catalogue())
    parsed = parse_catalogue(raw)
    assert parsed["raw"] == raw
    assert parsed["revision"] == REVISION
    assert parsed["entities"] == 2
    assert parsed["left_out"] == 0


@pytest.mark.parametrize("empty", [b"", b"  \n", None])
def test_an_empty_catalogue_is_no_catalogue(empty):
    assert parse_catalogue(empty) is None


INVALID = [
    pytest.param(b"{not json", id="not-json"),
    pytest.param(b"[1, 2]", id="not-an-object"),
    pytest.param(b"\xff\xfe", id="not-utf8"),
    pytest.param(encoded(catalogue(v=None)), id="no-version"),
    pytest.param(encoded(catalogue(v=True)), id="version-bool"),
    pytest.param(encoded(catalogue(v="1")), id="version-text"),
    pytest.param(encoded(catalogue(revision="3F9C2E4A1B7D5C60")), id="revision-upper-case"),
    pytest.param(encoded(catalogue(revision="3f9c2e4a1b7d5c6")), id="revision-of-15"),
    pytest.param(encoded(catalogue(revision="3f9c2e4a1b7d5c600")), id="revision-of-17"),
    pytest.param(encoded(catalogue(revision=1234567890123456)), id="revision-number"),
    pytest.param(encoded(catalogue(menu={})), id="menu-not-a-list"),
    pytest.param(encoded(catalogue(entities=None)), id="entities-not-a-list"),
    pytest.param(b'{"v": 1e999999, "revision": "3f9c2e4a1b7d5c60", "menu": [], "entities": []}',
                 id="version-infinite"),
    pytest.param(b'{"v": ' + b"9" * 5000 + b', "revision": "3f9c2e4a1b7d5c60", "menu": [], '
                 b'"entities": []}', id="version-of-5000-digits"),
    pytest.param(encoded(catalogue(v=0)), id="version-0"),
    pytest.param(encoded(catalogue(v=-3)), id="version-negative"),
    pytest.param(b'{"v": 1, "revision": "3f9c2e4a1b7d5c60", "menu": [], "entities": [], '
                 b'"left_out": NaN}', id="nan"),
    pytest.param(b'{"v": 1, "revision": "3f9c2e4a1b7d5c60", "menu": [], "entities": [], '
                 b'"left_out": Infinity}', id="infinity"),
    pytest.param(b'{"v": 1, "revision": "3f9c2e4a1b7d5c60", "menu": [], "entities": [], '
                 b'"left_out": -Infinity}', id="minus-infinity"),
    pytest.param(b'{"v": 1, "revision": "3f9c2e4a1b7d5c60", "menu": [], "entities": [], '
                 b'"left_out": 1e999}', id="float-overflow"),
    pytest.param(b'{"v": 1, "revision": "3f9c2e4a1b7d5c60", "menu": [], "entities": [], '
                 b'"left_out": 1.' + b"0" * 100 + b'}', id="float-of-100-characters"),
]


@pytest.mark.parametrize("raw", INVALID)
def test_a_catalogue_that_breaks_the_envelope_is_refused(raw):
    with pytest.raises(InvalidCatalogue):
        parse_catalogue(raw)


OFF = b'{"v": 1, "editing": false}'


def test_editing_switched_off_is_a_catalogue_that_says_so_and_nothing_else():
    parsed = parse_catalogue(OFF)
    assert parsed == {"raw": OFF, "revision": None, "entities": 0, "left_out": None,
                      "editing": False}


def test_a_full_catalogue_says_editing_is_on_with_the_key_or_without_it():
    assert parse_catalogue(encoded(catalogue(editing=True)))["editing"] is True
    assert parse_catalogue(encoded(catalogue()))["editing"] is True


@pytest.mark.parametrize("value", ["false", 0, None, [], 1])
def test_an_editing_flag_that_is_not_true_or_false_is_invalid(value):
    with pytest.raises(InvalidCatalogue, match="editing"):
        parse_catalogue(encoded(catalogue(editing=value)))


def test_editing_on_still_needs_the_whole_envelope():
    with pytest.raises(InvalidCatalogue):
        parse_catalogue(b'{"v": 1, "editing": true}')


def test_editing_switched_off_replaces_the_catalogue_before_it(world, plugin_log):
    world.client.fire_message(CATALOGUE_TOPIC, encoded(catalogue()), retain=True)
    world.client.fire_message(CATALOGUE_TOPIC, OFF, retain=True)
    assert editor(world).catalogue["raw"] == OFF
    assert editor(world).status()["editing"] is False
    assert "switched off in Home Assistant" in plugin_log()


def test_a_catalogue_of_another_version_is_a_newer_one():
    with pytest.raises(NewerCatalogue) as refused:
        parse_catalogue(encoded(catalogue(v=2)))
    assert refused.value.version == 2


@pytest.mark.parametrize("version", [0, -1])
def test_a_version_below_one_is_invalid_not_newer(version):
    with pytest.raises(InvalidCatalogue) as refused:
        parse_catalogue(encoded(catalogue(v=version)))
    assert not isinstance(refused.value, NewerCatalogue)


def test_a_newer_versions_number_is_cut_in_the_sentence():
    with pytest.raises(NewerCatalogue) as refused:
        parse_catalogue(encoded(catalogue(v=10 ** 30)))
    assert str(refused.value) == ("a catalogue of version 100000000000..., which this plugin "
                                  "does not know")


def test_a_megabyte_of_digits_is_refused_before_it_is_converted():
    """Python 3.9 before 3.9.14 converts any length, in time growing with the square of it."""
    digits = b"9" * (menueditor.MAX_CATALOGUE_BYTES - 100)
    with pytest.raises(InvalidCatalogue, match="not JSON"):
        parse_catalogue(b'{"v": ' + digits + b"}")
    assert parse_catalogue(encoded(catalogue(left_out=10 ** 31)))["left_out"] == 10 ** 31
    with pytest.raises(InvalidCatalogue):
        parse_catalogue(encoded(catalogue(left_out=10 ** 32)))


@pytest.mark.parametrize("raw", ["[1e5]", "[-0.5]", "[12345678901234567890123456789012]"])
def test_ordinary_numbers_still_parse(raw):
    assert menueditor.loads_bounded(raw) == json.loads(raw)


@pytest.mark.parametrize("raw", ["[NaN]", "[Infinity]", "[-Infinity]", "[1e400]",
                                 "[" + "1" * 33 + "]"])
def test_loads_bounded_refuses_what_json_does_not_have_or_cannot_bound(raw):
    with pytest.raises(ValueError):
        menueditor.loads_bounded(raw)


def test_an_answer_with_a_nan_or_a_long_number_is_not_an_answer():
    assert parse_answer(b'{"id": "8c1d2e3f4a5b", "result": "saved", "x": NaN}') is None
    assert parse_answer(b'{"id": "8c1d2e3f4a5b", "result": "saved", "x": ' + b"1" * 40
                        + b"}") is None


def test_an_edit_with_a_nan_is_never_published():
    with pytest.raises(ValueError, match="unreadable"):
        encode_edit("0" * 12, REVISION, [float("nan")], 1792050000)
    with pytest.raises(ValueError, match="unreadable"):
        encode_edit("0" * 12, REVISION, [float("inf")], 1792050000)


def test_a_recursion_bomb_is_a_refusal_never_an_escaping_exception():
    """A megabyte of brackets: the parser's stack up to 3.13, a list on 3.14 - a refusal both."""
    with pytest.raises(InvalidCatalogue):
        parse_catalogue(b"[" * 500000 + b"]" * 500000)
    with pytest.raises(InvalidCatalogue):
        parse_catalogue(b'{"v": 1, "menu": ' + b"[" * 400000 + b"]" * 400000 + b"}")


def test_the_bound_is_its_own_one_mebibyte_not_the_menus():
    assert menueditor.MAX_CATALOGUE_BYTES == 1048576
    base = encoded(catalogue(padding=""))
    pad = menueditor.MAX_CATALOGUE_BYTES - len(base)
    exact = encoded(catalogue(padding="x" * pad))
    assert len(exact) == menueditor.MAX_CATALOGUE_BYTES
    assert parse_catalogue(exact)["raw"] == exact
    with pytest.raises(InvalidCatalogue, match="over 1048576"):
        parse_catalogue(encoded(catalogue(padding="x" * (pad + 1))))


def test_an_unusable_left_out_is_read_as_unknown():
    assert parse_catalogue(encoded(catalogue(left_out=-1)))["left_out"] is None
    assert parse_catalogue(encoded(catalogue(left_out=True)))["left_out"] is None
    assert parse_catalogue(encoded(catalogue(left_out=7)))["left_out"] == 7


def test_a_received_catalogue_is_kept_with_its_time(world):
    raw = encoded(catalogue())
    world.client.fire_message(CATALOGUE_TOPIC, raw, retain=True)
    status = editor(world).status()
    assert editor(world).catalogue["raw"] == raw
    assert status["revision"] == REVISION and status["entities"] == 2
    assert status["received"] is not None and status["reason"] is None


def test_an_invalid_catalogue_keeps_the_one_before_it(world, plugin_log):
    raw = encoded(catalogue())
    world.client.fire_message(CATALOGUE_TOPIC, raw, retain=True)
    world.client.fire_message(CATALOGUE_TOPIC, b"[" * 300000, retain=False)
    world.client.fire_message(CATALOGUE_TOPIC, encoded(catalogue(revision="nope")))
    assert editor(world).catalogue["raw"] == raw
    assert plugin_log().count("ignoring an invalid menu catalogue") == 2


def test_an_empty_catalogue_forgets_the_one_before_it(world):
    world.client.fire_message(CATALOGUE_TOPIC, encoded(catalogue()), retain=True)
    world.client.fire_message(CATALOGUE_TOPIC, b"", retain=True)
    assert editor(world).catalogue is None
    assert editor(world).status()["reason"] == "none"


def test_a_newer_catalogue_is_not_shown_and_says_why_once(world, plugin_log):
    world.client.fire_message(CATALOGUE_TOPIC, encoded(catalogue()), retain=True)
    world.client.fire_message(CATALOGUE_TOPIC, encoded(catalogue(v=2)))
    world.client.fire_message(CATALOGUE_TOPIC, encoded(catalogue(v=2)))
    assert editor(world).catalogue is None
    assert editor(world).status()["reason"] == "newer"
    assert plugin_log().count("ignoring the menu catalogue: a catalogue of version 2") == 1


def test_the_versions_named_in_the_log_are_bounded(world, plugin_log):
    for version in range(2, 2 + menueditor.MAX_VERSIONS_LOGGED + 5):
        world.client.fire_message(CATALOGUE_TOPIC, encoded(catalogue(v=version)))
    assert len(editor(world)._versions_logged) == menueditor.MAX_VERSIONS_LOGGED
    assert plugin_log().count("ignoring the menu catalogue") == menueditor.MAX_VERSIONS_LOGGED


# ----------------------------------------------------------------------- edits --


def test_a_save_is_published_once_not_retained_at_qos_1(world):
    refusal, answers = submit(world)
    assert refusal is None and answers == []
    sent = world.client.all_for(EDIT_TOPIC)
    assert len(sent) == 1
    assert sent[0].qos == 1 and sent[0].retain is False
    payload = sent[0].json()
    assert set(payload) == {"id", "base", "ts", "menu"}
    assert re.fullmatch(r"[0-9a-f]{12}", payload["id"])
    assert payload["base"] == REVISION
    assert isinstance(payload["ts"], int)
    assert payload["menu"] == a_menu()


def test_the_answer_with_the_edits_id_is_handed_to_the_save(world):
    _refusal, answers = submit(world)
    ident = edits(world)[0]["id"]
    answer(world, "0" * 12)
    assert answers == []
    answer(world, ident, "saved")
    assert answers == [{"result": "saved", "revision": NEW_REVISION, "path": None,
                        "detail": None}]
    # Answered once: a repeat of the answer finds nothing waiting.
    answer(world, ident, "stale")
    assert len(answers) == 1
    assert editor(world).pending == {}


def test_a_retained_answer_is_ignored(world):
    _refusal, answers = submit(world)
    answer(world, edits(world)[0]["id"], retain=True)
    assert answers == []


@pytest.mark.parametrize("raw", [
    b"", b"{", b"[]", b'{"id": "8c1d2e3f4a5b"}', b'{"result": "saved"}',
    b'{"id": "8C1D2E3F4A5B", "result": "saved"}', b'{"id": "8c1d2e3f4a5", "result": "saved"}',
    b'{"id": "8c1d2e3f4a5b", "result": "Saved!"}', b'{"id": "8c1d2e3f4a5b", "result": 1}',
    b"x" * 5000,
])
def test_an_unreadable_answer_is_not_an_answer(raw):
    assert parse_answer(raw) is None


def test_an_answers_texts_are_cut_and_made_one_line():
    ident, parsed = parse_answer(json.dumps({
        "id": "8c1d2e3f4a5b", "result": "invalid", "revision": "NOT-HEX",
        # A line break, and a right-to-left override (U+202E), which draws nothing.
        "path": "menu[2].rows[5].control", "detail": "a\nb" + chr(0x202E) + "c" + "d" * 300,
    }).encode("utf-8"))
    assert ident == "8c1d2e3f4a5b"
    assert parsed["revision"] is None
    assert parsed["path"] == "menu[2].rows[5].control"
    assert parsed["detail"].startswith("a bc")
    assert len(parsed["detail"]) == menueditor.DETAIL_LIMIT


def test_a_result_word_this_plugin_does_not_know_is_passed_on():
    _ident, parsed = parse_answer(b'{"id": "8c1d2e3f4a5b", "result": "postponed"}')
    assert parsed["result"] == "postponed"


def test_no_answer_within_ten_seconds_is_no_answer(world):
    assert menueditor.ANSWER_SECONDS == 10
    _refusal, answers = submit(world)
    MainLoop.advance(9999)
    assert answers == []
    MainLoop.advance(1)
    assert answers == [{"result": "no_answer"}]
    answer(world, edits(world)[0]["id"])
    assert len(answers) == 1


def test_an_edit_the_broker_never_took_is_dropped_on_disconnect(world):
    _refusal, answers = submit(world)
    assert EDIT_TOPIC in world.client.queued_for_reconnect()
    world.client.fire_disconnect(7)
    assert answers == [{"result": "not_connected"}]
    assert EDIT_TOPIC not in world.client.queued_for_reconnect()
    assert editor(world).pending == {}


def test_an_edit_the_broker_took_is_no_answer_on_disconnect(world):
    _refusal, answers = submit(world)
    world.client.acknowledge()
    world.client.fire_disconnect(7)
    assert answers == [{"result": "no_answer"}]


def test_a_save_without_a_broker_connection_publishes_nothing(world):
    world.client.fire_disconnect(7)
    published = len(world.client.published)
    refusal, answers = submit(world)
    assert refusal == "not_connected" and answers == []
    assert len(world.client.published) == published


def test_more_than_four_waiting_saves_are_the_receivers_own_busy(world):
    """Never Home Assistant's word: the page says the receiver is the one waiting."""
    for _index in range(menueditor.MAX_PENDING):
        assert submit(world)[0] is None
    assert submit(world)[0] == "receiver_busy"
    assert menueditor.RECEIVER_BUSY != menueditor.BUSY
    assert len(edits(world)) == menueditor.MAX_PENDING


@pytest.mark.parametrize("base, menu, refusal", [
    ("3F9C2E4A1B7D5C60", [], "unreadable"),
    (None, [], "unreadable"),
    (REVISION, {"type": "group"}, "unreadable"),
    (REVISION, ["\ud800"], "unreadable"),
    (REVISION, [{"label": "x" * 70000}], "too_big"),
])
def test_an_envelope_the_receiver_cannot_send_is_refused_unsent(world, base, menu, refusal):
    published = len(world.client.published)
    assert submit(world, menu, base)[0] == refusal
    assert len(world.client.published) == published


def test_the_edit_bound_is_the_encoded_payload_at_64_kib():
    assert menueditor.MAX_EDIT_BYTES == 65536
    head = len(encode_edit("0" * 12, REVISION, [""], 1792050000)[1])
    fits = ["x" * (menueditor.MAX_EDIT_BYTES - head)]
    assert len(encode_edit("0" * 12, REVISION, fits, 1792050000)[1]) == menueditor.MAX_EDIT_BYTES
    with pytest.raises(ValueError, match="too_big"):
        encode_edit("0" * 12, REVISION, ["x" + fits[0]], 1792050000)


def test_a_stopping_bridge_answers_every_waiting_save(world):
    _refusal, answers = submit(world)
    world.bridge.stop()
    assert answers == [{"result": "no_answer"}]


def test_the_menu_switched_off_answers_waiting_saves_and_offers_nothing(world, settings):
    _refusal, answers = submit(world)
    old = world.publisher
    settings.menu_allowed.value = False
    world.bridge.reload()
    assert answers == [{"result": "no_answer"}]
    assert world.bridge.publisher("menu") is None
    assert old.editor.pending == {}


def test_a_callback_that_raises_never_reaches_the_main_loop(world, plugin_log):
    def broken(_answer):
        raise RuntimeError("the page went away")

    assert editor(world).submit(REVISION, a_menu(), broken) is None
    answer(world, edits(world)[0]["id"])
    assert "could not be handed to the page" in plugin_log()


# ----------------------------------------------- cmd/config, now partial, over MQTT --


def config(world, payload):
    world.client.fire_message(CONFIG_TOPIC, json.dumps(payload).encode("utf-8"))


def test_an_empty_config_object_is_accepted_changes_nothing_and_says_info_again(world, settings):
    world.client.clear()
    config(world, {})
    assert world.client.last(LAST_ERROR) is None
    assert world.info()["settings"]["menu_transparency"] == 20
    assert settings.publish_keys.value is True and settings.screenshot.value == "on_zap"


def test_one_key_alone_changes_that_key_alone(world, settings):
    settings.screenshot_interval.value = 300
    config(world, {"menu_transparency": 50})
    assert settings.menu_transparency.value == 50
    assert settings.menu_transparency.saved_value == 50
    assert settings.screenshot_interval.value == 300
    assert world.info()["settings"]["menu_transparency"] == 50


def test_the_full_object_of_0_4_0_still_works(world, settings):
    config(world, {"publish_keys": False, "screenshot": "interval", "screenshot_interval": 90})
    assert settings.publish_keys.value is False
    assert settings.screenshot_interval.value == 90
    assert settings.menu_transparency.value == 20


@pytest.mark.parametrize("value, sentence", [
    (25, "menu_transparency must be a multiple of 10"),
    (90, "menu_transparency must be between 0 and 80"),
    (-10, "menu_transparency must be between 0 and 80"),
    (True, "menu_transparency must be an integer"),
    ("30", "menu_transparency must be an integer"),
    (30.0, "menu_transparency must be an integer"),
])
def test_a_transparency_off_its_steps_is_refused_and_never_rounded(world, settings, value,
                                                                    sentence):
    config(world, {"menu_transparency": value})
    assert world.client.last(LAST_ERROR).json()["error"] == sentence
    assert settings.menu_transparency.value == 20
