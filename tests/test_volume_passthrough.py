"""Whether the receiver's volume changes anything: `volume.passthrough`.

A receiver set to hand AC-3 or AAC to the television undecoded has a volume
that moves a bar and nothing else. The member says so for the track being
played: true, false, or null when it cannot be told (ADR-0019).
"""

import pytest
from Components.config import ConfigSelection, ConfigSubsection, config
from conftest import AudioTrackInfo, iPlayableService

from MQTTBridge import audio

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
VOLUME = ROOT + "/volume"
INFO = ROOT + "/info"

EV_START = 1
EV_END = 2
EV_UPDATED_INFO = 5


def word(value):
    return ConfigSelection(choices=["downmix", "passthrough"], default=value)


@pytest.fixture
def av(monkeypatch):
    """`config.av` as OpenViX builds it for a receiver that can pass AC-3 and AAC on."""
    section = ConfigSubsection()
    section.downmix_ac3 = word("passthrough")
    section.downmix_aac = word("downmix")
    monkeypatch.setattr(config, "av", section, raising=False)
    return section


@pytest.fixture
def dolby(receiver):
    """A service with an MPEG track and a Dolby Digital one, the MPEG one playing."""
    receiver.audio.tracks = [AudioTrackInfo("MPEG", "pol", 5001),
                             AudioTrackInfo("Dolby Digital", "pol", 5002)]
    receiver.audio.current = 0
    return receiver.audio


def passthrough(factory):
    return factory.client.last(VOLUME).json()["passthrough"]


def reconcile(bridge):
    bridge.publisher("volume")._ticker.timer.fire()


def test_the_volume_says_whether_it_changes_anything(av, dolby, live_bridge, factory):
    payload = factory.client.last(VOLUME).json()
    assert payload == {"level": 35, "muted": False, "passthrough": False}


def test_a_track_handed_to_the_television_undecoded_is_passthrough(av, dolby, live_bridge,
                                                                   factory, receiver):
    dolby.current = 1
    receiver.nav.fire(EV_UPDATED_INFO)
    assert passthrough(factory) is True


def test_an_image_without_the_settings_publishes_null_for_a_track_it_might_pass_on(
    dolby, live_bridge, factory, receiver
):
    dolby.current = 1
    receiver.nav.fire(EV_UPDATED_INFO)
    assert passthrough(factory) is None


@pytest.mark.parametrize("event", ["evStart", "evUpdatedInfo", "evEnd"])
def test_a_service_event_publishes_it_at_once(av, dolby, live_bridge, factory, receiver, event):
    dolby.current = 1
    factory.client.clear()
    receiver.nav.fire(getattr(iPlayableService, event))
    assert passthrough(factory) is True


def test_a_service_event_that_changes_nothing_publishes_nothing(av, dolby, live_bridge, factory,
                                                                receiver):
    factory.client.clear()
    receiver.nav.fire(EV_UPDATED_INFO)
    receiver.nav.fire(EV_START)
    assert factory.client.all_for(VOLUME) == []


def test_an_event_the_volume_does_not_care_about_reads_nothing(av, dolby, live_bridge, factory,
                                                               receiver, monkeypatch):
    read = []
    monkeypatch.setattr(audio, "passthrough", lambda session: read.append(1))
    receiver.nav.fire(iPlayableService.evUpdatedEventInfo)
    receiver.nav.fire(iPlayableService.evTunedIn)
    assert read == []


def test_a_track_chosen_on_the_remote_is_caught_by_the_reconciliation(av, dolby, live_bridge,
                                                                      factory):
    """The image raises no event for it; the five-second look the volume already takes does."""
    factory.client.clear()
    dolby.selectTrack(1)
    assert factory.client.all_for(VOLUME) == []
    reconcile(live_bridge)
    assert passthrough(factory) is True


def test_a_changed_setting_is_caught_by_the_reconciliation(av, dolby, live_bridge, factory):
    dolby.selectTrack(1)
    reconcile(live_bridge)
    av.downmix_ac3.value = "downmix"
    factory.client.clear()
    reconcile(live_bridge)
    assert passthrough(factory) is False


def test_with_nothing_playing_it_cannot_be_told(av, live_bridge, factory, receiver):
    receiver.nav.sref = ""
    receiver.nav.service = None
    receiver.nav.fire(EV_END)
    assert passthrough(factory) is None


def test_a_volume_change_carries_it_too(av, dolby, live_bridge, factory, receiver):
    dolby.current = 1
    receiver.volume_control.volUp()
    payload = factory.client.last(VOLUME).json()
    assert (payload["level"], payload["passthrough"]) == (36, True)


def test_a_reconnect_publishes_it(av, dolby, live_bridge, factory):
    dolby.current = 1
    factory.client.clear()
    factory.client.fire_connect()
    assert passthrough(factory) is True


def test_tracks_that_cannot_be_read_do_not_cost_the_volume(av, live_bridge, factory, receiver,
                                                          monkeypatch, plugin_log):
    def broken(session):
        raise RuntimeError("the service went away")

    monkeypatch.setattr(audio, "passthrough", broken)
    receiver.volume.setVolume(70, 70)
    reconcile(live_bridge)
    assert factory.client.last(VOLUME).json() == {"level": 70, "muted": False,
                                                  "passthrough": None}


def test_reading_the_volume_for_a_command_reads_no_track(av, live_bridge, monkeypatch):
    """`read()` is what mute compares with; the track is the publisher's business."""
    from MQTTBridge import volume

    monkeypatch.setattr(audio, "passthrough", lambda session: 1 / 0)
    assert volume.read() == {"level": 35, "muted": False}


def test_it_is_a_capability_where_the_image_has_the_settings(av, live_bridge, factory):
    names = factory.client.last(INFO).json()["capabilities"]
    assert names[names.index("volume") + 1] == "volume_passthrough"


def test_it_is_no_capability_where_the_image_has_none_of_them(live_bridge, factory):
    """The member is still there, false for a track the receiver always decodes."""
    assert "volume_passthrough" not in factory.client.last(INFO).json()["capabilities"]
    assert passthrough(factory) is False


def test_the_listener_is_taken_off_when_the_publisher_stops(live_bridge, receiver):
    publisher = live_bridge.publisher("volume")
    before = len(receiver.nav.event)
    publisher.stop()
    assert len(receiver.nav.event) == before - 1
    publisher.stop()
    assert len(receiver.nav.event) == before - 1


def test_a_listener_that_cannot_publish_does_not_raise_into_the_image(av, live_bridge, receiver,
                                                                     monkeypatch, plugin_log):
    publisher = live_bridge.publisher("volume")
    monkeypatch.setattr(publisher, "_publish_now", lambda: 1 / 0)
    receiver.nav.fire(EV_START)
    assert "publishing the volume on a service event raised" in plugin_log()


def test_a_session_without_service_events_still_has_a_volume(make_bridge, factory, settings,
                                                             receiver, monkeypatch):
    """The reconciliation alone still says it, within five seconds."""
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    del receiver.nav.event
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert factory.client.last(VOLUME).json()["level"] == 35
    assert bridge.publisher("volume") is not None
