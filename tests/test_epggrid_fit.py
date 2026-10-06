"""An EPG grid that does not fit one packet: cut to the events that do, and only then withheld.

The grid of a large bouquet is the payload most likely to pass the packet bound,
and withholding it leaves a consumer with no guide at all for that bouquet. So
the events per channel are lowered until the packet fits, the payload says how
many it was built with, and the fitted payload is the one a connect sends again.
"""

import pytest
from conftest import TVN, TVP1, Event

from MQTTBridge import bridge as bridge_module

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
INFO = ROOT + "/info"
ULUBIONE = ROOT + "/epg_grid/ulubione_tv"
SPORT = ROOT + "/epg_grid/sport_hd"

OVERHEAD = 1 + 4 + 2

# Titles this wide make the grid's size a matter of how many events it has:
# two channels, about 10 kB an event.
WIDE = 10000

# Three events on each of the two channels fit this; four do not.
LOW = 70000


def packet(sent):
    return OVERHEAD + len(sent.topic.encode("utf-8")) + len(sent.payload or b"")


def programmes(count, tag, width=WIDE):
    return [
        Event(100 + number, 1789459200 + number * 1800, 1800, f"{tag}{number:02d} " + "t" * width)
        for number in range(count)
    ]


def guide(receiver, count=4, width=WIDE):
    """Give both channels of the first bouquet `count` programmes with wide titles."""
    receiver.epg.events[TVP1] = programmes(count, "a", width)
    receiver.epg.events[TVN] = programmes(count, "b", width)


def size_with_all(receiver):
    """Less than the uncut grid needs: the titles of both channels and nothing else."""
    return sum(len(event.title) for sref in (TVP1, TVN) for event in receiver.epg.events[sref])


def generate(bridge):
    publisher = bridge.publisher("epg_grid")
    publisher.regenerate()
    for _ in range(100):
        if not publisher._queue and publisher._current is None:
            break
        publisher._step.timer.fire()
    return publisher


def titles(payload):
    return [[event["title"][:3] for event in channel["events"]] for channel in payload["channels"]]


def not_published(factory):
    return factory.client.last(INFO).json()["not_published"]


def start(make_bridge, factory, settings, receiver, events):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.ha_mode.value = "integration"
    settings.epg_grid_events.value = events
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    generate(bridge)
    return bridge


def size_with(factory, events):
    """The packet of the first bouquet's grid as last published, cut by hand to `events`."""
    sent = factory.client.last(ULUBIONE)
    payload = sent.json()
    payload["events_per_channel"] = events
    for channel in payload["channels"]:
        channel["events"] = channel["events"][:events]
    return OVERHEAD + len(ULUBIONE) + len(bridge_module._encoded(payload).encode("utf-8"))


# ------------------------------------------------------------------ the member --


def test_every_grid_says_how_many_events_it_was_built_with(live_bridge, factory):
    assert factory.client.last(ULUBIONE).json()["events_per_channel"] == 4
    # Also where no channel has an event at all: it is the count, not a tally.
    assert factory.client.last(SPORT).json()["events_per_channel"] == 4


def test_it_is_the_setting_when_nothing_was_cut(make_bridge, factory, settings, receiver):
    start(make_bridge, factory, settings, receiver, events=2)

    payload = factory.client.last(ULUBIONE).json()
    assert payload["events_per_channel"] == 2
    assert len(payload["channels"][0]["events"]) == 2


# --------------------------------------------------------------------- the cut --


def test_a_grid_too_big_is_cut_to_the_most_events_that_fit(live_bridge, factory, receiver,
                                                           monkeypatch):
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", LOW)
    guide(receiver)
    receiver.epg.queries = []
    factory.client.clear()

    generate(live_bridge)

    sent = factory.client.all_for(ULUBIONE)
    assert len(sent) == 1
    assert sent[0].retain is True
    payload = sent[0].json()
    assert payload["events_per_channel"] == 3
    # Each channel keeps its earliest programmes, in order.
    assert titles(payload) == [["a00", "a01", "a02"], ["b00", "b01", "b02"]]
    assert packet(sent[0]) <= LOW < size_with_all(receiver)
    # Nothing was withheld, so `info` had nothing new to say.
    assert live_bridge.not_published() == []
    assert factory.client.all_for(INFO) == []
    assert all(packet(one) <= LOW for one in factory.client.published)
    # Cut from the lists already built: one query a bouquet, as for any pass.
    assert len(receiver.epg.queries) == 2


def test_the_other_bouquets_keep_their_count(live_bridge, factory, receiver, monkeypatch):
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", LOW)
    guide(receiver)
    generate(live_bridge)
    factory.client.clear()

    factory.client.fire_connect()

    assert factory.client.last(ULUBIONE).json()["events_per_channel"] == 3
    assert factory.client.last(SPORT).json()["events_per_channel"] == 4


@pytest.mark.parametrize("fits", [19, 13, 8, 2, 1])
def test_the_cut_is_the_largest_count_that_fits_exactly(make_bridge, factory, settings, receiver,
                                                        monkeypatch, fits):
    """A bound of exactly the packet with `fits` events takes them; one byte less, one fewer."""
    guide(receiver, count=20, width=1000)
    bridge = start(make_bridge, factory, settings, receiver, events=20)
    assert factory.client.last(ULUBIONE).json()["events_per_channel"] == 20
    exact = size_with(factory, fits)

    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", exact)
    bridge.forget_published()
    generate(bridge)
    sent = factory.client.last(ULUBIONE)
    assert sent.json()["events_per_channel"] == fits
    assert packet(sent) == exact
    assert [len(channel["events"]) for channel in sent.json()["channels"]] == [fits, fits]

    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", exact - 1)
    factory.client.clear()
    # A bound does not move on a receiver; here it did, under a payload that
    # has not changed, so the comparison with what was sent is set aside.
    bridge.forget_published()
    generate(bridge)
    sent = factory.client.last(ULUBIONE)
    if fits == 1:
        # Nothing left to cut: withheld, and the copy that fitted is taken back.
        assert sent.text == ""
    else:
        assert sent.json()["events_per_channel"] == fits - 1
        assert packet(sent) < exact


def test_a_grid_is_whole_again_when_it_fits(live_bridge, factory, receiver, monkeypatch):
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", LOW)
    guide(receiver)
    generate(live_bridge)
    assert factory.client.last(ULUBIONE).json()["events_per_channel"] == 3

    guide(receiver, width=100)
    generate(live_bridge)

    payload = factory.client.last(ULUBIONE).json()
    assert payload["events_per_channel"] == 4
    assert [len(channel["events"]) for channel in payload["channels"]] == [4, 4]


def test_only_a_grid_too_big_with_one_event_is_withheld(live_bridge, factory, receiver,
                                                        monkeypatch, plugin_log):
    """Two channels at 10 kB an event: one each is 20 kB, and that is the least there is."""
    assert factory.client.last(ULUBIONE).json()["channels"]
    guide(receiver)

    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", 25000)
    generate(live_bridge)
    assert factory.client.last(ULUBIONE).json()["events_per_channel"] == 1
    assert not_published(factory) == []
    assert "not publishing" not in plugin_log()

    # The guide moves on, and now one event on each channel is too much.
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", 15000)
    guide(receiver, width=WIDE + 1)
    factory.client.clear()
    generate(live_bridge)

    # The copy that fitted is taken back, and `info` names the topic.
    assert [(sent.text, sent.retain) for sent in factory.client.all_for(ULUBIONE)] == [("", True)]
    listed = not_published(factory)
    assert [one["topic"] for one in listed] == ["epg_grid/ulubione_tv"]
    # The size is of the smallest grid there was to send, not of the whole one.
    assert 20000 < listed[0]["bytes"] < 21000
    assert plugin_log().count("not publishing epg_grid/ulubione_tv") == 1

    # A connect in that state hands the client nothing over the bound.
    factory.client.clear()
    factory.client.fire_connect()
    assert factory.client.all_for(ULUBIONE) == []
    assert all(packet(sent) <= 15000 for sent in factory.client.published)

    # And it is published again as soon as one event on each channel fits.
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", 25000)
    generate(live_bridge)
    assert factory.client.last(ULUBIONE).json()["events_per_channel"] == 1
    assert not_published(factory) == []


def test_the_cut_is_logged_when_it_starts_and_when_it_moves(live_bridge, receiver, monkeypatch,
                                                            plugin_log):
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", LOW)
    guide(receiver)

    generate(live_bridge)
    generate(live_bridge)

    line = "epg grid: Ulubione TV is cut from 4 to 3 event(s) per channel to fit one packet"
    assert plugin_log().count(line) == 1
    assert "WARNING" in [row for row in plugin_log().splitlines() if line in row][0]
    assert plugin_log().count("is cut from") == 1

    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", 45000)
    generate(live_bridge)
    generate(live_bridge)
    assert plugin_log().count("is cut from 4 to 2 event(s)") == 1

    # Whole again, then cut again: that is a new cut, and says so.
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", 1000000)
    generate(live_bridge)
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", 45000)
    generate(live_bridge)
    assert plugin_log().count("is cut from 4 to 2 event(s)") == 2


# --------------------------------------------------------------------- the cost --


def watch_encoding(monkeypatch):
    """Record the length of every JSON text the bridge builds."""
    built = []
    encode = bridge_module._encoded

    def watching(payload):
        text = encode(payload)
        built.append((len(text), payload))
        return text

    monkeypatch.setattr(bridge_module, "_encoded", watching)
    return built


def test_a_connect_sends_the_fitted_grid_and_fits_nothing_again(live_bridge, factory, receiver,
                                                                monkeypatch):
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", LOW)
    guide(receiver)
    generate(live_bridge)
    built = watch_encoding(monkeypatch)
    measured = []
    measure = live_bridge.measure
    monkeypatch.setattr(
        live_bridge, "measure",
        lambda suffix, payload: measured.append(suffix) or measure(suffix, payload),
    )
    factory.client.clear()

    factory.client.fire_connect()
    factory.client.fire_connect()

    sent = factory.client.all_for(ULUBIONE)
    assert [one.json()["events_per_channel"] for one in sent] == [3, 3]
    assert all(packet(one) <= LOW for one in factory.client.published)
    # Nothing over the bound was even built, and no grid was measured again.
    assert built and max(length for length, _payload in built) < LOW
    assert [suffix for suffix in measured if suffix.startswith("epg_grid/")] == []


def test_a_grid_that_fits_costs_no_more_encoding_than_before(live_bridge, factory, receiver,
                                                             monkeypatch):
    """Measured and published from the same JSON; the second is the change comparison's."""
    receiver.epg.events[TVP1] = [Event(1, 10, 20, "Something else")]
    built = watch_encoding(monkeypatch)

    generate(live_bridge)

    grids = [payload for _length, payload in built
             if isinstance(payload, dict) and payload.get("bouquet") == "Ulubione TV"]
    assert len(grids) == 2
    assert grids[0]["events_per_channel"] == 4
    assert "generated" not in grids[1]


def test_a_cut_is_found_in_a_few_encodings(make_bridge, factory, settings, receiver,
                                           monkeypatch):
    """With the setting at its highest, 20: by halves, not one count at a time."""
    guide(receiver, count=20, width=1000)
    bridge = start(make_bridge, factory, settings, receiver, events=20)
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", size_with(factory, 3))
    measured = []
    measure = bridge.measure
    monkeypatch.setattr(
        bridge, "measure",
        lambda suffix, payload: measured.append((suffix, payload["events_per_channel"]))
        or measure(suffix, payload),
    )

    generate(bridge)

    assert factory.client.last(ULUBIONE).json()["events_per_channel"] == 3
    tried = [count for suffix, count in measured if suffix == "epg_grid/ulubione_tv"]
    assert tried[0] == 20
    assert len(tried) <= 6
