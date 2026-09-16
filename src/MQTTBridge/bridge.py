"""The bridge itself: enigma2 on one side, the broker on the other.

Three rules shape everything below.

**The receiver comes first.** `start` cannot raise. An invalid configuration
produces one log line and an idle plugin, never a retry storm and never a
dialog, because the graphical interface must come up whatever the state of the
broker.

**`on_connect` publishes everything, every time** — availability, the full
snapshot, the announcement, and in discovery mode the discovery payloads. Not
once at start-up: a daemon that announces only at start-up never registers what
it learned after it last connected, and a broker that lost its retained store
never gets it back.

**What was published retained is remembered**, so that it can be retracted later
even from a fresh process. That record is `discovery.StateStore`.

Publishers are how the next milestone grows this file without touching it: a
publisher has a name, which becomes a capability, and a `snapshot()` that is
called on connect and on reset.
"""

import json
import time

from . import boxinfo, discovery
from . import config as settings_module
from .commands import CommandDispatcher
from .log import configure as configure_logging
from .log import get_logger, register_secret
from .mqttclient import BrokerSettings, MqttClient
from .version import __version__

LOG = get_logger("bridge")


def _capped(text, limit):
    """`text` in at most `limit` characters, visibly cut when it was too long."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


ONLINE = "online"
OFFLINE = "offline"

# The contract: state topics are retained at QoS 0, commands arrive at QoS 1.
STATE_QOS = 0
COMMAND_QOS = 1

# The will is the one publish nobody gets to retry, so it is asked for at QoS 1.
WILL_QOS = 1

# Capabilities are the feature-area names in docs/TOPICS.md and nothing else —
# `power`, `service`, `epg`, `tuner`, … — each added by the publisher that binds
# its hooks. This build binds none, so it publishes an empty list rather than
# inventing names outside the contract's vocabulary.
CORE_CAPABILITIES = ()

SHUTDOWN_FLUSH_SECONDS = 1.0

# A command name comes off the topic, so its length is the publisher's choice.
# The retained `last_error` is not the place to store somebody's 4 KB topic.
LAST_ERROR_CMD_LIMIT = 64


class Publisher:
    """One feature area's state. Subclassed in the next milestone."""

    name = ""

    # Suffixes whose payload goes out verbatim rather than as JSON. The contract
    # has three of them — `availability`, `power` and `screen` — because a
    # string and a JPEG are not improved by being wrapped in quotes.
    raw = ()

    def snapshot(self):
        """{topic suffix: payload} for everything this publisher owns."""
        return {}

    def start(self):
        """Bind enigma2 hooks. Returns True when the image provided them."""
        return True

    def stop(self):
        pass


class Bridge:
    def __init__(self, session=None, settings=None, client_factory=None, dispatcher=None,
                 state_store=None, provisioning_path=None, log_path=None):
        self.session = session
        self.settings = settings if settings is not None else settings_module.settings
        self.client = None
        self.running = False
        self.idle_reason = None
        self._client_factory = client_factory
        self._dispatcher = dispatcher
        self._state_store = state_store
        self._provisioning_path = provisioning_path
        self._log_path = log_path
        self._publishers = []
        self._commands = CommandDispatcher(self)
        self._last_error_published = False

    # ----------------------------------------------------------------- settings --

    def value(self, name):
        return settings_module.value(name, self.settings)

    def set_value(self, name, new_value):
        element = settings_module.element(name, self.settings)
        if element is None:
            return False
        element.value = new_value
        return True

    def save_settings(self):
        return settings_module.save(self.settings)

    @property
    def state(self):
        if self._state_store is None:
            self._state_store = discovery.StateStore()
        return self._state_store

    # -------------------------------------------------------------------- topics --

    @property
    def base_topic(self):
        return (self.value("base_topic") or settings_module.DEFAULT_BASE_TOPIC).strip("/")

    @property
    def node_id(self):
        return (self.value("node_id") or "").strip()

    def topic(self, suffix):
        return self.base_topic + "/" + self.node_id + "/" + suffix

    @property
    def command_root(self):
        return self.topic("cmd")

    def command_name(self, topic):
        """`enigma2/<node>/cmd/ha_mode` -> `ha_mode`, and None for anything else."""
        prefix = self.command_root + "/"
        if not str(topic or "").startswith(prefix):
            return None
        return str(topic)[len(prefix):].strip("/") or None

    @property
    def connected(self):
        return self.client is not None and self.client.connected

    # ------------------------------------------------------------------ lifecycle --

    def start(self):
        """Never raises. The graphical interface must come up regardless."""
        try:
            self._start()
        except Exception:
            LOG.exception("the bridge failed to start; the receiver is unaffected")
            self.idle_reason = "the bridge failed to start"
        return self

    def _start(self):
        if self.client is not None:
            # enigma2 can hand a plugin its session start more than once. The
            # session that is already open is the healthy one; opening a second
            # would strand the first one's network thread and its will.
            LOG.info("the bridge is already running; leaving the open session alone")
            return

        configure_logging(self.value("log_level"), self._log_path)

        # Before anything connects: an installer may have written the broker in.
        settings_module.import_provisioning(self._provisioning_path, self.settings)
        configure_logging(self.value("log_level"), self._log_path)

        self._ensure_identity()
        register_secret(self.value("password"))

        if not self.value("enabled"):
            self._idle("the plugin is switched off in its settings")
            return
        host = (self.value("host") or "").strip()
        if not host:
            self._idle("no broker address is configured")
            return
        if not self.node_id:
            self._idle("no node id could be derived")
            return

        broker = BrokerSettings(
            host=host,
            port=self.value("port"),
            tls=self.value("tls"),
            ca_file=self.value("ca_file"),
            username=self.value("username"),
            password=self.value("password"),
            client_id="mqttbridge-" + self.node_id,
        )
        self.client = MqttClient(
            broker,
            on_connect=self.on_connect,
            on_message=self.on_message,
            on_disconnect=self.on_disconnect,
            dispatcher=self._dispatcher,
            client_factory=self._client_factory,
        )
        if not self.client.usable:
            # No Twisted and no usable ePythonMessagePump. Running paho's
            # callbacks on its network thread would touch enigma2 from off the
            # main loop, so this image gets a plugin that loads and does nothing.
            self.client = None
            self._idle("this image offers no way to reach the main loop from a thread")
            return
        self.client.set_will(self.topic("availability"), OFFLINE, qos=WILL_QOS, retain=True)

        self._last_error_published = self.state.knows(self.topic("last_error"))
        self._start_publishers()

        self.idle_reason = None
        self.running = True
        LOG.info(
            "starting: node %s, broker %s:%s, ha_mode %s, plugin %s",
            self.node_id,
            broker.host,
            broker.port,
            self.value("ha_mode"),
            __version__,
        )
        self.client.start()

    def _idle(self, reason):
        self.running = False
        self.idle_reason = reason
        LOG.warning("idle: %s", reason)

    def stop(self):
        """Say goodbye properly: a clean disconnect suppresses the will."""
        try:
            self.running = False
            if self.client is None:
                return
            if self.client.connected:
                info = self.client.publish(
                    self.topic("availability"), OFFLINE, qos=STATE_QOS, retain=True
                )
                self.client.wait_for(info, SHUTDOWN_FLUSH_SECONDS)
            for publisher in self._publishers:
                try:
                    publisher.stop()
                except Exception:
                    LOG.exception("stopping the %s publisher raised", publisher.name)
            self.client.stop()
            self.client = None
            self.state.save()
        except Exception:
            LOG.exception("shutting the bridge down raised; the receiver is unaffected")

    def reload(self):
        """Apply changed settings. Called by the setup screen after a save."""
        try:
            if self.connected:
                self.retract_stale()
            if self.client is not None:
                self.client.stop()
                self.client = None
            self._publishers = []
        except Exception:
            LOG.exception("could not shut the old session down cleanly")
        return self.start()

    def _stale_topics(self):
        root = self.base_topic + "/" + self.node_id + "/"
        announcement = discovery.announcement_topic(self.node_id)
        return [
            topic
            for topic in self.state.retained_topics
            if not topic.startswith(root) and topic != announcement
        ]

    def retract_stale(self):
        """Retract retained topics this node published under a name it no longer has.

        Renaming the node or the base topic orphans everything published under
        the old one: retained payloads nothing will ever update, and in Home
        Assistant a device that looks alive. The state file is what makes them
        findable at all, and it survives a restart — which is why this runs on
        every connect and not only from `reload`. The rename is just as likely
        to happen while the box is disconnected, or while the plugin is not even
        running, as it is to happen with a session open.
        """
        stale = self._stale_topics()
        if not stale or self.client is None:
            return 0
        LOG.info("retracting %d retained topic(s) this node no longer owns", len(stale))
        for topic in stale:
            self.client.publish(topic, "", qos=STATE_QOS, retain=True)
            self.state.forget(topic)
        self.state.save()
        return len(stale)

    # ------------------------------------------------------------------ identity --

    def _ensure_identity(self):
        """Derive the node id and the device name once, then keep them forever."""
        changed = []
        if not self.node_id:
            derived = boxinfo.derive_node_id()
            self.set_value("node_id", derived)
            changed.append("node_id")
            LOG.info("derived node id %s", derived)
        if not (self.value("friendly_name") or "").strip():
            self.set_value("friendly_name", boxinfo.box_type())
            changed.append("friendly_name")
        if changed:
            self.save_settings()

    # ---------------------------------------------------------------- publishers --

    def register_publisher(self, publisher):
        self._publishers.append(publisher)
        return publisher

    def _start_publishers(self):
        for publisher in list(self._publishers):
            try:
                if not publisher.start():
                    LOG.warning("this image does not provide the %s hooks", publisher.name)
                    self._publishers.remove(publisher)
            except Exception:
                LOG.exception("the %s publisher could not start", publisher.name)
                self._publishers.remove(publisher)

    def capabilities(self):
        names = list(CORE_CAPABILITIES)
        for publisher in self._publishers:
            if publisher.name and publisher.name not in names:
                names.append(publisher.name)
        return names

    # -------------------------------------------------------------------- events --

    def on_connect(self):
        # First, before a single new payload: whatever this node published under
        # an older name is still on the broker and this is the first chance to
        # take it back.
        self.retract_stale()
        info = self.build_info()
        self.publish_raw(self.topic("availability"), ONLINE)
        self.publish_snapshot(info)
        self.publish_announcement(info)
        self.publish_discovery(info)
        self.client.subscribe(self.command_root + "/#", qos=COMMAND_QOS)
        self.state.save()

    def on_message(self, topic, payload, retain):
        self._commands.handle(topic, payload, retain)

    def on_disconnect(self, reason_code):
        pass

    # ----------------------------------------------------------------- publishing --

    def publish_raw(self, topic, payload, retain=True):
        if self.client is None:
            return None
        info = self.client.publish(topic, payload, qos=STATE_QOS, retain=retain)
        if retain:
            self.state.remember(topic)
        return info

    def publish_json(self, topic, payload, retain=True):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self.publish_raw(topic, encoded, retain=retain)

    def retract(self, topic):
        if self.client is None:
            return None
        info = self.client.publish(topic, "", qos=STATE_QOS, retain=True)
        self.state.forget(topic)
        return info

    def build_info(self):
        return {
            "image": boxinfo.image_version(),
            "enigma": boxinfo.enigma_version(),
            "plugin": __version__,
            "boxtype": boxinfo.box_type(),
            "mac": boxinfo.mac_address(),
            "ip": boxinfo.local_ip(self.value("host")),
            "uptime": boxinfo.uptime_seconds(),
            "ha_mode": self.value("ha_mode"),
            "capabilities": self.capabilities(),
        }

    def publish_snapshot(self, info=None):
        """Everything this node knows, as a consumer would want it on subscribe."""
        self.publish_json(self.topic("info"), info if info is not None else self.build_info())
        for publisher in self._publishers:
            try:
                raw = getattr(publisher, "raw", ())
                for suffix, payload in publisher.snapshot().items():
                    if suffix in raw:
                        self.publish_raw(self.topic(suffix), payload)
                    else:
                        self.publish_json(self.topic(suffix), payload)
            except Exception:
                LOG.exception("the %s publisher could not produce a snapshot", publisher.name)

    def publish_announcement(self, info=None):
        topic = discovery.announcement_topic(self.node_id)
        if self.value("ha_mode") == "off":
            LOG.debug("ha_mode is off; retracting the announcement")
            self.retract(topic)
            return
        payload = discovery.build_announcement(
            info if info is not None else self.build_info(),
            self.node_id,
            self.value("friendly_name"),
            self.base_topic,
        )
        self.publish_json(topic, payload)

    def publish_discovery(self, info=None):
        if self.value("ha_mode") != "discovery":
            return
        components = discovery.build_discovery_components(
            self.node_id,
            self.value("friendly_name"),
            self.base_topic,
            info if info is not None else self.build_info(),
        )
        if not components:
            LOG.debug("this build publishes no Home Assistant discovery payloads")
            return
        for topic, payload in sorted(components.items()):
            self.publish_json(topic, payload)
        self.state.set_components(sorted(components))

    def retract_discovery(self):
        topics = self.state.components
        if not topics:
            return 0
        LOG.info("retracting %d Home Assistant discovery payload(s)", len(topics))
        for topic in topics:
            self.retract(topic)
        self.state.set_components([])
        return len(topics)

    # ------------------------------------------------------------------ commands --

    def publish_last_error(self, command, message):
        payload = {
            "cmd": _capped(command, LAST_ERROR_CMD_LIMIT),
            "error": message,
            "ts": int(time.time()),
        }
        LOG.warning("cmd/%s refused: %s", payload["cmd"], message)
        self.publish_json(self.topic("last_error"), payload)
        self._last_error_published = True

    def clear_last_error(self):
        if not self._last_error_published:
            return False
        self.retract(self.topic("last_error"))
        self._last_error_published = False
        return True

    def set_ha_mode(self, mode):
        """The republished `info` is the acknowledgement; there is no ack topic."""
        previous = self.value("ha_mode")
        if mode != previous:
            self.set_value("ha_mode", mode)
            self.save_settings()
            LOG.info("ha_mode %s -> %s", previous, mode)
        if mode != "discovery":
            self.retract_discovery()
        info = self.build_info()
        self.publish_json(self.topic("info"), info)
        self.publish_announcement(info)
        self.publish_discovery(info)
        self.state.save()

    def reset_retained(self):
        """Retract everything this node owns, then put it straight back.

        A reset is a cleanup, not a factory reset: settings are untouched and the
        snapshot returns immediately, so it is safe to run at any time and it is
        the documented step before uninstalling.
        """
        topics = self.state.retained_topics
        LOG.info("reset: retracting %d retained topic(s)", len(topics))
        for topic in topics:
            if self.client is not None:
                self.client.publish(topic, "", qos=STATE_QOS, retain=True)
        self.state.forget_all()
        self.state.save(force=True)
        self._last_error_published = False

        info = self.build_info()
        self.publish_raw(self.topic("availability"), ONLINE)
        self.publish_snapshot(info)
        self.publish_announcement(info)
        self.publish_discovery(info)
        self.state.save()
        return len(topics)
