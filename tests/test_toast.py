"""The discreet toast, against a model of the image rather than a mock of it.

What this feature gets wrong if it is written from intuition is **what may be
in the screen, where it sits, and how it goes away**, and a mock would agree
with all three mistakes. So the session instantiates and deletes the dialog the
way `StartEnigma.py` does, the screen is a `dict` whose `doClose()` empties
itself, the desktop orders its windows by z with ties going to the older one,
and a list widget binds its keys in its own constructor (see the notes in
`conftest.py`). `MainLoop.advance` turns the clock.

The `cmd/message` contract these tests hold the plugin to is the table in
`docs/TOPICS.md`; the popup's own behaviour is `test_command_actions.py`.
"""

import json

import pytest
import Screens.Standby as standby_module
import skin
from Components.ActionMap import ActionMap
from Components.GUIComponent import GUIComponent
from Components.Label import Label
from Components.MenuList import MenuList
from Components.Pixmap import Pixmap
from conftest import DESKTOP, KeyActionMap, MainLoop, eLabel, ePixmap, eSize, eWindow
from Tools.Notifications import Notifications, notifications

from MQTTBridge import config as settings_module
from MQTTBridge import plugin as plugin_module
from MQTTBridge import toast

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
INFO = ROOT + "/info"
LAST_ERROR = ROOT + "/last_error"


def send(factory, payload):
    if not isinstance(payload, (bytes, str)):
        payload = json.dumps(payload)
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    factory.client.fire_message(ROOT + "/cmd/message", payload)


def error(factory):
    entry = factory.client.last(LAST_ERROR)
    return None if entry is None or entry.text == "" else entry.json()["error"]


def publisher(bridge):
    return bridge.publisher("toast")


def dialog(bridge):
    return publisher(bridge)._dialog


def window(bridge):
    return dialog(bridge).instance


def on_screen(bridge):
    found = dialog(bridge)
    return found is not None and found.instance is not None and found.instance.visible


def shown_text(bridge):
    return dialog(bridge)["message"].instance.text


def timer(bridge):
    """The toast's running `eTimer`, or None."""
    ticker = publisher(bridge)._timer
    return None if ticker.timer is None or not ticker.timer.running else ticker.timer


def capabilities(factory):
    return factory.client.last(INFO).json()["capabilities"]


@pytest.fixture
def start_bridge(make_bridge, factory, settings, receiver):
    """A bridge on the receiver, started when the test says so."""

    def start():
        settings.host.value = "10.0.0.5"
        settings.node_id.value = NODE
        settings.friendly_name.value = "Living room receiver"
        bridge = make_bridge(session=receiver.session)
        bridge.start()
        factory.client.fire_connect()
        return bridge

    return start


def offending_items(screen):
    """Everything in a screen that breaks the widget rule. See `toast.py`."""
    found = []
    for name, item in list(screen.items()) + [("renderer", r) for r in screen.renderer]:
        if isinstance(item, ActionMap):
            found.append(name)
        elif isinstance(item, GUIComponent) and type(item) not in (Label, Pixmap):
            found.append(name)
    return found


# ------------------------------------------------------- the popup is unchanged --


def test_a_payload_without_a_style_is_the_popup_it_always_was(live_bridge, factory):
    send(factory, {"text": "hello"})
    assert Notifications.popups == [{"text": "hello", "type": 1, "timeout": 10, "id": "mqttbridge"}]
    assert not on_screen(live_bridge)


def test_a_payload_that_is_not_json_is_still_a_popup(live_bridge, factory):
    send(factory, b"Dobranoc")
    assert Notifications.popups == [
        {"text": "Dobranoc", "type": 1, "timeout": 10, "id": "mqttbridge"}
    ]
    assert not on_screen(live_bridge)


@pytest.mark.parametrize("style", [None, "popup", " Popup "])
def test_a_null_or_popup_style_is_the_popup(live_bridge, factory, style):
    send(factory, {"text": "hello", "style": style})
    assert Notifications.popups == [{"text": "hello", "type": 1, "timeout": 10, "id": "mqttbridge"}]
    assert not on_screen(live_bridge)


def test_a_toast_never_touches_the_notification_queue(live_bridge, factory):
    send(factory, {"text": "Pranie gotowe", "style": "toast"})
    assert error(factory) is None
    assert Notifications.popups == []
    assert Notifications.removed == []
    assert notifications == []
    assert on_screen(live_bridge)
    assert shown_text(live_bridge) == "Pranie gotowe"


@pytest.mark.parametrize("style", ["toast", "TOAST", " Toast "])
def test_the_style_is_trimmed_and_case_insensitive(live_bridge, factory, style):
    send(factory, {"text": "hello", "style": style})
    assert on_screen(live_bridge)
    assert Notifications.popups == []


def test_a_popup_and_a_toast_do_not_remove_each_other(live_bridge, factory):
    send(factory, {"text": "toast", "style": "toast"})
    send(factory, {"text": "popup"})
    assert on_screen(live_bridge)
    assert Notifications.popups[-1]["text"] == "popup"
    assert Notifications.removed == ["mqttbridge"]


# ------------------------------------------------------------------- timeouts --


def test_a_toast_without_a_timeout_lasts_five_seconds_not_ten(live_bridge, factory):
    send(factory, {"text": "hello", "style": "toast"})
    assert timer(live_bridge).started == (5000, True)


@pytest.mark.parametrize(
    "given, milliseconds",
    [(1, 1000), (30, 30000), (31, 30000), (300, 30000), ("7", 7000), (5.9, 5000), (True, 1000)],
)
def test_a_toast_timeout_is_parsed_as_the_popups_and_capped_at_thirty(
        live_bridge, factory, given, milliseconds):
    send(factory, {"text": "hello", "style": "toast", "timeout": given})
    assert error(factory) is None
    assert timer(live_bridge).started == (milliseconds, True)


def test_a_capped_timeout_is_noted_in_the_log(live_bridge, factory, plugin_log):
    send(factory, {"text": "hello", "style": "toast", "timeout": 31})
    assert "toast timeout 31 clamped to 30 seconds" in plugin_log()


@pytest.mark.parametrize("given", [0, -5, 0.5, "0", -1])
def test_a_toast_that_would_never_hide_is_refused(live_bridge, factory, given):
    send(factory, {"text": "hello", "style": "toast", "timeout": given})
    assert error(factory) == "a toast hides itself; timeout must be 1–30 seconds"
    assert not on_screen(live_bridge)


@pytest.mark.parametrize("given", [None, "soon", [5]])
def test_a_toast_timeout_that_is_not_a_number_is_refused_as_for_a_popup(
        live_bridge, factory, given):
    send(factory, {"text": "hello", "style": "toast", "timeout": given})
    assert "is not a number of seconds" in error(factory)
    assert not on_screen(live_bridge)


# ------------------------------------------------------------- style and type --


def test_an_unknown_style_is_refused(live_bridge, factory):
    send(factory, {"text": "hello", "style": "banner"})
    assert error(factory) == "unknown message style 'banner'; expected popup or toast"
    assert Notifications.popups == []
    assert not on_screen(live_bridge)


@pytest.mark.parametrize("style", ["", False, 0])
def test_an_empty_or_false_style_is_refused_not_taken_as_absent(live_bridge, factory, style):
    """Only an absent or `null` style is the popup; a falsy value is still a value.

    The echo of a falsy value is empty: `_echo` reads it as „nothing given".
    """
    send(factory, {"text": "hello", "style": style})
    assert error(factory) == "unknown message style ''; expected popup or toast"
    assert Notifications.popups == []
    assert not on_screen(live_bridge)


@pytest.mark.parametrize("payload, sentence", [
    ({"timeout": 0}, "a toast hides itself; timeout must be 1–30 seconds"),
    ({"type": "shouting"}, "unknown message type 'shouting'; expected one of info, warning, error"),
    ({"text": "  "}, "message text is empty"),
])
def test_a_toast_is_validated_before_the_box_is_asked_whether_it_can(
        start_bridge, factory, settings, payload, sentence):
    """An invalid toast gets its own reason, even from a box that has no toast at all."""
    settings.osd_toast.value = False
    start_bridge()
    message = {"text": "hello", "style": "toast"}
    message.update(payload)
    send(factory, message)
    assert error(factory) == sentence


@pytest.mark.parametrize("style", ["popup", "toast"])
def test_an_unknown_type_is_refused_whatever_the_style(live_bridge, factory, style):
    send(factory, {"text": "hello", "style": style, "type": "shouting"})
    assert error(factory) == "unknown message type 'shouting'; expected one of info, warning, error"
    assert Notifications.popups == []
    assert not on_screen(live_bridge)


def _appearance(bridge):
    screen = dialog(bridge)
    return (
        dict(screen.instance.attributes),
        screen["header"].instance.text,
        dict(screen["header"].instance.attributes),
        dict(screen["message"].instance.attributes),
        screen["message"].instance.text,
        screen.instance.size(),
    )


def test_a_valid_type_does_not_change_the_toast(live_bridge, factory):
    send(factory, {"text": "hello", "style": "toast", "type": "info"})
    plain = _appearance(live_bridge)
    for kind in ("warning", "error"):
        send(factory, {"text": "hello", "style": "toast", "type": kind})
        assert error(factory) is None
        assert _appearance(live_bridge) == plain


# ----------------------------------------------------------------------- text --


def test_the_text_is_truncated_at_two_hundred(live_bridge, factory, plugin_log):
    send(factory, {"text": "x" * 450, "style": "toast"})
    assert error(factory) is None
    assert shown_text(live_bridge) == "x" * 200
    assert "toast truncated from 450 to 200 characters" in plugin_log()


def test_escapes_are_removed_before_the_cap_so_none_is_cut_in_half(live_bridge, factory):
    # The escape spans characters 196 to 205: cut first, and „\\cFFF" would be left.
    send(factory, {"text": "x" * 195 + "\\cFFFF0000" + "Alarm", "style": "toast"})
    assert shown_text(live_bridge) == "x" * 195 + "Alarm"
    assert "\\c" not in shown_text(live_bridge)


def test_a_colour_escape_is_removed(live_bridge, factory):
    send(factory, {"text": "\\cFFFF0000Alarm", "style": "toast"})
    assert shown_text(live_bridge) == "Alarm"


def test_an_escape_a_removal_would_create_is_removed_too(live_bridge, factory):
    # Taking out the inner escape joins the leading backslash to `cBBBBBBBB`.
    send(factory, {"text": "\\" + "\\cAAAAAAAA" + "cBBBBBBBBok", "style": "toast"})
    assert shown_text(live_bridge) == "ok"


def test_an_escape_counts_a_newline_as_one_of_its_eight_characters(live_bridge, factory):
    # The renderer takes the next eight characters whatever they are.
    send(factory, {"text": "\\cFF\nF0000Alarm", "style": "toast"})
    assert shown_text(live_bridge) == "Alarm"


def test_a_backslash_c_without_eight_characters_is_left_alone(live_bridge, factory):
    send(factory, {"text": "C:\\config", "style": "toast"})
    assert shown_text(live_bridge) == "C:\\config"


def test_a_real_newline_is_kept(live_bridge, factory):
    send(factory, {"text": "Pralka\nskończyła", "style": "toast"})
    assert shown_text(live_bridge) == "Pralka\nskończyła"


@pytest.mark.parametrize("text", ["", "   ", None, "\\c00000000", "\\c00000000  "])
def test_a_toast_with_nothing_to_show_is_refused(live_bridge, factory, text):
    send(factory, {"text": text, "style": "toast"})
    assert error(factory) == "message text is empty"
    assert not on_screen(live_bridge)


# -------------------------------------------------- replacement and expiry --


def test_the_newest_toast_replaces_the_current_one_and_restarts_the_timer(live_bridge, factory):
    send(factory, {"text": "A", "style": "toast", "timeout": 10})
    MainLoop.advance(4000)
    send(factory, {"text": "B", "style": "toast"})
    assert shown_text(live_bridge) == "B"
    assert len(DESKTOP.front_to_back()) == 1
    # Five seconds from B, not ten from A.
    MainLoop.advance(4999)
    assert on_screen(live_bridge)
    MainLoop.advance(1)
    assert not on_screen(live_bridge)


def test_a_shorter_toast_is_not_kept_up_by_the_longer_one_before_it(live_bridge, factory):
    send(factory, {"text": "A", "style": "toast", "timeout": 30})
    send(factory, {"text": "B", "style": "toast", "timeout": 2})
    MainLoop.advance(2000)
    assert not on_screen(live_bridge)


def test_after_its_timer_the_toast_is_hidden_and_can_be_shown_again(live_bridge, factory):
    send(factory, {"text": "one", "style": "toast", "timeout": 3})
    MainLoop.advance(3000)
    assert not on_screen(live_bridge)
    send(factory, {"text": "two", "style": "toast", "timeout": 3})
    assert on_screen(live_bridge)
    assert shown_text(live_bridge) == "two"


def test_the_toast_appears_without_an_animation(live_bridge):
    assert window(live_bridge).animation_mode == 0


# --------------------------------------------------------------------- header --


def test_the_header_is_the_translated_plugin_name_whatever_the_payload(
        start_bridge, factory, monkeypatch):
    monkeypatch.setattr(toast, "_", lambda text: "«" + text + "»")
    bridge = start_bridge()
    send(factory, {"text": "Aktualizacja systemu", "style": "toast", "type": "error",
                   "title": "System", "header": "Receiver"})
    assert dialog(bridge)["header"].instance.text == "«MQTT Bridge»"
    assert shown_text(bridge) == "Aktualizacja systemu"


# ---------------------------------------------------------------- widget rule --


def test_the_toast_holds_only_labels_and_pixmaps(live_bridge):
    """🔴 The property that makes „cannot steal a key press" true. See `toast.py`."""
    screen = dialog(live_bridge)
    assert offending_items(screen) == []
    assert all(
        type(widget) in (eWindow, eLabel, ePixmap) for widget in screen.instance.widgets()
    )
    assert KeyActionMap.getInstance().native == []


def test_the_widget_rule_check_catches_a_list(receiver):
    """The check above, shown able to fail: a toast with a `MenuList` in it."""
    base = toast.screen_class()

    class WithAList(base):
        def __init__(self, session, shape):
            base.__init__(self, session, shape)
            self["list"] = MenuList([])

    shape = toast.geometry(1920, 1080)
    screen = receiver.session.instantiateDialog(WithAList, shape)
    assert offending_items(screen) == ["list"]
    assert KeyActionMap.getInstance().native != []


def test_the_toast_is_above_the_channel_list_whichever_was_made_first(live_bridge, factory):
    """z 10 against the channel list's 0 — and after a skin reload, which makes it newer."""
    channel_list = eWindow(DESKTOP, 0)
    channel_list.show()
    send(factory, {"text": "one", "style": "toast", "timeout": 30})
    assert DESKTOP.front_to_back()[0] is window(live_bridge)

    skin.InitSkins(booting=False)
    send(factory, {"text": "two", "style": "toast", "timeout": 30})
    assert DESKTOP.front_to_back()[0] is window(live_bridge)
    assert window(live_bridge).z == 10


# ---------------------------------------------------------------- lifecycle --


def test_the_screen_is_only_ever_instantiated_and_deleted(start_bridge, receiver, factory,
                                                          monkeypatch):
    closed = []
    monkeypatch.setattr(toast.screen_class(), "close", lambda self, *a: closed.append(a))
    bridge = start_bridge()
    send(factory, {"text": "hello", "style": "toast"})
    screen = dialog(bridge)
    bridge.stop()

    assert receiver.session.opened == []
    assert [type(item).__name__ for item in receiver.session.instantiated] == ["MQTTBridgeToast"]
    assert receiver.session.deleted == [screen]
    assert closed == []


def test_stop_stops_the_timer_before_it_deletes_the_screen(live_bridge, receiver, factory,
                                                           monkeypatch):
    send(factory, {"text": "hello", "style": "toast", "timeout": 30})
    owner = publisher(live_bridge)
    screen = dialog(live_bridge)
    events = []
    real_stop = owner._timer.stop
    real_delete = receiver.session.deleteDialog

    def stop():
        events.append(("timer", owner._timer.timer is not None))
        return real_stop()

    def delete(item):
        events.append(("delete", owner._timer.timer is None))
        return real_delete(item)

    monkeypatch.setattr(owner._timer, "stop", stop)
    monkeypatch.setattr(receiver.session, "deleteDialog", delete)
    live_bridge.stop()

    assert events[0] == ("timer", True)
    assert ("delete", True) in events
    assert events.index(("delete", True)) > 0
    assert owner._dialog is None
    # `doClose()` ran: the session is gone and every other attribute is `None`.
    assert "session" not in vars(screen)
    assert screen.instance is None and screen.skin is None and len(screen) == 0
    assert DESKTOP.front_to_back() == []


def test_a_toast_on_screen_is_gone_when_the_plugin_is_shut_down(live_bridge, factory,
                                                                monkeypatch):
    """`WHERE_AUTOSTART` reason 1 — before enigma2 paints its last frame."""
    monkeypatch.setattr(plugin_module, "_bridge", live_bridge)
    send(factory, {"text": "hello", "style": "toast", "timeout": 30})
    assert DESKTOP.front_to_back() != []
    plugin_module.autostart(1)
    assert DESKTOP.front_to_back() == []
    assert live_bridge.publisher("toast") is None


def test_a_settings_save_replaces_the_toast_rather_than_adding_one(live_bridge, receiver,
                                                                   factory):
    send(factory, {"text": "hello", "style": "toast", "timeout": 30})
    first = dialog(live_bridge)
    live_bridge.reload()
    factory.client.fire_connect()

    assert receiver.session.deleted == [first]
    assert len(receiver.session.instantiated) == 2
    assert DESKTOP.front_to_back() == []
    send(factory, {"text": "again", "style": "toast"})
    assert len(DESKTOP.front_to_back()) == 1


# ------------------------------------------------------------------- standby --


def test_standby_hides_the_toast_and_stops_its_timer(live_bridge, receiver, factory):
    send(factory, {"text": "hello", "style": "toast", "timeout": 30})
    receiver.enter_standby()
    assert not on_screen(live_bridge)
    assert timer(live_bridge) is None


def test_stop_detaches_from_the_standby_counter(live_bridge):
    from Components.config import config

    owner = publisher(live_bridge)
    assert owner._entered_standby in config.misc.standbyCounter.notifiers
    live_bridge.stop()
    assert owner._entered_standby not in config.misc.standbyCounter.notifiers


def test_a_toast_in_standby_is_refused(live_bridge, receiver, factory):
    receiver.enter_standby()
    send(factory, {"text": "hello", "style": "toast"})
    assert error(factory) == "the receiver is in standby"
    assert not on_screen(live_bridge)


def test_a_toast_on_the_way_out_of_the_main_loop_is_refused(live_bridge, factory):
    standby_module.inTryQuitMainloop = True
    send(factory, {"text": "hello", "style": "toast"})
    assert error(factory) == "the receiver is in standby"
    assert not on_screen(live_bridge)


def test_a_toast_after_standby_is_shown_again(live_bridge, receiver, factory):
    receiver.enter_standby()
    receiver.leave_standby()
    send(factory, {"text": "hello", "style": "toast"})
    assert error(factory) is None
    assert on_screen(live_bridge)


def test_a_popup_in_standby_is_not_affected(live_bridge, receiver, factory):
    receiver.enter_standby()
    send(factory, {"text": "hello"})
    assert error(factory) is None
    assert Notifications.popups[-1]["text"] == "hello"


# --------------------------------------------------------------- skin reload --


def test_a_skin_reload_rebuilds_the_toast_for_the_new_desktop(live_bridge, receiver, factory):
    first = dialog(live_bridge)
    DESKTOP.resize(eSize(1280, 720))
    skin.InitSkins(booting=False)

    assert receiver.session.deleted == [first]
    assert dialog(live_bridge) is not first
    assert window(live_bridge).position().x() == 1280 - 420 - 12
    assert window(live_bridge).size().width() == 420
    send(factory, {"text": "hello", "style": "toast"})
    assert on_screen(live_bridge)


def test_the_skin_callback_is_registered_while_running_and_not_after(live_bridge):
    owner = publisher(live_bridge)
    assert owner._skin_reloaded in skin.onLoadCallbacks
    live_bridge.stop()
    assert owner._skin_reloaded not in skin.onLoadCallbacks


def test_a_failed_rebuild_takes_the_capability_back_and_says_so(live_bridge, receiver, factory,
                                                                monkeypatch):
    assert "toast" in capabilities(factory)

    def broken(*args, **kwargs):
        raise RuntimeError("no skin")

    monkeypatch.setattr(receiver.session, "instantiateDialog", broken)
    skin.InitSkins(booting=False)

    assert "toast" not in capabilities(factory)
    assert "toast" not in live_bridge.capabilities()
    send(factory, {"text": "hello", "style": "toast"})
    assert error(factory) == "the discreet toast could not be created on this receiver"

    monkeypatch.undo()
    skin.InitSkins(booting=False)
    assert "toast" in capabilities(factory)


def test_a_skin_may_restyle_the_toast(start_bridge, factory):
    skin.domScreens["MQTTBridgeToast"] = (
        '<screen position="40,600" size="800,200" zPosition="10">'
        '<widget name="header" position="10,10" size="780,40" font="Regular;30" />'
        '<widget name="message" position="10,60" size="780,130" font="Regular;30" />'
        "</screen>"
    )
    bridge = start_bridge()
    send(factory, {"text": "hello", "style": "toast"})
    assert window(bridge).position().x() == 40
    assert window(bridge).size().width() == 800
    assert dialog(bridge)["message"].instance.size().width() == 780


# ------------------------------------------------------------------ geometry --


@pytest.mark.parametrize("width, height", [(1280, 720), (1920, 1080), (3840, 2160)])
def test_the_toast_sits_top_right_with_equal_margins(start_bridge, factory, width, height):
    DESKTOP.resize(eSize(width, height))
    bridge = start_bridge()
    send(factory, {"text": "hello", "style": "toast"})
    factor = height / 720
    box = window(bridge)
    right = width - box.position().x() - box.size().width()
    assert box.position().y() == right == int(12 * factor)
    assert box.size().width() == int(420 * factor)
    assert box.size().width() <= width


def test_the_toast_is_never_wider_than_the_desktop(start_bridge, factory):
    DESKTOP.resize(eSize(400, 1080))
    bridge = start_bridge()
    send(factory, {"text": "hello", "style": "toast"})
    box = window(bridge)
    assert 0 <= box.position().x()
    assert box.position().x() + box.size().width() <= 400


def test_the_height_follows_the_text_and_stops_at_the_cap(live_bridge, factory):
    send(factory, {"text": "short", "style": "toast"})
    short = window(live_bridge).size().height()
    send(factory, {"text": "long " * 40, "style": "toast"})
    long = window(live_bridge).size().height()
    send(factory, {"text": "line\n" * 40, "style": "toast"})
    capped = window(live_bridge).size().height()

    assert short < long < capped
    assert capped == int(220 * 1080 / 720)
    # Back down again for a short one: the box follows the newest text.
    send(factory, {"text": "short", "style": "toast"})
    assert window(live_bridge).size().height() == short


def test_the_skin_is_plain_integers():
    """No coordinate expression the image might not have: `f`, `e`, `c`, `center`, `%`."""
    text = toast.build_skin(toast.geometry(1920, 1080))
    import re

    for value in re.findall(r'(?:position|size)="([^"]*)"', text):
        assert re.fullmatch(r"\d+,\d+", value), value
    assert 'zPosition="10"' in text


# ---------------------------------------------------------------- capability --


def test_the_capability_is_claimed_once_the_screen_exists(live_bridge, factory):
    assert "toast" in capabilities(factory)
    assert dialog(live_bridge) is not None


@pytest.mark.parametrize("failure", ["raises", "returns_none", "missing"])
def test_no_capability_when_the_screen_cannot_be_made(start_bridge, receiver, factory,
                                                      monkeypatch, plugin_log, failure):
    def raises(*args, **kwargs):
        raise RuntimeError("no screen for you")

    replacement = {
        "raises": raises,
        "returns_none": lambda *args, **kwargs: None,
        "missing": None,
    }[failure]
    monkeypatch.setattr(receiver.session, "instantiateDialog", replacement)
    bridge = start_bridge()

    assert "toast" not in capabilities(factory)
    assert "message" in capabilities(factory)
    send(factory, {"text": "hello", "style": "toast"})
    assert error(factory) == "the discreet toast could not be created on this receiver"
    send(factory, {"text": "hello"})
    assert Notifications.popups[-1]["text"] == "hello"
    assert bridge.publisher("toast") is None


def test_the_setting_off_means_nothing_is_created(start_bridge, receiver, factory, settings):
    from Components.config import config

    settings.osd_toast.value = False
    bridge = start_bridge()

    assert receiver.session.instantiated == []
    assert "toast" not in capabilities(factory)
    assert bridge.publisher("toast") is None
    assert skin.onLoadCallbacks == []
    assert not any(
        getattr(notifier, "__self__", None).__class__.__name__ == "ToastPublisher"
        for notifier in config.misc.standbyCounter.notifiers
    )
    send(factory, {"text": "hello", "style": "toast"})
    assert error(factory) == "the discreet toast is switched off on this receiver"


def test_a_bridge_without_a_session_refuses_a_toast(connected_bridge, factory):
    send(factory, {"text": "hello", "style": "toast"})
    assert error(factory) == "the discreet toast could not be created on this receiver"


# ------------------------------------------------------------------- setting --


def test_osd_toast_is_on_by_default_and_box_only(connected_bridge, factory):
    assert settings_module.settings.osd_toast.default is True
    assert "osd_toast" not in settings_module.REMOTE_SETTING_NAMES
    assert "osd_toast" not in settings_module.READ_ONLY_SETTING_NAMES
    assert "osd_toast" not in factory.client.last(INFO).json()["settings"]


def test_osd_toast_cannot_be_written_over_mqtt(connected_bridge, factory, settings):
    factory.client.fire_message(ROOT + "/cmd/config", json.dumps({
        "publish_keys": True, "screenshot": "on_zap", "screenshot_interval": 60,
        "osd_toast": False,
    }).encode())
    assert error(factory) is not None
    assert settings.osd_toast.value is True


def test_osd_toast_can_be_provisioned(tmp_path, settings):
    path = tmp_path / "mqttbridge.json"
    path.write_text('{"osd_toast": "off"}', encoding="utf-8")
    assert settings_module.import_provisioning(str(path), settings) == ["osd_toast"]
    assert settings.osd_toast.value is False


# ---------------------------------------------------- nothing raises into the GUI --


def test_a_toast_that_cannot_be_drawn_is_reported_not_raised(live_bridge, factory, monkeypatch):
    def broken(text):
        raise RuntimeError("font missing")

    monkeypatch.setattr(dialog(live_bridge)["message"], "setText", broken)
    send(factory, {"text": "hello", "style": "toast"})
    assert error(factory) == "RuntimeError: font missing"


def test_showing_answers_with_a_sentence_rather_than_raising(live_bridge, monkeypatch):
    """Wrapped in the toast itself, not only by the command dispatcher around it."""
    def broken(text):
        raise RuntimeError("font missing")

    monkeypatch.setattr(dialog(live_bridge)["message"], "setText", broken)
    assert publisher(live_bridge).show("hello", 5) == "RuntimeError: font missing"


def test_a_screen_the_image_did_not_return_is_one_log_line(start_bridge, receiver, monkeypatch,
                                                           plugin_log):
    monkeypatch.setattr(receiver.session, "instantiateDialog", lambda *a, **k: None)
    start_bridge()
    text = plugin_log()
    assert "the image returned no toast screen; messages stay popups" in text
    assert "Traceback" not in text


def test_a_hook_that_raises_stays_in_the_log(live_bridge, receiver, factory, monkeypatch,
                                             plugin_log):
    send(factory, {"text": "hello", "style": "toast", "timeout": 30})

    def broken():
        raise RuntimeError("window gone")

    monkeypatch.setattr(dialog(live_bridge), "hide", broken)
    receiver.enter_standby()
    MainLoop.advance(30000)
    assert "hiding the discreet toast for standby raised" in plugin_log()

    monkeypatch.setattr(publisher(live_bridge), "_delete", broken)
    skin.InitSkins(booting=False)
    assert "rebuilding the discreet toast after a skin reload raised" in plugin_log()


def test_a_delete_that_raises_still_lets_go(live_bridge, receiver, monkeypatch, plugin_log):
    def broken(item):
        raise RuntimeError("already gone")

    owner = publisher(live_bridge)
    monkeypatch.setattr(receiver.session, "deleteDialog", broken)
    live_bridge.stop()
    assert owner._dialog is None
    assert "the discreet toast could not be deleted" in plugin_log()
