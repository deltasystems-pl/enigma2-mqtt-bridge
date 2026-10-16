"""`menu_transparency`: the setting, the see-through background, and the television's Menu section.

The receiver menu's styled screen has three background layers: the screen itself, the list's
panel and the bar on the selected row. The first two take the setting as their alpha; the bar,
the text and the icons stay opaque (docs/TOPICS.md, "The receiver menu", "How it looks").
"""

import re

import pytest
from test_menu import World

from MQTTBridge import config as settings_module
from MQTTBridge import setup as setup_screen
from MQTTBridge import styledlist


def colours(skin, attribute):
    return re.findall(attribute + r'="(#[0-9a-f]{8})"', skin)


# ------------------------------------------------------------------- the setting --


def test_the_setting_is_a_selection_of_nine_steps_twenty_by_default(settings):
    element = settings.menu_transparency
    assert [key for key, _label in element.choices] == [0, 10, 20, 30, 40, 50, 60, 70, 80]
    assert element.default == 20
    assert [label for _key, label in element.choices][2] == "20 %"


def test_it_is_remote_published_and_in_the_menu_settings():
    assert "menu_transparency" in settings_module.REMOTE_SETTING_NAMES
    assert "menu_transparency" not in settings_module.READ_ONLY_SETTING_NAMES
    assert settings_module.MENU_SETTING_NAMES == ("menu_allowed", "menu_hotkey",
                                                  "menu_transparency")
    assert settings_module.SETTING_KINDS["menu_transparency"] == "int"


@pytest.mark.parametrize("raw, value", [("0", 0), ("80", 80), (40, 40), (" 30 ", 30)])
def test_every_step_is_valid(raw, value):
    assert settings_module.validate_setting("menu_transparency", raw) == value


@pytest.mark.parametrize("raw, sentence", [
    ("25", "menu_transparency must be a multiple of 10"),
    ("5", "menu_transparency must be a multiple of 10"),
    ("90", "menu_transparency must be between 0 and 80"),
    ("-10", "menu_transparency must be between 0 and 80"),
    ("half", "menu_transparency: expected a number"),
])
def test_a_value_between_the_steps_is_refused_with_a_sentence_never_rounded(raw, sentence):
    with pytest.raises(ValueError) as refused:
        settings_module.validate_setting("menu_transparency", raw)
    assert str(refused.value) == sentence


def test_the_step_rule_is_the_transparencys_alone():
    assert settings_module.step_refusal("screenshot_interval", 7) is None
    assert settings_module.step_refusal("menu_transparency", 70) is None
    assert settings_module.step_refusal("menu_transparency", 71) == (
        "menu_transparency must be a multiple of 10"
    )


@pytest.mark.parametrize("raw, value", [
    (30, 30), ("30", 30), (None, 20), (True, 20), (False, 20), (35, 20), ("35", 20), (90, 20), (30.0, 20),
    ("x", 20),
])
def test_the_value_read_is_always_one_of_the_steps(raw, value):
    """The element's keys are ints; whatever an image hands back, the reader gets a step."""
    assert settings_module.transparency(raw) == value


def test_info_publishes_it_as_an_int(world):
    assert world.info()["settings"]["menu_transparency"] == 20


def test_a_partial_object_with_only_the_transparency_validates_to_the_whole_subset(settings):
    values = settings_module.validate_remote_settings({"menu_transparency": 70})
    assert values["menu_transparency"] == 70
    assert set(values) == set(settings_module.REMOTE_SETTING_NAMES)
    assert values["publish_keys"] is True and values["screenshot"] == "on_zap"


def test_an_empty_object_validates_to_what_is_there(settings):
    settings.screenshot_delay.value = 11
    values = settings_module.validate_remote_settings({})
    assert values["screenshot_delay"] == 11
    assert values["menu_transparency"] == 20


# ------------------------------------------------------------------- the alpha --


@pytest.mark.parametrize("percent, alpha", [(0, 0x00), (20, 0x33), (50, 0x80), (80, 0xCC)])
def test_the_alpha_is_the_percent_of_255_rounded(percent, alpha):
    assert styledlist.alpha(percent) == alpha


@pytest.mark.parametrize("percent", [None, True, -10, 101, "20", 20.0])
def test_anything_but_a_percent_is_opaque(percent):
    assert styledlist.alpha(percent) == 0


@pytest.mark.parametrize("percent, alpha", [(0, "00"), (20, "33"), (80, "cc")])
def test_the_screen_and_the_panel_take_it_and_the_selection_bar_does_not(percent, alpha):
    shape = styledlist.geometry(1920, 1080)
    shape["transparency"] = percent
    skin = styledlist.build_skin(shape)
    screen = re.search(r'<screen [^>]*backgroundColor="(#[0-9a-f]{8})"', skin).group(1)
    panel = re.search(r'<widget name="list"[^>]* backgroundColor="(#[0-9a-f]{8})"', skin).group(1)
    assert screen == "#" + alpha + styledlist.BACKGROUND_RGB
    assert panel == "#" + alpha + styledlist.PANEL_RGB
    assert colours(skin, "backgroundColorSelected") == [styledlist.SELECTED]
    assert styledlist.SELECTED.startswith("#00")
    # The text stays solid: every foreground colour is opaque.
    assert all(colour.startswith("#00") for colour in colours(skin, "foregroundColor"))


def test_at_zero_the_skin_is_the_one_before_but_for_the_screens_slight_alpha():
    shape = styledlist.geometry(1280, 720)
    opaque = styledlist.build_skin(shape)
    assert 'backgroundColor="#00101820"' in opaque
    assert 'backgroundColor="#0018242f"' in opaque
    shape["transparency"] = 0
    assert styledlist.build_skin(shape) == opaque


def test_the_transparency_is_read_at_every_open(make_bridge, factory, settings):
    world = World(make_bridge, factory, settings)
    world.open()
    assert 'backgroundColor="#33101820"' in world.screen.skin
    world.exit()

    settings.menu_transparency.value = 80
    world.long_help()

    assert 'backgroundColor="#cc101820"' in world.screen.skin
    assert 'backgroundColor="#cc18242f"' in world.screen.skin


def test_an_open_menu_keeps_its_look(make_bridge, factory, settings):
    world = World(make_bridge, factory, settings)
    screen = world.open()
    skin = screen.skin
    settings.menu_transparency.value = 60
    world.send_live()
    assert world.screen is screen and screen.skin == skin


# --------------------------------------------------- the television's Menu section --


def test_the_menu_section_is_last_under_its_heading(settings):
    entries = setup_screen.build_entries(settings)
    heading = entries.index(("Menu",))
    assert [entry[1] for entry in entries[heading + 1:]] == [
        settings.menu_allowed, settings.menu_hotkey, settings.menu_transparency,
    ]
    assert all(not setup_screen.is_heading(entry) for entry in entries[:heading])


def test_the_screen_order_still_pins_the_configuration_order():
    names = [name for name, _label in setup_screen.setting_labels()]
    assert names[-3:] == list(settings_module.MENU_SETTING_NAMES)


class _Session:
    def __init__(self):
        self.opened = []
        self.callbacks = []

    def open(self, what, *args, **kwargs):
        self.opened.append(what)

    def openWithCallback(self, callback, what, *args, **kwargs):
        self.opened.append(what)
        self.callbacks.append(callback)


def _screen(settings):
    return setup_screen.MQTTBridgeSetup(_Session(), settings=settings, bridge=None)


def test_a_heading_is_never_saved_cancelled_or_asked_whether_it_changed(settings):
    from Components.config import configfile

    screen = _screen(settings)
    assert any(setup_screen.is_heading(entry) for entry in screen.entries)
    assert screen._anything_changed() is False
    settings.menu_transparency.value = 60
    assert screen._anything_changed() is True
    screen.keySave()
    assert settings.menu_transparency.saved_value == 60
    assert configfile.save_calls == 1


def test_discarding_skips_the_heading_and_restores_the_settings(settings):
    screen = _screen(settings)
    settings.menu_transparency.value = 60
    screen.keyCancel()
    screen.session.callbacks.pop()(True)
    assert settings.menu_transparency.value == 20
    assert screen.closed_with == (False,)


class _List:
    def __init__(self, current):
        self.current = current

    def getCurrent(self):
        return self.current


def test_menu_and_text_on_a_heading_do_nothing(settings, monkeypatch):
    calls = []
    monkeypatch.setattr(setup_screen.ConfigListScreen, "keyMenu",
                        lambda self, *a: calls.append("menu"), raising=False)
    monkeypatch.setattr(setup_screen.ConfigListScreen, "keyText",
                        lambda self, *a: calls.append("text"), raising=False)
    screen = _screen(settings)
    screen["config"] = _List(("Menu",))
    screen.keyMenu()
    screen.keyText()
    assert calls == []
    screen["config"] = _List(("Label", settings.menu_hotkey))
    screen.keyMenu()
    screen.keyText()
    assert calls == ["menu", "text"]


def test_menu_and_text_on_an_image_without_them_do_nothing(settings):
    screen = _screen(settings)
    screen["config"] = _List(("Label", settings.menu_hotkey))
    assert screen.keyMenu() is None
    assert screen.keyText() is None


@pytest.fixture
def world(make_bridge, factory, settings):
    return World(make_bridge, factory, settings)
