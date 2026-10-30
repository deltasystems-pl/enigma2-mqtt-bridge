"""The press combo: a key pressed N times, each within a gap of the one before, opens the menu.

Driven as a household drives it: a television's remote over HDMI-CEC delivers its Back key as
`KEY_PREVIOUS` makes and breaks, against `StartEnigma`'s modal session with the info bar
executing. The image's own binding of the key - OpenViX's history zap in
`InfobarChannelSelection` - is bound too, at the image's priority, to prove that it never sees
the key in live TV and the players while the combo is set, and sees it everywhere else
(docs/TOPICS.md, `info.menu`; ADR-0025).
"""

import json
import sys
from pathlib import Path

import pytest
from conftest import ChoiceBox, MainLoop
from test_menu import KEY_HELP, NODE, World
from test_save_applies_live import save_on_the_screen

from MQTTBridge import combo as combo_module
from MQTTBridge import config as settings_module
from MQTTBridge import menu as menu_module
from MQTTBridge.remote import BIND_PRIORITY

FIXTURE = Path(__file__).resolve().parent / "keymaps" / "openvix-6.6-keymap-excerpt.xml"
CONFIG_TOPIC = "enigma2/" + NODE + "/cmd/config"
ERROR_TOPIC = "enigma2/" + NODE + "/last_error"
KEY_TOPIC = "enigma2/" + NODE + "/key"
DEVICE = "dreambox remote control (native)"
MAKE, BREAK, REPEAT, LONG = 0, 1, 2, 3
PREVIOUS = combo_module.FALLBACK_CODES["KEY_PREVIOUS"]
EXIT = 174
SWITCHED_OFF = "the press combo is switched off in the plugin's settings"


class Clock:
    """A monotonic clock the test moves by hand, in milliseconds."""

    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now

    def advance(self, milliseconds):
        self.now += milliseconds / 1000.0


@pytest.fixture(autouse=True)
def the_fixture_is_the_images_keymap(monkeypatch):
    monkeypatch.setattr(combo_module, "DEFAULT_KEYMAP", str(FIXTURE))


class ComboWorld(World):
    """Live TV with the combo on `KEY_PREVIOUS`, a menu received, and the image's history zap."""

    def __init__(self, make_bridge, factory, settings, **changes):
        changes.setdefault("menu_combo_key", "previous")
        World.__init__(self, make_bridge, factory, settings, **changes)
        self.clock = Clock()
        self.zaps = []
        actions = self.receiver.actions
        actions.bindKey("keymap.xml", "generic", PREVIOUS, (1 << MAKE) | (1 << REPEAT),
                        "InfobarChannelSelection", "historyBack")
        actions.bindAction("InfobarChannelSelection", 0, self._image)
        self.send_menu()
        self.attach_clock()

    def attach_clock(self):
        if self.combo is not None:
            self.combo.clock = self.clock

    def _image(self, context, action):
        self.zaps.append(action)
        return 1

    @property
    def combo(self):
        publisher = self.publisher
        return None if publisher is None else publisher._combo

    def tap(self, code=PREVIOUS, after=0):
        """One short press, `after` milliseconds after the one before: a make and a break."""
        self.clock.advance(after)
        return self.press(code, MAKE, BREAK)

    def taps(self, count, gap, code=PREVIOUS):
        return [self.tap(code, after=gap if index else 0) for index in range(count)]

    def config(self, values):
        self.client.fire_message(CONFIG_TOPIC, json.dumps(values).encode())
        self.attach_clock()


@pytest.fixture
def world(make_bridge, factory, settings):
    return ComboWorld(make_bridge, factory, settings)


def combo_entries(actions):
    return [entry for entry in actions.keys if entry[4] == combo_module.CONTEXT]


# ------------------------------------------------------------------- the binding --


def test_every_flag_of_the_key_is_mapped_into_the_combos_own_context(world):
    entries = combo_entries(world.receiver.actions)
    assert sorted((bits, action) for _d, _dev, _k, bits, _c, action in entries) == [
        (1 << MAKE, "make"), (1 << BREAK, "break"), (1 << REPEAT, "repeat"), (1 << LONG, "long"),
    ]
    assert {(domain, device, key) for domain, device, key, *_rest in entries} == {
        (combo_module.DOMAIN, "generic", PREVIOUS)}


def test_the_combo_is_asked_after_the_key_publisher_and_before_every_image_binding(world):
    bound = world.receiver.actions.bound
    (combo,) = [entry for entry in bound if entry[0] == combo_module.CONTEXT]
    assert combo[1] == -sys.maxsize
    others = [priority for context, priority, _f in bound if context != combo_module.CONTEXT]
    assert BIND_PRIORITY in others
    assert all(combo[1] > priority for priority in others if priority == BIND_PRIORITY)
    assert all(combo[1] < priority for priority in others if priority != BIND_PRIORITY)


def test_off_by_default_binds_nothing_and_says_why(make_bridge, factory, settings):
    world = World(make_bridge, factory, settings)
    assert settings.menu_combo_key.value == "off"
    assert combo_entries(world.receiver.actions) == []
    assert [entry for entry in world.receiver.actions.bound
            if entry[0] == combo_module.CONTEXT] == []
    menu = world.info()["menu"]
    assert menu["combo"] is None
    assert menu["combo_reason"] == SWITCHED_OFF


def test_an_image_that_cannot_bind_a_key_says_so(make_bridge, factory, settings, monkeypatch):
    from conftest import KeyActionMap

    monkeypatch.delattr(KeyActionMap, "bindKey")
    world = World(make_bridge, factory, settings, menu_combo_key="previous")
    menu = world.info()["menu"]
    assert menu["combo"] is None
    assert menu["combo_reason"] == "this image does not let a plugin bind a key"
    assert "menu" in world.info()["capabilities"]


@pytest.mark.parametrize("choice, code", [
    ("previous", 412), ("exit", 174), ("playpause", 164), ("stop", 128),
])
def test_each_key_choice_is_bound_to_its_key_and_opens_the_menu(make_bridge, factory, settings,
                                                                 choice, code):
    world = ComboWorld(make_bridge, factory, settings, menu_combo_key=choice)
    assert {entry[2] for entry in combo_entries(world.receiver.actions)} == {code}
    world.taps(3, 300, code)
    assert world.screen is not None
    assert world.info()["menu"]["combo"]["key"] == choice


def test_info_says_what_is_bound(world):
    menu = world.info()["menu"]
    assert menu["combo"] == {
        "key": "previous", "presses": 3, "gap_ms": 1200,
        "replaces": [
            "InfobarChannelSelection.historyBack",
            "2ndInfobarActions.prevEvent",
            "InfobarSubserviceQuickzapActions.prevSubservice",
            "InfobarTimeshiftActions.jumpPreviousFile",
            "MoviePlayerActions.movePrev",
            "InfobarCueSheetActions.jumpPreviousMark",
        ],
    }
    assert menu["combo_reason"] is None
    # The hotkey is reported as before, beside it.
    assert (menu["hotkey"], menu["reason"]) == ("help_long", None)


# ------------------------------------------------------------------ the counting --


@pytest.mark.parametrize("gap, opens", [(1199, True), (1200, True), (1201, False)])
def test_the_gap_is_measured_make_to_make_and_includes_its_bound(world, gap, opens):
    world.taps(3, gap)
    assert (world.screen is not None) is opens
    assert world.zaps == []


def test_a_late_press_starts_a_new_count(world):
    world.tap()
    world.tap(after=1201)
    world.tap(after=500)
    assert world.screen is None
    world.tap(after=500)
    assert world.screen is not None


@pytest.mark.parametrize("presses", [2, 5])
def test_the_menu_opens_on_the_nth_press_and_not_before(make_bridge, factory, settings, presses):
    world = ComboWorld(make_bridge, factory, settings, menu_combo_presses=presses)
    world.taps(presses - 1, 400)
    assert world.screen is None
    world.tap(after=400)
    assert world.screen is not None
    assert world.zaps == []


def test_the_count_starts_again_after_the_menu_opened(world):
    world.taps(3, 300)
    world.exit()
    assert world.screen is None
    world.taps(2, 300)
    assert world.screen is None
    world.tap(after=300)
    assert world.screen is not None


def test_a_held_key_counts_once_its_repeats_are_not_presses(world):
    assert world.press(PREVIOUS, MAKE, REPEAT, REPEAT, REPEAT, REPEAT, BREAK) == [1] * 6
    world.clock.advance(300)
    world.press(PREVIOUS, MAKE, REPEAT, REPEAT, BREAK)
    assert world.screen is None
    assert world.combo.count == 2


def test_a_cec_hold_that_arrives_as_makes_counts_each_make(world):
    """A held Back over CEC arrives as short presses about 0.84 s apart: each counts."""
    world.taps(3, 840)
    assert world.screen is not None


def test_leaving_live_tv_starts_the_count_again(world):
    world.taps(2, 300)
    world.session.open(ChoiceBox, title="the image's own", list=[("a", 1)])
    # The key over another screen goes on to the image, and the count is given up.
    world.tap(after=300)
    assert world.zaps == ["historyBack"]
    assert world.combo.count == 0
    world.session.current_dialog.close(None)
    MainLoop.advance(0)
    world.taps(2, 300)
    assert world.screen is None
    world.tap(after=300)
    assert world.screen is not None


def test_another_live_tv_or_player_screen_starts_the_count_again(world):
    from conftest import MoviePlayer

    world.taps(2, 300)
    world.session.open(MoviePlayer)
    world.tap(after=300)
    assert world.screen is None
    assert world.combo.count == 1
    world.taps(2, 300)
    assert world.screen is not None


def test_the_menu_opened_another_way_starts_the_count_again(world):
    world.taps(2, 300)
    world.long_help()
    assert world.screen is not None
    assert world.combo.count == 0
    world.exit()
    world.tap(after=300)
    assert world.screen is None
    assert world.combo.count == 1


def test_a_rebind_starts_the_count_again(world):
    world.taps(2, 300)
    world.config({"menu_combo_gap_ms": 1500})
    world.tap(after=300)
    assert world.screen is None
    assert world.combo.count == 1


# ----------------------------------------------------------- what is consumed --


def test_every_event_of_the_key_is_consumed_in_live_tv(world):
    assert world.press(PREVIOUS, MAKE, REPEAT, BREAK) == [1, 1, 1]
    # After a long press the image offers the break to no named context at all (hotkey.py).
    assert world.press(PREVIOUS, MAKE, LONG, BREAK) == [1, 1, 0]
    assert world.zaps == []


def test_one_and_two_presses_do_nothing_at_all(world):
    world.tap()
    world.tap(after=300)
    world.tap(after=5000)
    assert world.screen is None
    assert world.zaps == []


def test_every_event_of_the_key_is_consumed_in_a_player(world):
    from conftest import MoviePlayer

    world.session.open(MoviePlayer)
    assert world.press(PREVIOUS, MAKE, REPEAT, BREAK) == [1, 1, 1]
    # After a long press the image offers the break to no named context at all (hotkey.py).
    assert world.press(PREVIOUS, MAKE, LONG, BREAK) == [1, 1, 0]
    assert world.zaps == []
    world.clock.advance(2000)
    world.taps(3, 300)
    assert world.screen is not None
    assert world.zaps == []


def test_nothing_of_the_key_is_consumed_over_another_screen(world):
    world.session.open(ChoiceBox, title="the image's own", list=[("a", 1)])
    world.zaps.clear()
    assert world.press(PREVIOUS, MAKE, REPEAT, LONG, BREAK)[::2] == [1, 0]
    # The make and the repeat are the image binding's (it consumes them), the long and the
    # break nobody's: none of them was the combo's.
    assert world.zaps == ["historyBack", "historyBack"]
    world.taps(3, 300)
    assert world.screen is None


def test_over_the_open_menu_the_key_is_exit_and_no_count(world):
    """The combo does not count over its own menu; PREVIOUS there is EXIT (`backkey.py`)."""
    world.taps(3, 300)
    assert world.screen is not None
    world.zaps.clear()
    assert world.press(PREVIOUS, MAKE, BREAK) == [1, 0]
    MainLoop.advance(0)
    assert world.screen is None
    assert world.zaps == []
    assert world.combo.count == 0


def test_with_the_menu_not_opening_the_presses_are_still_taken(world, monkeypatch):
    monkeypatch.setattr(world.publisher, "open", lambda session=None: False)
    world.taps(3, 300)
    assert world.screen is None
    assert world.zaps == []
    assert world.combo.count == 0


def test_with_no_dialog_executing_the_key_goes_on(world, monkeypatch):
    from conftest import InfoBar

    monkeypatch.setattr(InfoBar, "instance", None)
    monkeypatch.setattr(world.session, "current_dialog", None)
    assert world.combo._action(combo_module.CONTEXT, "make") == 0


def test_another_action_in_the_combos_context_is_not_the_combos(world):
    world.receiver.actions.bindKey("elsewhere", "generic", EXIT, 1 << MAKE,
                                   combo_module.CONTEXT, "other")
    assert world.press(EXIT, MAKE, BREAK) == [0, 0]


def test_the_combo_never_raises_into_the_dispatcher(world, monkeypatch, plugin_log):
    def broken():
        raise RuntimeError("no session")

    monkeypatch.setattr(world.combo, "_screen", broken)
    assert world.press(PREVIOUS, MAKE, BREAK) == [1, 0]
    assert world.zaps == ["historyBack"]
    world.press(PREVIOUS, MAKE, BREAK)
    assert plugin_log().count("the press combo raised; the key goes on to the image") == 1


def test_a_menu_that_raises_while_opening_still_takes_the_key(world, monkeypatch, plugin_log):
    def broken(session=None):
        raise RuntimeError("no screen")

    monkeypatch.setattr(world.publisher, "hotkey_pressed", broken)
    monkeypatch.setattr(world.combo, "_open", broken)
    assert [world.tap(after=300) for _ in range(3)][-1] == [1, 1]
    assert world.zaps == []
    assert "the press combo could not open the menu" in plugin_log()


def test_the_hotkey_still_opens_the_menu_beside_the_combo(world):
    assert world.press(KEY_HELP, MAKE, LONG, BREAK)[1] == 1
    assert world.screen is not None


def test_the_key_publisher_still_publishes_every_press_of_the_combo_key(world):
    from MQTTBridge.keys import name_for

    world.client.clear()
    world.taps(3, 300)
    published = [json.loads(entry.text) for entry in world.client.all_for(KEY_TOPIC)]
    assert published == [{"key": name_for(PREVIOUS) or "KEY_412", "press": "short"}] * 3


# ------------------------------------------------------------ binding and letting go --


@pytest.mark.parametrize("change, expected", [
    ({"menu_combo_key": "exit"}, ("exit", 3, 1200)),
    ({"menu_combo_presses": 2}, ("previous", 2, 1200)),
    ({"menu_combo_gap_ms": 3000}, ("previous", 3, 3000)),
])
def test_a_change_over_mqtt_rebinds_at_once_without_a_reconnect(world, factory, change,
                                                                expected):
    clients = len(factory.clients)
    world.config(change)
    assert len(factory.clients) == clients
    combo = world.info()["menu"]["combo"]
    assert (combo["key"], combo["presses"], combo["gap_ms"]) == expected
    assert world.info()["settings"][next(iter(change))] == change[next(iter(change))]
    code = {"previous": PREVIOUS, "exit": EXIT}[expected[0]]
    assert {entry[2] for entry in combo_entries(world.receiver.actions)} == {code}
    world.taps(expected[1], expected[2], code)
    assert world.screen is not None


@pytest.mark.parametrize("name, value, expected", [
    ("menu_combo_key", "stop", ("stop", 3, 1200)),
    ("menu_combo_presses", 4, ("previous", 4, 1200)),
    ("menu_combo_gap_ms", 600, ("previous", 3, 600)),
])
def test_a_save_on_the_screen_rebinds_without_a_reconnect(world, factory, name, value,
                                                          expected):
    clients = len(factory.clients)
    save_on_the_screen(world.bridge, **{name: value})
    assert len(factory.clients) == clients
    assert world.bridge.last_save_reconnected is False
    combo = world.info()["menu"]["combo"]
    assert (combo["key"], combo["presses"], combo["gap_ms"]) == expected


def test_switching_the_combo_off_gives_the_key_back_to_the_image(world):
    world.config({"menu_combo_key": "off"})
    assert combo_entries(world.receiver.actions) == []
    assert world.info()["menu"]["combo"] is None
    assert world.info()["menu"]["combo_reason"] == SWITCHED_OFF
    world.taps(3, 300)
    assert world.screen is None
    assert world.zaps == ["historyBack"] * 3


def test_a_config_that_changes_something_else_keeps_the_binding(world, monkeypatch):
    calls = []
    monkeypatch.setattr(combo_module, "read_keymap",
                        lambda *args: calls.append(args) or ([], []))
    world.taps(2, 300)
    world.config({"menu_transparency": 40})
    assert calls == []
    world.tap(after=300)
    assert world.screen is not None


def test_stopping_the_bridge_lets_go_of_the_key(world):
    world.bridge.stop()
    assert combo_entries(world.receiver.actions) == []
    assert [entry for entry in world.receiver.actions.bound
            if entry[0] == combo_module.CONTEXT] == []


def test_switching_the_menu_off_lets_go_of_the_key(world):
    save_on_the_screen(world.bridge, menu_allowed=False)
    assert world.bridge.publisher("menu") is None
    assert combo_entries(world.receiver.actions) == []
    world.taps(3, 300)
    assert world.zaps == ["historyBack"] * 3


def test_one_handler_object_is_bound_and_unbound(world):
    combo = world.combo
    (entry,) = [entry for entry in world.receiver.actions.bound
                if entry[0] == combo_module.CONTEXT]
    assert entry[2] is combo._handler
    combo.bind("exit", 3, 1200)
    (entry,) = [entry for entry in world.receiver.actions.bound
                if entry[0] == combo_module.CONTEXT]
    assert entry[2] is combo._handler


# ------------------------------------------------------------------ `replaces` --


def test_replaces_reads_the_images_keymap_for_the_key():
    functions, setup = combo_module.read_keymap(str(FIXTURE), "KEY_PREVIOUS")
    assert functions == [
        "InfobarChannelSelection.historyBack",
        "2ndInfobarActions.prevEvent",
        "InfobarSubserviceQuickzapActions.prevSubservice",
        "InfobarTimeshiftActions.jumpPreviousFile",
        "MoviePlayerActions.movePrev",
        "InfobarCueSheetActions.jumpPreviousMark",
    ]
    assert setup == ["previous"]
    # What the key does in a list, a text field or the EPG is not taken, and not reported.
    assert not any(function.startswith(("SetupActions", "TextEditActions", "EPGSelectActions",
                                        "ChannelSelectBaseActions", "DirectionActions"))
                   for function in functions)


@pytest.mark.parametrize("key, functions, setup", [
    ("KEY_EXIT", ["InfobarShowHideActions.hide", "InfobarSubserviceQuickzapActions.exit",
                  "MoviePlayerActions.leavePlayerOnExit"], []),
    ("KEY_PLAYPAUSE", ["InfobarSeekActions.playpauseService",
                       "InfobarSeekActionsPTS.playpauseService",
                       "InfobarTimeshiftActions.timeshiftStart",
                       "InfobarTimeshiftActivateActions.timeshiftActivateEndAndPause",
                       "InfobarTimeshiftSeekPointerActions.SeekPointerPlay",
                       "MediaPlayerActions.pause"], ["playpause"]),
    ("KEY_STOP", ["InfobarTimeshiftActions.timeshiftStop", "MoviePlayerActions.leavePlayer",
                  "MediaPlayerActions.stop", "MediaPlayerActions.shift_stop"],
     ["stop"]),
])
def test_replaces_for_the_other_keys(key, functions, setup):
    assert combo_module.read_keymap(str(FIXTURE), key) == (functions, setup)


def test_a_key_named_by_its_code_in_the_keymap_is_found(tmp_path):
    keymap = tmp_path / "keymap.xml"
    keymap.write_text('<keymap><map context="InfobarActions">'
                      '<key id="412" mapto="numbered" flags="m" />'
                      '<key id="KEY_OK" mapto="other" flags="m" /></map></keymap>')
    assert combo_module.read_keymap(str(keymap), "KEY_PREVIOUS") == (
        ["InfobarActions.numbered"], [])


@pytest.mark.parametrize("content", [None, "<keymap><map", ""])
def test_a_keymap_that_cannot_be_read_is_not_known(tmp_path, content):
    keymap = tmp_path / "keymap.xml"
    if content is not None:
        keymap.write_text(content)
    assert combo_module.read_keymap(str(keymap), "KEY_PREVIOUS") is None


def test_an_unreadable_keymap_reports_null_and_still_binds(make_bridge, factory, settings,
                                                           monkeypatch, tmp_path):
    monkeypatch.setattr(combo_module, "DEFAULT_KEYMAP", str(tmp_path / "missing.xml"))
    world = ComboWorld(make_bridge, factory, settings)
    menu = world.info()["menu"]
    assert menu["combo"]["replaces"] is None
    assert menu["combo_reason"] is None
    world.taps(3, 300)
    assert world.screen is not None


def test_a_keymap_with_nothing_on_the_key_reports_an_empty_list(make_bridge, factory, settings,
                                                                monkeypatch, tmp_path):
    keymap = tmp_path / "keymap.xml"
    keymap.write_text('<keymap><map context="InfobarActions">'
                      '<key id="KEY_OK" mapto="other" flags="m" /></map></keymap>')
    monkeypatch.setattr(combo_module, "DEFAULT_KEYMAP", str(keymap))
    world = ComboWorld(make_bridge, factory, settings)
    assert world.info()["menu"]["combo"]["replaces"] == []


def test_the_keymap_the_image_is_configured_with_is_read(make_bridge, factory, settings,
                                                         monkeypatch, tmp_path):
    from Components.config import ConfigSubsection, ConfigText, config

    keymap = tmp_path / "keymap.u80"
    keymap.write_text('<keymap><map context="InfobarActions">'
                      '<key id="KEY_PREVIOUS" mapto="configured" flags="m" /></map></keymap>')
    usage = ConfigSubsection()
    usage.keymap = ConfigText(default=str(keymap))
    monkeypatch.setattr(config, "usage", usage, raising=False)
    world = ComboWorld(make_bridge, factory, settings)
    assert world.info()["menu"]["combo"]["replaces"] == ["InfobarActions.configured"]


@pytest.mark.parametrize("section", ["ButtonSetup", "hotkey"])
def test_an_assignment_in_the_images_button_setup_is_reported(world, monkeypatch, section):
    from Components.config import ConfigSubsection, ConfigText, config

    assigned = ConfigSubsection()
    assigned.previous = ConfigText(default="Infobar/openEPG")
    monkeypatch.setattr(config.misc, section, assigned, raising=False)
    world.bridge.publish_info()
    replaces = world.info()["menu"]["combo"]["replaces"]
    assert replaces[-1] == "ButtonSetup.previous"
    assert replaces.count("ButtonSetup.previous") == 1
    # Overridden in live TV, not yielded to as the hotkey yields: the combo still opens.
    world.taps(3, 300)
    assert world.screen is not None


def test_an_empty_assignment_is_no_assignment(world, monkeypatch):
    from Components.config import ConfigSubsection, ConfigText, config

    assigned = ConfigSubsection()
    assigned.previous = ConfigText(default="  ")
    monkeypatch.setattr(config.misc, "ButtonSetup", assigned, raising=False)
    world.bridge.publish_info()
    assert "ButtonSetup.previous" not in world.info()["menu"]["combo"]["replaces"]


@pytest.mark.parametrize("name, expected", [
    ("InfobarChannelSelection", True), ("2ndInfobarActions", True),
    ("InfoBarVideoSetupActions", True), ("MoviePlayerActions", True),
    ("MediaPlayerSeekActions", True), ("SetupActions", False), ("TextEditActions", False),
    ("EPGSelectActions", False), ("ButtonSetupActions", False), ("DVDPlayerActions", False),
])
def test_the_contexts_of_live_tv_and_the_players(name, expected):
    assert combo_module.live_context(name) is expected


# ------------------------------------------------------------------- the settings --


def test_the_three_settings_are_menu_settings_writable_remotely():
    names = ("menu_combo_key", "menu_combo_presses", "menu_combo_gap_ms")
    assert settings_module.MENU_SETTING_NAMES[-3:] == names
    assert settings_module.SETTING_NAMES[-3:] == names
    for name in names:
        assert name in settings_module.REMOTE_SETTING_NAMES
        assert name not in settings_module.READ_ONLY_SETTING_NAMES
    assert settings_module.CHOICES["menu_combo_key"] == combo_module.CHOICES


def test_the_elements_and_their_defaults(settings):
    assert [key for key, _label in settings.menu_combo_key.choices] == [
        "off", "previous", "exit", "playpause", "stop"]
    assert settings.menu_combo_key.default == "off"
    assert [key for key, _label in settings.menu_combo_presses.choices] == [2, 3, 4, 5]
    assert settings.menu_combo_presses.default == 3
    gaps = settings.menu_combo_gap_ms.choices
    assert [key for key, _label in gaps] == list(range(300, 3001, 100))
    assert settings.menu_combo_gap_ms.default == 1200
    labels = dict(gaps)
    assert (labels[300], labels[1200], labels[3000]) == ("0.3 s", "1.2 s", "3.0 s")


@pytest.mark.parametrize("raw, value", [
    (None, 3), ("4", 4), (True, 3), (6, 3), (2.0, 3),
])
def test_the_presses_read_are_always_one_of_the_choices(raw, value):
    assert settings_module.selection_integer("menu_combo_presses", raw) == value


@pytest.mark.parametrize("raw, value", [
    (None, 1200), ("900", 900), (1250, 1200), (3100, 1200), (False, 1200),
])
def test_the_gap_read_is_always_one_of_the_steps(raw, value):
    assert settings_module.selection_integer("menu_combo_gap_ms", raw) == value


def test_info_publishes_the_three(world):
    published = world.info()["settings"]
    assert (published["menu_combo_key"], published["menu_combo_presses"],
            published["menu_combo_gap_ms"]) == ("previous", 3, 1200)


@pytest.mark.parametrize("change, sentence", [
    ({"menu_combo_key": "blue"}, "menu_combo_key must be one of off, previous, exit, playpause, "
                                 "stop"),
    ({"menu_combo_key": "PREVIOUS"}, "menu_combo_key must be one of"),
    ({"menu_combo_key": 1}, "menu_combo_key must be one of"),
    ({"menu_combo_presses": 1}, "menu_combo_presses must be between 2 and 5"),
    ({"menu_combo_presses": 6}, "menu_combo_presses must be between 2 and 5"),
    ({"menu_combo_presses": "3"}, "menu_combo_presses must be an integer"),
    ({"menu_combo_presses": 3.0}, "menu_combo_presses must be an integer"),
    ({"menu_combo_presses": True}, "menu_combo_presses must be an integer"),
    ({"menu_combo_gap_ms": 200}, "menu_combo_gap_ms must be between 300 and 3000"),
    ({"menu_combo_gap_ms": 3100}, "menu_combo_gap_ms must be between 300 and 3000"),
    ({"menu_combo_gap_ms": 1250}, "menu_combo_gap_ms must be a multiple of 100"),
    ({"menu_combo_gap_ms": 1.2}, "menu_combo_gap_ms must be an integer"),
    ({"menu_combo_gap_ms": "1200"}, "menu_combo_gap_ms must be an integer"),
    ({"menu_combo_gap_ms": 1200, "menu_combo_presses": 9},
     "menu_combo_presses must be between 2 and 5"),
])
def test_a_value_out_of_range_or_of_the_wrong_type_is_refused_whole(world, settings, change,
                                                                    sentence):
    world.config(change)
    assert world.client.last(ERROR_TOPIC).json()["error"].startswith(sentence)
    assert (settings.menu_combo_key.value, settings.menu_combo_presses.value,
            settings.menu_combo_gap_ms.value) == ("previous", 3, 1200)
    assert world.info()["menu"]["combo"]["gap_ms"] == 1200


def test_a_partial_object_keeps_the_other_two(world, settings):
    world.config({"menu_combo_presses": 5})
    assert (settings.menu_combo_key.value, settings.menu_combo_presses.value,
            settings.menu_combo_gap_ms.value) == ("previous", 5, 1200)
    assert settings.menu_combo_presses.saved_value == 5


@pytest.mark.parametrize("name, raw, value", [
    ("menu_combo_key", "previous", "previous"), ("menu_combo_key", " Stop ", "stop"),
    ("menu_combo_presses", "2", 2), ("menu_combo_presses", 5, 5),
    ("menu_combo_gap_ms", "300", 300), ("menu_combo_gap_ms", 3000, 3000),
])
def test_every_writer_validates_the_same_values(name, raw, value):
    assert settings_module.validate_setting(name, raw) == value


@pytest.mark.parametrize("name, raw, sentence", [
    ("menu_combo_gap_ms", "1150", "menu_combo_gap_ms must be a multiple of 100"),
    ("menu_combo_gap_ms", "299", "menu_combo_gap_ms must be between 300 and 3000"),
    ("menu_combo_presses", "1", "menu_combo_presses must be between 2 and 5"),
    ("menu_combo_key", "ok", "menu_combo_key: expected one of off, previous, exit, playpause, "
                             "stop"),
])
def test_every_writer_refuses_the_same_values(name, raw, sentence):
    with pytest.raises(ValueError) as refused:
        settings_module.validate_setting(name, raw)
    assert str(refused.value) == sentence


def test_provisioning_takes_valid_values_and_skips_the_rest(tmp_path, settings, plugin_log):
    path = tmp_path / "mqttbridge.json"
    path.write_text(json.dumps({"menu_combo_key": "exit", "menu_combo_presses": 4,
                                "menu_combo_gap_ms": 1250}))
    imported = settings_module.import_provisioning(str(path))
    assert sorted(imported) == ["menu_combo_key", "menu_combo_presses"]
    assert (settings.menu_combo_key.value, settings.menu_combo_presses.value,
            settings.menu_combo_gap_ms.value) == ("exit", 4, 1200)
    assert "skipping menu_combo_gap_ms (menu_combo_gap_ms must be a multiple of 100)" in (
        plugin_log())


def test_the_screen_check_is_the_hotkeys(world, monkeypatch):
    """One rule for both shortcuts: the combo asks `hotkey_screen` and nothing else."""
    asked = []
    monkeypatch.setattr(menu_module, "hotkey_screen", lambda dialog: asked.append(dialog))
    assert world.publisher.combo_screen() is None
    assert asked == [world.session.current_dialog]


# -------------------------------------------------------------- the OpenWebif page --


@pytest.fixture
def page_world(make_bridge, factory, settings, monkeypatch):
    from MQTTBridge import webif

    webif.mounted()
    world = ComboWorld(make_bridge, factory, settings)
    monkeypatch.setattr(webif, "_bridge", lambda: world.bridge)
    world.resource = webif.MQTTBridgeWebResource()
    return world


def test_the_subpage_offers_the_three_as_lists(page_world):
    import html

    from test_webif import menu_get

    _request, body = menu_get(page_world.resource)
    text = html.unescape(body.decode("utf-8"))
    assert "<option value='previous' selected>PREVIOUS (Back on many TV remotes)</option>" in text
    assert "<option value='3' selected>3</option>" in text
    assert "<option value='1200' selected>1.2 s</option>" in text
    assert "<option value='3000'>3.0 s</option>" in text
    assert "that key does nothing else in live TV; PREVIOUS also in the players" in text


def test_the_main_page_line_names_the_combo_when_one_is_set(page_world, settings):
    import html

    from test_webif import get

    _request, body = get(page_world.resource)
    text = html.unescape(body.decode("utf-8"))
    assert ("Background transparency: 20 %. Or PREVIOUS (Back on many TV remotes) pressed 3 "
            "times, at most 1.2 s apart.") in text
    settings.menu_combo_key.value = "off"
    _request, body = get(page_world.resource)
    assert "pressed 3 times" not in html.unescape(body.decode("utf-8"))


def test_a_subpage_save_rebinds_without_a_reconnect(page_world, factory, settings):
    from test_webif import new_session
    from test_webif_menu import menu_settings, post_menu

    clients = len(factory.clients)
    fields = menu_settings(page_world.bridge.settings, menu_combo_key="stop",
                           menu_combo_presses="2", menu_combo_gap_ms="800")
    request, body = post_menu(page_world, new_session(), fields)
    assert request.response_code == 200, body[:300]
    assert len(factory.clients) == clients
    assert (settings.menu_combo_key.saved_value, settings.menu_combo_presses.saved_value,
            settings.menu_combo_gap_ms.saved_value) == ("stop", 2, 800)
    combo = page_world.info()["menu"]["combo"]
    assert (combo["key"], combo["presses"], combo["gap_ms"]) == ("stop", 2, 800)


@pytest.mark.parametrize("name, value", [
    ("menu_combo_gap_ms", "1250"), ("menu_combo_gap_ms", "3100"), ("menu_combo_presses", "6"),
    ("menu_combo_key", "ok"),
])
def test_a_subpage_value_off_its_choices_is_refused(page_world, settings, name, value):
    from Components.config import configfile
    from test_webif import new_session
    from test_webif_menu import menu_settings, post_menu

    request, _body = post_menu(page_world, new_session(),
                               menu_settings(page_world.bridge.settings, **{name: value}))
    assert request.response_code == 400
    assert (settings.menu_combo_key.value, settings.menu_combo_presses.value,
            settings.menu_combo_gap_ms.value) == ("previous", 3, 1200)
    assert configfile.save_calls == 0


# ------------------------------------------------------------- the television --


def test_the_three_rows_close_the_menu_section_with_their_labels(settings):
    from MQTTBridge import setup as setup_screen

    labels = dict(setup_screen.setting_labels())
    assert labels["menu_combo_key"] == "Key pressed several times to open the menu"
    assert labels["menu_combo_presses"] == "Presses that open the menu"
    assert labels["menu_combo_gap_ms"] == "Longest pause between two presses"
    entries = setup_screen.build_entries(settings)
    assert [entry[1] for entry in entries[-3:]] == [
        settings.menu_combo_key, settings.menu_combo_presses, settings.menu_combo_gap_ms]


@pytest.mark.parametrize("gap, opens", [(1200.4, True), (1200.6, False)])
def test_the_gap_is_compared_to_the_millisecond(world, gap, opens):
    """A gap measured as 1200.4 ms is the 1200 ms bound; 1200.6 ms is past it."""
    world.taps(3, gap)
    assert (world.screen is not None) is opens


def test_the_handler_answers_only_for_its_own_context(world):
    assert world.combo._action("InfobarChannelSelection", "make") == 0
    assert world.combo.count == 0


# ------------------------------------------- keys that count over the info bar alone --


LIVE_ONLY = [("exit", 174, "leavePlayerOnExit"), ("stop", 128, "leavePlayer"),
             ("playpause", 164, "pause")]


def _players():
    from conftest import ModelScreen, MoviePlayer

    media_player = type("MediaPlayer", (ModelScreen,),
                        {"__module__": "Plugins.Extensions.MediaPlayer.plugin"})
    return [MoviePlayer, media_player]


@pytest.mark.parametrize("player", [0, 1], ids=["movie-player", "media-player"])
@pytest.mark.parametrize("choice, code, action", LIVE_ONLY)
def test_a_live_tv_key_goes_to_the_player_untouched(make_bridge, factory, settings, choice,
                                                    code, action, player):
    world = ComboWorld(make_bridge, factory, settings, menu_combo_key=choice)
    taken = []
    world.receiver.actions.bindKey("keymap.xml", "generic", code, 1 << MAKE, "PlayerActions",
                                   action)
    world.receiver.actions.bindAction("PlayerActions", 0,
                                      lambda context, name: taken.append(name) or 1)
    world.session.open(_players()[player])
    assert world.press(code, MAKE, REPEAT, BREAK) == [1, 0, 0]
    assert world.press(code, MAKE, LONG, BREAK) == [1, 0, 0]
    assert taken == [action, action]
    world.taps(3, 300, code)
    assert world.screen is None
    assert taken == [action] * 5
    assert world.combo.count == 0


@pytest.mark.parametrize("choice, code, _action", LIVE_ONLY)
def test_a_live_tv_key_counts_over_the_info_bar(make_bridge, factory, settings, choice, code,
                                                _action):
    world = ComboWorld(make_bridge, factory, settings, menu_combo_key=choice)
    assert world.press(code, MAKE, REPEAT, BREAK) == [1, 1, 1]
    world.clock.advance(2000)
    world.taps(3, 300, code)
    assert world.screen is not None


def test_a_count_begun_in_live_tv_ends_in_a_player(make_bridge, factory, settings):
    from conftest import MoviePlayer

    world = ComboWorld(make_bridge, factory, settings, menu_combo_key="exit")
    world.taps(2, 300, EXIT)
    assert world.combo.count == 2
    world.session.open(MoviePlayer)
    world.tap(EXIT, after=300)
    assert world.combo.count == 0


def test_previous_still_counts_in_the_players(world):
    for player in _players():
        world.session.open(player)
        world.clock.advance(2000)
        world.taps(3, 300)
        assert world.screen is not None
        world.exit()


@pytest.mark.parametrize("choice, expected", [
    ("exit", ["InfobarShowHideActions.hide", "InfobarSubserviceQuickzapActions.exit"]),
    ("stop", ["InfobarTimeshiftActions.timeshiftStop", "ButtonSetup.stop"]),
    ("playpause", ["InfobarSeekActions.playpauseService",
                   "InfobarSeekActionsPTS.playpauseService",
                   "InfobarTimeshiftActions.timeshiftStart",
                   "InfobarTimeshiftActivateActions.timeshiftActivateEndAndPause",
                   "InfobarTimeshiftSeekPointerActions.SeekPointerPlay"]),
])
def test_replaces_names_only_live_tv_for_a_live_tv_key(make_bridge, factory, settings,
                                                        monkeypatch, choice, expected):
    from Components.config import ConfigSubsection, ConfigText, config

    assigned = ConfigSubsection()
    assigned.stop = ConfigText(default="Infobar/openEPG")
    monkeypatch.setattr(config.misc, "ButtonSetup", assigned, raising=False)
    world = ComboWorld(make_bridge, factory, settings, menu_combo_key=choice)
    assert world.info()["menu"]["combo"]["replaces"] == expected


def test_the_keymap_read_for_live_tv_leaves_the_players_contexts_out():
    functions, setup = combo_module.read_keymap(str(FIXTURE), "KEY_STOP", players=False)
    assert functions == ["InfobarTimeshiftActions.timeshiftStop"]
    assert setup == ["stop"]
    assert combo_module.live_context("MoviePlayerActions", players=False) is False
    assert combo_module.live_context("MediaPlayerSeekActions", players=False) is False
    assert combo_module.live_context("InfobarSeekActions", players=False) is True


def test_only_previous_counts_in_the_players():
    assert combo_module.PLAYER_KEYS == ("previous",)


def test_live_tv_is_the_info_bar_itself(world, monkeypatch):
    from conftest import InfoBar, MoviePlayer

    assert menu_module.live_tv_screen(world.session.current_dialog) is True
    player = world.session.open(MoviePlayer)
    assert menu_module.live_tv_screen(player) is False
    assert menu_module.live_tv_screen(None) is False
    monkeypatch.setattr(InfoBar, "instance", None)
    assert menu_module.live_tv_screen(None) is False
    monkeypatch.setitem(sys.modules, "Screens.InfoBar", None)
    assert menu_module.live_tv_screen(player) is False


# ------------------------------------------------------------ the review's round --


def test_presses_sent_with_cmd_key_count_and_are_taken(make_bridge, factory, settings):
    from MQTTBridge import remote

    world = ComboWorld(make_bridge, factory, settings, menu_combo_key="exit")
    taken = []
    world.receiver.actions.bindKey("keymap.xml", "generic", EXIT, 1 << MAKE,
                                   "InfobarShowHideActions", "hide")
    world.receiver.actions.bindAction("InfobarShowHideActions", 0,
                                      lambda context, action: taken.append(action) or 1)
    for _ in range(3):
        remote.forget_rate_limit()
        world.client.fire_message("enigma2/" + NODE + "/cmd/key", b'{"key": "KEY_EXIT"}')
        world.clock.advance(300)
    assert taken == []
    assert world.screen is not None


@pytest.mark.parametrize("cause, setup", [
    ("no menu screen can be built on this image", "screen"),
    ("a menu is still open", "controllers"),
    ("neither the styled screen nor the image's own list opened", "opens"),
])
def test_a_menu_that_cannot_open_still_takes_the_key_and_says_why_once(world, monkeypatch,
                                                                       plugin_log, cause, setup):
    publisher = world.publisher
    if setup == "screen":
        monkeypatch.setattr(publisher, "screen_kind", None)
    elif setup == "controllers":
        monkeypatch.setattr(publisher, "_controllers", [object()])
    else:
        monkeypatch.setattr(publisher, "open", lambda session=None: False)
    world.taps(9, 300)
    assert world.zaps == []
    assert world.screen is None
    line = "the press combo asked for the receiver menu and it could not open (" + cause + ")"
    assert plugin_log().count(line) == 1


def test_the_refusal_is_said_again_after_the_menu_opened_once(world, monkeypatch, plugin_log):
    publisher = world.publisher
    monkeypatch.setattr(publisher, "screen_kind", None)
    world.taps(3, 300)
    monkeypatch.undo()
    world.clock.advance(5000)
    world.taps(3, 300)
    assert world.screen is not None
    world.exit()
    monkeypatch.setattr(publisher, "screen_kind", None)
    world.clock.advance(5000)
    world.taps(3, 300)
    assert plugin_log().count("it could not open (no menu screen can be built") == 2


def test_a_keymap_read_that_raises_leaves_replaces_null_and_the_key_bound(
        make_bridge, factory, settings, monkeypatch):
    def broken(*_arguments):
        raise MemoryError("no memory")

    monkeypatch.setattr(combo_module, "read_keymap", broken)
    world = ComboWorld(make_bridge, factory, settings)
    menu = world.info()["menu"]
    assert menu["combo"]["key"] == "previous"
    assert menu["combo"]["replaces"] is None
    assert menu["combo_reason"] is None
    world.taps(3, 300)
    assert world.screen is not None


def test_a_combo_let_go_of_always_says_why(world):
    combo = world.combo
    combo.unbind()
    assert combo.report() == (None, combo_module.UNBOUND)
    combo.bind("previous", 3, 1200)
    assert combo.report()[1] is None
    combo.bind("off", 3, 1200)
    combo.unbind()
    assert combo.report() == (None, SWITCHED_OFF)


def test_the_reason_after_a_stop_is_never_none(world):
    combo = world.combo
    world.bridge.stop()
    assert combo.report()[0] is None and combo.report()[1]


@pytest.mark.parametrize("ident", ["0x19c", "0x19C", "412"])
def test_a_code_in_hex_of_either_case_or_decimal_is_found(tmp_path, ident):
    keymap = tmp_path / "keymap.xml"
    keymap.write_text('<keymap><map context="InfobarActions">'
                      '<key id="' + ident + '" mapto="numbered" flags="m" /></map></keymap>')
    assert combo_module.read_keymap(str(keymap), "KEY_PREVIOUS") == (
        ["InfobarActions.numbered"], [])


def test_a_keymap_without_the_button_setup_names_no_assignment(make_bridge, factory, settings,
                                                               monkeypatch, tmp_path):
    from Components.config import ConfigSubsection, ConfigText, config

    keymap = tmp_path / "keymap.xml"
    keymap.write_text('<keymap><map context="InfobarActions">'
                      '<key id="KEY_PREVIOUS" mapto="here" flags="m" /></map></keymap>')
    monkeypatch.setattr(combo_module, "DEFAULT_KEYMAP", str(keymap))
    assigned = ConfigSubsection()
    assigned.previous = ConfigText(default="Infobar/openEPG")
    monkeypatch.setattr(config.misc, "ButtonSetup", assigned, raising=False)
    world = ComboWorld(make_bridge, factory, settings)
    assert world.info()["menu"]["combo"]["replaces"] == ["InfobarActions.here"]
