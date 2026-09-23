"""The OpenWebif page: the bridge's status, every setting, and every command.

**This page trusts the web interface that mounted it.** OpenWebif builds one
resource tree, mounts this page on it, and wraps that tree in its own
authentication — for HTTP and again for HTTPS — deciding on the first path
segment, before this code is ever asked for anything. A client OpenWebif refuses
never reaches the page; a client it admits reaches OpenWebif's own `saveconfig`,
which sets any `config.*` key, and its settings listing, which prints every
saved value. So the page enforces no login of its own: a second copy of
OpenWebif's decision would only drift from the first, and refusing somebody the
web interface already admits protects nothing (ADR-0009).

What it does keep is what stops *another* web page from using it through a
household member's browser. Every request must name the receiver itself in
`Host` — an IP literal, `localhost`, or the box's own hostname bare or with
`.local` — which is what defeats DNS rebinding, where a hostile name is later
pointed at the receiver and is same-origin with itself. Every write must also
be same-origin, carry the session's one-shot token and exactly the form's
fields. Sessions exist without a login: OpenWebif opens one for every request
before it decides anything.

These checks protect the page, not the receiver. While OpenWebif authentication
is off, any page a browser on the LAN opens can already switch the box off
through OpenWebif's own endpoints; this page must not be the weakest link, and
it cannot be the strongest.

What the page changes, it changes through what exists. A setting goes through
`config.validate_setting`, then the path the rest of the plugin uses for it —
`apply_remote_settings` for `cmd/config`'s subset, the setup screen's
save-and-reload for everything else. A command goes through the dispatcher's
own handler with the origin `page` (`origin.py`), which answers the box-side
permission and nothing else: every household-safety guard still applies.

The page is script-free (`default-src 'none'`), so every confirmation is a
second page rendered by the server, never a dialog.
"""

import hmac
import html
import ipaddress
import json
import os
import re
import secrets
import socket
import stat
from urllib.parse import urlsplit

try:
    from twisted.web import http, resource
except ImportError:  # OpenWebif is optional; tests exercise the resource as a duck type.

    class _Resource:
        def __init__(self):
            self.children = {}

        def putChild(self, path, child):
            self.children[path] = child

    class resource:  # noqa: N801
        Resource = _Resource

    class http:  # noqa: N801
        BAD_REQUEST = 400
        FORBIDDEN = 403
        CONFLICT = 409
        INTERNAL_SERVER_ERROR = 500


from . import config as settings_module
from . import log as log_module
from .i18n import _
from .origin import PAGE
from .version import __version__

LOG = log_module.get_logger("webif")

MAX_LOG_BYTES = 65536
MAX_LOG_LINES = 200
CSRF_KEY = "mqttbridge_csrf"
CONFIRM_KEY = "mqttbridge_confirm"
# `Misdirected Request`: the request reached a server that will not answer for
# the name it asked for. Not in every Twisted's constants, so spelled here.
MISDIRECTED_REQUEST = 421
# One form field, in bytes. A thousand characters of a bouquet filter can be
# four thousand bytes of UTF-8; the setting's own limit is checked afterwards,
# in characters.
MAX_FIELD_BYTES = 4096
# A confirmation carries the change it confirms, which for a settings save can
# be several text fields at once.
MAX_CONFIRM_BYTES = 16384
# What the page shows of one retained payload. The bridge keeps more.
MAX_SHOWN_PAYLOAD = 4096
SETTINGS_FORM = "settings"
# The name a confirmed settings save goes by in the session. Not a command.
SETTINGS_ACTION = "settings"

# The setup screen's order, in the groups the page shows. A setting in none of
# them is still shown — under behaviour — and a test fails, so a new setting is
# placed on purpose rather than by accident.
SETTING_GROUPS = (
    ("identity", ("enabled", "node_id", "friendly_name")),
    ("broker", ("host", "port", "tls", "ca_file", "username", "password")),
    ("topics", ("base_topic", "ha_discovery_prefix", "ha_mode")),
    (
        "behaviour",
        (
            "publish_keys",
            "screenshot",
            "screenshot_interval",
            "screenshot_delay",
            "cam_telemetry",
            "oscam_telemetry",
            "oscam_port",
            "oscam_username",
            "oscam_password",
            "bouquets_for_select",
            "softcam_autoheal",
            "softcam_autoheal_seconds",
            "epg_grid_events",
        ),
    ),
    (
        "permissions",
        (
            "deep_standby_allowed",
            "softcam_restart_allowed",
            "cec_standby_workaround",
            "osd_toast",
        ),
    ),
    ("diagnostics", ("log_level",)),
)
SCREENSHOT_LABELS = {
    "off": "Off",
    "on_zap": "After a channel change",
    "interval": "Periodically",
}
_ASSIGNMENT_SECRET = re.compile(
    r"""(?ix)
    (?P<prefix>["']?(?:password|passwd|token|secret|username|user|broker|host)["']?
    \s*[:=]\s*)
    (?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\S+)
    """
)
_URI_USERINFO = re.compile(r"(?i)([a-z][a-z0-9+.-]*://)[^/@\s]+@")
_HOST_CHARACTERS = re.compile(r"^[A-Za-z0-9.:\[\]-]+$")


class _NotRunning(Exception):
    """An action asked of a bridge that is idle; the message says why it is."""


def _bridge():
    from .plugin import get_bridge

    return get_bridge()


def _section(bridge):
    return bridge.settings if bridge is not None else settings_module.settings


# ------------------------------------------------------------------ the gate --


def _hostname():
    """The receiver's own name, lower case, or "" when it has none."""
    try:
        return (socket.gethostname() or "").strip().lower()
    except Exception:
        return ""


def _host_name(header):
    """The host part of a `Host` header, lower case; None when it is malformed.

    An IPv6 literal must be bracketed, as the header requires, and a port must
    be digits. Nothing else is interpreted: a name is compared, never resolved.
    """
    if not isinstance(header, str) or not header or not _HOST_CHARACTERS.match(header):
        return None
    if header.startswith("["):
        end = header.find("]")
        if end < 0:
            return None
        name, rest = header[1:end], header[end + 1:]
        try:
            if ipaddress.ip_address(name).version != 6:
                return None
        except ValueError:
            return None
    else:
        # An unbracketed IPv6 address fails here too: whatever follows its
        # first colon is not a port.
        name, colon, port = header.partition(":")
        rest = colon + port
    if rest and not (rest[0] == ":" and rest[1:].isdigit() and len(rest) <= 6):
        return None
    return name.lower() or None


def _host_allowed(request):
    """True when the request names the receiver itself, and nothing else.

    🔴 This is the check that stands between the page and DNS rebinding. A
    browser sends the name it resolved, so a hostile name that later resolves to
    the receiver arrives here as that name, same-origin with itself; the
    same-origin check cannot see it and this one can. The cost is accepted and
    documented: a name of the user's own, or a reverse proxy, is refused too.
    """
    name = _host_name(request.getHeader("host"))
    if name is None:
        return False
    if name == "localhost":
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    own = _hostname()
    return bool(own) and name in (own, own + ".local")


def _suggestions(request):
    """The addresses the refused page may be opened by instead."""
    scheme = "https" if request.isSecure() else "http"
    mount = _mount_path(request)
    found = []
    try:
        address = str(request.getHost().host)
        if address.startswith("::ffff:"):
            address = address[len("::ffff:"):]
        if ipaddress.ip_address(address).version == 6:
            address = "[" + address + "]"
        found.append(scheme + "://" + address + mount)
    except Exception:
        pass
    own = _hostname()
    if own:
        found.append(scheme + "://" + own + ".local" + mount)
    return found


def _misdirected(request):
    request.setResponseCode(MISDIRECTED_REQUEST)
    _set_headers(request, "text/plain; charset=utf-8")
    places = ", ".join(_suggestions(request)) or _("the receiver's IP address")
    return (
        _("This page answers only to the receiver's own address. Open it as: %s") % places
    ).encode("utf-8")


# ------------------------------------------------------------ session tokens --


def _session(request):
    return request.getSession().sessionNamespaces


def _csrf_token(request):
    session = _session(request)
    token = session.get(CSRF_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session[CSRF_KEY] = token
    return token


def _rotate(request):
    _session(request)[CSRF_KEY] = secrets.token_urlsafe(32)


def _check_token(request):
    """The session's one-shot token, or PermissionError."""
    try:
        supplied = _single_arg(request, "csrf")
    except ValueError:
        raise PermissionError from None
    expected = _session(request).get(CSRF_KEY)
    if (
        not isinstance(expected, str)
        or len(expected) < 32
        or len(supplied) < 32
        or not hmac.compare_digest(supplied, expected)
    ):
        raise PermissionError


# ------------------------------------------------------------------ the form --


def _single_arg(request, name, limit=MAX_FIELD_BYTES):
    values = request.args.get(name.encode("ascii"))
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError("missing or repeated form field " + name)
    value = values[0]
    if not isinstance(value, bytes) or len(value) > limit:
        raise ValueError("invalid form field " + name)
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("invalid form field " + name) from None


def _bool_arg(request, name):
    """A checkbox, sent as a hidden `false` and, when ticked, a `true` after it."""
    values = request.args.get(name.encode("ascii"))
    if values not in ([b"false"], [b"false", b"true"]):
        raise ValueError("invalid checkbox field " + name)
    return values[-1].decode("ascii")


def _exact(request, names):
    """Exactly these fields and no other — no extra, no missing."""
    if set(request.args) != {name.encode("ascii") for name in names}:
        raise ValueError("unexpected or missing form field")


def _same_origin(request):
    origin = request.getHeader("origin")
    host = request.getHeader("host")
    if not origin or not host or any(char in host for char in "\r\n/@"):
        return False
    try:
        parsed = urlsplit(origin)
        origin_host = parsed.netloc.lower()
    except (TypeError, ValueError):
        return False
    expected_scheme = "https" if request.isSecure() else "http"
    return (
        parsed.scheme == expected_scheme
        and origin_host == host.lower()
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
    )


# ---------------------------------------------------------------- the log tail --


def _sanitize_log(text):
    text = log_module.redact(text)
    text = _URI_USERINFO.sub(r"\1***@", text)
    return _ASSIGNMENT_SECRET.sub(lambda match: match.group("prefix") + "***", text)


def bounded_log_tail(path=None):
    """Return a fixed-size, sanitized tail without accepting a caller path."""
    path = log_module.active_path() if path is None else path
    if not path:
        return _("Logging is unavailable.")
    try:
        metadata = os.lstat(path)
        if not stat.S_ISREG(metadata.st_mode):
            return _("Logging is unavailable.")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return _("Logging is unavailable.")
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - MAX_LOG_BYTES))
            data = handle.read(MAX_LOG_BYTES)
    except (OSError, ValueError):
        return _("Logging is unavailable.")
    text = data.decode("utf-8", "replace")
    if size > MAX_LOG_BYTES and "\n" in text:
        text = text.split("\n", 1)[1]
    return _sanitize_log("\n".join(text.splitlines()[-MAX_LOG_LINES:]))


# ---------------------------------------------------------------- the actions --


class Field:
    """One input of an action's form."""

    def __init__(self, name, kind, label, options=(), limits=None, default=""):
        self.name = name
        self.kind = kind  # text, number, select, checkbox or bouquet
        self.label = label
        self.options = tuple(options)
        self.limits = limits
        self.default = default


class Action:
    """One page action: the command it runs and how its payload is built.

    `build` turns the form's values into the payload a broker client would have
    sent. `confirm`, when it returns a sentence, puts a second page in front of
    the command that says what the household loses.
    """

    def __init__(self, key, command, label, fields=(), build=None, confirm=None,
                 ends_session=False):
        self.key = key
        self.command = command
        self.label = label
        self.fields = tuple(fields)
        self.build = build or (lambda _values: "")
        self.confirm = confirm
        self.ends_session = ends_session


def _json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _zap_payload(values):
    return _json({values["by"]: values["channel"]})


def _message_payload(values):
    return _json({
        "text": values["text"], "type": values["type"], "timeout": values["timeout"],
        "style": values["style"],
    })


def actions(bouquets=()):
    """Every page action, in the order the page shows them.

    Built per request, so every label is in the language of the moment. Every
    command in the dispatcher's table is here except `config`, whose page
    action is the settings form; a test holds the two lists to each other.
    """
    on_off = (("on", _("On")), ("off", _("Off")))
    sref = Field("sref", "text", _("Service reference"))
    begin = Field("begin", "number", _("Begins (epoch seconds)"))
    end = Field("end", "number", _("Ends (epoch seconds)"))
    return (
        Action(
            "power", "power", _("Power"),
            (Field("state", "select", _("State"), (
                ("on", _("On")), ("standby", _("Standby")), ("toggle", _("Toggle")),
            )),),
            build=lambda values: values["state"],
        ),
        Action(
            "deep_standby", "deep_standby", _("Deep standby"),
            confirm=lambda _values: _(
                "The receiver switches off completely. It records nothing and "
                "answers nothing until somebody switches it on again."
            ),
            ends_session=True,
        ),
        Action(
            "reboot", "reboot", _("Reboot"),
            confirm=lambda _values: _(
                "The receiver restarts. The picture and the connection to Home "
                "Assistant stop for a minute or two."
            ),
            ends_session=True,
        ),
        Action(
            "restart_gui", "restart_gui", _("Restart the user interface"),
            confirm=lambda _values: _(
                "The user interface restarts. The picture stops for about half a minute."
            ),
            ends_session=True,
        ),
        Action(
            "zap", "zap", _("Change channel"),
            (
                Field("by", "select", _("By"), (
                    ("name", _("Channel name")), ("sref", _("Service reference")),
                )),
                Field("channel", "text", _("Channel")),
            ),
            build=_zap_payload,
        ),
        Action(
            "bouquet", "bouquet", _("Bouquet"),
            (Field("sref", "bouquet", _("Bouquet"), bouquets),),
            build=lambda values: _json({"sref": values["sref"]}),
        ),
        Action(
            "volume", "volume", _("Volume"),
            (Field("level", "number", _("Level"), limits=(0, 100)),),
            build=lambda values: str(values["level"]),
        ),
        Action(
            "mute", "mute", _("Mute"),
            (Field("state", "select", _("State"), on_off),),
            build=lambda values: values["state"],
        ),
        Action(
            "key", "key", _("Remote key"),
            (
                Field("key", "text", _("Key name")),
                Field("long", "checkbox", _("Long press")),
            ),
            build=lambda values: _json({"key": values["key"], "long": values["long"]}),
        ),
        Action(
            "message", "message", _("Message on the television"),
            (
                Field("text", "text", _("Text")),
                Field("type", "select", _("Type"), (
                    ("info", _("Information")), ("warning", _("Warning")), ("error", _("Error")),
                )),
                Field("timeout", "number", _("Seconds on screen"), default="10"),
                # The toast is §11 g's second style, through the same handler:
                # with `osd_toast` off the handler refuses it, as over MQTT.
                Field("style", "select", _("Style"), (
                    ("popup", _("Popup")), ("toast", _("Discreet toast")),
                )),
            ),
            build=_message_payload,
        ),
        Action(
            "timer_add_event", "timer", _("Add a timer for an EPG event"),
            (sref, Field("event_id", "number", _("Event id"))),
            build=lambda values: _json(
                {"action": "add", "sref": values["sref"], "event_id": values["event_id"]}
            ),
        ),
        Action(
            "timer_add", "timer", _("Add a timer by time"),
            (sref, begin, end, Field("name", "text", _("Name"))),
            build=lambda values: _json({
                "action": "add", "sref": values["sref"], "begin": values["begin"],
                "end": values["end"], "name": values["name"],
            }),
        ),
        Action(
            "timer_delete", "timer", _("Delete a timer"),
            (sref, begin, end),
            build=lambda values: _json({
                "action": "delete", "sref": values["sref"], "begin": values["begin"],
                "end": values["end"],
            }),
            confirm=lambda _values: _(
                "The timer is deleted, and the recording it would have made will not happen."
            ),
        ),
        Action(
            "record", "record", _("Instant recording"),
            (Field("what", "select", _("Action"), (
                ("start", _("Start")), ("stop", _("Stop")),
            )),),
            build=lambda values: values["what"],
            confirm=lambda values: _("The running recording stops.")
            if values["what"] == "stop" else None,
        ),
        Action("screenshot", "screenshot", _("Take a screenshot")),
        Action(
            "softcam_restart", "softcam_restart", _("Restart the softcam"),
            confirm=lambda _values: _(
                "Encrypted channels go dark for a few seconds while the softcam restarts."
            ),
        ),
        Action("epg_grid", "epg_grid", _("Rebuild the EPG grid")),
        Action("discovery", "discovery", _("Publish discovery again")),
        Action(
            "ha_mode", "ha_mode", _("Home Assistant mode"),
            (Field("mode", "select", _("Mode"), [
                (mode, mode) for mode in settings_module.HA_MODES
            ]),),
            build=lambda values: values["mode"],
            confirm=lambda _values: _(
                "Home Assistant's view of this receiver changes: its entities are "
                "published, handed to the integration, or removed."
            ),
        ),
        Action(
            "reset", "reset", _("Reset retained topics"),
            confirm=lambda _values: _(
                "Every retained topic is emptied and published again; Home "
                "Assistant sees the receiver disappear for a moment."
            ),
        ),
    )


def _bouquets(bridge):
    """`(sref, name)` for every published bouquet, read from what is in memory."""
    channels = bridge.publisher("channels") if bridge is not None else None
    found = []
    for bouquet in getattr(channels, "bouquets", None) or []:
        reference = str(bouquet.get("sref") or "")
        if reference:
            found.append((reference, str(bouquet.get("name") or reference)))
    return found


def _field_value(request, field):
    """One action input, as the payload wants it; ValueError names the field."""
    if field.kind == "checkbox":
        return _bool_arg(request, field.name) == "true"
    text = _single_arg(request, field.name)
    if field.kind == "select":
        if text not in [value for value, _label in field.options]:
            raise ValueError(field.name + " is not one of the offered choices")
        return text
    if field.kind == "number":
        stripped = text.strip()
        if not stripped.lstrip("-").isdigit():
            raise ValueError(field.name + " must be a whole number")
        number = int(stripped)
        if field.limits is not None and not field.limits[0] <= number <= field.limits[1]:
            raise ValueError(
                field.name + " must be between "
                + str(field.limits[0]) + " and " + str(field.limits[1])
            )
        return number
    return text


# ------------------------------------------------------------------- settings --


def _setting_labels():
    try:
        from .setup import setting_labels

        return dict(setting_labels())
    except Exception:
        LOG.debug("the setup screen's labels are unavailable; showing setting names")
        return {}


def _grouped():
    """`(group, names)` in the page's order, every setting in exactly one group."""
    placed = {name for _group, names in SETTING_GROUPS for name in names}
    extra = tuple(name for name in settings_module.SETTING_NAMES if name not in placed)
    groups = []
    for group, names in SETTING_GROUPS:
        names = tuple(name for name in names if name in settings_module.SETTING_NAMES)
        if group == "behaviour":
            names += extra
        groups.append((group, names))
    return groups


def _group_label(group):
    return {
        "identity": _("Identity"),
        "broker": _("Broker"),
        "topics": _("Topics"),
        "behaviour": _("Behaviour"),
        "permissions": _("Permissions and kill-switches"),
        "diagnostics": _("Diagnostics"),
    }.get(group, group)


def _changes(request, section):
    """The settings the form changes, validated; secrets left blank are not changes."""
    changes = {}
    for name in settings_module.SETTING_NAMES:
        if settings_module.SETTING_KINDS.get(name) == "bool":
            raw = _bool_arg(request, name)
        else:
            raw = _single_arg(request, name)
        if name in settings_module.SECRET_NAMES and raw == "":
            # Write-only: the field is always rendered empty, so empty means
            # „leave it as it is". Clearing a password is the setup screen's job.
            continue
        wanted = settings_module.validate_setting(name, raw)
        if wanted != settings_module.value(name, section):
            changes[name] = wanted
    return changes


def _settings_payload(changes):
    """What a settings confirmation is bound to. A secret is named, never carried."""
    return _json({
        name: (None if name in settings_module.SECRET_NAMES else changed)
        for name, changed in changes.items()
    })


def _apply(request, bridge, changes):
    """Save through the path the rest of the plugin uses, chosen by what changed."""
    remote = set(settings_module.REMOTE_SETTING_NAMES)
    if bridge.running and set(changes) <= remote:
        raw = bridge.remote_settings()
        raw.update(changes)
        values = settings_module.validate_remote_settings(raw, bridge.settings)
        error = bridge.apply_remote_settings(values)
        done = _("Settings saved.")
    else:
        # The setup screen's path. The reconnect is what republishes `info`,
        # and on a slow broker it takes seconds: say so instead of implying the
        # change has already arrived.
        error = bridge.apply_settings(changes)
        done = _("Settings saved; the bridge is reconnecting, which can take a few seconds.")
    if error:
        raise RuntimeError(error)
    _rotate(request)
    return _answer(request, done)


# ----------------------------------------------------------- the POST handlers --


def _save_settings(request):
    bridge = _bridge()
    if bridge is None:
        raise RuntimeError("the bridge is unavailable")
    _exact(request, ("csrf", "form") + tuple(settings_module.SETTING_NAMES))
    changes = _changes(request, bridge.settings)
    if not changes:
        return _answer(request, _("Nothing to save: every setting already has that value."))
    if set(changes) & set(settings_module.IDENTITY_SETTING_NAMES):
        labels = _setting_labels()
        detail = ", ".join(labels.get(name, name) for name in changes)
        return _ask(
            request, SETTINGS_ACTION, _settings_payload(changes), _("Save settings"),
            _(
                "The node id, the base topic or the discovery prefix changes: every "
                "retained topic moves, and Home Assistant sees a new device."
            ),
            detail=detail, changes=changes,
        )
    return _apply(request, bridge, changes)


def _running_bridge():
    bridge = _bridge()
    if bridge is None:
        raise _NotRunning(_("the plugin did not start"))
    if not bridge.running:
        raise _NotRunning(bridge.idle_reason or _("the bridge is idle"))
    return bridge


def _action(key, bridge=None):
    for action in actions(_bouquets(bridge)):
        if action.key == key:
            return action
    raise ValueError("unknown action")


def _act(request):
    key = _single_arg(request, "action")
    bridge = _bridge()
    action = _action(key, bridge)
    _exact(request, ("csrf", "form", "action") + tuple(field.name for field in action.fields))
    values = {field.name: _field_value(request, field) for field in action.fields}
    bridge = _running_bridge()
    payload = action.build(values)
    sentence = action.confirm(values) if action.confirm is not None else None
    if sentence:
        return _ask(request, action.key, payload, action.label, sentence)
    return _perform(request, bridge, action, payload)


def _perform(request, bridge, action, payload):
    """Run one command through the dispatcher, as the page."""
    _rotate(request)
    if action.ends_session:
        # Rendered before the command runs: once the main loop quits, nothing
        # rendered afterwards is certain to reach the browser. A refusal still
        # replaces it, because a refused command quits nothing.
        body = _answer(
            request,
            _("Sent: %s. The receiver may stop answering this page now.") % action.label,
        )
        error = bridge.run_command(action.command, payload, PAGE)
        if error is None:
            return body
        return _answer(request, _("Refused: %s") % error)
    error = bridge.run_command(action.command, payload, PAGE)
    if error:
        return _answer(request, _("Refused: %s") % error)
    return _answer(request, _("Done: %s") % action.label)


def _ask(request, key, payload, label, sentence, detail="", changes=None):
    """The first step of a confirmed action: a fresh token bound to exactly this."""
    token = secrets.token_urlsafe(32)
    pending = {"token": token, "action": key, "payload": payload}
    if changes is not None:
        pending["changes"] = dict(changes)
    _session(request)[CONFIRM_KEY] = pending
    try:
        return _confirmation(request, label, sentence, key, payload, token, detail)
    except Exception:
        LOG.exception("the confirmation page could not be rendered")
        return _failed(request)


def _confirmed(request):
    """The second step. One-shot: the pending confirmation is gone whatever happens."""
    pending = _session(request).pop(CONFIRM_KEY, None)
    try:
        supplied = _single_arg(request, "csrf")
    except ValueError:
        raise PermissionError from None
    if (
        not isinstance(pending, dict)
        or not isinstance(pending.get("token"), str)
        or len(supplied) < 32
        or not hmac.compare_digest(supplied, pending["token"])
    ):
        raise PermissionError
    _exact(request, ("csrf", "form", "action", "payload"))
    key = _single_arg(request, "action")
    payload = _single_arg(request, "payload", MAX_CONFIRM_BYTES)
    if key != pending.get("action") or payload != pending.get("payload"):
        raise PermissionError
    if key == SETTINGS_ACTION:
        bridge = _bridge()
        if bridge is None:
            raise RuntimeError("the bridge is unavailable")
        return _apply(request, bridge, pending.get("changes") or {})
    bridge = _running_bridge()
    return _perform(request, bridge, _action(key, bridge), payload)


# ------------------------------------------------------------------ rendering --


def _set_headers(request, content_type="text/html; charset=utf-8"):
    request.setHeader("content-type", content_type)
    request.setHeader("cache-control", "no-store")
    request.setHeader("x-content-type-options", "nosniff")
    request.setHeader("x-frame-options", "SAMEORIGIN")
    request.setHeader("referrer-policy", "no-referrer")
    request.setHeader(
        "content-security-policy",
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
        "form-action 'self'; frame-ancestors 'self'",
    )


def _failed(request):
    """The last resort, for when building the page is what went wrong.

    OpenWebif would otherwise answer with its own traceback page, which is a
    stack trace from this plugin on a web page.
    """
    request.setResponseCode(http.INTERNAL_SERVER_ERROR)
    _set_headers(request, "text/plain; charset=utf-8")
    return _("The MQTT Bridge page could not be built; see the plugin log.").encode("utf-8")


def _answer(request, message="", code=None):
    """The status page, or the failure page when building it raised.

    Every entry point answers through this: a template or a setting the page
    reads can fail at any time, and a plugin is not allowed to turn that into
    OpenWebif's traceback page.
    """
    if code is not None:
        request.setResponseCode(code)
    try:
        return _page(request, message)
    except Exception:
        LOG.exception("the MQTT Bridge status page could not be rendered")
        return _failed(request)


def _mount_path(request):
    """Where this page is mounted, as the browser reached it.

    The mount point is the first element of the tuple handed to OpenWebif's
    `addExternalChild`, but it is OpenWebif that decides what to do with it, so
    the page asks the request rather than hard-coding its own name.
    """
    segments = [part for part in (getattr(request, "prepath", None) or []) if part]
    joined = "/".join(
        part.decode("utf-8", "replace") if isinstance(part, bytes) else str(part)
        for part in segments
    )
    return "/" + joined if joined else ""


def _e(value):
    return html.escape(str(value), quote=True)


def _shown(text):
    """A payload as the page may show it: redacted, and cut to a readable size."""
    text = log_module.redact(str(text))
    if len(text) > MAX_SHOWN_PAYLOAD:
        text = text[:MAX_SHOWN_PAYLOAD] + "…"
    return text


def _rows(pairs):
    return "".join(f"<dt>{_e(label)}</dt><dd>{_e(value)}</dd>" for label, value in pairs)


def _hidden(name, value):
    return f"<input type='hidden' name='{_e(name)}' value='{_e(value)}'>"


def _checkbox(name, checked):
    return (
        _hidden(name, "false")
        + f"<input type='checkbox' name='{_e(name)}' value='true'{' checked' if checked else ''}>"
    )


def _select(name, options, current):
    rendered = "".join(
        f"<option value='{_e(value)}'{' selected' if value == current else ''}>{_e(label)}</option>"
        for value, label in options
    )
    return f"<select name='{_e(name)}'>{rendered}</select>"


def _setting_control(name, current):
    kind = settings_module.SETTING_KINDS.get(name)
    if kind == "bool":
        return _checkbox(name, bool(current))
    if kind == "choice":
        choices = settings_module.CHOICES.get(name, ())
        if name == "screenshot":
            options = [(choice, _(SCREENSHOT_LABELS.get(choice, choice))) for choice in choices]
        else:
            options = [(choice, choice) for choice in choices]
        return _select(name, options, current)
    if kind == "int":
        minimum, maximum = settings_module.INTEGER_LIMITS[name]
        return (
            f"<input type='number' name='{_e(name)}' min='{minimum}' max='{maximum}' "
            f"value='{_e(current)}'>"
        )
    limit = settings_module.TEXT_LIMITS.get(name, settings_module.TEXT_LIMIT)
    if name in settings_module.SECRET_NAMES:
        # 🔴 Write-only. The stored value is never put into the page in any
        # form — not as a value, not as a placeholder, not as a length.
        return (
            f"<input type='password' name='{_e(name)}' value='' maxlength='{limit}' "
            "autocomplete='new-password'>"
        )
    return (
        f"<input type='text' name='{_e(name)}' maxlength='{limit}' value='{_e(current or '')}'>"
    )


def _settings_section(section, token):
    labels = _setting_labels()
    groups = []
    for group, names in _grouped():
        controls = "".join(
            f"<label><span>{_e(labels.get(name, name))}</span>"
            f"{_setting_control(name, settings_module.value(name, section))}</label>"
            for name in names
        )
        groups.append(f"<fieldset><legend>{_e(_group_label(group))}</legend>{controls}</fieldset>")
    hint = _("Passwords are never shown. Leave a password empty to keep the stored one.")
    return (
        f"<section><h2>{_e(_('Settings'))}</h2><p>{_e(hint)}</p>"
        "<form method='post' class='settings'>"
        + _hidden("form", SETTINGS_FORM)
        + _hidden("csrf", token)
        + "".join(groups)
        + f"<button type='submit'>{_e(_('Save settings'))}</button></form></section>"
    )


def _action_control(field):
    if field.kind == "checkbox":
        return _checkbox(field.name, False)
    if field.kind == "select" or (field.kind == "bouquet" and field.options):
        return _select(field.name, field.options, None)
    if field.kind == "number":
        limits = ""
        if field.limits is not None:
            limits = f" min='{field.limits[0]}' max='{field.limits[1]}'"
        return (
            f"<input type='number' name='{_e(field.name)}'{limits} value='{_e(field.default)}'>"
        )
    return f"<input type='text' name='{_e(field.name)}' value='{_e(field.default)}'>"


def _action_form(action, token, running):
    controls = "".join(
        f"<label><span>{_e(field.label)}</span>{_action_control(field)}</label>"
        for field in action.fields
    )
    return (
        "<form method='post' class='action'>"
        f"<fieldset{'' if running else ' disabled'}><legend>{_e(action.label)}</legend>"
        + _hidden("form", "action")
        + _hidden("action", action.key)
        + _hidden("csrf", token)
        + controls
        + f"<button type='submit'>{_e(action.label)}</button></fieldset></form>"
    )


def _actions_section(bridge, token):
    running = bridge is not None and bool(bridge.running)
    note = ""
    if not running:
        reason = getattr(bridge, "idle_reason", None) if bridge is not None else None
        why = _("Commands need a running bridge: %s") % (reason or _("the bridge is idle"))
        note = f"<p class='notice'>{_e(why)}</p>"
    forms = "".join(
        _action_form(action, token, running) for action in actions(_bouquets(bridge))
    )
    return (
        f"<section><h2>{_e(_('Commands'))}</h2>{note}"
        f"<div class='actions'>{forms}</div></section>"
    )


def _status_section(bridge, section):
    value = settings_module.value
    if bridge is None:
        state = _("Stopped")
    elif bridge.running:
        state = _("Running")
    else:
        state = _("Idle") + ": " + str(bridge.idle_reason or "")
    connected = bridge is not None and bool(bridge.connected)
    host = (value("host", section) or "").strip()
    last_error = bridge.last_error() if bridge is not None else None
    rows = (
        (_("Version"), __version__),
        (_("Bridge"), state),
        (_("MQTT"), _("Connected") if connected else _("Disconnected")),
        (_("Broker"), host + ":" + str(value("port", section)) if host else "-"),
        (_("TLS"), _("On") if value("tls", section) else _("Off")),
        (_("Node id"), value("node_id", section) or "-"),
        (_("Base topic"), value("base_topic", section) or "-"),
        (_("Home Assistant discovery prefix"), value("ha_discovery_prefix", section) or "-"),
        (_("Home Assistant mode"), value("ha_mode", section)),
        (_("Capabilities"), ", ".join(bridge.capabilities()) if bridge is not None else "-"),
        (_("Last error"), _shown(last_error) if last_error else _("None")),
        (_("Log level"), value("log_level", section)),
    )
    published = bridge.published_settings() if bridge is not None else {}
    diagnostics = bridge.diagnostics() if bridge is not None else {}
    return (
        f"<section><h2>{_e(_('Status'))}</h2><dl>{_rows(rows)}</dl>"
        f"<h3>{_e(_('Settings as published'))}</h3>"
        f"<dl>{_rows((name, _json(item)) for name, item in sorted(published.items()))}</dl>"
        f"<h3>{_e(_('Diagnostics'))}</h3>"
        f"<dl>{_rows((name, _json(item)) for name, item in sorted(diagnostics.items()))}</dl>"
        "</section>"
    )


def _topics_section(bridge):
    rows = bridge.last_payloads() if bridge is not None else []
    if not rows:
        listing = f"<p>{_e(_('Nothing has been published since the plugin started.'))}</p>"
    else:
        listing = "".join(
            f"<details><summary>{_e(topic)}</summary><pre>{_e(_shown(payload))}</pre></details>"
            if payload is not None
            else f"<p class='raw'>{_e(topic)}: {_e(_('published, not shown'))}</p>"
            for topic, payload in rows
        )
    return f"<section><h2>{_e(_('Last published payloads'))}</h2>{listing}</section>"


_STYLE = """
body{font:15px system-ui,sans-serif;margin:0;background:#f4f5f4;color:#202522}
body{letter-spacing:0;overflow-wrap:anywhere}
main{max-width:900px;margin:auto;padding:24px}
header{display:flex;align-items:center;gap:14px}header img{width:52px;height:52px}
section{border-top:1px solid #c8ceca;padding:18px 0}
dl{display:grid;grid-template-columns:220px 1fr;gap:8px}dt{color:#59615c}dd{margin:0}
form{display:grid;gap:12px;max-width:560px}
fieldset{border:1px solid #c8ceca;display:grid;gap:10px;padding:12px}
label{display:grid;grid-template-columns:1fr 220px;align-items:center;gap:12px}
input,select,button{font:inherit;padding:8px;background:#fff;color:#202522}
input,select,button{border:1px solid #89928c}
button{background:#087f5b;color:#fff;cursor:pointer}
fieldset[disabled] button{background:#89928c;cursor:not-allowed}
.actions{display:grid;gap:12px}
pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#272b29;color:#f4f5f4;padding:14px}
pre{max-height:34rem;overflow:auto}.notice{font-weight:600}
@media(max-width:520px){main{padding:16px}label{grid-template-columns:1fr}}
@media(max-width:520px){dl{grid-template-columns:120px 1fr}}
"""


def _document(request, body):
    icon = _e(_mount_path(request) + "/icon")
    document = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>MQTT Bridge</title><style>{_STYLE}</style></head><body><main>"
        f"<header><img src='{icon}' alt=''><div><h1>MQTT Bridge</h1>"
        f"<p>{_e(_('Receiver integration status'))}</p></div></header>"
        f"{body}</main></body></html>"
    )
    _set_headers(request)
    return document.encode("utf-8")


def _page(request, message=""):
    bridge = _bridge()
    section = _section(bridge)
    token = _csrf_token(request)
    notice = f"<p class='notice'>{_e(message)}</p>" if message else ""
    log_tail = _e(bounded_log_tail())
    body = (
        notice
        + _status_section(bridge, section)
        + _settings_section(section, token)
        + _actions_section(bridge, token)
        + _topics_section(bridge)
        + f"<section><h2>{_e(_('Sanitized log tail'))}</h2><pre>{log_tail}</pre></section>"
    )
    return _document(request, body)


def _confirmation(request, label, sentence, key, payload, token, detail=""):
    detail_html = f"<p>{_e(detail)}</p>" if detail else ""
    back = _e(_mount_path(request) or "/")
    body = (
        f"<section><h2>{_e(_('Confirm: %s') % label)}</h2><p class='notice'>{_e(sentence)}</p>"
        f"{detail_html}<form method='post'>"
        + _hidden("form", "confirm")
        + _hidden("csrf", token)
        + _hidden("action", key)
        + _hidden("payload", payload)
        + f"<button type='submit'>{_e(_('Yes, go ahead'))}</button></form>"
        f"<p><a href='{back}'>{_e(_('Cancel'))}</a></p></section>"
    )
    return _document(request, body)


# ------------------------------------------------------------------ resources --


class PluginIconResource(resource.Resource):
    isLeaf = True

    def render_GET(self, request):
        if not _host_allowed(request):
            return _misdirected(request)
        try:
            with open(os.path.join(os.path.dirname(__file__), "plugin.png"), "rb") as handle:
                image = handle.read(256 * 1024)
        except OSError:
            request.setResponseCode(404)
            return b""
        _set_headers(request, "image/png")
        request.setHeader("content-security-policy", "default-src 'none'; frame-ancestors 'self'")
        return image


class MQTTBridgeWebResource(resource.Resource):
    """The page. It trusts the web interface that mounted it."""

    isLeaf = False

    def __init__(self):
        resource.Resource.__init__(self)
        self.putChild(b"icon", PluginIconResource())
        # `/mqttbridge/` is the same page as `/mqttbridge`. Twisted resolves the
        # trailing slash to an empty child, and without this the receiver
        # answers 404 to a perfectly ordinary URL — measured on the box.
        self.putChild(b"", self)

    def render_GET(self, request):
        if not _host_allowed(request):
            return _misdirected(request)
        return _answer(request)

    def render_POST(self, request):
        # The Host check comes before anything reads the session, so a rebound
        # name never so much as learns whether a token exists.
        if not _host_allowed(request):
            return _misdirected(request)
        content_type = (request.getHeader("content-type") or "").split(";", 1)[0].lower()
        if content_type != "application/x-www-form-urlencoded" or not _same_origin(request):
            return _answer(request, _("Request rejected."), http.FORBIDDEN)
        try:
            form = _single_arg(request, "form")
            if form == "confirm":
                return _confirmed(request)
            _check_token(request)
            if form == SETTINGS_FORM:
                return _save_settings(request)
            if form == "action":
                return _act(request)
            raise ValueError("unknown form")
        except PermissionError:
            return _answer(request, _("Request rejected."), http.FORBIDDEN)
        except _NotRunning as error:
            return _answer(
                request, _("Commands need a running bridge: %s") % error, http.CONFLICT
            )
        except (KeyError, TypeError, ValueError) as error:
            return _answer(request, _("Nothing was changed: %s") % error, http.BAD_REQUEST)
        except Exception:
            LOG.exception("the MQTT Bridge page could not complete a request")
            return _answer(
                request, _("Nothing was changed; see the plugin log."),
                http.INTERNAL_SERVER_ERROR,
            )


def create_resource():
    return MQTTBridgeWebResource()
