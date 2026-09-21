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


def test_config_persists_all_values_rebinds_hooks_and_publishes_info(
        live_bridge, factory, settings, receiver):
    settings.screenshot_delay.value = 9
    hdd = live_bridge.publisher("hdd")
    epg_grid = live_bridge.publisher("epg_grid")
    factory.client.clear()
    send(factory, "config", b'{"publish_keys":false,"screenshot":"interval",'
          b'"screenshot_interval":90}')

    assert settings.publish_keys.value is False
    assert settings.publish_keys.saved_value is False
    assert settings.screenshot.value == "interval"
    assert settings.screenshot.saved_value == "interval"
    assert settings.screenshot_interval.value == 90
    assert settings.screenshot_interval.saved_value == 90
    assert settings.screenshot_delay.value == 9
    assert receiver.actions.bound == []
    assert live_bridge.publisher("hdd") is hdd
    assert live_bridge.publisher("epg_grid") is epg_grid
    assert live_bridge.publisher("screenshot")._interval.timer.started == (90000, False)
    assert factory.client.last(INFO).json()["settings"] == {
        "publish_keys": False,
        "screenshot": "interval",
        "screenshot_interval": 90,
        "screenshot_delay": 9,
        "cam_telemetry": False,
        "oscam_telemetry": False,
        "deep_standby_allowed": False,
    }


def test_config_cannot_write_the_permission_it_can_read(connected_bridge, factory, settings):
    """A read-only member of `info.settings` is still an unknown key to `cmd/config`.

    The permission is published so a consumer can hide the buttons the box would
    refuse; publishing it must not turn it into a remote switch, because the
    whole point of it is that it is granted at the television.
    """
    assert factory.client.last(INFO).json()["settings"]["deep_standby_allowed"] is False
    factory.client.clear()
    send(factory, "config", b'{"publish_keys":true,"screenshot":"on_zap",'
          b'"screenshot_interval":60,"deep_standby_allowed":true}')

    payload = factory.client.last(LAST_ERROR).json()
    assert payload["cmd"] == "config"
    assert payload["error"] == "the config object contains unknown settings"
    assert settings.deep_standby_allowed.value is False
    assert settings.deep_standby_allowed.saved_value is False
    # Nothing was applied, so nothing is acknowledged either.
    assert factory.client.last(INFO) is None


def test_config_persists_a_custom_post_zap_delay(live_bridge, factory, settings):
    send(factory, "config", b'{"publish_keys":true,"screenshot":"on_zap",'
          b'"screenshot_interval":60,"screenshot_delay":8}')
    assert settings.screenshot_delay.value == 8
    assert settings.screenshot_delay.saved_value == 8
    assert factory.client.last(INFO).json()["settings"]["screenshot_delay"] == 8


def test_config_enables_cam_telemetry(live_bridge, factory, settings):
    send(factory, "config", b'{"publish_keys":true,"screenshot":"on_zap",'
          b'"screenshot_interval":60,"screenshot_delay":4,"cam_telemetry":true}')
    assert settings.cam_telemetry.value is True
    assert settings.cam_telemetry.saved_value is True
    assert live_bridge.publisher("cam") is not None


def test_config_enables_oscam_without_accepting_its_credentials(
    live_bridge, factory, settings, monkeypatch
):
    from MQTTBridge.oscam import OscamPublisher

    monkeypatch.setattr(OscamPublisher, "start", lambda _self: True)
    send(
        factory,
        "config",
        b'{"publish_keys":true,"screenshot":"on_zap","screenshot_interval":60,'
        b'"oscam_telemetry":true}',
    )
    assert settings.oscam_telemetry.value is True
    assert settings.oscam_telemetry.saved_value is True
    assert live_bridge.publisher("oscam") is not None


def test_repeated_config_does_not_restart_unrelated_publishers(
        live_bridge, factory):
    hdd = live_bridge.publisher("hdd")
    worker_lock = hdd._worker_lock
    ticker = hdd._ticker
    send(factory, "config", b'{"publish_keys":false,"screenshot":"off",'
          b'"screenshot_interval":60}')
    send(factory, "config", b'{"publish_keys":true,"screenshot":"on_zap",'
          b'"screenshot_interval":60}')
    assert live_bridge.publisher("hdd") is hdd
    assert live_bridge.publisher("hdd")._worker_lock is worker_lock
    assert live_bridge.publisher("hdd")._ticker is ticker


def test_switching_screenshots_off_retracts_the_private_image(
        live_bridge, factory):
    live_bridge.publish_raw(live_bridge.topic("screen"), b"private image")
    send(factory, "config", b'{"publish_keys":true,"screenshot":"off",'
          b'"screenshot_interval":60}')
    retraction = factory.client.last(live_bridge.topic("screen"))
    assert retraction.payload == b""
    assert retraction.retain is True


def test_config_rejects_the_whole_patch_before_changing_anything(
        connected_bridge, factory, settings):
    before = (settings.publish_keys.value, settings.screenshot.value,
              settings.screenshot_interval.value)
    factory.client.clear()
    send(factory, "config", b'{"publish_keys":false,"screenshot":"interval",'
          b'"screenshot_interval":4}')
    assert (settings.publish_keys.value, settings.screenshot.value,
            settings.screenshot_interval.value) == before
    assert factory.client.last(LAST_ERROR).json()["cmd"] == "config"
    assert factory.client.last(INFO) is None


def test_config_restores_runtime_values_when_persistence_fails(
        connected_bridge, factory, settings, monkeypatch):
    before = (settings.publish_keys.value, settings.screenshot.value,
              settings.screenshot_interval.value)
    from Components.config import configfile

    def fail():
        raise OSError("disk full")

    monkeypatch.setattr(configfile, "save", fail)
    factory.client.clear()
    send(factory, "config", b'{"publish_keys":false,"screenshot":"off",'
          b'"screenshot_interval":120}')
    assert (settings.publish_keys.value, settings.screenshot.value,
            settings.screenshot_interval.value) == before
    assert (settings.publish_keys.saved_value, settings.screenshot.saved_value,
            settings.screenshot_interval.saved_value) == before
    assert factory.client.last(LAST_ERROR).json()["error"] == (
        "could not persist the plugin settings"
    )
    assert factory.client.last(INFO) is None


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
