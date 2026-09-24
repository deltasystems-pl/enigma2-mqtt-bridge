"""The MQTT session, and the one thread it is allowed to have.

The plugin runs inside the process that renders the television. paho owns a
network thread; every one of its callbacks does exactly one thing, which is to
hand the event to enigma2's main loop through `reactor.callFromThread`. Nothing
in this file touches enigma2 state on the network thread, and every handler that
runs on the main thread is wrapped so that a bad payload cannot raise into the
user interface.

The vendored paho package imports itself as the top-level name `paho`, so the
directory that contains it goes on `sys.path` before it is imported. That is the
whole trick: no install step, no pip, no dependency on an image feed.
"""

import os
import sys
import threading
import time

from .log import get_logger, redact

LOG = get_logger("mqtt")

PLUGIN_DIRECTORY = os.path.dirname(os.path.abspath(__file__))
# The vendored packages live one directory down, and it is that directory - not
# the plugin's own - that goes on sys.path. See `ensure_paho_on_path`.
VENDOR_DIRECTORY = os.path.join(PLUGIN_DIRECTORY, "_vendor")

KEEPALIVE = 30
RECONNECT_MIN = 1
RECONNECT_MAX = 60

# Bounds on paho's own outgoing queues. A broker that stops reading must cost a
# dropped publish and a log line, not unbounded growth inside the process that
# draws the television.
MAX_QUEUED_MESSAGES = 200
MAX_INFLIGHT_MESSAGES = 20

# paho's MQTTErrorCode.MQTT_ERR_QUEUE_SIZE, spelled out so that reading a publish
# result needs no import of the vendored package.
MQTT_ERR_QUEUE_SIZE = 15

# How much of a payload a debug line is allowed to carry.
LOG_PAYLOAD_LIMIT = 500
SLOW_DISPATCH_SECONDS = 0.25


def ensure_paho_on_path():
    """Make the vendored copy importable as `paho`, exactly once.

    The directory inserted here is searched **before the standard library**, for
    the whole enigma2 process and every other plugin in it. So it is the
    `_vendor` directory and never the plugin's own: this package ships `config`,
    `setup`, `log`, `keys`, `plugin`, `commands` and `version`, and putting that
    directory at `sys.path[0]` would make `import config` anywhere in enigma2
    resolve to ours.
    """
    if VENDOR_DIRECTORY not in sys.path:
        sys.path.insert(0, VENDOR_DIRECTORY)
    return VENDOR_DIRECTORY


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
        self._signal = signal
        if hasattr(signal, "get"):
            signal.get().append(self._drain)
        else:
            self._connection = signal.connect(self._drain)

    def __call__(self, function, *args, **kwargs):
        pump = self._pump
        if pump is None:
            LOG.debug("the message pump is closed; dropping a queued handler")
            return
        self._queue.put((function, args, kwargs))
        pump.send(0)

    def stop(self):
        """Detach from the pump.

        A session is replaced whenever the settings change, and a listener left
        attached is one more `_drain` on the pump for every reload the box ever
        does - each holding the dead session's queue alive.
        """
        signal, self._signal = self._signal, None
        self._pump = None
        if signal is None:
            return
        try:
            if self._connection is not None:
                # enigma2's own signal connection: dropping the object is the
                # documented way to disconnect it.
                self._connection = None
            elif hasattr(signal, "get"):
                listeners = signal.get()
                if self._drain in listeners:
                    listeners.remove(self._drain)
        except Exception:
            LOG.exception("could not detach from the message pump")

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


def make_dispatcher():
    """However this image lets a background thread reach the main loop.

    None when there is no way at all. Running a callback on paho's network
    thread is not a fallback - it would touch enigma2 from off the main loop,
    which is how a receiver loses its user interface - so a bridge with no
    dispatcher stays idle instead.
    """
    reactor = _twisted_reactor()
    if reactor is not None:
        return reactor.callFromThread
    try:
        return MessagePumpDispatcher()
    except Exception:
        LOG.error("unsupported image: no main-loop bridge")
        return None


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
        if dispatcher is not None:
            self._dispatch = dispatcher
            self._owns_dispatcher = False
        else:
            self._dispatch = make_dispatcher()
            self._owns_dispatcher = True
        self._factory = client_factory if client_factory is not None else default_client_factory
        self._client = None
        self._will = None
        self._epoch = 0
        self._epoch_started = None
        self._disconnect_callbacks = 0
        self._dispatch_lock = threading.Lock()
        self._dispatch_pending = 0
        self._dispatch_peak = 0
        self._dispatch_count = 0
        self._dispatch_delay_total = 0.0
        self._dispatch_delay_max = 0.0

    @property
    def usable(self):
        """False on an image that offers no way to reach the main loop."""
        return self._dispatch is not None

    # --------------------------------------------------------------- lifecycle --

    def set_will(self, topic, payload, qos=1, retain=True):
        """Registered before the connection, or the broker never learns about it."""
        self._will = (topic, payload, qos, retain)

    def start(self):
        if not self.usable:
            LOG.error("no main-loop bridge on this image; not connecting")
            return None
        if self._client is not None:
            # Never reuse a client object: paho's network thread is tied to it,
            # and a second loop_start on the same one is a stranded thread. The
            # thread bridge is left alone - this session is about to need it.
            LOG.warning("a session was already open; closing it before opening another")
            previous, self._client = self._client, None
            self.connected = False
            self._close_client(previous)

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
        _bound_queues(client)

        self._client = client
        self._epoch += 1
        self._epoch_started = time.monotonic()
        self._disconnect_callbacks = 0
        client.connect_async(
            self.settings.host, self.settings.port, keepalive=self.settings.keepalive
        )
        client.loop_start()
        LOG.info("mqtt epoch %d: connection attempt started (tls=%s)",
                 self._epoch, "on" if self.settings.tls else "off")
        return client

    def stop(self):
        client = self._client
        self.connected = False
        self._client = None
        self._close_dispatcher()
        if client is not None:
            self._close_client(client)

    def _close_client(self, client):
        try:
            client.disconnect()
        except Exception:
            LOG.exception("disconnect failed")
        # `loop_stop()` joins paho's network thread, and that thread may be
        # inside a connect attempt to an address that black-holes packets:
        # 2-5 seconds of a blocked caller, measured. Both callers of `stop` are
        # on enigma2's main thread - the setup screen's Save, and the shutdown
        # hook - so the join gets a thread of its own and the user interface
        # never waits for a broker to time out.
        try:
            threading.Thread(
                target=_join_network_thread,
                args=(client,),
                name="mqttbridge-stop",
                daemon=True,
            ).start()
        except Exception:
            LOG.exception("could not hand the network loop's shutdown to a thread")
        LOG.info("disconnected")

    def _close_dispatcher(self):
        """Only the one this client made: a dispatcher that was handed in belongs
        to whoever handed it in."""
        if not self._owns_dispatcher:
            return
        closer = getattr(self._dispatch, "stop", None)
        if closer is None:
            return
        try:
            closer()
        except Exception:
            LOG.exception("closing the thread bridge failed")

    # ------------------------------------------------------------------- publish --

    def publish(self, topic, payload=None, qos=0, retain=False):
        client = self._client
        if client is None:
            LOG.debug("no session; dropping a publish")
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
            LOG.exception("publishing failed")
            return None
        if getattr(info, "rc", None) == MQTT_ERR_QUEUE_SIZE:
            # paho took nothing: the outgoing queue is at its bound, which means
            # the broker has not been reading for a while. Say so once per
            # publish rather than let the message vanish silently.
            LOG.warning(
                "the outgoing queue is full (%d messages); publish was not queued",
                MAX_QUEUED_MESSAGES,
            )
        if LOG.isEnabledFor(10):  # DEBUG
            LOG.debug(
                "publish qos=%d retain=%s bytes=%d",
                qos,
                retain,
                len(data),
            )
        return info

    def subscribe(self, topic, qos=1):
        client = self._client
        if client is None:
            LOG.debug("no session; not subscribing")
            return None
        try:
            result = client.subscribe(topic, qos=qos)
        except Exception:
            LOG.exception("subscribing failed")
            return None
        LOG.info("subscribed (qos %d)", qos)
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
        self._queue_dispatch("connect", self._handle_connect, reason_code)

    def _paho_on_message(self, client, userdata, message):
        try:
            topic = message.topic
            payload = message.payload
            retain = bool(getattr(message, "retain", False))
        except Exception:
            LOG.exception("an incoming message could not be read")
            return
        self._queue_dispatch("message", self._handle_message, topic, payload, retain)

    def _paho_on_disconnect(self, client, userdata, flags=None, reason_code=None, properties=None):
        self._queue_dispatch("disconnect", self._handle_disconnect, reason_code)

    def _queue_dispatch(self, kind, function, *args):
        queued_at = time.monotonic()
        with self._dispatch_lock:
            self._dispatch_pending += 1
            self._dispatch_peak = max(self._dispatch_peak, self._dispatch_pending)
        self._dispatch(self._run_dispatched, kind, queued_at, function, args)

    def _run_dispatched(self, kind, queued_at, function, args):
        delay = max(0.0, time.monotonic() - queued_at)
        with self._dispatch_lock:
            self._dispatch_pending = max(0, self._dispatch_pending - 1)
            self._dispatch_count += 1
            self._dispatch_delay_total += delay
            self._dispatch_delay_max = max(self._dispatch_delay_max, delay)
            pending = self._dispatch_pending
            peak = self._dispatch_peak
        if delay >= SLOW_DISPATCH_SECONDS:
            LOG.warning("slow main-loop dispatch kind=%s delay_ms=%d pending=%d peak=%d",
                        kind, int(delay * 1000), pending, peak)
        function(*args)

    def _dispatch_summary(self):
        with self._dispatch_lock:
            count = self._dispatch_count
            average = self._dispatch_delay_total / count if count else 0.0
            return count, average, self._dispatch_delay_max, self._dispatch_peak

    def diagnostics(self):
        """The counters the log lines are made of, for the OpenWebif page.

        Read from memory only, so it is safe on the main loop while a request is
        being answered; nothing in it identifies the broker or the login.
        """
        count, average, maximum, peak = self._dispatch_summary()
        with self._dispatch_lock:
            pending = self._dispatch_pending
        return {
            "epoch": self._epoch,
            "dispatched": count,
            "dispatch_delay_avg_ms": int(average * 1000),
            "dispatch_delay_max_ms": int(maximum * 1000),
            "dispatch_pending": pending,
            "dispatch_peak": peak,
        }

    # -------------------------------------------------------- main-thread halves --

    def _handle_connect(self, reason_code):
        try:
            if not reason_is_success(reason_code):
                self.connected = False
                LOG.error("the broker refused the connection: %s", reason_code)
                return
            if self._disconnect_callbacks:
                self._epoch += 1
            self.connected = True
            elapsed = 0.0 if self._epoch_started is None else time.monotonic() - self._epoch_started
            count, average, maximum, peak = self._dispatch_summary()
            LOG.info(
                "mqtt epoch %d: connected in %.3fs; lifetime dispatch "
                "count=%d avg_ms=%d max_ms=%d peak=%d",
                self._epoch, elapsed, count, int(average * 1000), int(maximum * 1000), peak,
            )
            self._disconnect_callbacks = 0
            if self._on_connect is not None:
                self._on_connect()
        except Exception:
            LOG.exception("the connect handler raised; the receiver is unaffected")

    def _handle_message(self, topic, payload, retain):
        try:
            if self._on_message is not None:
                self._on_message(topic, payload, retain)
        except Exception:
            LOG.exception("a message handler raised; the receiver is unaffected")

    def _handle_disconnect(self, reason_code):
        try:
            self.connected = False
            self._disconnect_callbacks += 1
            count, average, maximum, peak = self._dispatch_summary()
            LOG.warning(
                "mqtt epoch %d: disconnected reason=%s callback=%d duplicate=%s; "
                "lifetime dispatch count=%d avg_ms=%d max_ms=%d peak=%d",
                self._epoch, reason_code, self._disconnect_callbacks,
                self._disconnect_callbacks > 1, count, int(average * 1000),
                int(maximum * 1000), peak,
            )
            if self._disconnect_callbacks == 1:
                self._epoch_started = time.monotonic()
                LOG.info("mqtt epoch %d: automatic reconnect pending", self._epoch + 1)
            if self._on_disconnect is not None:
                self._on_disconnect(reason_code)
        except Exception:
            LOG.exception("the disconnect handler raised; the receiver is unaffected")


def _join_network_thread(client):
    """`loop_stop()`, off the main thread. Never raises into the thread runner."""
    try:
        client.loop_stop()
    except Exception:
        LOG.exception("stopping the network loop failed")


def _bound_queues(client):
    """Cap what paho will hold for a broker that has stopped reading."""
    for name, size in (
        ("max_queued_messages_set", MAX_QUEUED_MESSAGES),
        ("max_inflight_messages_set", MAX_INFLIGHT_MESSAGES),
    ):
        setter = getattr(client, name, None)
        if setter is None:
            continue
        try:
            setter(size)
        except Exception:
            LOG.exception("%s(%d) was refused", name, size)


def _describe(data):
    """A payload as a debug line may carry it: decoded, redacted, truncated."""
    try:
        text = data.decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return f"<{len(data)} bytes>" if data else "<empty>"
    text = redact(text)
    if len(text) > LOG_PAYLOAD_LIMIT:
        return text[:LOG_PAYLOAD_LIMIT] + "\u2026"
    return text
