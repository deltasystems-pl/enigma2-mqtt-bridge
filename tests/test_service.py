"""What is playing, what is on, and how well it is arriving."""

from conftest import POLSAT, TVN, TVP1, Event, Frontend, Service, ServiceInfo

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
    receiver.nav.fire(6)  # evUpdatedEventInfo — the EPG publisher's business
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


def test_a_zap_plays_the_service(live_bridge, receiver):
    assert service_module.zap(receiver.session, TVN) is None
    assert receiver.nav.played == [TVN]


def test_a_zap_wakes_the_box_first(live_bridge, receiver):
    screen = receiver.enter_standby()
    service_module.zap(receiver.session, TVN)
    assert screen.power_calls == 1
    assert receiver.nav.played == [TVN]


def test_a_zap_uses_the_channel_list_when_the_service_is_in_it(live_bridge, receiver):
    channel_list = receiver.with_channel_list([TVP1, TVN])
    assert service_module.zap(receiver.session, TVN) is None
    assert channel_list.zaps == 1
    # And it did not also play it a second way.
    assert receiver.nav.played == []


def test_a_zap_never_asks_the_channel_list_to_tune_the_wrong_channel(live_bridge, receiver):
    """🔴 `zap()` tunes what is *selected*, so a selection that did not take
    would tune the television to whatever was selected before."""
    channel_list = receiver.with_channel_list([TVP1])
    assert service_module.zap(receiver.session, POLSAT) is None
    assert channel_list.zaps == 0
    assert receiver.nav.played == [POLSAT]


def test_a_zap_in_standby_goes_straight_to_the_player(live_bridge, receiver):
    channel_list = receiver.with_channel_list([TVP1, TVN])
    receiver.enter_standby()
    service_module.zap(receiver.session, TVN)
    assert channel_list.zaps == 0
    assert receiver.nav.played == [TVN]


def test_a_zap_to_nonsense_is_refused(live_bridge, receiver, monkeypatch):
    monkeypatch.setattr(service_module, "service_reference", lambda sref: None)
    assert "not a service reference" in service_module.zap(receiver.session, "rubbish")


def test_a_zap_without_a_session_is_refused():
    assert "no session" in service_module.zap(None, TVN)


def test_a_zap_that_lands_says_nothing(live_bridge, factory, receiver):
    publisher = live_bridge.publisher("service")
    publisher.expect(TVN)
    receiver.nav.sref = TVN
    receiver.nav.fire(1)
    factory.client.clear()
    publisher._verify.timer.fire()
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
    receiver.nav.sref = TVN
    receiver.nav.fire(1)
    factory.client.clear()
    publisher._verify.timer.fire()
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
