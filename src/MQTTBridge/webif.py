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
be same-origin, carry the session's token and exactly the form's
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

**How the page is opened.** OpenWebif's menu entry loads it into its own
content panel with jQuery (`$("#content_container").load(url)`), which injects
whatever comes back into OpenWebif's document and runs any script in it. So a
panel load — `X-Requested-With: XMLHttpRequest`, or `Sec-Fetch-Dest: empty` for
a theme that uses `fetch()` — is answered, before the `Host` check, with a
fragment and nothing else: an
`<iframe>` of this page and a link to open it in a new tab, with no script, no
style element and no data. The frame is the full page, under its own headers,
which admit OpenWebif's origin and nobody else's. Every navigation — a tab, a
bookmark, the frame itself — gets the full page (ADR-0010).
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
import time
from urllib.parse import unquote_to_bytes, urlsplit

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
RETIRED_KEY = "mqttbridge_csrf_retired"
MAX_RETIRED_TOKENS = 8
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
# The whole POST body: every setting at its field limit, the rendered snapshot
# and the token, with room to spare. Anything larger is not this page's form.
MAX_BODY_BYTES = 262144
# What the page shows of one retained payload. The bridge keeps more.
MAX_SHOWN_PAYLOAD = 4096
SETTINGS_FORM = "settings"
# How long a grab in flight keeps the page refreshing itself. `_busy` stays set
# for as long as `grab` does not call back, so without a bound a hung grab
# would refresh the page for ever.
REFRESH_BOUND_SECONDS = 20
REFRESH_EVERY_SECONDS = 2
# The panel frame's presentation, inline because the fragment may carry no
# style element: one would restyle OpenWebif around it.
FRAME_STYLE = "display:block;width:100%;height:calc(100vh - 140px);min-height:480px;border:0"
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
            "wol_arm",
            "softcam_restart_allowed",
            "epg_import_allowed",
            "uninstall_allowed",
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
    """Replace the session's token, remembering the few it replaced.

    The retired ones are kept only to tell a tab left open in this session —
    which gets „the page is out of date" — from a token that was never this
    session's, which gets the plain refusal. None of them is ever accepted.
    """
    session = _session(request)
    retired = [item for item in session.get(RETIRED_KEY) or [] if isinstance(item, str)]
    if isinstance(session.get(CSRF_KEY), str):
        retired.append(session[CSRF_KEY])
    session[RETIRED_KEY] = retired[-MAX_RETIRED_TOKENS:]
    session[CSRF_KEY] = secrets.token_urlsafe(32)


class _OutOfDate(PermissionError):
    """A well-formed token that is no longer this session's: the page is stale."""


def _body_fields(request):
    """The POST body's fields, `{name: [value, …]}` in bytes — and nothing else.

    🔴 Twisted's `request.args` is the query string and the body merged, and its
    parser splits on `;` as well as `&`, so a token could arrive in the URL in
    spellings a query check would have to enumerate — and a URL ends up in logs,
    in history and in a `Referer`. So the page never reads `request.args`: it
    parses the body itself, as `application/x-www-form-urlencoded` is defined,
    with `&` as the only separator. A field in the query string is simply not a
    field the page can see.
    """
    cached = getattr(request, "_mqttbridge_body", None)
    if cached is not None:
        return cached
    body = b""
    content = getattr(request, "content", None)
    if content is not None:
        try:
            content.seek(0)
            body = content.read(MAX_BODY_BYTES + 1)
        except Exception:
            body = b""
    if not isinstance(body, bytes) or len(body) > MAX_BODY_BYTES:
        raise ValueError("the request body is too large")
    fields = {}
    for part in body.split(b"&"):
        if not part:
            continue
        name, _equals, value = part.partition(b"=")
        name = unquote_to_bytes(name.replace(b"+", b" "))
        value = unquote_to_bytes(value.replace(b"+", b" "))
        fields.setdefault(name, []).append(value)
    try:
        request._mqttbridge_body = fields
    except Exception:
        pass
    return fields


def _check_token(request):
    """The session's token, from the POST body, or PermissionError.

    A token of the right shape that is simply not the current one is what an
    open page sends after another tab of the same session saved and replaced it,
    so that refusal says the page is out of date rather than only „rejected".
    """
    try:
        supplied = _single_arg(request, "csrf")
    except ValueError:
        raise PermissionError from None
    expected = _session(request).get(CSRF_KEY)
    if not isinstance(expected, str) or len(expected) < 32 or len(supplied) < 32:
        raise PermissionError
    if not hmac.compare_digest(supplied, expected):
        retired = _session(request).get(RETIRED_KEY) or []
        if any(isinstance(old, str) and hmac.compare_digest(supplied, old) for old in retired):
            raise _OutOfDate
        raise PermissionError


# ------------------------------------------------------------------ the form --


def _single_arg(request, name, limit=MAX_FIELD_BYTES):
    values = _body_fields(request).get(name.encode("ascii"))
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
    values = _body_fields(request).get(name.encode("ascii"))
    if values not in ([b"false"], [b"false", b"true"]):
        raise ValueError("invalid checkbox field " + name)
    return values[-1].decode("ascii")


def _exact(request, names):
    """Exactly these fields and no other — no extra, no missing."""
    if set(_body_fields(request)) != {name.encode("ascii") for name in names}:
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


def actions(bouquets=(), node_id=""):
    """Every page action, in the order the page shows them.

    Built per request, so every label is in the language of the moment. Every
    command in the dispatcher's table is here except `config`, whose page
    action is the settings form; a test holds the two lists to each other.
    `node_id` is `uninstall`'s payload, which the page fills in: the node id is
    the confirmation a broker client has to type, and on the page the second,
    server-rendered step is that confirmation.
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
        Action(
            "epg_import", "epg_import", _("Import the EPG now"),
            confirm=lambda _values: _(
                "EPG-Importer downloads the guide now. When it finishes, the menus "
                "freeze for two or three seconds while the guide is saved."
            ),
        ),
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
        Action(
            "uninstall", "uninstall", _("Remove the plugin from this receiver"),
            build=lambda _values: node_id,
            confirm=lambda _values: _(
                "The plugin is removed from this receiver and the user interface "
                "restarts. There is no way back from here or from Home Assistant: "
                "only the receiver's own plugin menu or SSH can install it again. "
                "Its settings stay on the receiver."
            ),
            ends_session=True,
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


def _node_id(bridge):
    """The node id `uninstall` confirms, as the bridge resolves it, or "" without one."""
    return str(getattr(bridge, "node_id", "") or "") if bridge is not None else ""


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


def _rendered_values(section):
    """What the settings form shows, name to value — every setting but the secrets."""
    return {
        name: settings_module.value(name, section)
        for name in settings_module.SETTING_NAMES
        if name not in settings_module.SECRET_NAMES
    }


def _seal(token, values):
    """The rendered values as the form carries them, sealed with the session token.

    A form submits every field, touched or not. Without knowing what it was
    rendered with, a form left open while a setting changed elsewhere — over
    `cmd/config`, or a permission revoked at the television — would write its
    stale copy back as though somebody had chosen it. The seal only says this
    server produced the snapshot for this token; the token itself is what keeps
    another web site out.
    """
    encoded = _json(values)
    digest = hmac.new(token.encode("utf-8"), encoded.encode("utf-8"), "sha256").hexdigest()
    return digest + ":" + encoded


def _unseal(request):
    """The values the submitted form was rendered with, or PermissionError."""
    sealed = _single_arg(request, "rendered", MAX_CONFIRM_BYTES)
    digest, _colon, encoded = sealed.partition(":")
    token = _session(request).get(CSRF_KEY)
    if not isinstance(token, str) or not encoded:
        raise PermissionError
    expected = hmac.new(token.encode("utf-8"), encoded.encode("utf-8"), "sha256").hexdigest()
    if not hmac.compare_digest(digest, expected):
        raise PermissionError
    values = json.loads(encoded)
    if not isinstance(values, dict):
        raise ValueError("invalid form field rendered")
    return values


def _untouched(name, raw, rendered):
    """True when a submitted field still says what the form was rendered with.

    Compared after the type coercion alone — the limits are not asked, because a
    field that was not edited is not being written.
    """
    try:
        return settings_module.coerce(name, raw) == rendered
    except ValueError:
        return False


def _changes(request, section):
    """The settings the form changes, validated.

    🔴 A field counts only when its submitted value differs from the value the
    form was rendered with — an untouched field never overwrites a newer value
    set elsewhere since. Secrets are never rendered, so for them empty means
    unchanged and anything else is a change.
    """
    rendered = _unseal(request)
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
        if name in rendered and _untouched(name, raw, rendered[name]):
            # Not validated either: a value stored before today's rules — a
            # host longer than the page allows, say — must not make every
            # other save on the page fail over a field nobody touched.
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


def _uninstalling(bridge):
    """Whether `cmd/uninstall` is under way on this bridge. Anything unreadable is „no"."""
    try:
        return bool(bridge.uninstaller.underway)
    except Exception:
        return False


def _apply(request, bridge, changes):
    """Save through the path the rest of the plugin uses, chosen by what changed."""
    remote = set(settings_module.REMOTE_SETTING_NAMES)
    if _uninstalling(bridge):
        # Saved, not applied: the setup screen's rule, in the same words.
        error = bridge.defer_settings(changes)
        done = _(
            "Settings saved. The plugin is being removed from this receiver, so they "
            "are not applied now; they take effect if the removal stops or the plugin "
            "is installed again."
        )
    elif bridge.running and set(changes) <= remote:
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
    _exact(request, ("csrf", "form", "rendered") + tuple(settings_module.SETTING_NAMES))
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
    for action in actions(_bouquets(bridge), _node_id(bridge)):
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
    # 🔴 `same-origin`, never `no-referrer`. By the Fetch standard a browser
    # that POSTs a form from a document whose policy is `no-referrer` sends
    # `Origin: null` — and the same-origin check rightly refuses that, so every
    # save and every command from a real browser was a 403. `same-origin`
    # still sends nothing to another site.
    request.setHeader("referrer-policy", "same-origin")
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
        + _hidden("rendered", _seal(token, _rendered_values(section)))
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


def _capture_in_flight(bridge, now=None):
    """True while a grab started no more than the bound ago has not called back."""
    publisher = bridge.publisher("screenshot") if bridge is not None else None
    reader = getattr(publisher, "capture_state", None)
    if reader is None:
        return False
    busy, started = reader()
    if not busy or started is None:
        return False
    now = time.time() if now is None else now
    # A clock stepped back behind a hung grab is not a grab that just began.
    return 0 <= now - started <= REFRESH_BOUND_SECONDS


def _screenshot_figure(request, bridge):
    """The last picture put on `screen`, beside the action that takes one."""
    running = bridge is not None and bool(bridge.running)
    if running and settings_module.value("screenshot", bridge.settings) == "off":
        text = _("Screenshots are switched off in the settings.")
    elif not running or bridge.publisher("screenshot") is None:
        text = _("Screenshots are not available right now.")
    elif _capture_in_flight(bridge):
        text = _("Taking a screenshot; this page will refresh by itself.")
    elif bridge.last_screenshot() is None:
        text = _("No screenshot to show yet.")
    else:
        _image, taken = bridge.last_screenshot()
        source = _e(_mount_path(request) + "/screen.jpg?v=" + str(int(taken)))
        caption = _("Screenshot from %s") % time.strftime("%H:%M:%S", time.localtime(taken))
        return (
            f"<figure class='shot'><a href='{source}'><img src='{source}' alt='' width='360'>"
            f"</a><figcaption>{_e(caption)} · <a href='{source}'>{_e(_('Full size'))}</a>"
            "</figcaption></figure>"
        )
    return f"<figure class='shot'><figcaption>{_e(text)}</figcaption></figure>"


def _actions_section(request, bridge, token):
    running = bridge is not None and bool(bridge.running)
    note = ""
    if not running:
        reason = getattr(bridge, "idle_reason", None) if bridge is not None else None
        why = _("Commands need a running bridge: %s") % (reason or _("the bridge is idle"))
        note = f"<p class='notice'>{_e(why)}</p>"
    forms = "".join(
        _action_form(action, token, running)
        + (_screenshot_figure(request, bridge) if action.key == "screenshot" else "")
        for action in actions(_bouquets(bridge), _node_id(bridge))
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
pre{max-height:34rem;overflow:auto}
figure.shot{margin:0}.notice{font-weight:600}
figure.shot img{width:360px;max-width:100%;height:auto;display:block}
@media(max-width:520px){main{padding:16px}label{grid-template-columns:1fr}}
@media(max-width:520px){dl{grid-template-columns:120px 1fr}}
"""


def _document(request, body, head=""):
    icon = _e(_mount_path(request) + "/icon")
    document = (
        f"<!doctype html><html><head><meta charset='utf-8'>{head}"
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
        + _actions_section(request, bridge, token)
        + _topics_section(bridge)
        + f"<section><h2>{_e(_('Sanitized log tail'))}</h2><pre>{log_tail}</pre></section>"
    )
    head = ""
    if bridge is not None and bridge.running and _capture_in_flight(bridge):
        # „Taking…" without script: the page reloads itself by GET, which never
        # mutates anything, so a refresh can never repeat the command.
        target = _e(_mount_path(request) + "/")
        head = f"<meta http-equiv='refresh' content='{REFRESH_EVERY_SECONDS};url={target}'>"
    return _document(request, body, head)


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


class ScreenshotResource(resource.Resource):
    """`<mount>/screen.jpg` — the last picture this process put on `screen`.

    Never a fresh capture: it runs no `grab`. Whoever reaches it reaches
    OpenWebif's own `/grab`, which takes a new picture on every request, so it
    shows nobody anything new. A query string is ignored; the page adds one only
    to defeat the browser's image cache.
    """

    isLeaf = True

    def render_GET(self, request):
        if not _host_allowed(request):
            return _misdirected(request)
        bridge = _bridge()
        recorded = None
        if bridge is None or not bridge.running:
            reason = _("Screenshots are not available right now.")
        elif settings_module.value("screenshot", bridge.settings) == "off":
            reason = _("Screenshots are switched off in the settings.")
        else:
            recorded = bridge.last_screenshot()
            reason = _("No screenshot to show yet.")
        if recorded is None:
            request.setResponseCode(404)
            _set_headers(request, "text/plain; charset=utf-8")
            return reason.encode("utf-8")
        _set_headers(request, "image/jpeg")
        request.setHeader("content-security-policy", "default-src 'none'; frame-ancestors 'self'")
        return recorded[0]


def _fragment(request):
    """What OpenWebif's panel load gets: a frame of the page and a link to a tab.

    🔴 No script, no style element, no class OpenWebif styles, no token and no
    data: jQuery runs a fragment's scripts in OpenWebif's own origin, and a
    style element would restyle OpenWebif. Presentation is the frame's inline
    `style` only.
    """
    target = _e(_mount_path(request) + "/")
    request.setHeader("content-type", "text/html; charset=utf-8")
    request.setHeader("cache-control", "no-store")
    request.setHeader("x-content-type-options", "nosniff")
    return (
        f"<div><iframe src='{target}' title='MQTT Bridge' style='{FRAME_STYLE}'></iframe>"
        f"<a href='{target}' target='_blank' rel='noopener'>{_e(_('Open in a new tab'))}</a>"
        "</div>"
    ).encode()


def _panel_load(request):
    """True for OpenWebif loading the page into its panel, never for a navigation."""
    requested_with = (request.getHeader("x-requested-with") or "").strip().lower()
    destination = (request.getHeader("sec-fetch-dest") or "").strip().lower()
    return requested_with == "xmlhttprequest" or destination == "empty"


class MQTTBridgeWebResource(resource.Resource):
    """The page. It trusts the web interface that mounted it."""

    isLeaf = False

    def __init__(self):
        resource.Resource.__init__(self)
        self.putChild(b"icon", PluginIconResource())
        self.putChild(b"screen.jpg", ScreenshotResource())
        # `/mqttbridge/` is the same page as `/mqttbridge`. Twisted resolves the
        # trailing slash to an empty child, and without this the receiver
        # answers 404 to a perfectly ordinary URL — measured on the box.
        self.putChild(b"", self)

    def render_GET(self, request):
        # Fragment or page, the answer depends on these two headers, so a cache
        # must not hand one to a request that asked for the other.
        request.setHeader("vary", "X-Requested-With, Sec-Fetch-Dest")
        # 🔴 The fragment goes out before the Host check. OpenWebif's jQuery
        # `.load()` injects nothing at all on a non-2xx answer, so a 421 here
        # would be a menu entry that does nothing under a DNS name or a proxy.
        # The fragment holds no token and no data, and its mount comes from the
        # path, never from `Host`; the frame it opens is a navigation, which
        # meets the Host check below and shows the 421 inside the frame.
        if _panel_load(request):
            return _fragment(request)
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
        except _OutOfDate:
            return _answer(
                request,
                _(
                    "This page is out of date: it was changed in another tab or window "
                    "since it was opened. Reload the page and try again."
                ),
                http.FORBIDDEN,
            )
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
