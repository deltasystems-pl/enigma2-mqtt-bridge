"""`timeshift`: on the image's events and the info bar's own calls, never on a timer.

The info bar here is OpenViX's as far as timeshift goes (`Components/Timeshift.py`):
`activatePermanentTimeshift`, `startTimeshift` and `stopTimeshift`, a `seekstate`
with its `SEEK_STATE_PAUSE`, the `onPlayStateChanged` list and `pts_starttime`. The
service's buffer answers `isTimeshiftEnabled` and `isTimeshiftActive`, and starts or
stops without telling anybody - which is the image's own behaviour.
"""

import sys
import types

import pytest
from conftest import InfoBar, MainLoop, eTimer, iPlayableService

from MQTTBridge import timeshift as timeshift_module

NODE = "vuuno4kse_005301"
TOPIC = "enigma2/" + NODE + "/timeshift"
INFO = "enigma2/" + NODE + "/info"
SEEKABLE = 50
USER = 60


class Buffer:
    def __init__(self):
        self.enabled = False
        self.active = False

    def isTimeshiftEnabled(self):
        return self.enabled

    def isTimeshiftActive(self):
        return self.active


class TimeshiftInfoBar(InfoBar):
    SEEK_STATE_PLAY = (0, 0, 0, ">")
    SEEK_STATE_PAUSE = (1, 0, 0, "||")

    def __init__(self, servicelist, buffer, clock=1792050000):
        InfoBar.__init__(self, servicelist)
        self.buffer = buffer
        self.seekstate = self.SEEK_STATE_PLAY
        self.onPlayStateChanged = []
        self.pts_starttime = 0
        self.clock = clock

    def activatePermanentTimeshift(self):
        self.buffer.enabled = True
        self.pts_starttime = self.clock

    def startTimeshift(self):
        self.activatePermanentTimeshift()

    def stopTimeshift(self):
        self.buffer.enabled = False
        self.buffer.active = False

    def setSeekState(self, state):
        self.seekstate = state
        for listener in self.onPlayStateChanged:
            listener(state)


@pytest.fixture
def image(monkeypatch):
    module = types.ModuleType("Components.Timeshift")
    module.InfoBarTimeshift = type("InfoBarTimeshift", (), {})
    monkeypatch.setitem(sys.modules, "Components.Timeshift", module)
    monkeypatch.setattr(iPlayableService, "evSeekableStatusChanged", SEEKABLE, raising=False)
    monkeypatch.setattr(iPlayableService, "evUser", USER, raising=False)


@pytest.fixture
def buffer(receiver):
    one = Buffer()
    receiver.service.timeshift = lambda: one
    return one


@pytest.fixture
def start(image, buffer, make_bridge, factory, settings, receiver):
    def run(infobar=True):
        settings.host.value = "10.0.0.5"
        settings.node_id.value = NODE
        if infobar:
            install_infobar(receiver, buffer)
        bridge = make_bridge(session=receiver.session)
        bridge.start()
        factory.client.fire_connect()
        return bridge

    return run


def install_infobar(receiver, buffer):
    servicelist = receiver.with_channel_list()
    InfoBar.instance = TimeshiftInfoBar(servicelist, buffer)
    receiver.session.current_dialog = InfoBar.instance
    return InfoBar.instance


def state(factory):
    return factory.client.last(TOPIC).json()


def test_an_image_without_timeshift_in_its_info_bar_has_no_topic(make_bridge, factory,
                                                                   settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert bridge.publisher("timeshift") is None
    assert factory.client.last(TOPIC) is None


def test_no_buffer_is_inactive_and_the_rest_null(start, factory):
    bridge = start()
    assert state(factory) == {"active": False, "paused": None, "behind": None, "since": None}
    assert factory.client.last(TOPIC).retain is True
    assert "timeshift" in bridge.capabilities()


def test_a_buffer_started_by_the_info_bar_is_seen_with_the_images_start(start, factory):
    start()
    InfoBar.instance.startTimeshift()
    assert state(factory) == {"active": True, "paused": False, "behind": False,
                              "since": 1792050000}


def test_the_permanent_timeshift_is_seen_and_so_is_its_end(start, factory):
    start()
    InfoBar.instance.activatePermanentTimeshift()
    assert state(factory)["active"] is True
    InfoBar.instance.stopTimeshift()
    assert state(factory) == {"active": False, "paused": None, "behind": None, "since": None}


def test_pause_and_playing_behind_live_come_from_the_images_events(start, factory, receiver,
                                                                    buffer):
    start()
    infobar = InfoBar.instance
    infobar.startTimeshift()
    infobar.setSeekState(infobar.SEEK_STATE_PAUSE)
    assert state(factory)["paused"] is True
    buffer.active = True
    receiver.nav.fire(SEEKABLE)
    assert state(factory)["behind"] is True
    infobar.setSeekState(infobar.SEEK_STATE_PLAY)
    assert state(factory) == {"active": True, "paused": False, "behind": True,
                              "since": 1792050000}


def test_a_buffer_the_service_starts_alone_is_seen_at_the_next_event(start, factory, receiver,
                                                                     buffer):
    start()
    buffer.enabled = True
    assert state(factory)["active"] is False
    receiver.nav.fire(USER + 1)
    assert state(factory)["active"] is True


def test_without_the_images_start_since_is_when_the_plugin_first_saw_it(start, factory,
                                                                        receiver, buffer,
                                                                        monkeypatch):
    start()
    InfoBar.instance.pts_starttime = None
    monkeypatch.setattr(timeshift_module.time, "time", lambda: 1792000123.7)
    buffer.enabled = True
    receiver.nav.fire(iPlayableService.evStart)
    assert state(factory)["since"] == 1792000123
    monkeypatch.setattr(timeshift_module.time, "time", lambda: 1792999999)
    receiver.nav.fire(SEEKABLE)
    assert state(factory)["since"] == 1792000123


def test_the_state_goes_out_only_when_it_changed(start, factory, receiver):
    start()
    factory.client.clear()
    for _ in range(5):
        receiver.nav.fire(iPlayableService.evStart)
        receiver.nav.fire(SEEKABLE)
    assert factory.client.all_for(TOPIC) == []


def test_an_event_this_area_does_not_follow_reads_nothing(start, factory, receiver,
                                                          monkeypatch):
    bridge = start()
    publisher = bridge.publisher("timeshift")
    read = []
    monkeypatch.setattr(publisher, "read", lambda: read.append(1) or {})
    receiver.nav.fire(iPlayableService.evUpdatedInfo)
    assert read == []


def test_nothing_is_read_on_a_timer(start, factory, receiver, monkeypatch):
    bridge = start()
    publisher = bridge.publisher("timeshift")
    read = []
    monkeypatch.setattr(publisher, "read", lambda: read.append(1) or {})
    MainLoop.advance(3600 * 1000, limit=100000)
    assert read == []
    assert publisher._bind.timer is None


def test_an_info_bar_built_later_is_found_and_announced(start, factory, receiver, buffer):
    bridge = start(infobar=False)
    assert "timeshift" not in bridge.capabilities()
    assert factory.client.last(TOPIC) is None
    install_infobar(receiver, buffer)
    bridge.publisher("timeshift")._bind.timer.fire()
    assert "timeshift" in factory.client.last(INFO).json()["capabilities"]
    assert state(factory)["active"] is False


def test_the_search_for_the_info_bar_slows_down_after_a_minute(start, plugin_log):
    bridge = start(infobar=False)
    publisher = bridge.publisher("timeshift")
    for _ in range(timeshift_module.BIND_ATTEMPTS):
        publisher._bind.timer.fire()
    assert publisher._bind.timer.started == (timeshift_module.SLOW_BIND_MILLISECONDS, False)
    assert "no info bar with timeshift" in plugin_log()


def test_stopping_puts_the_info_bars_own_methods_back(start, receiver):
    bridge = start()
    infobar = InfoBar.instance
    assert "startTimeshift" in vars(infobar)
    bridge.stop()
    for name in timeshift_module.WRAPPED:
        assert name not in vars(infobar), name
    assert infobar.onPlayStateChanged == []
    infobar.startTimeshift()
    assert infobar.buffer.enabled is True


def test_a_reload_wraps_the_methods_once(start, receiver, factory):
    bridge = start()
    bridge.reload()
    factory.client.fire_connect()
    infobar = InfoBar.instance
    wrapper = vars(infobar)["startTimeshift"]
    assert getattr(wrapper, timeshift_module.MARKER)
    assert len(infobar.onPlayStateChanged) == 1


def test_a_service_without_a_buffer_interface_is_inactive(start, factory, receiver):
    start()
    receiver.service.timeshift = lambda: None
    receiver.nav.fire(iPlayableService.evStart)
    assert state(factory)["active"] is False


def test_a_buffer_that_raises_is_read_as_none(start, factory, receiver, buffer):
    start()
    InfoBar.instance.startTimeshift()

    def broken():
        raise RuntimeError("no demux")

    buffer.isTimeshiftActive = broken
    receiver.nav.fire(SEEKABLE)
    assert state(factory)["active"] is True and state(factory)["behind"] is None


def test_no_unwanted_timer_is_left_running_after_a_bind(start):
    bridge = start()
    publisher = bridge.publisher("timeshift")
    owned = [timer for timer in eTimer.instances
             if any(getattr(function, "__self__", None) is publisher._bind
                    for function in timer.callback)]
    assert all(not timer.running for timer in owned)


def test_a_buffer_a_key_starts_or_stops_is_seen_after_the_key(start, factory, receiver, buffer):
    """The info bar's key map calls its own methods, never the wrappers."""
    start()
    buffer.enabled = True
    receiver.actions.keyPressed("dreambox remote control (native)", 119, 0)
    receiver.actions.keyPressed("dreambox remote control (native)", 119, 1)
    assert state(factory)["active"] is False
    MainLoop.advance(timeshift_module.KEY_SETTLE_MILLISECONDS)
    assert state(factory)["active"] is True
    buffer.enabled = False
    receiver.actions.keyPressed("dreambox remote control (native)", 128, 1)
    MainLoop.advance(timeshift_module.KEY_SETTLE_MILLISECONDS)
    assert state(factory)["active"] is False


def test_the_key_listener_never_takes_a_key_and_goes_with_the_publisher(start, receiver):
    bridge = start()
    publisher = bridge.publisher("timeshift")
    assert publisher._on_key(119, 1) == 0
    assert any(entry[2] is publisher._key_handler for entry in receiver.actions.bound)
    bridge.stop()
    assert not any(entry[2] is publisher._key_handler for entry in receiver.actions.bound)
