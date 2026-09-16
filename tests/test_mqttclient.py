"""The transport, and the vendored MQTT client it wraps.

Three things are worth a test here rather than a comment. The vendored paho
imports itself as the top-level name `paho`, which only works because the
directory holding it goes on `sys.path` first — if that ever stops being true
the plugin fails on a receiver and nowhere else, and if the *wrong* directory
goes there the plugin's own modules shadow the standard library for every other
plugin in the process. Every paho callback must hand its work to the main thread
rather than touch enigma2 from the network thread. And nothing in the shutdown
path may block the thread that draws the television.
"""

import importlib.util
import sys
import threading
import time

import pytest

from MQTTBridge import mqttclient


def test_the_vendored_client_imports_as_top_level_paho():
    module = mqttclient.import_paho()
    assert module.CallbackAPIVersion.VERSION2
    assert "paho" in sys.modules
    assert sys.modules["paho"].__file__.replace("\\", "/").endswith(
        "MQTTBridge/_vendor/paho/__init__.py"
    )


def test_only_the_vendor_directory_reaches_sys_path():
    """sys.path[0] is searched before the standard library, for every plugin.

    The plugin directory holds `config.py`, `log.py`, `setup.py`, `keys.py` and
    `version.py`. Putting it there would make `import config` anywhere in the
    enigma2 process resolve to ours.
    """
    mqttclient.import_paho()
    assert mqttclient.VENDOR_DIRECTORY in sys.path
    assert mqttclient.PLUGIN_DIRECTORY not in sys.path


def test_the_vendor_directory_is_only_added_once():
    mqttclient.ensure_paho_on_path()
    mqttclient.ensure_paho_on_path()
    assert sys.path.count(mqttclient.VENDOR_DIRECTORY) == 1


def test_the_plugins_own_modules_do_not_shadow_a_top_level_import():
    """Measured against the old insertion, all seven of these resolved to the
    plugin's own file for the whole enigma2 process."""
    mqttclient.ensure_paho_on_path()
    for name in ("config", "setup", "log", "keys", "version", "commands", "discovery"):
        sys.modules.pop(name, None)
        spec = importlib.util.find_spec(name)
        origin = "" if spec is None else (spec.origin or "")
        assert "MQTTBridge" not in origin, name + " resolves to " + origin


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


def test_without_any_bridge_there_is_no_dispatcher(monkeypatch, plugin_log):
    """A callback on paho's network thread is not a fallback, it is the bug."""
    monkeypatch.setattr(mqttclient, "_twisted_reactor", lambda: None)
    monkeypatch.delitem(sys.modules, "enigma")

    assert mqttclient.make_dispatcher() is None
    assert "unsupported image: no main-loop bridge" in plugin_log()


def test_a_client_without_a_dispatcher_never_connects(monkeypatch, factory, plugin_log):
    monkeypatch.setattr(mqttclient, "_twisted_reactor", lambda: None)
    monkeypatch.delitem(sys.modules, "enigma")

    client = mqttclient.MqttClient(
        mqttclient.BrokerSettings(host="10.0.0.5"), client_factory=factory
    )
    assert client.usable is False
    assert client.start() is None
    assert factory.clients == []


# ----------------------------------------------------------------- shutdown --


class SlowToStopClient:
    """paho's `loop_stop()` joins the network thread — and that thread can be
    inside a five-second `socket.create_connection` to an address that answers
    nothing. This is that client, without the socket."""

    def __init__(self, client_id="", delay=1.0):
        self.client_id = client_id
        self.delay = delay
        self.stopped = threading.Event()
        self.disconnect_calls = 0
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    def will_set(self, *args, **kwargs):
        pass

    def reconnect_delay_set(self, **kwargs):
        pass

    def max_queued_messages_set(self, size):
        return self

    def max_inflight_messages_set(self, size):
        pass

    def connect_async(self, *args, **kwargs):
        pass

    def loop_start(self):
        pass

    def disconnect(self):
        self.disconnect_calls += 1

    def loop_stop(self):
        time.sleep(self.delay)
        self.stopped.set()


def test_stopping_does_not_block_the_main_thread():
    """The setup screen's Save and the shutdown hook both call this from the
    thread that draws the television."""
    made = []

    def factory(client_id):
        client = SlowToStopClient(client_id, delay=1.0)
        made.append(client)
        return client

    client = mqttclient.MqttClient(
        mqttclient.BrokerSettings(host="10.0.0.5"),
        client_factory=factory,
        dispatcher=lambda fn, *a, **k: fn(*a, **k),
    )
    client.start()

    began = time.monotonic()
    client.stop()
    elapsed = time.monotonic() - began

    assert elapsed < 0.05, f"stop() blocked the main thread for {elapsed:.3f}s"
    assert made[0].disconnect_calls == 1
    # It does happen, just not here.
    assert made[0].stopped.wait(5) is True


def test_a_restart_never_reuses_the_client_object(factory):
    client = mqttclient.MqttClient(
        mqttclient.BrokerSettings(host="10.0.0.5"),
        client_factory=factory,
        dispatcher=lambda fn, *a, **k: fn(*a, **k),
    )
    client.start()
    client.stop()
    client.start()

    assert len(factory.clients) == 2
    assert factory.clients[0] is not factory.clients[1]


def test_a_second_start_closes_the_first_session(factory, plugin_log):
    client = mqttclient.MqttClient(
        mqttclient.BrokerSettings(host="10.0.0.5"),
        client_factory=factory,
        dispatcher=lambda fn, *a, **k: fn(*a, **k),
    )
    client.start()
    client.start()

    assert factory.clients[0].disconnect_calls == 1
    assert "already open" in plugin_log()


def test_a_second_start_keeps_the_thread_bridge_it_is_about_to_need(monkeypatch, factory):
    """Closing the old session must not take the dispatcher down with it."""
    monkeypatch.setattr(mqttclient, "_twisted_reactor", lambda: None)
    client = mqttclient.MqttClient(
        mqttclient.BrokerSettings(host="10.0.0.5"), client_factory=factory
    )
    client.start()
    dispatcher = client._dispatch
    client.start()

    assert client._dispatch is dispatcher
    seen = []
    factory.client.fire_message("enigma2/x/cmd/reset", b"PRESS")
    dispatcher(seen.append, "still alive")
    assert seen == ["still alive"]


# ------------------------------------------------------------ bounded queues --


def test_the_outgoing_queues_are_bounded(factory):
    """A broker that stops reading must cost a dropped publish, not memory."""
    client = mqttclient.MqttClient(
        mqttclient.BrokerSettings(host="10.0.0.5"), client_factory=factory,
        dispatcher=lambda fn, *a, **k: fn(*a, **k),
    )
    client.start()

    assert factory.client.max_queued == mqttclient.MAX_QUEUED_MESSAGES
    assert factory.client.max_inflight == mqttclient.MAX_INFLIGHT_MESSAGES


def test_a_rejected_publish_is_logged(factory, plugin_log):
    client = mqttclient.MqttClient(
        mqttclient.BrokerSettings(host="10.0.0.5"), client_factory=factory,
        dispatcher=lambda fn, *a, **k: fn(*a, **k),
    )
    client.start()
    factory.client.publish_rc = mqttclient.MQTT_ERR_QUEUE_SIZE
    client.publish("enigma2/x/info", "{}")

    assert "the outgoing queue is full" in plugin_log()


def test_a_client_missing_the_queue_setters_still_starts():
    """Not every stand-in for paho has them, and none of them are load-bearing."""

    class Bare:
        def __init__(self, client_id=""):
            self.on_connect = self.on_message = self.on_disconnect = None

        def reconnect_delay_set(self, **kwargs):
            pass

        def connect_async(self, *args, **kwargs):
            pass

        def loop_start(self):
            pass

    client = mqttclient.MqttClient(
        mqttclient.BrokerSettings(host="10.0.0.5"),
        client_factory=lambda client_id: Bare(client_id),
        dispatcher=lambda fn, *a, **k: fn(*a, **k),
    )
    assert client.start() is not None


# -------------------------------------------------------------- message pump --


def test_the_message_pump_dispatcher_detaches_on_stop(monkeypatch):
    monkeypatch.setattr(mqttclient, "_twisted_reactor", lambda: None)
    dispatcher = mqttclient.make_dispatcher()
    pump = dispatcher._pump
    assert len(pump.recv_msg.get()) == 1

    dispatcher.stop()
    assert pump.recv_msg.get() == []

    # And it is inert afterwards rather than raising on a late callback.
    dispatcher(lambda: None)


def test_reload_does_not_accumulate_pump_listeners(monkeypatch, make_bridge, factory, settings):
    """One `_drain` per reload would hold every dead session's queue alive."""
    import enigma

    shared = enigma.ePythonMessagePump()
    monkeypatch.setattr(mqttclient, "_twisted_reactor", lambda: None)
    monkeypatch.setattr(enigma, "ePythonMessagePump", lambda: shared)

    settings.host.value = "10.0.0.5"
    settings.node_id.value = "vuuno4kse_005301"
    bridge = make_bridge(dispatcher=None)
    bridge.start()
    assert len(shared.recv_msg.get()) == 1

    bridge.reload()
    bridge.reload()
    assert len(shared.recv_msg.get()) == 1

    bridge.stop()
    assert shared.recv_msg.get() == []
    assert len(factory.clients) == 3
