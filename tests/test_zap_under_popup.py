"""A zap made while an information popup is over the info bar is still recorded.

The receiver puts a popup on the screen with `AddPopup`: the info bar opens it
with `session.open`, so the popup becomes the executing dialog and the info bar
waits under it, on top of `dialog_stack`, as a `(screen, shown)` tuple. Before this, a
zap from Home Assistant in those seconds - under the plugin's own `cmd/message`
popup, or under the image's "Zapped to timer service" one - was played directly
and left out of the zap history, as if somebody were working in a menu.

Every popup here reaches the screen the way the receiver puts it there:
`Receiver(modal=True)` runs `StartEnigma`'s modal session with the info bar on
it (`ModalInfoBar` in `conftest.py`), and the popup comes through the
notification queue. Every test runs twice: with the info bar as the session's
first dialog, and with a screen a session-start plugin opened under it - the
Vu+ HbbTV plugin's `VBMain` on the receiver this was accepted on, where a rule
that wanted the info bar to be the only entry refused every popup. No test sets
`current_dialog` or `dialog_stack` to build the popup state; the few that
change something afterwards say what and why.

The rule (`service.info_popup_over_infobar`) is narrow on purpose, and each
refusal below is a case it must not take for a popup: a question, a box with
answers, a type the image does not know, a subclass, a popup over anything but
the info bar, and a popup that is already closing.
"""

import json

import pytest
from Components.config import ConfigSelection, ConfigSubsection, ConfigYesNo, config
from conftest import (
    BOUQUET_ROOT,
    FIRST_BOUQUET,
    POLSAT,
    SECOND_BOUQUET,
    TVN,
    TVP1,
    InfoBar,
    MainLoop,
    MoviePlayer,
    NotifiableInfoBar,
    Receiver,
    eServiceReference,
)
from Screens.MessageBox import MessageBox
from Tools.Notifications import AddNotification, AddNotificationWithID, AddPopup

from MQTTBridge import osd, zaphistory
from MQTTBridge import service as service_module

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
LAST_ERROR = ROOT + "/last_error"
BOUQUET = ROOT + "/bouquet"


@pytest.fixture(params=[False, True], ids=["info bar first", "session-start screen under it"])
def receiver(request):
    """This module's receiver runs the modal session, as the image does."""
    return Receiver(modal=True, session_start_screen=request.param)


@pytest.fixture
def usage(monkeypatch):
    """`config.usage` as the image builds it, for the zap history's commands."""
    section = ConfigSubsection()
    section.panicbutton = ConfigYesNo(default=True)
    section.pip_zero_button = ConfigSelection(
        choices=["standard", "swap", "swapstop", "stop"], default="standard"
    )
    section.multibouquet = ConfigYesNo(default=True)
    monkeypatch.setattr(config, "usage", section, raising=False)
    return section


def entry(sref, bouquet=FIRST_BOUQUET):
    return [eServiceReference(BOUQUET_ROOT), eServiceReference(bouquet), eServiceReference(sref)]


@pytest.fixture
def box(make_bridge, factory, settings, receiver, usage):
    """A receiver on POLSAT whose history holds TVP1, TVN and POLSAT, nothing open."""
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    channel_list = receiver.with_channel_list([TVP1, TVN])
    channel_list.history = [entry(TVP1), entry(TVN), entry(POLSAT, SECOND_BOUQUET)]
    channel_list.history_pos = 2
    receiver.nav.sref = POLSAT
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    box = type("Box", (), {})()
    box.bridge = bridge
    box.list = channel_list
    box.receiver = receiver
    box.session = receiver.session
    box.factory = factory
    # What the session-start plugins left under the info bar, as the session
    # stacked it when the info bar opened over it: shown then.
    start = receiver.session_start_screen
    box.below = [] if start is None else [(start, True)]
    return box


def under_popup(box, info_bar_shown=False):
    """The dialog stack under a popup opened over the info bar."""
    return box.below + [(InfoBar.instance, info_bar_shown)]


def send(box, name, body):
    box.factory.client.fire_message(ROOT + "/cmd/" + name, json.dumps(body).encode())


def last_error(box):
    message = box.factory.client.last(LAST_ERROR)
    return None if message is None or message.text == "" else message.json()


def front(box):
    return box.list.history[-1][-1].toString()


def assert_recorded(box, sref):
    """The channel list zapped it, into the history - nothing played directly."""
    assert box.list.zaps == 1
    assert box.receiver.nav.played == []
    assert box.receiver.nav.sref == sref
    assert front(box) == sref
    assert last_error(box) is None


def assert_played_directly(box, sref):
    """Today's fallback: `playService`, the channel list and its history untouched."""
    assert box.list.zaps == 0
    assert InfoBar.instance.started == []
    assert box.receiver.nav.played == [sref]
    assert front(box) == POLSAT
    assert last_error(box) is None


def assert_popup_state(box):
    """Built by the model, not by hand: one popup, the info bar under it, hidden."""
    popup = box.session.current_dialog
    assert type(popup) is MessageBox
    assert box.session.dialog_stack == under_popup(box)
    assert box.session.in_exec is True
    return popup


# ------------------------------------------------------- recorded under a popup --


@pytest.mark.parametrize("kind", ["info", "warning", "error"])
def test_a_zap_under_the_plugins_own_popup_is_recorded(box, kind):
    assert osd.show("Dinner is ready", kind, 15) is None
    popup = assert_popup_state(box)

    send(box, "zap", {"sref": TVN})

    assert_recorded(box, TVN)
    # The popup is left alone, as the image's own channel-list zap leaves it: it
    # is still what the receiver shows, and closes on its own timeout.
    assert box.session.current_dialog is popup
    assert box.session.dialog_stack == under_popup(box)
    assert popup.execing is True


@pytest.mark.parametrize("osd_showing", [False, True], ids=["info bar hidden", "info bar showing"])
def test_a_zap_under_the_images_zap_timer_popup_is_recorded(box, osd_showing):
    """RecordTimer.pyc 664: `AddPopup(text=..., type=MessageBox.TYPE_INFO, timeout=...)`.

    The popup a zap timer leaves on the screen, no id. It arrives right after
    the timer's own zap, so the info bar's OSD is often still showing: the
    session then stacks the info bar as `(InfoBar.instance, True)` and hides
    it under the popup (StartEnigma.py 130-133, 94). Either way it is the info
    bar under a popup, and the zap is recorded.
    """
    if osd_showing:
        InfoBar.instance.show()
    AddPopup("Zapped to timer service TVN HD!", MessageBox.TYPE_INFO, 5)
    assert type(box.session.current_dialog) is MessageBox
    assert box.session.dialog_stack == under_popup(box, osd_showing)
    assert InfoBar.instance.shown is False

    send(box, "zap", {"sref": TVN})

    assert_recorded(box, TVN)


def test_a_zap_history_command_under_an_information_popup_is_the_history_screens(box):
    osd.show("Dinner is ready", "info", 15)
    assert_popup_state(box)

    send(box, "zap_history", {"sref": TVN})

    # `historyMenuClosed` moved the entry to the front and played it by its path.
    assert box.list.history_paths == 1
    assert front(box) == TVN
    assert box.receiver.nav.sref == TVN
    assert last_error(box) is None


def test_a_bouquet_zap_under_an_information_popup_goes_through_the_channel_list(box):
    osd.show("Dinner is ready", "info", 15)
    assert_popup_state(box)

    # POLSAT is not in it, so its first channel is tuned.
    send(box, "bouquet", {"sref": FIRST_BOUQUET})

    assert box.list.zaps == 1
    assert box.receiver.nav.played == []
    assert box.receiver.nav.sref == TVP1
    assert box.factory.client.last(BOUQUET).json()["sref"] == FIRST_BOUQUET
    assert last_error(box) is None


# ------------------------------------------------------- and when it is not --


@pytest.mark.parametrize(
    "arguments",
    [
        # RecordTimer.pyc 590 and 736-759, InfoBarGenerics 4021: no type at all.
        {},
        # The same question with its default answer the other way round.
        {"default": False},
        # Components/Timeshift 477-483: a question offered as a list of choices.
        {"list": [("Save the timeshift", "savetimeshift"), ("No", "no")]},
    ],
    ids=["no type", "default no", "choices"],
)
def test_a_zap_under_a_question_is_played_directly(box, arguments):
    AddNotification(MessageBox, "A timer failed to record! Disable TV and try again?",
                    timeout=20, **arguments)
    question = assert_popup_state(box)
    assert question.type == MessageBox.TYPE_YESNO
    assert question.list
    assert service_module.info_popup_over_infobar(box.session) is False

    send(box, "zap", {"sref": TVN})

    assert_played_directly(box, TVN)
    assert box.session.current_dialog is question


def test_a_question_without_answers_is_still_a_question(box):
    """The image always gives a question answers; the rule does not lean on that alone.

    Each check stands on its own: a question whose list was emptied after it
    opened is refused by its type, as a box with answers is refused by its list.
    """
    AddNotification(MessageBox, "A timer failed to record! Disable TV and try again?",
                    timeout=20)
    question = assert_popup_state(box)
    question.list = []
    assert service_module.info_popup_over_infobar(box.session) is False

    send(box, "zap", {"sref": TVN})

    assert_played_directly(box, TVN)


def test_a_box_with_answers_to_choose_from_is_a_question_whatever_its_type(box):
    """The image empties the list of every type but a question, at construction.

    Nothing on the image puts answers on an information box afterwards, and the
    rule does not take the type's word for it either: answers mean a question.
    """
    osd.show("Dinner is ready", "info", 15)
    popup = assert_popup_state(box)
    popup.list = [("Yes", True), ("No", False)]
    assert service_module.info_popup_over_infobar(box.session) is False

    send(box, "zap", {"sref": TVN})

    assert_played_directly(box, TVN)


def test_a_type_the_image_does_not_know_is_played_directly(box):
    # MessageBox.pyc 36-39 make it `TYPE_MESSAGE`, which no caller on the image
    # asks for; what the caller meant by it is unknown.
    AddPopup("Something", 7, 5)
    popup = assert_popup_state(box)
    assert popup.type == MessageBox.TYPE_MESSAGE
    assert popup.list == []
    assert service_module.info_popup_over_infobar(box.session) is False

    send(box, "zap", {"sref": TVN})

    assert_played_directly(box, TVN)


def test_a_popup_without_a_type_is_played_directly(box):
    osd.show("Dinner is ready", "info", 15)
    popup = assert_popup_state(box)
    del popup.type
    assert service_module.info_popup_over_infobar(box.session) is False

    send(box, "zap", {"sref": TVN})

    assert_played_directly(box, TVN)


class InformationSubclass(MessageBox):
    """A screen built on MessageBox - the image's `TryQuitMainloop` is one."""


def test_a_subclass_of_the_message_box_is_played_directly(box):
    AddNotificationWithID("mine", InformationSubclass, text="Almost done",
                          type=MessageBox.TYPE_INFO, timeout=5)
    popup = box.session.current_dialog
    assert type(popup) is InformationSubclass
    assert popup.type == MessageBox.TYPE_INFO and popup.list == []
    assert box.session.dialog_stack == under_popup(box)
    assert service_module.info_popup_over_infobar(box.session) is False

    send(box, "zap", {"sref": TVN})

    assert_played_directly(box, TVN)


class PlayerWithNotifications(MoviePlayer, NotifiableInfoBar):
    """The movie player, which drains the queue too (Screens/InfoBar.pyc 1845)."""


def test_an_information_popup_two_screens_deep_is_played_directly(box):
    player = box.session.open(PlayerWithNotifications)
    osd.show("Dinner is ready", "info", 15)
    popup = box.session.current_dialog
    assert type(popup) is MessageBox and popup.type == MessageBox.TYPE_INFO
    below = [screen for screen, _shown in box.below]
    assert [screen for screen, _shown in box.session.dialog_stack] == below + [
        InfoBar.instance, player]
    assert service_module.info_popup_over_infobar(box.session) is False

    send(box, "zap", {"sref": TVN})

    assert_played_directly(box, TVN)


def test_an_information_popup_over_another_screen_than_the_info_bar_is_played_directly(box):
    """One screen under the popup, and it is not the info bar the zap goes through.

    The popup is opened over the modelled info bar as usual; then the image's
    `InfoBar.instance` is another object with the same channel list - a base
    screen that is not the info bar, which is the case the stack test is for.
    """
    osd.show("Dinner is ready", "info", 15)
    base = box.session.dialog_stack[-1][0]
    InfoBar.instance = InfoBar(box.list)
    assert base is not InfoBar.instance
    assert service_module.info_popup_over_infobar(box.session) is False

    send(box, "zap", {"sref": TVN})

    assert box.list.zaps == 0
    assert box.receiver.nav.played == [TVN]


def test_a_popup_that_is_closing_is_played_directly(box):
    # StartEnigma.py 164-180: after `close`, the popup stays the current dialog
    # for one more turn of the main loop, with the session no longer executing
    # it. A popup on its way out is not the state the rule is about.
    osd.show("Dinner is ready", "info", 15)
    popup = assert_popup_state(box)
    popup.close(True)
    assert box.session.current_dialog is popup
    assert box.session.in_exec is False
    assert service_module.info_popup_over_infobar(box.session) is False

    send(box, "zap", {"sref": TVN})

    assert_played_directly(box, TVN)
    MainLoop.advance(0)
    assert box.session.current_dialog is InfoBar.instance


def test_an_unexpected_stack_entry_is_a_screen_open_and_nothing_raises(box):
    """`StartEnigma` stacks `(screen, shown)` tuples; anything else is not trusted."""
    osd.show("Dinner is ready", "info", 15)
    assert_popup_state(box)
    box.session.dialog_stack[-1] = InfoBar.instance
    assert service_module.info_popup_over_infobar(box.session) is False

    send(box, "zap", {"sref": TVN})

    assert box.list.zaps == 0
    assert box.receiver.nav.played == [TVN]


def test_a_zap_history_command_under_a_question_leaves_the_history_alone(box):
    AddNotification(MessageBox, "Do you want to resume playback?", timeout=20)
    assert_popup_state(box)
    assert service_module.info_popup_over_infobar(box.session) is False
    before = [item[-1].toString() for item in box.list.history]

    send(box, "zap_history", {"sref": TVN})

    assert box.list.history_paths == 0
    assert [item[-1].toString() for item in box.list.history] == before
    assert box.receiver.nav.played == [TVN]


def test_a_bouquet_zap_under_a_question_is_played_directly(box):
    AddNotification(MessageBox, "A timer failed to record! Disable TV and try again?",
                    timeout=20)
    assert_popup_state(box)
    assert service_module.info_popup_over_infobar(box.session) is False

    send(box, "bouquet", {"sref": FIRST_BOUQUET})

    assert box.list.zaps == 0
    assert box.receiver.nav.played == [TVP1]


def test_history_clear_is_still_refused_under_an_information_popup(box):
    """It is the 0 key, and with a popup on the screen the key goes to the popup."""
    osd.show("Dinner is ready", "info", 15)
    assert_popup_state(box)
    assert service_module.info_popup_over_infobar(box.session) is True

    send(box, "history_clear", "PRESS")

    assert last_error(box)["reason"] == "screen_open"
    assert last_error(box)["error"] == zaphistory.SCREEN_OPEN[0]
    assert InfoBar.instance.keys == []


def test_a_refused_popup_says_in_the_log_which_condition_refused(box, plugin_log):
    """The next hardware surprise explains itself, at the default level."""
    AddNotification(MessageBox, "A timer failed to record! Disable TV and try again?",
                    timeout=20)
    assert_popup_state(box)

    send(box, "zap", {"sref": TVN})

    assert_played_directly(box, TVN)
    log = plugin_log()
    assert (
        "INFO MQTTBridge.service: the channel list is not used for this zap: "
        "the message box is of type 0, not information, warning or error"
    ) in log
    assert "A timer failed" not in log


def test_a_popup_over_the_movie_player_names_the_stack_in_the_log(box, plugin_log):
    box.session.open(PlayerWithNotifications)
    osd.show("Dinner is ready", "info", 15)

    send(box, "zap", {"sref": TVN})

    below = "SessionStartScreen > " if box.below else ""
    assert ("the screen under the message box is not the info bar (stack: " + below
            + "ModalInfoBar > PlayerWithNotifications)") in plugin_log()
    assert "Dinner" not in plugin_log()
