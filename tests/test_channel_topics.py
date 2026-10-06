"""The channel list per bouquet: `channels/<bouquet_slug>`, and `bouquets`, which indexes them.

`channels` grows with the sum of every bouquet, so on a receiver with a very
large list its packet passes the bound and it is withheld. Each bouquet's list
is therefore on a topic of its own as well, and a small index names them.
`channels` itself is what it has always been; the assertions here are about
the topics beside it, and about what all of it costs to encode.
"""

import json

import conftest
import pytest
from conftest import FIRST_BOUQUET, POLSAT, SECOND_BOUQUET, TVN, TVP1

from MQTTBridge import bridge as bridge_module
from MQTTBridge import channels as channels_module
from MQTTBridge.discovery import StateStore

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
INFO = ROOT + "/info"
CHANNELS = ROOT + "/channels"
BOUQUETS = ROOT + "/bouquets"
ULUBIONE = ROOT + "/channels/ulubione_tv"
SPORT = ROOT + "/channels/sport_hd"
GRID = ROOT + "/epg_grid/ulubione_tv"

THIRD_BOUQUET = '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.kino.tv" ORDER BY bouquet'
KINO = "1:0:19:2840:3FB:1:C00000:0:0:0:"

# Written out rather than read from the module: the number is the promise.
LIMIT = 1000000
OVERHEAD = 1 + 4 + 2

# A bound small enough that two modest bouquets pass it together and neither
# does alone, and large enough for everything else the plugin publishes.
LOW = 60000

NOW = 1789459200


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


def pinned_clock(monkeypatch, now=NOW):
    clock = [float(now)]
    monkeypatch.setattr(channels_module.time, "time", lambda: clock[0])
    return clock


def not_published(factory):
    return factory.client.last(INFO).json()["not_published"]


def index(factory):
    return factory.client.last(BOUQUETS).json()["bouquets"]


def name_bouquets(receiver, *names):
    """Call the receiver's bouquets `names`, in that order; a third one has its own channel."""
    references = (FIRST_BOUQUET, SECOND_BOUQUET, THIRD_BOUQUET)
    receiver.service_center.contents[conftest.BOUQUET_ROOT] = list(zip(references, names))
    receiver.service_center.contents[THIRD_BOUQUET] = [(KINO, "Kino Polska")]


def kind(payload):
    """Which of the channel list's payloads an encoding was of, or None."""
    if isinstance(payload, dict) and "bouquets" in payload:
        return "channels"
    if isinstance(payload, dict) and set(payload) == {"bouquet", "sref", "generated", "channels"}:
        # A bouquet's whole payload: its list encoded a second time.
        return "list again"
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        if "slug" in payload[0]:
            return "index"
        if set(payload[0]) == {"sref", "name"}:
            return "list"
    return None


def watch_encoding(monkeypatch):
    """Record every JSON text the bridge builds: `(kind, characters)`."""
    built = []
    encode = bridge_module._encoded

    def watching(payload):
        text = encode(payload)
        built.append((kind(payload), len(text)))
        return text

    monkeypatch.setattr(bridge_module, "_encoded", watching)
    return built


def kinds(built):
    return sorted(found for found, _length in built if found)


# ----------------------------------------------------------------- channels itself --


def test_channels_is_byte_for_byte_what_it_was(live_bridge, factory, monkeypatch):
    """The payload `main` builds for this receiver: the walk's time and the bouquets as read."""
    pinned_clock(monkeypatch)
    factory.client.clear()

    live_bridge.publisher("channels").refresh()

    _root, bouquets = channels_module.read_bouquets([])
    as_before = json.dumps(
        {"generated": NOW, "bouquets": bouquets},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    assert factory.client.last(CHANNELS).text == as_before
    payload = factory.client.last(CHANNELS).json()
    assert set(payload) == {"generated", "bouquets"}
    assert [set(bouquet) for bouquet in payload["bouquets"]] == [{"name", "sref", "channels"}] * 2

    # And a connect sends the same bytes again.
    factory.client.clear()
    factory.client.fire_connect()
    assert factory.client.last(CHANNELS).text == as_before


def test_the_receivers_own_bouquets_gain_nothing(live_bridge):
    """The slug and the count are in the index; the grid and the lookups read the plain list."""
    publisher = live_bridge.publisher("channels")
    assert set(publisher.index()["bouquets"][0]) == {"name", "sref", "slug", "count"}
    for bouquet in publisher.bouquets:
        assert set(bouquet) == {"name", "sref", "channels"}


# ------------------------------------------------------------------------ the index --


def test_the_index_names_every_bouquet_with_its_slug_and_size(live_bridge, factory):
    sent = factory.client.last(BOUQUETS)
    assert sent.retain is True
    payload = sent.json()
    assert set(payload) == {"generated", "bouquets"}
    assert isinstance(payload["generated"], int)
    assert payload["bouquets"] == [
        {"name": "Ulubione TV", "sref": FIRST_BOUQUET, "slug": "ulubione_tv", "count": 2},
        {"name": "Sport (HD)", "sref": SECOND_BOUQUET, "slug": "sport_hd", "count": 1},
    ]


def test_the_index_is_not_republished_when_nothing_in_it_changed(live_bridge, factory, receiver,
                                                                 monkeypatch):
    stamp = factory.client.last(BOUQUETS).json()["generated"]
    clock = pinned_clock(monkeypatch)
    publisher = live_bridge.publisher("channels")
    factory.client.clear()

    publisher.refresh()
    # A channel renamed is a change to that bouquet's list, not to the index.
    receiver.service_center.contents[SECOND_BOUQUET] = [(POLSAT, "Polsat Sport HD")]
    publisher.refresh()
    assert factory.client.all_for(BOUQUETS) == []

    # A connect sends it with the stamp it had.
    factory.client.fire_connect()
    assert factory.client.last(BOUQUETS).json()["generated"] == stamp
    factory.client.clear()

    receiver.service_center.contents[SECOND_BOUQUET].append((TVN, "TVN HD"))
    clock[0] += 60
    publisher.refresh()
    sent = factory.client.all_for(BOUQUETS)
    assert len(sent) == 1
    assert sent[0].json()["generated"] == NOW + 60
    assert [bouquet["count"] for bouquet in sent[0].json()["bouquets"]] == [2, 2]


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


def test_the_index_leads_to_what_channels_carries(live_bridge, factory):
    whole = factory.client.last(CHANNELS).json()["bouquets"]
    for entry, bouquet in zip(index(factory), whole):
        own = factory.client.last(ROOT + "/channels/" + entry["slug"]).json()
        assert own["bouquet"] == bouquet["name"] == entry["name"]
        assert own["sref"] == bouquet["sref"] == entry["sref"]
        assert own["channels"] == bouquet["channels"]
        assert entry["count"] == len(bouquet["channels"])


def test_the_json_put_together_by_hand_is_the_encoders(live_bridge, factory, receiver):
    """Quotes, a backslash, a control character, two- and four-byte letters."""
    awkward = ['4" News \\ One', "Tab\there", "Żółć Łódź", "TV " + chr(0x1F4FA), ""]
    receiver.service_center.contents[conftest.BOUQUET_ROOT] = [
        (FIRST_BOUQUET, 'Ulubione "TV" \\ Żółć'),
        (SECOND_BOUQUET, "Sport (HD)"),
    ]
    receiver.service_center.contents[FIRST_BOUQUET] = [
        (f"1:0:19:{number:X}:3FB:1:C00000:0:0:0:", name) for number, name in enumerate(awkward)
    ]
    factory.client.clear()

    live_bridge.publisher("channels").refresh()
    factory.client.fire_connect()

    for topic in (ROOT + "/channels/ulubione_tv_zolc", SPORT, BOUQUETS):
        sent = factory.client.all_for(topic)
        # From the walk where it changed, and from the connect in any case.
        assert sent
        for one in sent:
            assert one.text == bridge_module._encoded(one.json())
    names = [c["name"] for c in factory.client.last(ROOT + "/channels/ulubione_tv_zolc").json()[
        "channels"]]
    assert names[:4] == awkward[:4]


def test_a_bouquet_nobody_edited_is_not_republished(live_bridge, factory, receiver, monkeypatch):
    """`generated` is stamped when a list differs; alone it is not a change."""
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
    assert changed[0].json()["generated"] == NOW + 60
    # `channels` itself says when the bouquets were walked, as it always did.
    assert factory.client.last(CHANNELS).json()["generated"] == NOW + 60


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
    assert factory.client.all_for(BOUQUETS) == []


def test_a_bouquet_keeps_its_stamp_until_its_list_differs(live_bridge, factory, receiver,
                                                          monkeypatch):
    """Also across a reconnect, which sends every list again and edits none."""
    first = factory.client.last(ULUBIONE).json()["generated"]
    clock = pinned_clock(monkeypatch, first + 1000)
    publisher = live_bridge.publisher("channels")

    receiver.service_center.contents[SECOND_BOUQUET].append((TVN, "TVN HD"))
    publisher.refresh()
    clock[0] += 500
    publisher.refresh()
    factory.client.clear()
    factory.client.fire_connect()

    assert factory.client.last(ULUBIONE).json()["generated"] == first
    assert factory.client.last(SPORT).json()["generated"] == first + 1000
    # `channels` is another matter: it says when the bouquets were last walked.
    assert factory.client.last(CHANNELS).json()["generated"] == first + 1500

    # Edited and put back is two changes; the stamp is of the second.
    receiver.service_center.contents[SECOND_BOUQUET].pop()
    clock[0] += 500
    publisher.refresh()
    assert factory.client.last(SPORT).json()["generated"] == first + 2000


def test_a_restart_of_the_plugin_stamps_every_bouquet_anew(make_bridge, factory, settings,
                                                           receiver, state_path, monkeypatch):
    """The stamps are kept in memory; a new process has only its own first walk to go by."""
    clock = pinned_clock(monkeypatch)
    first = start(make_bridge, factory, settings, receiver)
    assert factory.client.last(ULUBIONE).json()["generated"] == NOW
    first.stop()

    clock[0] += 3600
    bridge = make_bridge(session=receiver.session, state_store=StateStore(path=state_path))
    bridge.start()
    factory.client.fire_connect()

    assert factory.client.last(ULUBIONE).json()["generated"] == NOW + 3600
    assert factory.client.last(BOUQUETS).json()["generated"] == NOW + 3600


def test_the_topics_come_back_on_a_reconnect(live_bridge, factory):
    factory.client.clear()
    factory.client.fire_connect()

    assert factory.client.last(ULUBIONE).json()["bouquet"] == "Ulubione TV"
    assert factory.client.last(SPORT).json()["bouquet"] == "Sport (HD)"
    assert [entry["slug"] for entry in index(factory)] == ["ulubione_tv", "sport_hd"]


# ----------------------------------------------------------------- what it costs --


def test_a_walk_encodes_each_list_once_and_a_connect_none(live_bridge, factory, receiver,
                                                          monkeypatch):
    """One encoding for `channels`, one for each bouquet's list, and a small one for the index."""
    stamps = [(("bouquets.tv", 1.0),)]
    monkeypatch.setattr(channels_module, "bouquet_mtimes", lambda directory=None: stamps[0])
    publisher = live_bridge.publisher("channels")
    publisher._poll()
    built = watch_encoding(monkeypatch)

    # The poll a minute later, with no bouquet file touched: nothing at all.
    publisher._poll()
    assert built == []

    # A walk that finds one bouquet edited.
    receiver.service_center.contents[SECOND_BOUQUET].append((TVN, "TVN HD"))
    stamps[0] = (("bouquets.tv", 2.0),)
    publisher._poll()
    assert kinds(built) == ["channels", "index", "list", "list"]

    # A connect: `channels` as ever, and the lists from what the walk made.
    del built[:]
    factory.client.fire_connect()
    assert kinds(built) == ["channels"]
    assert factory.client.last(SPORT).json()["channels"][-1] == {"sref": TVN, "name": "TVN HD"}


# ------------------------------------------------------------------ over the bound --


def test_lists_too_big_together_are_each_on_their_own_topic(make_bridge, factory, settings,
                                                            receiver):
    """At the real bound: 720 kB a bouquet, so each fits a packet and the two do not."""
    fill(receiver, FIRST_BOUQUET, 3000, "A", width=180)
    fill(receiver, SECOND_BOUQUET, 3000, "B", width=180)
    bridge = start(make_bridge, factory, settings, receiver, epg_grid_events=0)

    # `channels` is withheld by the bound, as on `main`, and `info` says so.
    assert factory.client.all_for(CHANNELS) == []
    listed = not_published(factory)
    assert [one["topic"] for one in listed] == ["channels"]
    assert listed[0]["bytes"] > 1400000 and listed[0]["limit"] == LIMIT
    assert not bridge.state.knows(CHANNELS)

    # The index is small, and every list is on the broker in a packet of its own.
    assert packet(factory.client.last(BOUQUETS)) < 1000
    assert [(entry["slug"], entry["count"]) for entry in index(factory)] == [
        ("ulubione_tv", 3000), ("sport_hd", 3000),
    ]
    for topic, tag in ((ULUBIONE, "A"), (SPORT, "B")):
        sent = factory.client.last(topic)
        assert 700000 < packet(sent) <= LIMIT
        own = sent.json()["channels"]
        assert len(own) == 3000
        assert own[0]["name"].startswith(tag + " 00000 ")
    assert all(packet(sent) <= LIMIT for sent in factory.client.published)
    assert bridge.publisher("channels").find_by_name("B 02999 " + "n" * 180)[1] is None


def test_channels_is_withheld_one_byte_over_the_bound_and_not_at_it(make_bridge, factory,
                                                                    settings, receiver,
                                                                    monkeypatch):
    start(make_bridge, factory, settings, receiver, epg_grid_events=0)
    size = packet(factory.client.last(CHANNELS))

    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", size)
    bridge = start(make_bridge, factory, settings, receiver, epg_grid_events=0)
    assert packet(factory.client.last(CHANNELS)) == size
    assert "channels" not in [one["topic"] for one in bridge.not_published()]

    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", size - 1)
    bridge = start(make_bridge, factory, settings, receiver, epg_grid_events=0)
    # No payload - only the retraction of the copy the session before left.
    assert [sent.text for sent in factory.client.all_for(CHANNELS)] == [""]
    assert {"topic": "channels", "bytes": size, "limit": size - 1} in bridge.not_published()


def test_a_list_that_outgrows_the_bound_takes_channels_back_and_returns_it(
    make_bridge, factory, settings, receiver, monkeypatch, plugin_log
):
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", LOW)
    bridge = start(make_bridge, factory, settings, receiver)
    publisher = bridge.publisher("channels")
    assert factory.client.last(CHANNELS).json()["bouquets"]
    factory.client.clear()

    fill(receiver, FIRST_BOUQUET, 300, "A")
    fill(receiver, SECOND_BOUQUET, 300, "B")
    publisher.refresh()

    # The copy that fitted is taken back rather than left to look current.
    assert [(sent.text, sent.retain) for sent in factory.client.all_for(CHANNELS)] == [("", True)]
    assert [one["topic"] for one in not_published(factory)] == ["channels"]
    assert len(factory.client.last(SPORT).json()["channels"]) == 300
    line = "not publishing channels:"
    assert plugin_log().count(line) == 1

    # Still too big at the next walk: nothing new to say or to take back.
    factory.client.clear()
    publisher.refresh()
    assert factory.client.all_for(CHANNELS) == []
    assert plugin_log().count(line) == 1

    fill(receiver, SECOND_BOUQUET, 3, "B")
    publisher.refresh()
    assert [len(b["channels"]) for b in factory.client.last(CHANNELS).json()["bouquets"]] == [
        300, 3]
    assert not_published(factory) == []


def test_the_receiver_still_knows_every_channel(make_bridge, factory, settings, receiver,
                                                monkeypatch):
    """Only the wire changed: names resolve, the select has its options, the grid is built."""
    two_bouquets_too_big_together(receiver, monkeypatch)
    receiver.service_center.contents[SECOND_BOUQUET].append((POLSAT, "Polsat Sport"))
    bridge = start(make_bridge, factory, settings, receiver)
    assert factory.client.all_for(CHANNELS) == []
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


def test_a_reconnect_over_the_bound_sends_and_builds_nothing_too_big(
    make_bridge, factory, settings, receiver, monkeypatch, plugin_log
):
    """The size is kept with the bouquets; a connect does not encode megabytes to find it again."""
    two_bouquets_too_big_together(receiver, monkeypatch)
    bridge = start(make_bridge, factory, settings, receiver)
    before = bridge.not_published()
    assert [one["topic"] for one in before] == ["channels"]
    built = watch_encoding(monkeypatch)
    factory.client.clear()

    factory.client.fire_connect()
    factory.client.fire_connect()

    assert built and max(length for _kind, length in built) < LOW
    assert kinds(built) == []
    assert factory.client.all_for(CHANNELS) == []
    assert all(packet(sent) <= LOW for sent in factory.client.published)
    assert len(factory.client.all_for(ULUBIONE)) == 2
    assert len(factory.client.all_for(BOUQUETS)) == 2
    # The bookkeeping is what a measurement would have left: the same entry,
    # in the first `info` of each connect, and one line in the log in all.
    lists = [sent.json()["not_published"] for sent in factory.client.all_for(INFO)]
    assert lists == [before, before]
    assert plugin_log().count("not publishing channels:") == 1


def test_a_connect_withholds_it_as_a_measurement_would(make_bridge, factory, settings, receiver,
                                                       monkeypatch, plugin_log):
    """Taken off the list by something else, it is back on it at the next connect."""
    two_bouquets_too_big_together(receiver, monkeypatch)
    bridge = start(make_bridge, factory, settings, receiver)
    before = bridge.not_published()
    bridge.retract(CHANNELS)
    assert bridge.not_published() == []

    factory.client.fire_connect()

    assert bridge.not_published() == before
    assert not_published(factory) == before
    assert plugin_log().count("not publishing channels:") == 2


def test_reset_over_the_bound_leaves_channels_withheld(make_bridge, factory, settings, receiver,
                                                       monkeypatch):
    two_bouquets_too_big_together(receiver, monkeypatch)
    bridge = start(make_bridge, factory, settings, receiver)
    built = watch_encoding(monkeypatch)
    factory.client.clear()

    bridge.reset_retained()

    assert factory.client.all_for(CHANNELS) == []
    assert [one["topic"] for one in not_published(factory)] == ["channels"]
    assert [bool(sent.text) for sent in factory.client.all_for(BOUQUETS)] == [False, True]
    assert kinds(built) == []


# ------------------------------------------------------------------------ the slugs --


@pytest.mark.parametrize("names, slugs", [
    (["Ulubione TV", "Sport (HD)"], ["ulubione_tv", "sport_hd"]),
    (["Kino HD", "Kino (HD)"], ["kino_hd", "kino_hd_2"]),
    (["Kino HD", "Kino (HD)", "Kino-HD"], ["kino_hd", "kino_hd_2", "kino_hd_3"]),
    # A bouquet really called that keeps its slug, before the others or after.
    (["Kino HD", "Kino (HD)", "Kino HD 2"], ["kino_hd", "kino_hd_3", "kino_hd_2"]),
    (["Kino HD 2", "Kino HD", "Kino (HD)"], ["kino_hd_2", "kino_hd", "kino_hd_3"]),
    (["Kino HD", "Kino (HD)", "Kino HD 2", "Kino (HD) 2"],
     ["kino_hd", "kino_hd_3", "kino_hd_2", "kino_hd_2_2"]),
    (["***", "Kino", "---", "Kino!"], ["", "kino", "", "kino_2"]),
    ([], []),
])
def test_a_later_bouquet_with_a_slug_already_taken_is_numbered(names, slugs):
    found = channels_module.topic_slugs(names)
    assert found == slugs
    taken = [slug for slug in found if slug]
    assert len(taken) == len(set(taken))


def test_three_bouquets_with_one_slug_each_have_a_topic(live_bridge, factory, receiver,
                                                        plugin_log):
    name_bouquets(receiver, "Kino HD", "Kino (HD)", "Kino-HD")
    factory.client.clear()
    publisher = live_bridge.publisher("channels")

    publisher.refresh()

    assert [(entry["name"], entry["slug"]) for entry in index(factory)] == [
        ("Kino HD", "kino_hd"), ("Kino (HD)", "kino_hd_2"), ("Kino-HD", "kino_hd_3"),
    ]
    for slug, name, first in (("kino_hd", "Kino HD", TVP1), ("kino_hd_2", "Kino (HD)", POLSAT),
                              ("kino_hd_3", "Kino-HD", KINO)):
        sent = factory.client.all_for(ROOT + "/channels/" + slug)
        assert len(sent) == 1
        assert sent[0].json()["bouquet"] == name
        assert sent[0].json()["channels"][0]["sref"] == first
    assert publisher.published_slugs == ["kino_hd", "kino_hd_2", "kino_hd_3"]
    assert live_bridge.state.channel_slugs == ["kino_hd", "kino_hd_2", "kino_hd_3"]

    # One line for each bouquet that was numbered, and not again while nothing moves.
    assert "the bouquet Kino (HD) slugs the same as an earlier one" in plugin_log()
    assert "its channel list is on channels/kino_hd_3" in plugin_log()
    assert plugin_log().count("slugs the same as an earlier one") == 2
    publisher.refresh()
    assert plugin_log().count("slugs the same as an earlier one") == 2


def test_a_numbered_slug_steps_past_a_bouquet_really_called_that(live_bridge, factory, receiver):
    name_bouquets(receiver, "Kino HD", "Kino (HD)", "Kino HD 2")

    live_bridge.publisher("channels").refresh()

    assert [entry["slug"] for entry in index(factory)] == ["kino_hd", "kino_hd_3", "kino_hd_2"]
    assert factory.client.last(ROOT + "/channels/kino_hd_2").json()["bouquet"] == "Kino HD 2"
    assert factory.client.last(ROOT + "/channels/kino_hd_3").json()["bouquet"] == "Kino (HD)"


def test_two_bouquets_changing_places_swap_their_topics(live_bridge, factory, receiver,
                                                        plugin_log):
    """The first in order has the plain slug; nothing stale is left when the order turns."""
    name_bouquets(receiver, "Kino HD", "Kino (HD)")
    publisher = live_bridge.publisher("channels")
    publisher.refresh()
    plain, numbered = ROOT + "/channels/kino_hd", ROOT + "/channels/kino_hd_2"
    assert factory.client.last(plain).json()["sref"] == FIRST_BOUQUET
    assert factory.client.last(numbered).json()["sref"] == SECOND_BOUQUET
    factory.client.clear()

    receiver.service_center.contents[conftest.BOUQUET_ROOT] = [
        (SECOND_BOUQUET, "Kino (HD)"), (FIRST_BOUQUET, "Kino HD"),
    ]
    publisher.refresh()

    for topic, name, reference, service in ((plain, "Kino (HD)", SECOND_BOUQUET, POLSAT),
                                            (numbered, "Kino HD", FIRST_BOUQUET, TVP1)):
        sent = factory.client.all_for(topic)
        assert len(sent) == 1
        assert (sent[0].json()["bouquet"], sent[0].json()["sref"]) == (name, reference)
        assert sent[0].json()["channels"][0]["sref"] == service
    assert [(entry["name"], entry["slug"]) for entry in index(factory)] == [
        ("Kino (HD)", "kino_hd"), ("Kino HD", "kino_hd_2"),
    ]
    assert all(sent.text for sent in factory.client.published)
    assert live_bridge.state.channel_slugs == ["kino_hd", "kino_hd_2"]
    # Another bouquet is the numbered one now, and the log says which.
    assert "the bouquet Kino HD slugs the same as an earlier one" in plugin_log()


def test_a_collision_that_ends_takes_the_numbered_topic_back(live_bridge, factory, receiver):
    name_bouquets(receiver, "Kino HD", "Kino (HD)")
    publisher = live_bridge.publisher("channels")
    publisher.refresh()
    factory.client.clear()

    name_bouquets(receiver, "Kino HD", "Sport (HD)")
    publisher.refresh()

    assert [sent.text for sent in factory.client.all_for(ROOT + "/channels/kino_hd_2")] == [""]
    assert factory.client.last(SPORT).json()["sref"] == SECOND_BOUQUET
    assert live_bridge.state.channel_slugs == ["kino_hd", "sport_hd"]


def test_the_grid_slugs_each_name_by_itself(live_bridge, receiver):
    """Only the channel topics are numbered; a consumer takes their slug from the index."""
    name_bouquets(receiver, "Kino HD", "Kino (HD)")
    live_bridge.publisher("channels").refresh()
    finish_grid(live_bridge)

    assert live_bridge.publisher("channels").published_slugs == ["kino_hd", "kino_hd_2"]
    assert live_bridge.publisher("epg_grid").published_slugs == ["kino_hd"]


def test_a_name_that_leaves_no_slug_has_no_topic(live_bridge, factory, receiver):
    name_bouquets(receiver, "***", "Sport (HD)")
    factory.client.clear()

    live_bridge.publisher("channels").refresh()

    assert ROOT + "/channels/" not in factory.client.topics()
    assert [(entry["name"], entry["slug"], entry["count"]) for entry in index(factory)] == [
        ("***", "", 2), ("Sport (HD)", "sport_hd", 1),
    ]
    assert live_bridge.publisher("channels").published_slugs == ["sport_hd"]
    # It is still in `channels`, with its list.
    assert factory.client.last(CHANNELS).json()["bouquets"][0]["name"] == "***"


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
    assert [entry["slug"] for entry in index(factory)] == ["ulubione_tv"]


def test_a_renamed_bouquet_has_its_old_topic_retracted(live_bridge, factory, receiver):
    name_bouquets(receiver, "Ulubione TV", "Sport 4K")
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


def test_with_no_channel_list_every_topic_and_the_index_are_retracted(
    make_bridge, factory, settings, receiver, state_path, monkeypatch
):
    first = start(make_bridge, factory, settings, receiver)
    first.stop()

    monkeypatch.setattr(channels_module, "_service_center", lambda: None)
    bridge = make_bridge(session=receiver.session, state_store=StateStore(path=state_path))
    bridge.start()
    factory.client.fire_connect()

    assert retracted(factory, ULUBIONE)
    assert retracted(factory, SPORT)
    assert retracted(factory, BOUQUETS)
    assert bridge.state.channel_slugs == []
    assert not bridge.state.knows(BOUQUETS)


def test_a_reconnect_retracts_nothing_that_is_configured(live_bridge, factory):
    factory.client.clear()
    factory.client.fire_connect()

    for topic in (ULUBIONE, SPORT, BOUQUETS):
        assert [bool(sent.text) for sent in factory.client.all_for(topic)] == [True]
    assert live_bridge.state.channel_slugs == ["ulubione_tv", "sport_hd"]


def test_reset_retracts_the_lists_and_the_index_and_puts_them_back(live_bridge, factory):
    factory.client.clear()

    factory.client.fire_message(ROOT + "/cmd/reset", b"PRESS")

    for topic in (ULUBIONE, SPORT, BOUQUETS):
        assert [bool(sent.text) for sent in factory.client.all_for(topic)] == [False, True]
        assert live_bridge.state.knows(topic)
    assert live_bridge.state.channel_slugs == ["ulubione_tv", "sport_hd"]


def test_a_rename_of_the_node_takes_the_lists_and_the_index_back(live_bridge, factory, settings):
    old = factory.client
    settings.node_id.value = "vuuno4kse_005302"
    old.clear()

    live_bridge.reload()

    for topic in (ULUBIONE, SPORT, BOUQUETS):
        assert [sent.text for sent in old.all_for(topic)] == [""]


# ------------------------------------------------------------- one bouquet too big --


def test_a_bouquet_too_big_by_itself_is_named_in_info(make_bridge, factory, settings, receiver,
                                                      monkeypatch):
    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", LOW)
    fill(receiver, FIRST_BOUQUET, 600, "A")
    bridge = start(make_bridge, factory, settings, receiver, epg_grid_events=0)

    assert factory.client.all_for(ULUBIONE) == []
    listed = not_published(factory)
    assert [one["topic"] for one in listed] == ["channels", "channels/ulubione_tv"]
    assert listed[1]["bytes"] > LOW
    assert listed[1]["limit"] == LOW
    # The other bouquet has its list, and the index names both.
    assert factory.client.last(SPORT).json()["channels"] == [
        {"sref": POLSAT, "name": "Polsat Sport"}
    ]
    assert [(entry["slug"], entry["count"]) for entry in index(factory)] == [
        ("ulubione_tv", 600), ("sport_hd", 1),
    ]
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
    assert "channels/ulubione_tv" in [one["topic"] for one in not_published(factory)]
    # Still configured, so still a slug this node answers for.
    assert bridge.state.channel_slugs == ["ulubione_tv", "sport_hd"]


# -------------------------------------------------------------------- capability --


def test_the_capability_is_claimed_with_the_channel_list(live_bridge, factory):
    capabilities = factory.client.last(INFO).json()["capabilities"]
    assert "channel_topics" in capabilities
    assert capabilities.index("channel_topics") == capabilities.index("channels") + 1
    assert capabilities.count("channel_topics") == 1
    # What it promises is there.
    assert factory.client.last(BOUQUETS) is not None
    assert factory.client.last(ULUBIONE) is not None


def test_no_channel_list_means_no_capability(make_bridge, factory, settings, receiver,
                                             monkeypatch):
    monkeypatch.setattr(channels_module, "_service_center", lambda: None)
    bridge = start(make_bridge, factory, settings, receiver)

    assert "channel_topics" not in bridge.capabilities()
    assert factory.client.all_for(ULUBIONE) == []
    assert factory.client.all_for(BOUQUETS) == []
    assert bridge.state.channel_slugs == []
