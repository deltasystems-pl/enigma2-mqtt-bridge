"""Discovery mode on a receiver with a very large channel list: the device survives.

The device's discovery payload carries every channel name as the options of its
channel select, so it is the one discovery payload that can pass the packet
bound. Withheld, a first install has no device at all; so the select is what is
given up, by name when Home Assistant already had it, and it returns when the
names fit again.
"""

import test_channel_topics
from conftest import FIRST_BOUQUET, POLSAT, SECOND_BOUQUET
from test_packet_size import DEVICE, INFO, LIMIT, NODE, ROOT, SMALL, Padding, connect

from MQTTBridge import bridge as bridge_module

OVERHEAD = 1 + 4 + 2

# 600 names of two kilobytes: 1.2 MB of options, and nothing else of any size.
MANY = [f"{number:04d} " + "n" * 2000 for number in range(600)]
FEW = ["One", "Two"]

LEFT_OUT = "the channel select is left out of the discovery payload"


def packet(sent):
    return OVERHEAD + len(sent.topic.encode("utf-8")) + len(sent.payload or b"")


def components(factory):
    return factory.client.last(DEVICE).json()["cmps"]


def not_published(factory):
    return factory.client.last(INFO).json()["not_published"]


def discovery_box(make_bridge, factory, settings, names):
    return connect(make_bridge, factory, settings, SMALL, ha_mode="discovery", names=names)


def test_a_device_payload_too_big_is_published_without_the_channel_select(make_bridge, factory,
                                                                          settings):
    bridge, _publisher = discovery_box(make_bridge, factory, settings, MANY)

    sent = factory.client.last(DEVICE)
    assert sent is not None and sent.retain is True
    assert packet(sent) <= LIMIT
    found = components(factory)
    assert "channel_select" not in found
    # Every other entity is announced in full.
    assert {"restart_gui", "refresh_discovery", "uptime"} <= set(found)
    assert all(len(component) > 1 for component in found.values())
    assert sent.json()["dev"]["ids"] == [NODE]
    assert not_published(factory) == []
    assert bridge.state.knows(DEVICE)
    assert "channel_select" not in bridge.state.component_keys
    assert all(packet(one) <= LIMIT for one in factory.client.published)


def test_with_a_list_that_fits_the_select_is_announced_as_ever(make_bridge, factory, settings,
                                                               plugin_log):
    discovery_box(make_bridge, factory, settings, FEW)

    assert components(factory)["channel_select"]["ops"] == FEW
    assert LEFT_OUT not in plugin_log()


def test_a_select_that_was_announced_is_removed_by_name(make_bridge, factory, settings):
    """Left out of a republished payload, Home Assistant would keep it with its old options."""
    bridge, publisher = discovery_box(make_bridge, factory, settings, FEW)
    assert bridge.state.component_keys["channel_select"] == "select"
    whole = components(factory)
    factory.client.clear()

    publisher.names = MANY
    bridge.publish_discovery()

    sent = factory.client.all_for(DEVICE)
    assert len(sent) == 1 and sent[0].text
    found = sent[0].json()["cmps"]
    assert found["channel_select"] == {"p": "select"}
    # Nothing else is touched by it.
    assert {key: value for key, value in found.items() if key != "channel_select"} == {
        key: value for key, value in whole.items() if key != "channel_select"
    }
    assert "channel_select" not in bridge.state.component_keys

    # Removed once: the payloads after it do not mention the select at all.
    factory.client.clear()
    factory.client.fire_connect()
    assert "channel_select" not in components(factory)
    assert all(one.text for one in factory.client.all_for(DEVICE))


def test_the_select_comes_back_when_the_names_fit(make_bridge, factory, settings):
    bridge, publisher = discovery_box(make_bridge, factory, settings, MANY)
    assert "channel_select" not in components(factory)

    publisher.names = FEW
    bridge.publish_discovery()

    select = components(factory)["channel_select"]
    assert select["ops"] == FEW
    assert select["cmd_t"] == ROOT + "/cmd/zap"
    assert bridge.state.component_keys["channel_select"] == "select"


def test_the_switch_is_at_the_bound_itself(make_bridge, factory, settings, monkeypatch):
    """A payload of exactly the bound keeps its select; one byte less room and it goes."""
    discovery_box(make_bridge, factory, settings, FEW)
    size = packet(factory.client.last(DEVICE))

    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", size)
    factory.client.fire_connect()
    assert components(factory)["channel_select"]["ops"] == FEW

    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", size - 1)
    factory.client.fire_connect()
    assert components(factory)["channel_select"] == {"p": "select"}
    assert packet(factory.client.last(DEVICE)) < size


def test_it_is_logged_when_it_starts_and_not_at_every_connect(make_bridge, factory, settings,
                                                              plugin_log):
    bridge, publisher = discovery_box(make_bridge, factory, settings, MANY)
    factory.client.fire_connect()
    factory.client.fire_connect()
    bridge.publish_discovery()

    line = (
        LEFT_OUT + ": with 600 channel name(s) it would be over the " + str(LIMIT)
        + " byte packet limit"
    )
    assert plugin_log().count(line) == 1
    assert "WARNING" in [row for row in plugin_log().splitlines() if line in row][0]
    # The reason, never the names.
    assert "n" * 64 not in plugin_log()

    publisher.names = FEW
    bridge.publish_discovery()
    publisher.names = MANY
    bridge.publish_discovery()
    assert plugin_log().count(LEFT_OUT) == 2


def test_a_reconnect_without_the_select_sends_nothing_over_the_bound(make_bridge, factory,
                                                                     settings):
    discovery_box(make_bridge, factory, settings, MANY)
    factory.client.clear()

    factory.client.fire_connect()
    factory.client.fire_connect()

    sent = factory.client.all_for(DEVICE)
    assert len(sent) == 2
    assert all("channel_select" not in one.json()["cmps"] for one in sent)
    assert all(packet(one) <= LIMIT for one in factory.client.published)
    assert not_published(factory) == []


def test_a_payload_too_big_without_the_select_is_withheld_as_before(make_bridge, factory,
                                                                    settings, monkeypatch,
                                                                    plugin_log):
    padding = Padding(monkeypatch)
    padding.on = False
    bridge, publisher = discovery_box(make_bridge, factory, settings, FEW)
    factory.client.clear()

    padding.on = True
    publisher.names = MANY
    bridge.publish_discovery()

    # Neither a payload nor a retraction, and what was announced is still remembered.
    assert factory.client.all_for(DEVICE) == []
    assert [one["topic"] for one in not_published(factory)] == [DEVICE]
    assert bridge.state.component_keys["channel_select"] == "select"
    # The size is of the smaller payload - how far it is from fitting.
    assert not_published(factory)[0]["bytes"] < LIMIT + 10000
    # Nothing was published without the select, so the log does not say it was.
    assert LEFT_OUT not in plugin_log()

    # Once the rest fits, the select is removed by name after all.
    padding.on = False
    bridge.publish_discovery()
    assert components(factory)["channel_select"] == {"p": "select"}
    assert not_published(factory) == []


def test_reset_replaces_a_device_payload_that_fits_without_its_select(make_bridge, factory,
                                                                     settings):
    """It can be put back, so a reset has no reason to leave it alone."""
    bridge, publisher = discovery_box(make_bridge, factory, settings, FEW)
    publisher.names = MANY
    factory.client.clear()

    bridge.reset_retained()

    assert [bool(one.text) for one in factory.client.all_for(DEVICE)] == [False, True]
    assert "channel_select" not in components(factory)
    assert bridge.state.knows(DEVICE)


def test_a_real_channel_list_too_big_for_the_select_keeps_every_other_entity(
    make_bridge, factory, settings, receiver, monkeypatch
):
    """The whole receiver, at a bound two modest bouquets pass together."""
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", test_channel_topics.LOW)
    test_channel_topics.fill(receiver, FIRST_BOUQUET, 300, "A")
    test_channel_topics.fill(receiver, SECOND_BOUQUET, 300, "B")
    receiver.service_center.contents[SECOND_BOUQUET].append((POLSAT, "Polsat Sport"))
    bridge = test_channel_topics.start(
        make_bridge, factory, settings, receiver, ha_mode="discovery"
    )

    assert len(bridge.channel_options()) == 601
    found = components(factory)
    assert "channel_select" not in found
    assert {"power", "channel", "program", "volume", "mute", "screen", "uptime"} <= set(found)
    assert not_published(factory) == []
    assert all(packet(one) <= test_channel_topics.LOW for one in factory.client.published)

    # The select is gone; what it did still works.
    factory.client.fire_message(ROOT + "/cmd/zap", b'{"name": "Polsat Sport"}')
    assert receiver.nav.played == [POLSAT]
