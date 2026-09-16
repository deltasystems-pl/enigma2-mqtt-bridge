"""The MQTT session, and the one thread it is allowed to have.

The plugin runs inside the process that renders the television. paho owns a
network thread; every one of its callbacks does exactly one thing, which is to
hand the event to enigma2's main loop through `reactor.callFromThread`. Nothing
in this file touches enigma2 state on the network thread, and every handler that
runs on the main thread is wrapped so that a bad payload cannot raise into the
user interface.

The vendored paho package imports itself as the top-level name `paho`, so the
plugin directory goes on `sys.path` before it is imported. That is the whole
trick: no install step, no pip, no dependency on an image feed.
"""

import os
import sys

from .log import get_logger, redact

LOG = get_logger("mqtt")

PLUGIN_DIRECTORY = os.path.dirname(os.path.abspath(__file__))

KEEPALIVE = 30
RECONNECT_MIN = 1
RECONNECT_MAX = 60

# How much of a payload a debug line is allowed to carry.
LOG_PAYLOAD_LIMIT = 500


def ensure_paho_on_path():
    """Make the vendored copy importable as `paho`, exactly once."""
    if PLUGIN_DIRECTORY not in sys.path:
        sys.path.insert(0, PLUGIN_DIRECTORY)
    return PLUGIN_DIRECTORY


def import_paho():
    ensure_paho_on_path()
    from paho.mqtt import client as paho_client

    return paho_client


def default_client_factory(client_id):
    paho_client = import_paho()
    return paho_client.Client(
        callback_api_version=paho_client.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True,
        protocol=paho_client.MQTTv311,
    )


# ------------------------------------------------------------ thread bridging --


def _twisted_reactor():
    try:
        from twisted.internet import reactor

        if hasattr(reactor, "callFromThread"):
            return reactor
    except Exception:
        pass
    return None


class MessagePumpDispatcher:
    """The fallback thread bridge for an image without Twisted in reach.

    `ePythonMessagePump` is enigma2's own thread-to-main-loop signal; the work
    itself travels in a queue because the pump only carries an integer.
    """

    def __init__(self):
        import queue

        from enigma import ePythonMessagePump

        self._queue = queue.Queue()
        self._pump = ePythonMessagePump()
        self._connection = None
        signal = getattr(self._pump, "recv_msg", None)
        if signal is None:
            raise AttributeError("ePythonMessagePump has no recv_msg")
        if hasattr(signal, "get"):
            signal.get().append(self._drain)
        else:
            self._connection = signal.connect(self._drain)

    def __call__(self, function, *args, **kwargs):
        self._queue.put((function, args, kwargs))
        self._pump.send(0)

    def _drain(self, *_ignored):
        while True:
            try:
                function, args, kwargs = self._queue.get_nowait()
            except Exception:
                return
            try:
                function(*args, **kwargs)
            except Exception:
                LOG.exception("a queued handler raised")


def _run_here(function, *args, **kwargs):
    function(*args, **kwargs)


def make_dispatcher():
    """However this image lets a background thread reach the main loop."""
    reactor = _twisted_reactor()
    if reactor is not None:
        return reactor.callFromThread
    try:
        return MessagePumpDispatcher()
    except Exception:
        LOG.warning("no thread bridge available; MQTT callbacks will run on the network thread")
        return _run_here


# -------------------------------------------------------------------- client --


class BrokerSettings:
    """Everything the session needs, resolved once, with no config lookups later."""

    def __init__(self, host, port=1883, tls=False, ca_file="", username="", password="",
                 client_id="", keepalive=KEEPALIVE):
        self.host = host
        self.port = int(port)
        self.tls = bool(tls)
        self.ca_file = ca_file or ""
        self.username = username or ""
        self.password = password or ""
        self.client_id = client_id or ""
        self.keepalive = int(keepalive)


def reason_is_success(reason_code):
    """paho 2 hands back a ReasonCode for MQTTv5 and an int-like one for v3."""
    if reason_code is None:
        return True
    is_failure = getattr(reason_code, "is_failure", None)
    if is_failure is not None:
        return not bool(is_failure)
    try:
        return int(reason_code) == 0
    except (TypeError, ValueError):
        return True


class MqttClient:
    """A thin wrapper: connection lifecycle, the will, and the thread bridge."""

    def __init__(self, settings, on_connect=None, on_message=None, on_disconnect=None,
                 dispatcher=None, client_factory=None):
        self.settings = settings
        self.connected = False
        self._on_connect = on_connect
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._dispatch = dispatcher if dispatcher is not None else make_dispatcher()
        self._factory = client_factory if client_factory is not None else default_client_factory
        self._client = None
        self._will = None

    # --------------------------------------------------------------- lifecycle --

    def set_will(self, topic, payload, qos=1, retain=True):
        """Registered before the connection, or the broker never learns about it."""
        self._will = (topic, payload, qos, retain)

    def start(self):
        client = self._factory(self.settings.client_id)
        client.on_connect = self._paho_on_connect
        client.on_message = self._paho_on_message
        client.on_disconnect = self._paho_on_disconnect

        if self.settings.username:
            client.username_pw_set(self.settings.username, self.settings.password or None)

        if self.settings.tls:
            if self.settings.ca_file:
                client.tls_set(ca_certs=self.settings.ca_file)
            else:
                client.tls_set()

        if self._will is not None:
            topic, payload, qos, retain = self._will
            client.will_set(topic, payload, qos=qos, retain=retain)

        client.reconnect_delay_set(min_delay=RECONNECT_MIN, max_delay=RECONNECT_MAX)

        self._client = client
        client.connect_async(
            self.settings.host, self.settings.port, keepalive=self.settings.keepalive
        )
        client.loop_start()
        LOG.info(
            "connecting to %s:%s as %s (tls=%s)",
            self.settings.host,
            self.settings.port,
            self.settings.client_id,
            "on" if self.settings.tls else "off",
        )
        return client

    def stop(self):
        client = self._client
        self.connected = False
        self._client = None
        if client is None:
            return
        try:
            client.disconnect()
        except Exception:
            LOG.exception("disconnect failed")
        try:
            client.loop_stop()
        except Exception:
            LOG.exception("stopping the network loop failed")
        LOG.info("disconnected")

    # ------------------------------------------------------------------- publish --

    def publish(self, topic, payload=None, qos=0, retain=False):
        client = self._client
        if client is None:
            LOG.debug("no session; dropping a publish to %s", topic)
            return None
        if payload is None:
            data = b""
        elif isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
        else:
            data = str(payload).encode("utf-8")
        try:
            info = client.publish(topic, data, qos=qos, retain=retain)
        except Exception:
            LOG.exception("publishing to %s failed", topic)
            return None
        if LOG.isEnabledFor(10):  # DEBUG
            LOG.debug(
                "publish %s qos=%d retain=%s %s",
                topic,
                qos,
                retain,
                _describe(data),
            )
        return info

    def subscribe(self, topic, qos=1):
        client = self._client
        if client is None:
            LOG.debug("no session; not subscribing to %s", topic)
            return None
        try:
            result = client.subscribe(topic, qos=qos)
        except Exception:
            LOG.exception("subscribing to %s failed", topic)
            return None
        LOG.info("subscribed to %s (qos %d)", topic, qos)
        return result

    @staticmethod
    def wait_for(info, timeout=1.0):
        """Give a publish a bounded chance to reach the socket. Shutdown only."""
        if info is None:
            return False
        waiter = getattr(info, "wait_for_publish", None)
        if waiter is None:
            return False
        try:
            waiter(timeout)
        except Exception:
            return False
        return True

    # ----------------------------------------------------------- paho callbacks --
    # These four run on paho's network thread. They hand over and return.

    def _paho_on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._dispatch(self._handle_connect, reason_code)

    def _paho_on_message(self, client, userdata, message):
        try:
            topic = message.topic
            payload = message.payload
            retain = bool(getattr(message, "retain", False))
        except Exception:
            LOG.exception("an incoming message could not be read")
            return
        self._dispatch(self._handle_message, topic, payload, retain)

    def _paho_on_disconnect(self, client, userdata, flags=None, reason_code=None, properties=None):
        self._dispatch(self._handle_disconnect, reason_code)

    # -------------------------------------------------------- main-thread halves --

    def _handle_connect(self, reason_code):
        try:
            if not reason_is_success(reason_code):
                self.connected = False
                LOG.error("the broker refused the connection: %s", reason_code)
                return
            self.connected = True
            LOG.info("connected to %s:%s", self.settings.host, self.settings.port)
            if self._on_connect is not None:
                self._on_connect()
        except Exception:
            LOG.exception("the connect handler raised; the receiver is unaffected")

    def _handle_message(self, topic, payload, retain):
        try:
            if self._on_message is not None:
                self._on_message(topic, payload, retain)
        except Exception:
            LOG.exception("the handler for %s raised; the receiver is unaffected", topic)

    def _handle_disconnect(self, reason_code):
        try:
            self.connected = False
            LOG.warning("disconnected from the broker: %s", reason_code)
            if self._on_disconnect is not None:
                self._on_disconnect(reason_code)
        except Exception:
            LOG.exception("the disconnect handler raised; the receiver is unaffected")


def _describe(data):
    """A payload as a debug line may carry it: decoded, redacted, truncated."""
    try:
        text = data.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return f"<{len(data)} bytes>" if data else "<empty>"
    text = redact(text)
    if len(text) > LOG_PAYLOAD_LIMIT:
        return text[:LOG_PAYLOAD_LIMIT] + "…"
    return text
