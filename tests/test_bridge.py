"""What lands on the broker, and when.

Everything here is an assertion about `docs/TOPICS.md` rather than about the
code: the retain flag, the QoS, the topic spelling, the payload fields, and the
rule that `on_connect` publishes the lot *every* time rather than only at
start-up.
"""

import pytest

from MQTTBridge import boxinfo, discovery
from MQTTBridge import config as settings_module
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
        "image", "enigma", "plugin", "boxtype", "mac", "ip", "uptime", "wol", "ha_mode",
        "settings", "capabilities",
    }
    assert set(payload["wol"]) == {"supported", "armed", "iface", "mechanism"}
    assert payload["plugin"] == __version__
    assert payload["boxtype"] == "vuuno4kse"
    assert payload["enigma"] == "2024-09-11-Release"
    assert payload["image"] == "openvix 6.6.007"
    assert payload["ha_mode"] == "discovery"
    assert isinstance(payload["uptime"], int)
    assert isinstance(payload["capabilities"], list)
    assert payload["settings"] == {
        "publish_keys": True,
        "screenshot": "on_zap",
        "screenshot_interval": 60,
        "screenshot_delay": 4,
        "cam_telemetry": False,
        "oscam_telemetry": False,
        "softcam_autoheal": False,
        "softcam_autoheal_seconds": 90,
        "deep_standby_allowed": False,
        "softcam_restart_allowed": False,
        "epg_import_allowed": False,
        "uninstall_allowed": False,
    }


def test_info_settings_echo_the_box_only_permission(make_bridge, factory, settings):
    """`deep_standby_allowed` is in `info.settings` whichever way it is set.

    It is there so a consumer can tell „the box refused this" from „the box
    cannot do this" and hide the two buttons rather than offering ones that
    always fail — which is only possible if the key is present either way.
    """
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.deep_standby_allowed.value = True
    make_bridge().start()
    factory.client.fire_connect()

    assert factory.client.last(INFO).json()["settings"]["deep_standby_allowed"] is True


def test_the_writable_subset_is_smaller_than_what_info_publishes(connected_bridge):
    """Presence in `info.settings` is not permission to write it back.

    `remote_settings` is what `cmd/config` replaces; `published_settings` is what
    a consumer reads. Letting the two converge would make the contract's
    read-only members writable by accident.
    """
    published = connected_bridge.published_settings()
    assert set(connected_bridge.remote_settings()) == set(
        settings_module.REMOTE_SETTING_NAMES
    )
    assert set(published) == set(settings_module.REMOTE_SETTING_NAMES) | set(
        settings_module.READ_ONLY_SETTING_NAMES
    )
    assert not set(settings_module.REMOTE_SETTING_NAMES) & set(
        settings_module.READ_ONLY_SETTING_NAMES
    )


def test_info_is_retained(connected_bridge, factory):
    assert factory.client.last(INFO).retain is True


def test_capabilities_are_empty_until_a_feature_area_is_bound(connected_bridge, factory):
    """The contract's vocabulary is the feature areas; this build binds none."""
    assert factory.client.last(INFO).json()["capabilities"] == []


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

    assert factory.client.last(INFO).json()["capabilities"] == ["power"]
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
    assert payload["capabilities"] == []
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


def test_a_field_the_payload_stamps_itself_is_not_a_change(connected_bridge, factory):
    """A wall-clock stamp must not decide whether the box changed.

    Without this, a topic that says when it was built republishes itself every
    time it is built — which is exactly the recorder churn publish-on-change
    exists to prevent.
    """
    topic = "enigma2/" + NODE + "/demo"
    connected_bridge.publish_state("demo", {"generated": 1, "value": 7},
                                   volatile=("generated",))
    factory.client.clear()

    connected_bridge.publish_state("demo", {"generated": 2, "value": 7},
                                   volatile=("generated",))
    assert factory.client.all_for(topic) == []

    connected_bridge.publish_state("demo", {"generated": 3, "value": 8},
                                   volatile=("generated",))
    assert factory.client.last(topic).json() == {"generated": 3, "value": 8}


def test_the_snapshot_records_what_the_change_test_will_compare(make_bridge, factory, settings):
    """The connect snapshot and a later publish must measure the topic the same way.

    `on_connect` sends everything whether or not it moved, because a broker that
    lost its retained store has to be able to converge. What the snapshot
    records as sent is then what the next publish is judged against, so a
    snapshot that forgot which fields are volatile would make the first rebuild
    after every connect look like a change — the same defect once per
    connection rather than once per refresh.

    This goes through `publish_snapshot` itself rather than calling the encoder
    underneath it, because the wiring between the two is the thing that can be
    wrong.
    """
    class Grid(Publisher):
        name = "hdd"
        volatile = ("generated",)

        def snapshot(self):
            return {"demo": {"generated": 1, "value": 7}}

    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge()
    bridge.register_publisher(Grid())
    bridge.start()
    factory.client.fire_connect()

    topic = "enigma2/" + NODE + "/demo"
    assert factory.client.last(topic).json() == {"generated": 1, "value": 7}
    factory.client.clear()

    bridge.publisher("hdd").publish("demo", {"generated": 2, "value": 7})
    assert factory.client.all_for(topic) == []


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


def test_shutdown_says_goodbye_and_disconnects(connected_bridge, factory, wait_until):
    client = factory.client
    client.clear()
    connected_bridge.stop()

    entry = client.last(AVAILABILITY)
    assert entry.text == "offline"
    assert entry.retain is True
    assert client.disconnect_calls == 1
    # The network loop is stopped off the main thread, so it ends shortly after.
    assert wait_until(lambda: client.loop_started is False)


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

    assert factory.client.last(INFO).json()["capabilities"] == []
    assert "does not provide the tuner hooks" in plugin_log()


def test_a_feature_switched_off_is_not_blamed_on_the_image(make_bridge, settings, receiver,
                                                           factory, plugin_log):
    """🔴 „this image does not provide the oscam hooks" for a setting nobody
    turned on reads as a broken receiver. It is a choice, and it is logged as one."""
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.publish_keys.value = False
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()

    written = plugin_log()
    # Each of them says it once, in its own words, and none of them blames the
    # receiver for a switch somebody chose. The phrases are the distinctive
    # halves: „cam_telemetry is off" is also a substring of the oscam line.
    assert written.count("publish_keys is off") == 1
    assert written.count("conditional-access status is not published") == 1
    assert written.count("OSCam health is not published") == 1
    for name in ("keys", "cam", "oscam"):
        assert "does not provide the " + name + " hooks" not in written
    assert "keys" not in bridge.capabilities()


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


def _retained_on_the_broker(factory, topic):
    """What a broker holds for `topic` once every session here has ended cleanly.

    Sessions in the order they were opened, publishes in the order each made
    them. A clean disconnect discards the will, so the will never counts.
    """
    held = None
    for client in factory.clients:
        for entry in client.published:
            if entry.topic == topic and entry.retain:
                held = entry.text
    return held


def _record_disconnect_order(client):
    """Note how many publishes the client had made when it was told to disconnect."""
    seen = []
    disconnect = client.disconnect

    def recorded():
        seen.append(len(client.published))
        disconnect()

    client.disconnect = recorded
    return seen


def _offline_from(client):
    return [entry for entry in client.published if entry.text == "offline"]


@pytest.mark.parametrize("name, changed", [
    ("host", "10.0.0.9"),
    ("port", 8883),
    ("username", "someone"),
    ("password", "something else"),
    ("tls", True),
    ("ca_file", "/etc/ssl/certs/ca.pem"),
])
def test_a_reload_whose_reconnect_fails_leaves_offline_retained(connected_bridge, factory,
                                                                 settings, name, changed):
    """A save that changes the connection: the old session says `offline` itself.

    The clean disconnect drops the will, and the new session may never connect —
    a wrong address, a password that no longer matches.
    """
    old = factory.client
    assert _retained_on_the_broker(factory, AVAILABILITY) == "online"
    seen = _record_disconnect_order(old)

    getattr(settings, name).value = changed
    connected_bridge.reload()

    assert factory.client is not old
    assert factory.client.published == []
    assert _retained_on_the_broker(factory, AVAILABILITY) == "offline"
    offline = [i for i, e in enumerate(old.published) if e.topic == AVAILABILITY
               and e.text == "offline" and e.retain]
    # Before the DISCONNECT, which is what a broker would still deliver.
    assert offline and offline[-1] < seen[0]
    # A state topic, so QoS 0 — the will alone is asked for at QoS 1.
    assert [e.qos for e in _offline_from(old)] == [0]


@pytest.mark.parametrize("name, changed", [
    ("log_level", "debug"),
    ("screenshot_delay", 9),
    ("publish_keys", False),
    ("friendly_name", "Kitchen receiver"),
])
def test_a_reload_that_leaves_the_connection_alone_says_nothing(connected_bridge, factory,
                                                                settings, name, changed):
    """The setup screen reloads on every save: only a connection change may blink `offline`."""
    old = factory.client
    getattr(settings, name).value = changed
    connected_bridge.reload()

    assert _offline_from(old) == []
    assert _retained_on_the_broker(factory, AVAILABILITY) == "online"
    factory.client.fire_connect()
    assert _retained_on_the_broker(factory, AVAILABILITY) == "online"


@pytest.mark.parametrize("name, changed, renamed", [
    ("node_id", "vuuno4kse_beef01", "enigma2/vuuno4kse_beef01/availability"),
    ("base_topic", "stb", "stb/" + NODE + "/availability"),
])
@pytest.mark.parametrize("with_host", [False, True], ids=["rename", "rename-and-new-broker"])
def test_a_rename_publishes_no_offline_for_either_name(connected_bridge, factory, settings,
                                                       name, changed, renamed, with_host):
    """The old name is retracted; the new one belongs to the new session alone.

    An `offline` from the old session under the new name could land after the
    new session's `online` — two client ids, which the broker does not order —
    and stay retained under a name that is live. One under the old name would
    land after its retraction and stay behind for a name nobody uses. That holds
    when the same save also changes the broker.
    """
    old = factory.client
    getattr(settings, name).value = changed
    if with_host:
        settings.host.value = "10.0.0.9"
    connected_bridge.reload()

    assert _offline_from(old) == []
    assert old.last(AVAILABILITY).text == ""
    assert old.all_for(renamed) == []
    factory.client.fire_connect()
    assert _retained_on_the_broker(factory, renamed) == "online"


def test_a_reload_after_the_connection_dropped_publishes_nothing(connected_bridge, factory,
                                                                 settings):
    """With the session already gone, the broker has published the will; there is no one to tell."""
    old = factory.client
    old.fire_disconnect(reason_code=7)
    assert connected_bridge.connected is False
    before = len(old.published)

    settings.host.value = "10.0.0.9"
    connected_bridge.reload()

    assert old.published[before:] == []


def test_a_reload_that_switches_the_plugin_off_leaves_offline_retained(connected_bridge,
                                                                       factory, settings):
    settings.enabled.value = False
    connected_bridge.reload()

    assert connected_bridge.running is False
    assert _retained_on_the_broker(factory, AVAILABILITY) == "offline"


def test_a_reload_that_reconnects_ends_online(connected_bridge, factory, settings):
    settings.host.value = "10.0.0.9"
    connected_bridge.reload()
    factory.client.fire_connect()

    assert _retained_on_the_broker(factory, AVAILABILITY) == "online"


def test_the_default_state_path_sits_beside_enigma2s_settings(monkeypatch):
    monkeypatch.setattr(discovery.os.path, "isdir", lambda path: path == "/etc/enigma2")
    monkeypatch.setattr(discovery.os, "access", lambda path, mode: True)
    assert discovery.default_state_path() == "/etc/enigma2/mqttbridge-state.json"


def test_the_state_path_falls_back_when_etc_is_read_only(monkeypatch):
    monkeypatch.setattr(discovery.os.path, "isdir", lambda path: False)
    assert discovery.default_state_path("/opt/plugin").endswith("/opt/plugin/mqttbridge-state.json")


def test_a_rename_while_disconnected_is_retracted_on_the_next_connect(
    make_bridge, factory, settings, state_path
):
    """The rename that matters happens while nothing is connected.

    `reload` only sees the case where a session was open at the time. A node id
    edited on a box with no network — or between two runs of the plugin — leaves
    the old tree retained on the broker with nobody to take it back, and the
    state file is the only record it ever existed.
    """
    settings.host.value = "10.0.0.5"
    settings.node_id.value = "box_a"
    first = make_bridge()
    first.start()
    factory.client.fire_connect()
    assert "enigma2/box_a/availability" in factory.client.topics()

    # Down, and only then renamed: no session is open to retract anything.
    factory.client.fire_disconnect()
    first.stop()

    settings.node_id.value = "box_b"
    make_bridge().start()
    fresh = factory.client
    fresh.fire_connect()

    retracted = [e.topic for e in fresh.published if e.text == "" and e.retain]
    assert "enigma2/box_a/availability" in retracted
    assert "enigma2/box_a/info" in retracted
    assert "enigma2mqtt/discovery/box_a/config" in retracted

    # And before the new node says a word, so nothing is retracted after the
    # payload that replaced it.
    topics = fresh.topics()
    assert topics.index("enigma2/box_a/availability") < topics.index("enigma2/box_b/availability")


def test_the_retracted_topics_are_forgotten(make_bridge, factory, settings):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = "box_a"
    bridge = make_bridge()
    bridge.start()
    factory.client.fire_connect()
    factory.client.fire_disconnect()
    bridge.stop()

    settings.node_id.value = "box_b"
    second = make_bridge()
    second.start()
    factory.client.fire_connect()
    factory.client.clear()
    factory.client.fire_connect()

    # Nothing of box_a is left to retract a second time.
    assert not [e for e in factory.client.published if "box_a" in e.topic]


def test_a_second_start_leaves_the_open_session_alone(make_bridge, factory, settings, plugin_log):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge()
    bridge.start()
    bridge.start()

    assert len(factory.clients) == 1
    assert [client for client in factory.clients if client.loop_started] == [factory.client]
    assert factory.client.disconnect_calls == 0
    assert "already running" in plugin_log()


def test_a_bridge_that_went_idle_can_still_be_started(make_bridge, factory, settings):
    """The guard is about a live session, not about having tried once."""
    bridge = make_bridge()
    bridge.start()
    assert factory.clients == []

    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge.start()
    assert len(factory.clients) == 1


def test_an_image_with_no_thread_bridge_stays_idle(make_bridge, factory, monkeypatch, settings,
                                                   plugin_log):
    """Running paho's callbacks on its own thread is not an acceptable fallback."""
    import sys

    from MQTTBridge import mqttclient

    monkeypatch.setattr(mqttclient, "_twisted_reactor", lambda: None)
    monkeypatch.delitem(sys.modules, "enigma")

    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(dispatcher=None)
    bridge.start()

    assert bridge.running is False
    assert bridge.client is None
    assert "no way to reach the main loop" in bridge.idle_reason
    assert factory.clients == []
    assert "unsupported image: no main-loop bridge" in plugin_log()


def test_a_preposterous_command_name_is_capped_in_last_error(connected_bridge, factory):
    """The name comes off the topic, so its length is somebody else's choice."""
    connected_bridge.on_message("enigma2/" + NODE + "/cmd/" + "z" * 4000, b"", False)

    payload = factory.client.last("enigma2/" + NODE + "/last_error").json()
    assert len(payload["cmd"]) == 64
    assert payload["cmd"].startswith("zzz")
    assert payload["error"] == "unknown command"


def test_a_short_command_name_is_left_alone(connected_bridge, factory):
    connected_bridge.on_message("enigma2/" + NODE + "/cmd/nonsense", b"", False)
    assert factory.client.last("enigma2/" + NODE + "/last_error").json()["cmd"] == "nonsense"


def test_a_bridge_built_with_no_arguments_uses_the_real_settings():
    """Constructing one must not need a session, a broker or a file."""
    from MQTTBridge import config as settings_module

    bridge = Bridge()
    assert bridge.settings is settings_module.settings
    assert bridge.base_topic == "enigma2"


def test_the_event_loop_monitor_follows_the_active_bridge_lifecycle(
    make_bridge, settings
):
    class Monitor:
        def __init__(self):
            self.starts = 0
            self.stops = 0

        def start(self):
            self.starts += 1
            return True

        def stop(self):
            self.stops += 1

    monitor = Monitor()
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(loop_monitor=monitor)

    bridge.start()
    bridge.reload()
    bridge.stop()

    assert monitor.starts == 2
    assert monitor.stops == 2


def test_an_idle_bridge_stops_its_event_loop_monitor(make_bridge, settings):
    class Monitor:
        def __init__(self):
            self.started = False
            self.stopped = False

        def start(self):
            self.started = True
            return True

        def stop(self):
            self.stopped = True

    monitor = Monitor()
    settings.host.value = ""
    bridge = make_bridge(loop_monitor=monitor)
    bridge.start()

    assert monitor.started is False
    assert monitor.stopped is True


def test_startup_lifecycle_log_omits_node_and_broker_identity(
    make_bridge, settings, plugin_log
):
    settings.host.value = "private-broker.example"
    settings.node_id.value = "private-node"
    bridge = make_bridge()
    bridge.start()

    text = plugin_log()
    assert "starting: ha_mode=" in text
    assert "private-broker.example" not in text
    assert "private-node" not in text
