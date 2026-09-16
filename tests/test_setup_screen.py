"""The setup screen.

The screen cannot be drawn without a receiver, but the two things that go wrong
about it can both be checked here: a setting that exists in the configuration and
is missing from the screen — invisible, and therefore unreachable on a box with
no SSH — and a save that does not reach enigma2's settings file.
"""

from Components.ConfigList import ConfigListScreen as REAL_CONFIG_LIST_SCREEN

from MQTTBridge import config as settings_module
from MQTTBridge import setup as setup_screen


class FakeSession:
    def __init__(self):
        self.opened = []
        self.callbacks = []

    def open(self, what, *args, **kwargs):
        self.opened.append(what)

    def openWithCallback(self, callback, what, *args, **kwargs):
        self.opened.append(what)
        self.callbacks.append(callback)
        self.last_args = args

    def answer(self, value):
        """Give the dialog the answer the user would have given."""
        self.callbacks.pop()(value)


def build(settings, bridge=None):
    return setup_screen.MQTTBridgeSetup(FakeSession(), settings=settings, bridge=bridge)


def session_of(screen):
    return screen.session


def test_every_setting_appears_exactly_once(settings):
    screen = build(settings)
    elements = [entry[1] for entry in screen.entries]
    for name in settings_module.SETTING_NAMES:
        element = getattr(settings, name)
        assert elements.count(element) == 1, name
    assert len(elements) == len(settings_module.SETTING_NAMES)


def test_the_screen_order_is_the_configuration_order(settings):
    assert [name for name, _label in setup_screen.setting_labels()] == list(
        settings_module.SETTING_NAMES
    )


def test_every_label_is_a_non_empty_string():
    for name, label in setup_screen.setting_labels():
        assert isinstance(label, str) and label.strip(), name


def test_the_list_is_handed_to_the_config_widget(settings):
    screen = build(settings)
    assert screen["config"].list == screen.entries


def test_saving_writes_enigma2s_settings_file(settings):
    from Components.config import configfile

    screen = build(settings)
    settings.host.value = "10.0.0.5"
    screen.keySave()

    assert settings.host.saved_value == "10.0.0.5"
    assert configfile.save_calls == 1
    assert screen.closed_with == (True,)


def test_saving_asks_the_bridge_to_reconnect(settings):
    class Recorder:
        idle_reason = None
        connected = True

        def __init__(self):
            self.reloads = 0

        def reload(self):
            self.reloads += 1

    bridge = Recorder()
    build(settings, bridge=bridge).keySave()
    assert bridge.reloads == 1


def test_a_bridge_that_cannot_reload_does_not_break_the_screen(settings):
    class Broken:
        idle_reason = None
        connected = False

        def reload(self):
            raise RuntimeError("the broker moved")

    screen = build(settings, bridge=Broken())
    screen.keySave()
    assert screen.closed_with == (True,)


def test_cancelling_an_untouched_screen_closes_it_straight_away(settings):
    screen = build(settings)
    screen.keyCancel()

    assert session_of(screen).opened == []
    assert screen.closed_with == (False,)


def test_cancelling_after_an_edit_asks_first(settings):
    """Exit is next to the arrow keys, and a broker password is minutes of typing."""
    from Screens.MessageBox import MessageBox

    screen = build(settings)
    settings.host.value = "10.0.0.5"
    screen.keyCancel()

    assert session_of(screen).opened == [MessageBox]
    assert screen.closed_with is None
    assert settings.host.value == "10.0.0.5"


def test_answering_yes_restores_every_element_and_closes(settings):
    screen = build(settings)
    settings.host.value = "10.0.0.5"
    screen.keyCancel()
    session_of(screen).answer(True)

    assert settings.host.value == ""
    assert screen.closed_with == (False,)


def test_answering_no_leaves_the_screen_and_the_edits_alone(settings):
    screen = build(settings)
    settings.host.value = "10.0.0.5"
    screen.keyCancel()
    session_of(screen).answer(False)

    assert settings.host.value == "10.0.0.5"
    assert screen.closed_with is None


def test_the_status_line_names_the_state_and_the_node(settings):
    class Connected:
        idle_reason = None
        connected = True

    settings.node_id.value = "vuuno4kse_005301"
    text = setup_screen.status_text(Connected(), settings)
    assert "Connected" in text
    assert "vuuno4kse_005301" in text


def test_the_status_line_distinguishes_idle_from_disconnected(settings):
    class Idle:
        idle_reason = "no broker address is configured"
        connected = False

    class Down:
        idle_reason = None
        connected = False

    assert "Idle" in setup_screen.status_text(Idle(), settings)
    assert "Disconnected" in setup_screen.status_text(Down(), settings)


def test_the_status_line_says_so_when_the_plugin_is_switched_off(settings):
    settings.enabled.value = False
    assert "Disabled" in setup_screen.status_text(None, settings)


def test_the_screen_has_the_colour_key_labels(settings):
    screen = build(settings)
    assert screen["key_red"].text
    assert screen["key_green"].text
    assert "mqttbridgeActions" in screen.widgets


def test_the_screen_carries_its_own_skin(settings):
    """No setup.xml entry to register, so the skin travels with the screen."""
    skin = setup_screen.MQTTBridgeSetup.skin
    for widget in ("config", "status", "key_red", "key_green"):
        assert 'name="' + widget + '"' in skin


# --------------------------------------------------------------- the status line --


def test_the_status_line_is_refreshed_while_the_fields_are_edited(settings):
    """`on_change` is why the line is worth having: it answers the question
    „did that help?" without closing and reopening the screen."""
    screen = build(settings)
    settings.enabled.value = False
    screen._entry_changed()

    assert "Disabled" in screen["status"].text


def test_the_config_list_is_given_the_on_change_hook(settings, monkeypatch):
    seen = {}

    class Recording(REAL_CONFIG_LIST_SCREEN):
        def __init__(self, entries, session=None, on_change=None):
            seen["on_change"] = on_change
            REAL_CONFIG_LIST_SCREEN.__init__(self, entries, session=session)

    monkeypatch.setattr(setup_screen, "ConfigListScreen", Recording)
    build(settings)
    assert callable(seen["on_change"])


def test_an_image_whose_config_list_takes_no_on_change_still_works(settings, monkeypatch):
    """Older images' ConfigListScreen has a narrower signature; the screen is
    not worth losing over a refreshing label."""
    calls = []

    class Legacy(REAL_CONFIG_LIST_SCREEN):
        def __init__(self, entries, session=None):
            calls.append(entries)
            REAL_CONFIG_LIST_SCREEN.__init__(self, entries, session=session)

    monkeypatch.setattr(setup_screen, "ConfigListScreen", Legacy)
    screen = build(settings)

    assert len(calls) == 1
    assert screen["config"].list == screen.entries
    assert screen["status"].text
