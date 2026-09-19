"""Commands, their guards, and the fact that the answer is a state topic.

The two guards are the interesting part. A retained command is the trap the
contract warns publishers about, and it costs nothing to be robust against a
publisher who did not read it; an oversized payload is somebody else's mistake
being parsed on the thread that draws the television.
"""

NODE = "vuuno4kse_005301"
INFO = "enigma2/" + NODE + "/info"
LAST_ERROR = "enigma2/" + NODE + "/last_error"
ANNOUNCEMENT = "enigma2mqtt/discovery/" + NODE + "/config"


def send(factory, name, payload, retain=False):
    factory.client.fire_message("enigma2/" + NODE + "/cmd/" + name, payload, retain=retain)


def test_ha_mode_changes_the_setting_and_republishes(connected_bridge, factory, settings):
    factory.client.clear()
    send(factory, "ha_mode", b"integration")

    assert settings.ha_mode.value == "integration"
    assert settings.ha_mode.saved_value == "integration"
    assert factory.client.last(INFO).json()["ha_mode"] == "integration"
    assert factory.client.last(ANNOUNCEMENT).json()["ha_mode"] == "integration"


def test_ha_mode_off_retracts_the_announcement(connected_bridge, factory):
    factory.client.clear()
    send(factory, "ha_mode", b"off")

    assert factory.client.last(ANNOUNCEMENT).text == ""
    assert factory.client.last(ANNOUNCEMENT).retain is True
    assert factory.client.last(INFO).json()["ha_mode"] == "off"


def test_ha_mode_is_case_insensitive(connected_bridge, factory, settings):
    send(factory, "ha_mode", b"  Integration  ")
    assert settings.ha_mode.value == "integration"


def test_ha_mode_republishes_even_when_it_did_not_change(connected_bridge, factory):
    """The republished `info` is the acknowledgement, so it must be unconditional."""
    factory.client.clear()
    send(factory, "ha_mode", b"discovery")
    assert factory.client.last(INFO) is not None


def test_an_invalid_ha_mode_goes_to_last_error(connected_bridge, factory, settings):
    send(factory, "ha_mode", b"sometimes")

    payload = factory.client.last(LAST_ERROR).json()
    assert payload["cmd"] == "ha_mode"
    assert "sometimes" in payload["error"]
    assert isinstance(payload["ts"], int)
    assert payload["ts"] > 0
    assert factory.client.last(LAST_ERROR).retain is True
    assert settings.ha_mode.value == "discovery"


def test_an_unknown_command_goes_to_last_error(connected_bridge, factory):
    send(factory, "teleport", b"PRESS")

    payload = factory.client.last(LAST_ERROR).json()
    assert payload["cmd"] == "teleport"
    assert payload["error"] == "unknown command"


def test_last_error_is_cleared_by_the_next_success(connected_bridge, factory):
    send(factory, "teleport", b"PRESS")
    assert factory.client.last(LAST_ERROR).text != ""

    send(factory, "discovery", b"PRESS")
    entry = factory.client.last(LAST_ERROR)
    assert entry.text == ""
    assert entry.retain is True


def test_a_success_with_no_outstanding_error_publishes_nothing_extra(connected_bridge, factory):
    factory.client.clear()
    send(factory, "discovery", b"PRESS")
    assert LAST_ERROR not in factory.client.topics()


def test_a_retained_command_is_discarded_and_logged(connected_bridge, factory, settings,
                                                    plugin_log):
    factory.client.clear()
    send(factory, "ha_mode", b"integration", retain=True)

    assert settings.ha_mode.value == "discovery"
    assert factory.client.published == []
    assert "RETAINED" in plugin_log()


def test_an_oversized_payload_is_discarded(connected_bridge, factory, plugin_log):
    factory.client.clear()
    send(factory, "ha_mode", b"x" * 4097)

    assert factory.client.published == []
    assert "over the 4096 byte limit" in plugin_log()


def test_a_payload_at_the_limit_is_still_read(connected_bridge, factory):
    send(factory, "ha_mode", b"integration".ljust(4096))
    # Whitespace-padded, so it is still a valid mode.
    assert factory.client.last(INFO).json()["ha_mode"] == "integration"


def test_a_message_on_a_foreign_topic_is_ignored(connected_bridge, factory, plugin_log):
    factory.client.clear()
    factory.client.fire_message("frigate/events", b"{}")

    assert factory.client.published == []
    assert "unexpected topic" in plugin_log()


def test_a_handler_that_raises_becomes_a_last_error(connected_bridge, factory, monkeypatch):
    def explode(mode):
        raise RuntimeError("enigma2 said no")

    monkeypatch.setattr(connected_bridge, "set_ha_mode", explode)
    send(factory, "ha_mode", b"integration")

    payload = factory.client.last(LAST_ERROR).json()
    assert payload["cmd"] == "ha_mode"
    assert "RuntimeError" in payload["error"]


def test_an_undecodable_payload_does_not_raise(connected_bridge, factory):
    send(factory, "ha_mode", b"\xff\xfe")
    assert factory.client.last(LAST_ERROR).json()["cmd"] == "ha_mode"


def test_the_error_echo_is_bounded(connected_bridge, factory):
    send(factory, "ha_mode", b"z" * 400)
    assert len(factory.client.last(LAST_ERROR).json()["error"]) < 200


# ------------------------------------------------------------------------ reset --


def test_reset_retracts_everything_then_puts_it_straight_back(connected_bridge, factory):
    factory.client.clear()
    send(factory, "reset", b"PRESS")

    published = factory.client.published
    retractions = {entry.topic for entry in published if entry.text == "" and entry.retain}
    assert "enigma2/" + NODE + "/availability" in retractions
    assert INFO in retractions
    assert ANNOUNCEMENT in retractions

    # And then the snapshot returns, after the retraction, on the same topics.
    assert factory.client.last("enigma2/" + NODE + "/availability").text == "online"
    assert factory.client.last(INFO).json()["plugin"]
    assert factory.client.last(ANNOUNCEMENT).json()["node_id"] == NODE


def test_reset_forgets_and_then_relearns_what_is_retained(connected_bridge, state_path):
    from MQTTBridge.discovery import StateStore

    connected_bridge.reset_retained()
    reopened = StateStore(path=state_path)
    assert INFO in reopened.retained_topics
    assert ANNOUNCEMENT in reopened.retained_topics


def test_reset_retracts_a_topic_recorded_before_this_process_started(make_bridge, factory,
                                                                    settings, state_path):
    from MQTTBridge.discovery import StateStore

    earlier = StateStore(path=state_path)
    earlier.remember("enigma2/" + NODE + "/service")
    earlier.save()

    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(state_store=StateStore(path=state_path))
    bridge.start()
    factory.client.fire_connect()
    factory.client.clear()

    bridge.reset_retained()
    retracted = {e.topic for e in factory.client.published if e.text == "" and e.retain}
    assert "enigma2/" + NODE + "/service" in retracted


def test_reset_clears_an_outstanding_last_error(connected_bridge, factory):
    send(factory, "teleport", b"PRESS")
    factory.client.clear()
    send(factory, "reset", b"PRESS")

    assert factory.client.last(LAST_ERROR).text == ""


def test_a_last_error_from_a_previous_run_is_cleared_by_the_first_success(
    make_bridge, factory, settings, state_path
):
    from MQTTBridge.discovery import StateStore

    earlier = StateStore(path=state_path)
    earlier.remember(LAST_ERROR)
    earlier.save()

    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(state_store=StateStore(path=state_path))
    bridge.start()
    factory.client.fire_connect()
    factory.client.clear()

    send(factory, "discovery", b"PRESS")
    assert factory.client.last(LAST_ERROR).text == ""


# -------------------------------------------------------------------- discovery --


def test_cmd_discovery_republishes_the_announcement(connected_bridge, factory):
    factory.client.clear()
    send(factory, "discovery", b"PRESS")
    assert factory.client.last(ANNOUNCEMENT).json()["node_id"] == NODE


def test_a_bridge_with_no_feature_areas_still_announces_the_plugins_own_entities(
    connected_bridge, factory
):
    """A box that bound nothing can still be restarted and still has an uptime.

    There is no session here, so no publisher registered and no capability was
    claimed — and the discovery payload is exactly the components that depend on
    no capability at all.
    """
    device = factory.client.last("homeassistant/device/" + NODE + "/config").json()
    assert set(device["cmps"]) == {"restart_gui", "refresh_discovery", "uptime"}
    # And nothing claiming to know what is playing.
    assert "channel" not in device["cmps"]
