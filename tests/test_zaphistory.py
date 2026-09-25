"""The receiver's own zap history: published as it is, zapped into, and cleared as 0 clears it.

The stubs in `conftest.py` follow the receiver's bytecode: the channel list's
`history` (entries of path plus service, oldest first), `historyMenuClosed`,
`setHistoryPath` and `addToHistory`, and the info bar's `keyNumberGlobal(0)`
whose panic branch **replaces** the list with a new one and zaps to channel 1.
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
    KeyActionMap,
    MainLoop,
    MoviePlayer,
    ServiceCenter,
    ServiceReference,
    eServiceReference,
)

from MQTTBridge import discovery, zaphistory

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
TOPIC = ROOT + "/zap_history"
INFO = ROOT + "/info"
LAST_ERROR = ROOT + "/last_error"
RADIO = "1:0:2:1B1D:802:2:11A0000:0:0:0:"


@pytest.fixture
def usage(monkeypatch):
    """`config.usage` as the image builds it; absent from the shared stub on purpose."""
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
    """A receiver whose history holds TVP1, TVN and POLSAT, POLSAT newest and playing."""
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
    box.publisher = bridge.publisher("zap_history")
    box.factory = factory
    return box


def payload(factory):
    return factory.client.last(TOPIC).json()


def send(factory, name, body):
    factory.client.fire_message(ROOT + "/cmd/" + name, body)


def last_error(factory):
    message = factory.client.last(LAST_ERROR)
    return None if message is None or message.text == "" else message.json()


def tick(box):
    box.publisher._ticker.timer.fire()


# --------------------------------------------------------------------- topic --


def test_the_payload_is_newest_first_with_paths_and_names(box):
    published = payload(box.factory)
    assert [item["sref"] for item in published["entries"]] == [POLSAT, TVN, TVP1]
    assert published["entries"][0] == {
        "sref": POLSAT,
        "name": "Polsat Sport",
        "bouquet": SECOND_BOUQUET,
        "bouquet_name": "Sport (HD)",
    }
    assert published["entries"][2]["bouquet_name"] == "Ulubione TV"
    assert published["current"] == 0
    assert published["limit"] == 20
    assert published["panic_button"] is True


def test_the_topic_is_retained(box):
    assert box.factory.client.last(TOPIC).retain is True


def test_current_follows_history_pos_into_the_newest_first_order(box):
    box.list.history_pos = 0
    tick(box)
    assert payload(box.factory)["current"] == 2


def test_an_entry_the_receivers_screen_would_skip_is_not_published(box, monkeypatch):
    """The History Zap screen skips an entry `eServiceCenter` has no information for."""
    monkeypatch.setattr(
        ServiceCenter, "info",
        lambda self, reference: None if reference.toString() == TVN else object(),
    )
    box.list.history_pos = 1
    box.list.history.append(entry(RADIO, "1:7:2:0:0:0:0:0:0:0:FROM BOUQUET \"r.radio\""))
    tick(box)
    published = payload(box.factory)
    assert [item["sref"] for item in published["entries"]] == [RADIO, POLSAT, TVP1]
    # The current entry was the one omitted.
    assert published["current"] is None


def test_an_entry_without_a_path_or_in_an_unpublished_bouquet_has_null_bouquet_fields(box):
    box.list.history.append([eServiceReference(RADIO)])
    box.list.history.append(entry(TVN, "1:7:2:0:0:0:0:0:0:0:FROM BOUQUET \"r.radio\""))
    tick(box)
    newest, second = payload(box.factory)["entries"][:2]
    assert newest["bouquet"].endswith('"r.radio"')
    assert newest["bouquet_name"] is None
    assert second["bouquet"] is None and second["bouquet_name"] is None


def test_an_empty_history_is_an_empty_list(box):
    box.list.history = []
    box.list.history_pos = 0
    tick(box)
    assert payload(box.factory) == {
        "entries": [], "current": None, "limit": 20, "panic_button": True,
    }


def test_a_replaced_list_is_read_on_the_next_tick(box):
    """🔴 The panic branch replaces the list object; a held reference would show it for ever."""
    box.list.history = [entry(TVN)]
    box.list.history_pos = 0
    tick(box)
    assert [item["sref"] for item in payload(box.factory)["entries"]] == [TVN]


def test_an_unchanged_history_publishes_nothing_and_resolves_no_names(box, monkeypatch):
    calls = []
    monkeypatch.setattr(zaphistory, "service_name", lambda sref: calls.append(sref) or "x")
    box.factory.client.clear()
    tick(box)
    tick(box)
    assert box.factory.client.all_for(TOPIC) == []
    assert calls == []


def test_a_change_publishes_once(box):
    box.factory.client.clear()
    box.list.history.append(entry(TVP1))
    del box.list.history[0]
    box.list.history_pos = 2
    tick(box)
    tick(box)
    assert len(box.factory.client.all_for(TOPIC)) == 1


def test_the_panic_setting_is_part_of_the_payload(box, usage):
    usage.panicbutton.value = False
    tick(box)
    assert payload(box.factory)["panic_button"] is False


def test_the_limit_is_null_on_an_image_without_historysize(box, monkeypatch):
    import Screens.ChannelSelection as module

    monkeypatch.delattr(module, "HISTORYSIZE")
    box.list.history.pop()
    tick(box)
    assert payload(box.factory)["limit"] is None


# -------------------------------------------------------------- capabilities --


def test_both_capabilities_are_claimed_on_a_readable_list(box):
    capabilities = box.factory.client.last(INFO).json()["capabilities"]
    assert "zap_history" in capabilities
    assert "history_clear" in capabilities


def test_nothing_is_claimed_before_the_list_can_be_read(make_bridge, factory, settings,
                                                      receiver, usage):
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert "zap_history" not in factory.client.last(INFO).json()["capabilities"]
    assert factory.client.last(TOPIC) is None

    receiver.with_channel_list([TVP1, TVN])
    bridge.publisher("zap_history")._ticker.timer.fire()
    assert "zap_history" in factory.client.last(INFO).json()["capabilities"]
    assert factory.client.last(TOPIC).json()["entries"] == []


def test_an_image_that_never_offers_a_list_slows_down_but_keeps_watching(
    make_bridge, factory, settings, receiver, usage
):
    settings.host.value = "192.0.2.2"
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    publisher = bridge.publisher("zap_history")
    for _ in range(zaphistory.BIND_ATTEMPTS):
        publisher._ticker.timer.fire()
    assert publisher._ticker.timer.started[0] == zaphistory.SLOW_POLL_MILLISECONDS
    receiver.with_channel_list([TVP1])
    publisher._ticker.timer.fire()
    assert publisher.claimed()
    assert publisher._ticker.timer.started[0] == zaphistory.POLL_MILLISECONDS


@pytest.mark.parametrize("name", ["keyNumberGlobal", "recallPrevService", "checkTimeshiftRunning"])
def test_history_clear_is_not_claimed_without_the_keys_own_path(
    make_bridge, factory, settings, receiver, usage, monkeypatch, name
):
    settings.host.value = "192.0.2.2"
    receiver.with_channel_list([TVP1])
    monkeypatch.setattr(InfoBar, name, None)
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    capabilities = bridge.capabilities()
    assert "zap_history" in capabilities
    assert "history_clear" not in capabilities


def test_history_clear_is_not_claimed_without_the_panic_setting(
    make_bridge, factory, settings, receiver, usage
):
    del config.usage.panicbutton
    settings.host.value = "192.0.2.2"
    receiver.with_channel_list([TVP1])
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    assert "history_clear" not in bridge.capabilities()
    assert "zap_history" in bridge.capabilities()


def test_zap_history_is_not_claimed_without_the_history_screens_calls(
    make_bridge, factory, settings, receiver, usage, monkeypatch
):
    from conftest import ChannelList

    settings.host.value = "192.0.2.2"
    receiver.with_channel_list([TVP1])
    monkeypatch.setattr(ChannelList, "historyMenuClosed", None)
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    assert "zap_history" not in bridge.capabilities()


# -------------------------------------------------------------- cmd/zap_history --


@pytest.mark.parametrize(
    "body, sentence",
    [
        (b"not json", "cmd/zap_history takes a JSON object"),
        (b'{"sref": "' + TVN.encode() + b'", "name": "TVN"}',
         "cmd/zap_history takes exactly one sref field"),
        (b"{}", "cmd/zap_history takes exactly one sref field"),
        (b'{"sref": ""}', "cmd/zap_history sref must be a non-empty string"),
        (b'{"sref": 7}', "cmd/zap_history sref must be a non-empty string"),
    ],
)
def test_cmd_zap_history_takes_exactly_one_reference(box, body, sentence):
    send(box.factory, "zap_history", body)
    assert last_error(box.factory)["error"] == sentence
    assert box.receiver.nav.played == []


@pytest.mark.parametrize(
    "spelling",
    [TVN, TVN.lower(), TVN + ":TVN HD", TVN.rstrip(":")],
)
def test_cmd_zap_history_matches_the_entry_by_identity(box, spelling):
    send(box.factory, "zap_history", json.dumps({"sref": spelling}).encode())
    assert box.receiver.nav.sref == TVN
    assert last_error(box.factory) is None
    assert payload(box.factory)["entries"][0]["sref"] == TVN


def test_cmd_zap_history_calls_the_screens_own_callback_with_the_entrys_object(
    box, monkeypatch
):
    target = box.list.history[1][-1]
    called = []
    original = type(box.list).historyMenuClosed
    monkeypatch.setattr(
        type(box.list), "historyMenuClosed",
        lambda self, ref: called.append(ref) or original(self, ref),
    )
    send(box.factory, "zap_history", json.dumps({"sref": TVN}).encode())
    assert len(called) == 1 and called[0] is target
    # The entry moved to the front, as the receiver's own screen moves it.
    assert box.list.history[-1][-1] is target
    assert box.list.history_pos == 2
    assert box.receiver.nav.played == [TVN]


def test_cmd_zap_history_is_verified_on_service(box):
    send(box.factory, "zap_history", json.dumps({"sref": TVN}).encode())
    assert box.bridge.publisher("service")._waiting_for == TVN


def test_the_current_entry_that_is_not_playing_is_played_by_its_path(box):
    """`historyMenuClosed` does nothing for the entry at `history_pos`."""
    box.receiver.nav.sref = TVP1  # zapped by something the history does not know
    send(box.factory, "zap_history", json.dumps({"sref": POLSAT}).encode())
    assert box.list.history_paths == 1
    assert box.receiver.nav.sref == POLSAT
    assert box.list.getRoot().toString() == SECOND_BOUQUET


def test_an_unknown_reference_is_refused(box):
    send(box.factory, "zap_history", json.dumps({"sref": RADIO}).encode())
    assert last_error(box.factory)["error"] == zaphistory.GONE
    assert box.receiver.nav.played == []


def test_an_entry_the_screen_would_skip_cannot_be_zapped_to(box, monkeypatch):
    monkeypatch.setattr(
        ServiceCenter, "info",
        lambda self, reference: None if reference.toString() == TVN else object(),
    )
    send(box.factory, "zap_history", json.dumps({"sref": TVN}).encode())
    assert last_error(box.factory)["error"] == zaphistory.GONE


def test_cmd_zap_history_from_standby_waits_for_the_restore(box):
    screen = box.receiver.enter_standby(restoring=True)
    send(box.factory, "zap_history", json.dumps({"sref": TVN}).encode())
    assert screen.power_calls == 1
    MainLoop.advance(0)
    assert box.receiver.nav.played == []

    screen.finish_close()
    assert box.receiver.nav.played == [POLSAT]     # the restore
    MainLoop.advance(0)
    assert box.receiver.nav.played == [POLSAT, TVN]
    assert box.list.history[-1][-1].toString() == TVN


def test_a_channel_not_in_the_history_is_refused_without_waking_the_receiver(box):
    screen = box.receiver.enter_standby(restoring=True)
    send(box.factory, "zap_history", json.dumps({"sref": RADIO}).encode())
    assert last_error(box.factory)["error"] == zaphistory.GONE
    assert screen.power_calls == 0


def test_cmd_zap_history_with_a_screen_open_is_played_directly(box):
    """The history's own calls would move the channel list under an open screen."""
    before = [item[-1].toString() for item in box.list.history]
    box.receiver.session.current_dialog = box.list
    send(box.factory, "zap_history", json.dumps({"sref": TVN}).encode())
    assert last_error(box.factory) is None
    assert box.receiver.nav.played == [TVN]
    assert box.list.history_paths == 0
    assert [item[-1].toString() for item in box.list.history] == before


@pytest.mark.parametrize("nav", [None, object()], ids=["no navigation", "no playService"])
def test_cmd_zap_history_with_a_screen_open_and_no_player_says_why(box, nav):
    """The direct play needs `session.nav`; without it, the sentence `cmd/zap` gives."""
    from MQTTBridge import service

    before = [item[-1].toString() for item in box.list.history]
    box.receiver.session.current_dialog = box.list
    box.receiver.session.nav = nav
    send(box.factory, "zap_history", json.dumps({"sref": TVN}).encode())
    expected = service.NO_SESSION if nav is None else service.NO_PLAYER
    assert last_error(box.factory)["error"] == expected
    assert box.receiver.nav.played == []
    assert box.list.history_paths == 0
    assert [item[-1].toString() for item in box.list.history] == before


@pytest.mark.parametrize("nav", [None, object()], ids=["no navigation", "no playService"])
def test_zap_and_zap_history_say_the_same_about_a_missing_player(box, nav):
    """One missing piece, one sentence: `last_error` must not depend on which command found it."""
    box.receiver.session.current_dialog = box.list
    box.receiver.session.nav = nav
    send(box.factory, "zap", json.dumps({"sref": TVN}).encode())
    by_zap = last_error(box.factory)
    send(box.factory, "zap_history", json.dumps({"sref": TVN}).encode())
    by_zap_history = last_error(box.factory)
    assert by_zap is not None and by_zap_history is not None
    assert by_zap_history["error"] == by_zap["error"]


def test_cmd_zap_history_cancels_a_zap_waiting_for_the_wake(box):
    from MQTTBridge import service

    screen = box.receiver.enter_standby(restoring=True)
    service.zap(box.receiver.session, TVP1, channels=box.bridge.publisher("channels"))
    screen.finish_close()
    send(box.factory, "zap_history", json.dumps({"sref": TVN}).encode())
    MainLoop.advance(0)
    assert box.receiver.nav.sref == TVN


def test_a_timeshift_check_that_raises_refuses_the_clear(box):
    """Fail-safe: 0 could open the timeshift question, so the clear is refused."""
    def broken():
        raise RuntimeError("no seek interface")

    InfoBar.instance.isSeekable = broken
    send(box.factory, "history_clear", b"PRESS")
    assert last_error(box.factory)["reason"] == "timeshift"
    assert InfoBar.instance.keys == []


def test_cmd_zap_history_waiting_for_the_wake_is_dropped_by_an_uninstall(box):
    screen = box.receiver.enter_standby(restoring=True)
    send(box.factory, "zap_history", json.dumps({"sref": TVN}).encode())
    box.bridge.uninstaller.phase = "scheduled"
    screen.finish_close()
    MainLoop.advance(0)
    assert box.receiver.nav.played == [POLSAT]


def test_cmd_zap_history_is_refused_while_a_recording_plays_back(box):
    box.receiver.session.current_dialog = MoviePlayer(box.receiver.session)
    send(box.factory, "zap_history", json.dumps({"sref": TVN}).encode())
    error = last_error(box.factory)
    assert error["error"] == "a recording is being played back"
    assert error["reason"] == "playback"
    assert box.receiver.nav.played == []


def test_cmd_zap_history_without_the_capability_is_refused(
    make_bridge, factory, settings, receiver, usage
):
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    send(factory, "zap_history", json.dumps({"sref": TVN}).encode())
    assert last_error(factory)["error"] == zaphistory.NOT_AVAILABLE


def test_a_zap_history_command_left_retained_is_discarded(box):
    box.factory.client.fire_message(
        ROOT + "/cmd/zap_history", json.dumps({"sref": TVN}).encode(), retain=True
    )
    assert box.receiver.nav.played == []


# ------------------------------------------------------------ cmd/history_clear --


def _every_refusal_armed(box, usage):
    """Every condition under which 0 would not clear, at once."""
    box.receiver.enter_standby()
    usage.panicbutton.value = False
    box.list.history = [entry(POLSAT)]
    InfoBar.instance.seekable = True
    InfoBar.instance.timeshift = True
    InfoBar.instance.pts_blockZap_timer.active = True
    InfoBar.instance.pip_zero = True
    box.receiver.session.current_dialog = MoviePlayer(box.receiver.session)


def _disarm(box, usage, reason):
    from Screens import Standby

    if reason == "standby":
        Standby.inStandby = None
    elif reason == "panic_off":
        usage.panicbutton.value = True
    elif reason == "too_short":
        box.list.history = [entry(TVP1), entry(TVN), entry(POLSAT, SECOND_BOUQUET)]
        box.list.history_pos = 2
    elif reason == "timeshift":
        InfoBar.instance.seekable = False
    elif reason == "zap_blocked":
        InfoBar.instance.pts_blockZap_timer.active = False
    elif reason == "pip":
        InfoBar.instance.pip_zero = False
    elif reason == "playback":
        # Another screen over the info bar: the channel list the remote opened.
        box.receiver.session.current_dialog = box.list
    elif reason == "screen_open":
        box.receiver.session.current_dialog = InfoBar.instance


ORDER = ("standby", "panic_off", "too_short", "timeshift", "zap_blocked", "pip", "playback",
         "screen_open")
SENTENCES = {
    "standby": "the receiver is in standby, where 0 does not clear the history",
    "panic_off": "the receiver's panic-button setting is off, so 0 goes back one channel "
                 "instead of clearing the history",
    "too_short": "the receiver's zap history holds at most one channel, and 0 does nothing then",
    "timeshift": "timeshift is active; 0 would ask on screen whether to leave it",
    "zap_blocked": "the receiver is holding zaps for a moment after timeshift; try again",
    "pip": "picture-in-picture is showing and 0 is set to act on it",
    "playback": "a recording is being played back",
    "screen_open": "a screen is open on the receiver, and 0 does not reach the zap history there",
}


def test_every_refusal_comes_in_order_with_its_sentence_and_reason(box, usage):
    _every_refusal_armed(box, usage)
    for reason in ORDER:
        send(box.factory, "history_clear", b"PRESS")
        error = last_error(box.factory)
        assert error == {
            "cmd": "history_clear", "error": SENTENCES[reason], "reason": reason,
            "ts": error["ts"],
        }
        assert InfoBar.instance.keys == []
        _disarm(box, usage, reason)
    send(box.factory, "history_clear", b"PRESS")
    assert last_error(box.factory) is None
    assert InfoBar.instance.keys == [0]


def test_a_timeshift_waiting_to_be_saved_refuses_the_clear(box):
    InfoBar.instance.save_current_timeshift = True
    send(box.factory, "history_clear", b"PRESS")
    assert last_error(box.factory)["reason"] == "timeshift"
    assert InfoBar.instance.questions == []


def test_the_clear_runs_the_zero_key_once_and_collapses_the_history(box):
    KeyActionMap.getInstance().pressed.clear()
    box.factory.client.clear()
    send(box.factory, "history_clear", b"PRESS")

    assert InfoBar.instance.keys == [0]
    assert KeyActionMap.getInstance().pressed == []
    assert last_error(box.factory) is None
    assert [item["sref"] for item in payload(box.factory)["entries"]] == [TVP1]
    assert payload(box.factory)["current"] == 0
    assert box.receiver.nav.sref == TVP1
    assert len(box.factory.client.all_for(TOPIC)) == 1


def test_a_clear_that_leaves_more_than_one_entry_says_so(box):
    InfoBar.instance.panic_keeps = [entry(TVN)]
    send(box.factory, "history_clear", b"PRESS")
    error = last_error(box.factory)
    assert error["error"] == "the receiver did not clear its zap history"
    assert error["reason"] == "not_cleared"


def test_the_clear_ignores_its_payload(box):
    send(box.factory, "history_clear", b'{"anything": ["at", "all"]}')
    assert InfoBar.instance.keys == [0]


def test_the_clear_without_the_capability_is_refused(
    make_bridge, factory, settings, receiver, usage, monkeypatch
):
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    receiver.with_channel_list([TVP1])
    monkeypatch.setattr(InfoBar, "keyNumberGlobal", None)
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    send(factory, "history_clear", b"PRESS")
    error = last_error(factory)
    assert error["error"] == zaphistory.CLEAR_NOT_AVAILABLE
    assert "reason" not in error


# -------------------------------------------------------------- last_error --


def test_last_error_without_a_reason_is_what_it_always_was(box):
    """Only handlers that define codes set `reason`; every other refusal is unchanged."""
    send(box.factory, "zap", b'{"name": "Nowhere"}')
    assert set(last_error(box.factory)) == {"cmd", "error", "ts"}
    send(box.factory, "bouquet", b'{"sref": "x"}')
    assert set(last_error(box.factory)) == {"cmd", "error", "ts"}
    send(box.factory, "volume", b"loud")
    assert set(last_error(box.factory)) == {"cmd", "error", "ts"}


def test_the_reason_is_cleared_with_the_refusal(box):
    box.list.history = [entry(POLSAT)]
    send(box.factory, "history_clear", b"PRESS")
    assert last_error(box.factory)["reason"] == "too_short"
    send(box.factory, "zap_history", json.dumps({"sref": POLSAT}).encode())
    assert box.factory.client.last(LAST_ERROR).text == ""


# ----------------------------------------------------------------- discovery --


def _components(capabilities):
    info = {"capabilities": capabilities}
    topics = discovery.build_discovery_components(NODE, "Receiver", "enigma2", info)
    return topics[discovery.device_topic("homeassistant", NODE)]["cmps"]


def test_discovery_announces_the_clear_button_with_its_capability():
    components = _components(["zap_history", "history_clear"])
    button = components["history_clear"]
    assert button["p"] == "button"
    assert button["cmd_t"] == ROOT + "/cmd/history_clear"
    assert button["name"] == "Clear zap history"


def test_discovery_announces_no_history_select():
    """A core MQTT select carries its options in the discovery payload; they change every zap."""
    components = _components(["zap_history", "history_clear"])
    assert not any(
        "zap_history" in json.dumps(component) and component.get("p") == "select"
        for component in components.values()
    )


def test_discovery_has_no_clear_button_without_the_capability():
    assert "history_clear" not in _components(["zap_history"])


def test_the_history_is_named_the_way_the_service_topic_names_it(box):
    ServiceReference.names[TVN] = "TVN HD (renamed)"
    box.list.history.append(entry(TVN))
    del box.list.history[1]
    tick(box)
    assert payload(box.factory)["entries"][0]["name"] == "TVN HD (renamed)"
