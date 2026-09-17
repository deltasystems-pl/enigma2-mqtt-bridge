"""Selecting a bouquet changes the receiver's real channel-list context."""

import json

from conftest import FIRST_BOUQUET, POLSAT, SECOND_BOUQUET, TVN, TVP1

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


def test_late_infobar_binding_keeps_capability_and_publishes_when_ready(
    make_bridge, factory, settings, receiver
):
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()

    publisher = bridge.publisher("bouquet_context")
    assert publisher is not None
    assert "bouquet_context" in bridge.capabilities()
    assert factory.client.last(BOUQUET) is None

    receiver.with_channel_list()
    publisher._ticker.timer.fire()
    assert factory.client.last(BOUQUET).json()["sref"] == FIRST_BOUQUET


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
