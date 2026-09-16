"""The snapshot a working receiver produces, and the capabilities it claims.

This module is about the whole set: that a bridge with a session registers every
feature area, that each one lands on the topic `docs/TOPICS.md` names, and that
`capabilities` says exactly what bound. The per-area behaviour — what a zap
publishes, what a guard refuses — is in the module beside each publisher.
"""

import json

from MQTTBridge import channels as channels_module
from MQTTBridge import enigma2, publishers

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE


def topic(suffix):
    return ROOT + "/" + suffix


def test_a_bridge_with_a_session_registers_every_feature_area(live_bridge):
    names = [publisher.name for publisher in live_bridge._publishers]
    assert names == [
        "power", "service", "epg", "tuner", "recording", "timers", "volume", "hdd",
        "channels", "epg_grid", "keys", "screenshot",
    ]


def test_capabilities_name_the_areas_that_bound(live_bridge, factory):
    capabilities = factory.client.last(topic("info")).json()["capabilities"]
    # `message` has no publisher: it is a command, and what makes it real is the
    # popup machinery being importable.
    assert capabilities == [
        "power", "service", "epg", "tuner", "recording", "timers", "volume", "hdd",
        "channels", "epg_grid", "keys", "screenshot", "message",
    ]


def test_capabilities_drop_an_area_whose_hooks_are_missing(make_bridge, factory, settings,
                                                           receiver, monkeypatch):
    """A box with no volume control claims no volume, rather than a dead entity."""
    from MQTTBridge import volume

    monkeypatch.setattr(volume, "_hardware", lambda: None)
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()

    assert "volume" not in bridge.capabilities()
    assert factory.client.last(topic("volume")) is None


def test_capabilities_drop_message_when_the_image_has_no_popups(live_bridge, monkeypatch):
    from MQTTBridge import osd

    monkeypatch.setattr(osd, "_message_box", lambda: None)
    assert "message" not in live_bridge.capabilities()


def test_the_snapshot_covers_every_state_topic(live_bridge, factory):
    published = set(factory.client.topics())
    for suffix in ("availability", "info", "power", "service", "epg", "tuner", "recording",
                   "timers", "volume", "hdd", "channels"):
        assert topic(suffix) in published, suffix


def test_the_snapshot_does_not_include_the_key_topic(live_bridge, factory):
    """A key press is an event; the last one is not what the remote is doing."""
    assert topic("key") not in factory.client.topics()


def test_every_state_topic_is_retained_at_qos_0(live_bridge, factory):
    for entry in factory.client.published:
        if entry.topic.endswith("/key"):
            continue
        assert entry.qos == 0, entry.topic
        assert entry.retain is True, entry.topic


def test_a_publisher_publishes_only_when_something_changed(live_bridge, factory, receiver):
    factory.client.clear()
    live_bridge.publisher("hdd")._poll()
    live_bridge.publisher("hdd")._poll()
    assert factory.client.all_for(topic("hdd")) == []


def test_a_publisher_publishes_again_when_something_does_change(live_bridge, factory, receiver,
                                                                monkeypatch):
    from MQTTBridge import hdd

    monkeypatch.setattr(hdd, "read", lambda path=None: {"mounted": True, "path": "/media/hdd",
                                                        "free_mb": 1})
    factory.client.clear()
    live_bridge.publisher("hdd")._poll()
    monkeypatch.setattr(hdd, "read", lambda path=None: {"mounted": True, "path": "/media/hdd",
                                                        "free_mb": 2})
    live_bridge.publisher("hdd")._poll()
    assert len(factory.client.all_for(topic("hdd"))) == 2


def test_a_reconnect_republishes_everything_even_when_nothing_moved(live_bridge, factory):
    """The broker may have lost its retained store; „unchanged" is about the box."""
    factory.client.clear()
    factory.client.fire_connect()
    published = set(factory.client.topics())
    for suffix in ("availability", "info", "power", "service", "epg", "volume", "hdd"):
        assert topic(suffix) in published, suffix


def test_the_payloads_are_compact_json(live_bridge, factory):
    text = factory.client.last(topic("service")).text
    assert ", " not in text and '": ' not in text


def test_polish_survives_the_encoding(live_bridge, factory):
    """`ensure_ascii=False`, so „Wiadomości" is readable on the wire."""
    text = factory.client.last(topic("epg")).text
    assert "Wiadomości" in text
    assert json.loads(text)["now"]["title"] == "Wiadomości"


def test_a_publisher_that_raises_on_start_is_dropped(make_bridge, factory, settings, receiver,
                                                     monkeypatch):
    from MQTTBridge.hdd import HddPublisher

    def explode(self):
        raise RuntimeError("no disk subsystem")

    monkeypatch.setattr(HddPublisher, "start", explode)
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()

    assert "hdd" not in bridge.capabilities()
    # And the receiver is otherwise unaffected.
    assert "power" in bridge.capabilities()


def test_a_publisher_that_raises_on_snapshot_costs_only_its_own_topic(live_bridge, factory,
                                                                     monkeypatch):
    publisher = live_bridge.publisher("tuner")
    monkeypatch.setattr(publisher, "snapshot", lambda: 1 / 0)
    factory.client.clear()
    live_bridge.publish_snapshot()
    assert topic("tuner") not in factory.client.topics()
    assert topic("volume") in factory.client.topics()


def test_the_default_registry_is_the_documented_order():
    assert [cls.name for cls in publishers.PUBLISHER_CLASSES] == [
        "power", "service", "epg", "tuner", "recording", "timers", "volume", "hdd",
        "channels", "epg_grid", "keys", "screenshot",
    ]


def test_a_bridge_without_a_session_registers_nothing(connected_bridge):
    assert connected_bridge._publishers == []
    assert connected_bridge.capabilities() == []


def test_a_hand_registered_publisher_suppresses_the_defaults(make_bridge, settings, receiver,
                                                             factory):
    from MQTTBridge.bridge import Publisher

    class Only(Publisher):
        name = "power"

    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.register_publisher(Only())
    bridge.start()
    assert [p.name for p in bridge._publishers] == ["power"]


def test_register_publisher_adopts_a_publisher_with_no_bridge(connected_bridge):
    from MQTTBridge.bridge import Publisher

    publisher = Publisher()
    connected_bridge.register_publisher(publisher)
    assert publisher.bridge is connected_bridge


def test_a_reload_does_not_leave_two_of_every_listener(live_bridge, receiver, factory):
    """🔴 The lists a publisher attaches to belong to enigma2 and outlive it.

    Saving the settings screen reloads the bridge. Dropping the registry without
    stopping the publishers would add a second set of listeners on every save —
    two publishes per zap after one save, three after two — until the box was
    restarted.
    """
    before = len(receiver.nav.event)
    live_bridge.reload()
    factory.client.fire_connect()
    assert len(receiver.nav.event) == before
    assert len(receiver.actions.bound) == 1

    factory.client.clear()
    receiver.nav.fire(1)
    assert len(factory.client.all_for(topic("service"))) <= 1


def test_stopping_the_bridge_detaches_everything(live_bridge, receiver):
    live_bridge.stop()
    assert receiver.nav.event == []
    assert receiver.nav.record_event == []
    assert receiver.actions.bound == []
    assert live_bridge._publishers == []


def test_nothing_playing_still_publishes_the_shape(make_bridge, factory, settings, receiver):
    """A key with no value is `null`; it is never a missing topic."""
    receiver.nav.service = None
    receiver.nav.sref = ""
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert factory.client.last(topic("service")).json()["sref"] is None
    assert factory.client.last(topic("tuner")).json()["snr"] is None


def test_missing_is_logged_once_not_once_a_minute(plugin_log):
    enigma2.missing("Screens.Nowhere")
    enigma2.missing("Screens.Nowhere")
    enigma2.missing("Screens.Nowhere")
    assert plugin_log().count("Screens.Nowhere") == 1


def test_a_ticker_that_cannot_reach_its_callback_does_not_raise(monkeypatch):
    monkeypatch.setattr(enigma2, "enigma_attribute", lambda name: None)
    ticker = enigma2.Ticker(lambda: None, "nowhere")
    assert ticker.start(1000) is False
    assert ticker.stop() is False


def test_a_ticker_callback_that_raises_is_caught(plugin_log):
    def explode():
        raise ValueError("inside a timer")

    ticker = enigma2.Ticker(explode, "explosive")
    ticker.start(10)
    ticker.timer.fire()
    assert "explosive" in plugin_log()


def test_identity_ignores_the_name_a_bouquet_gave_a_service():
    plain = "1:0:19:283D:3FB:1:C00000:0:0:0:"
    named = "1:0:19:283D:3FB:1:C00000:0:0:0::TVP 1 HD"
    assert enigma2.same_service(plain, named)


def test_identity_keeps_two_streams_apart():
    """🔴 The ten numbers of an IPTV service are identical; the URL is not."""
    one = "4097:0:1:0:0:0:0:0:0:0:http%3a//example.invalid/one"
    other = "4097:0:1:0:0:0:0:0:0:0:http%3a//example.invalid/two"
    assert not enigma2.same_service(one, other)


def test_identity_of_nothing_matches_nothing():
    assert enigma2.identity("") == ""
    assert not enigma2.same_service("", "")


def test_slugify_transliterates_polish():
    assert channels_module.slugify("Ulubione TV") == "ulubione_tv"
    assert channels_module.slugify("Łódź — Kanały Główne") == "lodz_kanaly_glowne"
    assert channels_module.slugify("Favourites (TV)") == "favourites_tv"
    assert channels_module.slugify("   ") == ""
