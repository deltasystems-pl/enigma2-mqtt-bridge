"""What lands on the broker, and when.

Everything here is an assertion about `docs/TOPICS.md` rather than about the
code: the retain flag, the QoS, the topic spelling, the payload fields, and the
rule that `on_connect` publishes the lot *every* time rather than only at
start-up.
"""

from MQTTBridge import boxinfo, discovery
from MQTTBridge.bridge import Bridge, Publisher
from MQTTBridge.version import __version__

NODE = "vuuno4kse_005301"
AVAILABILITY = "enigma2/" + NODE + "/availability"
INFO = "enigma2/" + NODE + "/info"
ANNOUNCEMENT = "enigma2mqtt/discovery/" + NODE + "/config"
COMMAND_WILDCARD = "enigma2/" + NODE + "/cmd/#"


def test_availability_is_online_retained_on_connect(connected_bridge, factory):
    entry = factory.client.last(AVAILABILITY)
    assert entry.text == "online"
    assert entry.retain is True
    # The contract: state topics are QoS 0.
    assert entry.qos == 0


def test_info_carries_every_documented_field(connected_bridge, factory):
    payload = factory.client.last(INFO).json()
    assert set(payload) == {
        "image", "enigma", "plugin", "boxtype", "mac", "ip", "uptime", "ha_mode", "capabilities",
    }
    assert payload["plugin"] == __version__
    assert payload["boxtype"] == "vuuno4kse"
    assert payload["enigma"] == "5.4"
    assert payload["image"] == "openvix 6.6.007"
    assert payload["ha_mode"] == "discovery"
    assert isinstance(payload["uptime"], int)
    assert isinstance(payload["capabilities"], list)


def test_info_is_retained(connected_bridge, factory):
    assert factory.client.last(INFO).retain is True


def test_capabilities_name_only_what_this_build_publishes(connected_bridge, factory):
    assert factory.client.last(INFO).json()["capabilities"] == ["info", "reset"]


def test_a_publisher_adds_its_name_and_its_snapshot(make_bridge, factory, settings):
    class Power(Publisher):
        name = "power"
        # `power` is `on`/`standby`, not JSON — the contract says so.
        raw = ("power",)

        def snapshot(self):
            return {"power": "on", "volume": {"level": 35, "muted": False}}

    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge()
    bridge.register_publisher(Power())
    bridge.start()
    factory.client.fire_connect()

    assert factory.client.last(INFO).json()["capabilities"] == ["info", "reset", "power"]
    assert factory.client.last("enigma2/" + NODE + "/power").text == "on"
    assert factory.client.last("enigma2/" + NODE + "/volume").json() == {
        "level": 35, "muted": False
    }


def test_the_announcement_is_where_the_integration_looks(connected_bridge, factory):
    payload = factory.client.last(ANNOUNCEMENT).json()
    assert payload["node_id"] == NODE
    assert payload["name"] == "Living room receiver"
    assert payload["base_topic"] == "enigma2"
    assert payload["ha_mode"] == "discovery"
    assert payload["capabilities"] == ["info", "reset"]
    assert set(payload) == {
        "node_id", "name", "base_topic", "image", "enigma", "plugin", "boxtype", "mac", "ip",
        "capabilities", "ha_mode",
    }
    assert factory.client.last(ANNOUNCEMENT).retain is True


def test_commands_are_subscribed_at_qos_1(connected_bridge, factory):
    assert (COMMAND_WILDCARD, 1) in factory.client.subscriptions


def test_the_will_is_registered_before_the_connection(connected_bridge, factory):
    will = factory.client.will
    assert will.topic == AVAILABILITY
    assert will.payload == "offline"
    assert will.retain is True
    assert will.qos == 1


def test_the_session_is_configured_the_way_the_contract_says(connected_bridge, factory):
    client = factory.client
    assert client.client_id == "mqttbridge-" + NODE
    assert client.reconnect_delay == (1, 60)
    assert client.connect_calls == [("10.0.0.5", 1883, 30)]
    assert client.loop_started is True


def test_credentials_are_only_set_when_there_is_a_username(make_bridge, factory, settings):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    make_bridge().start()
    assert factory.client.credentials is None

    settings.username.value = "enigma2box"
    settings.password.value = "hunter2"
    make_bridge().start()
    assert factory.clients[-1].credentials == ("enigma2box", "hunter2")


def test_tls_uses_the_configured_ca(make_bridge, factory, settings):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.tls.value = True
    settings.ca_file.value = "/etc/ssl/certs/broker.pem"
    make_bridge().start()
    assert factory.client.tls == "/etc/ssl/certs/broker.pem"


def test_a_second_connect_republishes_everything(connected_bridge, factory):
    factory.client.clear()
    factory.client.fire_connect()

    topics = factory.client.topics()
    assert AVAILABILITY in topics
    assert INFO in topics
    assert ANNOUNCEMENT in topics
    assert (COMMAND_WILDCARD, 1) in factory.client.subscriptions


def test_ha_mode_off_retracts_the_announcement_but_keeps_the_state(make_bridge, factory, settings):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.ha_mode.value = "off"
    make_bridge().start()
    factory.client.fire_connect()

    assert factory.client.last(ANNOUNCEMENT).text == ""
    assert factory.client.last(ANNOUNCEMENT).retain is True
    assert factory.client.last(AVAILABILITY).text == "online"
    assert factory.client.last(INFO) is not None


def test_no_host_means_idle_and_no_session(make_bridge, factory, plugin_log):
    bridge = make_bridge()
    bridge.start()

    assert bridge.running is False
    assert bridge.idle_reason == "no broker address is configured"
    assert factory.clients == []
    assert "no broker address is configured" in plugin_log()


def test_disabled_means_idle(make_bridge, factory, settings):
    settings.enabled.value = False
    settings.host.value = "10.0.0.5"
    bridge = make_bridge()
    bridge.start()

    assert bridge.running is False
    assert factory.clients == []


def test_start_never_raises(make_bridge, monkeypatch, settings, plugin_log):
    settings.host.value = "10.0.0.5"
    bridge = make_bridge()

    def explode(*args, **kwargs):
        raise RuntimeError("the image is unusual")

    monkeypatch.setattr(bridge, "_ensure_identity", explode)
    assert bridge.start() is bridge
    assert bridge.running is False
    assert "the receiver is unaffected" in plugin_log()


def test_the_node_id_is_derived_and_kept(make_bridge, settings, monkeypatch):
    from Components.config import configfile

    monkeypatch.setattr(boxinfo, "mac_address", lambda: "00:00:5e:00:53:01")
    settings.host.value = "10.0.0.5"
    bridge = make_bridge()
    bridge.start()

    assert settings.node_id.value == NODE
    assert settings.node_id.saved_value == NODE
    assert settings.friendly_name.value == "vuuno4kse"
    assert configfile.save_calls == 1


def test_shutdown_says_goodbye_and_disconnects(connected_bridge, factory):
    factory.client.clear()
    connected_bridge.stop()

    entry = factory.client.last(AVAILABILITY)
    assert entry.text == "offline"
    assert entry.retain is True
    assert factory.client.disconnect_calls == 1
    assert factory.client.loop_started is False


def test_shutdown_without_a_session_is_harmless(make_bridge):
    bridge = make_bridge()
    bridge.stop()
    assert bridge.running is False


def test_a_disconnect_does_not_raise(connected_bridge, factory):
    factory.client.fire_disconnect(reason_code=7)
    assert connected_bridge.connected is False


def test_a_refused_connection_is_logged_and_publishes_nothing(make_bridge, factory, settings,
                                                              plugin_log):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE

    class Refusal:
        is_failure = True

        def __str__(self):
            return "Not authorised"

    make_bridge().start()
    factory.client.fire_connect(reason_code=Refusal())

    assert factory.client.published == []
    assert "refused the connection" in plugin_log()


# ------------------------------------------------------------------- state file --


def test_the_state_file_remembers_what_was_retained(connected_bridge, state_path):
    import json

    with open(state_path, encoding="utf-8") as handle:
        stored = json.load(handle)

    assert stored["version"] == 1
    assert AVAILABILITY in stored["retained_topics"]
    assert INFO in stored["retained_topics"]
    assert ANNOUNCEMENT in stored["retained_topics"]


def test_the_state_file_survives_a_restart(connected_bridge, state_path):
    reopened = discovery.StateStore(path=state_path)
    assert AVAILABILITY in reopened.retained_topics
    assert reopened.knows(ANNOUNCEMENT)


def test_an_unreadable_state_file_starts_from_empty(tmp_path, plugin_log):
    path = tmp_path / "state.json"
    path.write_text("{not json", encoding="utf-8")
    store = discovery.StateStore(path=str(path))
    assert store.retained_topics == []
    assert "not readable JSON" in plugin_log()


def test_the_state_file_is_written_atomically(tmp_path):
    path = tmp_path / "state.json"
    store = discovery.StateStore(path=str(path))
    store.remember("enigma2/x/info")
    assert store.save() is True
    # Nothing left behind: a crash mid-write cannot leave a half file in place.
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_a_publisher_that_cannot_bind_is_dropped(make_bridge, factory, settings, plugin_log):
    class Missing(Publisher):
        name = "tuner"

        def start(self):
            return False

    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge()
    bridge.register_publisher(Missing())
    bridge.start()
    factory.client.fire_connect()

    assert factory.client.last(INFO).json()["capabilities"] == ["info", "reset"]
    assert "does not provide the tuner hooks" in plugin_log()


def test_reload_restarts_the_session_with_the_new_settings(connected_bridge, factory, settings):
    settings.host.value = "10.0.0.9"
    connected_bridge.reload()

    assert factory.clients[0].disconnect_calls == 1
    assert factory.clients[-1].connect_calls == [("10.0.0.9", 1883, 30)]


def test_reload_retracts_topics_the_new_node_id_orphans(connected_bridge, factory, settings):
    settings.node_id.value = "vuuno4kse_beef01"
    connected_bridge.reload()

    retractions = [e for e in factory.clients[0].published if e.text == "" and e.retain]
    assert AVAILABILITY in [e.topic for e in retractions]
    assert INFO in [e.topic for e in retractions]


def test_the_default_state_path_sits_beside_enigma2s_settings(monkeypatch):
    monkeypatch.setattr(discovery.os.path, "isdir", lambda path: path == "/etc/enigma2")
    monkeypatch.setattr(discovery.os, "access", lambda path, mode: True)
    assert discovery.default_state_path() == "/etc/enigma2/mqttbridge-state.json"


def test_the_state_path_falls_back_when_etc_is_read_only(monkeypatch):
    monkeypatch.setattr(discovery.os.path, "isdir", lambda path: False)
    assert discovery.default_state_path("/opt/plugin").endswith("/opt/plugin/mqttbridge-state.json")


def test_a_bridge_built_with_no_arguments_uses_the_real_settings():
    """Constructing one must not need a session, a broker or a file."""
    from MQTTBridge import config as settings_module

    bridge = Bridge()
    assert bridge.settings is settings_module.settings
    assert bridge.base_topic == "enigma2"
