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
"""

from .config import HA_MODES
from .log import get_logger, redact

LOG = get_logger("commands")

MAX_PAYLOAD_BYTES = 4096

# How much of a rejected payload is echoed back on last_error.
ERROR_ECHO_LIMIT = 64


def decode(payload):
    if payload is None:
        return ""
    if isinstance(payload, (bytes, bytearray)):
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return bytes(payload).decode("utf-8", "replace")
    return str(payload)


def _echo(text):
    """A rejected payload, safe to put on a retained topic."""
    cleaned = redact(str(text or "").strip())
    if len(cleaned) > ERROR_ECHO_LIMIT:
        cleaned = cleaned[:ERROR_ECHO_LIMIT] + "…"
    return cleaned


class CommandDispatcher:
    def __init__(self, bridge):
        self.bridge = bridge
        self.handlers = {
            "ha_mode": self.ha_mode,
            "reset": self.reset,
            "discovery": self.discovery,
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

    # ------------------------------------------------------------------ handlers --
    # A handler returns None on success, or the reason it refused.

    def ha_mode(self, text):
        mode = text.strip().lower()
        if mode not in HA_MODES:
            return "unknown ha_mode '" + _echo(mode) + "'; expected one of " + ", ".join(HA_MODES)
        self.bridge.set_ha_mode(mode)
        return None

    def reset(self, text):
        self.bridge.reset_retained()
        return None

    def discovery(self, text):
        info = self.bridge.build_info()
        self.bridge.publish_announcement(info)
        self.bridge.publish_discovery(info)
        return None
