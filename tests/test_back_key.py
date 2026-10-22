"""PREVIOUS works as EXIT on the plugin's own screens, whatever the press combo is set to.

A television's remote over HDMI-CEC sends its Back key as `KEY_PREVIOUS` on many televisions,
and on OpenViX that key is no "cancel" - `DirectionActions` maps it to `shiftUp`. So on the
receiver menu and the list of choices, styled and fallback, the plugin binds it as EXIT itself
(`backkey.py`); the image's own screens keep it.
"""

import sys

import pytest
from conftest import ChoiceBox, MainLoop, VirtualKeyBoard
from test_choicelist import World as ListWorld
from test_choicelist import _no_styled_screen as _no_styled_list
from test_menu import World as MenuWorld
from test_menu import _no_styled_screen as _no_styled_menu
from test_menu_combo import ComboWorld

from MQTTBridge import backkey
from MQTTBridge import combo as combo_module
from MQTTBridge import menu as menu_module

PREVIOUS = backkey.FALLBACK_CODE
MAKE, BREAK, REPEAT, LONG = 0, 1, 2, 3


def back(world, *flags):
    return [world.receiver.actions.keyPressed("dreambox remote control (native)", PREVIOUS,
                                              flag) for flag in flags or (MAKE, BREAK)]


def mappings(world):
    return [entry for entry in world.receiver.actions.keys if entry[4] == backkey.CONTEXT]


def bindings(world):
    return [entry for entry in world.receiver.actions.bound if entry[0] == backkey.CONTEXT]


@pytest.fixture
def menu(make_bridge, factory, settings):
    world = MenuWorld(make_bridge, factory, settings)
    world.send_menu()
    return world


# ------------------------------------------------------------------ the menu --


def test_previous_closes_the_menu_at_its_top(menu):
    menu.open()
    assert back(menu) == [1, 0]
    MainLoop.advance(0)
    assert menu.screen is None
    assert menu.publisher._controllers == []


def test_previous_goes_back_one_level(menu):
    menu.open()
    menu.ok("g_salon")
    assert menu.screen.controller.level != menu_module.TOP
    back(menu)
    MainLoop.advance(0)
    assert menu.screen is not None
    assert menu.screen.controller.level == menu_module.TOP


def test_previous_is_exit_on_a_moved_slider_too(menu):
    menu.open()
    menu.ok("g_salon")
    menu.select("r_blind")
    menu.screen.key_right()
    back(menu)
    assert menu.screen is not None
    assert menu.actions() == []


def test_previous_on_the_images_keyboard_is_the_images(menu):
    menu.open()
    menu.ok("r_note")
    keyboard = menu.session.current_dialog
    assert isinstance(keyboard, VirtualKeyBoard)
    assert back(menu) == [0, 0]
    assert menu.session.current_dialog is keyboard


def test_the_fallback_menu_takes_previous_as_exit(make_bridge, factory, settings, monkeypatch):
    _no_styled_menu(monkeypatch)
    world = MenuWorld(make_bridge, factory, settings)
    world.send_menu()
    world.long_help()
    box = world.session.current_dialog
    assert isinstance(box, ChoiceBox)
    assert back(world) == [1, 0]
    MainLoop.advance(0)
    assert not isinstance(world.session.current_dialog, ChoiceBox)
    assert world.publisher._controllers == []


def test_the_fallback_menu_goes_back_one_level_on_previous(make_bridge, factory, settings,
                                                           monkeypatch):
    _no_styled_menu(monkeypatch)
    world = MenuWorld(make_bridge, factory, settings)
    world.send_menu()
    world.long_help()
    box = world.session.current_dialog
    box.close([choice for choice in box.list if choice[1] == "g_salon"][0])
    MainLoop.advance(0)
    group = world.session.current_dialog
    assert isinstance(group, ChoiceBox) and group is not box
    back(world)
    MainLoop.advance(0)
    top = world.session.current_dialog
    assert isinstance(top, ChoiceBox)
    assert "g_salon" in [choice[1] for choice in top.list]


# ---------------------------------------------------------- the list of choices --


def test_previous_dismisses_the_styled_list(make_bridge, factory, settings):
    world = ListWorld(make_bridge, factory, settings)
    world.show()
    assert back(world) == [1, 0]
    assert world.endings() == [("dismissed", None)]
    world.settle()
    assert world.screen is None


def test_previous_dismisses_the_fallback_list(make_bridge, factory, settings, monkeypatch):
    _no_styled_list(monkeypatch)
    world = ListWorld(make_bridge, factory, settings)
    world.send()
    assert isinstance(world.session.current_dialog, ChoiceBox)
    assert back(world) == [1, 0]
    world.settle()
    assert world.endings() == [("dismissed", None)]


# ------------------------------------------------------- whatever the combo says --


@pytest.mark.parametrize("choice", ["off", "previous", "exit"])
def test_previous_is_exit_on_the_menu_whatever_the_combo(make_bridge, factory, settings,
                                                         choice):
    world = ComboWorld(make_bridge, factory, settings, menu_combo_key=choice)
    world.open()
    world.zaps.clear()
    assert back(world) == [1, 0]
    MainLoop.advance(0)
    assert world.screen is None
    assert world.zaps == []


def test_the_press_that_opened_the_menu_does_not_close_it(make_bridge, factory, settings):
    """The third Back opens the menu on its make; its repeats and its break are not EXIT."""
    world = ComboWorld(make_bridge, factory, settings)
    world.taps(2, 300)
    world.clock.advance(300)
    assert world.press(PREVIOUS, MAKE) == [1]
    assert world.screen is not None
    world.press(PREVIOUS, REPEAT, REPEAT, BREAK)
    MainLoop.advance(0)
    assert world.screen is not None
    # The next press is EXIT.
    back(world)
    MainLoop.advance(0)
    assert world.screen is None


def test_a_binding_made_while_a_press_is_handed_out_does_not_hear_that_press(menu):
    """What enigma2's dispatcher does, and what the test action map does too."""
    heard = []
    actions = menu.receiver.actions
    actions.bindKey("probe", "generic", PREVIOUS, 1 << MAKE, "Probe", "make")

    def first(context, action):
        actions.bindAction("Probe", 0, lambda *answer: heard.append(answer) or 0)
        return 0

    actions.bindAction("Probe", -5, first)
    back(menu, MAKE)
    assert heard == []


# ----------------------------------------------------------------- the binding --


def test_the_mapping_lives_as_long_as_a_screen_is_open(menu):
    assert mappings(menu) == []
    menu.open()
    ((_domain, device, key, bits, _context, action),) = mappings(menu)
    assert (device, key, bits, action) == ("generic", PREVIOUS, 1 << MAKE, "back")
    assert len(bindings(menu)) == 1
    menu.exit()
    assert mappings(menu) == []
    assert bindings(menu) == []


def test_two_screens_share_one_mapping_and_one_press_closes_one(make_bridge, factory,
                                                               settings):
    world = ListWorld(make_bridge, factory, settings)
    menu_publisher = world.bridge.publisher("menu")
    world.client.fire_message("enigma2mqtt/menu/" + "vuuno4kse_005301", b"", retain=True)
    assert menu_publisher.open(world.session)
    world.show()
    assert len(mappings(world)) == 1
    assert len(bindings(world)) == 2
    back(world)
    world.settle()
    assert world.endings() == [("dismissed", None)]
    # The menu under the list is still open, and still takes PREVIOUS.
    assert len(mappings(world)) == 1
    assert len(bindings(world)) == 1
    back(world)
    world.settle()
    assert menu_publisher._controllers == []
    assert mappings(world) == []


def test_previous_is_after_the_combo_and_before_the_image(menu):
    menu.open()
    ((_context, priority, _function),) = bindings(menu)
    assert priority == combo_module.PRIORITY + 1 == -sys.maxsize + 1
    image = [entry[1] for entry in menu.receiver.actions.bound
             if entry[0] not in (backkey.CONTEXT, combo_module.CONTEXT, "")]
    assert image and all(priority < other for other in image)


def test_a_cancel_that_raises_lets_the_key_go_on(menu, monkeypatch, plugin_log):
    menu.open()
    ((_context, _priority, function),) = bindings(menu)

    def broken(*_arguments):
        raise RuntimeError("no screen")

    monkeypatch.setattr(menu.screen, "key_cancel", broken)
    owner = function.__self__
    monkeypatch.setattr(owner, "_cancel", broken)
    assert back(menu) == [0, 0]
    assert "PREVIOUS as EXIT raised; the key goes on to the image" in plugin_log()


def test_another_action_in_the_context_is_not_previous(menu):
    menu.open()
    ((_context, _priority, function),) = bindings(menu)
    assert function(backkey.CONTEXT, "other") == 0
    assert function("OkCancelActions", "back") == 0
    assert menu.screen is not None


def test_an_image_that_cannot_bind_a_key_keeps_the_menu(make_bridge, factory, settings,
                                                        monkeypatch):
    from conftest import KeyActionMap

    monkeypatch.delattr(KeyActionMap, "bindKey")
    world = MenuWorld(make_bridge, factory, settings)
    world.send_menu()
    assert world.publisher.open(world.session)
    assert world.screen is not None
    assert bindings(world) == []
