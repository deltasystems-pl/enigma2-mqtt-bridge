"""A payload too big for one MQTT packet: what is sent instead, and what says so.

A broker closes the connection on a packet over its limit, the will says
`offline`, the client reconnects and the snapshot sends the same payload again.
So the assertions here are about what the client is handed - an oversized
payload never, on any connect - and about `info.not_published`, which is how a
consumer learns that a topic is missing on purpose.
"""

import conftest
from conftest import TVP1, Event

from MQTTBridge import bridge as bridge_module
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
SPORT = ROOT + "/epg_grid/sport_hd"

# Written out rather than read from the module: the number is the promise.
LIMIT = 1000000
# The fixed header at its longest, and the two bytes of the topic's length.
OVERHEAD = 1 + 4 + 2


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
    return factory.client.last(INFO).json().get("not_published")


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
    factory.client.clear()

    publisher.publish("channels", SMALL)

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
                                                                    settings):
    """Every channel name is an option of the select, so the device payload grows with them."""
    names = [f"{number:04d} " + "n" * 2000 for number in range(600)]
    connect(make_bridge, factory, settings, SMALL, ha_mode="discovery", names=names)

    assert factory.client.all_for(DEVICE) == []
    assert factory.client.last(CHANNELS).json() == SMALL
    listed = not_published(factory)
    assert [one["topic"] for one in listed] == [DEVICE]
    assert listed[0]["bytes"] > LIMIT
    assert listed[0]["limit"] == LIMIT


def test_a_discovery_payload_on_the_broker_is_never_retracted(make_bridge, factory, settings,
                                                              plugin_log):
    """An empty retained device payload deletes the device and every entity it has."""
    bridge, publisher = connect(make_bridge, factory, settings, SMALL, ha_mode="discovery",
                                names=["One", "Two"])
    assert factory.client.last(DEVICE).json()
    assert bridge.state.knows(DEVICE)
    factory.client.clear()

    publisher.names = [f"{number:04d} " + "n" * 2000 for number in range(600)]
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
