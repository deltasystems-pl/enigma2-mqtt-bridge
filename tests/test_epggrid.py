"""The EPG grid: one topic per bouquet, and what happens to a bouquet that goes away."""

import conftest
from conftest import TVN, TVP1, Event

from MQTTBridge import epggrid

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
ULUBIONE = ROOT + "/epg_grid/ulubione_tv"
SPORT = ROOT + "/epg_grid/sport_hd"


def generate(bridge):
    """Run a whole pass: the grid is built one bouquet per turn of the main loop."""
    publisher = bridge.publisher("epg_grid")
    publisher.regenerate()
    for _ in range(100):
        if not publisher._queue and publisher._current is None:
            break
        publisher._step.timer.fire()
    return publisher


class Clock:
    """A wall clock the test moves on purpose.

    The grid stamps `generated` from `time.time()`, so „were these two builds
    in the same second?" decides what the change test sees. Left to the real
    clock that is a coin toss, and a test whose answer depends on a coin toss
    proves nothing either way — it only hides the defect most of the time.
    """

    def __init__(self, now):
        self.now = float(now)

    def __call__(self):
        return self.now


def pinned_clock(monkeypatch, now=1789459200):
    """Put every `time.time()` reader on a clock this test owns."""
    clock = Clock(now)
    monkeypatch.setattr(epggrid.time, "time", clock)
    return clock


def test_each_bouquet_gets_its_own_topic(live_bridge, factory):
    assert factory.client.last(ULUBIONE) is not None
    assert factory.client.last(SPORT) is not None


def test_the_slug_is_the_bouquet_name_made_addressable(live_bridge):
    assert live_bridge.publisher("epg_grid").published_slugs == ["ulubione_tv", "sport_hd"]


def test_a_polish_bouquet_name_slugs_to_ascii(live_bridge, factory, receiver):
    receiver.service_center.contents[conftest.BOUQUET_ROOT] = [
        (conftest.FIRST_BOUQUET, "Kanały Główne — Łódź"),
    ]
    live_bridge.publisher("channels").refresh()
    generate(live_bridge)
    assert factory.client.last(ROOT + "/epg_grid/kanaly_glowne_lodz") is not None


def test_the_payload_has_the_documented_shape(live_bridge, factory):
    payload = factory.client.last(ULUBIONE).json()
    assert set(payload) == {"bouquet", "generated", "channels"}
    assert isinstance(payload["generated"], int)
    channel = payload["channels"][0]
    assert set(channel) == {"sref", "name", "events"}
    assert set(channel["events"][0]) == {"title", "begin", "end", "event_id"}


def test_the_payload_carries_the_name_not_the_slug(live_bridge, factory):
    """A slug is an address; the name is what a person should be shown."""
    assert factory.client.last(ULUBIONE).json()["bouquet"] == "Ulubione TV"


def test_every_channel_of_the_bouquet_is_there(live_bridge, factory):
    payload = factory.client.last(ULUBIONE).json()
    assert [c["sref"] for c in payload["channels"]] == [TVP1, TVN]


def test_a_channel_with_no_epg_has_an_empty_list(live_bridge, factory):
    payload = factory.client.last(SPORT).json()
    assert payload["channels"][0]["events"] == []


def test_the_events_are_the_programmes_in_order(live_bridge, factory):
    events = factory.client.last(ULUBIONE).json()["channels"][0]["events"]
    assert [event["title"] for event in events] == ["Wiadomości", "Pogoda", "Film"]
    assert events[0]["end"] == events[0]["begin"] + 1500


def test_the_number_of_events_is_the_setting(make_bridge, factory, settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.epg_grid_events.value = 2
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    conftest.settle(bridge)
    events = factory.client.last(ULUBIONE).json()["channels"][0]["events"]
    assert len(events) == 2


def test_a_grid_whose_content_has_not_changed_is_not_republished(live_bridge, factory,
                                                                  monkeypatch):
    """A retained payload nobody changed is a row in somebody's database.

    The two builds are a second apart, which is the case that matters: the
    refresh runs every quarter of an hour, so on a receiver no two builds are
    ever in the same second. `generated` is stamped from the clock, and if it
    counts as a change then every grid rewrites itself for ever with nothing on
    television having moved.
    """
    clock = pinned_clock(monkeypatch)
    generate(live_bridge)
    factory.client.clear()
    clock.now += 1
    generate(live_bridge)
    assert factory.client.all_for(ULUBIONE) == []


def test_the_first_rebuild_after_a_connect_is_compared_too(live_bridge, factory, monkeypatch):
    """`on_connect` sends the lot; what it remembers has to be comparable.

    The snapshot is the deliberate exception to publish-on-change, so it goes
    out whether or not anything moved. What it records as „sent" is then what
    the next rebuild is judged against, and if the two are measured differently
    the first refresh after every reconnect republishes a grid nobody changed.
    """
    clock = pinned_clock(monkeypatch)
    factory.client.fire_connect()
    factory.client.clear()
    clock.now += 1
    generate(live_bridge)
    assert factory.client.all_for(ULUBIONE) == []


def test_a_grid_that_did_change_still_says_when_it_was_built(live_bridge, factory, receiver,
                                                             monkeypatch):
    """Only the comparison ignores `generated`; the payload still carries it."""
    clock = pinned_clock(monkeypatch)
    generate(live_bridge)
    receiver.epg.events[TVP1] = [Event(1, 10, 20, "Something else")]
    factory.client.clear()
    clock.now += 900
    generate(live_bridge)
    payload = factory.client.last(ULUBIONE).json()
    assert [e["title"] for e in payload["channels"][0]["events"]] == ["Something else"]
    assert payload["generated"] == 1789459200 + 900


def test_zero_events_turns_the_feature_off(make_bridge, factory, settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.epg_grid_events.value = 0
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert "epg_grid" not in bridge.capabilities()
    assert factory.client.last(ULUBIONE) is None


def test_one_channel_batch_is_built_per_turn_of_the_main_loop(live_bridge, factory, receiver):
    """A hundred channels of lookups in one call is a frozen television."""
    publisher = live_bridge.publisher("epg_grid")
    publisher._channels().bouquets[0]["channels"] *= 3
    receiver.epg.queries = []
    factory.client.clear()
    live_bridge.forget_published()
    publisher.regenerate()
    assert factory.client.all_for(ULUBIONE) == []
    publisher._step.timer.fire()
    assert factory.client.all_for(ULUBIONE) == []
    assert len(receiver.epg.queries[-1]) - 1 == epggrid.CHANNELS_PER_STEP
    publisher._step.timer.fire()
    assert factory.client.all_for(ULUBIONE) != []
    assert factory.client.all_for(SPORT) == []
    publisher._step.timer.fire()
    assert factory.client.all_for(SPORT) != []


def test_the_time_each_bouquet_took_is_logged(live_bridge, plugin_log):
    generate(live_bridge)
    log = plugin_log()
    assert "epg grid: Ulubione TV took" in log
    assert "bouquet(s) in" in log


def test_a_slow_bouquet_is_a_warning(live_bridge, plugin_log, monkeypatch):
    clock = [0.0]

    def tick():
        clock[0] += 0.5
        return clock[0]

    monkeypatch.setattr(epggrid.time, "time", tick)
    generate(live_bridge)
    assert "WARNING" in plugin_log()


def test_the_refresh_runs_every_quarter_of_an_hour(live_bridge):
    assert live_bridge.publisher("epg_grid")._refresh.timer.started == (15 * 60 * 1000, False)


def test_a_bouquet_change_rebuilds_the_grid(live_bridge, factory, receiver):
    receiver.epg.events[TVP1] = [Event(1, 10, 20, "Something else")]
    factory.client.clear()
    live_bridge.publisher("channels").refresh()
    publisher = live_bridge.publisher("epg_grid")
    for _ in range(3):
        if publisher._queue:
            publisher._step.timer.fire()
    titles = [e["title"] for e in factory.client.last(ULUBIONE).json()["channels"][0]["events"]]
    assert titles == ["Something else"]


def test_a_bouquet_that_is_no_longer_configured_is_retracted(live_bridge, factory, receiver,
                                                             settings):
    """🔴 Otherwise the broker serves the grid of a bouquet nobody has, forever."""
    settings.bouquets_for_select.value = "Ulubione TV"
    live_bridge.publisher("channels").refresh()
    factory.client.clear()
    generate(live_bridge)
    retraction = factory.client.last(SPORT)
    assert retraction is not None
    assert retraction.text == ""
    assert retraction.retain is True


def test_a_renamed_bouquet_retracts_its_old_slug(live_bridge, factory, receiver):
    receiver.service_center.contents[conftest.BOUQUET_ROOT] = [
        (conftest.FIRST_BOUQUET, "Ulubione TV"),
        (conftest.SECOND_BOUQUET, "Sport 4K"),
    ]
    live_bridge.publisher("channels").refresh()
    factory.client.clear()
    generate(live_bridge)
    assert factory.client.last(SPORT).text == ""
    assert factory.client.last(ROOT + "/epg_grid/sport_4k").text != ""


def test_the_published_slugs_are_remembered_across_a_restart(live_bridge, state_path):
    import json

    live_bridge.state.save(force=True)
    with open(state_path, encoding="utf-8") as handle:
        assert json.load(handle)["epg_grid_slugs"] == ["ulubione_tv", "sport_hd"]


def test_turning_the_grid_off_retracts_every_slug(make_bridge, factory, settings, receiver,
                                                  state_path):
    """The publisher is not even registered, so the bridge does the retracting."""
    from MQTTBridge.discovery import StateStore

    store = StateStore(path=state_path)
    store.set_grid_slugs(["ulubione_tv", "sport_hd"])
    store.save(force=True)

    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.epg_grid_events.value = 0
    bridge = make_bridge(session=receiver.session, state_store=StateStore(path=state_path))
    bridge.start()
    factory.client.fire_connect()

    assert factory.client.last(ULUBIONE).text == ""
    assert factory.client.last(SPORT).text == ""
    assert bridge.state.grid_slugs == []


def test_a_multi_service_query_is_one_call_for_the_whole_bouquet(live_bridge, receiver):
    receiver.epg.queries = []
    generate(live_bridge)
    first = receiver.epg.queries[0]
    assert first[0] == "RIBDT"
    assert len(first) == 3  # the format string and two channels


def test_an_image_that_refuses_a_multi_service_query_is_asked_one_at_a_time(live_bridge,
                                                                            factory, receiver):
    """enigma2 answers None to a query it does not understand, rather than raising."""
    receiver.epg.multi_service = False
    receiver.epg.queries = []
    factory.client.clear()
    live_bridge.forget_published()
    generate(live_bridge)
    # The two-channel bouquet was asked for both at once, refused, then asked
    # for each channel on its own.
    assert len(receiver.epg.queries[0]) == 3
    assert [len(query) for query in receiver.epg.queries[1:3]] == [2, 2]
    events = factory.client.last(ULUBIONE).json()["channels"][0]["events"]
    assert [event["title"] for event in events] == ["Wiadomości", "Pogoda", "Film"]


def test_an_epg_cache_that_raises_costs_an_empty_grid(live_bridge, factory, receiver):
    receiver.epg.raises = True
    factory.client.clear()
    generate(live_bridge)
    payload = factory.client.last(ULUBIONE).json()
    assert all(channel["events"] == [] for channel in payload["channels"])


def test_the_query_asks_for_a_bounded_window(live_bridge, receiver):
    """„Every event this service ever knew" is a week of television per channel."""
    receiver.epg.queries = []
    generate(live_bridge)
    _sref, kind, begin, minutes = receiver.epg.queries[0][1]
    assert kind == 0
    assert begin == -1
    assert minutes == 4 * epggrid.MINUTES_PER_EVENT + epggrid.EXTRA_MINUTES


def test_a_box_with_no_epg_cache_has_no_grid(make_bridge, factory, settings, receiver,
                                             monkeypatch):
    monkeypatch.setattr(epggrid, "epg_cache", lambda: None)
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert "epg_grid" not in bridge.capabilities()


def test_the_grid_needs_the_channel_list(make_bridge, factory, settings, receiver, monkeypatch):
    from MQTTBridge import channels as channels_module

    monkeypatch.setattr(channels_module, "_service_center", lambda: None)
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert "epg_grid" not in bridge.capabilities()


def test_a_row_the_cache_answers_with_nonsense_is_skipped():
    assert epggrid._event(["only one field"]) == (None, None)
    assert epggrid._event([]) == (None, None)


def test_the_grid_comes_back_on_a_reconnect(live_bridge, factory):
    factory.client.clear()
    factory.client.fire_connect()
    assert factory.client.last(ULUBIONE) is not None
    assert factory.client.last(SPORT) is not None
