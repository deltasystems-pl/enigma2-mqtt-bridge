"""A payload too big for one MQTT packet: what is sent instead, and what says so.

A broker closes the connection on a packet over its limit, the will says
`offline`, the client reconnects and the snapshot sends the same payload again.
So the assertions here are about what the client is handed - an oversized
payload never, on any connect - and about `info.not_published`, which is how a
consumer learns that a topic is missing on purpose.
"""

import conftest
import pytest
from conftest import TVP1, Event

from MQTTBridge import bridge as bridge_module
from MQTTBridge import discovery
from MQTTBridge.bridge import Publisher

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
AVAILABILITY = ROOT + "/availability"
INFO = ROOT + "/info"
CHANNELS = ROOT + "/channels"
VOLUME = ROOT + "/volume"
ANNOUNCEMENT = "enigma2mqtt/discovery/" + NODE + "/config"
DEVICE = "homeassistant/device/" + NODE + "/config"
ULUBIONE = ROOT + "/epg_grid/ulubione_tv"
ULUBIONE_LIST = ROOT + "/channels/ulubione_tv"
SPORT = ROOT + "/epg_grid/sport_hd"

# Written out rather than read from the module: the number is the promise.
LIMIT = 1000000
# The fixed header at its longest, and the two bytes of the topic's length.
OVERHEAD = 1 + 4 + 2
# paho's MQTT_ERR_NO_CONN: its answer to a publish while the socket is gone.
NO_CONNECTION = 4
# How long `info` waits for a change to `not_published` to settle, and at most.
SETTLE = 2000
LATEST = 10000


def packet_bytes(topic, payload):
    """What the plugin counts for one publish: header, topic and payload, in bytes."""
    if not isinstance(payload, bytes):
        payload = payload.encode("utf-8")
    return OVERHEAD + len(topic.encode("utf-8")) + len(payload)


def channel_list(characters):
    """A `channels` payload of about `characters` characters."""
    return {
        "generated": 1789459200,
        "bouquets": [{"name": "All", "channels": [{"name": "x" * characters, "sref": "1:0:1"}]}],
    }


BIG = channel_list(LIMIT + 1000)
SMALL = channel_list(10)


class Channels(Publisher):
    """The channel list, with a payload the test sets."""

    name = "channels"

    def __init__(self, payload, names=()):
        Publisher.__init__(self)
        self.payload = payload
        self.names = list(names)

    @property
    def bouquets(self):
        return [{"name": "All", "channels": [{"name": name} for name in self.names]}]

    def snapshot(self):
        return {"channels": self.payload, "volume": {"level": 35, "muted": False}}


def connect(make_bridge, factory, settings, payload, ha_mode="integration", names=()):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.ha_mode.value = ha_mode
    bridge = make_bridge()
    publisher = bridge.register_publisher(Channels(payload, names))
    bridge.start()
    factory.client.fire_connect()
    return bridge, publisher


def not_published(factory):
    """What `info` says is withheld, once it has had its moment to say so."""
    conftest.say_withheld()
    return factory.client.last(INFO).json().get("not_published")


def close_the_doors(bridge, door):
    """The two states in which nothing may go out: a removal under way, an update gone silent."""
    if door == "uninstall":
        bridge.uninstaller.closed = True
    else:
        bridge.self_update.silent = True


class Padding:
    """A device discovery payload too big whatever its channel select holds.

    The select is the component that grows with the receiver, and the bridge
    leaves it out before it withholds the payload (`test_discovery_fit.py`). So
    a payload that is still too big is made here from the other end: the
    origin's URL, which no component reads, is padded past the bound while
    `on` is set.
    """

    def __init__(self, monkeypatch):
        self.on = True
        build = discovery.build_discovery_components

        def padded(*args, **kwargs):
            topics = build(*args, **kwargs)
            if self.on:
                topics[DEVICE]["o"]["url"] += "/" + "p" * (LIMIT + 1000)
            return topics

        monkeypatch.setattr(discovery, "build_discovery_components", padded)


def entry(suffix, topic, payload):
    return {
        "topic": suffix,
        "bytes": packet_bytes(topic, bridge_module._encoded(payload)),
        "limit": LIMIT,
    }


def test_the_bound_is_under_the_brokers_defaults():
    assert bridge_module.MAX_PACKET_BYTES == LIMIT
    assert LIMIT < 1024 * 1024 < 2000000


def test_an_oversized_channel_list_is_never_handed_to_the_client(make_bridge, factory, settings):
    connect(make_bridge, factory, settings, BIG)

    # Nothing at all: it was never published, so there is nothing to retract either.
    assert factory.client.all_for(CHANNELS) == []
    assert all(len(sent.payload) < LIMIT for sent in factory.client.published)


def test_everything_else_is_still_published(make_bridge, factory, settings):
    connect(make_bridge, factory, settings, BIG)

    assert factory.client.all_for(CHANNELS) == []
    assert factory.client.last(AVAILABILITY).text == "online"
    assert factory.client.last(INFO).json()["capabilities"] == ["channels"]
    assert factory.client.last(VOLUME).json() == {"level": 35, "muted": False}
    assert factory.client.last(ANNOUNCEMENT).json()["node_id"] == NODE


def test_a_reconnect_does_not_send_it_either(make_bridge, factory, settings):
    connect(make_bridge, factory, settings, BIG)
    factory.client.clear()

    factory.client.fire_connect()
    factory.client.fire_connect()

    assert factory.client.all_for(CHANNELS) == []
    # The rest of the snapshot still goes out on every connect.
    assert len(factory.client.all_for(VOLUME)) == 2


def test_it_is_logged_once_and_not_on_every_connect(make_bridge, factory, settings, plugin_log):
    connect(make_bridge, factory, settings, BIG)
    factory.client.fire_connect()
    factory.client.fire_connect()

    written = plugin_log()
    size = packet_bytes(CHANNELS, bridge_module._encoded(BIG))
    line = f"not publishing channels: {size} bytes is over the {LIMIT} byte packet limit"
    assert written.count(line) == 1
    assert written.count("not publishing") == 1
    assert "WARNING" in [row for row in written.splitlines() if line in row][0]
    # The size and the name, never the content.
    assert "x" * 64 not in written


def test_another_size_while_it_stays_too_big_says_nothing_new(make_bridge, factory, settings,
                                                              plugin_log):
    """A grid that stays too big has another size at every pass; that is not news."""
    _bridge, publisher = connect(make_bridge, factory, settings, BIG)
    factory.client.clear()

    publisher.publish("channels", channel_list(LIMIT + 2000))
    publisher.publish("channels", channel_list(LIMIT + 3000))

    assert plugin_log().count("not publishing channels") == 1
    assert factory.client.published == []

    # And the size a consumer reads is the one first measured.
    factory.client.fire_connect()
    assert not_published(factory) == [entry("channels", CHANNELS, BIG)]


def test_too_big_again_after_it_fitted_is_a_new_withholding(make_bridge, factory, settings,
                                                            plugin_log):
    _bridge, publisher = connect(make_bridge, factory, settings, BIG)
    publisher.publish("channels", SMALL)
    factory.client.clear()
    bigger = channel_list(LIMIT + 2000)

    publisher.publish("channels", bigger)
    conftest.say_withheld()

    assert plugin_log().count("not publishing channels") == 2
    # The copy that fitted is taken back, and `info` says so with the new size.
    assert [sent.text for sent in factory.client.all_for(CHANNELS)] == [""]
    lists = [sent.json()["not_published"] for sent in factory.client.all_for(INFO)]
    assert lists == [[entry("channels", CHANNELS, bigger)]]


def test_info_names_what_was_not_published(make_bridge, factory, settings):
    connect(make_bridge, factory, settings, BIG)

    assert not_published(factory) == [entry("channels", CHANNELS, BIG)]


def test_info_is_published_again_when_the_list_changes(make_bridge, factory, settings):
    """`info` is built before the snapshot runs, so the first one cannot know."""
    connect(make_bridge, factory, settings, BIG)
    conftest.say_withheld()

    lists = [sent.json().get("not_published") for sent in factory.client.all_for(INFO)]
    assert lists == [[], [entry("channels", CHANNELS, BIG)]]


def test_a_reconnect_says_it_in_the_first_info(make_bridge, factory, settings):
    connect(make_bridge, factory, settings, BIG)
    factory.client.clear()

    factory.client.fire_connect()

    lists = [sent.json().get("not_published") for sent in factory.client.all_for(INFO)]
    assert lists == [[entry("channels", CHANNELS, BIG)]]


def test_info_is_empty_handed_when_everything_fits(make_bridge, factory, settings):
    connect(make_bridge, factory, settings, SMALL)

    assert not_published(factory) == []
    assert len(factory.client.all_for(INFO)) == 1
    assert factory.client.last(CHANNELS).json() == SMALL


def test_it_is_published_once_it_fits_again(make_bridge, factory, settings):
    _bridge, publisher = connect(make_bridge, factory, settings, BIG)
    conftest.say_withheld()
    factory.client.clear()

    publisher.publish("channels", SMALL)
    conftest.say_withheld()

    assert factory.client.last(CHANNELS).json() == SMALL
    assert factory.client.last(CHANNELS).retain is True
    assert [sent.json()["not_published"] for sent in factory.client.all_for(INFO)] == [[]]


def test_a_smaller_copy_on_the_broker_is_retracted(make_bridge, factory, settings):
    """Left retained, last week's channel list would go on claiming to be this week's."""
    bridge, publisher = connect(make_bridge, factory, settings, SMALL)
    assert bridge.state.knows(CHANNELS)
    factory.client.clear()

    publisher.payload = BIG
    publisher.publish("channels", BIG)

    sent = factory.client.all_for(CHANNELS)
    assert [(one.text, one.retain) for one in sent] == [("", True)]
    assert not bridge.state.knows(CHANNELS)
    assert not_published(factory) == [entry("channels", CHANNELS, BIG)]

    # Retracted once: the next attempt and the next connect have nothing to take back.
    factory.client.clear()
    publisher.publish("channels", BIG)
    factory.client.fire_connect()
    assert factory.client.all_for(CHANNELS) == []


def test_a_copy_an_earlier_process_left_is_retracted(make_bridge, factory, settings):
    """The state file is what knows about it; this process never published it."""
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL)
    bridge.stop()
    factory.client.clear()

    connect(make_bridge, factory, settings, BIG)

    assert [one.text for one in factory.client.all_for(CHANNELS)] == [""]


def test_the_page_does_not_show_it_as_published(make_bridge, factory, settings):
    bridge, publisher = connect(make_bridge, factory, settings, SMALL)
    assert CHANNELS in [topic for topic, _payload in bridge.last_payloads()]

    publisher.publish("channels", BIG)

    assert CHANNELS not in [topic for topic, _payload in bridge.last_payloads()]


def test_a_topic_retracted_on_purpose_leaves_the_list(make_bridge, factory, settings):
    bridge, _publisher = connect(make_bridge, factory, settings, BIG)
    assert not_published(factory) != []
    factory.client.clear()

    bridge.retract(CHANNELS)
    conftest.say_withheld()

    assert [sent.json()["not_published"] for sent in factory.client.all_for(INFO)] == [[]]


def test_a_connect_still_says_online_before_anything_else(connected_bridge, factory):
    """The retractions a connect opens with can change the list; `info` waits for its turn."""
    cam = ROOT + "/cam"
    connected_bridge.publish_raw(cam, b"\xff" * LIMIT)
    assert [one["topic"] for one in not_published(factory)] == ["cam"]
    factory.client.clear()

    # `cam_telemetry` is off, so the connect retracts `cam` before it publishes.
    factory.client.fire_connect()

    payloads = [sent for sent in factory.client.published if sent.text]
    assert payloads[0].topic == AVAILABILITY
    assert [sent.json()["not_published"] for sent in factory.client.all_for(INFO)] == [[]]


def test_a_change_while_disconnected_waits_for_the_connect(make_bridge, factory, settings):
    """A timer does not stop for an outage; the `info` of the next connect says it."""
    _bridge, publisher = connect(make_bridge, factory, settings, SMALL)
    factory.client.fire_disconnect(7)
    factory.client.clear()

    publisher.payload = BIG
    publisher.publish("channels", BIG)
    assert factory.client.all_for(INFO) == []

    factory.client.fire_connect()
    lists = [sent.json().get("not_published") for sent in factory.client.all_for(INFO)]
    assert lists == [[entry("channels", CHANNELS, BIG)]]


def test_a_settings_save_measures_everything_again(make_bridge, factory, settings):
    """Narrowing the bouquets is the remedy, and it arrives as a reload."""
    bridge, publisher = connect(make_bridge, factory, settings, BIG)

    publisher.payload = SMALL
    bridge.reload()
    bridge.register_publisher(Channels(SMALL))
    factory.client.fire_connect()

    assert not_published(factory) == []
    assert len(factory.client.all_for(INFO)) == 1


def test_exactly_the_bound_passes_and_one_byte_over_does_not(connected_bridge, factory):
    topic = ROOT + "/first"
    other = ROOT + "/other"
    fits = b"\xff" * (LIMIT - OVERHEAD - len(topic))
    assert packet_bytes(topic, fits) == LIMIT

    connected_bridge.publish_raw(topic, fits)
    assert len(factory.client.last(topic).payload) == len(fits)
    factory.client.clear()

    connected_bridge.publish_raw(other, fits + b"\xff")
    assert factory.client.all_for(other) == []
    assert not_published(factory) == [{"topic": "other", "bytes": LIMIT + 1, "limit": LIMIT}]


def test_text_is_measured_at_the_bound_too(connected_bridge, factory):
    topic = ROOT + "/text"
    fits = "x" * (LIMIT - OVERHEAD - len(topic))

    connected_bridge.publish_raw(topic, fits)
    assert len(factory.client.last(topic).payload) == len(fits)
    factory.client.clear()

    connected_bridge.publish_raw(topic, fits + "x")
    # One retraction of the copy that fitted, and no payload.
    assert [sent.text for sent in factory.client.all_for(topic)] == [""]


def test_a_payload_is_measured_in_bytes_not_characters(connected_bridge, factory):
    """600,000 letters fit; 600,000 Polish ones are 1,200,000 bytes and do not."""
    count = 600000
    assert count < LIMIT < count * 2

    connected_bridge.publish_raw(ROOT + "/ascii", "l" * count)
    connected_bridge.publish_raw(ROOT + "/polish", "ł" * count)

    assert len(factory.client.last(ROOT + "/ascii").payload) == count
    assert factory.client.all_for(ROOT + "/polish") == []
    assert not_published(factory) == [
        {"topic": "polish", "bytes": OVERHEAD + len(ROOT + "/polish") + count * 2, "limit": LIMIT}
    ]


def test_an_oversized_discovery_payload_is_named_by_its_full_topic(make_bridge, factory,
                                                                    settings, monkeypatch):
    """A device payload too big even without its channel select is not sent."""
    Padding(monkeypatch)
    connect(make_bridge, factory, settings, SMALL, ha_mode="discovery", names=["One", "Two"])

    assert factory.client.all_for(DEVICE) == []
    assert factory.client.last(CHANNELS).json() == SMALL
    listed = not_published(factory)
    assert [one["topic"] for one in listed] == [DEVICE]
    assert listed[0]["bytes"] > LIMIT
    assert listed[0]["limit"] == LIMIT


def test_a_discovery_payload_on_the_broker_is_never_retracted(make_bridge, factory, settings,
                                                              plugin_log, monkeypatch):
    """An empty retained device payload deletes the device and every entity it has."""
    padding = Padding(monkeypatch)
    padding.on = False
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL, ha_mode="discovery",
                                 names=["One", "Two"])
    assert factory.client.last(DEVICE).json()
    assert bridge.state.knows(DEVICE)
    factory.client.clear()

    padding.on = True
    bridge.publish_discovery()

    # Neither the payload nor a retraction: the device keeps the options it had.
    assert factory.client.all_for(DEVICE) == []
    assert bridge.state.knows(DEVICE)
    assert [one["topic"] for one in not_published(factory)] == [DEVICE]
    assert plugin_log().count("not publishing " + DEVICE) == 1

    factory.client.clear()
    factory.client.fire_connect()
    assert factory.client.all_for(DEVICE) == []
    assert [one["topic"] for one in not_published(factory)] == [DEVICE]


def test_a_grid_too_big_is_reported_when_its_timer_publishes_it(live_bridge, factory, receiver):
    """The grid is built on a timer, long after the connect published `info`."""
    assert factory.client.last(ULUBIONE).json()["channels"]
    assert not_published(factory) == []
    receiver.epg.events[TVP1] = [Event(1, 10, 20, "t" * (LIMIT + 1000))]
    factory.client.clear()

    publisher = live_bridge.publisher("epg_grid")
    publisher.regenerate()
    conftest.settle(live_bridge)

    # The grid that fitted is taken back, the other bouquet is untouched.
    assert [sent.text for sent in factory.client.all_for(ULUBIONE)] == [""]
    listed = not_published(factory)
    assert [one["topic"] for one in listed] == ["epg_grid/ulubione_tv"]
    assert listed[0]["bytes"] > LIMIT
    assert len(factory.client.all_for(INFO)) == 1

    # And the next pass, with the same grid, says nothing new.
    factory.client.clear()
    publisher.regenerate()
    conftest.settle(live_bridge)
    assert factory.client.all_for(ULUBIONE) == []
    assert factory.client.all_for(INFO) == []


# --------------------------------------------------- a retraction that cannot be sent --
#
# Forgetting a topic is only true once the broker was told. A publisher that
# publishes while it starts does so before the session exists, and a timer does
# not stop for an outage; in both cases the retained copy is still on the broker.


def test_a_copy_an_earlier_process_left_is_retracted_although_the_publisher_starts_first(
    make_bridge, factory, settings, receiver
):
    """The real channel list publishes in `start()`, before the client has a session.

    One name of a megabyte is too much for `channels` and for the list of the
    bouquet it is in, `channels/ulubione_tv`; both are withheld the same way.
    """
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.ha_mode.value = "integration"
    settings.epg_grid_events.value = 0
    first = make_bridge(session=receiver.session)
    first.start()
    factory.client.fire_connect()
    assert factory.client.last(CHANNELS).json()["bouquets"]
    assert factory.client.last(ULUBIONE_LIST).json()["channels"]
    first.stop()

    receiver.service_center.contents[conftest.FIRST_BOUQUET].append(
        (conftest.POLSAT, "x" * (LIMIT + 1000))
    )
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    client = factory.client
    # Nothing could be sent yet, so the state file still has to know the topics.
    for topic in (CHANNELS, ULUBIONE_LIST):
        assert client.all_for(topic) == []
        assert bridge.state.knows(topic)

    client.fire_connect()

    for topic in (CHANNELS, ULUBIONE_LIST):
        assert [sent.text for sent in client.all_for(topic)] == [""]
        assert not bridge.state.knows(topic)
    # They were measured before the connect, so the first `info` already names them.
    lists = [sent.json()["not_published"] for sent in client.all_for(INFO)]
    assert [[one["topic"] for one in listed] for listed in lists] == [
        ["channels", "channels/ulubione_tv"]
    ]


def test_a_topic_that_outgrew_the_bound_during_an_outage_is_retracted_on_the_connect(
    make_bridge, factory, settings
):
    bridge, publisher = connect(make_bridge, factory, settings, SMALL)
    factory.client.fire_disconnect(7)

    publisher.payload = BIG
    publisher.publish("channels", BIG)
    # The fake takes a publish without a session; the broker would never see it.
    assert bridge.state.knows(CHANNELS)
    factory.client.clear()

    factory.client.fire_connect()

    assert [sent.text for sent in factory.client.all_for(CHANNELS)] == [""]
    assert not bridge.state.knows(CHANNELS)


def test_a_withheld_topic_no_snapshot_carries_is_retracted_on_the_connect(connected_bridge,
                                                                          factory):
    topic = ROOT + "/late"
    connected_bridge.publish_raw(topic, "small")
    factory.client.fire_disconnect(7)
    connected_bridge.publish_raw(topic, b"\xff" * LIMIT)
    factory.client.clear()

    factory.client.fire_connect()

    assert [sent.text for sent in factory.client.all_for(topic)] == [""]
    assert not connected_bridge.state.knows(topic)
    assert [one["topic"] for one in not_published(factory)] == ["late"]


def test_a_retraction_the_client_did_not_take_is_not_forgotten(make_bridge, factory, settings):
    """The socket is gone and the disconnect has not reached the main thread yet.

    The session still looks open, so the retraction is handed to the client -
    which answers "no connection". The broker never saw it, and the state file
    is the only thing that still knows of the copy there.
    """
    bridge, publisher = connect(make_bridge, factory, settings, SMALL)
    factory.client.clear()
    factory.client.publish_rc = NO_CONNECTION

    publisher.payload = BIG
    publisher.publish("channels", BIG)

    assert [sent.text for sent in factory.client.all_for(CHANNELS)] == [""]
    assert bridge.connected
    assert bridge.state.knows(CHANNELS)

    factory.client.publish_rc = 0
    factory.client.fire_disconnect(7)
    factory.client.clear()
    factory.client.fire_connect()

    assert [sent.text for sent in factory.client.all_for(CHANNELS)] == [""]
    assert not bridge.state.knows(CHANNELS)


def test_a_retraction_the_client_dropped_is_not_forgotten_either(make_bridge, factory, settings,
                                                                 monkeypatch):
    """The client answers None for a publish that raised: nothing was sent then either."""
    bridge, publisher = connect(make_bridge, factory, settings, SMALL)
    monkeypatch.setattr(bridge.client, "publish", lambda *args, **kwargs: None)

    publisher.payload = BIG
    publisher.publish("channels", BIG)

    assert bridge.state.knows(CHANNELS)


@pytest.mark.parametrize("door", ["uninstall", "update"])
def test_the_connect_sweep_sends_nothing_behind_closed_doors(make_bridge, factory, settings,
                                                             door):
    """A reconnect while the plugin removes itself, or after an update took the topics back."""
    bridge, publisher = connect(make_bridge, factory, settings, SMALL)
    factory.client.fire_disconnect(7)
    publisher.payload = BIG
    publisher.publish("channels", BIG)
    assert bridge.state.knows(CHANNELS)
    close_the_doors(bridge, door)
    factory.client.clear()

    factory.client.fire_connect()

    assert factory.client.all_for(CHANNELS) == []
    assert bridge.state.knows(CHANNELS)


@pytest.mark.parametrize("door", ["uninstall", "update"])
def test_a_publisher_withholds_nothing_behind_closed_doors(make_bridge, factory, settings, door):
    """`withhold_state` is `publish_raw` for a payload already measured, guard included."""
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL)
    close_the_doors(bridge, door)
    factory.client.clear()

    assert bridge.withhold_state("channels", LIMIT + 1) is None

    assert factory.client.published == []
    assert bridge.not_published() == []
    assert bridge.state.knows(CHANNELS)


def test_the_same_small_payload_is_published_again_after_a_big_one(make_bridge, factory,
                                                                   settings):
    """The copy that fitted was retracted, so "unchanged" is not a reason to keep quiet."""
    _bridge, publisher = connect(make_bridge, factory, settings, SMALL)
    publisher.publish("channels", BIG)
    factory.client.clear()

    publisher.publish("channels", SMALL)

    assert [sent.json() for sent in factory.client.all_for(CHANNELS)] == [SMALL]


# ------------------------------------------------------------------- discovery --


def test_reset_leaves_a_discovery_payload_it_could_not_replace(make_bridge, factory, settings,
                                                               monkeypatch):
    """A reset retracts and republishes; a device it cannot republish it must not delete."""
    padding = Padding(monkeypatch)
    padding.on = False
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL, ha_mode="discovery",
                                 names=["One", "Two"])
    padding.on = True
    factory.client.clear()

    bridge.reset_retained()

    assert factory.client.all_for(DEVICE) == []
    assert bridge.state.knows(DEVICE)
    assert "channel_select" in bridge.state.component_keys
    assert [one["topic"] for one in not_published(factory)] == [DEVICE]
    # Everything else is retracted and put back, as a reset always did.
    assert [bool(sent.text) for sent in factory.client.all_for(CHANNELS)] == [False, True]
    assert [bool(sent.text) for sent in factory.client.all_for(ANNOUNCEMENT)] == [False, True]


def test_reset_counts_what_it_retracted(make_bridge, factory, settings, monkeypatch,
                                        plugin_log):
    """The number it returns is the number in its log line: the payload it kept is in neither."""
    padding = Padding(monkeypatch)
    padding.on = False
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL, ha_mode="discovery",
                                 names=["One", "Two"])
    owned = len(bridge.state.retained_topics)
    padding.on = True
    factory.client.clear()

    count = bridge.reset_retained()

    emptied = [sent.topic for sent in factory.client.published if not sent.text]
    assert DEVICE not in emptied
    assert count == len(emptied) == owned - 1
    assert f"reset: retracting {count} retained topic(s)" in plugin_log()


def test_reset_counts_everything_when_nothing_is_kept(make_bridge, factory, settings):
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL, ha_mode="discovery",
                                 names=["One", "Two"])
    owned = len(bridge.state.retained_topics)

    assert bridge.reset_retained() == owned


def test_reset_still_replaces_a_discovery_payload_that_fits(make_bridge, factory, settings):
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL, ha_mode="discovery",
                                 names=["One", "Two"])
    factory.client.clear()

    bridge.reset_retained()

    assert [bool(sent.text) for sent in factory.client.all_for(DEVICE)] == [False, True]


def test_a_withheld_discovery_payload_does_not_forget_what_was_announced(make_bridge, factory,
                                                                         settings, monkeypatch):
    """What Home Assistant still has is the last payload that was sent, not the last one built."""
    padding = Padding(monkeypatch)
    padding.on = False
    bridge, publisher = connect(make_bridge, factory, settings, SMALL, ha_mode="discovery",
                                names=["One", "Two"])
    announced = bridge.state.component_keys
    assert "channel_select" in announced
    # A component the device payload on the broker carries, and the next one will not.
    bridge.state.set_component_keys(dict(announced, gone="sensor"))

    padding.on = True
    bridge.publish_discovery()
    assert bridge.state.component_keys.get("gone") == "sensor"
    assert "channel_select" in bridge.state.component_keys

    padding.on = False
    publisher.names = ["One"]
    bridge.publish_discovery()
    assert factory.client.last(DEVICE).json()["cmps"]["gone"] == {"p": "sensor"}
    assert "gone" not in bridge.state.component_keys


# ----------------------------------------------------------------- the measure --


def test_four_byte_characters_are_counted_as_four(connected_bridge, factory):
    topic = ROOT + "/pictures"
    count = 300000
    assert count * 3 < LIMIT < count * 4

    connected_bridge.publish_raw(topic, chr(0x1F4FA) * count)

    assert factory.client.all_for(topic) == []
    assert not_published(factory) == [
        {"topic": "pictures", "bytes": OVERHEAD + len(topic) + count * 4, "limit": LIMIT}
    ]


def test_the_topic_is_measured_in_bytes_too(connected_bridge, factory):
    topic = ROOT + "/" + chr(0x142) * 8
    assert len(topic.encode("utf-8")) == len(topic) + 8

    connected_bridge.publish_raw(topic, b"\xff" * (LIMIT + 1 - packet_bytes(topic, b"")))

    assert factory.client.all_for(topic) == []


def test_a_bytearray_is_measured_as_the_bytes_it_is(connected_bridge, factory):
    topic = ROOT + "/buffer"
    fits = bytearray(b"\xff" * (LIMIT - packet_bytes(topic, b"")))

    connected_bridge.publish_raw(topic, fits)

    assert len(factory.client.last(topic).payload) == len(fits)


def test_an_event_too_big_is_dropped_and_takes_nothing_back(connected_bridge, factory,
                                                            plugin_log):
    """A publish that is not retained has no retained copy of its own to retract."""
    topic = ROOT + "/key"
    connected_bridge.publish_raw(topic, "retained")
    factory.client.clear()

    connected_bridge.publish_raw(topic, b"\xff" * LIMIT, retain=False)

    assert factory.client.published == []
    assert connected_bridge.state.knows(topic)
    assert connected_bridge.not_published() == []
    assert "not publishing key" in plugin_log()


def test_every_event_too_big_is_logged(connected_bridge, plugin_log):
    """An event is not a state that stays withheld: each one dropped is one line."""
    topic = ROOT + "/key"
    connected_bridge.publish_raw(topic, b"\xff" * LIMIT)
    assert plugin_log().count("not publishing key") == 1

    connected_bridge.publish_raw(topic, b"\xff" * LIMIT, retain=False)
    connected_bridge.publish_raw(topic, b"\xff" * LIMIT, retain=False)

    assert plugin_log().count("not publishing key") == 3


def test_the_list_is_sorted_by_topic(connected_bridge, factory):
    for suffix in ("zebra", "alpha", "middle"):
        connected_bridge.publish_raw(ROOT + "/" + suffix, b"\xff" * LIMIT)

    assert [one["topic"] for one in not_published(factory)] == ["alpha", "middle", "zebra"]


def test_json_too_big_is_not_kept_for_the_page(connected_bridge):
    topic = ROOT + "/never_published"

    connected_bridge.publish_json(topic, BIG)

    assert topic not in [name for name, _payload in connected_bridge.last_payloads()]


def test_a_removal_forgets_the_list_with_everything_else(make_bridge, factory, settings):
    bridge, _publisher = connect(make_bridge, factory, settings, BIG)
    assert bridge.not_published() != []

    bridge.forget_everything_published()

    assert bridge.not_published() == []


# ------------------------------------------------------ one `info` for a burst of changes --
#
# Topics become withheld in bursts - at a start the grids are built one after
# another - so the `info` that says so waits for the list to settle.


def infos(factory):
    return [sent.json()["not_published"] for sent in factory.client.all_for(INFO)]


def test_a_burst_of_changes_costs_one_info(connected_bridge, factory):
    factory.client.clear()

    for suffix in ("one", "two", "three", "four"):
        connected_bridge.publish_raw(ROOT + "/" + suffix, b"\xff" * LIMIT)
        conftest.MainLoop.advance(500)
    assert factory.client.all_for(INFO) == []

    conftest.MainLoop.advance(SETTLE)

    assert [[one["topic"] for one in listed] for listed in infos(factory)] == [
        ["four", "one", "three", "two"]
    ]
    # And nothing more follows.
    conftest.MainLoop.advance(SETTLE * 3)
    assert len(factory.client.all_for(INFO)) == 1


def test_a_single_change_is_said_soon_after(connected_bridge, factory):
    factory.client.clear()

    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)

    conftest.MainLoop.advance(SETTLE - 1)
    assert factory.client.all_for(INFO) == []
    conftest.MainLoop.advance(1)
    assert [[one["topic"] for one in listed] for listed in infos(factory)] == [["late"]]


def test_changes_that_keep_coming_do_not_keep_info_back_for_good(connected_bridge, factory,
                                                                 monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock[0])
    factory.client.clear()

    # A change a second: the wait would start again every time.
    for number in range(LATEST // 1000):
        connected_bridge.publish_raw(ROOT + f"/t{number:02d}", b"\xff" * LIMIT)
        assert factory.client.all_for(INFO) == []
        clock[0] += 1.0
        conftest.MainLoop.advance(1000)

    assert len(factory.client.all_for(INFO)) == 1
    assert len(not_published(factory)) == LATEST // 1000


def test_the_latest_is_counted_from_the_first_change_still_unsaid(connected_bridge, factory,
                                                                 monkeypatch):
    """Not from a change that was said long ago, and not from before the last connect."""
    clock = [1000.0]
    monkeypatch.setattr(bridge_module.time, "monotonic", lambda: clock[0])

    connected_bridge.publish_raw(ROOT + "/one", b"\xff" * LIMIT)
    conftest.MainLoop.advance(SETTLE)
    assert [one["topic"] for one in not_published(factory)] == ["one"]

    # A minute later the next change has its own two seconds.
    clock[0] += 60.0
    factory.client.clear()
    connected_bridge.publish_raw(ROOT + "/two", b"\xff" * LIMIT)
    conftest.MainLoop.advance(SETTLE - 1)
    assert factory.client.all_for(INFO) == []
    conftest.MainLoop.advance(1)
    assert len(factory.client.all_for(INFO)) == 1

    # And so has the first change after a connect that found one waiting.
    connected_bridge.publish_raw(ROOT + "/three", b"\xff" * LIMIT)
    clock[0] += LATEST / 1000 - 0.5
    factory.client.fire_connect()
    factory.client.clear()
    connected_bridge.publish_raw(ROOT + "/four", b"\xff" * LIMIT)
    conftest.MainLoop.advance(SETTLE - 1)
    assert factory.client.all_for(INFO) == []
    conftest.MainLoop.advance(1)
    assert [one["topic"] for one in factory.client.last(INFO).json()["not_published"]] == [
        "four", "one", "three", "two"
    ]


def test_a_change_taken_back_before_it_was_said_says_nothing(connected_bridge, factory):
    topic = ROOT + "/late"
    connected_bridge.publish_raw(topic, "small")
    factory.client.clear()

    connected_bridge.publish_raw(topic, b"\xff" * LIMIT)
    connected_bridge.publish_raw(topic, "small")
    conftest.MainLoop.advance(SETTLE)

    assert factory.client.all_for(INFO) == []
    assert connected_bridge.not_published() == []


def test_the_first_info_of_a_connect_does_not_wait(make_bridge, factory, settings):
    """What was withheld before the connect is in the `info` the connect publishes itself."""
    connect(make_bridge, factory, settings, BIG)
    conftest.MainLoop.advance(SETTLE)
    factory.client.clear()

    factory.client.fire_connect()

    # At once, with no turn of the main loop - and a turn later there is nothing to add.
    assert infos(factory) == [[entry("channels", CHANNELS, BIG)]]
    conftest.MainLoop.advance(SETTLE)
    assert len(factory.client.all_for(INFO)) == 1


def test_a_connect_while_a_change_waits_says_it_once(connected_bridge, factory):
    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    factory.client.clear()

    factory.client.fire_connect()
    conftest.MainLoop.advance(SETTLE)

    assert [[one["topic"] for one in listed] for listed in infos(factory)] == [["late"]]


def test_a_change_waiting_when_the_session_drops_is_said_by_the_connect(connected_bridge,
                                                                        factory):
    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    factory.client.fire_disconnect(7)
    factory.client.clear()

    conftest.MainLoop.advance(SETTLE)
    assert factory.client.all_for(INFO) == []

    factory.client.fire_connect()
    assert [[one["topic"] for one in listed] for listed in infos(factory)] == [["late"]]


def test_a_stop_says_a_change_that_was_still_waiting(connected_bridge, factory):
    """The retained `info` must not be the one from before the change."""
    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    factory.client.clear()

    connected_bridge.stop()

    said = [sent for sent in factory.client.published if sent.topic in (INFO, AVAILABILITY)]
    assert [sent.topic for sent in said] == [INFO, AVAILABILITY]
    assert [one["topic"] for one in said[0].json()["not_published"]] == ["late"]
    assert said[1].text == "offline"
    # The wait went with the session.
    conftest.MainLoop.advance(SETTLE)
    assert len(factory.client.all_for(INFO)) == 1


def test_a_stop_with_nothing_waiting_publishes_no_info(connected_bridge, factory):
    factory.client.clear()

    connected_bridge.stop()

    assert factory.client.all_for(INFO) == []


def test_a_reload_says_a_change_that_was_still_waiting(make_bridge, factory, settings):
    """The session that follows may never connect - the plugin switched off, a wrong broker."""
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL)
    old = factory.client
    bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    old.clear()

    settings.enabled.value = False
    bridge.reload()

    said = [sent for sent in old.published if sent.topic in (INFO, AVAILABILITY)]
    assert [sent.topic for sent in said] == [INFO, AVAILABILITY]
    assert [one["topic"] for one in said[0].json()["not_published"]] == ["late"]
    conftest.MainLoop.advance(SETTLE)
    assert len(old.all_for(INFO)) == 1


def test_a_rename_does_not_say_it_under_the_new_name(make_bridge, factory, settings):
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL)
    old = factory.client
    bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    old.clear()

    settings.node_id.value = "vuuno4kse_005302"
    bridge.reload()

    assert [sent.text for sent in old.all_for(INFO)] == [""]
    assert old.all_for("enigma2/vuuno4kse_005302/info") == []


def test_the_wait_stops_with_the_session(connected_bridge):
    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    timer =connected_bridge._not_published_ticker.timer
    assert timer.running

    connected_bridge.stop()

    assert not timer.running


# ------------------------------------------- said is what was published, not what was built --
#
# What `info` last said is recorded where `info` is published. A caller that
# builds one for something else - `cmd/discovery` builds it for the announcement
# - must not leave a change looking as if it had been said.


def test_cmd_discovery_inside_the_wait_does_not_swallow_a_new_entry(connected_bridge, factory):
    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    factory.client.clear()

    factory.client.fire_message(ROOT + "/cmd/discovery", b"")
    assert factory.client.all_for(INFO) == []
    conftest.MainLoop.advance(SETTLE)

    assert [[one["topic"] for one in listed] for listed in infos(factory)] == [["late"]]


def test_cmd_discovery_inside_the_wait_does_not_keep_an_entry_that_left(connected_bridge,
                                                                        factory):
    topic = ROOT + "/late"
    connected_bridge.publish_raw(topic, b"\xff" * LIMIT)
    assert [one["topic"] for one in not_published(factory)] == ["late"]
    factory.client.clear()

    connected_bridge.publish_raw(topic, "small")
    factory.client.fire_message(ROOT + "/cmd/discovery", b"")
    conftest.MainLoop.advance(SETTLE)

    assert infos(factory) == [[]]


def test_measuring_discovery_inside_the_wait_does_not_swallow_the_change(make_bridge, factory,
                                                                         settings):
    """The withheld half of what `cmd/reset`'s measurement must leave alone."""
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL, ha_mode="discovery",
                                 names=["One", "Two"])
    bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    factory.client.clear()

    assert bridge._discovery_too_big() == set()
    conftest.MainLoop.advance(SETTLE)

    assert [[one["topic"] for one in listed] for listed in infos(factory)] == [["late"]]


def test_an_info_the_client_did_not_take_is_not_counted_as_said(connected_bridge, factory):
    """The timer fires with the socket gone; whoever publishes `info` next still has the news."""
    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    factory.client.publish_rc = NO_CONNECTION
    conftest.MainLoop.advance(SETTLE)
    factory.client.publish_rc = 0
    factory.client.clear()

    connected_bridge.stop()

    assert [[one["topic"] for one in listed] for listed in infos(factory)] == [["late"]]


def test_an_info_too_big_for_a_packet_does_not_ask_for_itself_again(connected_bridge, factory,
                                                                    monkeypatch, plugin_log):
    """`info` cannot announce that `info` is missing; trying would start the wait for ever."""
    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", 200)
    factory.client.clear()

    conftest.MainLoop.advance(SETTLE)
    assert plugin_log().count("not publishing info:") == 1
    timer = connected_bridge._not_published_ticker.timer
    assert not timer.running

    conftest.MainLoop.advance(SETTLE * 5)
    assert not timer.running
    assert [sent for sent in factory.client.all_for(INFO) if sent.text] == []


def test_the_wait_is_one_shot(connected_bridge):
    """A timer that repeated would wake the receiver every two seconds for nothing."""
    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)

    assert connected_bridge._not_published_ticker.timer.started == (2000, True)


def test_an_image_without_a_timer_says_it_at_once(connected_bridge, factory, monkeypatch):
    monkeypatch.delattr(conftest.enigma, "eTimer")
    factory.client.clear()

    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)

    assert [[one["topic"] for one in listed] for listed in infos(factory)] == [["late"]]


def test_a_stop_says_offline_although_the_waiting_info_raises(connected_bridge, factory,
                                                              monkeypatch, plugin_log):
    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    client = factory.client
    client.clear()
    publish_json = connected_bridge.publish_json

    def failing(topic, *arguments, **options):
        if topic == INFO:
            raise RuntimeError("no info today")
        return publish_json(topic, *arguments, **options)

    monkeypatch.setattr(connected_bridge, "publish_json", failing)

    connected_bridge.stop()

    assert client.last(AVAILABILITY).text == "offline"
    assert client.client_disconnected()
    assert connected_bridge.client is None
    assert "no info today" in plugin_log()


def test_a_reload_after_everything_was_retracted_publishes_no_info(connected_bridge, factory):
    """A downgrade that failed after its retraction reloads; the old session stays silent.

    The retraction took `info` back with the rest, so there is no `info` of
    this session on the broker for a waiting change to correct.
    """
    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    assert [one["topic"] for one in not_published(factory)] == ["late"]
    old = factory.client
    connected_bridge.forget_everything_published()
    old.clear()

    connected_bridge.reload()

    assert old.all_for(INFO) == []


def test_reset_without_a_session_says_it_retracted_nothing(make_bridge, factory, settings,
                                                           plugin_log):
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL)
    bridge.stop()
    bridge.state.remember(CHANNELS)

    assert bridge.reset_retained() == 0
    assert "reset: retracting 0 retained topic(s)" in plugin_log()


# ----------------------------------------------------------------- the delta review --


def test_a_reload_goes_through_although_the_waiting_info_raises(make_bridge, factory, settings,
                                                                monkeypatch, plugin_log):
    """The old client is stopped, the saved settings are applied, a new session starts."""
    bridge, _publisher = connect(make_bridge, factory, settings, SMALL)
    bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    old = factory.client
    publish_json = bridge.publish_json

    def failing(topic, *arguments, **options):
        if topic == INFO:
            raise RuntimeError("no info today")
        return publish_json(topic, *arguments, **options)

    monkeypatch.setattr(bridge, "publish_json", failing)

    settings.host.value = "10.0.0.6"
    bridge.reload()

    assert old.client_disconnected()
    assert factory.client is not old
    assert factory.client.connect_calls[0][0] == "10.0.0.6"
    assert bridge.running and bridge.client is not None
    assert "no info today" in plugin_log()


def test_publish_info_records_the_payload_it_was_handed(connected_bridge, factory):
    """Built before a change and published after it: the change is still news."""
    info = connected_bridge.build_info()
    connected_bridge.publish_raw(ROOT + "/late", b"\xff" * LIMIT)
    factory.client.clear()

    connected_bridge.publish_info(info)
    conftest.MainLoop.advance(SETTLE)

    assert [[one["topic"] for one in listed] for listed in infos(factory)] == [[], ["late"]]
