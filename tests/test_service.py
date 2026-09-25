"""What is playing, what is on, and how well it is arriving."""

from conftest import (
    POLSAT,
    SECOND_BOUQUET,
    TVN,
    TVP1,
    Event,
    Frontend,
    InfoBar,
    MainLoop,
    Service,
    ServiceInfo,
    eServiceReference,
)

from MQTTBridge import service as service_module

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
SERVICE = ROOT + "/service"
EPG = ROOT + "/epg"
TUNER = ROOT + "/tuner"
LAST_ERROR = ROOT + "/last_error"


# ------------------------------------------------------------------- service --


def test_the_service_payload_has_every_documented_field(live_bridge, factory):
    payload = factory.client.last(SERVICE).json()
    assert set(payload) == {"sref", "name", "bouquet", "provider", "width", "height"}
    assert payload["sref"] == TVP1
    assert payload["name"] == "TVP 1 HD"
    assert payload["provider"] == "Cyfrowy Polsat"
    assert payload["width"] == 1920
    assert payload["height"] == 1080


def test_the_bouquet_is_the_one_the_service_is_in(live_bridge, factory):
    assert factory.client.last(SERVICE).json()["bouquet"] == "Ulubione TV"


def test_a_service_in_no_configured_bouquet_has_a_null_bouquet(live_bridge, factory, receiver):
    receiver.nav.sref = "1:0:19:9999:3FB:1:C00000:0:0:0:"
    factory.client.clear()
    receiver.nav.fire(1)  # evStart
    assert factory.client.last(SERVICE).json()["bouquet"] is None


def test_the_resolution_is_null_until_a_frame_has_been_decoded(live_bridge, factory, receiver):
    """enigma2 answers -1 between the zap and the first frame; -1 is not a size."""
    receiver.info.width = -1
    receiver.info.height = -1
    factory.client.clear()
    receiver.nav.fire(1)
    payload = factory.client.last(SERVICE).json()
    assert payload["width"] is None
    assert payload["height"] is None


def test_the_resolution_is_published_again_when_it_arrives(live_bridge, factory, receiver):
    receiver.info.width = -1
    receiver.info.height = -1
    receiver.nav.fire(1)
    factory.client.clear()
    receiver.info.width = 1280
    receiver.info.height = 720
    receiver.nav.fire(5)  # evUpdatedInfo
    assert factory.client.last(SERVICE).json()["width"] == 1280


def test_a_zap_publishes_the_new_service(live_bridge, factory, receiver):
    factory.client.clear()
    receiver.nav.sref = TVN
    receiver.info.name = "TVN HD"
    receiver.nav.fire(1)
    assert factory.client.last(SERVICE).json()["name"] == "TVN HD"


def test_an_event_this_publisher_does_not_care_about_publishes_nothing(live_bridge, factory,
                                                                      receiver):
    factory.client.clear()
    receiver.nav.fire(6)  # evUpdatedEventInfo - the EPG publisher's business
    assert factory.client.all_for(SERVICE) == []


def test_nothing_playing_publishes_an_empty_shape(live_bridge, factory, receiver):
    receiver.nav.sref = ""
    receiver.nav.service = None
    factory.client.clear()
    receiver.nav.fire(2)  # evEnd
    payload = factory.client.last(SERVICE).json()
    assert payload == {"sref": None, "name": None, "bouquet": None, "provider": None,
                       "width": None, "height": None}


def test_the_service_name_falls_back_to_the_stream_info(live_bridge, factory, receiver):
    from ServiceReference import ServiceReference

    ServiceReference.names = {}
    receiver.info.name = "A name only the stream knows"
    factory.client.clear()
    receiver.nav.fire(1)
    assert factory.client.last(SERVICE).json()["name"] == "A name only the stream knows"


def test_the_publisher_detaches_when_it_stops(live_bridge, receiver):
    publisher = live_bridge.publisher("service")
    before = len(receiver.nav.event)
    publisher.stop()
    assert len(receiver.nav.event) == before - 1


# ----------------------------------------------------------------------- epg --


def test_the_epg_payload_carries_now_and_next(live_bridge, factory):
    payload = factory.client.last(EPG).json()
    assert set(payload) == {"now", "next"}
    assert payload["now"]["title"] == "Wiadomości"
    assert payload["now"]["begin"] == 1789459200
    assert payload["now"]["end"] == 1789459200 + 1500
    assert payload["now"]["event_id"] == 27431
    assert payload["now"]["short"] == "Serwis informacyjny"
    assert payload["next"]["title"] == "Pogoda"


def test_a_service_with_no_epg_publishes_nulls(live_bridge, factory, receiver):
    receiver.info.events = []
    factory.client.clear()
    receiver.nav.fire(6)
    assert factory.client.last(EPG).json() == {"now": None, "next": None}


def test_an_image_that_raises_on_getevent_costs_nothing(live_bridge, factory, receiver):
    receiver.info.raises_on_event = True
    factory.client.clear()
    receiver.nav.fire(6)
    assert factory.client.last(EPG).json() == {"now": None, "next": None}


def test_the_programme_is_re_read_when_it_should_have_ended(live_bridge, factory, receiver):
    """Nothing happens on the box when the news ends, so a timer has to."""
    publisher = live_bridge.publisher("epg")
    receiver.info.events = [Event(99, 1789461000, 600, "Film")]
    factory.client.clear()
    publisher._boundary.timer.fire()
    assert factory.client.last(EPG).json()["now"]["title"] == "Film"


def test_the_boundary_timer_is_armed_for_the_end_of_the_programme(live_bridge, receiver,
                                                                  monkeypatch):
    publisher = live_bridge.publisher("epg")
    monkeypatch.setattr(service_module.time, "time", lambda: 1789460000)
    receiver.nav.fire(6)
    interval, single = publisher._boundary.timer.started
    # 1789459200 + 1500 = 1789460700, seven hundred seconds away, plus a second.
    assert interval == 701 * 1000
    assert single is True


def test_the_boundary_timer_never_sleeps_longer_than_a_quarter_of_an_hour(live_bridge, receiver,
                                                                          monkeypatch):
    publisher = live_bridge.publisher("epg")
    receiver.info.events = [Event(99, 1789459200, 86400, "A very long film")]
    monkeypatch.setattr(service_module.time, "time", lambda: 1789459300)
    receiver.nav.fire(6)
    assert publisher._boundary.timer.started[0] == service_module.EPG_MAX_SLEEP_MILLISECONDS


def test_the_boundary_timer_never_sleeps_less_than_five_seconds(live_bridge, receiver,
                                                                monkeypatch):
    publisher = live_bridge.publisher("epg")
    monkeypatch.setattr(service_module.time, "time", lambda: 1789460700)
    receiver.nav.fire(6)
    assert publisher._boundary.timer.started[0] == service_module.EPG_MIN_SLEEP_MILLISECONDS


# --------------------------------------------------------------------- tuner --


def test_the_tuner_payload_is_percentages_and_a_letter(live_bridge, factory):
    payload = factory.client.last(TUNER).json()
    assert set(payload) == {"snr", "agc", "ber", "tuner"}
    assert payload["snr"] == 80  # 52428 of 65535
    assert payload["agc"] == 61
    assert payload["ber"] == 0
    assert payload["tuner"] == "A"


def test_the_bit_error_rate_is_a_count_not_a_percentage(live_bridge, factory, receiver):
    """Scaling an error count by 65535 produces a number that means nothing."""
    receiver.frontend.ber = 4211
    factory.client.clear()
    receiver.nav.fire(3)  # evTunedIn
    assert factory.client.last(TUNER).json()["ber"] == 4211


def test_the_tuner_letter_counts_from_a(live_bridge, factory, receiver):
    receiver.frontend.tuner_number = 2
    factory.client.clear()
    receiver.nav.fire(3)
    assert factory.client.last(TUNER).json()["tuner"] == "C"


def test_a_stream_has_no_tuner(live_bridge, factory, receiver):
    """`frontendInfo()` answers None for IPTV and for playing a recording."""
    receiver.nav.service = Service(receiver.info, None)
    factory.client.clear()
    receiver.nav.fire(3)
    assert factory.client.last(TUNER).json() == {"snr": None, "agc": None, "ber": None,
                                                 "tuner": None}


def test_an_image_without_the_status_dictionary_uses_the_enum(live_bridge, factory, receiver):
    frontend = Frontend(quality=65535, power=32768, ber=7, status=False)
    receiver.nav.service = Service(receiver.info, frontend)
    factory.client.clear()
    receiver.nav.fire(3)
    payload = factory.client.last(TUNER).json()
    assert payload["snr"] == 100
    assert payload["agc"] == 50
    assert payload["ber"] == 7


def test_the_tuner_is_re_read_on_a_slow_tick(live_bridge, factory, receiver):
    publisher = live_bridge.publisher("tuner")
    receiver.frontend.quality = 0
    factory.client.clear()
    publisher._ticker.timer.fire()
    assert factory.client.last(TUNER).json()["snr"] == 0


def test_nothing_playing_publishes_the_empty_tuner_shape(live_bridge, factory, receiver):
    receiver.nav.service = None
    factory.client.clear()
    receiver.nav.fire(4)  # evTuneFailed
    assert factory.client.last(TUNER).json()["tuner"] is None


# ----------------------------------------------------------------------- zap --


def _zap(bridge, receiver, sref, **options):
    """`service.zap` as `cmd/zap` calls it: with the channel cache to choose a bouquet."""
    return service_module.zap(
        receiver.session, sref, channels=bridge.publisher("channels"), **options
    )


def _front(channel_list):
    """The newest history entry's service, as a string."""
    return channel_list.history[-1][-1].toString() if channel_list.history else None


def test_a_zap_plays_the_service(live_bridge, receiver):
    """No channel list at all: there is nothing to record through."""
    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.played == [TVN]


def test_a_zap_wakes_the_box_first(live_bridge, receiver):
    """Changed in 0.3.0: the zap waits for the turn after the wake, not the same call."""
    screen = receiver.enter_standby()
    assert _zap(live_bridge, receiver, TVN) is None
    assert screen.power_calls == 1
    assert receiver.nav.played == []
    MainLoop.advance(0)
    assert receiver.nav.played == [TVN]


def test_a_zap_uses_the_channel_lists_own_number_zap(live_bridge, receiver):
    """The remote's path, `selectAndStartService`, so the zap is in the history."""
    channel_list = receiver.with_channel_list([TVP1, TVN])
    assert _zap(live_bridge, receiver, TVN) is None
    assert InfoBar.instance.started == [(eServiceReference(TVN), channel_list.root)]
    assert channel_list.zaps == 1
    assert channel_list.corrected == 1
    assert _front(channel_list) == TVN
    # And it did not also play it a second way.
    assert receiver.nav.played == []


def test_a_zap_in_the_browsed_bouquet_does_not_move_the_channel_list(live_bridge, receiver):
    """Rule 1: the bouquet on screen wins over the first bouquet that holds the service."""
    receiver.service_center.contents[SECOND_BOUQUET].append((TVN, "TVN HD"))
    live_bridge.publisher("channels").refresh()
    channel_list = receiver.with_channel_list([POLSAT])
    channel_list.bouquets[SECOND_BOUQUET] = [POLSAT, TVN]
    channel_list.enterPath(eServiceReference(SECOND_BOUQUET))
    path_before = [reference.toString() for reference in channel_list.path]

    assert _zap(live_bridge, receiver, TVN) is None

    service, bouquet = InfoBar.instance.started[0]
    assert bouquet is channel_list.root
    assert bouquet.toString() == SECOND_BOUQUET
    assert [reference.toString() for reference in channel_list.path] == path_before
    assert _front(channel_list) == TVN


def test_a_zap_outside_the_browsed_bouquet_moves_the_channel_list_to_its_bouquet(
    live_bridge, receiver
):
    """Rule 2, and changed in 0.3.0: this zap used to be a bare `playService`.

    POLSAT is not in the bouquet being browsed, so the zap goes through the
    first published bouquet that holds it - and the channel list follows it
    there, exactly as a number zap on the remote does.
    """
    channel_list = receiver.with_channel_list([TVP1])
    assert _zap(live_bridge, receiver, POLSAT) is None
    assert receiver.nav.played == []
    assert channel_list.zaps == 1
    assert channel_list.getRoot().toString() == SECOND_BOUQUET
    assert channel_list.history[-1][-2].toString() == SECOND_BOUQUET
    assert _front(channel_list) == POLSAT


def test_a_zap_is_selected_in_the_bouquets_own_spelling(live_bridge, receiver):
    """The caller's spelling is not the one the channel list compares against."""
    channel_list = receiver.with_channel_list([TVP1, TVN])
    assert _zap(live_bridge, receiver, TVN.lower() + ":TVN HD") is None
    assert channel_list.zaps == 1
    assert receiver.nav.sref == TVN
    assert receiver.nav.played == []


def test_a_zap_never_leaves_the_wrong_channel_on_the_television(live_bridge, receiver):
    """🔴 `selectAndStartService` zaps whatever it managed to select.

    A bouquet edited since the channel cache was read leaves the selection
    where it was, and the channel list then tunes that. The plugin reads back
    what is playing and puts the right channel on, the old way.
    """
    # TVN left the bouquet file; the cache still has it.
    channel_list = receiver.with_channel_list([TVP1])
    channel_list.selection = TVP1
    receiver.nav.sref = POLSAT

    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.sref == TVN
    assert receiver.nav.played == [TVN]


def test_a_channel_the_list_cannot_select_is_played_directly(live_bridge, receiver):
    """The cache says the browsed bouquet holds it; the list itself does not.

    A bouquet edited since the `channels` cache was read (up to a minute), or a
    list that hides the channel: the selection stays on what is playing, the
    channel list re-zaps it, and nothing is tuned. Played directly instead.
    """
    channel_list = receiver.with_channel_list([TVP1])
    channel_list.selection = TVP1
    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.sref == TVN
    assert receiver.nav.played == [TVN]


def test_a_protected_channel_waits_for_its_pin_and_is_not_played_twice(live_bridge, receiver):
    from Components.ParentalControl import parentalControl

    parentalControl.protected.add(TVN)
    channel_list = receiver.with_channel_list([TVP1, TVN])
    channel_list.zap = lambda **_arguments: None  # the PIN is on the television
    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.played == []
    assert receiver.nav.sref == TVP1


def test_parental_control_that_cannot_answer_counts_as_a_pin_waiting(live_bridge, receiver):
    from Components.ParentalControl import parentalControl

    parentalControl.raises = True
    channel_list = receiver.with_channel_list([TVP1, TVN])
    channel_list.zap = lambda **_arguments: None
    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.played == []


def test_a_zap_with_a_screen_open_is_played_directly(live_bridge, receiver):
    """The channel list, the EPG or a menu over the info bar: no channel-list zap."""
    channel_list = receiver.with_channel_list([TVP1, TVN])
    receiver.session.current_dialog = channel_list
    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.played == [TVN]
    assert channel_list.zaps == 0
    assert InfoBar.instance.started == []


def test_a_session_that_does_not_say_what_is_open_counts_as_a_screen_open(live_bridge, receiver):
    channel_list = receiver.with_channel_list([TVP1, TVN])
    del receiver.session.current_dialog
    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.played == [TVN]
    assert channel_list.zaps == 0


def test_a_timeshift_check_that_raises_counts_as_timeshift(live_bridge, receiver):
    """Fail-safe: the direct play cannot open the timeshift question."""
    channel_list = receiver.with_channel_list([TVP1, TVN])

    def broken():
        raise RuntimeError("no seek interface")

    InfoBar.instance.isSeekable = broken
    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.played == [TVN]
    assert channel_list.zaps == 0


def test_a_newer_zap_cancels_one_whose_wake_has_finished_but_not_yet_run(live_bridge, receiver):
    """The standby screen closed, the waiting zap's 0 ms timer has not fired yet."""
    channel_list = receiver.with_channel_list([TVP1, TVN])
    screen = receiver.enter_standby(restoring=True)
    assert _zap(live_bridge, receiver, TVN) is None
    screen.finish_close()
    assert _zap(live_bridge, receiver, POLSAT) is None
    MainLoop.advance(0)
    MainLoop.advance(service_module.WAKE_WAIT_MILLISECONDS)
    assert receiver.nav.sref == POLSAT
    assert channel_list.history[-1][-1].toString() == POLSAT


def test_a_channel_in_no_published_bouquet_is_played_directly(live_bridge, receiver, plugin_log):
    """Rule 3: there is no bouquet to enter it through; logged once per reference."""
    radio = "1:0:2:1B1D:802:2:11A0000:0:0:0:"
    channel_list = receiver.with_channel_list([TVP1, TVN])
    assert _zap(live_bridge, receiver, radio) is None
    assert _zap(live_bridge, receiver, radio) is None
    assert receiver.nav.played == [radio, radio]
    assert channel_list.zaps == 0
    assert InfoBar.instance.started == []
    assert plugin_log().count("not in the zap history") == 1


def test_a_zap_during_timeshift_is_played_directly(live_bridge, receiver):
    """The channel list would ask on the television whether to leave timeshift."""
    channel_list = receiver.with_channel_list([TVP1, TVN])
    InfoBar.instance.seekable = True
    InfoBar.instance.timeshift = True
    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.played == [TVN]
    assert channel_list.zaps == 0
    assert InfoBar.instance.started == []


def test_a_timeshift_waiting_to_be_saved_counts_as_timeshift(live_bridge, receiver):
    channel_list = receiver.with_channel_list([TVP1, TVN])
    InfoBar.instance.save_current_timeshift = True
    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.played == [TVN]
    assert channel_list.zaps == 0


def test_a_zap_in_picture_in_picture_zap_mode_is_played_directly(live_bridge, receiver):
    """The channel list would zap the small picture."""
    channel_list = receiver.with_channel_list([TVP1, TVN])
    channel_list.dopipzap = True
    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.played == [TVN]
    assert channel_list.zaps == 0
    assert InfoBar.instance.started == []


def test_a_zap_with_the_channel_list_in_radio_mode_is_played_directly(live_bridge, receiver):
    """A television bouquet entered under the radio root would be saved as the radio root."""
    channel_list = receiver.with_channel_list([TVP1, TVN])
    channel_list.mode = 1
    assert _zap(live_bridge, receiver, TVN) is None
    assert receiver.nav.played == [TVN]
    assert InfoBar.instance.started == []


def test_the_recorded_zap_opens_no_dialog(live_bridge, receiver):
    """The CEC workaround closes channel-list dialogs; this path opens and shows none."""
    receiver.with_channel_list([TVP1, TVN])
    opened = list(receiver.session.opened)
    instantiated = list(receiver.session.instantiated)
    assert _zap(live_bridge, receiver, POLSAT) is None
    assert receiver.session.opened == opened
    assert receiver.session.instantiated == instantiated


def test_a_zap_from_standby_waits_for_the_restore_and_is_recorded(live_bridge, receiver):
    """The standby screen closes a turn late and plays what the box slept on first."""
    channel_list = receiver.with_channel_list([TVP1, TVN])
    screen = receiver.enter_standby(restoring=True)
    zapped = []

    assert _zap(live_bridge, receiver, TVN, on_zap=zapped.append) is None
    assert screen.power_calls == 1
    MainLoop.advance(0)
    assert channel_list.zaps == 0 and zapped == []

    screen.finish_close()
    assert receiver.nav.played == [TVP1]      # the restore, not recorded
    assert channel_list.zaps == 0
    MainLoop.advance(0)
    assert channel_list.zaps == 1
    assert receiver.nav.sref == TVN
    assert _front(channel_list) == TVN
    assert zapped == [TVN]
    MainLoop.advance(10000)
    assert channel_list.zaps == 1


def test_a_zap_made_while_the_standby_screen_closes_is_not_recorded(live_bridge, receiver):
    """Why the zap after a wake waits one more turn: inside `onClose` the screen is still open.

    The standby screen is the executing dialog until it has been popped, and it
    is popped only after its `onClose` has been walked. A zap from in there
    meets a screen open over the info bar, so it is played directly and the
    receiver's history never hears of it.
    """
    channel_list = receiver.with_channel_list([TVP1, TVN])
    screen = receiver.enter_standby(restoring=True)
    assert receiver.session.current_dialog is screen
    seen = []

    def zap_inside_on_close():
        seen.append(receiver.session.current_dialog)
        seen.append(_zap(live_bridge, receiver, TVN))

    screen.onClose.append(zap_inside_on_close)
    screen.finish_close()
    assert seen == [screen, None]
    assert receiver.nav.sref == TVN
    assert channel_list.zaps == 0
    assert _front(channel_list) != TVN
    assert receiver.session.current_dialog is InfoBar.instance


def test_a_standby_that_never_ends_says_so(live_bridge, receiver):
    channel_list = receiver.with_channel_list([TVP1, TVN])
    receiver.enter_standby(restoring=True)
    refusals = []

    assert _zap(live_bridge, receiver, TVN,
                report=lambda command, text: refusals.append((command, text))) is None
    MainLoop.advance(service_module.WAKE_WAIT_MILLISECONDS)
    assert refusals == [("zap", "the receiver did not leave standby")]
    assert channel_list.zaps == 0


def test_a_later_zap_from_standby_replaces_the_one_waiting(live_bridge, receiver):
    channel_list = receiver.with_channel_list([TVP1, TVN])
    screen = receiver.enter_standby(restoring=True)
    assert _zap(live_bridge, receiver, TVN) is None
    assert _zap(live_bridge, receiver, TVP1) is None
    screen.finish_close()
    MainLoop.advance(0)
    assert channel_list.zaps == 1
    assert receiver.nav.sref == TVP1


def test_a_zap_waiting_for_the_wake_is_dropped_by_an_uninstall(live_bridge, receiver):
    """🔴 A removal accepted meanwhile comes first: nothing starts after it."""
    channel_list = receiver.with_channel_list([TVP1, TVN])
    screen = receiver.enter_standby(restoring=True)
    open_doors = [True]
    assert _zap(live_bridge, receiver, TVN, allowed=lambda: open_doors[0]) is None
    open_doors[0] = False
    screen.finish_close()
    MainLoop.advance(0)
    assert channel_list.zaps == 0
    assert receiver.nav.sref == TVP1


def test_cmd_zap_from_standby_verifies_after_the_wake(live_bridge, factory, receiver):
    """The five seconds start when the zap is made, not when the command came."""
    receiver.with_channel_list([TVP1, TVN])
    screen = receiver.enter_standby(restoring=True)
    factory.client.fire_message(ROOT + "/cmd/zap", TVN.encode())
    publisher = live_bridge.publisher("service")
    assert publisher._waiting_for is None
    screen.finish_close()
    MainLoop.advance(0)
    assert publisher._waiting_for == TVN


def test_cmd_zap_is_dropped_after_the_wake_while_the_plugin_removes_itself(
    live_bridge, factory, receiver
):
    receiver.with_channel_list([TVP1, TVN])
    screen = receiver.enter_standby(restoring=True)
    factory.client.fire_message(ROOT + "/cmd/zap", TVN.encode())
    live_bridge.uninstaller.phase = "scheduled"
    screen.finish_close()
    MainLoop.advance(0)
    assert receiver.nav.sref == TVP1


def test_a_zap_to_nonsense_is_refused(live_bridge, receiver, monkeypatch):
    monkeypatch.setattr(service_module, "service_reference", lambda sref: None)
    assert "not a service reference" in service_module.zap(receiver.session, "rubbish")


def test_a_zap_without_a_session_is_refused():
    assert "no session" in service_module.zap(None, TVN)


def test_a_zap_without_a_navigation_is_refused_before_the_receiver_is_woken(
    live_bridge, factory, receiver
):
    """Refused on the spot: a zap that cannot be made must not turn the receiver on first."""
    receiver.with_channel_list([TVP1, TVN])
    screen = receiver.enter_standby(restoring=True)
    receiver.session.nav = None
    factory.client.fire_message(ROOT + "/cmd/zap", TVN.encode())
    assert screen.power_calls == 0
    assert screen.closing is False
    assert factory.client.last(LAST_ERROR).json()["error"] == service_module.NO_SESSION


def test_a_navigation_gone_during_the_wake_refuses_the_zap_with_cmd_zaps_sentence(
    live_bridge, factory, receiver
):
    """The check runs again after the wake, and names the missing piece as `cmd/zap` does."""
    channel_list = receiver.with_channel_list([TVP1, TVN])
    screen = receiver.enter_standby(restoring=True)
    nav = receiver.nav
    factory.client.fire_message(ROOT + "/cmd/zap", TVN.encode())
    assert screen.power_calls == 1
    receiver.session.nav = None
    screen.finish_close()
    MainLoop.advance(0)
    assert factory.client.last(LAST_ERROR).json()["error"] == service_module.NO_SESSION
    assert nav.played == [TVP1]      # the restore, and no zap after it
    assert channel_list.zaps == 0


def test_a_zap_that_lands_says_nothing(live_bridge, factory, receiver):
    publisher = live_bridge.publisher("service")
    publisher.expect(TVN)
    verify = publisher._verify.timer          # the zap satisfies it and hands it back
    receiver.nav.sref = TVN
    receiver.nav.fire(1)
    factory.client.clear()
    verify.fire()
    assert factory.client.all_for(LAST_ERROR) == []


def test_a_zap_that_does_not_land_says_so(live_bridge, factory, receiver):
    publisher = live_bridge.publisher("service")
    publisher.expect(TVN)
    factory.client.clear()
    publisher._verify.timer.fire()
    payload = factory.client.last(LAST_ERROR).json()
    assert payload["cmd"] == "zap"
    assert TVN in payload["error"]


def test_a_zap_is_verified_against_the_identity_not_the_spelling(live_bridge, factory, receiver):
    publisher = live_bridge.publisher("service")
    publisher.expect(TVN + ":TVN HD")
    verify = publisher._verify.timer
    receiver.nav.sref = TVN
    receiver.nav.fire(1)
    factory.client.clear()
    verify.fire()
    assert factory.client.all_for(LAST_ERROR) == []


def test_the_service_info_of_a_box_with_nothing_playing_is_none(receiver):
    receiver.nav.service = None
    receiver.nav.sref = ""
    assert service_module.read_service(receiver.session) is None


def test_reading_a_service_needs_no_channel_list(receiver):
    payload = service_module.read_service(receiver.session)
    assert payload["bouquet"] is None
    assert payload["sref"] == TVP1


def test_an_event_payload_survives_an_event_that_answers_nonsense():
    class Broken:
        def getBeginTime(self):
            raise RuntimeError("no")

    assert service_module._event_payload(Broken()) is None


def test_reading_the_epg_of_a_box_with_nothing_playing(receiver):
    receiver.nav.service = None
    assert service_module.read_epg(receiver.session) == {"now": None, "next": None}


def test_a_service_with_no_provider_publishes_null(live_bridge, factory, receiver):
    receiver.nav.service = Service(ServiceInfo(provider=""), receiver.frontend)
    factory.client.clear()
    receiver.nav.fire(1)
    assert factory.client.last(SERVICE).json()["provider"] is None
