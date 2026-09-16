"""The transport, and the vendored MQTT client it wraps.

Two things are worth a test here rather than a comment. The vendored paho
imports itself as the top-level name `paho`, which only works because the plugin
directory goes on `sys.path` first — if that ever stops being true the plugin
fails on a receiver and nowhere else. And every paho callback must hand its work
to the main thread rather than touch enigma2 from the network thread.
"""

import sys

import pytest

from MQTTBridge import mqttclient


def test_the_vendored_client_imports_as_top_level_paho():
    module = mqttclient.import_paho()
    assert module.CallbackAPIVersion.VERSION2
    assert "paho" in sys.modules
    assert sys.modules["paho"].__file__.replace("\\", "/").endswith("MQTTBridge/paho/__init__.py")


def test_the_plugin_directory_is_only_added_once():
    mqttclient.ensure_paho_on_path()
    mqttclient.ensure_paho_on_path()
    assert sys.path.count(mqttclient.PLUGIN_DIRECTORY) == 1


def test_the_default_factory_builds_a_version_2_client():
    client = mqttclient.default_client_factory("mqttbridge-test")
    assert client._client_id == b"mqttbridge-test"
    client.loop_stop()


def test_every_callback_goes_through_the_dispatcher(factory):
    """Nothing may touch enigma2 from paho's network thread."""
    handed = []

    def dispatcher(function, *args, **kwargs):
        handed.append(function.__name__)
        return function(*args, **kwargs)

    client = mqttclient.MqttClient(
        mqttclient.BrokerSettings(host="10.0.0.5"), dispatcher=dispatcher, client_factory=factory
    )
    client.start()
    factory.client.fire_connect()
    factory.client.fire_message("enigma2/x/cmd/reset", b"PRESS")
    factory.client.fire_disconnect()

    assert handed == ["_handle_connect", "_handle_message", "_handle_disconnect"]


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (None, True),
        (0, True),
        (5, False),
    ],
)
def test_reason_codes_of_both_shapes(reason, expected):
    assert mqttclient.reason_is_success(reason) is expected


def test_a_reason_code_object_is_read_by_its_failure_flag():
    class Reason:
        is_failure = True

    assert mqttclient.reason_is_success(Reason()) is False


def test_an_unreadable_reason_code_is_not_treated_as_a_failure():
    assert mqttclient.reason_is_success(object()) is True


def test_publishing_without_a_session_is_a_no_op():
    client = mqttclient.MqttClient(mqttclient.BrokerSettings(host="10.0.0.5"), dispatcher=None)
    assert client.publish("enigma2/x/info", "{}") is None
    assert client.subscribe("enigma2/x/cmd/#") is None


def test_payloads_of_every_shape_reach_the_wire_as_bytes(factory):
    client = mqttclient.MqttClient(
        mqttclient.BrokerSettings(host="10.0.0.5"), client_factory=factory,
        dispatcher=lambda fn, *a, **k: fn(*a, **k),
    )
    client.start()
    client.publish("a", "text")
    client.publish("b", b"bytes")
    client.publish("c", None)

    assert [entry.payload for entry in factory.client.published] == [b"text", b"bytes", b""]


def test_a_handler_that_raises_never_reaches_enigma2(factory, plugin_log):
    def explode(topic, payload, retain):
        raise RuntimeError("bad payload")

    client = mqttclient.MqttClient(
        mqttclient.BrokerSettings(host="10.0.0.5"),
        on_message=explode,
        client_factory=factory,
        dispatcher=lambda fn, *a, **k: fn(*a, **k),
    )
    client.start()
    factory.client.fire_message("enigma2/x/cmd/reset", b"PRESS")

    assert "the receiver is unaffected" in plugin_log()


def test_the_dispatcher_prefers_twisted():
    """enigma2 runs on Twisted; `callFromThread` is the sanctioned bridge."""
    from twisted.internet import reactor

    assert mqttclient.make_dispatcher() is reactor.callFromThread


def test_the_message_pump_is_the_fallback(monkeypatch):
    monkeypatch.setattr(mqttclient, "_twisted_reactor", lambda: None)
    dispatcher = mqttclient.make_dispatcher()
    assert isinstance(dispatcher, mqttclient.MessagePumpDispatcher)

    seen = []
    dispatcher(seen.append, "work")
    assert seen == ["work"]


def test_without_any_bridge_the_callback_still_runs(monkeypatch, plugin_log):
    monkeypatch.setattr(mqttclient, "_twisted_reactor", lambda: None)
    monkeypatch.delitem(sys.modules, "enigma")
    dispatcher = mqttclient.make_dispatcher()

    seen = []
    dispatcher(seen.append, "work")
    assert seen == ["work"]
    assert "no thread bridge available" in plugin_log()
