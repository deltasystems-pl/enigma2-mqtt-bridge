"""A list of choices on the television: the payload, the keys, the endings and the one answer.

Home Assistant sends `cmd/list` (docs/TOPICS.md, section 2) and the receiver answers once on
`list_answer` (section 1) with the row the viewer picked, or how the list ended without one. The
tests drive it on `StartEnigma`'s modal session, so a list that opens is the executing dialog
and a close pops it on the next turn of the main loop (`MainLoop.advance(0)`), as on the
receiver. The countdown runs on a clock the test moves by hand.
"""

import json

import pytest
from conftest import (
    DESKTOP,
    ChoiceBox,
    InfoBar,
    MainLoop,
    MoviePlayer,
    Receiver,
    Session,
    Standby,
    VirtualKeyBoard,
    config,
    eSize,
    skin_module,
    standby_module,
)

from MQTTBridge import choicelist, styledlist
from MQTTBridge.choicelist import InvalidList, parse_list
from MQTTBridge.menu import ICON_NAMES

NODE = "vuuno4kse_005301"
COMMAND = "enigma2/" + NODE + "/cmd/list"
ANSWER = "enigma2/" + NODE + "/list_answer"
LAST_ERROR = "enigma2/" + NODE + "/last_error"
INFO = "enigma2/" + NODE + "/info"
AVAILABILITY = "enigma2/" + NODE + "/availability"
MENU_TOPIC = "enigma2mqtt/menu/" + NODE
LIST_ID = "a1b2c3d4e5f60718"


def doorbell(**changes):
    """The contract's example."""
    payload = {
        "title": "Doorbell", "text": "Someone is at the gate.", "timeout": 60,
        "default": "ignore",
        "rows": [{"id": "open", "label": "Open the gate", "key": "green", "icon": "gate"},
                 {"id": "ignore", "label": "Ignore", "key": "red"}],
    }
    payload.update(changes)
    return payload


def rows(count, **fields):
    return [dict({"id": f"r{index}", "label": f"Row {index}"}, **fields)
            for index in range(count)]


def parsed(**changes):
    return parse_list(json.dumps(doorbell(**changes)))[0]


def refusal(payload):
    with pytest.raises(InvalidList) as raised:
        parse_list(payload if isinstance(payload, str) else json.dumps(payload))
    return str(raised.value)


# ---------------------------------------------------------------- the payload --


def test_the_contract_example_is_a_list():
    spec, unknown = parse_list(json.dumps(doorbell()))
    assert unknown == 0
    assert spec == {
        "title": "Doorbell", "text": "Someone is at the gate.", "timeout": 60,
        "default": "ignore",
        "rows": [{"id": "open", "label": "Open the gate", "key": "green", "icon": "gate"},
                 {"id": "ignore", "label": "Ignore", "key": "red", "icon": None}],
    }


def test_the_least_a_list_needs_is_a_title_and_one_row():
    spec, _unknown = parse_list(json.dumps({"title": "T", "rows": [{"id": "a", "label": "A"}]}))
    assert spec == {"title": "T", "text": None, "timeout": 60, "default": None,
                    "rows": [{"id": "a", "label": "A", "key": None, "icon": None}]}


def test_one_and_twenty_four_rows_are_lists_and_none_or_twenty_five_are_not():
    assert len(parsed(rows=rows(1), default=None)["rows"]) == 1
    assert len(parsed(rows=rows(24), default=None)["rows"]) == 24
    assert refusal(doorbell(rows=[], default=None)) == "rows is empty; a list has 1 to 24 rows"
    assert refusal(doorbell(rows=rows(25), default=None)) == "rows has more than 24 rows"
    assert refusal(doorbell(rows={"id": "a"}, default=None)) == "rows is not a list"
    assert refusal(doorbell(rows=None, default=None)) == "rows is not a list"


@pytest.mark.parametrize("ident", ["a" * 32, "A.b_c:d-9", "0", "x" * 31 + ":"])
def test_a_row_id_of_up_to_32_of_the_characters_is_one(ident):
    assert parsed(rows=[{"id": ident, "label": "L"}], default=None)["rows"][0]["id"] == ident


@pytest.mark.parametrize("ident", ["a" * 33, "", "a b", "a/b", chr(0xE9), "a" + chr(10), 7, None,
                                   True])
def test_any_other_row_id_is_refused_without_its_text(ident):
    sentence = refusal(doorbell(rows=[{"id": "ok", "label": "L"}, {"id": ident, "label": "L"}],
                                default=None))
    assert sentence == ("rows[1].id must be 1 to 32 of A-Z, a-z, 0-9, '.', '_', ':' and '-'")


def test_ids_repeat_only_with_the_same_case():
    assert len(parsed(rows=[{"id": "a", "label": "x"}, {"id": "A", "label": "y"}],
                      default=None)["rows"]) == 2
    assert refusal(doorbell(rows=[{"id": "a", "label": "x"}, {"id": "a", "label": "y"}],
                            default=None)) == "rows[1].id is the id of an earlier row"


def test_the_title_loses_its_backslashes_before_it_is_cut_at_60():
    assert parsed(title="\\" * 10 + "a" * 70)["title"] == "a" * 60
    assert parsed(title="a" * 60)["title"] == "a" * 60
    assert parsed(title="\\cFFFF0000Alarm")["title"] == "cFFFF0000Alarm"


def test_a_title_blank_after_the_cut_is_refused():
    assert refusal(doorbell(title=" " * 60 + "x")) == "title is empty"
    assert refusal(doorbell(title="\\\\  ")) == "title is empty"
    assert refusal(doorbell(title="")) == "title is empty"
    assert refusal(doorbell(title=None)) == "title is missing"
    assert refusal(doorbell(title=42)) == "title is not text"
    payload = doorbell()
    del payload["title"]
    assert refusal(payload) == "title is missing"


def test_the_text_is_cut_at_200_after_its_backslashes_and_keeps_a_line_break():
    assert parsed(text="\\" * 5 + "b" * 250)["text"] == "b" * 200
    assert parsed(text="b" * 200)["text"] == "b" * 200
    assert parsed(text="one\r\ntwo")["text"] == "one\ntwo"
    # A tab is a space; a zero-width space and a right-to-left override are dropped.
    hidden = "a" + chr(9) + "b" + chr(0x200B) + "c" + chr(0x202E) + "d"
    assert parsed(text=hidden)["text"] == "a bcd"


@pytest.mark.parametrize("text", [None, "", "   ", "\\\\"])
def test_no_text_or_a_blank_one_is_no_text(text):
    assert parsed(text=text)["text"] is None


def test_a_text_that_is_not_text_is_refused():
    assert refusal(doorbell(text=["a"])) == "text is not text or null"


def test_a_label_is_one_line_cut_at_60_and_required():
    spec = parsed(rows=[{"id": "a", "label": "\\" + "x" * 70}, {"id": "b", "label": "1\n2"}],
                  default=None)
    assert spec["rows"][0]["label"] == "x" * 60
    assert spec["rows"][1]["label"] == "1 2"
    assert refusal(doorbell(rows=[{"id": "a"}], default=None)) == "rows[0].label is missing"
    assert refusal(doorbell(rows=[{"id": "a", "label": " \\ "}], default=None)) == \
        "rows[0].label is empty"
    assert refusal(doorbell(rows=[{"id": "a", "label": 3}], default=None)) == \
        "rows[0].label is not text"


@pytest.mark.parametrize("key", list(choicelist.ROW_KEYS) + [None])
def test_each_key_of_the_contract_is_a_key(key):
    assert parsed(rows=[{"id": "a", "label": "A", "key": key}], default=None)["rows"][0][
        "key"] == key


@pytest.mark.parametrize("key", ["RED", "Red", "0", "10", 1, "", "ok", True, ["red"]])
def test_any_other_key_is_refused(key):
    assert refusal(doorbell(rows=[{"id": "a", "label": "A", "key": key}], default=None)) == \
        'rows[0].key is not red, green, yellow, blue or "1" to "9"'


def test_two_rows_with_one_key_are_refused():
    assert refusal(doorbell(rows=[{"id": "a", "label": "A", "key": "1"},
                                  {"id": "b", "label": "B", "key": "1"}], default=None)) == \
        "rows[1].key is the key of an earlier row"


def test_an_unknown_icon_is_neutral_and_not_refused():
    spec = parsed(rows=[{"id": "a", "label": "A", "icon": "rocket"},
                        {"id": "b", "label": "B", "icon": "gate"},
                        {"id": "c", "label": "C", "icon": None},
                        {"id": "d", "label": "D", "icon": "../plugin"}], default=None)
    assert [row["icon"] for row in spec["rows"]] == ["neutral", "gate", None, "neutral"]
    assert refusal(doorbell(rows=[{"id": "a", "label": "A", "icon": 5}], default=None)) == \
        "rows[0].icon is not text or null"


@pytest.mark.parametrize("timeout", [5, 600, 60, 300])
def test_a_timeout_of_5_to_600_whole_seconds_is_one(timeout):
    assert parsed(timeout=timeout)["timeout"] == timeout


@pytest.mark.parametrize("timeout", [4, 601, 0, -5, True, False, 60.0, 59.5, "60", None, [60]])
def test_any_other_timeout_is_refused(timeout):
    assert refusal(doorbell(timeout=timeout)) == \
        "timeout must be a whole number of seconds from 5 to 600"


def test_without_a_timeout_a_list_has_60_seconds():
    payload = doorbell()
    del payload["timeout"]
    assert parse_list(json.dumps(payload))[0]["timeout"] == 60


@pytest.mark.parametrize("default", ["nobody", "IGNORE", 1, ["ignore"], True])
def test_a_default_that_is_no_row_id_is_refused(default):
    assert refusal(doorbell(default=default)) == "default is not the id of a row"


def test_no_default_is_none():
    assert parsed(default=None)["default"] is None


def test_members_this_plugin_does_not_know_are_counted_and_ignored():
    payload = doorbell(colour="red", sound=True)
    payload["rows"][0]["hint"] = "x"
    spec, unknown = parse_list(json.dumps(payload))
    assert unknown == 3
    assert spec["title"] == "Doorbell"


@pytest.mark.parametrize("text", ["", "PRESS", "[1, 2]", '"a"', "{not json", "null", "7"])
def test_a_payload_that_is_no_object_is_refused(text):
    assert refusal(text) == "cmd/list takes a JSON object"


def test_a_row_that_is_no_object_is_refused():
    assert refusal(doorbell(rows=[{"id": "a", "label": "A"}, "b"], default=None)) == \
        "rows[1] is not an object"


SECRET = "SECRET-HOUSEHOLD-TEXT"


@pytest.mark.parametrize("payload", [
    doorbell(title=7, text=SECRET),
    doorbell(rows=[{"id": SECRET + " x", "label": SECRET}], default=None),
    doorbell(rows=[{"id": "a", "label": SECRET, "key": SECRET}], default=None),
    doorbell(rows=[{"id": "a", "label": SECRET, "icon": [SECRET]}], default=None),
    doorbell(default=SECRET),
    doorbell(timeout=SECRET),
    doorbell(text={SECRET: SECRET}),
])
def test_a_refusal_never_says_the_senders_text(payload):
    assert SECRET not in refusal(payload)


# ------------------------------------------------------------------- the world --


class World:
    """A receiver in live TV, a connected bridge, and a clock the test moves."""

    def __init__(self, make_bridge, factory, settings, receiver=None, **changes):
        self.receiver = receiver or Receiver(modal=True)
        if receiver is None:
            self.receiver.with_channel_list()
        settings.host.value = "10.0.0.5"
        settings.node_id.value = NODE
        settings.friendly_name.value = "Living room receiver"
        for name, value in changes.items():
            getattr(settings, name).value = value
        self.bridge = make_bridge(session=self.receiver.session)
        self.bridge.start()
        self.factory = factory
        self.client = factory.client
        self.client.fire_connect()
        self.now = 1000.0
        if self.publisher is not None:
            self.publisher.clock = lambda: self.now

    @property
    def session(self):
        return self.receiver.session

    @property
    def publisher(self):
        return self.bridge.publisher("list")

    @property
    def screen(self):
        dialog = self.session.current_dialog
        return dialog if isinstance(dialog, choicelist.screen_class()) else None

    def send(self, payload=None, ident=LIST_ID, raw=None, retain=False):
        if raw is None:
            body = doorbell() if payload is None else payload
            raw = json.dumps({"id": ident, "payload": body} if ident else body)
        self.client.fire_message(COMMAND, raw.encode("utf-8"), retain=retain)

    def show(self, payload=None, ident=LIST_ID):
        self.send(payload, ident)
        assert self.screen is not None, f"the list did not open: {self.error()!r}"
        return self.screen

    def answers(self):
        return [entry.json() for entry in self.client.all_for(ANSWER)]

    def endings(self):
        return [(answer["ending"], answer["row"]) for answer in self.answers()]

    def error(self):
        entry = self.client.last(LAST_ERROR)
        return None if entry is None or entry.text == "" else entry.json()

    def info(self):
        return self.client.last(INFO).json()

    def view_rows(self):
        return [entry[0] for entry in self.screen["list"].list]

    def select(self, ident):
        screen = self.screen
        screen["list"].moveToIndex(screen._ids.index(ident))

    def settle(self):
        MainLoop.advance(0)

    def elapse(self, seconds):
        """Move the clock and let the countdown's timer fire, once a second."""
        for _ in range(int(seconds)):
            self.now += 1
            timer = self.publisher._tick.timer
            if timer is not None and timer.running:
                timer.fire()


@pytest.fixture
def world(make_bridge, factory, settings):
    return World(make_bridge, factory, settings)


# ------------------------------------------------------- capability and info --


def test_the_capability_and_info_say_a_list_can_be_shown(world):
    info = world.info()
    assert "list" in info["capabilities"]
    assert info["list"] == {"screen": "styled"}


def test_without_a_session_there_is_no_list_and_the_command_says_so(connected_bridge, factory):
    client = factory.client
    assert "list" not in client.last(INFO).json()["capabilities"]
    assert "list" not in client.last(INFO).json()
    client.fire_message(COMMAND, json.dumps({"id": LIST_ID, "payload": doorbell()}).encode())
    error = client.last(LAST_ERROR).json()
    assert error == {"cmd": "list", "error": "lists cannot be shown on this receiver",
                     "reason": "no_capability", "id": LIST_ID, "ts": error["ts"]}


def test_without_a_session_an_invalid_list_is_still_invalid(connected_bridge, factory):
    factory.client.fire_message(COMMAND, b'{"title": "T"}')
    assert factory.client.last(LAST_ERROR).json()["reason"] == "invalid"


def _no_styled_screen(monkeypatch):
    monkeypatch.setattr(styledlist, "styled_refusal", lambda: "enigma has no gFont")


def test_an_image_without_the_styled_screens_parts_shows_the_images_list(
        make_bridge, factory, settings, monkeypatch):
    _no_styled_screen(monkeypatch)
    world = World(make_bridge, factory, settings)
    assert world.info()["list"] == {"screen": "choicebox"}


def test_an_image_with_no_list_screen_at_all_claims_nothing(make_bridge, factory, settings,
                                                             monkeypatch):
    _no_styled_screen(monkeypatch)
    monkeypatch.setattr(styledlist, "fallback_refusal", lambda: "no ChoiceBox")
    world = World(make_bridge, factory, settings)
    assert world.publisher is None
    assert "list" not in world.info()["capabilities"]
    assert "list" not in world.info()
    world.send()
    assert world.error()["reason"] == "no_capability"


def test_a_skin_whose_screen_cannot_be_built_takes_the_styled_screen_away(
        make_bridge, factory, settings, receiver):
    """A skin may define `MQTTBridgeChoices`; one that names a widget the screen lacks fails."""
    skin_module.domScreens["MQTTBridgeChoices"] = (
        '<screen name="MQTTBridgeChoices"><widget name="bogus" /></screen>')
    world = World(make_bridge, factory, settings, receiver=receiver)
    assert world.info()["list"] == {"screen": "choicebox"}


def test_a_skin_that_restyles_the_screen_with_its_widgets_keeps_it_styled(
        make_bridge, factory, settings, receiver):
    skin_module.domScreens["MQTTBridgeChoices"] = (
        '<screen name="MQTTBridgeChoices" position="0,0" size="800,600">'
        '<widget name="title" position="10,10" size="700,40" />'
        '<widget name="list" position="10,60" size="700,400" /></screen>')
    world = World(make_bridge, factory, settings, receiver=receiver)
    assert world.info()["list"] == {"screen": "styled"}


def test_a_skin_reload_that_can_build_nothing_takes_the_capability_back_and_says_so(
        world, monkeypatch):
    _no_styled_screen(monkeypatch)
    monkeypatch.setattr(styledlist, "fallback_refusal", lambda: "no ChoiceBox")
    skin_module.InitSkins(booting=False)
    info = world.info()
    assert "list" not in info["capabilities"]
    assert "list" not in info
    world.send()
    assert world.error()["reason"] == "no_capability"
    # And back, when the next reload can.
    monkeypatch.undo()
    skin_module.InitSkins(booting=False)
    assert world.info()["list"] == {"screen": "styled"}


def test_a_skin_reload_that_can_build_only_the_images_list_says_so(world, monkeypatch):
    _no_styled_screen(monkeypatch)
    skin_module.InitSkins(booting=False)
    assert world.info()["list"] == {"screen": "choicebox"}


def test_stopping_lets_go_of_the_standby_counter_and_the_skin_reload(world):
    publisher = world.publisher
    assert publisher._entered_standby in config.misc.standbyCounter.notifiers
    assert publisher._skin_reloaded in skin_module.onLoadCallbacks
    world.bridge.stop()
    assert publisher._entered_standby not in config.misc.standbyCounter.notifiers
    assert publisher._skin_reloaded not in skin_module.onLoadCallbacks


# ------------------------------------------------------------------ the screen --


def test_the_list_opens_over_live_tv_with_everything_the_contract_says(world):
    screen = world.show()
    assert screen["header"].getText() == "From Home Assistant, via MQTT Bridge"
    assert screen["title"].getText() == "Doorbell"
    assert screen["text"].getText() == "Someone is at the gate."
    assert world.view_rows() == ["open", "ignore"]
    first, second = screen["list"].list
    assert first[1]["kind"] == "pixmap" and first[1]["png"].path.endswith("icons/gate.png")
    assert first[2]["text"] == "Open the gate"
    # The key: a filled rounded button in its colour, scaled like the icons - no colour word.
    assert first[3]["kind"] == "pixmap" and first[3]["png"].path.endswith("keys/green.png")
    assert first[3]["flags"] == 128 | 256
    # No icon, and the label where the other row's label is.
    assert [part["kind"] for part in second[1:]] == ["text", "pixmap"]
    assert second[1]["pos"][0] == first[2]["pos"][0]
    assert second[2]["png"].path.endswith("keys/red.png")
    # The label stops before the key.
    assert first[2]["pos"][0] + first[2]["size"][0] <= first[3]["pos"][0]
    assert all(part.get("text") not in ("Green", "Red") for row in (first, second)
               for part in row[1:])
    # With a default row, the countdown says what the timeout answers.
    assert screen["countdown"].getText() == "Ignore in 60 s"
    assert screen["legend"].getText() == "OK or the row's key choose   EXIT dismiss"
    # The selection starts on the default row.
    assert screen.selected() == "ignore"
    assert world.answers() == []
    assert world.error() is None


def test_without_a_default_the_selection_starts_on_the_first_row(world):
    screen = world.show(doorbell(default=None))
    assert screen.selected() == "open"


def test_a_list_without_text_or_keys_has_neither(world):
    screen = world.show({"title": "Bedtime", "rows": [{"id": "a", "label": "A"},
                                                      {"id": "b", "label": "B"}]})
    # The text's label is there, empty: every list has the same widgets.
    assert screen["text"].getText() == ""
    assert screen.shape["text_height"] == 0
    assert screen["legend"].getText() == "OK choose   EXIT dismiss"
    assert screen["countdown"].getText() == "Closes in 60 s"
    # The id and the label: no icon, and no key on the right.
    assert [len(entry) for entry in screen["list"].list] == [2, 2]


def test_the_list_draws_on_the_menus_see_through_background(make_bridge, factory, settings):
    world = World(make_bridge, factory, settings, menu_transparency=40)
    screen = world.show()
    assert 'backgroundColor="#66101820"' in screen.skin


def test_the_geometry_fits_24_rows_at_720_and_1080_lines():
    for height, width in ((720, 1280), (1080, 1920)):
        shape = choicelist.geometry(width, height, 24, True)
        assert shape["visible"] == choicelist.VISIBLE_ROWS
        assert shape["y"] + shape["height"] <= height
        assert shape["x"] + shape["width"] <= width
        assert shape["list_height"] == choicelist.VISIBLE_ROWS * shape["row"]
    small = choicelist.geometry(1280, 720, 2, False)
    assert small["visible"] == 2 and small["text_height"] == 0
    full = choicelist.geometry(1920, 1080, 2, True)
    assert full["row"] == 69 and full["text_height"] == 180 and full["title"] == 96


def test_a_24_row_list_opens_on_a_720_line_desktop_with_every_row(make_bridge, factory,
                                                                   settings):
    DESKTOP.resize(eSize(1280, 720))
    world = World(make_bridge, factory, settings)
    screen = world.show(doorbell(rows=rows(24), default=None))
    assert len(screen["list"].list) == 24
    assert screen.shape["row"] == 46


def test_a_long_label_is_cut_and_drawn_with_an_ellipsis(world):
    screen = world.show(doorbell(rows=[{"id": "a", "label": "L" * 90}], default=None))
    label = screen["list"].list[0][1]
    assert label["text"] == "L" * 60
    assert label["flags"] & 64


def test_an_unknown_icon_is_drawn_neutral(world):
    screen = world.show(doorbell(rows=[{"id": "a", "label": "A", "icon": "rocket"}],
                                 default=None))
    assert screen["list"].list[0][1]["png"].path.endswith("icons/neutral.png")


@pytest.mark.parametrize("name", ICON_NAMES)
def test_every_icon_name_is_drawn_with_its_own_icon(world, name):
    screen = world.show(doorbell(rows=[{"id": "a", "label": "A", "icon": name}], default=None))
    assert screen["list"].list[0][1]["png"].path.endswith("icons/" + name + ".png")


def test_the_screen_binds_ok_exit_the_colours_and_the_numbers(world):
    screen = world.show()
    assert screen["listActions"].contexts == ["OkCancelActions"]
    assert set(screen["listActions"].actions) == {"ok", "cancel"}
    assert screen["listColours"].contexts == ["ColorActions"]
    assert set(screen["listColours"].actions) == {"red", "green", "yellow", "blue"}
    assert screen["listNumbers"].contexts == ["NumberActions"]
    assert set(screen["listNumbers"].actions) == {str(number) for number in range(1, 10)}
    assert {screen[name].prio for name in ("listActions", "listColours", "listNumbers")} == {-1}
    assert screen.skinName == "MQTTBridgeChoices"


# --------------------------------------------------------------------- the keys --


def test_ok_picks_the_selected_row_and_the_screen_closes(world):
    world.show()
    world.select("open")
    world.screen.key_ok()
    (answer,) = world.client.all_for(ANSWER)
    assert answer.qos == 1 and answer.retain is False
    payload = answer.json()
    assert payload == {"id": LIST_ID, "ending": "picked", "row": "open", "ts": payload["ts"]}
    assert isinstance(payload["ts"], int)
    world.settle()
    assert world.screen is None
    assert world.session.current_dialog is InfoBar.instance


@pytest.mark.parametrize("colour, row", [("green", "open"), ("red", "ignore")])
def test_a_colour_key_picks_its_row_at_once(world, colour, row):
    screen = world.show()
    screen["listColours"].actions[colour]()
    assert world.endings() == [("picked", row)]


def test_a_number_key_picks_its_row_at_once(world):
    screen = world.show(doorbell(rows=[{"id": "a", "label": "A", "key": "3"},
                                       {"id": "b", "label": "B", "key": "1"}], default=None))
    # The digit, in a small box.
    box, digit = screen["list"].list[0][2:]
    assert box["kind"] == "pixmap" and box["png"].path.endswith("keys/box.png")
    assert digit["text"] == "3" and digit["pos"] == box["pos"] and digit["size"] == box["size"]
    screen["listNumbers"].actions["1"]("1")
    assert world.endings() == [("picked", "b")]


def test_a_key_no_row_has_does_nothing(world):
    screen = world.show()
    screen["listColours"].actions["yellow"]()
    screen["listColours"].actions["blue"]()
    for number in range(1, 10):
        screen["listNumbers"].actions[str(number)](str(number))
    assert world.answers() == []
    assert world.screen is screen


def test_exit_dismisses_and_says_no_row(world):
    world.show().key_cancel()
    assert world.endings() == [("dismissed", None)]
    world.settle()
    assert world.screen is None


def test_a_list_without_an_id_is_answered_without_one(world):
    world.show(ident=None).key_cancel()
    (answer,) = world.answers()
    assert "id" not in answer
    assert answer["ending"] == "dismissed"


def test_an_id_the_plugin_cannot_use_is_dropped_and_the_list_is_shown(world):
    world.show(ident="has spaces")
    world.screen.key_cancel()
    assert "id" not in world.answers()[0]


def test_the_list_hands_the_keys_back_to_the_screen_under_it(world):
    player = world.session.open(MoviePlayer)
    world.show().key_cancel()
    world.settle()
    assert world.session.current_dialog is player


# ---------------------------------------------------------------- the countdown --


def test_the_countdown_counts_and_a_timeout_names_the_default_row(world):
    screen = world.show(doorbell(timeout=5))
    assert screen["countdown"].getText() == "Ignore in 5 s"
    world.elapse(1)
    assert screen["countdown"].getText() == "Ignore in 4 s"
    world.elapse(3)
    assert screen["countdown"].getText() == "Ignore in 1 s"
    assert world.answers() == []
    world.elapse(1)
    assert world.endings() == [("timeout", "ignore")]
    world.settle()
    assert world.screen is None


def test_a_timeout_without_a_default_says_no_row(world):
    screen = world.show(doorbell(timeout=5, default=None))
    world.elapse(1)
    assert screen["countdown"].getText() == "Closes in 4 s"
    world.elapse(4)
    assert world.endings() == [("timeout", None)]


def test_the_countdown_lands_on_the_whole_seconds_whatever_the_timer_does(world):
    world.show(doorbell(timeout=10))
    world.now += 3.4
    world.publisher._tick.timer.fire()
    assert world.screen["countdown"].getText() == "Ignore in 7 s"
    interval, single = world.publisher._tick.timer.started
    # 6.6 s are left: the next tick is when 6 are.
    assert single is True and interval == 600
    world.now += 6.6
    world.publisher._tick.timer.fire()
    assert world.endings() == [("timeout", "ignore")]


def test_a_list_shown_for_600_seconds_times_out_after_600(world):
    world.show(doorbell(timeout=600))
    world.elapse(599)
    assert world.answers() == []
    world.elapse(1)
    assert world.endings() == [("timeout", "ignore")]


# -------------------------------------------------------- exactly one answer --


def test_ok_on_something_that_is_no_row_of_the_list_answers_nothing(world):
    screen = world.show()
    for selected in ("ghost", None, "OPEN"):
        screen.controller.key_ok(selected)
    assert world.answers() == []
    assert world.screen is screen


def test_a_list_ends_once_whoever_asks_again(world):
    screen = world.show()
    controller = screen.controller
    assert world.publisher.end(controller, choicelist.PICKED, "open") is True
    assert world.publisher.end(controller, choicelist.CLOSED) is False
    assert world.publisher.end(controller, choicelist.TIMEOUT, "ignore") is False
    assert world.endings() == [("picked", "open")]


@pytest.mark.parametrize("ending", ["dismissed", "replaced", "closed"])
def test_only_a_pick_and_a_timeout_name_a_row(world, ending):
    controller = world.show().controller
    world.publisher.end(controller, ending, "open")
    assert world.endings() == [(ending, None)]


def test_a_timeout_and_an_ok_in_the_same_turn_answer_once(world):
    screen = world.show(doorbell(timeout=5))
    world.elapse(5)
    screen.key_ok()
    screen["listColours"].actions["green"]()
    screen.key_cancel()
    assert world.endings() == [("timeout", "ignore")]


def test_an_ok_and_the_timer_in_the_same_turn_answer_once(world):
    screen = world.show(doorbell(timeout=5))
    world.select("open")
    screen.key_ok()
    world.now += 10
    world.publisher._tick_fired()
    assert world.endings() == [("picked", "open")]


def test_a_newer_list_replaces_the_open_one_which_is_answered_first(world):
    first = world.show()
    world.send(doorbell(title="Bedtime", rows=[{"id": "more", "label": "+15 minutes"}],
                        default=None), ident="b2")
    assert world.endings() == [("replaced", None)]
    assert world.answers()[0]["id"] == LIST_ID
    # The old screen is closing; the new one opens once it has gone.
    world.settle()
    second = world.screen
    assert second is not None and second is not first
    assert second["title"].getText() == "Bedtime"
    second.key_ok()
    assert world.endings() == [("replaced", None), ("picked", "more")]
    assert world.answers()[1]["id"] == "b2"


def test_an_exit_on_a_replaced_list_answers_nothing_more(world):
    first = world.show()
    world.send(doorbell(title="Second"), ident="b2")
    first.key_cancel()
    first.key_ok()
    assert world.endings() == [("replaced", None)]
    world.settle()
    assert world.screen["title"].getText() == "Second"
    assert world.answers()[-1]["id"] == LIST_ID


def test_a_third_list_replaces_one_still_waiting_for_the_screen(world):
    world.show()
    world.send(doorbell(title="Second"), ident="b2")
    world.send(doorbell(title="Third"), ident="b3")
    assert [(answer["id"], answer["ending"]) for answer in world.answers()] == [
        (LIST_ID, "replaced"), ("b2", "replaced")]
    world.settle()
    assert world.screen["title"].getText() == "Third"


def test_a_refused_list_leaves_the_open_one_alone(world):
    screen = world.show()
    world.send({"title": "", "rows": []}, ident="bad")
    assert world.error()["id"] == "bad" and world.error()["reason"] == "invalid"
    assert world.answers() == []
    assert world.screen is screen


def _enter_standby(world):
    """The power key over a screen that allows it: the standby screen opens on top."""
    return world.session.open(Standby)


def _leave_standby(world, standby):
    standby.Power()
    standby.close()
    world.settle()
    world.settle()


def test_entering_standby_closes_the_list_and_it_never_shows_again(world, monkeypatch):
    from Screens.Screen import Screen

    screen = world.show()
    standby = _enter_standby(world)
    assert world.endings() == [("closed", None)]
    shown = []
    drawn = Screen.show

    def show(self):
        shown.append(self)
        drawn(self)

    monkeypatch.setattr(Screen, "show", show)
    _leave_standby(world, standby)
    assert world.screen is None
    assert world.session.current_dialog is InfoBar.instance
    assert screen not in shown, "the ended list was drawn again on its way out"
    assert world.endings() == [("closed", None)]


def test_a_pick_after_standby_closed_the_list_answers_nothing_more(world):
    screen = world.show()
    _enter_standby(world)
    screen.key_ok()
    screen["listColours"].actions["green"]()
    assert world.endings() == [("closed", None)]


def test_a_list_waiting_for_the_screen_is_closed_by_standby_too(world):
    world.show()
    world.send(doorbell(title="Second"), ident="b2")
    config.misc.standbyCounter.increment()
    assert [(answer["id"], answer["ending"]) for answer in world.answers()] == [
        (LIST_ID, "replaced"), ("b2", "closed")]


def test_a_list_is_refused_in_standby_with_the_toasts_sentence(world):
    standby_module.inStandby = object()
    world.send()
    assert world.error() == {"cmd": "list", "error": "the receiver is in standby",
                             "reason": "standby", "id": LIST_ID, "ts": world.error()["ts"]}
    assert world.screen is None
    assert world.answers() == []


def test_a_list_is_refused_while_the_shutdown_question_is_on_screen(world):
    standby_module.inTryQuitMainloop = True
    world.send()
    assert world.error()["reason"] == "standby"


def test_an_invalid_list_is_refused_with_its_id_and_the_reason(world):
    world.send(doorbell(timeout=1))
    assert world.error() == {
        "cmd": "list", "error": "timeout must be a whole number of seconds from 5 to 600",
        "reason": "invalid", "id": LIST_ID, "ts": world.error()["ts"]}


def test_the_doors_sentence_has_the_reason_busy_for_a_list(world):
    world.bridge.self_update.closed = True
    try:
        world.send()
        assert world.error() == {"cmd": "list", "error": "an update is being applied on the "
                                 "receiver", "reason": "busy", "id": LIST_ID,
                                 "ts": world.error()["ts"]}
        # Every other command keeps its sentence alone.
        world.client.fire_message("enigma2/" + NODE + "/cmd/screenshot", b"PRESS")
        assert "reason" not in world.error()
    finally:
        world.bridge.self_update.closed = False


def test_a_retained_list_is_discarded(world):
    world.send(retain=True)
    assert world.screen is None
    assert world.error() is None


def test_a_list_that_worked_clears_last_error(world):
    world.send(doorbell(timeout=1))
    assert world.error() is not None
    world.show()
    assert world.error() is None


# ------------------------------------------------------ over whatever is there --


def test_a_list_over_the_receiver_menu_leaves_the_menu_open_underneath(world):
    from test_menu import example, payload

    world.client.fire_message(MENU_TOPIC, payload(example()), retain=True)
    assert world.bridge.publisher("menu").open(world.session)
    menu = world.session.current_dialog
    assert isinstance(menu, styledlist.screen_class())
    world.show().key_cancel()
    world.settle()
    assert world.session.current_dialog is menu
    assert world.bridge.publisher("menu")._controllers


def test_a_list_over_the_keyboard_gives_it_back_its_keys(world):
    keyboard = world.session.open(VirtualKeyBoard, title="Note", text="abc")
    world.show()
    world.select("open")
    world.screen.key_ok()
    world.settle()
    assert world.session.current_dialog is keyboard


def test_a_list_waits_while_the_session_is_between_two_screens(world):
    keyboard = world.session.open(VirtualKeyBoard)
    keyboard.close(None)
    # The keyboard is closing; it is popped on the next turn.
    world.send()
    assert world.screen is None and world.error() is None
    world.settle()
    world.publisher._retry.timer.fire()
    assert world.screen is not None


def test_a_session_that_stays_between_two_screens_is_a_late_refusal(world, monkeypatch):
    monkeypatch.setattr(choicelist, "_can_open", lambda session: False)
    world.send()
    assert world.error() is None
    for _ in range(choicelist.OPEN_RETRY_LIMIT):
        world.publisher._retry.timer.fire()
    assert world.error() == {"cmd": "list", "error": "the receiver could not show the list",
                             "reason": "not_shown", "id": LIST_ID, "ts": world.error()["ts"]}
    assert world.answers() == []


def test_over_a_screen_that_allows_no_standby_the_list_allows_none(world):
    screen = world.show()
    assert not getattr(InfoBar.instance, "ALLOW_SUSPEND", None)
    assert not getattr(screen, "ALLOW_SUSPEND", None)


def test_the_power_key_does_on_the_list_what_it_does_on_the_screen_under_it(world, monkeypatch):
    monkeypatch.setattr(type(world.session.current_dialog), "ALLOW_SUSPEND", 1, raising=False)
    screen = world.show()
    assert screen.ALLOW_SUSPEND == 1


def test_a_player_that_pauses_is_paused_through_the_list(world):
    class Pausing(MoviePlayer):
        ALLOW_SUSPEND = 2
        paused = []

        def pauseService(self):
            self.paused.append("pause")

        def unPauseService(self):
            self.paused.append("unpause")

    player = world.session.open(Pausing)
    screen = world.show()
    assert screen.ALLOW_SUSPEND == 2
    screen.pauseService()
    screen.unPauseService()
    assert player.paused == ["pause", "unpause"]


def test_a_player_that_cannot_pause_offers_no_standby_from_under_a_list(world):
    class Stubborn(MoviePlayer):
        ALLOW_SUSPEND = 2

    world.session.open(Stubborn)
    screen = world.show()
    assert not getattr(screen, "ALLOW_SUSPEND", None)


def test_a_screen_closed_by_something_else_is_answered_closed(world):
    screen = world.show()
    world.session.close(screen)
    world.settle()
    assert world.endings() == [("closed", None)]


# ------------------------------------------------------------- the plugin stops --


def test_stopping_the_plugin_answers_closed_before_offline(world):
    world.show()
    world.bridge.stop()
    topics = [entry.topic for entry in world.client.published]
    assert world.endings() == [("closed", None)]
    assert topics.index(ANSWER) < len(topics) - 1 - topics[::-1].index(AVAILABILITY)
    assert world.client.last(AVAILABILITY).text == "offline"


def test_a_settings_save_answers_closed_on_the_session_that_ends(world):
    old = world.client
    world.show()
    world.bridge.reload()
    assert [entry.json()["ending"] for entry in old.all_for(ANSWER)] == ["closed"]


def test_the_doors_of_an_update_close_the_list(world):
    world.show()
    updater = world.bridge.self_update
    updater._current = {"id": "u1"}
    updater._close_doors()
    try:
        assert world.endings() == [("closed", None)]
    finally:
        updater.closed = False


def test_without_a_session_up_the_stop_sends_nothing_later(world):
    world.show()
    world.client.fire_disconnect()
    world.bridge.stop()
    assert world.answers() == []
    assert world.client.queued_for_reconnect() == []


def test_the_uninstall_answers_closed_before_it_closes_the_doors(world):
    world.show()
    uninstaller = world.bridge.uninstaller
    before = len(world.client.published)
    uninstaller.phase = "scheduled"
    uninstaller._begin_steps()
    assert uninstaller.closed
    assert world.endings() == [("closed", None)]
    # Before a single retraction.
    assert world.client.published[before].topic == ANSWER


# ------------------------------------------------------------- never queued --


def test_an_answer_the_broker_has_not_taken_is_dropped_when_the_connection_goes(world):
    world.show().key_cancel()
    assert world.client.queued_for_reconnect() == [ANSWER]
    world.client.fire_disconnect()
    assert world.client.queued_for_reconnect() == []


def test_an_answer_is_dropped_at_the_reconnect_the_main_loop_has_not_seen_go(world):
    world.show().key_cancel()
    world.client.fire_connect()
    assert world.client.queued_for_reconnect() == []
    assert len(world.answers()) == 1


def test_an_acknowledged_answer_is_not_touched_by_a_disconnect(world, plugin_log):
    world.show().key_cancel()
    world.client.acknowledge()
    world.client.fire_disconnect()
    assert "dropped a list answer" not in plugin_log()


def test_an_answer_the_socket_refused_is_taken_back_at_once(world):
    screen = world.show()
    world.client.publish_rc = 4  # MQTT_ERR_NO_CONN
    screen.key_cancel()
    assert world.client.queued_for_reconnect() == []


def test_a_pick_without_a_broker_connection_is_lost_not_kept(world, plugin_log):
    screen = world.show()
    world.client.fire_disconnect()
    screen.key_cancel()
    assert world.answers() == []
    assert "the answer was not sent" in plugin_log()
    world.client.fire_connect()
    assert world.answers() == []


# --------------------------------------------------------------- the fallback --


@pytest.fixture
def fallback(make_bridge, factory, settings, monkeypatch):
    _no_styled_screen(monkeypatch)
    return World(make_bridge, factory, settings)


def _choicebox(world):
    dialog = world.session.current_dialog
    return dialog if isinstance(dialog, ChoiceBox) else None


def test_the_fallback_is_the_images_list_with_the_same_rows_ids_and_keys(fallback):
    fallback.send()
    box = _choicebox(fallback)
    assert box is not None
    assert box.title == ("From Home Assistant, via MQTT Bridge\nDoorbell\n"
                         "Someone is at the gate.")
    assert box.list == [("Open the gate", "open"), ("Ignore", "ignore")]
    assert box.keys == ["green", "red"]
    assert box.selection == 1
    box.close(box.list[0])
    fallback.settle()
    assert fallback.endings() == [("picked", "open")]
    assert _choicebox(fallback) is None


def test_exit_on_the_fallback_dismisses(fallback):
    fallback.send(doorbell(rows=[{"id": "a", "label": "A"}], default=None))
    box = _choicebox(fallback)
    assert box.keys == [""]
    box.close(None)
    fallback.settle()
    assert fallback.endings() == [("dismissed", None)]


def test_a_timeout_closes_the_fallback_and_answers_once(fallback):
    fallback.send(doorbell(timeout=5))
    assert _choicebox(fallback) is not None
    fallback.elapse(5)
    assert fallback.endings() == [("timeout", "ignore")]
    fallback.settle()
    assert _choicebox(fallback) is None
    assert fallback.endings() == [("timeout", "ignore")]


def test_the_fallback_counts_down_in_its_title_where_the_image_lets_it(fallback, monkeypatch):
    titles = []
    monkeypatch.setattr(ChoiceBox, "setTitle", lambda self, text: titles.append(text),
                        raising=False)
    fallback.send(doorbell(timeout=5))
    fallback.elapse(2)
    # As on OpenViX and OpenATV: the header line is the window title, and stays in it.
    header = "From Home Assistant, via MQTT Bridge - "
    assert titles == [header + "Ignore in 5 s", header + "Ignore in 4 s",
                      header + "Ignore in 3 s"]


def test_where_the_image_keeps_the_header_in_its_text_the_title_is_the_countdown(
        fallback, monkeypatch):
    """As on OpenPLi: the whole question in the list's text, the window title its own."""
    from conftest import Label

    titles = []
    monkeypatch.setattr(ChoiceBox, "setTitle", lambda self, text: titles.append(text),
                        raising=False)
    monkeypatch.setattr(ChoiceBox, "__getitem__",
                        lambda self, name: Label(self.title) if name == "text" else None,
                        raising=False)
    fallback.send(doorbell(timeout=5, default=None))
    assert titles == ["Closes in 5 s"]


def test_where_the_image_splits_off_the_header_the_title_keeps_it(fallback, monkeypatch):
    """As on OpenViX and OpenATV: the first line - the header - in the window title, the rest
    of the question in the list's text."""
    from conftest import Label

    titles = []
    monkeypatch.setattr(ChoiceBox, "setTitle", lambda self, text: titles.append(text),
                        raising=False)
    monkeypatch.setattr(
        ChoiceBox, "__getitem__",
        lambda self, name: Label(self.title.split(chr(10), 1)[1]) if name == "text" else None,
        raising=False)
    fallback.send(doorbell(timeout=5, default=None))
    assert titles == ["From Home Assistant, via MQTT Bridge - Closes in 5 s"]


def test_a_fallback_that_takes_no_title_shows_no_countdown(fallback, plugin_log):
    fallback.send(doorbell(timeout=5))
    fallback.elapse(1)
    assert plugin_log().count("it shows no countdown") == 1
    fallback.elapse(4)
    assert fallback.endings() == [("timeout", "ignore")]


def test_a_newer_list_replaces_the_fallback(fallback):
    fallback.send()
    first = _choicebox(fallback)
    fallback.send(doorbell(title="Second"), ident="b2")
    assert fallback.endings() == [("replaced", None)]
    fallback.settle()
    second = _choicebox(fallback)
    assert second is not None and second is not first
    assert "Second" in second.title
    assert fallback.endings() == [("replaced", None)]


def test_standby_closes_the_fallback_and_a_pick_after_it_answers_nothing(fallback):
    fallback.send()
    box = _choicebox(fallback)
    picked = box.callback
    _enter_standby(fallback)
    assert fallback.endings() == [("closed", None)]
    # The image's list hands over the pick it had after the list was closed: nothing more.
    picked(box.list[0])
    assert fallback.endings() == [("closed", None)]


def test_a_styled_screen_that_cannot_be_built_at_opening_uses_the_fallback(world, monkeypatch):
    class Broken:
        def __init__(self, *arguments, **keywords):
            raise KeyError("list")

    monkeypatch.setattr(choicelist, "screen_class", lambda: Broken)
    world.send()
    assert _choicebox(world) is not None
    assert world.info()["list"] == {"screen": "choicebox"}
    assert world.error() is None


def test_a_list_neither_screen_can_show_is_refused(world, monkeypatch):
    class Broken:
        def __init__(self, *arguments, **keywords):
            raise KeyError("list")

    monkeypatch.setattr(choicelist, "screen_class", lambda: Broken)
    monkeypatch.setattr(styledlist, "fallback_refusal", lambda: "no ChoiceBox")
    world.send()
    assert world.error()["reason"] == "not_shown"
    assert world.answers() == []


# --------------------------------------------------------------- the log --


def test_the_log_never_holds_the_senders_text(world, plugin_log):
    world.show(doorbell(title=SECRET, text=SECRET,
                        rows=[{"id": "x" + "S" * 20, "label": SECRET, "key": "1"}],
                        default=None, extra=SECRET))
    world.screen["listNumbers"].actions["1"]("1")
    log = plugin_log()
    assert SECRET not in log
    assert "S" * 20 not in log
    assert "list (id " + LIST_ID + ") ended: picked" in log


def test_members_this_plugin_does_not_know_are_logged_once_a_run(world, plugin_log):
    world.show(doorbell(colour="red"))
    world.screen.key_cancel()
    world.settle()
    world.show(doorbell(sound="ding"))
    assert plugin_log().count("member(s) this plugin does not know were ignored") == 1


# ---------------------------------------------- the image's plain session too --


def test_on_the_plain_session_model_the_trial_build_reads_the_skin(make_bridge, factory,
                                                                    settings, receiver):
    world = World(make_bridge, factory, settings, receiver=receiver)
    assert world.info()["list"] == {"screen": "styled"}
    assert isinstance(receiver.session, Session)


# ------------------------------------------------------ the text rule, both halves --

# The vectors both halves of the contract hold their text rule to: (raw, as a title, as text).
# Each is built from code points, so the source holds nothing invisible.
NUL, SOH, STX = chr(0), chr(1), chr(2)
ZWSP, RLO, LRI, SHY, BOM = chr(0x200B), chr(0x202E), chr(0x2066), chr(0xAD), chr(0xFEFF)
LONE_HIGH, LONE_LOW = chr(0xD800), chr(0xDFFF)
TEXT_RULE = [
    (ZWSP, None, None),
    (RLO + LRI, None, None),
    (SOH + STX, None, None),
    (SHY, None, None),
    (BOM, None, None),
    ("  Open  ", "Open", "Open"),
    ("a" + NUL + "b", "a b", "a b"),
    (ZWSP * 10 + "x" * 60, "x" * 60, "x" * 60),
    ("Line1" + chr(10) + "Line2", "Line1 Line2", "Line1" + chr(10) + "Line2"),
    ("x" + LONE_HIGH + "y", "xy", "xy"),
    (LONE_LOW, None, None),
]


@pytest.mark.parametrize("raw, title, text", TEXT_RULE)
def test_the_text_rule_on_a_title_and_a_text(raw, title, text):
    if title is None:
        assert refusal(doorbell(title=raw)) == "title is empty"
    else:
        assert parsed(title=raw)["title"] == title
    assert parsed(text=raw)["text"] == text


@pytest.mark.parametrize("raw, title, _text", TEXT_RULE)
def test_the_text_rule_on_a_label(raw, title, _text):
    members = [{"id": "a", "label": raw}]
    if title is None:
        assert refusal(doorbell(rows=members, default=None)) == "rows[0].label is empty"
    else:
        assert parsed(rows=members, default=None)["rows"][0]["label"] == title


def test_a_lone_surrogate_in_a_list_without_an_envelope_never_reaches_the_screen(world):
    """JSON can spell half a surrogate pair; the renderer would be handed it unchecked."""
    backslash = chr(92)
    raw = ('{"title": "Door' + backslash + 'ud800bell", "text": "a' + backslash + 'udfffb", '
           '"rows": [{"id": "a", "label": "Op' + backslash + 'udc00en"}]}')
    world.send(raw=raw, ident=None)
    screen = world.screen
    assert screen["title"].getText() == "Doorbell"
    assert screen["text"].getText() == "ab"
    assert screen["list"].list[0][1]["text"] == "Open"
    world.send(raw='{"title": "' + backslash + 'udfff", "rows": [{"id": "a", "label": "A"}]}',
               ident=None)
    assert world.error()["error"] == "title is empty"


# ------------------------------------------------------------ the keys' pictures --


def _render_keys():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tools" / "render-keys.py"
    spec = importlib.util.spec_from_file_location("render_keys", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["red", "green", "yellow", "blue", "box"])
def test_every_key_picture_is_the_one_the_tool_draws(name):
    tool = _render_keys()
    with open(choicelist.key_path(name), "rb") as handle:
        assert handle.read() == tool.render(name)


@pytest.mark.parametrize("name", ["red", "green", "yellow", "blue"])
def test_a_button_is_drawn_in_the_colour_the_second_way_fills(name):
    _path, colour = _render_keys().KEYS[name]
    assert (colour[0] << 16) | (colour[1] << 8) | colour[2] == choicelist.KEY_RGB[name]


def test_the_button_fills_the_share_of_its_square_the_second_way_draws():
    tool = _render_keys()
    alpha = [[row[column] for row in _alpha(tool.render("green"))] for column in (32,)][0]
    filled = [index for index, value in enumerate(alpha) if value > 127]
    assert (filled[-1] - filled[0] + 1) / 64 == choicelist.BUTTON_SHARE


def _alpha(png):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "tools" / "render-icons.py"
    spec = importlib.util.spec_from_file_location("render_icons", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_png_alpha(png)


def test_an_image_that_places_no_pixmap_fills_a_colour_key_and_writes_a_digit(world,
                                                                             monkeypatch):
    import Components.MultiContent as multicontent

    monkeypatch.delattr(multicontent, "MultiContentEntryPixmapAlphaBlend")
    screen = world.show(doorbell(rows=[{"id": "a", "label": "A", "key": "green"},
                                       {"id": "b", "label": "B", "key": "7"}], default=None))
    green, seven = screen["list"].list
    assert green[2]["text"] == "" and green[2]["backcolor"] == choicelist.KEY_RGB["green"]
    assert green[2]["backcolor_sel"] == choicelist.KEY_RGB["green"]
    assert [part["text"] for part in seven[1:]] == ["B", "7"]


# ------------------------------------------------------- the text sized to its content --


@pytest.fixture
def sizes_without_equality(monkeypatch):
    """The image's `eSize` as Python sees it: SWIG exposes no `==`, so two equal sizes are two
    objects that compare by identity. A layout that compared sizes would never shrink here."""
    monkeypatch.setattr(eSize, "__eq__", object.__eq__)
    monkeypatch.setattr(eSize, "__ne__", object.__ne__)
    monkeypatch.setattr(eSize, "__hash__", object.__hash__)


def _number(size):
    return size.width(), size.height()


def _laid_out(spec, width=1920, height=1080):
    """The styled screen as the plain session builds it: skin read, applied, laid out."""
    DESKTOP.resize(eSize(width, height))
    session = Session(nav=None)

    class Clock:
        clock = staticmethod(lambda: 0.0)

    controller = choicelist.ListController(Clock(), spec)
    shape = choicelist.desktop_shape(spec)
    return session.instantiateDialog(choicelist.screen_class(), shape,
                                     choicelist._Trial(controller))


@pytest.mark.parametrize("width, height", [(1280, 720), (1920, 1080)])
def test_a_60_character_title_and_a_200_character_text_are_shown_whole(
        width, height, sizes_without_equality):
    spec = parsed(title="T" * 60, text="x " * 99 + "y", default=None)
    screen = _laid_out(spec, width, height)
    for name in ("title", "text"):
        widget = screen[name]
        needed = widget.instance.calculateSize().height()
        assert widget.instance.size().height() >= needed, name
    window = screen.instance
    assert window.position().y() + window.size().height() <= height
    # Laid out one under the other, nothing overlapping.
    title, text, listed = (screen[name].instance for name in ("title", "text", "list"))
    assert title.position().y() + title.size().height() <= text.position().y()
    assert text.position().y() + text.size().height() <= listed.position().y()


def test_a_short_title_and_no_text_make_a_short_screen(sizes_without_equality):
    long = _laid_out(parsed(title="T" * 60, text="x" * 200, default=None))
    short = _laid_out(parsed(title="Bell", text=None, default=None))
    assert short["text"].instance.size().height() == 0
    assert short["title"].instance.size().height() < long["title"].instance.size().height()
    assert short.instance.size().height() < long.instance.size().height()
    # Still in the middle of the desktop.
    window = short.instance
    assert abs(window.position().y() * 2 + window.size().height() - 1080) <= 1


def test_a_skin_that_restyles_the_screen_keeps_its_own_layout():
    skin_module.domScreens["MQTTBridgeChoices"] = (
        '<screen name="MQTTBridgeChoices" position="0,0" size="800,600">'
        '<widget name="title" position="10,10" size="700,40" />'
        '<widget name="list" position="10,60" size="700,400" /></screen>')
    screen = _laid_out(parsed(title="T" * 60, text="x" * 200, default=None))
    assert _number(screen.instance.size()) == (800, 600)
    assert _number(screen["title"].instance.size()) == (700, 40)


NINE_LINES = chr(10).join(f"Line {number}" for number in range(1, 10))


@pytest.mark.parametrize("width, height", [(1280, 720), (1920, 1080)])
def test_a_text_of_nine_short_lines_is_shown_whole(width, height, sizes_without_equality):
    """Nine lines need more than the five the skin gives the text: it grows."""
    screen = _laid_out(parsed(text=NINE_LINES, default=None), width, height)
    text = screen["text"].instance
    assert text.size().height() >= text.calculateSize().height()
    assert text.calculateSize().height() > screen.shape["text"]
    listed = screen["list"].instance
    assert text.position().y() + text.size().height() <= listed.position().y()
    window = screen.instance
    assert window.position().y() >= 0
    assert window.position().y() + window.size().height() <= height


@pytest.mark.parametrize("width, height", [(1280, 720), (1920, 1080)])
def test_a_long_text_takes_rows_from_the_list_which_scrolls(width, height,
                                                           sizes_without_equality):
    # Eighteen lines: more than the five the skin gives, less than the desktop leaves.
    text = chr(10).join(f"Line {number}" for number in range(1, 19))
    screen = _laid_out(parsed(text=text, rows=rows(24), default=None), width, height)
    widget = screen["text"].instance
    assert widget.size().height() >= widget.calculateSize().height()
    listed = screen["list"].instance
    shown = listed.size().height() // screen.shape["row"]
    assert 1 <= shown < choicelist.VISIBLE_ROWS
    assert listed.size().height() == shown * screen.shape["row"]
    window = screen.instance
    assert window.position().y() + window.size().height() <= height


@pytest.mark.parametrize("height", [400, 576, 720, 1080, 2160])
def test_the_tallest_list_fits_every_desktop(height):
    shape = choicelist.geometry(height * 16 // 9, height, 24, True)
    assert shape["visible"] == choicelist.VISIBLE_ROWS
    assert shape["y"] >= 0 and shape["y"] + shape["height"] <= height


# ------------------------------------------------- a fallback that fails to open --


def _raising(*_arguments, **_keywords):
    raise RuntimeError("no skin")


def test_a_choicebox_that_raises_inside_open_gives_the_session_back(world, monkeypatch):
    class Broken:
        def __init__(self, *arguments, **keywords):
            raise KeyError("list")

    infobar = world.session.current_dialog
    monkeypatch.setattr(choicelist, "screen_class", lambda: Broken)
    monkeypatch.setattr(ChoiceBox, "__init__", _raising)
    world.send()
    assert world.error()["reason"] == "not_shown"
    assert world.session.current_dialog is infobar
    assert world.session.in_exec and infobar.execing


def test_the_menus_choicebox_that_raises_inside_open_gives_the_session_back(world,
                                                                           monkeypatch):
    from test_menu import example, payload

    monkeypatch.setattr(ChoiceBox, "__init__", _raising)
    infobar = world.session.current_dialog
    world.client.fire_message(MENU_TOPIC, payload(example()), retain=True)
    menu = world.bridge.publisher("menu")
    menu.screen_kind = styledlist.FALLBACK
    assert menu.open(world.session) is False
    assert world.session.current_dialog is infobar
    assert world.session.in_exec and infobar.execing


def test_an_image_whose_choicebox_cannot_be_built_has_no_fallback(make_bridge, factory,
                                                                  settings, monkeypatch):
    _no_styled_screen(monkeypatch)
    monkeypatch.setattr(ChoiceBox, "__init__", _raising)
    world = World(make_bridge, factory, settings)
    assert world.publisher is None
    assert "list" not in world.info()["capabilities"]


# ------------------------------------------------------ a list that waits, again --


def test_a_list_that_replaces_a_waiting_one_waits_its_own_full_time(world, monkeypatch):
    monkeypatch.setattr(choicelist, "_can_open", lambda session: False)
    world.send()
    for _ in range(choicelist.OPEN_RETRY_LIMIT - 1):
        world.publisher._retry.timer.fire()
    world.send(doorbell(title="Second"), ident="b2")
    for _ in range(choicelist.OPEN_RETRY_LIMIT - 1):
        world.publisher._retry.timer.fire()
    assert world.error() is None, "the second list inherited the first one's turns"
    world.publisher._retry.timer.fire()
    assert world.error()["id"] == "b2" and world.error()["reason"] == "not_shown"


# ----------------------------------------------- the doors while the plugin is silent --


def test_the_doors_refuse_a_list_even_while_an_update_keeps_the_plugin_silent(world):
    updater = world.bridge.self_update
    updater.closed = True
    updater.silent = True
    try:
        before = len(world.client.published)
        world.send()
        (refused,) = world.client.published[before:]
        assert refused.topic == LAST_ERROR and refused.retain is False and refused.qos == 1
        assert refused.json() == {"cmd": "list", "error": "an update is being applied on the "
                                  "receiver", "reason": "busy", "id": LIST_ID,
                                  "ts": refused.json()["ts"]}
        # Any other command stays unsaid, as before.
        world.client.fire_message("enigma2/" + NODE + "/cmd/screenshot", b"PRESS")
        assert len(world.client.published) == before + 1
        # Never queued: one the broker had not acknowledged is gone at the next connect.
        assert world.client.queued_for_reconnect() == [LAST_ERROR]
        world.client.fire_connect()
        assert world.client.queued_for_reconnect() == []
    finally:
        updater.closed = False
        updater.silent = False


def test_without_a_connection_the_silent_refusal_is_not_handed_to_the_client(world):
    updater = world.bridge.self_update
    updater.closed = True
    updater.silent = True
    try:
        world.client.fire_disconnect()
        before = len(world.client.published)
        assert world.bridge.say_refusal_unretained("list", "x", command_id=LIST_ID) is None
        world.send()
        assert len(world.client.published) == before
    finally:
        updater.closed = False
        updater.silent = False


# ------------------------------------------------- a Save on the receiver while a list is open --


def _live_settings():
    from test_save_applies_live import LIVE

    return LIVE


@pytest.mark.parametrize("name", _live_settings())
def test_a_save_that_does_not_reconnect_leaves_the_list_open(world, name):
    """A Save is applied under the open session: the list on the screen is not touched."""
    from test_save_applies_live import other_value

    from MQTTBridge import config as settings_module

    screen = world.show()
    client = world.client
    current = settings_module.value(name, world.bridge.settings)
    getattr(world.bridge.settings, name).value = other_value(name, current)

    assert world.bridge.apply_saved_settings() is False
    assert world.answers() == []
    assert world.screen is screen
    # Answered as usual, on the same session.
    screen.key_cancel()
    assert world.endings() == [("dismissed", None)]
    assert world.client is client


def test_a_live_save_of_the_menus_transparency_leaves_the_list_open(world):
    screen = world.show()
    world.bridge.settings.menu_transparency.value = 60
    assert world.bridge.apply_saved_settings() is False
    assert world.screen is screen and world.answers() == []
    world.select("open")
    screen.key_ok()
    assert world.endings() == [("picked", "open")]


def test_a_save_that_reconnects_closes_the_list_on_the_session_that_ends(world):
    old = world.client
    world.show()
    world.bridge.settings.host.value = "10.0.0.6"
    assert world.bridge.apply_saved_settings() is True
    assert [entry.json()["ending"] for entry in old.all_for(ANSWER)] == ["closed"]
    # Before the old session says `offline` and lets go.
    topics = [entry.topic for entry in old.published]
    assert topics.index(ANSWER) < topics.index(AVAILABILITY, topics.index(ANSWER))
