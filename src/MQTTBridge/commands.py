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

Every handler returns `None` when it worked and a sentence when it did not, and
that sentence is written for the person who will read it on `last_error` at
eleven at night — it says what was refused and why, not which function returned
what.
"""

import json

from .config import HA_MODES
from .log import get_logger, redact

LOG = get_logger("commands")

MAX_PAYLOAD_BYTES = 4096

# How much of a rejected payload is echoed back on last_error.
ERROR_ECHO_LIMIT = 64

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

    Several commands take either — `zap` accepts a bare service reference and a
    `{"name": …}` object — so this answers with both rather than forcing every
    handler to try `json.loads` in a `try`.
    """
    stripped = str(text or "").strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except ValueError:
        return None


def _echo(text):
    """A rejected payload, safe to put on a retained topic."""
    cleaned = redact(str(text or "").strip())
    if len(cleaned) > ERROR_ECHO_LIMIT:
        cleaned = cleaned[:ERROR_ECHO_LIMIT] + "…"
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
            "volume": self.volume,
            "mute": self.mute,
            "key": self.key,
            "message": self.message,
            "timer": self.timer,
            "record": self.record,
            "screenshot": self.screenshot,
            "softcam_restart": self.softcam_restart,
            "epg_grid": self.epg_grid,
            "config": self.config,
            "discovery": self.discovery,
            "ha_mode": self.ha_mode,
            "reset": self.reset,
        }

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
            return False

        size = len(payload) if payload is not None else 0
        if size > MAX_PAYLOAD_BYTES:
            LOG.warning(
                "discarding cmd/%s: %d bytes, over the %d byte limit", name, size, MAX_PAYLOAD_BYTES
            )
            return False

        handler = self.handlers.get(name)
        if handler is None:
            self.bridge.publish_last_error(name, "unknown command")
            return False

        text = decode(payload)
        LOG.info("cmd/%s", name)
        try:
            error = handler(text)
        except Exception as exception:
            LOG.exception("cmd/%s raised", name)
            self.bridge.publish_last_error(
                name, type(exception).__name__ + ": " + redact(str(exception))
            )
            return False

        if error:
            self.bridge.publish_last_error(name, error)
            return False

        self.bridge.clear_last_error()
        return True

    # ------------------------------------------------------------------ helpers --

    @property
    def session(self):
        return self.bridge.session

    def publisher(self, name):
        return self.bridge.publisher(name)

    def _refresh(self, *names):
        """Read the state back after changing it — the „verified by effect" half."""
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

    def power(self, text):
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

    def _shutdown(self, command, retvalue, needs_permission):
        """Deep standby, reboot and a user-interface restart, with their guards.

        The guard is the whole of this method's reason to exist: all three end
        the process that is writing somebody's recording, and none of them can
        be taken back once the screen goes black.
        """
        from . import power as power_module
        from . import recording

        if needs_permission and not self.bridge.value("deep_standby_allowed"):
            return (
                "deep standby and reboot are switched off in the plugin's settings"
            )
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

    def deep_standby(self, _text):
        from .power import QUIT_SHUTDOWN

        return self._shutdown("deep_standby", QUIT_SHUTDOWN, True)

    def reboot(self, _text):
        from .power import QUIT_REBOOT

        return self._shutdown("reboot", QUIT_REBOOT, True)

    def restart_gui(self, _text):
        from .power import QUIT_RESTART

        return self._shutdown("restart_gui", QUIT_RESTART, False)

    def zap(self, text):
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

        error = zap_to(self.session, sref)
        if error:
            return error
        service = self.publisher("service")
        if service is not None:
            # The zap is verified when `service` echoes the reference. Nothing
            # is published here: `evStart` will, and a refusal arrives on
            # `last_error` if it does not.
            service.expect(sref)
        return None

    def bouquet(self, text):
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

    def volume(self, text):
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

    def mute(self, text):
        from . import volume as volume_module

        wanted = _boolean(text)
        if wanted is None:
            return "unknown mute state '" + _echo(text) + "'; expected ON or OFF"
        error = volume_module.set_muted(wanted)
        if error:
            return error
        self._refresh("volume")
        return None

    def key(self, text):
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

    def message(self, text):
        from . import osd

        payload = parse(text)
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

    def timer(self, text):
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

    def record(self, text):
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

    def screenshot(self, _text):
        publisher = self.publisher("screenshot")
        if publisher is None:
            return "screenshots are not available on this box"
        return publisher.capture(commanded=True)

    def softcam_restart(self, _text):
        """Collapse the cam to exactly one running instance.

        Every guard lives in the publisher, because the automatic restart uses
        the same ones and „one guard, one code path" is the point: a second copy
        of the recording check here would be a second thing to keep in step.
        🔴 The payload is ignored on purpose — nothing on the command line may
        come from the broker.
        """
        publisher = self.publisher("softcam")
        if publisher is None:
            return "this receiver's image has no softcam this plugin can restart"
        return publisher.restart()

    def epg_grid(self, _text):
        publisher = self.publisher("epg_grid")
        if publisher is None:
            return "the EPG grid is switched off"
        publisher.regenerate()
        return None

    def config(self, text):
        from .config import validate_remote_settings

        payload = parse(text)
        try:
            # The bridge's own settings, because that is what it will save into.
            values = validate_remote_settings(payload, self.bridge.settings)
        except ValueError as error:
            return str(error)
        return self.bridge.apply_remote_settings(values)

    def discovery(self, _text):
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

    def ha_mode(self, text):
        mode = text.strip().lower()
        if mode not in HA_MODES:
            return "unknown ha_mode '" + _echo(mode) + "'; expected one of " + ", ".join(HA_MODES)
        self.bridge.set_ha_mode(mode)
        return None

    def reset(self, _text):
        self.bridge.reset_retained()
        return None
