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

Publishers are how this file grows without being rewritten: a publisher has a
name, which becomes a capability, a `start()` that binds its enigma2 hooks and
says whether the image provided them, and a `snapshot()` that is called on
connect and on reset.

**A publisher publishes on change.** `publish_state` compares what it is about
to send with what went out last and drops a repeat, because a receiver that
re-publishes the same volume every five seconds writes a row into somebody's
recorder database every five seconds. The snapshot is the deliberate exception:
`on_connect` forgets everything it knows and sends the lot.
"""

import json
import time

from . import boxinfo, discovery
from . import config as settings_module
from .commands import CommandDispatcher
from .diagnostics import LoopMonitor
from .log import configure as configure_logging
from .log import get_logger, register_secret
from .mqttclient import BrokerSettings, MqttClient
from .publisher import Publisher
from .version import __version__

# `Publisher` is re-exported: it is part of this module's interface — every
# publisher subclasses it and the tests import it from here — and it lives in
# its own module only to keep the imports acyclic.
__all__ = ["Bridge", "Publisher"]

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
# `power`, `service`, `epg`, `tuner`, … — each added by the publisher that bound
# its hooks. The connection itself is not a capability, so this tuple is empty
# and stays empty: everything in the list got there by working.
CORE_CAPABILITIES = ()

# The one capability with no publisher behind it. `message` is a command, so
# what makes it real is the popup machinery being importable, not a hook.
MESSAGE_CAPABILITY = "message"

SHUTDOWN_FLUSH_SECONDS = 1.0

# A command name comes off the topic, so its length is the publisher's choice.
# The retained `last_error` is not the place to store somebody's 4 KB topic.
LAST_ERROR_CMD_LIMIT = 64
SLOW_SNAPSHOT_PUBLISHER_SECONDS = 0.25
SLOW_SNAPSHOT_TOTAL_SECONDS = 1.0

_DEFAULT_MONITOR = object()


class Bridge:
    def __init__(self, session=None, settings=None, client_factory=None, dispatcher=None,
                 state_store=None, provisioning_path=None, log_path=None,
                 loop_monitor=_DEFAULT_MONITOR):
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
        self._loop_monitor = LoopMonitor() if loop_monitor is _DEFAULT_MONITOR else loop_monitor
        self._publishers = []
        self._commands = CommandDispatcher(self)
        self._last_error_published = False
        # topic -> the bytes last sent to it, so a repeat can be dropped.
        self._published = {}

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

    def remote_settings(self):
        """The non-secret settings exposed to and writable by the integration."""
        return {
            name: self.value(name) for name in settings_module.REMOTE_SETTING_NAMES
        }

    def apply_remote_settings(self, values):
        """Persist one validated replacement, then apply its publisher lifecycle."""
        if not settings_module.save_remote_settings(values, self.settings):
            return "could not persist the plugin settings"

        self._replace_configurable_publishers()
        info = self.build_info()
        self.publish_json(self.topic("info"), info)
        self.publish_discovery(info)
        return None

    def _replace_configurable_publishers(self):
        """Rebind only hooks controlled by cmd/config; preserve all other work."""
        from .cam import CamPublisher
        from .oscam import OscamPublisher
        from .publishers import PUBLISHER_CLASSES
        from .remote import KeyPublisher
        from .screen import ScreenPublisher

        replacements = (CamPublisher, OscamPublisher, KeyPublisher, ScreenPublisher)
        for publisher_class in replacements:
            name = publisher_class.name
            old = self.publisher(name)
            if old is not None:
                old.stop()
                self._publishers.remove(old)

            replacement = publisher_class(self)
            try:
                started = replacement.start()
            except Exception:
                LOG.exception("the %s publisher could not restart", name)
                started = False
            if started:
                order = PUBLISHER_CLASSES.index(publisher_class)
                index = sum(
                    PUBLISHER_CLASSES.index(type(item)) < order
                    for item in self._publishers
                    if type(item) in PUBLISHER_CLASSES
                )
                self._publishers.insert(index, replacement)

        if self.value("screenshot") == "off":
            # A disabled private image must not remain readable from broker retention.
            self.retract(self.topic("screen"))
        if not self.value("cam_telemetry"):
            self.retract(self.topic("cam"))
        if not self.value("oscam_telemetry"):
            self.retract(self.topic("oscam"))

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
            self._stop_loop_monitor()
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
        if self._loop_monitor is not None:
            self._loop_monitor.start()
        self.client.set_will(self.topic("availability"), OFFLINE, qos=WILL_QOS, retain=True)

        self._last_error_published = self.state.knows(self.topic("last_error"))
        self.register_default_publishers()
        self._start_publishers()

        self.idle_reason = None
        self.running = True
        LOG.info(
            "starting: ha_mode=%s plugin=%s capabilities=%d",
            self.value("ha_mode"),
            __version__,
            len(self.capabilities()),
        )
        self.client.start()

    def _idle(self, reason):
        self._stop_loop_monitor()
        self.running = False
        self.idle_reason = reason
        LOG.warning("idle: %s", reason)

    def stop(self):
        """Say goodbye properly: a clean disconnect suppresses the will."""
        try:
            self.running = False
            self._stop_loop_monitor()
            if self.client is None:
                self._stop_publishers()
                return
            if self.client.connected:
                info = self.client.publish(
                    self.topic("availability"), OFFLINE, qos=STATE_QOS, retain=True
                )
                self.client.wait_for(info, SHUTDOWN_FLUSH_SECONDS)
            self._stop_publishers()
            self.client.stop()
            self.client = None
            self.state.save()
        except Exception:
            LOG.exception("shutting the bridge down raised; the receiver is unaffected")

    def _stop_publishers(self):
        """Every publisher lets go of its hooks, and the registry is emptied.

        🔴 Emptying the registry without this is how a receiver ends up with two
        of every listener. Publishers attach themselves to lists that belong to
        enigma2 — `session.nav.event`, the standby counter's notifiers, the
        action map — and those lists outlive the plugin's own objects. Dropping
        the registry on a settings save would leave the old listeners attached
        and add a second set beside them, once per save, until the box is
        restarted.
        """
        for publisher in list(self._publishers):
            try:
                publisher.stop()
            except Exception:
                LOG.exception("stopping the %s publisher raised", publisher.name)
        self._publishers = []

    def _stop_loop_monitor(self):
        if self._loop_monitor is not None:
            self._loop_monitor.stop()

    def reload(self):
        """Apply changed settings. Called by the setup screen after a save."""
        try:
            self._stop_loop_monitor()
            if self.connected:
                self.retract_stale()
            if self.client is not None:
                self.client.stop()
                self.client = None
            self._stop_publishers()
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
        if getattr(publisher, "bridge", None) is None:
            publisher.bridge = self
        self._publishers.append(publisher)
        return publisher

    def publisher(self, name):
        """One registered publisher by name, or None when it did not bind.

        Commands ask for the publisher of the state they are about to change,
        because a command is verified by reading the state back — and a state
        nothing publishes cannot be read back.
        """
        for publisher in self._publishers:
            if publisher.name == name:
                return publisher
        return None

    def register_default_publishers(self):
        """The feature areas of the contract, in the order `capabilities` lists them.

        Only when nothing has been registered by hand, and only with a session:
        every one of these hangs off `session.nav` or a screen, so a bridge
        built without one would register publishers that could only fail to
        start. A test that registers its own is left alone.
        """
        if self._publishers or self.session is None:
            return []
        from .publishers import default_publishers

        for publisher in default_publishers(self):
            self.register_publisher(publisher)
        return self._publishers

    def _start_publishers(self):
        for publisher in list(self._publishers):
            try:
                if not publisher.start():
                    if publisher.switched_off:
                        # The publisher has already said so in its own words,
                        # and it says it on the settings-change path too, where
                        # this loop does not run.
                        LOG.debug("%s did not start: switched off", publisher.name)
                    else:
                        LOG.warning("this image does not provide the %s hooks", publisher.name)
                    self._publishers.remove(publisher)
            except Exception:
                LOG.exception("the %s publisher could not start", publisher.name)
                self._publishers.remove(publisher)

    def capabilities(self):
        """What this box can actually do — never what the contract says it might.

        A name gets in here by a hook binding on *this* image. Consumers hide
        what is missing, so a capability claimed and not delivered is a dead
        entity in somebody's dashboard. Registered is not the same as bound:
        a publisher that is still waiting for a hook stays in the registry and
        out of this list until it has one — see `Publisher.claimed`.
        """
        names = list(CORE_CAPABILITIES)
        for publisher in self._publishers:
            if publisher.name and publisher.name not in names and publisher.claimed():
                names.append(publisher.name)
        if self.session is not None and MESSAGE_CAPABILITY not in names:
            from .osd import popups_available

            if popups_available():
                names.append(MESSAGE_CAPABILITY)
        return names

    def announce_capabilities(self):
        """Say again what this box can do, after a late bind changed the answer.

        `info` and the announcement are published on connect, so a capability
        that appears a few seconds later — a hook that could only bind once
        enigma2 had built the screen behind it — would otherwise stay invisible
        until the next reconnect.
        """
        if not self.connected:
            return False
        info = self.build_info()
        self.publish_json(self.topic("info"), info)
        self.publish_announcement(info)
        self.publish_discovery(info)
        return True

    # -------------------------------------------------------------------- events --

    def on_connect(self):
        # First, before a single new payload: whatever this node published under
        # an older name is still on the broker and this is the first chance to
        # take it back.
        self.retract_stale()
        # Privacy switches also apply to retained data left by an earlier
        # process, including when they were changed on the receiver setup screen.
        if self.value("screenshot") == "off":
            self.retract(self.topic("screen"))
        if not self.value("cam_telemetry"):
            self.retract(self.topic("cam"))
        if not self.value("oscam_telemetry"):
            self.retract(self.topic("oscam"))
        # Every payload goes out on every connect, so what was published before
        # this connection is not what is on the broker now.
        self.forget_published()
        info = self.build_info()
        self.publish_raw(self.topic("availability"), ONLINE)
        self.publish_snapshot(info)
        if self.publisher("epg_grid") is None:
            # With a grid publisher this is its business, and it does it at the
            # end of every pass. Doing it here as well would retract each grid
            # on every connect and republish it a moment later, which a consumer
            # sees as the feature disappearing and coming back.
            self.sync_grid_slugs()
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
            self._published[topic] = payload
        return info

    def publish_json(self, topic, payload, retain=True):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return self.publish_raw(topic, encoded, retain=retain)

    def forget_published(self):
        """Forget what was published, so the next publish goes out regardless.

        Used wherever the broker's copy stops being what this process last sent
        it — a new connection, and a reset.
        """
        self._published = {}

    def publish_state(self, suffix, payload, raw=False, retain=True):
        """A feature area's state topic — published only when it has changed.

        `sort_keys` in `publish_json` is what makes the comparison meaningful:
        two dictionaries built in a different order encode to the same bytes, so
        „changed" means the box changed, not that the code walked it differently.
        A topic that is not retained (`key`) is never compared — every press is
        an event, including the same press twice.
        """
        topic = self.topic(suffix)
        if raw:
            encoded = payload
        else:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if retain and self._published.get(topic) == encoded:
            return None
        return self.publish_raw(topic, encoded, retain=retain)

    def retract(self, topic):
        if self.client is None:
            return None
        info = self.client.publish(topic, "", qos=STATE_QOS, retain=True)
        self.state.forget(topic)
        self._published.pop(topic, None)
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
            "settings": self.remote_settings(),
            "capabilities": self.capabilities(),
        }

    def publish_snapshot(self, info=None):
        """Everything this node knows, as a consumer would want it on subscribe."""
        snapshot_started = time.monotonic()
        topic_count = 1
        self.publish_json(self.topic("info"), info if info is not None else self.build_info())
        for publisher in self._publishers:
            publisher_started = time.monotonic()
            published = 0
            try:
                raw = getattr(publisher, "raw", ())
                for suffix, payload in publisher.snapshot().items():
                    if suffix in raw:
                        self.publish_raw(self.topic(suffix), payload)
                    else:
                        self.publish_json(self.topic(suffix), payload)
                    published += 1
                    topic_count += 1
            except Exception:
                LOG.exception("the %s publisher could not produce a snapshot", publisher.name)
            finally:
                elapsed = time.monotonic() - publisher_started
                if elapsed >= SLOW_SNAPSHOT_PUBLISHER_SECONDS:
                    LOG.warning("slow snapshot publisher=%s elapsed_ms=%d topics=%d",
                                publisher.name, int(elapsed * 1000), published)
        elapsed = time.monotonic() - snapshot_started
        LOG.info("snapshot complete elapsed_ms=%d topics=%d", int(elapsed * 1000), topic_count)
        if elapsed >= SLOW_SNAPSHOT_TOTAL_SECONDS:
            LOG.warning("slow snapshot total elapsed_ms=%d topics=%d",
                        int(elapsed * 1000), topic_count)

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

    @property
    def discovery_prefix(self):
        return (
            self.value("ha_discovery_prefix") or settings_module.DEFAULT_DISCOVERY_PREFIX
        ).strip("/")

    def channel_options(self):
        """The channel names a `select` entity offers, in bouquet order.

        Deduplicated by name, because the options of a select are names and
        `cmd/zap` by name refuses a name that is not unique: offering the same
        „Sport" twice would be offering an option that can only fail.
        """
        channels = self.publisher("channels")
        if channels is None:
            return []
        names = []
        seen = set()
        for bouquet in channels.bouquets:
            for channel in bouquet.get("channels") or []:
                name = (channel.get("name") or "").strip()
                if name and name not in seen:
                    seen.add(name)
                    names.append(name)
        return names

    def publish_discovery(self, info=None):
        if self.value("ha_mode") != "discovery":
            return
        components = discovery.build_discovery_components(
            self.node_id,
            self.value("friendly_name"),
            self.base_topic,
            info if info is not None else self.build_info(),
            prefix=self.discovery_prefix,
            channel_options=self.channel_options(),
            deep_standby_allowed=bool(self.value("deep_standby_allowed")),
            previous=self.state.component_keys,
        )
        if not components:
            LOG.debug("this build publishes no Home Assistant discovery payloads")
            return
        for topic, payload in sorted(components.items()):
            self.publish_json(topic, payload)
        self.state.set_components(sorted(components))
        device = components.get(discovery.device_topic(self.discovery_prefix, self.node_id))
        self.state.set_component_keys(discovery.component_platforms(device))

    def sync_grid_slugs(self):
        """Retract the EPG grid of every bouquet that is no longer configured.

        A bouquet that was renamed is two things at once: a new slug nobody has
        published yet and an old slug nothing will ever update again. The second
        one is the retained ghost, and the state file is what makes it findable
        — including from a process that was restarted between the rename and
        now. Called with no grid publisher at all, this retracts the lot, which
        is what turning `epg_grid_events` down to `0` has to mean.
        """
        grid = self.publisher("epg_grid")
        published = list(getattr(grid, "published_slugs", [])) if grid is not None else []
        stale = [slug for slug in self.state.grid_slugs if slug not in published]
        for slug in stale:
            LOG.info("retracting the EPG grid of a bouquet that is no longer configured: %s", slug)
            self.retract(self.topic("epg_grid/" + slug))
        self.state.set_grid_slugs(published)
        self.state.save()
        return len(stale)

    def retract_discovery(self):
        topics = self.state.components
        if not topics:
            return 0
        LOG.info("retracting %d Home Assistant discovery payload(s)", len(topics))
        for topic in topics:
            self.retract(topic)
        self.state.set_components([])
        # Nothing is announced any more, so nothing is left to remove by name.
        self.state.set_component_keys({})
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
        # Everything on the broker was just emptied, so nothing this process
        # believes it published is true any more — including the screenshot and
        # every grid.
        self.forget_published()

        info = self.build_info()
        self.publish_raw(self.topic("availability"), ONLINE)
        self.publish_snapshot(info)
        self.sync_grid_slugs()
        self.publish_announcement(info)
        self.publish_discovery(info)
        self.state.save()
        return len(topics)
