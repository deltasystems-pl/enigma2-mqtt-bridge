"""The channel list per bouquet: `channels/<bouquet_slug>`, and what `channels` says of it.

`channels` grows with the sum of every bouquet, so on a receiver with a very
large list its packet passes the bound and, with nothing else to send, it would
not be published at all. Each bouquet's list is therefore on a topic of its own
as well, and `channels` drops its lists when they do not fit together. The
assertions are about what is on the wire: the receiver's own copy stays whole,
because `cmd/zap` by name and the grid are answered from it.
"""

import json

import conftest
from conftest import FIRST_BOUQUET, POLSAT, SECOND_BOUQUET, TVN, TVP1

from MQTTBridge import bridge as bridge_module
from MQTTBridge import channels as channels_module
from MQTTBridge.discovery import StateStore

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
INFO = ROOT + "/info"
CHANNELS = ROOT + "/channels"
ULUBIONE = ROOT + "/channels/ulubione_tv"
SPORT = ROOT + "/channels/sport_hd"
GRID = ROOT + "/epg_grid/ulubione_tv"

# Written out rather than read from the module: the number is the promise.
LIMIT = 1000000
OVERHEAD = 1 + 4 + 2

# A bound small enough that two modest bouquets pass it together and neither
# does alone, and large enough for everything else the plugin publishes.
LOW = 60000


def packet(sent):
    """The bytes the plugin counts for something the client was handed."""
    return OVERHEAD + len(sent.topic.encode("utf-8")) + len(sent.payload or b"")


def fill(receiver, bouquet, count, tag, width=80):
    """Give one bouquet `count` services with names `width` letters wide."""
    receiver.service_center.contents[bouquet] = [
        (f"1:0:19:{number:X}:{tag}:1:C00000:0:0:0:", f"{tag} {number:05d} " + "n" * width)
        for number in range(count)
    ]


def start(make_bridge, factory, settings, receiver, **values):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.ha_mode.value = "integration"
    for name, value in values.items():
        getattr(settings, name).value = value
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    return conftest.settle(bridge)


def finish_grid(bridge):
    """Turn the grid's timer until its pass is over: 300 channels are 75 turns a bouquet."""
    grid = bridge.publisher("epg_grid")
    for _ in range(1000):
        if not grid._queue and grid._current is None:
            break
        grid._step.timer.fire()


def two_bouquets_too_big_together(receiver, monkeypatch):
    """Lower the bound and fill both bouquets: each list fits it, the two together do not."""
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", LOW)
    fill(receiver, FIRST_BOUQUET, 300, "A")
    fill(receiver, SECOND_BOUQUET, 300, "B")


def pinned_clock(monkeypatch, now=1789459200):
    clock = [float(now)]
    monkeypatch.setattr(channels_module.time, "time", lambda: clock[0])
    return clock


def not_published(factory):
    return factory.client.last(INFO).json()["not_published"]


def lists(factory):
    return [bouquet["channels"] for bouquet in factory.client.last(CHANNELS).json()["bouquets"]]


# ------------------------------------------------------------ one topic a bouquet --


def test_each_bouquet_has_its_list_on_a_topic_of_its_own(live_bridge, factory):
    sent = factory.client.last(ULUBIONE)
    assert sent.retain is True
    payload = sent.json()
    assert set(payload) == {"bouquet", "sref", "generated", "channels"}
    assert payload["bouquet"] == "Ulubione TV"
    assert payload["sref"] == FIRST_BOUQUET
    assert isinstance(payload["generated"], int)
    assert payload["channels"] == [
        {"sref": TVP1, "name": "TVP 1 HD"},
        {"sref": TVN, "name": "TVN HD"},
    ]
    assert factory.client.last(SPORT).json()["channels"] == [
        {"sref": POLSAT, "name": "Polsat Sport"}
    ]


def test_the_topic_carries_what_channels_carries_for_that_bouquet(live_bridge, factory):
    whole = factory.client.last(CHANNELS).json()
    for bouquet in whole["bouquets"]:
        own = factory.client.last(ROOT + "/channels/" + bouquet["slug"]).json()
        assert own["bouquet"] == bouquet["name"]
        assert own["sref"] == bouquet["sref"]
        assert own["channels"] == bouquet["channels"]
        assert own["generated"] == whole["generated"]


def test_a_bouquet_nobody_edited_is_not_republished(live_bridge, factory, receiver, monkeypatch):
    """`generated` is stamped at every walk; alone it is not a change."""
    clock = pinned_clock(monkeypatch)
    publisher = live_bridge.publisher("channels")
    publisher.refresh()
    factory.client.clear()

    receiver.service_center.contents[SECOND_BOUQUET].append((TVN, "TVN HD"))
    clock[0] += 60
    publisher.refresh()

    assert factory.client.all_for(ULUBIONE) == []
    changed = factory.client.all_for(SPORT)
    assert len(changed) == 1
    assert changed[0].json()["generated"] == 1789459200 + 60
    # `channels` itself says when the bouquets were walked, as it always did.
    assert factory.client.last(CHANNELS).json()["generated"] == 1789459200 + 60


def test_the_first_walk_after_a_connect_is_compared_too(live_bridge, factory, monkeypatch):
    """What the snapshot records as sent is what the next walk is judged against."""
    clock = pinned_clock(monkeypatch)
    factory.client.fire_connect()
    assert factory.client.last(ULUBIONE).json()["channels"]
    factory.client.clear()

    clock[0] += 60
    live_bridge.publisher("channels").refresh()

    assert factory.client.all_for(ULUBIONE) == []
    assert factory.client.all_for(SPORT) == []


def test_the_topics_come_back_on_a_reconnect(live_bridge, factory):
    factory.client.clear()
    factory.client.fire_connect()

    assert factory.client.last(ULUBIONE).json()["bouquet"] == "Ulubione TV"
    assert factory.client.last(SPORT).json()["bouquet"] == "Sport (HD)"


def test_the_slug_is_the_one_the_grid_uses(live_bridge):
    assert live_bridge.publisher("channels").published_slugs == ["ulubione_tv", "sport_hd"]
    assert live_bridge.publisher("epg_grid").published_slugs == ["ulubione_tv", "sport_hd"]


def test_two_bouquets_with_one_slug_share_a_topic_and_the_second_wins(live_bridge, factory,
                                                                      receiver):
    receiver.service_center.contents[conftest.BOUQUET_ROOT] = [
        (FIRST_BOUQUET, "Kino HD"),
        (SECOND_BOUQUET, "Kino (HD)"),
    ]
    factory.client.clear()

    live_bridge.publisher("channels").refresh()

    sent = factory.client.all_for(ROOT + "/channels/kino_hd")
    assert len(sent) == 1
    assert sent[0].json()["bouquet"] == "Kino (HD)"
    assert sent[0].json()["channels"] == [{"sref": POLSAT, "name": "Polsat Sport"}]
    bouquets = factory.client.last(CHANNELS).json()["bouquets"]
    assert [bouquet["slug"] for bouquet in bouquets] == ["kino_hd", "kino_hd"]
    assert live_bridge.publisher("channels").published_slugs == ["kino_hd"]


def test_a_name_that_leaves_no_slug_has_no_topic(live_bridge, factory, receiver):
    receiver.service_center.contents[conftest.BOUQUET_ROOT] = [
        (FIRST_BOUQUET, "***"),
        (SECOND_BOUQUET, "Sport (HD)"),
    ]
    factory.client.clear()

    live_bridge.publisher("channels").refresh()

    assert ROOT + "/channels/" not in factory.client.topics()
    bouquets = factory.client.last(CHANNELS).json()["bouquets"]
    assert [bouquet["slug"] for bouquet in bouquets] == ["", "sport_hd"]
    assert live_bridge.publisher("channels").published_slugs == ["sport_hd"]


# --------------------------------------------------------------------- channels --


def test_channels_says_where_each_list_is_and_how_long(live_bridge, factory):
    payload = factory.client.last(CHANNELS).json()
    assert payload["embedded"] is True
    assert [(b["name"], b["slug"], b["count"]) for b in payload["bouquets"]] == [
        ("Ulubione TV", "ulubione_tv", 2),
        ("Sport (HD)", "sport_hd", 1),
    ]
    # The lists are where they have always been.
    assert [len(found) for found in lists(factory)] == [2, 1]


def test_the_receivers_own_bouquets_gain_nothing(live_bridge):
    """`slug` and `count` are on the wire; the grid and the lookups read the plain list."""
    publisher = live_bridge.publisher("channels")
    assert set(publisher.payload()["bouquets"][0]) == {"name", "sref", "slug", "count", "channels"}
    for bouquet in publisher.bouquets:
        assert set(bouquet) == {"name", "sref", "channels"}


def test_lists_that_do_not_fit_together_leave_channels(make_bridge, factory, settings, receiver):
    """At the real bound: 720 kB a bouquet, so each fits a packet and the two do not."""
    fill(receiver, FIRST_BOUQUET, 3000, "A", width=180)
    fill(receiver, SECOND_BOUQUET, 3000, "B", width=180)
    bridge = start(make_bridge, factory, settings, receiver, epg_grid_events=0)

    payload = factory.client.last(CHANNELS).json()
    assert payload["embedded"] is False
    assert [b["channels"] for b in payload["bouquets"]] == [None, None]
    assert [(b["name"], b["slug"], b["count"]) for b in payload["bouquets"]] == [
        ("Ulubione TV", "ulubione_tv", 3000),
        ("Sport (HD)", "sport_hd", 3000),
    ]
    assert packet(factory.client.last(CHANNELS)) < 1000

    # Every list is still on the broker, each in a packet of its own.
    for topic, tag in ((ULUBIONE, "A"), (SPORT, "B")):
        sent = factory.client.last(topic)
        assert 700000 < packet(sent) <= LIMIT
        own = sent.json()["channels"]
        assert len(own) == 3000
        assert own[0]["name"].startswith(tag + " 00000 ")
    assert all(packet(sent) <= LIMIT for sent in factory.client.published)
    assert not_published(factory) == []
    assert bridge.state.knows(CHANNELS)


def test_the_switch_is_at_the_bound_itself(make_bridge, factory, settings, receiver, monkeypatch):
    """A packet of exactly the bound is sent whole; one byte less room and the lists go."""
    start(make_bridge, factory, settings, receiver, epg_grid_events=0)
    whole = factory.client.last(CHANNELS)
    assert whole.json()["embedded"] is True
    size = packet(whole)

    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", size)
    start(make_bridge, factory, settings, receiver, epg_grid_events=0)
    assert factory.client.last(CHANNELS).json()["embedded"] is True
    assert packet(factory.client.last(CHANNELS)) == size

    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", size - 1)
    start(make_bridge, factory, settings, receiver, epg_grid_events=0)
    assert factory.client.last(CHANNELS).json()["embedded"] is False
    assert lists(factory) == [None, None]


def test_it_is_all_or_none(make_bridge, factory, settings, receiver, monkeypatch):
    """One small bouquet beside a big one does not keep its list in `channels`."""
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", LOW)
    fill(receiver, FIRST_BOUQUET, 300, "A")
    fill(receiver, SECOND_BOUQUET, 150, "B")
    start(make_bridge, factory, settings, receiver)

    assert lists(factory) == [None, None]
    assert len(factory.client.last(SPORT).json()["channels"]) == 150


def test_the_lists_come_back_when_they_fit_again(make_bridge, factory, settings, receiver,
                                                  monkeypatch, plugin_log):
    two_bouquets_too_big_together(receiver, monkeypatch)
    bridge = start(make_bridge, factory, settings, receiver)
    assert factory.client.last(CHANNELS).json()["embedded"] is False
    line = "channels is published without the lists"
    assert plugin_log().count(line) == 1
    assert "WARNING" in [row for row in plugin_log().splitlines() if line in row][0]

    # Still too big at the next walk: said once, not at every walk.
    bridge.publisher("channels").refresh()
    assert factory.client.last(CHANNELS).json()["embedded"] is False
    assert plugin_log().count(line) == 1

    fill(receiver, SECOND_BOUQUET, 3, "B")
    factory.client.clear()
    bridge.publisher("channels").refresh()

    payload = factory.client.last(CHANNELS).json()
    assert payload["embedded"] is True
    assert [len(found) for found in lists(factory)] == [300, 3]
    assert [b["count"] for b in payload["bouquets"]] == [300, 3]
    # Never withheld, so nothing was retracted on the way.
    assert all(sent.text for sent in factory.client.all_for(CHANNELS))

    # And too big once more is said once more.
    fill(receiver, SECOND_BOUQUET, 300, "B")
    bridge.publisher("channels").refresh()
    assert factory.client.last(CHANNELS).json()["embedded"] is False
    assert plugin_log().count(line) == 2


def test_the_receiver_still_knows_every_channel(make_bridge, factory, settings, receiver,
                                                monkeypatch):
    """Only the wire changed: names resolve, the select has its options, the grid is built."""
    two_bouquets_too_big_together(receiver, monkeypatch)
    receiver.service_center.contents[SECOND_BOUQUET].append((POLSAT, "Polsat Sport"))
    bridge = start(make_bridge, factory, settings, receiver)
    assert lists(factory) == [None, None]
    publisher = bridge.publisher("channels")

    assert publisher.find_by_name("polsat sport") == (POLSAT, None)
    assert publisher.bouquet_for(POLSAT) == "Sport (HD)"
    assert len(bridge.channel_options()) == 601
    assert [len(bouquet["channels"]) for bouquet in publisher.bouquets] == [300, 301]

    factory.client.fire_message(ROOT + "/cmd/zap", b'{"name": "Polsat Sport"}')
    assert receiver.nav.played == [POLSAT]

    finish_grid(bridge)
    grid = factory.client.last(GRID).json()
    assert grid["bouquet"] == "Ulubione TV"
    assert len(grid["channels"]) == 300


def test_a_reconnect_without_the_lists_sends_and_builds_nothing_too_big(
    make_bridge, factory, settings, receiver, monkeypatch
):
    """The decision is kept with the bouquets; a connect does not encode megabytes to make it."""
    two_bouquets_too_big_together(receiver, monkeypatch)
    start(make_bridge, factory, settings, receiver)
    built = []
    encode = bridge_module._encoded

    def watching(payload):
        text = encode(payload)
        built.append(len(text))
        return text

    monkeypatch.setattr(bridge_module, "_encoded", watching)
    factory.client.clear()

    factory.client.fire_connect()
    factory.client.fire_connect()

    assert built and max(built) < LOW
    assert all(packet(sent) <= LOW for sent in factory.client.published)
    assert [sent.json()["embedded"] for sent in factory.client.all_for(CHANNELS)] == [False, False]
    assert len(factory.client.all_for(ULUBIONE)) == 2
    assert not_published(factory) == []


def test_channels_is_encoded_once_when_it_fits(live_bridge, factory, monkeypatch):
    """Measured and published from the same JSON: the bound costs a fitting list nothing."""
    whole = []
    encode = bridge_module._encoded

    def watching(payload):
        if isinstance(payload, dict) and "bouquets" in payload:
            whole.append(payload["embedded"])
        return encode(payload)

    monkeypatch.setattr(bridge_module, "_encoded", watching)

    live_bridge.publisher("channels").refresh()
    assert whole == [True]

    factory.client.fire_connect()
    assert whole == [True, True]


# -------------------------------------------------------------------- retraction --


def retracted(factory, topic):
    return [(sent.text, sent.retain) for sent in factory.client.all_for(topic)] == [("", True)]


def test_a_deselected_bouquet_has_its_list_retracted(live_bridge, factory, settings):
    settings.bouquets_for_select.value = "Ulubione TV"
    factory.client.clear()

    live_bridge.publisher("channels").refresh()

    assert retracted(factory, SPORT)
    assert not live_bridge.state.knows(SPORT)
    assert live_bridge.state.channel_slugs == ["ulubione_tv"]
    assert factory.client.all_for(ULUBIONE) == []


def test_a_renamed_bouquet_has_its_old_topic_retracted(live_bridge, factory, receiver):
    receiver.service_center.contents[conftest.BOUQUET_ROOT] = [
        (FIRST_BOUQUET, "Ulubione TV"),
        (SECOND_BOUQUET, "Sport 4K"),
    ]
    factory.client.clear()

    live_bridge.publisher("channels").refresh()

    assert retracted(factory, SPORT)
    assert factory.client.last(ROOT + "/channels/sport_4k").json()["bouquet"] == "Sport 4K"
    assert live_bridge.state.channel_slugs == ["ulubione_tv", "sport_4k"]


def test_a_removed_bouquet_has_its_list_retracted(live_bridge, factory, receiver):
    receiver.service_center.contents[conftest.BOUQUET_ROOT] = [(FIRST_BOUQUET, "Ulubione TV")]
    factory.client.clear()

    live_bridge.publisher("channels").refresh()

    assert retracted(factory, SPORT)


def test_the_slugs_are_in_the_state_file(live_bridge, state_path):
    live_bridge.state.save(force=True)
    with open(state_path, encoding="utf-8") as handle:
        assert json.load(handle)["channels_slugs"] == ["ulubione_tv", "sport_hd"]


def test_a_bouquet_that_went_while_the_plugin_was_stopped_is_retracted_on_the_connect(
    make_bridge, factory, settings, receiver, state_path
):
    """The walk runs while the plugin starts, before there is anybody to tell."""
    first = start(make_bridge, factory, settings, receiver)
    first.stop()

    settings.bouquets_for_select.value = "Ulubione TV"
    bridge = make_bridge(session=receiver.session, state_store=StateStore(path=state_path))
    bridge.start()
    client = factory.client
    # Nothing could be sent yet, so the state file still has to know the slug.
    assert not bridge.connected
    assert client.all_for(SPORT) == []
    assert bridge.state.channel_slugs == ["ulubione_tv", "sport_hd"]
    assert bridge.state.knows(SPORT)

    client.fire_connect()

    assert [(sent.text, sent.retain) for sent in client.all_for(SPORT)] == [("", True)]
    assert not bridge.state.knows(SPORT)
    assert bridge.state.channel_slugs == ["ulubione_tv"]
    assert client.last(ULUBIONE).json()["bouquet"] == "Ulubione TV"
    with open(state_path, encoding="utf-8") as handle:
        assert json.load(handle)["channels_slugs"] == ["ulubione_tv"]


def test_a_walk_during_an_outage_leaves_the_retraction_to_the_connect(live_bridge, factory,
                                                                     settings):
    factory.client.fire_disconnect(7)
    settings.bouquets_for_select.value = "Ulubione TV"
    factory.client.clear()

    live_bridge.publisher("channels").refresh()
    assert factory.client.all_for(SPORT) == []
    assert live_bridge.state.channel_slugs == ["ulubione_tv", "sport_hd"]

    factory.client.fire_connect()
    assert retracted(factory, SPORT)
    assert live_bridge.state.channel_slugs == ["ulubione_tv"]


def test_with_no_channel_list_every_topic_is_retracted(make_bridge, factory, settings, receiver,
                                                       state_path, monkeypatch):
    first = start(make_bridge, factory, settings, receiver)
    first.stop()

    monkeypatch.setattr(channels_module, "_service_center", lambda: None)
    bridge = make_bridge(session=receiver.session, state_store=StateStore(path=state_path))
    bridge.start()
    factory.client.fire_connect()

    assert retracted(factory, ULUBIONE)
    assert retracted(factory, SPORT)
    assert bridge.state.channel_slugs == []


def test_a_reconnect_retracts_nothing_that_is_configured(live_bridge, factory):
    factory.client.clear()
    factory.client.fire_connect()

    for topic in (ULUBIONE, SPORT):
        assert [bool(sent.text) for sent in factory.client.all_for(topic)] == [True]
    assert live_bridge.state.channel_slugs == ["ulubione_tv", "sport_hd"]


def test_reset_retracts_the_lists_and_puts_them_back(live_bridge, factory):
    factory.client.clear()

    factory.client.fire_message(ROOT + "/cmd/reset", b"PRESS")

    for topic in (ULUBIONE, SPORT):
        assert [bool(sent.text) for sent in factory.client.all_for(topic)] == [False, True]
        assert live_bridge.state.knows(topic)
    assert live_bridge.state.channel_slugs == ["ulubione_tv", "sport_hd"]


def test_a_rename_of_the_node_takes_the_lists_back(live_bridge, factory, settings):
    old = factory.client
    settings.node_id.value = "vuuno4kse_005302"
    old.clear()

    live_bridge.reload()

    assert [sent.text for sent in old.all_for(ULUBIONE)] == [""]
    assert [sent.text for sent in old.all_for(SPORT)] == [""]


# ------------------------------------------------------------- one bouquet too big --


def test_a_bouquet_too_big_by_itself_is_named_in_info(make_bridge, factory, settings, receiver,
                                                      monkeypatch):
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", LOW)
    fill(receiver, FIRST_BOUQUET, 600, "A")
    bridge = start(make_bridge, factory, settings, receiver, epg_grid_events=0)

    assert factory.client.all_for(ULUBIONE) == []
    listed = not_published(factory)
    assert [one["topic"] for one in listed] == ["channels/ulubione_tv"]
    assert listed[0]["bytes"] > LOW
    assert listed[0]["limit"] == LOW
    # The other bouquet has its list, and `channels` names both.
    assert factory.client.last(SPORT).json()["channels"] == [
        {"sref": POLSAT, "name": "Polsat Sport"}
    ]
    payload = factory.client.last(CHANNELS).json()
    assert payload["embedded"] is False
    assert [b["count"] for b in payload["bouquets"]] == [600, 1]
    assert bridge.publisher("channels").find_by_name("A 00599 " + "n" * 80)[1] is None
    assert all(packet(sent) <= LOW for sent in factory.client.published)

    factory.client.clear()
    factory.client.fire_connect()
    assert factory.client.all_for(ULUBIONE) == []
    assert all(packet(sent) <= LOW for sent in factory.client.published)


def test_a_list_that_outgrew_the_bound_is_taken_back(make_bridge, factory, settings, receiver,
                                                     monkeypatch):
    """The copy that fitted would go on saying it is this bouquet's list."""
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", LOW)
    bridge = start(make_bridge, factory, settings, receiver, epg_grid_events=0)
    assert factory.client.last(ULUBIONE).json()["channels"]
    factory.client.clear()

    fill(receiver, FIRST_BOUQUET, 600, "A")
    bridge.publisher("channels").refresh()

    assert retracted(factory, ULUBIONE)
    assert [one["topic"] for one in not_published(factory)] == ["channels/ulubione_tv"]
    # Still configured, so still a slug this node answers for.
    assert bridge.state.channel_slugs == ["ulubione_tv", "sport_hd"]


# -------------------------------------------------------------------- capability --


def test_the_capability_is_claimed_with_the_channel_list(live_bridge, factory):
    capabilities = factory.client.last(INFO).json()["capabilities"]
    assert "channel_topics" in capabilities
    assert capabilities.index("channel_topics") == capabilities.index("channels") + 1
    assert capabilities.count("channel_topics") == 1


def test_no_channel_list_means_no_capability(make_bridge, factory, settings, receiver,
                                             monkeypatch):
    monkeypatch.setattr(channels_module, "_service_center", lambda: None)
    bridge = start(make_bridge, factory, settings, receiver)

    assert "channel_topics" not in bridge.capabilities()
    assert factory.client.all_for(ULUBIONE) == []
    assert bridge.state.channel_slugs == []
