"""The codec facts of the service on the decoder: video codec, scan, frame rate, audio tracks.

Members of the `service` payload, beside the resolution. The numbers the fake
image answers by default are the ones measured on OpenViX 6.6 for an H.264
service sent interlaced at 25 frames a second.
"""

import pytest
from conftest import TVP1, AudioTrackInfo, iPlayableService, iServiceInformation

from MQTTBridge import service as service_module

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
SERVICE = ROOT + "/service"
INFO = ROOT + "/info"

EV_START = 1
EV_END = 2
EV_UPDATED_INFO = 5


def published(factory):
    return factory.client.last(SERVICE).json()


def after_start(factory, receiver):
    receiver.nav.fire(EV_START)
    return published(factory)


def audio_tick(bridge):
    bridge.publisher("service")._audio.timer.fire()


def test_the_service_says_what_is_on_the_decoder(live_bridge, factory):
    payload = published(factory)
    assert payload["video_codec"] == "h264"
    assert payload["interlaced"] is True
    assert payload["frame_rate"] == 25.0
    assert payload["audio_tracks"] == [
        {"index": 0, "description": "MPEG", "language": "pol", "pid": 5001, "codec": "mp2",
         "active": True}]


@pytest.mark.parametrize(("video_type", "codec"), [
    (0, "mpeg2"), (1, "h264"), (3, "vc1"), (4, "mpeg4"), (5, "vc1_sm"), (6, "mpeg1"),
    (7, "hevc"), (16, "avs"), (40, "avs2"),
])
def test_the_video_codec_is_named_from_the_images_stream_type(live_bridge, factory, receiver,
                                                             video_type, codec):
    receiver.info.video_type = video_type
    assert after_start(factory, receiver)["video_codec"] == codec


@pytest.mark.parametrize("video_type", [-1, 2, 8, 9, 15, 17, 39, 41, 99])
def test_a_stream_type_no_image_defines_for_a_dvb_service_is_null(live_bridge, factory, receiver,
                                                                 video_type):
    """Never guessed, and never the number: an unknown value is `null`."""
    receiver.info.video_type = video_type
    assert after_start(factory, receiver)["video_codec"] is None


@pytest.mark.parametrize(("progressive", "interlaced"), [
    (0, True), (1, False), (-1, None), (2, None),
])
def test_interlaced_is_the_images_progressive_flag_turned_round(live_bridge, factory, receiver,
                                                                progressive, interlaced):
    receiver.info.progressive = progressive
    assert after_start(factory, receiver)["interlaced"] is interlaced


@pytest.mark.parametrize(("reported", "rate"), [
    (25000, 25.0), (50000, 50.0), (23976, 23.976), (29970, 29.97), (59940, 59.94), (1, 0.001),
    (0, None), (-1, None),
])
def test_the_frame_rate_is_the_images_thousandths_as_frames_a_second(live_bridge, factory,
                                                                    receiver, reported, rate):
    receiver.info.frame_rate = reported
    assert after_start(factory, receiver)["frame_rate"] == rate


def test_a_value_the_image_cannot_give_is_null_and_the_rest_is_published(live_bridge, factory,
                                                                        receiver, monkeypatch):
    real = receiver.info.getInfo

    def broken(key):
        if key in (iServiceInformation.sVideoType, iServiceInformation.sFrameRate,
                   iServiceInformation.sProgressive):
            raise RuntimeError("no decoder yet")
        return real(key)

    monkeypatch.setattr(receiver.info, "getInfo", broken)
    payload = after_start(factory, receiver)
    assert (payload["video_codec"], payload["interlaced"], payload["frame_rate"]) == (
        None, None, None)
    assert (payload["width"], payload["sref"]) == (1920, TVP1)


@pytest.mark.parametrize("name", ["sVideoType", "sFrameRate", "sProgressive"])
def test_an_image_without_the_constant_publishes_null(live_bridge, factory, receiver,
                                                      monkeypatch, plugin_log, name):
    monkeypatch.delattr(iServiceInformation, name)
    payload = after_start(factory, receiver)
    member = {"sVideoType": "video_codec", "sFrameRate": "frame_rate",
              "sProgressive": "interlaced"}[name]
    assert payload[member] is None
    assert [key for key in ("video_codec", "frame_rate", "interlaced")
            if payload[key] is None] == [member]
    assert "Traceback" not in plugin_log()


def test_the_codec_facts_are_a_capability(live_bridge, factory):
    names = factory.client.last(INFO).json()["capabilities"]
    assert names[names.index("service") + 1] == "service_codecs"


def test_an_image_that_cannot_name_the_video_type_does_not_claim_them(make_bridge, factory,
                                                                     settings, receiver,
                                                                     monkeypatch):
    monkeypatch.delattr(iServiceInformation, "sVideoType")
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    names = factory.client.last(INFO).json()["capabilities"]
    assert "service" in names and "service_codecs" not in names
    assert published(factory)["video_codec"] is None


def test_a_session_that_announces_no_service_events_has_no_service_area(
    make_bridge, factory, settings, receiver
):
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    del receiver.nav.event
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    names = factory.client.last(INFO).json()["capabilities"]
    assert "service" not in names and "service_codecs" not in names
    assert bridge.publisher("service") is None


def test_the_tracks_of_the_playing_service_are_listed(live_bridge, factory, receiver):
    receiver.audio.tracks = [AudioTrackInfo("MPEG", "pol", 5001),
                             AudioTrackInfo("Dolby Digital", "pol", 5002),
                             AudioTrackInfo("???", "qaa", 5003)]
    receiver.audio.current = 1
    tracks = after_start(factory, receiver)["audio_tracks"]
    assert [(t["codec"], t["language"], t["pid"], t["active"]) for t in tracks] == [
        ("mp2", "pol", 5001, False), ("ac3", "pol", 5002, True), (None, "qaa", 5003, False)]


def test_a_service_whose_tracks_cannot_be_read_has_a_null_list(live_bridge, factory, receiver):
    receiver.audio.raises = True
    payload = after_start(factory, receiver)
    assert payload["audio_tracks"] is None
    assert payload["video_codec"] == "h264"


def test_nothing_playing_has_every_new_member_null(live_bridge, factory, receiver):
    receiver.nav.sref = ""
    receiver.nav.service = None
    factory.client.clear()
    receiver.nav.fire(EV_END)
    payload = published(factory)
    assert payload == service_module.EMPTY_SERVICE
    for member in ("video_codec", "interlaced", "frame_rate", "audio_tracks"):
        assert payload[member] is None


def test_a_service_of_a_bouquet_that_is_not_published_has_its_codec_facts(
    make_bridge, factory, settings, receiver
):
    """As its reference and its name are: only `bouquet` is null for it."""
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    settings.bouquets_for_select.value = "Sport (HD)"
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    payload = published(factory)
    assert (payload["sref"], payload["bouquet"]) == (TVP1, None)
    assert (payload["video_codec"], payload["frame_rate"]) == ("h264", 25.0)
    assert payload["audio_tracks"][0]["codec"] == "mp2"


# --------------------------------------------------------------------- events --


@pytest.mark.parametrize("event", ["evVideoSizeChanged", "evVideoFramerateChanged",
                                   "evVideoProgressiveChanged"])
def test_what_the_decoder_learns_later_is_published_when_it_says_so(live_bridge, factory,
                                                                    receiver, event):
    """The frame rate and the scan arrive with the first frames, each with its own event."""
    receiver.info.frame_rate = -1
    receiver.info.progressive = -1
    receiver.nav.fire(EV_START)
    assert published(factory)["frame_rate"] is None
    receiver.info.frame_rate = 50000
    receiver.info.progressive = 1
    factory.client.clear()
    receiver.nav.fire(getattr(iPlayableService, event))
    payload = published(factory)
    assert (payload["frame_rate"], payload["interlaced"]) == (50.0, False)


def test_an_image_without_those_events_still_publishes_the_service(make_bridge, factory,
                                                                   settings, receiver,
                                                                   monkeypatch):
    for name in ("evVideoSizeChanged", "evVideoFramerateChanged", "evVideoProgressiveChanged"):
        monkeypatch.delattr(iPlayableService, name)
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert published(factory)["video_codec"] == "h264"
    receiver.info.frame_rate = 50000
    receiver.nav.fire(EV_UPDATED_INFO)
    assert published(factory)["frame_rate"] == 50.0


def test_a_decoder_event_that_changes_nothing_publishes_nothing(live_bridge, factory, receiver):
    factory.client.clear()
    receiver.nav.fire(iPlayableService.evVideoFramerateChanged)
    assert factory.client.all_for(SERVICE) == []


# ----------------------------------------------------- the track nobody announced --


def test_a_track_chosen_on_the_remote_is_noticed_without_an_event(live_bridge, factory, receiver):
    """The image raises no event for it, so the active track is looked at every five seconds."""
    receiver.audio.tracks = [AudioTrackInfo("MPEG", "pol", 5001),
                             AudioTrackInfo("AC3", "pol", 5002)]
    receiver.nav.fire(EV_START)
    factory.client.clear()
    receiver.audio.selectTrack(1)
    assert factory.client.all_for(SERVICE) == []
    audio_tick(live_bridge)
    tracks = published(factory)["audio_tracks"]
    assert [track["active"] for track in tracks] == [False, True]


def test_the_look_at_the_track_is_every_five_seconds_and_silent(live_bridge, factory):
    assert live_bridge.publisher("service")._audio.timer.started == (5000, False)
    factory.client.clear()
    audio_tick(live_bridge)
    audio_tick(live_bridge)
    assert factory.client.all_for(SERVICE) == []


def test_the_look_asks_for_the_index_and_reads_no_track(live_bridge, receiver, monkeypatch):
    asked = []
    real = receiver.audio.getTrackInfo
    monkeypatch.setattr(receiver.audio, "getTrackInfo",
                        lambda index: asked.append(index) or real(index))
    audio_tick(live_bridge)
    assert asked == []


def test_the_look_does_nothing_with_nothing_playing(live_bridge, factory, receiver):
    receiver.nav.sref = ""
    receiver.nav.service = None
    receiver.nav.fire(EV_END)
    factory.client.clear()
    audio_tick(live_bridge)
    assert factory.client.all_for(SERVICE) == []


def test_a_track_the_image_stops_naming_does_not_republish(live_bridge, factory, receiver):
    receiver.audio.raises = True
    factory.client.clear()
    audio_tick(live_bridge)
    assert factory.client.all_for(SERVICE) == []


def test_the_look_stops_with_the_publisher(live_bridge):
    publisher = live_bridge.publisher("service")
    publisher.stop()
    assert publisher._audio.timer is None
