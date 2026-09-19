"""Selecting a bouquet changes the receiver's real channel-list context."""

import json

from conftest import FIRST_BOUQUET, POLSAT, SECOND_BOUQUET, TVN, TVP1

from MQTTBridge import bouquet
from MQTTBridge import channels as channels_module

NODE = "vuuno4kse_005301"

ROOT = "enigma2/" + NODE
BOUQUET = ROOT + "/bouquet"


def started(make_bridge, settings, receiver):
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    servicelist = receiver.with_channel_list()
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    return bridge, servicelist


def select(factory, sref):
    factory.client.fire_message(
        ROOT + "/cmd/bouquet", json.dumps({"sref": sref}).encode(), retain=False
    )


def test_current_channel_is_preserved_when_it_belongs_to_selected_bouquet(
    make_bridge, factory, settings, receiver
):
    _bridge, servicelist = started(make_bridge, settings, receiver)

    select(factory, FIRST_BOUQUET)

    assert receiver.nav.sref == TVP1
    assert servicelist.getRoot().toString() == FIRST_BOUQUET
    assert servicelist.getCurrentSelection().toString() == TVP1
    assert servicelist.zaps == 0
    assert factory.client.last(BOUQUET).json() == {
        "name": "Ulubione TV",
        "sref": FIRST_BOUQUET,
    }


def test_reselecting_the_same_bouquet_still_publishes_a_fresh_ack(
    make_bridge, factory, settings, receiver
):
    started(make_bridge, settings, receiver)
    factory.client.clear()

    select(factory, FIRST_BOUQUET)
    select(factory, FIRST_BOUQUET)

    assert len(factory.client.all_for(BOUQUET)) == 2


def test_selecting_another_bouquet_tunes_its_first_playable_channel(
    make_bridge, factory, settings, receiver
):
    _bridge, servicelist = started(make_bridge, settings, receiver)

    select(factory, SECOND_BOUQUET)

    assert receiver.nav.sref == POLSAT
    assert servicelist.getRoot().toString() == SECOND_BOUQUET
    assert servicelist.getCurrentSelection().toString() == POLSAT
    assert servicelist.zaps == 1
    assert factory.client.last(BOUQUET).json()["sref"] == SECOND_BOUQUET


def test_channel_up_down_follow_the_selected_bouquet(
    make_bridge, factory, settings, receiver
):
    _bridge, servicelist = started(make_bridge, settings, receiver)
    receiver.nav.sref = POLSAT

    select(factory, FIRST_BOUQUET)
    assert receiver.nav.sref == TVP1
    servicelist.channel_down()
    assert receiver.nav.sref == TVN
    servicelist.channel_up()
    assert receiver.nav.sref == TVP1


def test_tv_bouquet_selection_is_refused_in_radio_mode_without_side_effects(
    make_bridge, factory, settings, receiver
):
    _bridge, servicelist = started(make_bridge, settings, receiver)
    radio = type(servicelist.bouquet_root)(
        '1:7:2:0:0:0:0:0:0:0:FROM BOUQUET "bouquets.radio" ORDER BY bouquet'
    )
    servicelist.bouquet_root = radio
    servicelist.path[:] = [radio]
    servicelist.root = radio
    servicelist.mode = 1
    old_service = receiver.nav.sref
    old_saved = servicelist.saved_roots

    select(factory, FIRST_BOUQUET)

    assert [item.toString() for item in servicelist.path] == [radio.toString()]
    assert servicelist.getRoot().toString() == radio.toString()
    assert receiver.nav.sref == old_service
    assert servicelist.saved_roots == old_saved
    assert "television mode" in factory.client.last(ROOT + "/last_error").json()["error"]


def test_unknown_or_empty_bouquet_is_refused_without_side_effects(
    make_bridge, factory, settings, receiver
):
    bridge, servicelist = started(make_bridge, settings, receiver)
    old_root = servicelist.getRoot().toString()
    old_service = receiver.nav.sref

    select(factory, "1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"private.tv\" ORDER BY bouquet")
    assert servicelist.getRoot().toString() == old_root
    assert receiver.nav.sref == old_service
    assert "not in the configured channel list" in factory.client.last(
        ROOT + "/last_error"
    ).json()["error"]

    channels = bridge.publisher("channels")
    channels._bouquets.append({"name": "Empty", "sref": "empty", "channels": []})
    select(factory, "empty")
    assert servicelist.getRoot().toString() == old_root
    assert receiver.nav.sref == old_service
    assert "no playable channels" in factory.client.last(ROOT + "/last_error").json()[
        "error"
    ]


def test_command_payload_is_exact_and_context_is_read_back(
    make_bridge, factory, settings, receiver
):
    _bridge, servicelist = started(make_bridge, settings, receiver)
    for payload in (b"not-json", b'{}', b'{"sref":"x","name":"y"}'):
        factory.client.fire_message(ROOT + "/cmd/bouquet", payload, retain=False)
        assert factory.client.last(ROOT + "/last_error") is not None

    servicelist.getRoot = lambda: None
    select(factory, FIRST_BOUQUET)
    assert "did not enter" in factory.client.last(ROOT + "/last_error").json()["error"]


def test_the_capability_is_claimed_only_once_the_list_can_be_read(
    make_bridge, factory, settings, receiver
):
    """An unreadable service list publishes no `bouquet`, so it claims nothing."""
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()

    publisher = bridge.publisher("bouquet_context")
    assert publisher is not None
    assert "bouquet_context" not in bridge.capabilities()
    assert "bouquet_context" not in factory.client.last(ROOT + "/info").json()["capabilities"]
    assert factory.client.last(BOUQUET) is None

    receiver.with_channel_list()
    publisher._ticker.timer.fire()

    assert factory.client.last(BOUQUET).json()["sref"] == FIRST_BOUQUET
    assert "bouquet_context" in bridge.capabilities()
    # The capability appeared after the connect, so what was published on the
    # connect is out of date and is said again.
    assert "bouquet_context" in factory.client.last(ROOT + "/info").json()["capabilities"]
    announcement = factory.client.last("enigma2mqtt/discovery/" + NODE + "/config")
    assert "bouquet_context" in announcement.json()["capabilities"]


def test_an_image_that_never_offers_a_list_slows_down_but_keeps_watching(
    make_bridge, factory, settings, receiver, plugin_log
):
    """Giving up for good would need a restart to undo; slowing down does not."""
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    publisher = bridge.publisher("bouquet_context")

    for _ in range(bouquet.BIND_ATTEMPTS + 5):
        publisher._ticker.timer.fire()

    assert "bouquet_context" not in bridge.capabilities()
    assert plugin_log().count("bouquet_context is not claimed") == 1
    assert publisher._ticker.timer.running is True
    assert publisher._ticker.timer.started[0] == bouquet.SLOW_POLL_MILLISECONDS

    # The slow tick is what makes the late arrival recoverable.
    receiver.with_channel_list()
    publisher._ticker.timer.fire()

    assert "bouquet_context" in bridge.capabilities()
    assert factory.client.last(BOUQUET).json()["sref"] == FIRST_BOUQUET
    assert publisher._ticker.timer.started[0] == bouquet.POLL_MILLISECONDS


def test_a_root_that_is_no_configured_bouquet_is_published_as_none(
    make_bridge, factory, settings, receiver, plugin_log
):
    """🔴 Radio, the movie list, a bouquet the filter leaves out: all ordinary."""
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    servicelist = receiver.with_channel_list()
    elsewhere = type(servicelist.bouquet_root)(
        '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.private.tv" ORDER BY bouquet'
    )
    servicelist.root = elsewhere
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    publisher = bridge.publisher("bouquet_context")

    for _ in range(bouquet.BIND_ATTEMPTS + 5):
        publisher._ticker.timer.fire()

    # The list reads perfectly well; it simply is not showing one of ours.
    assert factory.client.last(BOUQUET).json() == {"name": None, "sref": None}
    assert "bouquet_context" in bridge.capabilities()
    assert publisher._ticker.timer.started[0] == bouquet.POLL_MILLISECONDS
    assert "bouquet_context is not claimed" not in plugin_log()

    servicelist.root = type(servicelist.bouquet_root)(FIRST_BOUQUET)
    publisher._ticker.timer.fire()
    assert factory.client.last(BOUQUET).json()["sref"] == FIRST_BOUQUET


def test_a_box_with_no_bouquet_list_selects_inside_the_root_it_read(
    make_bridge, factory, settings, receiver
):
    """🔴 „Multiple bouquets" off: the list came from favourites, so entering
    `bouquets.tv` first would build — and persist — a path the box does not use."""
    favourites = channels_module.bouquet_roots()[1]
    receiver.service_center.contents = {favourites: [(TVP1, "TVP 1 HD"), (TVN, "TVN HD")]}
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    servicelist = receiver.with_channel_list()
    servicelist.bouquets = {favourites: [TVP1, TVN]}
    bridge = make_bridge(session=receiver.session)
    bridge.start()

    select(factory, favourites)

    assert [item.toString() for item in servicelist.path] == [favourites]
    assert servicelist.getRoot().toString() == favourites
    assert factory.client.last(BOUQUET).json()["sref"] == favourites
    assert factory.client.last(ROOT + "/last_error") is None


def test_failed_zap_restores_full_path_selection_and_persisted_root(
    make_bridge, factory, settings, receiver
):
    _bridge, servicelist = started(make_bridge, settings, receiver)
    old_path = [item.toString() for item in servicelist.servicePath]
    # A highlighted list row is not necessarily the service currently playing.
    servicelist.setCurrentSelection(type(servicelist.bouquet_root)(TVN))
    old_saved = servicelist.saved_roots

    def fail_zap():
        raise RuntimeError("tuner refused")

    servicelist.zap = fail_zap
    select(factory, SECOND_BOUQUET)

    assert [item.toString() for item in servicelist.servicePath] == old_path
    assert servicelist.getRoot().toString() == FIRST_BOUQUET
    assert servicelist.getCurrentSelection().toString() == TVN
    assert receiver.nav.sref == TVP1
    assert servicelist.saved_roots == old_saved + 1
    assert "tuner refused" in factory.client.last(ROOT + "/last_error").json()["error"]


def test_failure_after_tune_restores_previous_service_and_context(
    make_bridge, factory, settings, receiver
):
    bridge, servicelist = started(make_bridge, settings, receiver)
    old_path = [item.toString() for item in servicelist.servicePath]
    servicelist.setCurrentSelection(type(servicelist.bouquet_root)(TVN))
    old_saved = servicelist.saved_roots
    original_publish = bridge.publish_json

    def fail_bouquet_publish(topic, payload, retain=True):
        if topic == BOUQUET:
            raise RuntimeError("broker write failed")
        return original_publish(topic, payload, retain=retain)

    bridge.publish_json = fail_bouquet_publish
    select(factory, SECOND_BOUQUET)

    assert receiver.nav.sref == TVP1
    assert [item.toString() for item in servicelist.servicePath] == old_path
    assert servicelist.getRoot().toString() == FIRST_BOUQUET
    assert servicelist.getCurrentSelection().toString() == TVN
    assert servicelist.saved_roots == old_saved + 2
    assert "broker write failed" in factory.client.last(ROOT + "/last_error").json()["error"]
