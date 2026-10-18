"""Commands arriving on `<base>/<node>/cmd/<name>`.

Two guards run before any handler sees a payload, and both exist because of how
MQTT actually behaves rather than how it is described.

**A retained command is discarded.** Retain is a property of the publisher, not
of the topic: anything can publish a command retained, and the broker will then
replay it on every single reconnect. The plugin logs it and does nothing, which
is the only safe reading of an instruction whose age is unknown.

**An oversized payload is discarded.** Commands are a few hundred bytes at most.
Anything larger is a mistake or a probe, and parsing it is work done on the main
thread for somebody else's benefit.

There is no acknowledgement topic. A command's answer is its state topic
changing; a refusal is `last_error`, which is cleared when a command next
succeeds.

**A command may carry an id, and its refusal says it back** (ADR-0016). The id
comes in an envelope around the command's own payload, opened here and nowhere
else (`unwrap`): no handler knows about it, and a payload that is not an
envelope reaches its handler untouched. A refusal the dispatcher publishes
itself gets the id from the dispatcher. While a handler runs, the dispatcher's
`command_id` is that command's id; work a handler leaves for a later turn
takes it then and keeps it, and gives it to `publish_last_error` when it
fails. Nothing that publishes a refusal reads `command_id` at that moment, so
a late refusal carries the id of its own command or none - never the id of
whichever command is being handled when it is published.

Every handler returns `None` when it worked and a sentence when it did not, and
that sentence is written for the person who will read it on `last_error` at
eleven at night - it says what was refused and why, not which function returned
what.

**The OpenWebif page runs the same handlers**, through `run()`, with its origin
passed down the call (`origin.py`). It is not a second implementation of any
command: the page builds the payload a broker client would have sent, the
handler runs exactly as it does for MQTT - every household-safety guard
included - and `last_error` is published or cleared exactly as it is for MQTT.
The one difference is the box-side permission, which the page does not need.
"""

import json

from .config import HA_MODES
from .log import get_logger, redact
from .origin import MQTT, PAGE, SCREEN, granted
from .publisher import Refusal

LOG = get_logger("commands")

MAX_PAYLOAD_BYTES = 4096

# How much of a rejected payload is echoed back on last_error.
ERROR_ECHO_LIMIT = 64

# The envelope a broker client may put around a command's payload, and the bound
# on the id it carries (`docs/TOPICS.md`, section 2).
ENVELOPE_ID = "id"
ENVELOPE_PAYLOAD = "payload"
COMMAND_ID_LIMIT = 64
# What an id may be made of. Nothing that needs escaping anywhere it is shown:
# a JSON string, a line of the log, the OpenWebif page.
COMMAND_ID_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-"
)

# The reason code the doors' sentence carries for a command that defines codes for its
# refusals; every other command gets the sentence alone, exactly as before.
DOORS_REASONS = {
    "list": "busy",
    "play_recording": "busy",
    "delete_recording": "busy",
    "recordings_refresh": "busy",
}
# The commands whose refusal by the doors is published even while an update has made the
# plugin silent: a list of choices always has a caller waiting for its answer or its refusal.
SAID_WHILE_SILENT = frozenset(("list",))

# The words a keyword payload may be spelled with. Case never matters.
TRUE_WORDS = ("on", "true", "yes", "1", "press")
FALSE_WORDS = ("off", "false", "no", "0")


def decode(payload):
    if payload is None:
        return ""
    if isinstance(payload, (bytes, bytearray)):
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(payload).decode("utf-8", "replace")
    return str(payload)


def parse(text):
    """A payload as an object when it is JSON, and as text when it is not.

    Several commands take either - `zap` accepts a bare service reference and a
    `{"name": ...}` object - so this answers with both rather than forcing every
    handler to try `json.loads` in a `try`.
    """
    stripped = str(text or "").strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def id_problem(value):
    """Why `value` cannot be a command's id, or None when it can.

    An id is the sender's text, and it ends up on a retained topic and in the
    log. So it is held to a length, to the characters it may be made of, and
    against the registered secrets. One that fails is not cleaned or cut: a
    changed id is no longer the one its sender is waiting for, a cut one could
    be somebody else's, and a secret cut in the middle would no longer be the
    registered text and would pass as its own remainder.

    The sentence is for the log and never holds the id. An id that holds a
    registered secret is refused whole, and one that is only a piece of a
    secret is not looked for: answering "this is part of the password" one
    character at a time would give the password to whoever asked.
    """
    if not isinstance(value, str):
        return "it is not a string"
    if not value:
        return "it is empty"
    if len(value) > COMMAND_ID_LIMIT:
        return "it is longer than " + str(COMMAND_ID_LIMIT) + " characters"
    if not COMMAND_ID_CHARACTERS.issuperset(value):
        return "it holds a character other than a letter, a digit, '.', '_', ':' or '-'"
    if redact(value) != value:
        return "it holds a value this plugin keeps secret"
    return None


def clean_id(value):
    """`value` when it may be said back as a command's id, else None."""
    return value if id_problem(value) is None else None


def _inner(value):
    """The text a handler is given for an envelope's `payload` member.

    What the sender would have published without the envelope: a string is
    that text - the empty string the empty payload - and an object is its
    JSON text, which the handler reads as it reads a payload that arrived
    that way. Written without spaces: the handler is not given more text
    than the sender wrote spacing for. `unwrap` hands over nothing else.
    """
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def unwrap(text):
    """`(text, id, problem)`: a command's own payload, out of its envelope when it has one.

    An envelope is a JSON object with exactly the members `id` and `payload`,
    whose `payload` is a string or an object. Anything else - a `payload`
    that is a number, `true`, a list or `null` included - is the command's
    own payload and comes back as it arrived, the same object, with no id.
    An envelope is opened whatever its id is worth: the sender said which
    part is the command, and an id that cannot be used (`problem` says why)
    is dropped without touching that.

    Opened once. An envelope inside an envelope is the inner command's
    payload, and the handler's to refuse.

    A payload that cannot be sent on is not an envelope either. JSON can
    spell half a surrogate pair, which is a string no UTF-8 holds: a
    handler that echoed it would raise where `last_error` is published,
    and the refusal would be lost with it. Unopened, such a message is the
    text it arrived as - which was valid UTF-8, or it was replaced when it
    was decoded.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return text, None, None
    try:
        envelope = json.loads(stripped)
        if not isinstance(envelope, dict) or set(envelope) != {ENVELOPE_ID, ENVELOPE_PAYLOAD}:
            return text, None, None
        if not isinstance(envelope[ENVELOPE_PAYLOAD], (str, dict)):
            # The two shapes a command's payload has. A number or `null`
            # there is nobody's command, so it is not this plugin's envelope.
            return text, None, None
        inner = _inner(envelope[ENVELOPE_PAYLOAD])
        inner.encode("utf-8")
    except (ValueError, RecursionError):
        # Not JSON, nested past what the parser or the encoder follows, or a
        # payload UTF-8 has no bytes for (a `UnicodeEncodeError` is a
        # `ValueError`): the handler's to judge, exactly as before there was
        # an envelope.
        return text, None, None
    problem = id_problem(envelope[ENVELOPE_ID])
    command_id = None if problem else envelope[ENVELOPE_ID]
    return inner, command_id, problem


def _echo(text):
    """A rejected payload, safe to put on a retained topic."""
    cleaned = redact(str(text or "").strip())
    if len(cleaned) > ERROR_ECHO_LIMIT:
        cleaned = cleaned[:ERROR_ECHO_LIMIT] + "\u2026"
    return cleaned


def _boolean(text):
    word = str(text or "").strip().lower()
    if word in TRUE_WORDS:
        return True
    if word in FALSE_WORDS:
        return False
    return None


class CommandDispatcher:
    def __init__(self, bridge):
        self.bridge = bridge
        self.handlers = {
            "power": self.power,
            "deep_standby": self.deep_standby,
            "reboot": self.reboot,
            "restart_gui": self.restart_gui,
            "zap": self.zap,
            "bouquet": self.bouquet,
            "zap_history": self.zap_history,
            "history_clear": self.history_clear,
            "stream_stop": self.stream_stop,
            "stream_playable": self.stream_playable,
            "volume": self.volume,
            "mute": self.mute,
            "key": self.key,
            "message": self.message,
            "list": self.choice_list,
            "timer": self.timer,
            "record": self.record,
            "play_recording": self.play_recording,
            "delete_recording": self.delete_recording,
            "recordings_refresh": self.recordings_refresh,
            "screenshot": self.screenshot,
            "softcam_restart": self.softcam_restart,
            "epg_grid": self.epg_grid,
            "epg_import": self.epg_import,
            "config": self.config,
            "discovery": self.discovery,
            "ha_mode": self.ha_mode,
            "reset": self.reset,
            "clear_error": self.clear_error,
            "uninstall": self.uninstall,
            "update_check": self.update_check,
            "update": self.update,
            "relay": self.relay,
        }
        # Command topics of this node that somebody left a retained message on,
        # this session. Discarding one is not clearing it: the broker hands it
        # out again on every subscribe. `cmd/uninstall` retracts them with
        # everything else the node owns, so nothing of this node is left behind.
        self.discarded_retained = set()
        # The id of the command a handler is running for right now, or None:
        # between commands, for a command without one, and for every command
        # from the page or the receiver's own screen.
        self.command_id = None
        # Why an id was dropped, each reason once for this run of the plugin
        # at warning. A consumer whose ids keep failing one rule - they all
        # hold a two-letter password - would otherwise write a line a command.
        self._id_problems_said = set()

    def handle(self, topic, payload, retain=False):
        name = self.bridge.command_name(topic)
        if name is None:
            LOG.warning("a message arrived on an unexpected topic: %s", topic)
            return False

        if retain:
            LOG.warning(
                "discarding a RETAINED cmd/%s: a retained command re-fires on every reconnect",
                name,
            )
            self.discarded_retained.add(topic)
            return False

        size = len(payload) if payload is not None else 0
        if size > MAX_PAYLOAD_BYTES:
            LOG.warning(
                "discarding cmd/%s: %d bytes, over the %d byte limit", name, size, MAX_PAYLOAD_BYTES
            )
            return False

        if name == "relay":
            # Home Assistant's answer to this receiver's own `relay_request`, not something to
            # run: it neither sets nor clears `last_error`, so that an answer nobody here asked
            # for is a line in the log and nothing more (`selfupdate.py`).
            self.relay(decode(payload))
            return True

        text, command_id, problem = unwrap(decode(payload))
        if problem:
            # Never the id itself: it is the sender's text.
            said = LOG.debug if problem in self._id_problems_said else LOG.warning
            self._id_problems_said.add(problem)
            said("cmd/%s: the command's id is dropped, %s", name, problem)

        handler = self.handlers.get(name)
        if handler is None:
            self.bridge.publish_last_error(name, "unknown command", command_id=command_id)
            return False

        return self._execute(name, handler, text, MQTT, command_id=command_id) is None

    def run(self, name, text, origin, **consents):
        """One command from somewhere other than the broker; the refusal, or None.

        The page calls this with the payload a broker client would have sent.
        The size limit is the broker's, applied the same way; the retained-
        command rule has no meaning off the broker and is not asked. An unknown
        name is refused exactly as it is over MQTT, on `last_error`.

        `consents` are what the person at the television or on the page has
        just confirmed - today only `downgrade=True`, after the question that
        names what an older release takes away. They travel down the call, as
        the origin does, and are never part of the payload: nothing a broker
        client writes can carry one, and `handle()` never passes any.

        No id either, and no envelope: the payload is the handler's as it
        stands, so a message typed on the page is never read as one.
        """
        text = str(text or "")
        if len(text.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            LOG.warning("refusing cmd/%s: over the %d byte limit", name, MAX_PAYLOAD_BYTES)
            return "the command is over the " + str(MAX_PAYLOAD_BYTES) + " byte limit"
        if name == "relay":
            # An answer to the receiver's own question, taken from the broker only; from
            # anywhere else it is nothing, and says nothing on `last_error` either.
            LOG.info("cmd/relay is taken from the broker only; ignored from %s", origin)
            return None
        handler = self.handlers.get(name)
        if handler is None:
            self.bridge.publish_last_error(name, "unknown command")
            return "unknown command"
        return self._execute(name, handler, text, origin, consents)

    def _execute(self, name, handler, text, origin, consents=None, command_id=None):
        """Run one handler, and say how it went on `last_error`. The refusal, or None.

        `command_id` is this command's for as long as its handler runs, and
        the dispatcher's again afterwards - also when the handler raises.
        """
        previous, self.command_id = self.command_id, command_id
        try:
            return self._handled(name, handler, text, origin, consents, command_id)
        finally:
            self.command_id = previous

    def _handled(self, name, handler, text, origin, consents, command_id):
        if origin == PAGE:
            LOG.info("cmd/%s from the OpenWebif page", name)
        elif origin == SCREEN:
            LOG.info("cmd/%s from the receiver's own screen", name)
        elif command_id is not None:
            LOG.info("cmd/%s (id %s)", name, command_id)
        else:
            LOG.info("cmd/%s", name)
        doors = self._doors(name)
        if doors:
            # The new release is on disk under this process (`selfupdate.py`): nothing runs -
            # but the one command that is the repair the closed doors name.
            if name in DOORS_REASONS:
                # A command that defines codes says this refusal with one as well.
                doors = Refusal(doors, DOORS_REASONS[name])
            if self.bridge.self_update.silent and name in SAID_WHILE_SILENT:
                # Someone waits on this command's answer: the refusal goes out all the same,
                # not retained, so nothing comes back that the retraction took.
                self.bridge.say_refusal_unretained(name, doors, command_id=command_id)
            elif self.bridge.self_update.silent:
                LOG.info("cmd/%s refused, not published: %s", name, doors)
            else:
                self.bridge.publish_last_error(name, doors, command_id=command_id)
            return doors
        try:
            error = handler(text, origin=origin, **(consents or {}))
        except Exception as exception:
            LOG.exception("cmd/%s raised", name)
            error = type(exception).__name__ + ": " + redact(str(exception))
            self.bridge.publish_last_error(name, error, command_id=command_id)
            return error

        if error:
            self.bridge.publish_last_error(name, error, command_id=command_id)
            return error

        self.bridge.clear_last_error()
        return None

    # ------------------------------------------------------------------ helpers --

    @property
    def session(self):
        return self.bridge.session

    def _doors(self, name=None):
        updater = getattr(self.bridge, "self_update", None)
        return updater.doors_refusal(name) if updater is not None else None

    def publisher(self, name):
        return self.bridge.publisher(name)

    def _reporter(self):
        """`report(command, sentence)` for a refusal that comes after this command returned.

        Made while the handler runs, so it carries this command's id whichever
        command is being handled when it is called.
        """
        command_id = self.command_id

        def report(command, message):
            return self.bridge.publish_last_error(command, message, command_id=command_id)

        return report

    def _expecting(self):
        """`on_zap(sref)` that starts this command's verification, under this command's id."""
        command_id = self.command_id

        def on_zap(sref):
            self._expect(sref, command_id)

        return on_zap

    def _refresh(self, *names):
        """Read the state back after changing it - the „verified by effect" half."""
        for name in names:
            publisher = self.publisher(name)
            if publisher is None:
                continue
            try:
                refresh = getattr(publisher, "refresh", None) or getattr(
                    publisher, "_publish_now", None
                )
                if refresh is not None:
                    refresh()
            except Exception:
                LOG.exception("could not re-read the %s state", name)

    # ------------------------------------------------------------------ handlers --
    # A handler returns None on success, or the reason it refused.

    def power(self, text, origin=MQTT):
        from . import power as power_module

        mode = str(text or "").strip().lower()
        if mode == "toggle":
            mode = "on" if power_module.in_standby() else "standby"
        if mode == "on":
            error = power_module.wake()
        elif mode == "standby":
            error = power_module.enter_standby()
        else:
            return "unknown power state '" + _echo(text) + "'; expected on, standby or toggle"
        if error:
            return error
        self._refresh("power")
        return None

    def _shutdown(self, command, retvalue, needs_permission, origin=MQTT):
        """Deep standby, reboot and a user-interface restart, with their guards.

        The guard is the whole of this method's reason to exist: all three end
        the process that is writing somebody's recording, and none of them can
        be taken back once the screen goes black. The permission is asked for
        this command's origin; the guards below it are asked whatever the origin.
        """
        from . import power as power_module
        from . import recording

        if needs_permission and not granted(self.bridge.value, "deep_standby_allowed", origin):
            return (
                "deep standby and reboot are switched off in the plugin's settings"
            )
        from . import epgimport

        # The image does not guard against it, and a restart mid-import loses
        # the run while the guide it was building is half written. The
        # publisher knows when the block has lapsed; without one, only the
        # importer can be asked.
        follower = self.publisher("epg_import")
        if follower.blocks_power() if follower is not None else epgimport.running():
            return "an EPG import is running"
        refusal = recording.guard(self.session)
        if refusal:
            return refusal
        ready = power_module.can_quit(self.session)
        if ready:
            return ready
        if command == "deep_standby":
            # The box is about to stop answering. Saying `offline` on the way
            # out is the difference between a consumer knowing the receiver was
            # switched off and a consumer waiting out the keepalive to guess.
            self.bridge.stop()
        return power_module.quit_mainloop(self.session, retvalue)

    def deep_standby(self, _text, origin=MQTT):
        from .power import QUIT_SHUTDOWN

        return self._shutdown("deep_standby", QUIT_SHUTDOWN, True, origin)

    def reboot(self, _text, origin=MQTT):
        from .power import QUIT_REBOOT

        return self._shutdown("reboot", QUIT_REBOOT, True, origin)

    def restart_gui(self, _text, origin=MQTT):
        from .power import QUIT_RESTART

        return self._shutdown("restart_gui", QUIT_RESTART, False, origin)

    def zap(self, text, origin=MQTT):
        from .service import zap as zap_to

        payload = parse(text)
        sref = None
        if isinstance(payload, dict):
            sref = payload.get("sref")
            if not sref and payload.get("name"):
                channels = self.publisher("channels")
                if channels is None:
                    return "this box has no channel list, so a name cannot be resolved"
                sref, error = channels.find_by_name(payload.get("name"))
                if error:
                    return error
        else:
            sref = str(text or "").strip()
        if not sref:
            return "no service reference or name given"

        return zap_to(
            self.session, sref, channels=self.publisher("channels"), on_zap=self._expecting(),
            report=self._reporter(), allowed=self._still_open,
        )

    def _expect(self, sref, command_id=None):
        """Start verifying a zap, when it has actually been made.

        The zap is verified when `service` echoes the reference. Nothing is
        published here: `evStart` will, and a refusal arrives on `last_error` if
        it does not. From standby the zap is made a turn after the wake, and the
        five seconds start then, not when the command arrived. `command_id` is
        the zap's own, kept since its command was handled.
        """
        service = self.publisher("service")
        if service is not None:
            service.expect(sref, command_id)

    def _still_open(self):
        """Whether work a command left for a later turn may still run.

        🔴 A zap that waits for the standby screen to close runs a turn or more
        after its command was accepted. A removal of the plugin accepted in
        between comes first: from acceptance on, nothing is started.
        """
        uninstaller = getattr(self.bridge, "uninstaller", None)
        if uninstaller is None:
            return True
        return not (uninstaller.underway or uninstaller.closed)

    def bouquet(self, text, origin=MQTT):
        """Switch the active channel-list context to one published bouquet."""
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return "cmd/bouquet takes a JSON object"
        if not isinstance(payload, dict) or set(payload) != {"sref"}:
            return "cmd/bouquet takes exactly one sref field"
        sref = payload.get("sref")
        if not isinstance(sref, str) or not sref:
            return "cmd/bouquet sref must be a non-empty string"
        publisher = self.publisher("bouquet_context")
        if publisher is None:
            return "active bouquet selection is unavailable on this image"
        return publisher.select(sref)

    def zap_history(self, text, origin=MQTT):
        """Zap to one channel of the receiver's own zap history.

        By reference and never by position or name: the list reorders on every
        zap, and names repeat. No permission and no recording or timeshift
        guard, because the receiver's own History Zap screen has none - it
        plays the channel without asking. Refused while a recording is played
        back, when the list the command would act on is not on screen.
        """
        from . import zaphistory
        from .power import in_standby
        from .service import cancel_waiting_zap, run_after_wake

        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return "cmd/zap_history takes a JSON object"
        if not isinstance(payload, dict) or set(payload) != {"sref"}:
            return "cmd/zap_history takes exactly one sref field"
        sref = payload.get("sref")
        if not isinstance(sref, str) or not sref.strip():
            return "cmd/zap_history sref must be a non-empty string"
        publisher = self.publisher("zap_history")
        if publisher is None or not publisher.claimed():
            return zaphistory.NOT_AVAILABLE
        if zaphistory.playing_back(self.session):
            return zaphistory.refusal(zaphistory.PLAYBACK)
        # Checked before anything happens: a channel that is not in the list
        # must not wake a sleeping receiver on the way to being refused.
        if publisher.find(sref) is None:
            return zaphistory.GONE

        # Taken now: `awake` may run turns later, during another command.
        on_zap = self._expecting()

        def awake():
            return publisher.zap_to(sref, on_zap=on_zap)

        # A zap still waiting for the wake is older than this one.
        cancel_waiting_zap()
        if in_standby():
            return run_after_wake(
                "zap_history", awake, report=self._reporter(),
                allowed=self._still_open,
            )
        return awake()

    def history_clear(self, _text, origin=MQTT):
        """Clear the receiver's zap history exactly as its 0 key does.

        🔴 The payload is ignored. Every case in which the key would not clear
        is refused first, with a reason code on `last_error`; the guards and
        the call are the publisher's (`zaphistory.py`).
        """
        from . import zaphistory

        publisher = self.publisher("zap_history")
        if publisher is None:
            return zaphistory.CLEAR_NOT_AVAILABLE
        return publisher.clear()

    def stream_stop(self, text, origin=MQTT):
        """End one stream client's stream: the one its host and its service name.

        No permission, as a zap and standby have none (ADR-0018). Both members
        and nothing else: an empty payload stops nobody, and there is no
        spelling of "all of them".
        """
        from . import stream

        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            return "cmd/stream_stop takes a JSON object"
        if not isinstance(payload, dict) or set(payload) != {"host", "sref"}:
            return "cmd/stream_stop takes exactly the host and sref fields"
        host, sref = payload["host"], payload["sref"]
        if not isinstance(host, str) or not host.strip():
            return "cmd/stream_stop host must be a non-empty string"
        if not isinstance(sref, str) or not sref.strip():
            return "cmd/stream_stop sref must be a non-empty string"
        publisher = self.publisher("stream")
        if publisher is None:
            return stream.STOP_NOT_AVAILABLE
        return publisher.stop_client(host, sref)

    def stream_playable(self, text, origin=MQTT):
        """Ask whether one service could be played now; the answer goes out as an event.

        A bare reference or `{"sref": ...}`, as `cmd/zap` takes them. The
        answer is published on `stream_playable`, not retained, with this
        command's id when it carried one (ADR-0017); a question that cannot be
        asked is a refusal on `last_error`, as for every command.
        """
        from . import stream

        payload = parse(text)
        sref = payload.get("sref") if isinstance(payload, dict) else str(text or "")
        if isinstance(payload, list) or not isinstance(sref, str) or not sref.strip():
            return "no service reference given"
        publisher = self.publisher("stream")
        if publisher is None:
            return stream.PLAYABLE_NOT_AVAILABLE
        return publisher.playable(sref.strip(), self.command_id)

    def volume(self, text, origin=MQTT):
        from . import volume as volume_module

        raw = str(text or "").strip()
        payload = parse(text)
        if isinstance(payload, dict):
            raw = str(payload.get("level", "")).strip()
        try:
            wanted = int(float(raw))
        except (TypeError, ValueError):
            return "'" + _echo(text) + "' is not a volume between 0 and 100"
        clamped = volume_module.clamp(wanted)
        if clamped != wanted:
            LOG.info("volume %s clamped to %d", wanted, clamped)
        error = volume_module.set_level(clamped)
        if error:
            return error
        self._refresh("volume")
        return None

    def mute(self, text, origin=MQTT):
        from . import volume as volume_module

        wanted = _boolean(text)
        if wanted is None:
            return "unknown mute state '" + _echo(text) + "'; expected ON or OFF"
        error = volume_module.set_muted(wanted)
        if error:
            return error
        self._refresh("volume")
        return None

    def key(self, text, origin=MQTT):
        from .remote import press

        payload = parse(text)
        long = False
        if isinstance(payload, dict):
            name = payload.get("key")
            long = bool(payload.get("long"))
        else:
            name = str(text or "").strip()
        if not name:
            return "no key given"
        return press(name, long=long)

    def message(self, text, origin=MQTT):
        from . import osd

        payload = parse(text)
        # 🔴 The style is decided before any default is filled in. The popup's
        # ten seconds applied first would give every toast without a `timeout`
        # ten seconds instead of five.
        if isinstance(payload, dict) and payload.get("style") is not None:
            style = str(payload.get("style")).strip().lower()
            if style == "toast":
                return self._toast(payload)
            if style != "popup":
                return (
                    "unknown message style '" + _echo(payload.get("style"))
                    + "'; expected popup or toast"
                )
        if isinstance(payload, dict):
            body = payload.get("text")
            kind = str(payload.get("type") or "info").strip().lower()
            timeout = payload.get("timeout", osd.DEFAULT_TIMEOUT)
        else:
            body = text
            kind = "info"
            timeout = osd.DEFAULT_TIMEOUT
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return "'" + _echo(timeout) + "' is not a number of seconds"
        return osd.show(body, kind, timeout)

    def _toast(self, payload):
        """`style: toast`. The timeout is parsed exactly as the popup's is."""
        from . import toast

        kind = str(payload.get("type") or "info").strip().lower()
        timeout = payload.get("timeout", toast.DEFAULT_TIMEOUT)
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            return "'" + _echo(timeout) + "' is not a number of seconds"
        return toast.request(self.bridge, payload.get("text"), kind, timeout)

    def choice_list(self, text, origin=MQTT):
        """A list of choices on the television; its answer is one `list_answer` (ADR-0023).

        Validated whole first, so a payload is valid or invalid whatever the receiver is doing;
        then refused without the capability and in standby. The list's id is this command's,
        from its envelope, and the answer says it back. Every refusal has a reason code.
        """
        from . import choicelist

        publisher = self.publisher(choicelist.CAPABILITY)
        if publisher is None:
            try:
                choicelist.parse_list(text)
            except choicelist.InvalidList as error:
                return Refusal(str(error), choicelist.INVALID)
            return Refusal(choicelist.NOT_AVAILABLE, choicelist.NOT_AVAILABLE_REASON)
        return publisher.request(text, self.command_id)

    def timer(self, text, origin=MQTT):
        from . import recording

        payload = parse(text)
        if not isinstance(payload, dict):
            return "cmd/timer takes a JSON object, not '" + _echo(text) + "'"
        action = str(payload.get("action") or "").strip().lower()
        sref = str(payload.get("sref") or "").strip()
        if not sref:
            return "no sref in the timer"

        if action == "add":
            if payload.get("event_id") is not None:
                error = recording.add_event_timer(self.session, sref, payload["event_id"])
            elif payload.get("begin") is not None and payload.get("end") is not None:
                error = recording.add_manual_timer(
                    self.session, sref, payload["begin"], payload["end"], payload.get("name")
                )
            else:
                return "an added timer needs an event_id, or a begin and an end"
        elif action == "delete":
            if payload.get("begin") is None or payload.get("end") is None:
                return "a deleted timer is identified by sref, begin and end"
            error = recording.delete_timer(
                self.session, sref, payload["begin"], payload["end"]
            )
        else:
            return "unknown timer action '" + _echo(action) + "'; expected add or delete"

        if error:
            return error
        self._refresh("timers", "recording")
        return None

    def record(self, text, origin=MQTT):
        from . import recording

        what = str(text or "").strip().lower()
        if what == "start":
            error = recording.start_instant_recording(self.session)
        elif what == "stop":
            error = recording.stop_instant_recording(self.session)
        else:
            return "unknown record action '" + _echo(text) + "'; expected start or stop"
        if error:
            return error
        self._refresh("recording", "timers")
        return None

    def _library(self):
        publisher = self.publisher("recordings")
        if publisher is None or not publisher.claimed():
            return None
        return publisher

    def play_recording(self, text, origin=MQTT):
        """Play one recording of the library in the image's own movie player.

        By id and nothing else: the id is checked against the library before anything
        happens, so an id the library does not list never wakes a sleeping receiver.
        From standby the receiver is woken first, as for a zap. The image's resume rule
        is the image's - it may ask on the television. Verified on `service`.
        """
        from . import library
        from .power import in_standby
        from .service import cancel_waiting_zap, run_after_wake

        publisher = self._library()
        if publisher is None:
            return library.NOT_AVAILABLE
        recording_id = library.payload_id(text)
        if recording_id is None:
            return library.NO_ID
        if publisher.find(recording_id) is None:
            return library.refusal(library.UNKNOWN)
        command_id = self.command_id

        def on_play(sref):
            service = self.publisher("service")
            if service is not None:
                service.expect(sref, command_id, command="play_recording")

        def awake():
            return publisher.play(recording_id, on_play=on_play)

        cancel_waiting_zap()
        if in_standby():
            return run_after_wake(
                "play_recording", awake, report=self._reporter(), allowed=self._still_open,
            )
        return awake()

    def delete_recording(self, text, origin=MQTT):
        """Move one recording of the library into the image's trash - and never further.

        Refused unless `recording_delete_allowed` is on, whatever the payload. The
        answer is the library read again once the file has gone (`reason` `delete`).
        """
        from . import library

        if not granted(self.bridge.value, library.PERMISSION, origin):
            return library.refusal(library.NOT_PERMITTED)
        publisher = self._library()
        if publisher is None:
            return library.NOT_AVAILABLE
        recording_id = library.payload_id(text)
        if recording_id is None:
            return library.NO_ID
        return publisher.delete(recording_id, origin=origin, command_id=self.command_id)

    def recordings_refresh(self, _text, origin=MQTT):
        """Read the library again now - the one read that may wake a disk. Payload ignored."""
        from . import library

        publisher = self.publisher("recordings")
        if publisher is None:
            return library.NOT_AVAILABLE
        return publisher.refresh_command()

    def screenshot(self, _text, origin=MQTT):
        publisher = self.publisher("screenshot")
        if publisher is None:
            return "screenshots are not available on this box"
        return publisher.capture(commanded=True)

    def softcam_restart(self, _text, origin=MQTT):
        """Collapse the cam to exactly one running instance.

        Every guard lives in the publisher, because the automatic restart uses
        the same ones and „one guard, one code path" is the point: a second copy
        of the recording check here would be a second thing to keep in step.
        🔴 The payload is ignored on purpose - nothing on the command line may
        come from the broker.
        """
        publisher = self.publisher("softcam")
        if publisher is None:
            return "this receiver's image has no softcam this plugin can restart"
        return publisher.restart(origin=origin)

    def epg_grid(self, _text, origin=MQTT):
        publisher = self.publisher("epg_grid")
        if publisher is None:
            return "the EPG grid is switched off"
        publisher.regenerate()
        return None

    def epg_import(self, _text, origin=MQTT):
        """Ask the image's EPG importer for an import now.

        🔴 The payload is ignored: nothing from the broker reaches the importer.
        The permission is asked first even where the importer is missing, so the
        refusals come in the order the contract documents them.
        """
        from . import epgimport

        publisher = self.publisher("epg_import")
        if publisher is None:
            if not granted(self.bridge.value, "epg_import_allowed", origin):
                return epgimport.PERMISSION
            return epgimport.NOT_RESOLVED
        return publisher.request(origin=origin)

    def config(self, text, origin=MQTT):
        from .config import validate_remote_settings

        payload = parse(text)
        try:
            # The bridge's own settings, because that is what it will save into.
            values = validate_remote_settings(payload, self.bridge.settings)
        except ValueError as error:
            return str(error)
        return self.bridge.apply_remote_settings(values)

    def discovery(self, _text, origin=MQTT):
        info = self.bridge.build_info()
        self.bridge.publish_announcement(info)
        self.bridge.publish_discovery(info)
        channels = self.publisher("channels")
        if channels is not None:
            # The channel list is what a select entity's options are built from,
            # so „republish discovery" without it would announce a list of
            # channels nothing had refreshed.
            channels.refresh()
        return None

    def ha_mode(self, text, origin=MQTT):
        mode = text.strip().lower()
        if mode not in HA_MODES:
            return "unknown ha_mode '" + _echo(mode) + "'; expected one of " + ", ".join(HA_MODES)
        self.bridge.set_ha_mode(mode)
        return None

    def reset(self, _text, origin=MQTT):
        self.bridge.reset_retained()
        return None

    def clear_error(self, _text, origin=MQTT):
        """Empty the retained `last_error`, whether or not there is one.

        The payload is ignored. Every command that succeeds clears
        `last_error` on its way out (`_execute`); this one does it itself, and
        sends the retraction even when this process knows of no error
        (`Bridge.retract_last_error`), so it never depends on that. No
        permission: taking a message back changes nothing on the receiver.
        """
        self.bridge.retract_last_error()
        return None

    def uninstall(self, text, origin=MQTT):
        """Remove the plugin from the receiver: validated here, done on the next turn.

        🔴 The payload is this receiver's node id and nothing else - a
        confirmation of *which* receiver was meant, not a secret. Every guard
        and the ordered teardown are in `uninstall.py`; this handler returns
        before anything changes, so the dispatcher clears `last_error` first.
        """
        return self.bridge.uninstaller.request(text, origin=origin)

    def update_check(self, _text, origin=MQTT):
        """Ask the release origin which releases there are - or answer with the last answer.

        The payload is ignored: nothing from the broker names where to look. Refused over
        MQTT unless `update_check` is on; inside ten minutes of the last check the stored
        result is the answer and nothing is fetched (`updatecheck.py`). The answer is the
        `update` topic, published when the worker has finished.
        """
        return self.bridge.updates.request_check(origin=origin)

    def update(self, text, origin=MQTT, downgrade=False):
        """Install one signed release: every guard here, the rest in the update helper.

        `{"version": "0.4.1" | "latest", "sha256": ..., "relay": {"url", "expires"}}`.
        Upgrades and repairs only - a lower version is refused over MQTT whatever the
        payload says. `downgrade` is the television's or the page's answered question
        (`run()`); the updater honours it only for those two origins. The answer is the
        `update` topic moving (`selfupdate.py`).
        """
        return self.bridge.self_update.request(text, origin=origin, downgrade=downgrade)

    def relay(self, text, origin=MQTT):
        """Home Assistant's answer to the receiver's `relay_request`: `{"id", "version", "url",
        "expires"}`.

        Taken only from the broker, and only as the answer the receiver is waiting for
        (`selfupdate.py`); anything else is logged and dropped. It is nobody's action, so the
        page has none, and an answer that came from anywhere but the broker is not taken.
        """
        if origin == MQTT:
            self.bridge.self_update.on_relay(text)
        return None
