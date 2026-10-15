"""The receiver menu: the payload, the navigation, the pending mark, greying and the open paths.

The menu is published by the companion integration on `enigma2mqtt/menu/<node>`; the receiver
draws it and answers with `menu_action` (docs/TOPICS.md, section 3 and `menu_action`). The tests
drive it the way a household does - a long HELP in live TV, then OK, EXIT, LEFT, RIGHT and the
number keys on the screen that opened - against `StartEnigma`'s modal session, so that a screen
that opens is the executing dialog and a close pops it on the next turn of the main loop.
"""

import json
import re

import pytest
from conftest import (
    ChoiceBox,
    MainLoop,
    Receiver,
    VirtualKeyBoard,
    config,
    plugin_component_module,
)

from MQTTBridge import menu as menu_module
from MQTTBridge import plugin as plugin_module
from MQTTBridge import styledlist
from MQTTBridge.hotkey import CONTEXT
from MQTTBridge.menu import InvalidMenu, UnknownVersion, parse_menu

NODE = "vuuno4kse_005301"
MENU_TOPIC = "enigma2mqtt/menu/" + NODE
STATUS_TOPIC = "homeassistant/status"
ACTION_TOPIC = "enigma2/" + NODE + "/menu_action"
INFO = "enigma2/" + NODE + "/info"
DEVICE = "dreambox remote control (native)"
KEY_HELP = 138
KEY_RED = 398
MAKE, BREAK, LONG = 0, 1, 3


def example():
    """The contract's example, as the integration publishes it."""
    return {
        "v": 1, "rev": 42, "title": "Home Assistant",
        "items": [
            {"type": "group", "id": "g_salon", "label": "Salon", "icon": "sofa",
             "rows": [
                 {"type": "toggle", "id": "r_lamp", "label": "Lamp", "icon": "lamp",
                  "value": True, "state": "on", "color": "green", "available": True},
                 {"type": "slider", "id": "r_blind", "label": "Blind", "icon": "blinds",
                  "value": 40, "min": 0, "max": 100, "step": 10, "unit": "%",
                  "state": "40 %", "available": True},
                 {"type": "selector", "id": "r_mode", "label": "Heating", "icon": "radiator",
                  "value": "heat", "options": ["off", "heat", "auto"], "state": "heat",
                  "available": True}]},
            {"type": "button", "id": "r_movie", "label": "Movie night", "icon": "movie",
             "state": None, "available": True},
            {"type": "indicator", "id": "r_door", "label": "Front door", "icon": "door",
             "value": False, "state": "closed", "color": "green", "available": True},
            {"type": "input", "id": "r_note", "label": "Note", "icon": "note",
             "value": "", "max_length": 60, "state": "", "available": True},
        ],
    }


def payload(menu):
    return json.dumps(menu).encode("utf-8")


def changed(menu, ident, **fields):
    """The menu with one row's fields replaced, and a higher `rev`."""
    menu = json.loads(json.dumps(menu))
    menu["rev"] += 1
    for item in menu["items"]:
        for row in item.get("rows", [item]):
            if row["id"] == ident:
                row.update(fields)
    return menu


# ---------------------------------------------------------------- the payload --


def test_the_contract_example_is_a_menu():
    menu = parse_menu(payload(example()))
    assert menu["rev"] == 42
    assert menu["title"] == "Home Assistant"
    assert [item["id"] for item in menu["items"]] == ["g_salon", "r_movie", "r_door", "r_note"]
    assert sorted(menu["rows"]) == ["r_blind", "r_door", "r_lamp", "r_mode", "r_movie", "r_note"]
    assert menu["rows"]["r_mode"]["options"] == [("off", "off"), ("heat", "heat"),
                                                 ("auto", "auto")]


@pytest.mark.parametrize("empty", [b"", b"   ", None])
def test_an_empty_payload_is_no_menu(empty):
    assert parse_menu(empty) is None


def _broken(**changes):
    menu = example()
    menu.update(changes)
    return menu


def _row(row):
    menu = example()
    menu["items"] = [row]
    return menu


TOGGLE = {"type": "toggle", "id": "r_x", "label": "X", "icon": "lamp", "value": True,
          "state": "on", "color": "green", "available": True}
SLIDER = {"type": "slider", "id": "r_s", "label": "S", "value": 1, "min": 0, "max": 10,
          "step": 1, "unit": None, "state": "1", "available": True}
SELECTOR = {"type": "selector", "id": "r_o", "label": "O", "value": "a", "options": ["a", "b"],
            "state": "a", "available": True}
INPUT = {"type": "input", "id": "r_i", "label": "I", "value": "x", "max_length": 10,
         "state": "", "available": True}


def _with(base, **fields):
    row = dict(base)
    row.update(fields)
    return _row(row)


INVALID = [
    pytest.param(b"{not json", id="not-json"),
    pytest.param(b"[1, 2]", id="not-an-object"),
    pytest.param(b"\xff\xfe", id="not-utf8"),
    pytest.param(_broken(v=None), id="no-version"),
    pytest.param(_broken(v=True), id="version-bool"),
    pytest.param(_broken(rev="42"), id="rev-text"),
    pytest.param(_broken(rev=True), id="rev-bool"),
    pytest.param(_broken(items={}), id="items-not-a-list"),
    pytest.param(_broken(items=[dict(TOGGLE, id=f"r{n}") for n in range(25)]),
                 id="25-items"),
    pytest.param(_broken(items=[{"type": "group", "id": f"g{n}", "label": "G", "rows": []}
                                for n in range(9)]), id="9-groups"),
    pytest.param(_broken(items=[{"type": "group", "id": "g", "label": "G",
                                 "rows": [dict(TOGGLE, id=f"r{n}") for n in range(25)]}]),
                 id="25-rows-in-a-group"),
    pytest.param(_broken(items=[{"type": "group", "id": "g", "label": "G",
                                 "rows": [{"type": "group", "id": "h", "rows": []}]}]),
                 id="a-group-in-a-group"),
    pytest.param(_broken(items=[{"type": "group", "id": "g", "label": "G", "rows": None}]),
                 id="group-without-rows"),
    pytest.param(_with(TOGGLE, id="r x"), id="id-with-a-space"),
    pytest.param(_with(TOGGLE, id="r" * 33), id="id-of-33"),
    pytest.param(_with(TOGGLE, id=""), id="empty-id"),
    pytest.param(_with(TOGGLE, id=7), id="numeric-id"),
    pytest.param(_broken(items=[TOGGLE, dict(INPUT, id="r_x")]), id="duplicate-id"),
    pytest.param(_broken(items=[{"type": "group", "id": "r_x", "label": "G", "rows": []},
                                TOGGLE]), id="group-and-row-share-an-id"),
    pytest.param(_with(TOGGLE, label=5), id="label-not-text"),
    pytest.param(_with(TOGGLE, state=1), id="state-not-text"),
    pytest.param(_with(TOGGLE, value="on"), id="toggle-value-text"),
    pytest.param(_with(TOGGLE, available="yes"), id="available-not-bool"),
    pytest.param(_with(SELECTOR, value="c"), id="selector-value-not-an-option"),
    pytest.param(_with(SELECTOR, options=[f"o{n}" for n in range(25)], value=None),
                 id="25-options"),
    pytest.param(_with(SELECTOR, options=["a", 2]), id="option-not-text"),
    pytest.param(_with(SELECTOR, options="a,b"), id="options-not-a-list"),
    pytest.param(_with(SLIDER, step=0), id="step-0"),
    pytest.param(_with(SLIDER, step=-1), id="step-negative"),
    pytest.param(_with(SLIDER, min=10, max=0), id="max-below-min"),
    pytest.param(_with(SLIDER, max=201), id="201-steps"),
    pytest.param(_with(SLIDER, min="0"), id="min-text"),
    pytest.param(_with(SLIDER, step=True), id="step-bool"),
    pytest.param(_with(SLIDER, value="1"), id="slider-value-text"),
    pytest.param(_with(SLIDER, max=float("inf")), id="max-infinite"),
    pytest.param(_with(SLIDER, unit=8), id="unit-not-text"),
    pytest.param(_with(INPUT, max_length=0), id="max-length-0"),
    pytest.param(_with(INPUT, max_length=256), id="max-length-256"),
    pytest.param(_with(INPUT, max_length=True), id="max-length-bool"),
    pytest.param(_with(INPUT, value=5), id="input-value-number"),
    pytest.param(_broken(items=["r_x"]), id="row-not-an-object"),
    pytest.param(_broken(items=[{"id": "r_x"}]), id="row-without-a-type"),
    pytest.param(_broken(title=3), id="title-not-text"),
]


@pytest.mark.parametrize("raw", INVALID)
def test_a_payload_that_breaks_the_contract_is_refused(raw):
    with pytest.raises(InvalidMenu):
        parse_menu(raw if isinstance(raw, bytes) else payload(raw))


def test_a_payload_over_64_kib_is_refused_unread():
    menu = example()
    menu["padding"] = "x" * 70000
    with pytest.raises(InvalidMenu, match="over 65536"):
        parse_menu(payload(menu))


def test_a_version_this_plugin_does_not_know_is_its_own_refusal():
    with pytest.raises(UnknownVersion) as raised:
        parse_menu(payload(_broken(v=2)))
    assert raised.value.version == 2


def test_the_bounds_themselves_are_a_menu():
    groups = [{"type": "group", "id": f"g{n}", "label": "G", "icon": "home",
               "rows": [dict(TOGGLE, id=f"g{n}r{m}") for m in range(24)]}
              for n in range(8)]
    rows = [dict(TOGGLE, id=f"t{n}") for n in range(16)]
    menu = parse_menu(payload(_broken(items=groups + rows)))
    assert len(menu["items"]) == 24
    assert len(menu["rows"]) == 8 * 24 + 16
    assert parse_menu(payload(_with(SLIDER, min=0, max=100, step=0.5)))
    assert parse_menu(payload(_with(INPUT, max_length=255)))
    assert parse_menu(payload(_with(SELECTOR, options=[f"o{n}" for n in range(24)],
                                    value="o3")))
    assert parse_menu(payload(_with(TOGGLE, id="a" * 32)))


def test_text_is_cut_and_loses_what_the_renderer_would_read():
    menu = parse_menu(payload(_broken(
        title="T" * 70,
        items=[dict(TOGGLE, label="L" * 61, state="S" * 25),
               dict(SLIDER, unit="percent!!"),
               dict(INPUT, label="a\\cFFFF0000b\nc\x07d")],
    )))
    row = menu["rows"]["r_x"]
    assert menu["title"] == "T" * 60
    assert row["label"] == "L" * 60
    assert row["state"] == "S" * 24
    assert menu["rows"]["r_s"]["unit"] == "percent!"
    assert menu["rows"]["r_i"]["label"] == "acFFFF0000b c d"


def test_what_the_plugin_does_not_know_is_neutral_or_ignored():
    menu = parse_menu(payload(_broken(
        title=None,
        future_field={"x": 1},
        items=[dict(TOGGLE, icon="rocket", color="blue", extra=1),
               dict(INPUT, icon=None),
               {"type": "dial", "id": "r_d", "label": "Dial", "value": 3, "available": True}],
    )))
    assert menu["title"] == "Home Assistant"
    assert menu["rows"]["r_x"]["icon"] == "neutral"
    assert menu["rows"]["r_x"]["color"] is None
    assert menu["rows"]["r_i"]["icon"] == "neutral"
    dial = menu["rows"]["r_d"]
    assert dial["type"] == "dial"
    assert dial["value"] is None


def test_a_row_without_available_is_available():
    row = dict(TOGGLE)
    del row["available"]
    assert parse_menu(payload(_row(row)))["rows"]["r_x"]["available"] is True


@pytest.mark.parametrize("minimum, step, values", [
    (0, 10, [0, 10, 20]),
    (0, 0.1, [0, 0.1, 0.2, 0.3]),
    (16.5, 0.5, [16.5, 17.0, 17.5]),
    (-3, 1.5, [-3.0, -1.5, 0.0]),
])
def test_a_slider_steps_without_binary_noise(minimum, step, values):
    row = {"min": minimum, "max": minimum + step * 100, "step": step}
    got = [menu_module.slider_value(row, index) for index in range(len(values))]
    assert got == values
    assert all(isinstance(value, int) for value in got) == (
        float(minimum).is_integer() and float(step).is_integer())


def test_a_slider_value_is_shown_without_noise():
    assert menu_module.format_number(0.30000000000000004) == "0.3"
    assert menu_module.format_number(40.0) == "40"
    assert menu_module.format_number(None) == ""


# ------------------------------------------------------------------- the world --


class World:
    """A receiver in live TV, a connected bridge, and the menu as the integration sent it."""

    def __init__(self, make_bridge, factory, settings, **changes):
        self.receiver = Receiver(modal=True)
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
        self.menu = example()

    @property
    def session(self):
        return self.receiver.session

    @property
    def publisher(self):
        return self.bridge.publisher("menu")

    @property
    def screen(self):
        dialog = self.session.current_dialog
        return dialog if isinstance(dialog, styledlist.screen_class()) else None

    def send_menu(self, menu=None, raw=None):
        if menu is not None:
            self.menu = menu
        self.client.fire_message(MENU_TOPIC, raw if raw is not None else payload(self.menu),
                                 retain=True)

    def status(self, text):
        self.client.fire_message(STATUS_TOPIC, text.encode("utf-8"))

    def press(self, code, *flags):
        return [self.receiver.actions.keyPressed(DEVICE, code, flag) for flag in flags]

    def long_help(self):
        return self.press(KEY_HELP, MAKE, LONG, BREAK)

    def open(self):
        self.send_menu()
        self.long_help()
        assert self.screen is not None, "the menu did not open"
        return self.screen

    def actions(self):
        return [json.loads(entry.text) for entry in self.client.all_for(ACTION_TOPIC)]

    def view(self):
        return {row["id"]: row for row in self.screen.controller.view()["rows"]}

    def select(self, ident):
        self.screen.select(ident)

    def ok(self, ident=None):
        if ident is not None:
            self.select(ident)
        self.screen.key_ok()

    def exit(self):
        self.screen.key_cancel()
        MainLoop.advance(0)

    def notice(self):
        return self.screen["notice"].getText()

    def info(self):
        return self.client.last(INFO).json()


@pytest.fixture
def world(make_bridge, factory, settings):
    return World(make_bridge, factory, settings)


# ------------------------------------------------------------- subscriptions --


def test_the_menu_and_home_assistant_status_are_subscribed_at_qos_1(world):
    assert (MENU_TOPIC, 1) in world.client.subscriptions
    assert (STATUS_TOPIC, 1) in world.client.subscriptions


def test_the_status_topic_follows_the_discovery_prefix(make_bridge, factory, settings):
    world = World(make_bridge, factory, settings, ha_discovery_prefix="ha")
    assert ("ha/status", 1) in world.client.subscriptions
    assert (STATUS_TOPIC, 1) not in world.client.subscriptions


def test_the_capability_and_info_say_the_menu_is_there(world):
    info = world.info()
    assert "menu" in info["capabilities"]
    assert info["menu"] == {"screen": "styled", "hotkey": "help_long", "reason": None}
    assert info["settings"]["menu_allowed"] is True
    assert info["settings"]["menu_hotkey"] == "help_long"


def test_with_the_permission_off_there_is_no_menu_at_all(make_bridge, factory, settings):
    world = World(make_bridge, factory, settings, menu_allowed=False)
    info = world.info()
    assert "menu" not in info["capabilities"]
    assert info["menu"] is None
    assert info["settings"]["menu_allowed"] is False
    assert world.publisher is None
    assert [topic for topic, _qos in world.client.subscriptions
            if topic in (MENU_TOPIC, STATUS_TOPIC)] == []
    assert [entry for entry in world.receiver.actions.bound if entry[0] == CONTEXT] == []
    assert world.receiver.actions.keys == []
    # A menu that arrives anyway goes nowhere, and the hotkey opens nothing.
    world.send_menu()
    assert world.long_help() == [0, 0, 0]
    assert world.session.current_dialog is not None
    assert world.screen is None
    assert world.actions() == []


def test_the_permission_cannot_be_written_over_mqtt(world, settings):
    world.client.fire_message("enigma2/" + NODE + "/cmd/config", json.dumps({
        "publish_keys": True, "screenshot": "on_zap", "screenshot_interval": 60,
        "menu_allowed": False}).encode())
    assert settings.menu_allowed.value is True
    assert "menu_allowed" in world.client.last("enigma2/" + NODE + "/last_error").json()["error"]


def test_an_invalid_menu_keeps_the_one_before_it(world, plugin_log):
    world.send_menu()
    world.send_menu(raw=b'{"v": 1, "rev": 43, "items": [{"type": "toggle", "id": "a b"}]}')
    assert world.publisher.menu["rev"] == 42
    assert plugin_log().count("ignoring an invalid menu, keeping the one before it") == 1


def test_a_newer_menu_version_shows_no_menu_and_is_logged_once(world, plugin_log):
    world.send_menu()
    world.send_menu(raw=payload(_broken(v=2)))
    world.send_menu(raw=payload(_broken(v=2, rev=50)))
    assert world.publisher.menu is None
    assert plugin_log().count("a menu of version 2") == 1


def test_an_empty_payload_takes_the_menu_away(world):
    world.send_menu()
    world.send_menu(raw=b"")
    assert world.publisher.menu is None


# ------------------------------------------------------------------ opening --


def test_a_long_help_in_live_tv_opens_the_styled_menu_and_takes_the_key(world):
    world.send_menu()
    results = world.long_help()
    screen = world.screen
    assert screen is not None
    assert results[1] == 1, "the long press that opened the menu is consumed"
    assert screen["title"].getText() == "Home Assistant"
    assert screen["marker"].getText() == "from Home Assistant"
    assert [entry[0] for entry in screen["list"].list] == ["g_salon", "r_movie", "r_door",
                                                           "r_note"]


def test_a_short_help_is_left_to_the_image(world):
    world.send_menu()
    assert world.press(KEY_HELP, MAKE, BREAK) == [0, 0]
    assert world.screen is None


def test_the_hotkey_does_nothing_over_another_screen(world):
    world.send_menu()
    world.session.open(ChoiceBox, title="the image's own", list=[("a", 1)])
    assert world.long_help() == [0, 0, 0]
    assert world.screen is None


def test_the_extensions_entry_opens_the_menu(world, monkeypatch):
    world.send_menu()
    monkeypatch.setattr(plugin_module, "_bridge", world.bridge)
    plugin_module.open_menu(world.session)
    assert world.screen is not None


def test_the_extensions_entry_says_why_when_the_plugin_is_not_running(monkeypatch):
    receiver = Receiver(modal=True)
    receiver.with_channel_list()
    monkeypatch.setattr(plugin_module, "_bridge", None)
    plugin_module.open_menu(receiver.session)
    dialog = receiver.session.current_dialog
    assert "the plugin is not running" in dialog.text


def test_the_extensions_entry_follows_the_permission(world, settings):
    plugins = plugin_component_module.plugins
    assert plugin_module.menu_entry() in plugins.pluginList
    settings.menu_allowed.value = False
    world.bridge.reload()
    assert plugin_module.menu_entry() not in plugins.pluginList
    settings.menu_allowed.value = True
    world.bridge.reload()
    assert plugins.pluginList.count(plugin_module.menu_entry()) == 1


def test_the_menu_opens_once(world):
    world.open()
    assert world.publisher.open(world.session) is False


def test_with_no_menu_yet_the_screen_says_so(world):
    world.long_help()
    assert world.screen is not None
    assert world.screen["list"].list == []
    assert world.notice() == "There is no menu from Home Assistant yet."


# ---------------------------------------------------------------- navigation --


def test_ok_on_a_group_opens_its_rows_and_exit_goes_back_then_closes(world):
    world.open()
    world.ok("g_salon")
    assert [entry[0] for entry in world.screen["list"].list] == ["r_lamp", "r_blind", "r_mode"]
    assert world.screen["title"].getText() == "Home Assistant / Salon"
    world.exit()
    assert [entry[0] for entry in world.screen["list"].list][0] == "g_salon"
    # Back on the group it left.
    assert world.screen.selected() == "g_salon"
    controller = world.screen.controller
    world.exit()
    assert world.screen is None
    assert controller.closed
    assert world.publisher._controllers == []


def test_a_button_sends_press(world):
    world.open()
    world.ok("r_movie")
    (action,) = world.actions()
    assert action["action"] == "press"
    assert action["row"] == "r_movie"
    assert action["rev"] == 42
    assert "value" not in action
    assert re.fullmatch(r"[0-9a-f]{12}", action["id"])
    assert isinstance(action["ts"], int)
    entry = world.client.last(ACTION_TOPIC)
    assert entry.qos == 1 and entry.retain is False
    assert world.notice() == "Sent to Home Assistant."
    assert not world.view()["r_movie"]["pending"]


def test_every_action_has_a_new_id(world):
    world.open()
    world.ok("r_movie")
    world.ok("r_movie")
    first, second = world.actions()
    assert first["id"] != second["id"]


def test_a_toggle_sends_toggle_and_shows_the_pending_mark_until_the_value_changes(world):
    world.open()
    world.ok("g_salon")
    world.ok("r_lamp")
    (action,) = world.actions()
    assert action["action"] == "toggle" and "value" not in action
    row = world.view()["r_lamp"]
    assert row["pending"] and row["state"] == "\u2026"
    # A publish that does not change the lamp leaves the mark.
    world.send_menu(changed(world.menu, "r_blind", state="50 %"))
    assert world.view()["r_lamp"]["pending"]
    world.send_menu(changed(world.menu, "r_lamp", value=False, state="off", color="red"))
    row = world.view()["r_lamp"]
    assert not row["pending"]
    assert row["state"] == "off" and row["colour"] == "red"


def test_a_pending_mark_ends_after_five_seconds_and_the_footer_says_so(world, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(world.publisher, "clock", lambda: clock[0])
    world.open()
    world.ok("g_salon")
    world.ok("r_lamp")
    ticker = world.publisher._ticker
    assert ticker.timer.started == (5000, True)
    clock[0] += 4.9
    ticker.timer.fire()
    assert world.view()["r_lamp"]["pending"]
    clock[0] += 0.2
    ticker.timer.fire()
    row = world.view()["r_lamp"]
    assert not row["pending"]
    assert row["state"] == "on"
    assert world.notice() == "Home Assistant changed nothing."


def test_a_selector_lists_its_options_ok_sets_and_exit_discards(world):
    world.open()
    world.ok("g_salon")
    world.ok("r_mode")
    rows = world.screen.controller.view()["rows"]
    assert [row["label"] for row in rows] == ["off", "heat", "auto"]
    assert [row["state"] for row in rows] == ["", "current", ""]
    assert world.screen.selected() == "o1", "the list starts on the current option"
    world.exit()
    assert world.actions() == []
    assert [entry[0] for entry in world.screen["list"].list] == ["r_lamp", "r_blind", "r_mode"]
    world.ok("r_mode")
    world.ok("o2")
    (action,) = world.actions()
    assert (action["action"], action["row"], action["value"]) == ("set", "r_mode", "auto")
    assert world.view()["r_mode"]["state"] == "auto \u2026"


def test_a_slider_moves_locally_and_sends_only_on_ok(world):
    world.open()
    world.ok("g_salon")
    world.select("r_blind")
    assert world.screen.key_right() == 1
    assert world.screen.key_right() == 1
    assert world.view()["r_blind"]["state"] == "< 60 % >"
    assert world.actions() == []
    world.ok("r_blind")
    (action,) = world.actions()
    assert (action["action"], action["value"]) == ("set", 60)
    assert isinstance(action["value"], int)
    assert world.view()["r_blind"]["state"] == "60 % \u2026"


def test_exit_discards_a_moved_slider_and_stays(world):
    world.open()
    world.ok("g_salon")
    world.select("r_blind")
    world.screen.key_left()
    assert world.view()["r_blind"]["state"] == "< 30 % >"
    world.exit()
    assert world.view()["r_blind"]["state"] == "40 %"
    assert world.screen["title"].getText() == "Home Assistant / Salon"
    assert world.actions() == []


def test_a_slider_stops_at_its_bounds(world):
    world.send_menu(changed(example(), "r_blind", value=10))
    world.open()
    world.ok("g_salon")
    world.select("r_blind")
    for _ in range(3):
        world.screen.key_left()
    assert world.view()["r_blind"]["state"] == "< 0 % >"


def test_left_and_right_on_any_other_row_go_on_to_the_list(world):
    world.open()
    world.select("r_movie")
    assert world.screen.key_left() == 0
    assert world.screen.key_right() == 0


def test_ok_on_an_unmoved_slider_sends_nothing_and_says_how(world):
    world.open()
    world.ok("g_salon")
    world.ok("r_blind")
    assert world.actions() == []
    assert world.notice() == "Use LEFT and RIGHT to change the value, then OK."


def test_an_input_opens_the_keyboard_and_sends_the_text_cut(world):
    world.send_menu(changed(example(), "r_note", value="milk", max_length=5))
    world.open()
    world.ok("r_note")
    keyboard = world.session.current_dialog
    assert isinstance(keyboard, VirtualKeyBoard)
    assert keyboard.text == "milk"
    assert keyboard.title == "Note"
    keyboard.close("bread and butter")
    MainLoop.advance(0)
    (action,) = world.actions()
    assert (action["action"], action["value"]) == ("set", "bread")
    assert world.screen is not None


def test_exit_on_the_keyboard_sends_nothing(world):
    world.open()
    world.ok("r_note")
    world.session.current_dialog.close(None)
    MainLoop.advance(0)
    assert world.actions() == []


def test_an_indicator_only_shows_its_state(world):
    world.open()
    world.ok("r_door")
    assert world.actions() == []
    assert world.notice() == "This row only shows a state."
    row = world.view()["r_door"]
    assert (row["state"], row["colour"]) == ("closed", "green")


def test_an_unavailable_row_is_greyed_and_cannot_be_acted_on(world):
    world.send_menu(changed(example(), "r_movie", available=False))
    world.open()
    assert world.view()["r_movie"]["dim"]
    assert not world.view()["r_door"]["dim"]
    world.ok("r_movie")
    assert world.actions() == []
    assert world.notice() == "This is unavailable in Home Assistant."


def test_the_number_keys_pick_their_row(world):
    world.open()
    world.screen.key_number(2)
    (action,) = world.actions()
    assert action["row"] == "r_movie"
    world.screen.key_number(9)
    assert len(world.actions()) == 1


def test_a_row_of_an_unknown_type_cannot_be_acted_on(world):
    menu = example()
    menu["items"].append({"type": "dial", "id": "r_dial", "label": "Dial", "state": "3",
                          "available": True})
    world.send_menu(menu)
    world.open()
    world.ok("r_dial")
    assert world.actions() == []
    assert world.view()["r_dial"]["state"] == "3"


# ------------------------------------------------------------------- greying --


def test_without_the_broker_the_menu_is_greyed_and_sends_nothing(world):
    world.open()
    world.client.fire_disconnect()
    assert world.notice() == "No connection to the MQTT broker. Showing the last menu."
    assert all(row["dim"] for row in world.view().values())
    world.ok("r_movie")
    assert world.actions() == []
    world.client.fire_connect()
    assert world.notice() == ""
    assert not world.view()["r_movie"]["dim"]


def test_home_assistant_offline_greys_the_menu_and_online_lifts_it(world):
    world.open()
    world.status("offline")
    assert world.notice() == "Home Assistant is not reachable. Showing the last menu."
    world.ok("r_movie")
    assert world.actions() == []
    world.status("online")
    world.ok("r_movie")
    assert len(world.actions()) == 1


def test_a_status_never_heard_does_not_grey(world):
    world.open()
    assert world.publisher.ha_status is None
    assert world.publisher.greyed() is None


def test_an_unknown_status_changes_nothing(world):
    world.status("offline")
    world.status("restarting")
    assert world.publisher.ha_status == "offline"


def test_greyed_the_rows_can_still_be_browsed(world):
    world.open()
    world.status("offline")
    world.ok("g_salon")
    assert [entry[0] for entry in world.screen["list"].list] == ["r_lamp", "r_blind", "r_mode"]
    world.select("r_blind")
    assert world.screen.key_right() == 1
    assert world.view()["r_blind"]["state"] == "40 %"
    world.ok("r_mode")
    assert world.screen.controller.level == menu_module.IN_GROUP


def test_a_greyed_menu_drops_the_options_it_had_open(world):
    world.open()
    world.ok("g_salon")
    world.ok("r_mode")
    world.status("offline")
    assert world.screen.controller.level == menu_module.IN_GROUP


# -------------------------------------------------------------- live updates --


def test_a_new_menu_keeps_the_selection_on_its_row(world):
    world.open()
    world.select("r_door")
    menu = example()
    menu["items"].insert(0, dict(TOGGLE, id="r_new"))
    world.send_menu(menu)
    assert world.screen.selected() == "r_door"


def test_a_row_that_went_leaves_the_selection_on_the_nearest(world):
    world.open()
    world.select("r_door")
    menu = example()
    menu["items"] = [item for item in menu["items"] if item["id"] != "r_door"]
    world.send_menu(menu)
    assert world.screen.selected() == "r_note"


def test_a_group_that_went_takes_the_screen_back_to_the_top(world):
    world.open()
    world.ok("g_salon")
    menu = example()
    menu["items"] = menu["items"][1:]
    world.send_menu(menu)
    assert world.screen.controller.level == menu_module.TOP
    assert [entry[0] for entry in world.screen["list"].list] == ["r_movie", "r_door", "r_note"]


def test_a_slider_being_moved_is_not_overwritten(world):
    world.open()
    world.ok("g_salon")
    world.select("r_blind")
    world.screen.key_right()
    world.send_menu(changed(world.menu, "r_blind", value=90, state="90 %"))
    assert world.view()["r_blind"]["state"] == "< 50 % >"
    world.ok("r_blind")
    assert world.actions()[-1]["value"] == 50


def test_an_action_says_the_rev_it_acted_on(world):
    world.open()
    world.send_menu(changed(world.menu, "r_door", state="open"))
    world.ok("r_movie")
    assert world.actions()[-1]["rev"] == 43


# ------------------------------------------------------------- never sent --


def test_nothing_is_sent_for_a_row_the_menu_does_not_hold(world):
    world.send_menu()
    publisher = world.publisher
    assert publisher.send({"id": "r_gone", "type": "button"}, "press") is False
    assert publisher.send({"id": "r_movie", "type": "toggle"}, "toggle") is False
    assert publisher.send({"id": "r_door", "type": "indicator"}, "toggle") is False
    assert publisher.send({"id": "r_movie", "type": "button"}, "set", 3) is False
    assert world.actions() == []


def test_nothing_is_sent_without_a_menu(world):
    assert world.publisher.send({"id": "r_movie", "type": "button"}, "press") is False


def test_an_action_the_broker_has_not_taken_is_dropped_when_the_connection_goes(world):
    """Never queued across a disconnect (D64): paho would send it again after the reconnect."""
    world.open()
    world.ok("r_movie")
    assert world.client.queued_for_reconnect() == [ACTION_TOPIC]
    world.client.fire_disconnect()
    assert world.client.queued_for_reconnect() == []
    world.client.fire_connect()
    assert len(world.actions()) == 1


def test_an_acknowledged_action_is_not_touched_by_a_disconnect(world, plugin_log):
    world.open()
    world.ok("r_movie")
    world.client.acknowledge()
    world.client.fire_disconnect()
    assert "dropped a menu_action" not in plugin_log()


def test_an_action_the_socket_refused_is_taken_back_at_once(world):
    """The disconnect had not reached the main thread yet: the client still looked connected."""
    world.open()
    world.client.publish_rc = 4  # MQTT_ERR_NO_CONN
    world.ok("r_movie")
    assert world.client.queued_for_reconnect() == []
    assert world.notice() == "Could not send this to Home Assistant."


def test_the_real_client_keeps_a_qos_1_publish_for_the_reconnect_and_it_can_be_withdrawn():
    """Against the vendored paho itself: what the two tests above rely on."""
    from MQTTBridge.mqttclient import import_paho, withdraw_publish

    paho = import_paho()
    client = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id="t", protocol=paho.MQTTv311)
    info = client.publish(ACTION_TOPIC, b"{}", qos=1)
    assert info.rc == paho.MQTT_ERR_NO_CONN
    assert info.mid in client._out_messages
    assert withdraw_publish(client, info) is True
    assert client._out_messages == {}
    assert client._inflight_messages == 0
    assert withdraw_publish(client, info) is False


def test_an_empty_group_opens_to_a_line_that_says_so(world):
    menu = example()
    menu["items"].append({"type": "group", "id": "g_empty", "label": "Garden", "icon": "garden",
                          "rows": []})
    world.send_menu(menu)
    world.open()
    world.ok("g_empty")
    assert world.screen["list"].list == []
    assert world.notice() == "There are no controls here."
    world.exit()
    assert world.screen.selected() == "g_empty"


def test_stopping_the_bridge_closes_the_menu_and_unbinds_the_key(world):
    world.open()
    controller = world.screen.controller
    world.bridge.stop()
    assert controller.closed
    assert [entry for entry in world.receiver.actions.bound if entry[0] == CONTEXT] == []
    assert world.receiver.actions.keys == []


# ------------------------------------------------------------------ the hotkey --


@pytest.mark.parametrize("choice, code, flag", [
    ("help_long", 138, LONG),
    ("red_long", 398, LONG),
    ("f3", 61, BREAK),
    ("f4", 62, BREAK),
])
def test_each_hotkey_choice_is_bound_to_its_key(make_bridge, factory, settings, choice, code,
                                                flag):
    world = World(make_bridge, factory, settings, menu_hotkey=choice)
    world.send_menu()
    ((_domain, device, key, bits, context, _action),) = world.receiver.actions.keys
    assert (device, key, context) == ("generic", code, CONTEXT)
    assert bits == 1 << flag
    world.press(code, MAKE, flag) if flag == LONG else world.press(code, MAKE, BREAK)
    assert world.screen is not None
    assert world.info()["menu"]["hotkey"] == choice


def test_the_hotkey_is_asked_after_every_binding_of_the_image(world):
    (entry,) = [entry for entry in world.receiver.actions.bound if entry[0] == CONTEXT]
    others = [priority for context, priority, _f in world.receiver.actions.bound
              if context != CONTEXT]
    assert all(entry[1] > priority for priority in others)


def test_a_key_the_image_takes_first_never_reaches_the_menu(world):
    taken = []

    def image(context, action):
        taken.append(action)
        return 1

    world.receiver.actions.bindKey("keymap.xml", "generic", KEY_HELP, 1 << LONG,
                                   "InfoBarActions", "something")
    world.receiver.actions.bindAction("InfoBarActions", 0, image)
    world.send_menu()
    world.long_help()
    assert taken == ["something"]
    assert world.screen is None


def test_hotkey_off_binds_nothing_and_says_why(make_bridge, factory, settings):
    world = World(make_bridge, factory, settings, menu_hotkey="off")
    assert world.receiver.actions.keys == []
    assert world.info()["menu"] == {
        "screen": "styled", "hotkey": None,
        "reason": "the hotkey is switched off in the plugin's settings"}
    assert "menu" in world.info()["capabilities"]


def test_the_hotkey_changed_over_mqtt_is_bound_at_once(world):
    world.client.fire_message("enigma2/" + NODE + "/cmd/config", json.dumps({
        "publish_keys": True, "screenshot": "on_zap", "screenshot_interval": 60,
        "menu_hotkey": "red_long"}).encode())
    ((_domain, _device, key, _bits, _context, action),) = world.receiver.actions.keys
    assert (key, action) == (KEY_RED, "red_long")
    assert world.info()["menu"]["hotkey"] == "red_long"
    assert world.info()["settings"]["menu_hotkey"] == "red_long"
    world.send_menu()
    world.long_help()
    assert world.screen is None
    world.press(KEY_RED, MAKE, LONG, BREAK)
    assert world.screen is not None


def test_a_hotkey_that_is_no_choice_is_refused_over_mqtt(world, settings):
    world.client.fire_message("enigma2/" + NODE + "/cmd/config", json.dumps({
        "publish_keys": True, "screenshot": "on_zap", "screenshot_interval": 60,
        "menu_hotkey": "blue"}).encode())
    assert settings.menu_hotkey.value == "help_long"
    error = world.client.last("enigma2/" + NODE + "/last_error").json()["error"]
    assert error.startswith("menu_hotkey must be one of")


@pytest.mark.parametrize("section", ["ButtonSetup", "hotkey"])
def test_a_key_the_image_assigns_is_reported_as_not_bound(world, monkeypatch, section):
    from Components.config import ConfigSubsection, ConfigText

    assigned = ConfigSubsection()
    assigned.displayHelp_long = ConfigText(default="Infobar/showHelp")
    monkeypatch.setattr(config.misc, section, assigned, raising=False)
    world.bridge.publish_info()
    assert world.info()["menu"] == {
        "screen": "styled", "hotkey": None,
        "reason": "the receiver's own button setup gives this key a function"}


def test_an_image_that_cannot_bind_a_key_still_has_the_menu(make_bridge, factory, settings,
                                                            monkeypatch):
    from conftest import KeyActionMap

    monkeypatch.delattr(KeyActionMap, "bindKey")
    world = World(make_bridge, factory, settings)
    assert world.info()["menu"] == {
        "screen": "styled", "hotkey": None,
        "reason": "this image does not let a plugin bind a key"}
    assert "menu" in world.info()["capabilities"]


# ---------------------------------------------------------------- the fallback --


def _no_styled_screen(monkeypatch):
    def broken(*args, **kwargs):
        raise TypeError("this image's MenuList takes no content")

    class Broken:
        def __init__(self, *args, **kwargs):
            broken()

    monkeypatch.setattr(styledlist, "screen_class", lambda: Broken)


def test_a_styled_screen_that_cannot_be_built_leaves_the_session_alone(world, monkeypatch):
    _no_styled_screen(monkeypatch)
    infobar = world.session.current_dialog
    world.send_menu()
    world.long_help()
    # The trial build failed off the dialog stack: the info bar was never left, and the
    # image's own list took over.
    assert isinstance(world.session.current_dialog, ChoiceBox)
    assert world.session.dialog_stack[-1][0] is infobar
    assert world.info()["menu"]["screen"] == "fallback"


def test_the_fallback_is_the_images_list_with_the_same_rows_ids_and_keys(world, monkeypatch):
    _no_styled_screen(monkeypatch)
    world.send_menu()
    world.long_help()
    box = world.session.current_dialog
    assert box.list == [("Salon", "g_salon"), ("Movie night", "r_movie"),
                        ("Front door", "r_door"), ("Note", "r_note")]
    assert box.keys == ["1", "2", "3", "4"]
    assert box.title == "Home Assistant"
    box.close(("Movie night", "r_movie"))
    MainLoop.advance(0)
    assert world.actions()[-1]["action"] == "press"
    # The list comes back after an action, as the styled screen stays open.
    box = world.session.current_dialog
    assert isinstance(box, ChoiceBox)
    assert box.title == "Home Assistant\nSent to Home Assistant."
    box.close(("Salon", "g_salon"))
    MainLoop.advance(0)
    box = world.session.current_dialog
    assert [entry[1] for entry in box.list] == ["r_lamp", "r_blind", "r_mode"]
    box.close(("Blind", "r_blind"))
    MainLoop.advance(0)
    box = world.session.current_dialog
    assert box.list[0] == ("0 %", "v0") and box.list[-1] == ("100 %", "v10")
    assert box.selection == 4, "the list of values starts on the current one"
    box.close(("70 %", "v7"))
    MainLoop.advance(0)
    assert world.actions()[-1]["value"] == 70
    world.session.current_dialog.close(None)
    MainLoop.advance(0)
    world.session.current_dialog.close(None)
    MainLoop.advance(0)
    assert not isinstance(world.session.current_dialog, ChoiceBox)
    assert world.publisher._controllers == []


def test_an_image_without_the_styled_screens_parts_uses_the_fallback_from_the_start(
        make_bridge, factory, settings, monkeypatch):
    monkeypatch.setattr(styledlist, "styled_refusal", lambda: "ImportError: no MultiContent")
    world = World(make_bridge, factory, settings)
    assert world.info()["menu"]["screen"] == "fallback"
    world.send_menu()
    world.long_help()
    assert isinstance(world.session.current_dialog, ChoiceBox)


def test_an_image_with_no_screen_at_all_claims_no_menu(make_bridge, factory, settings,
                                                       monkeypatch):
    monkeypatch.setattr(styledlist, "styled_refusal", lambda: "no styled")
    monkeypatch.setattr(styledlist, "fallback_refusal", lambda: "no ChoiceBox")
    world = World(make_bridge, factory, settings)
    assert "menu" not in world.info()["capabilities"]
    assert world.info()["menu"] is None


def test_the_fallback_greyed_says_so_in_its_title_and_sends_nothing(world, monkeypatch):
    _no_styled_screen(monkeypatch)
    world.send_menu()
    world.status("offline")
    world.long_help()
    box = world.session.current_dialog
    assert box.title.endswith("Home Assistant is not reachable. Showing the last menu.")
    box.close(("Movie night", "r_movie"))
    MainLoop.advance(0)
    assert world.actions() == []
