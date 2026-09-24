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

**A field the payload stamps from the clock is not part of that comparison.** A
publisher names such fields in `volatile`, and they ride in every payload while
staying out of the change test. Left in, they answer „did the code run again?"
rather than „did the receiver change?", and a topic rebuilt by a timer would
then republish itself for as long as the box is switched on.
"""

import json
import time
from collections import OrderedDict

from . import boxinfo, discovery, wol
from . import config as settings_module
from .cec import TOPIC as CEC_TOPIC
from .cec import CecPublisher
from .commands import CommandDispatcher
from .diagnostics import LoopMonitor
from .log import configure as configure_logging
from .log import get_logger, register_secret
from .mqttclient import BrokerSettings, MqttClient
from .publisher import Publisher
from .uninstall import RUNNING as UNINSTALL_RUNNING
from .uninstall import Uninstaller
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


def _encoded(payload):
    """The canonical JSON for a payload: one dictionary, always the same bytes."""
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _change_key(encoded, payload, volatile):
    """What „has this changed?" is asked about: the payload without its own stamps.

    A field a payload writes from the wall clock moves whether or not the
    receiver did, so comparing bytes that carry one answers „did this run
    again?" instead of „is this different?". On a retained topic rebuilt by a
    timer that means a republish every time the timer fires however little has
    moved — a state change delivered to every consumer and a row in somebody's
    recorder, for a payload saying exactly what it said before — and nothing
    bounds it, because the timer does not stop.

    So the named fields are left out of the comparison and kept in what goes
    out. A consumer still reads a stamp; what the stamp means is now when the
    payload last differed, which is the question it was being asked anyway.
    """
    if not volatile or not isinstance(payload, dict):
        return encoded
    return _encoded(
        dict((name, value) for name, value in payload.items() if name not in volatile)
    )


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

# Claimed where the package manager installed this very copy of the plugin
# (`uninstall.py`), and by nothing else — there is no publisher behind it.
UNINSTALL_CAPABILITY = "uninstall"

SHUTDOWN_FLUSH_SECONDS = 1.0

# What a session is: where the broker is, who logs in, how, and under which
# name. A reload after a save that changed one of these — or switched the plugin
# off — says `offline` first; any other save reconnects silently. The last two
# name the will's topic, which makes a change to them a rename (see `reload`).
IDENTITY_CONNECTION_SETTING_NAMES = ("node_id", "base_topic")
CONNECTION_SETTING_NAMES = (
    "host", "port", "tls", "ca_file", "username", "password",
) + IDENTITY_CONNECTION_SETTING_NAMES

# A command name comes off the topic, so its length is the publisher's choice.
# The retained `last_error` is not the place to store somebody's 4 KB topic.
LAST_ERROR_CMD_LIMIT = 64
SLOW_SNAPSHOT_PUBLISHER_SECONDS = 0.25
SLOW_SNAPSHOT_TOTAL_SECONDS = 1.0

# What the OpenWebif page is allowed to show of what went out: the last payload
# of each retained JSON topic, as a consumer received it. Bounded both ways,
# because a channel select's discovery payload alone can run to tens of
# kilobytes and the page lives in the GUI process.
REMEMBERED_TOPICS = 256
REMEMBERED_BYTES = 16384

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
        # Built with the bridge, so that nothing the removal runs is a first
        # import after the package has gone.
        self._uninstaller = Uninstaller(self)
        # A refusal that has to wait for a session to be published on: the
        # reason a removal failed, reported once the fresh session is up.
        self._pending_error = None
        self._last_error_published = False
        # topic -> the bytes last sent to it, so a repeat can be dropped.
        self._published = {}
        # topic -> the JSON last published to it, for the OpenWebif page. Not
        # `_published`: that holds the change key, which for a topic with
        # volatile fields is the payload *without* its timestamps. Raw topics
        # (`availability`, the screenshot) are only named, never kept.
        self._last_json = OrderedDict()
        self._raw_topics = set()
        # The last picture this process put on `screen`, and when it was taken,
        # for the OpenWebif page. Held here and not on the publisher, because
        # the publisher is replaced by every settings save while the retained
        # picture on the broker outlives all of them. The same bytes object the
        # publisher sent — no copy.
        self._screenshot = None
        self._screenshot_topic = None
        # The open session's will topic, and the settings it connected with:
        # what `reload` compares a save against.
        self._will_topic = None
        self._session_settings = None

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

    def published_settings(self):
        """What `info.settings` carries: the non-secret settings, writable or not.

        Presence is not permission. The writable ones are the `cmd/config`
        allowlist and nothing else; the rest are read-only over MQTT and are set
        on the receiver — its setup screen, the provisioning file or the
        OpenWebif page — which `docs/TOPICS.md` says member by member.
        """
        values = self.remote_settings()
        for name in settings_module.READ_ONLY_SETTING_NAMES:
            # Published the way the command guard applies it — anything falsy is
            # a refusal — so it is a boolean even on an image where the element
            # did not build, and a consumer reading it hides the control.
            values[name] = bool(self.value(name))
        return values

    def apply_remote_settings(self, values):
        """Persist one validated replacement, then apply its publisher lifecycle."""
        if self._uninstaller.closed:
            return UNINSTALL_RUNNING
        if not settings_module.save_remote_settings(values, self.settings):
            return "could not persist the plugin settings"

        self._replace_configurable_publishers()
        info = self.build_info()
        self.publish_json(self.topic("info"), info)
        self.publish_discovery(info)
        return None

    def apply_settings(self, values):
        """Persist settings the broker cannot write, then restart the way the setup screen does.

        The setup screen's path, taken for any change outside `cmd/config`'s
        allowlist: save, write the settings file once, `reload()`. The reconnect
        publishes `info` — and in discovery mode the buttons a permission gates —
        so a setting changed on the OpenWebif page reaches a consumer exactly as
        one changed on the television does, and a kill-switch rebinds its hook
        the same way. `values` must already be validated.
        """
        if self._uninstaller.closed:
            return UNINSTALL_RUNNING
        if not settings_module.save_settings(values, self.settings):
            return "could not persist the plugin settings"
        self.reload()
        return None

    def run_command(self, name, text, origin):
        """A command from somewhere other than the broker — the dispatcher's refusal, or None.

        Nothing runs while the plugin is removing itself, and the refusal is not
        published: `last_error` would be a retained topic created after the
        retraction, which is the one thing the removal must not leave behind.
        """
        if self._uninstaller.closed:
            return UNINSTALL_RUNNING
        return self._commands.run(name, text, origin)

    @property
    def uninstaller(self):
        return self._uninstaller

    def discarded_retained_commands(self):
        """This node's command topics somebody left a retained message on, this session."""
        return set(self._commands.discarded_retained)

    def last_payloads(self):
        """`(topic, payload)` for every retained topic this process published, sorted.

        The payload is the JSON as it went out, or None for a raw topic, which
        the page names without showing.
        """
        rows = [(topic, payload) for topic, payload in self._last_json.items()]
        rows.extend((topic, None) for topic in self._raw_topics)
        return sorted(rows)

    def last_error(self):
        """The `last_error` payload as it stands on the broker, or None when it is clear."""
        if not self._last_error_published:
            return None
        return self._last_json.get(self.topic("last_error"))

    def diagnostics(self):
        """What §3.2's runtime diagnostics measure, read from memory for the page."""
        values = {}
        if self.client is not None:
            reader = getattr(self.client, "diagnostics", None)
            if reader is not None:
                values.update(reader())
        if self._loop_monitor is not None:
            reader = getattr(self._loop_monitor, "state", None)
            if reader is not None:
                values.update(reader())
        return values

    def _remember(self, topic, encoded):
        """Keep the last JSON of one retained topic, within the page's bounds."""
        self._raw_topics.discard(topic)
        if len(encoded) > REMEMBERED_BYTES:
            encoded = encoded[: REMEMBERED_BYTES - 1] + "…"
        self._last_json.pop(topic, None)
        self._last_json[topic] = encoded
        while len(self._last_json) > REMEMBERED_TOPICS:
            self._last_json.popitem(last=False)

    def _forget(self, topic):
        self._last_json.pop(topic, None)
        self._raw_topics.discard(topic)

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
        # A setting of the receiver's, not of the connection: honoured before
        # the broker is asked about, so a box with no broker yet still does what
        # its own setup screen says. Every start, because the setup screen and
        # the OpenWebif page both apply a change by restarting the bridge.
        wol.arm(self.settings)
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
        self._will_topic = self.topic("availability")
        self._session_settings = self._connection_settings()
        self.client.set_will(self._will_topic, OFFLINE, qos=WILL_QOS, retain=True)

        self._last_error_published = self.state.knows(self.topic("last_error"))
        self.register_default_publishers()
        self._start_publishers()
        self._uninstaller.probe()

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
            self._uninstaller.abandon()
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
        """Apply changed settings. Called by the setup screen after a save.

        🔴 The old session ends with a clean disconnect, and a clean disconnect
        is exactly what tells the broker to throw the will away. When the save
        changed the connection itself — see `_says_offline_on_reload` — the
        session therefore says `offline` first, as `stop` does. Without it the
        retained `availability` stays `online` from the old session, and if the
        new one never connects — a mistyped broker, a password that is now
        wrong, or the plugin switched off on the same screen — a consumer sees a
        receiver that is online with nothing connected, for as long as nobody
        looks. The new session's own `online` replaces it the moment it connects.

        Every other save stays silent. The setup screen reloads on every save
        and the OpenWebif page on every setting outside `cmd/config`'s
        allowlist; an `offline` there would make the receiver unavailable for
        the seconds a reconnect takes, and re-fire every automation that
        watches it, for a change that did not touch the connection at all.

        Unlike `stop`, this does not wait for the publish to leave: the process
        goes on running, paho's network thread writes its queue in order and
        does not end before the queue is empty, so the `offline` reaches the
        socket ahead of the DISCONNECT — and a reload runs on enigma2's main
        thread, where waiting on a broker is what the user would feel.
        """
        try:
            self._stop_loop_monitor()
            if self.connected:
                self.retract_stale()
                if self._says_offline_on_reload():
                    self.client.publish(self._will_topic, OFFLINE, qos=STATE_QOS, retain=True)
            if self.client is not None:
                self.client.stop()
                self.client = None
            self._will_topic = None
            self._session_settings = None
            self._stop_publishers()
        except Exception:
            LOG.exception("could not shut the old session down cleanly")
        return self.start()

    def _connection_settings(self):
        """What the open session was built from, as the settings hold it now."""
        return {name: self.value(name) for name in CONNECTION_SETTING_NAMES}

    def _says_offline_on_reload(self):
        """Whether the session being replaced should publish `offline` first.

        Yes when the save switched the plugin off, or changed how the box
        reaches the broker — the new session may never connect, and nothing
        else would take the old `online` back. No when nothing of the
        connection changed, and no on a rename either: the node id and the base
        topic name the will's topic, the settings already carry the new name,
        and the old topic has just been retracted by `retract_stale`. An
        `offline` for the new name from the old session could reach the broker
        after the new session's `online` — they are two client ids, so the
        broker does not order them — and would then stay retained under a name
        that is live.
        """
        before = self._session_settings
        if before is None or self._will_topic is None:
            return False
        now = self._connection_settings()
        if any(before[name] != now[name] for name in IDENTITY_CONNECTION_SETTING_NAMES):
            return False
        if not self.value("enabled"):
            return True
        return before != now

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
            self._forget(topic)
            if topic == self._screenshot_topic:
                self._screenshot = None
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
        if self._uninstaller.claimed:
            names.append(UNINSTALL_CAPABILITY)
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
        # The standby workaround's count outlives the workaround on the broker.
        # Switching it off on the setup screen reconnects without it, and a
        # retained `cec` left behind would tell a consumer it is still at work.
        # Keyed on the publisher rather than the setting, because the capability
        # also goes when HDMI-CEC does.
        if self.publisher(CecPublisher.name) is None:
            self.retract(self.topic(CEC_TOPIC))
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
        if self._pending_error is not None:
            command, message = self._pending_error
            self._pending_error = None
            self.publish_last_error(command, message)

    def on_message(self, topic, payload, retain):
        if self._uninstaller.closed:
            # Asked first: from here on nothing may re-create a topic.
            LOG.info("discarding a message on %s: the plugin is removing itself", topic)
            return
        self._commands.handle(topic, payload, retain)

    def on_disconnect(self, reason_code):
        pass

    # ----------------------------------------------------------------- publishing --

    def publish_raw(self, topic, payload, retain=True, change_key=None):
        if self.client is None or self._uninstaller.closed:
            # Closed: a publisher's timer or event that fires after the
            # retraction would re-create a retained topic for good.
            return None
        info = self.client.publish(topic, payload, qos=STATE_QOS, retain=retain)
        if retain:
            self.state.remember(topic)
            # What is remembered is what the next comparison will be made
            # against — never the bytes that went out, when the two differ. A
            # snapshot publish and a state publish reach the same topic, and if
            # they recorded it two different ways the first publish after every
            # connect would look like a change.
            self._published[topic] = payload if change_key is None else change_key
            self._last_json.pop(topic, None)
            if len(self._raw_topics) < REMEMBERED_TOPICS:
                self._raw_topics.add(topic)
            if topic == self.topic("screen") and payload:
                self._record_screenshot(payload)
        return info

    def _record_screenshot(self, payload):
        """Remember the picture just sent on `screen`, with the time it was taken.

        The time is the publisher's — when `grab` finished — so a snapshot that
        republishes an older picture keeps that picture's time.
        """
        publisher = self.publisher("screenshot")
        taken = getattr(publisher, "completed_at", None) if publisher is not None else None
        self._screenshot = (payload, taken if taken is not None else time.time())
        self._screenshot_topic = self.topic("screen")

    def last_screenshot(self):
        """`(jpeg bytes, completed_at)` of the last picture put on `screen`, or None.

        Only while `screen` is still the topic it went out on: after a rename of
        the node or the base topic the old picture is somebody else's retained
        topic, and is retracted as such.
        """
        if self._screenshot is None or self._screenshot_topic != self.topic("screen"):
            return None
        return self._screenshot

    def publish_json(self, topic, payload, retain=True, volatile=()):
        encoded = _encoded(payload)
        info = self.publish_raw(
            topic, encoded, retain=retain, change_key=_change_key(encoded, payload, volatile)
        )
        if retain and self.client is not None:
            self._remember(topic, encoded)
        return info

    def forget_published(self):
        """Forget what was published, so the next publish goes out regardless.

        Used wherever the broker's copy stops being what this process last sent
        it — a new connection, and a reset.
        """
        self._published = {}

    def publish_state(self, suffix, payload, raw=False, retain=True, volatile=()):
        """A feature area's state topic — published only when it has changed.

        `sort_keys` in `_encoded` is what makes the comparison meaningful: two
        dictionaries built in a different order encode to the same bytes, so
        „changed" means the box changed, not that the code walked it differently.
        `volatile` is that argument carried one step further, and it is the
        publisher owning the topic that names the fields — so a reader sees at
        the call site which topic tolerates what, rather than finding a list of
        exceptions here.
        A topic that is not retained (`key`) is never compared — every press is
        an event, including the same press twice.
        """
        topic = self.topic(suffix)
        if raw:
            encoded = payload
        else:
            encoded = _encoded(payload)
        change_key = _change_key(encoded, payload, volatile)
        if retain and self._published.get(topic) == change_key:
            return None
        info = self.publish_raw(topic, encoded, retain=retain, change_key=change_key)
        if not raw and retain and self.client is not None:
            self._remember(topic, encoded)
        return info

    def retract(self, topic):
        if topic == self.topic("screen"):
            # Whether or not the broker can be told: switching screenshots off
            # means the page must not keep showing the last one either.
            self._screenshot = None
        if self.client is None or self._uninstaller.closed:
            return None
        info = self.client.publish(topic, "", qos=STATE_QOS, retain=True)
        self.state.forget(topic)
        self._published.pop(topic, None)
        self._forget(topic)
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
            # Read from the image at every publish, never from what `wol_arm`
            # asked for — the image's switch can be changed in its own menu.
            "wol": wol.report(),
            "ha_mode": self.value("ha_mode"),
            "settings": self.published_settings(),
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
                volatile = getattr(publisher, "volatile", ())
                for suffix, payload in publisher.snapshot().items():
                    if suffix in raw:
                        self.publish_raw(self.topic(suffix), payload)
                    else:
                        self.publish_json(self.topic(suffix), payload, volatile=volatile)
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
        if info is None:
            info = self.build_info()
        components = discovery.build_discovery_components(
            self.node_id,
            self.value("friendly_name"),
            self.base_topic,
            info,
            prefix=self.discovery_prefix,
            channel_options=self.channel_options(),
            # From the same payload the announcement carries, so what is
            # announced and what is published cannot disagree about it.
            deep_standby_allowed=bool(info.get("settings", {}).get("deep_standby_allowed")),
            softcam_restart_allowed=bool(
                info.get("settings", {}).get("softcam_restart_allowed")
            ),
            epg_import_allowed=bool(info.get("settings", {}).get("epg_import_allowed")),
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
        self._last_json.clear()
        self._raw_topics.clear()
        self._screenshot = None

        info = self.build_info()
        self.publish_raw(self.topic("availability"), ONLINE)
        self.publish_snapshot(info)
        self.sync_grid_slugs()
        self.publish_announcement(info)
        self.publish_discovery(info)
        self.state.save()
        return len(topics)

    # ----------------------------------------------------------------- uninstall --
    # The bridge's half of `uninstall.py`: what only the bridge can reach.

    def forget_everything_published(self):
        """Step 5 of the removal: nothing this process published is on the broker any more."""
        self.state.forget_all()
        self.forget_published()
        self._last_json.clear()
        self._raw_topics.clear()
        self._screenshot = None
        self._last_error_published = False

    def disconnect_for_uninstall(self):
        """Step 6: a clean disconnect, so the will stays unsent and `offline` stays retained.

        The shutdown hook runs later, when the interface restarts; it finds no
        session and publishes nothing.
        """
        self.running = False
        self.idle_reason = "the plugin is being removed"
        self._stop_loop_monitor()
        if self.client is not None:
            self.client.stop()
            self.client = None

    def restart_after_failed_uninstall(self, command, message):
        """A removal that stopped ends where a reset ends: everything back, and why.

        `reload()` opens a fresh session, whose connect republishes availability,
        the snapshot, the announcement and discovery. The reason goes on
        `last_error` after that, on the new session — before it there is nobody
        to publish it to.
        """
        self._pending_error = (command, message)
        self.reload()
