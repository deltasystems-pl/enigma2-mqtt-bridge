"""The CEC standby workaround, against a model of the image rather than a mock of it.

What this feature gets wrong if it is written from intuition is **timing and
identity**, and a mock would agree with both mistakes. So the receiver is
modelled from its own bytecode (see the notes in `conftest.py`): the notification
queue and its no-argument listeners, `HdmiCec` and the bracket it puts around the
queueing call, a session whose `close()` pops nothing until a 0 ms timer fires, an
info bar that drains the queue only while it is executing, and a standby screen
that moves the standby counter the moment it runs. `MainLoop.advance` turns the
loop. `HdmiCec.sent` is what went to the television, and `"standby"` in it is the
echo this feature exists to stop.

The screen classes below carry the **names and bases** `Screens/ChannelSelection.pyc`
and its neighbours compile to on the receiver, because the allowlist is matched
against exactly those names.
"""

import sys
import types

import pytest
import Screens.Standby as standby_module
import Tools.Notifications as notifications_module
from Components.config import config
from conftest import (
    STANDBY_OPCODE,
    HdmiCec,
    MainLoop,
    ModalSession,
    ModelScreen,
    NotifiableInfoBar,
    notificationAdded,
    notifications,
)

from MQTTBridge import cec, power
from MQTTBridge import config as settings_module
from MQTTBridge.cec import CecPublisher

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
CEC = ROOT + "/cec"
INFO = ROOT + "/info"


# ------------------------------------------------------------ the image's screens --


class Recorded(ModelScreen):
    """A screen that remembers its own exit - and whether the echo was held then."""

    def __init__(self, session, *args, **kwargs):
        ModelScreen.__init__(self, session)
        self.cancelled = []

    def cancel(self):
        # Recorded as the image reads it - a yes or no (`HdmiCec` source line
        # 618) - because the plugin's hold is truthy without being `True`.
        instance = HdmiCec.instance
        self.cancelled.append(None if instance is None else bool(instance.handlingStandbyFromTV))
        self.close(None)


def _screen(name, bases, module):
    return type(name, bases, {"__module__": module})


CS = "Screens.ChannelSelection"
Screen = _screen("Screen", (Recorded,), "Screens.Screen")
HelpableScreen = _screen("HelpableScreen", (), "Screens.HelpMenu")
ProtectedScreen = _screen("ProtectedScreen", (), "Screens.ParentalControlSetup")
InfoBarButtonSetup = _screen("InfoBarButtonSetup", (), "Screens.ButtonSetup")
InfoBarBase = _screen("InfoBarBase", (), "Screens.InfoBarGenerics")

# `Screens/ChannelSelection.pyc`, class by class, with the bases it compiles to.
ChannelSelectionEdit = _screen("ChannelSelectionEdit", (), CS)
SelectionEventInfo = _screen("SelectionEventInfo", (), CS)
ChannelSelectionEPG = _screen("ChannelSelectionEPG", (InfoBarButtonSetup, HelpableScreen), CS)
ChannelSelectionBase = _screen("ChannelSelectionBase", (Screen, HelpableScreen), CS)
ChannelSelection = _screen(
    "ChannelSelection",
    (ChannelSelectionEdit, ChannelSelectionBase, ChannelSelectionEPG, SelectionEventInfo),
    CS,
)
PiPZapSelection = _screen("PiPZapSelection", (ChannelSelection,), CS)
ChannelSelectionRadio = _screen(
    "ChannelSelectionRadio",
    (ChannelSelectionEdit, ChannelSelectionBase, ChannelSelectionEPG, InfoBarBase,
     SelectionEventInfo),
    CS,
)
SimpleChannelSelection = _screen("SimpleChannelSelection", (ChannelSelectionBase,), CS)
ChannelContextMenu = _screen("ChannelContextMenu", (Screen,), CS)
BouquetSelector = _screen("BouquetSelector", (Screen,), CS)
EpgBouquetSelector = _screen("EpgBouquetSelector", (BouquetSelector,), CS)
HistoryZapSelector = _screen("HistoryZapSelector", (Screen, HelpableScreen), CS)

# Everything else the specification names, each from its own module.
EPGSelectionBase = _screen("EPGSelectionBase", (Screen, HelpableScreen),
                           "Screens.EpgSelectionBase")
EPGSelectionChannel = _screen("EPGSelectionChannel", (EPGSelectionBase,),
                              "Screens.EpgSelectionChannel")
EPGServiceZap = _screen("EPGServiceZap", (), "Screens.EpgSelectionBase")
EPGSelection = _screen("EPGSelection", (EPGSelectionChannel, EPGServiceZap),
                       "Screens.EpgSelection")
EPGServiceNumberSelectionPopup = _screen("EPGServiceNumberSelectionPopup", (Screen,),
                                         "Screens.EpgSelectionBase")
Menu = _screen("Menu", (Screen, HelpableScreen, ProtectedScreen), "Screens.Menu")
MainMenu = _screen("MainMenu", (Menu,), "Screens.Menu")
MenuSort = _screen("MenuSort", (Menu,), "Screens.Menu")
PluginBrowser = _screen("PluginBrowser", (Screen, ProtectedScreen), "Screens.PluginBrowser")
PluginDownloadBrowser = _screen("PluginDownloadBrowser", (Screen,), "Screens.PluginBrowser")
InputBox = _screen("InputBox", (Screen,), "Screens.InputBox")
VirtualKeyBoard = _screen("VirtualKeyBoard", (Screen,), "Screens.VirtualKeyBoard")
ChoiceBox = _screen("ChoiceBox", (Screen,), "Screens.ChoiceBox")
MessageBox = _screen("MessageBox", (Screen,), "Screens.MessageBox")
TimerEntryBase = _screen("TimerEntryBase", (Screen,), "Screens.TimerEntryBase")
TimerEntry = _screen("TimerEntry", (TimerEntryBase,), "Screens.TimerEntry")
Standby2 = _screen("Standby2", (Screen,), "Screens.Standby")
StandbyDialog = _screen("Standby", (Standby2,), "Screens.Standby")
TryQuitMainloop = _screen("TryQuitMainloop", (MessageBox,), "Screens.Standby")

ALLOWLISTED = (ChannelSelection, ChannelSelectionRadio, PiPZapSelection)

# 🔴 The specification's „must not match" table, as the parameter list. Two of
# them are the reason it is a table: `SimpleChannelSelection` inherits
# `ChannelSelectionBase` exactly as the real lists do, and `ChannelContextMenu`
# lives in the same module as they do.
NOT_ALLOWLISTED = (
    SimpleChannelSelection,
    ChannelContextMenu,
    ChannelSelectionBase,
    BouquetSelector,
    EpgBouquetSelector,
    HistoryZapSelector,
    EPGSelection,
    EPGSelectionBase,
    EPGServiceNumberSelectionPopup,
    Menu,
    MainMenu,
    MenuSort,
    PluginBrowser,
    PluginDownloadBrowser,
    InputBox,
    VirtualKeyBoard,
    ChoiceBox,
    MessageBox,
    TimerEntry,
    TimerEntryBase,
    StandbyDialog,
    TryQuitMainloop,
)


# ------------------------------------------------------------------ the world --


class World:
    """A receiver with HDMI-CEC running, the info bar up, and the bridge connected."""

    def __init__(self, make_bridge, factory, settings, workaround=True, image_first=True,
                 base=NotifiableInfoBar):
        settings.host.value = "10.0.0.5"
        settings.node_id.value = NODE
        settings.friendly_name.value = "Living room receiver"
        settings.cec_standby_workaround.value = workaround
        # What is left alone is said at debug, and some tests read it.
        settings.log_level.value = "debug"
        self.factory = factory
        # The image builds its singleton before any plugin starts.
        self.hdmi = HdmiCec()
        self.session = ModalSession()
        self.bridge = make_bridge(session=self.session)
        self.publisher = self.bridge.register_publisher(CecPublisher())
        self.bridge.start()
        factory.client.fire_connect()
        if not image_first:
            # 🔴 The order a reader has to worry about: the image's own notifier
            # on the standby counter runs *after* the plugin's.
            counter = config.misc.standbyCounter
            counter.removeNotifier(self.hdmi.onEnterStandby)
            counter.addNotifier(self.hdmi.onEnterStandby, initial_call=False)
            assert counter.notifiers.index(self.hdmi.onEnterStandby) > counter.notifiers.index(
                self.publisher._counter_moved
            )
        # The info bar comes up after the plugin's session start, so on a real
        # box the plugin's listener is first in `notificationAdded`.
        self.base = self.session.open(base)

    def show(self, screen_class):
        """Show a screen the way the info bar shows the channel list: `execDialog`."""
        dialog = self.session.instantiateDialog(screen_class)
        self.session.execDialog(dialog)
        return dialog

    def tv_standby(self):
        """The television switched itself off and said so over HDMI-CEC."""
        self.hdmi.messageReceived(STANDBY_OPCODE)
        return notifications[-1] if notifications else None

    def cec(self):
        entry = self.factory.client.last(CEC)
        return None if entry is None or entry.text == "" else entry.json()

    @property
    def held(self):
        return HdmiCec.instance.handlingStandbyFromTV


@pytest.fixture
def world(make_bridge, factory, settings):
    def build(**options):
        return World(make_bridge, factory, settings, **options)

    return build


def queued(entry):
    return any(item is entry for item in notifications)


# ------------------------------------------------------------ part 1: the close --


def test_the_channel_list_is_closed_and_the_standby_proceeds(world):
    box = world()
    channel_list = box.show(ChannelSelection)

    box.tv_standby()
    MainLoop.advance(0)

    assert channel_list.cancelled == [True]
    assert isinstance(standby_module.inStandby, standby_module.Standby)
    assert notifications == []
    # Counted once the standby has happened and the image has read the flag.
    MainLoop.advance(cec.SETTLE_MILLISECONDS)
    payload = box.cec()
    assert payload["kind"] == "closed_channel_list"
    assert payload["count"] == 1
    assert isinstance(payload["last_intervention"], int)
    assert payload["pending"] is False


@pytest.mark.parametrize("image_first", (True, False), ids=("image-first", "image-last"))
def test_the_echo_is_held_suppressed_across_the_close(world, image_first):
    """🔴 The flag is `True` when the close is issued and stays so until the image has read it.

    Parametrised on the order of the two notifiers on the standby counter: the
    image's reads the flag, the plugin's decides when to put it back, and the
    answer must not depend on which runs first.
    """
    box = world(image_first=image_first)
    channel_list = box.show(ChannelSelection)

    box.tv_standby()
    assert box.held is False  # the image's own bracket has already closed
    MainLoop.advance(0)

    assert channel_list.cancelled == [True]
    assert box.hdmi.sent == ["sourceinactive"]
    assert "standby" not in box.hdmi.sent
    assert box.held is cec.HELD

    MainLoop.advance(cec.SETTLE_MILLISECONDS)
    assert box.held is False


def test_the_echo_is_suppressed_when_the_image_sends_a_second_later(world):
    """With `next_boxes_detect` on, the image reads the flag from its own one-second timer."""
    config.hdmicec.next_boxes_detect.value = True
    box = world(image_first=False)
    box.show(ChannelSelection)

    box.tv_standby()
    MainLoop.advance(0)
    assert box.hdmi.sent == []
    MainLoop.advance(1000)

    assert box.hdmi.sent == ["sourceinactive"]
    MainLoop.advance(cec.SETTLE_MILLISECONDS)
    assert box.held is False


def test_the_flag_is_put_back_to_the_value_it_had(world):
    box = world()
    box.show(ChannelSelection)
    box.tv_standby()
    # Whatever the image had there, that is what comes back - not `False`.
    box.hdmi.handlingStandbyFromTV = "sentinel"

    MainLoop.advance(0)
    assert box.held is cec.HELD
    MainLoop.advance(cec.SETTLE_MILLISECONDS)

    assert box.held == "sentinel"


def test_the_hold_ends_at_its_deadline_when_the_standby_never_happens(world):
    """🔴 The deadline: a hold that leaked would stop „standby turns the TV off" for good."""
    # Nothing under the channel list drains the queue, so closing it releases
    # nothing and the standby counter never moves.
    box = world(base=Screen)
    channel_list = box.show(ChannelSelection)

    box.tv_standby()
    MainLoop.advance(0)
    assert channel_list.cancelled == [True]
    assert config.misc.standbyCounter.value == 0
    assert box.held is cec.HELD

    MainLoop.advance(cec.HOLD_SECONDS * 1000 - 1)
    assert box.held is cec.HELD
    MainLoop.advance(1)
    assert box.held is False


def test_without_the_workaround_the_late_standby_echoes(world):
    """The defect itself, on the model: proof that the test above is testing something."""
    box = world(workaround=False)
    channel_list = box.show(ChannelSelection)

    box.tv_standby()
    MainLoop.advance(60000)
    assert box.session.current_dialog is channel_list
    assert len(notifications) == 1

    channel_list.close(None)  # somebody presses EXIT, long after
    MainLoop.advance(0)

    assert box.hdmi.sent == ["standby"]


def test_with_the_info_bar_in_front_the_image_needs_no_help(world):
    box = world()

    box.tv_standby()
    MainLoop.advance(0)
    MainLoop.advance(cec.SETTLE_MILLISECONDS)

    assert box.hdmi.sent == ["sourceinactive"]
    payload = box.cec()
    assert payload == {"last_intervention": None, "kind": None, "count": 0, "pending": False}
    assert box.held is False


@pytest.mark.parametrize("screen_class", ALLOWLISTED, ids=lambda item: item.__name__)
def test_the_allowlist_is_the_three_channel_lists(world, screen_class):
    box = world()
    dialog = box.show(screen_class)

    box.tv_standby()
    MainLoop.advance(0)

    assert dialog.cancelled == [True]


@pytest.mark.parametrize("screen_class", NOT_ALLOWLISTED, ids=lambda item: item.__name__)
def test_any_other_screen_is_left_alone(world, plugin_log, screen_class):
    box = world()
    dialog = box.show(screen_class)

    entry = box.tv_standby()
    MainLoop.advance(0)

    assert dialog.cancelled == []
    assert box.session.current_dialog is dialog
    assert queued(entry)
    assert box.cec()["pending"] is True
    assert box.cec()["count"] == 0
    assert "which is not the channel list; leaving it alone" in plugin_log()
    assert screen_class.__name__ in plugin_log()


def test_the_allowlist_is_exactly_three_names():
    assert cec.ALLOWLIST == ("ChannelSelection", "ChannelSelectionRadio", "PiPZapSelection")


def test_the_timings_are_the_specifications():
    """Pinned as literals: the other tests are written relative to these, so cannot."""
    assert cec.HOLD_SECONDS == 5
    assert cec.STALE_SECONDS == 30
    assert cec.SETTLE_MILLISECONDS == 1500


def test_a_subclass_of_a_channel_list_is_matched_through_its_bases(world):
    """A skin or plugin that subclasses the channel list is still the channel list.

    Its own name is on no list, so only the match against the class's bases
    finds it - `PiPZapSelection` cannot show that, because it is listed by name.
    """
    Skinned = _screen("SkinnedChannelSelection", (ChannelSelection,), "Plugins.Extensions.Skin")
    box = world()
    dialog = box.show(Skinned)

    box.tv_standby()
    MainLoop.advance(0)

    assert dialog.cancelled == [True]


def test_a_dialog_stacked_on_the_channel_list_leaves_both_open(world):
    box = world()
    channel_list = box.show(ChannelSelection)
    menu = box.session.open(ChannelContextMenu)

    entry = box.tv_standby()
    MainLoop.advance(0)

    assert box.session.current_dialog is menu
    assert [dialog for dialog, _shown in box.session.dialog_stack][-1] is channel_list
    assert menu.cancelled == [] and channel_list.cancelled == []
    assert queued(entry)


def test_the_list_closing_underneath_the_hook_is_a_no_op(world, plugin_log):
    box = world()
    channel_list = box.show(ChannelSelection)

    box.tv_standby()
    channel_list.close(None)  # EXIT, in the same instant
    MainLoop.advance(0)

    # The user's close went through, the standby with it, and nothing was
    # closed a second time or counted.
    assert channel_list.cancelled == []
    assert isinstance(standby_module.inStandby, standby_module.Standby)
    assert box.cec()["count"] == 0
    assert "Traceback" not in plugin_log()


def test_a_list_that_is_already_closing_is_not_closed_again(world):
    """The session's own guard: after a close, nothing executes until the pop."""
    box = world()
    channel_list = box.show(ChannelSelection)

    box.tv_standby()
    box.publisher._act_now()
    box.publisher._act_now()

    assert channel_list.cancelled == [True]


def test_each_standby_closes_at_most_once(world):
    """The one-shot on the record, where the session's guard no longer stands in for it.

    Nothing under the list drains the queue, so the television's standby is
    still waiting after the close; the household opens the channel list again,
    and the workaround looks again. It has already acted for that standby, and
    must not close the list a second time.
    """
    box = world(base=Screen)
    first = box.show(ChannelSelection)
    entry = box.tv_standby()
    MainLoop.advance(0)
    assert first.cancelled == [True]
    assert queued(entry)

    again = box.show(ChannelSelection)
    assert box.session.in_exec and box.session.current_dialog is again
    box.publisher._act_now()
    MainLoop.advance(0)

    assert again.cancelled == []
    assert box.session.current_dialog is again


# -------------------------------------------------- never the household's standby --


def _cmd_power_standby():
    assert power.enter_standby() is None


# The remote's power button is not here because it never reaches the queue: on
# the image this was measured on, `PowerKey.standby` opens the standby screen
# directly (`StartEnigma.py`), and nothing notifies `notificationAdded`.
@pytest.mark.parametrize("screen_class", (ChannelSelection, Menu), ids=lambda c: c.__name__)
def test_a_standby_the_household_asked_for_is_never_touched(world, screen_class):
    """🔴 Byte-identical to the television's, and never tracked, closed or dropped."""
    box = world()
    dialog = box.show(screen_class)

    _cmd_power_standby()
    entry = notifications[-1]
    assert entry == (None, standby_module.Standby, (), {}, None)
    MainLoop.advance(cec.STALE_SECONDS * 1000 * 2)

    assert dialog.cancelled == []
    assert box.session.current_dialog is dialog
    assert notifications == [entry] and notifications[0] is entry
    assert box.cec() == {"last_intervention": None, "kind": None, "count": 0, "pending": False}
    assert box.held is False


def test_a_household_standby_during_the_hold_is_not_taken_for_the_televisions(world):
    """🔴 The plugin's own hold must not pass for the television's bracket.

    The list is closed and the flag held, but a popup was already queued ahead
    of the television's standby, so the info bar opens that first and the
    standby keeps waiting. A second later Home Assistant asks for standby -
    exactly the „television off, so receiver off" automation. That standby is
    queued while the flag is held, and must not be identified as the
    television's, or its deadline would silently throw the household's request
    away.
    """
    box = world()
    box.show(ChannelSelection)
    notifications_module.AddNotification(MessageBox, "a timer message")
    television = box.tv_standby()
    MainLoop.advance(0)

    assert isinstance(box.session.current_dialog, MessageBox)
    assert queued(television)
    assert box.held  # the hold is in place

    MainLoop.advance(1000)
    _cmd_power_standby()
    household = notifications[-1]
    assert household == television and household is not television
    MainLoop.advance(cec.STALE_SECONDS * 1000)

    assert queued(household)
    assert not queued(television)
    assert [queued_entry.entry for queued_entry in box.publisher._queued] == []
    payload = box.cec()
    assert payload["count"] == 1
    assert payload["kind"] == "dropped_stale_standby"


def test_a_second_close_inside_one_hold_keeps_the_value_to_put_back(world):
    """🔴 A hold already in place never records its own marker as the value to restore.

    Otherwise the marker would be put back at the end - and stay in the flag
    for the rest of the session, which is exactly the leak the deadline exists
    to prevent.
    """
    box = world(base=Screen)
    box.show(ChannelSelection)
    box.tv_standby()
    MainLoop.advance(0)
    assert box.held is cec.HELD

    box.publisher._start_hold()
    assert box.held is cec.HELD
    MainLoop.advance(cec.HOLD_SECONDS * 1000)

    assert box.held is False


def test_two_closes_inside_one_hold_both_keep_the_echo_suppressed(world):
    """The television's second standby writes `False` over the hold; the second close re-holds.

    The list is closed for the first standby, but a popup queued ahead is shown
    first. Within the hold somebody opens the list again and the television
    asks again, so the list is closed a second time. When the popup is then
    dismissed, the first standby finally runs - and must still not be echoed.
    """
    box = world()
    first = box.show(ChannelSelection)
    notifications_module.AddNotification(MessageBox, "a timer message")
    box.tv_standby()
    MainLoop.advance(0)
    assert first.cancelled == [True]
    popup = box.session.current_dialog
    assert isinstance(popup, MessageBox)

    MainLoop.advance(1000)
    again = box.show(ChannelSelection)
    second = box.tv_standby()
    assert box.held is False  # the image's own bracket has just ended the hold
    MainLoop.advance(0)
    assert again.cancelled == [True]
    assert box.held is cec.HELD

    popup.close()
    MainLoop.advance(0)
    assert isinstance(standby_module.inStandby, standby_module.Standby)
    assert box.hdmi.sent == ["sourceinactive"]

    MainLoop.advance(cec.SETTLE_MILLISECONDS)
    assert box.held is False
    assert not queued(second)
    assert box.cec()["count"] == 1
    assert box.cec()["kind"] == "closed_channel_list"


def test_a_second_close_restarts_the_hold_deadline(world):
    """🔴 The hold ends five seconds after the *last* close, not the first.

    The list is closed at 0 s for a standby that then waits behind a popup, and
    closed again at 4 s for the television's second standby. The popup is
    dismissed at 6 s - after the first close's deadline, inside the second's -
    and the standby that finally runs must still not be echoed. A deadline that
    kept counting from the first close would have put the flag back at 5 s.
    """
    box = world()
    box.show(ChannelSelection)
    notifications_module.AddNotification(MessageBox, "a timer message")
    box.tv_standby()
    MainLoop.advance(0)
    popup = box.session.current_dialog
    assert isinstance(popup, MessageBox)

    MainLoop.advance(4000)
    again = box.show(ChannelSelection)
    box.tv_standby()
    MainLoop.advance(0)
    assert again.cancelled == [True]
    assert box.held is cec.HELD

    # Past the first close's five seconds: still held.
    MainLoop.advance(cec.HOLD_SECONDS * 1000 - 4000 + 1000)
    assert box.held is cec.HELD

    popup.close()
    MainLoop.advance(0)
    assert isinstance(standby_module.inStandby, standby_module.Standby)
    assert "standby" not in box.hdmi.sent
    assert box.hdmi.sent == ["sourceinactive"]

    MainLoop.advance(cec.SETTLE_MILLISECONDS)
    assert box.held is False


def test_without_a_standby_the_hold_ends_five_seconds_after_the_last_close(world):
    """The deadline itself, to the millisecond, when nothing ever drains the queue."""
    box = world(base=Screen)
    box.show(ChannelSelection)
    box.tv_standby()
    MainLoop.advance(0)
    assert box.held is cec.HELD

    MainLoop.advance(3000)
    box.show(ChannelSelection)
    box.tv_standby()
    MainLoop.advance(0)
    assert box.held is cec.HELD

    MainLoop.advance(cec.HOLD_SECONDS * 1000 - 1)
    assert box.held is cec.HELD
    MainLoop.advance(1)
    assert box.held is False


def test_the_hold_is_truthy_but_is_not_true(world):
    """Truthy, so the image still does not echo; not `True`, so it is not the bracket."""
    box = world(base=Screen)
    box.show(ChannelSelection)
    box.tv_standby()
    MainLoop.advance(0)

    assert box.held is cec.HELD
    assert bool(box.held) is True
    assert box.held is not True


def test_a_notification_that_is_not_a_standby_is_ignored_even_inside_the_bracket(world):
    box = world()
    box.show(Menu)

    box.hdmi.handlingStandbyFromTV = True
    notifications_module.AddNotificationWithID("popup", MessageBox, "hello")
    box.hdmi.handlingStandbyFromTV = False
    MainLoop.advance(cec.STALE_SECONDS * 1000)

    assert len(notifications) == 1
    assert box.cec()["pending"] is False


# ------------------------------------------------------------- part 2: stale --


def test_a_stale_standby_is_dropped_by_identity(world):
    """🔴 An equal tuple - the household's own standby - survives the drop."""
    box = world()
    menu = box.show(Menu)
    _cmd_power_standby()
    household = notifications[-1]

    television = box.tv_standby()
    assert household == television and household is not television
    MainLoop.advance(0)
    assert box.cec()["pending"] is True

    MainLoop.advance(cec.STALE_SECONDS * 1000 - 1)
    assert queued(television)
    MainLoop.advance(1)

    assert notifications == [household] and notifications[0] is household
    assert menu.cancelled == []
    payload = box.cec()
    assert payload["kind"] == "dropped_stale_standby"
    assert payload["count"] == 1
    assert payload["pending"] is False


def test_closing_the_menu_after_the_drop_does_not_put_the_box_to_sleep(world):
    box = world()
    menu = box.show(Menu)
    box.tv_standby()
    MainLoop.advance(cec.STALE_SECONDS * 1000)

    menu.close()
    MainLoop.advance(0)

    assert standby_module.inStandby is None
    assert config.misc.standbyCounter.value == 0
    assert box.hdmi.sent == []


def test_a_standby_drained_normally_is_not_touched_and_is_forgotten(world):
    box = world()
    menu = box.show(Menu)
    box.tv_standby()
    MainLoop.advance(0)
    assert box.cec()["pending"] is True

    menu.close()  # the household leaves the menu; the queue drains as it always did
    MainLoop.advance(0)
    assert isinstance(standby_module.inStandby, standby_module.Standby)
    MainLoop.advance(cec.SETTLE_MILLISECONDS)
    assert box.cec()["pending"] is False

    MainLoop.advance(cec.STALE_SECONDS * 1000)
    assert box.cec()["count"] == 0
    assert box.publisher._queued == []


def test_a_close_that_released_nothing_is_counted_once_as_a_drop(world):
    """One standby, one intervention: the close is not a success if nothing followed it."""
    box = world(base=Screen)
    box.show(ChannelSelection)

    entry = box.tv_standby()
    MainLoop.advance(0)
    assert box.cec()["count"] == 0
    assert box.cec()["kind"] is None
    MainLoop.advance(cec.STALE_SECONDS * 1000)

    assert not queued(entry)
    payload = box.cec()
    assert payload["count"] == 1
    assert payload["kind"] == "dropped_stale_standby"


def test_a_close_is_counted_when_the_standby_has_happened(world, plugin_log):
    box = world()
    box.show(ChannelSelection)

    box.tv_standby()
    MainLoop.advance(0)
    assert isinstance(standby_module.inStandby, standby_module.Standby)
    assert box.cec()["count"] == 0
    MainLoop.advance(cec.SETTLE_MILLISECONDS)

    assert box.cec()["count"] == 1
    assert box.cec()["kind"] == "closed_channel_list"
    MainLoop.advance(cec.STALE_SECONDS * 1000)
    assert box.cec()["count"] == 1
    assert "went ahead after the channel list was closed" in plugin_log()


def test_without_the_standby_counter_a_close_is_counted_at_the_deadline(world):
    """On an image whose standby counter could not be watched, being drained is the evidence."""
    box = world()
    config.misc.standbyCounter.removeNotifier(box.publisher._counter_moved)
    box.show(ChannelSelection)

    box.tv_standby()
    MainLoop.advance(0)
    assert isinstance(standby_module.inStandby, standby_module.Standby)
    MainLoop.advance(cec.STALE_SECONDS * 1000 - 1)
    assert box.cec()["count"] == 0
    MainLoop.advance(1)

    payload = box.cec()
    assert payload["count"] == 1
    assert payload["kind"] == "closed_channel_list"
    assert payload["pending"] is False


@pytest.mark.parametrize("closed", (True, False), ids=("list-closed", "behind-a-menu"))
def test_a_television_standby_overtaken_by_the_remote_is_counted_once_as_a_drop(world, closed):
    """The receiver goes to standby some other way while the television's standby waits.

    Either the list was closed for it but a popup queued ahead is showing, or it
    sits behind a menu. The remote's power button opens the standby screen
    directly; none of the television's standbys ran. Removing it is the plugin
    throwing it away - a drop, counted once, and not a close, which released
    nothing.
    """
    box = world()
    if closed:
        channel_list = box.show(ChannelSelection)
        notifications_module.AddNotification(MessageBox, "a timer message")
    else:
        box.show(Menu)
    television = box.tv_standby()
    MainLoop.advance(0)
    if closed:
        assert channel_list.cancelled == [True]
    assert queued(television)

    MainLoop.advance(1000)
    box.session.open(standby_module.Standby)  # the remote's power button
    MainLoop.advance(0)
    assert isinstance(standby_module.inStandby, standby_module.Standby)
    MainLoop.advance(cec.SETTLE_MILLISECONDS)

    assert not queued(television)
    payload = box.cec()
    assert payload["count"] == 1
    assert payload["kind"] == "dropped_stale_standby"
    assert payload["pending"] is False
    MainLoop.advance(cec.STALE_SECONDS * 1000)
    assert box.cec()["count"] == 1


def test_without_the_standby_counter_a_standby_drained_without_a_close_is_not_counted(world):
    """The deadline's fallback counts a close; with no close there is nothing to count."""
    box = world()
    config.misc.standbyCounter.removeNotifier(box.publisher._counter_moved)
    menu = box.show(Menu)
    box.tv_standby()
    MainLoop.advance(0)

    menu.close()  # the household leaves the menu; the queue drains as it always did
    MainLoop.advance(0)
    assert isinstance(standby_module.inStandby, standby_module.Standby)
    MainLoop.advance(cec.STALE_SECONDS * 1000)

    assert box.cec() == {"last_intervention": None, "kind": None, "count": 0, "pending": False}


def test_a_repeated_television_standby_is_removed_once_the_receiver_sleeps(world):
    """The second `<Standby>` must not put a woken receiver straight back to sleep.

    The television says it twice before the list is closed; the info bar
    carries out one entry per turn, so the second is still queued when the
    receiver goes to standby. The household's own standby, queued alongside,
    is not the plugin's to touch and stays.
    """
    box = world()
    box.show(ChannelSelection)
    first = box.tv_standby()
    second = box.tv_standby()
    _cmd_power_standby()
    household = notifications[-1]
    assert first is not second and household is not first and household is not second
    MainLoop.advance(0)
    assert not queued(first)
    assert queued(second)
    assert isinstance(standby_module.inStandby, standby_module.Standby)

    MainLoop.advance(cec.SETTLE_MILLISECONDS)

    assert not queued(second)
    assert notifications == [household] and notifications[0] is household
    assert box.publisher._queued == []
    payload = box.cec()
    assert payload["count"] == 1
    assert payload["kind"] == "closed_channel_list"
    assert payload["pending"] is False


# --------------------------------------------------------- nothing reaches the GUI --


def test_a_session_that_raises_is_swallowed(world, plugin_log):
    box = world()
    box.show(ChannelSelection)

    class Broken:
        in_exec = True

        @property
        def current_dialog(self):
            raise RuntimeError("no dialog")

    box.tv_standby()
    box.bridge.session = Broken()
    MainLoop.advance(0)

    assert "could not read the current dialog" in plugin_log()
    assert box.held is False


def test_a_cancel_that_raises_is_swallowed(world, plugin_log):
    box = world()
    dialog = box.show(ChannelSelection)

    def boom():
        raise RuntimeError("closing")

    dialog.cancel = boom
    box.tv_standby()
    MainLoop.advance(0)

    assert "closing ChannelSelection raised" in plugin_log()
    assert box.cec()["count"] == 0
    MainLoop.advance(cec.HOLD_SECONDS * 1000)
    assert box.held is False


def test_a_dialog_without_cancel_is_closed_with_close(world):
    box = world()
    dialog = box.show(ChannelSelection)
    dialog.cancel = None

    box.tv_standby()
    MainLoop.advance(0)

    assert isinstance(standby_module.inStandby, standby_module.Standby)
    MainLoop.advance(cec.SETTLE_MILLISECONDS)
    assert box.cec()["kind"] == "closed_channel_list"


def test_the_list_is_still_closed_when_hdmi_cec_has_gone(world, plugin_log):
    box = world()
    channel_list = box.show(ChannelSelection)

    box.tv_standby()
    HdmiCec.instance = None
    MainLoop.advance(0)

    assert channel_list.cancelled == [None]
    MainLoop.advance(cec.SETTLE_MILLISECONDS)
    assert box.cec()["kind"] == "closed_channel_list"
    assert "HDMI-CEC is no longer reachable" in plugin_log()


def test_a_hook_that_raises_inside_the_queue_is_swallowed_and_logged_once(world, plugin_log):
    box = world()
    notifications.append(3)  # not a tuple: indexing it raises

    box.publisher._notification_added()
    box.publisher._notification_added()

    assert plugin_log().count("the CEC standby hook raised;") == 1


def test_the_hook_returns_at_once_for_a_screen_that_is_not_the_standby(world, monkeypatch):
    box = world()
    reads = []
    spy = type("Spy", (), {"instance": property(lambda self: reads.append(1))})()

    monkeypatch.setattr(box.publisher, "_cec_class", spy)
    notifications_module.AddNotification(MessageBox, "hello")
    notifications_module.AddNotification(standby_module.Standby)

    # Read once - for the standby - and not at all for the message box.
    assert reads == [1]


# ------------------------------------------------------- off means nothing at all --


MODULE_READS = []


class SpyModule(types.ModuleType):
    """A module that remembers every name read from it, in `MODULE_READS`."""

    def __getattribute__(self, name):
        if not name.startswith("__"):
            MODULE_READS.append(name)
        return types.ModuleType.__getattribute__(self, name)


def test_switched_off_nothing_is_bound_read_or_published(
    make_bridge, factory, settings, receiver, monkeypatch
):
    """🔴 The acceptance, literally: no listener, no read of `HdmiCec`, no capability."""
    hdmi = HdmiCec()
    spy = SpyModule("Components.HdmiCec")
    spy.HdmiCec = HdmiCec
    del MODULE_READS[:]
    monkeypatch.setitem(sys.modules, "Components.HdmiCec", spy)
    touched = []

    class Instance(HdmiCec):
        def __getattribute__(self, name):
            touched.append(name)
            return HdmiCec.__getattribute__(self, name)

        def __setattr__(self, name, value):
            touched.append(name)
            HdmiCec.__setattr__(self, name, value)

    hdmi.__class__ = Instance
    listeners = list(notificationAdded)

    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()

    assert list(notificationAdded) == listeners
    assert MODULE_READS == []
    assert touched == []
    assert bridge.publisher("cec_workaround") is None
    assert "cec_workaround" not in factory.client.last(INFO).json()["capabilities"]
    assert all(entry.text == "" for entry in factory.client.all_for(CEC))


def test_switched_off_a_television_standby_is_left_to_the_image(world):
    box = world(workaround=False)
    channel_list = box.show(ChannelSelection)

    entry = box.tv_standby()
    MainLoop.advance(cec.STALE_SECONDS * 1000 * 2)

    assert box.session.current_dialog is channel_list
    assert queued(entry)
    assert box.cec() is None


# ---------------------------------------------------------- capability and topic --


def test_the_capability_is_claimed_among_the_default_publishers(
    make_bridge, factory, settings, receiver
):
    HdmiCec()
    settings.cec_standby_workaround.value = True
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()

    capabilities = factory.client.last(INFO).json()["capabilities"]
    assert capabilities.index("cec_workaround") == capabilities.index("power") + 1
    assert factory.client.last(CEC).json() == {
        "last_intervention": None, "kind": None, "count": 0, "pending": False,
    }


def test_every_field_is_present_with_its_type(world):
    box = world(base=Screen)
    box.show(ChannelSelection)
    payload = box.cec()
    assert payload == {"last_intervention": None, "kind": None, "count": 0, "pending": False}

    box.tv_standby()
    MainLoop.advance(0)
    assert box.cec()["pending"] is True
    MainLoop.advance(cec.STALE_SECONDS * 1000)
    payload = box.cec()

    assert set(payload) == {"last_intervention", "kind", "count", "pending"}
    assert isinstance(payload["last_intervention"], int)
    assert payload["kind"] in ("closed_channel_list", "dropped_stale_standby")
    assert isinstance(payload["count"], int) and not isinstance(payload["count"], bool)
    assert isinstance(payload["pending"], bool)
    assert factory_entry_retained(box)


def factory_entry_retained(box):
    entry = box.factory.client.last(CEC)
    return entry.retain is True and entry.qos == 0


def test_no_capability_without_a_running_hdmi_cec(make_bridge, factory, settings, receiver):
    settings.cec_standby_workaround.value = True
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()

    assert HdmiCec.instance is None
    assert bridge.publisher("cec_workaround") is None
    assert notificationAdded == []


def _start_with_cec(make_bridge, factory, settings, receiver):
    settings.cec_standby_workaround.value = True
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    return bridge


@pytest.mark.parametrize("setting", ("enabled", "handle_tv_standby"))
def test_no_capability_when_the_image_will_not_follow_the_television(
    make_bridge, factory, settings, receiver, setting
):
    """The image builds `HdmiCec` with CEC switched off too; the singleton proves nothing.

    With HDMI-CEC off, or the receiver set not to follow the television into
    standby, the image never queues the television's standby, so a hook could
    never fire.
    """
    hdmi = HdmiCec()
    getattr(config.hdmicec, setting).value = False
    # The model agrees that nothing would ever be queued.
    hdmi.messageReceived(STANDBY_OPCODE)
    assert notifications == []

    bridge = _start_with_cec(make_bridge, factory, settings, receiver)

    assert HdmiCec.instance is hdmi
    assert bridge.publisher("cec_workaround") is None
    assert notificationAdded == []
    assert "cec_workaround" not in factory.client.last(INFO).json()["capabilities"]
    assert all(entry.text == "" for entry in factory.client.all_for(CEC))


@pytest.mark.parametrize("setting", ("enabled", "handle_tv_standby"))
def test_no_capability_on_an_image_without_the_cec_setting(
    make_bridge, factory, settings, receiver, monkeypatch, setting
):
    HdmiCec()
    monkeypatch.delattr(config.hdmicec, setting)

    bridge = _start_with_cec(make_bridge, factory, settings, receiver)

    assert bridge.publisher("cec_workaround") is None
    assert notificationAdded == []


@pytest.mark.parametrize("breakage", ("listeners", "queue", "standby"))
def test_no_capability_when_the_image_lacks_a_hook(
    make_bridge, factory, settings, receiver, monkeypatch, breakage
):
    HdmiCec()
    if breakage == "listeners":
        monkeypatch.setattr(notifications_module, "notificationAdded", ())
    elif breakage == "queue":
        monkeypatch.setattr(notifications_module, "notifications", None)
    else:
        monkeypatch.setattr(standby_module, "Standby", None)
    settings.cec_standby_workaround.value = True
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()

    assert bridge.publisher("cec_workaround") is None
    assert notificationAdded == []


def test_switching_it_off_retracts_the_topic_and_lets_go(world, factory, settings):
    box = world()
    box.show(ChannelSelection)
    box.tv_standby()
    MainLoop.advance(0)
    assert box.held is cec.HELD  # a hold is in progress
    publisher = box.publisher
    assert publisher._notification_added in notificationAdded

    settings.cec_standby_workaround.value = False
    box.bridge.reload()
    # The plugin re-registers nothing by hand; a bare reload has a fresh registry.
    factory.client.fire_connect()

    # 🔴 Given back at once, not at the deadline.
    assert box.held is False
    assert publisher._notification_added not in notificationAdded
    assert publisher._counter_moved not in config.misc.standbyCounter.notifiers
    assert all(not timer.running for timer in (
        publisher._act.timer, publisher._settle.timer, publisher._hold_deadline.timer,
    ) if timer is not None)
    assert factory.client.last(CEC).text == ""
    assert "cec_workaround" not in factory.client.last(INFO).json()["capabilities"]


def test_stopping_mid_wait_leaves_the_queued_standby_to_the_image(world):
    box = world()
    box.show(Menu)
    entry = box.tv_standby()
    MainLoop.advance(0)

    box.bridge.stop()
    MainLoop.advance(cec.STALE_SECONDS * 1000 * 2)

    assert queued(entry)


# ------------------------------------------------------------------ the setting --


def test_the_setting_is_box_only_and_not_echoed(connected_bridge, factory):
    assert "cec_standby_workaround" not in settings_module.REMOTE_SETTING_NAMES
    assert "cec_standby_workaround" not in settings_module.READ_ONLY_SETTING_NAMES
    assert "cec_standby_workaround" not in factory.client.last(INFO).json()["settings"]
    with pytest.raises(ValueError):
        settings_module.validate_remote_settings({
            "publish_keys": True, "screenshot": "off", "screenshot_interval": 60,
            "cec_standby_workaround": True,
        })


def test_the_setting_defaults_off(settings):
    assert settings.cec_standby_workaround.value is False


def test_the_topic_is_not_volatile():
    """`last_intervention` is when something happened, not when the payload was built."""
    assert CecPublisher.volatile == ()
