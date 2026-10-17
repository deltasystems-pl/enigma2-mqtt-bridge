"""The OpenWebif page: the bridge's status, every setting, and every command.

**This page trusts the web interface that mounted it.** OpenWebif builds one
resource tree, mounts this page on it, and wraps that tree in its own
authentication - for HTTP and again for HTTPS - deciding on the first path
segment, before this code is ever asked for anything. A client OpenWebif refuses
never reaches the page; a client it admits reaches OpenWebif's own `saveconfig`,
which sets any `config.*` key, and its settings listing, which prints every
saved value. So the page enforces no login of its own: a second copy of
OpenWebif's decision would only drift from the first, and refusing somebody the
web interface already admits protects nothing (ADR-0009).

What it does keep is what stops *another* web page from using it through a
household member's browser. Every request must name the receiver itself in
`Host` - an IP literal, `localhost`, or the box's own hostname bare or with
`.local` - which is what defeats DNS rebinding, where a hostile name is later
pointed at the receiver and is same-origin with itself. Every write must also
be same-origin, carry the session's token and exactly the form's
fields. Sessions exist without a login: OpenWebif opens one for every request
before it decides anything.

These checks protect the page, not the receiver. While OpenWebif authentication
is off, any page a browser on the LAN opens can already switch the box off
through OpenWebif's own endpoints; this page must not be the weakest link, and
it cannot be the strongest.

What the page changes, it changes through what exists. A setting goes through
`config.validate_setting`, then the path the rest of the plugin uses for it -
`apply_remote_settings` for `cmd/config`'s subset, the setup screen's
save-and-reload for everything else. A command goes through the dispatcher's
own handler with the origin `page` (`origin.py`), which answers the box-side
permission and nothing else: every household-safety guard still applies.

The page is script-free (`default-src 'none'`), so every confirmation is a
second page rendered by the server, never a dialog. It is bound to the session,
the action and the exact payload, is used up by its first answer, and expires
after `CONFIRM_SECONDS`.

**Behind an update's closed doors** (`selfupdate.py`) the page is one sentence,
and so is the answer to every POST: nothing is asked, saved or kept.

**Installing a release of the plugin** is `cmd/update` with the origin `page`
(ADR-0015 decision 4; SETUP.md, "Installing a release from the receiver"):
OpenWebif has admitted whoever is here, so neither `update_check` nor
`update_allowed` is asked, and every household guard still is. It is always
confirmed - it ends in a restart of the user interface - and a version below
the running one is a downgrade, whose confirmation names what it takes away.
That confirmation is the only way to one: the consent it gives is kept in the
session with the pending confirmation, never sent by the browser, so no form
another page could forge carries it. The versions offered are the ones the
signed index offers (`updateview.py`, shared with the television's screen).

**A GET can end an install's overdue wait.** An install on a receiver without internet may
wait before it starts (the relay handshake, `selfupdate.py`), and a wait past its bound is
ended by whoever looks at it, timer or not (`SelfUpdater.relay_wait`). Loading this page - or a
refresh of the television's screen - can therefore be what ends it and puts its refusal on
`last_error`: `no_relay` when no answer came, `clock_skew` when every answer that came had
already expired by this receiver's clock. That is not a GET changing the receiver: it
completes an install already confirmed, with the end its own timer gives it, only noticed
first. The "Plugin updates" section says how the last wait ended for an hour after it
(`updateview.relay_outcome`).

**How the page is opened.** OpenWebif's menu entry loads it into its own
content panel with jQuery (`$("#content_container").load(url)`), which injects
whatever comes back into OpenWebif's document and runs any script in it. So a
panel load - `X-Requested-With: XMLHttpRequest`, or `Sec-Fetch-Dest: empty` for
a theme that uses `fetch()` - is answered, before the `Host` check, with a
fragment and nothing else: an
`<iframe>` of this page and a link to open it in a new tab, with no script, no
style element and no data. The frame is the full page, under its own headers,
which admit OpenWebif's origin and nobody else's. Every navigation - a tab, a
bookmark, the frame itself - gets the full page (ADR-0010).

**The Menu subpage** (`<mount>/menu`) holds the receiver menu's settings and the
menu creator (ADR-0022). Its settings form is the main form's rules on three
fields, and works without script. The creator is the one script this plugin
serves - a static file, `creator.js`, under a CSP of its own that admits a script
from this origin and nothing else, on that page only; the main page and the
fragment stay script-free. The creator reads `catalogue.json` - the exact bytes of
the catalogue Home Assistant published - and posts the edited menu to `save`,
which holds the request until Home Assistant answers on MQTT or
`menueditor.ANSWER_SECONDS` pass. A save does not replace the session's token: the
page saves again without a reload, and the edit carries the revision it started
from, which is what keeps a stale copy from being written back. Only the main
page resource tells a panel load from a navigation; the creator's `fetch` calls
reach their own resources and are never answered with the fragment.
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
        NOT_ALLOWED = 405
        CONFLICT = 409
        INTERNAL_SERVER_ERROR = 500


try:
    from twisted.web.server import NOT_DONE_YET
except ImportError:  # Twisted's own value, for the tests.
    NOT_DONE_YET = 1

from . import buildid, menueditor, updateview
from . import config as settings_module
from . import log as log_module
from .i18n import _
from .origin import PAGE
from .selfupdate import household_doors
from .version import __version__

LOG = log_module.get_logger("webif")

MAX_LOG_BYTES = 65536
MAX_LOG_LINES = 200
CSRF_KEY = "mqttbridge_csrf"
RETIRED_KEY = "mqttbridge_csrf_retired"
MAX_RETIRED_TOKENS = 8
CONFIRM_KEY = "mqttbridge_confirm"
# How long a confirmation page may be answered. The session lives until OpenWebif's idle
# timeout, and a question left in a background tab for an evening is not a decision about
# the receiver as it is now; every guard would run again, but the question is the consent.
CONFIRM_SECONDS = 600
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

# The Menu subpage, `<mount>/menu`: its own settings form and the creator's save.
MENU_CHILD = "menu"
MENU_SETTINGS_FORM = "menu_settings"
MENU_EDIT_FORM = "menu_edit"
CREATOR_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "creator.js")
MAX_SCRIPT_BYTES = 262144
# The main page's settings form: every setting but the menu's, which are on the subpage only -
# one place to change each.
MAIN_SETTING_NAMES = tuple(
    name for name in settings_module.SETTING_NAMES
    if name not in settings_module.MENU_SETTING_NAMES
)
MENU_SETTING_NAMES = settings_module.MENU_SETTING_NAMES

# The main page's policy, byte for byte as it has always been: no script at all.
PAGE_CSP = (
    "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
    "form-action 'self'; frame-ancestors 'self'"
)
# The Menu subpage's: a script from this origin, fetches to this origin, nothing inline that
# runs, no `<base>` to move where its relative addresses point.
MENU_CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; img-src 'self'; "
    "style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'self'; base-uri 'none'"
)
# What is not a document - the catalogue, a save's answer, the script - is never framed.
DATA_CSP = "default-src 'none'; frame-ancestors 'none'"

# The setup screen's order, in the groups the page shows. A setting in none of
# them is still shown - under behaviour - and a test fails, so a new setting is
# placed on purpose rather than by accident. The menu's settings are on the Menu
# subpage instead (`MENU_SETTING_NAMES`).
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
            "update_check",
            "update_allowed",
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
MENU_HOTKEY_LABELS = {
    "help_long": "HELP, held",
    "red_long": "RED, held",
    "f3": "F3",
    "f4": "F4",
    "off": "off",
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


class _Expired(Exception):
    """A confirmation answered more than `CONFIRM_SECONDS` after it was asked."""


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

    The retired ones are kept only to tell a tab left open in this session -
    which gets „the page is out of date" - from a token that was never this
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
    """The POST body's fields, `{name: [value, ...]}` in bytes - and nothing else.

    🔴 Twisted's `request.args` is the query string and the body merged, and its
    parser splits on `;` as well as `&`, so a token could arrive in the URL in
    spellings a query check would have to enumerate - and a URL ends up in logs,
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
    """Exactly these fields and no other - no extra, no missing."""
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
        self.kind = kind  # text, number, select, checkbox, bouquet or history
        self.label = label
        self.options = tuple(options)
        self.limits = limits
        self.default = default


class Action:
    """One page action: the command it runs and how its payload is built.

    `build` turns the form's values into the payload a broker client would have
    sent. `confirm`, when it returns a sentence, puts a second page in front of
    the command that says what the household loses. `consent` names what a
    "yes" to that sentence grants the handler (`CommandDispatcher.run`); it is
    kept with the pending confirmation and passed only once it is confirmed.
    `done`, given the payload, is what the page says when the command was
    accepted, where "Done" would claim too much.
    """

    def __init__(self, key, command, label, fields=(), build=None, confirm=None,
                 ends_session=False, consent=None, done=None):
        self.key = key
        self.command = command
        self.label = label
        self.fields = tuple(fields)
        self.build = build or (lambda _values: "")
        self.confirm = confirm
        self.ends_session = ends_session
        self.consent = consent
        self.done = done


def _json(payload):
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _zap_payload(values):
    return _json({values["by"]: values["channel"]})


def _update_consent(values):
    # Granted by the downgrade question alone: `updateview.question` asks it for exactly
    # these versions.
    return {"downgrade": True} if updateview.older(values["version"]) else {}


def _update_started(payload):
    # Accepted is not started when the install waits first (the relay handshake): the page
    # says what it waits for, and its "Plugin updates" section says how the wait ended.
    waiting = updateview.relay_line(_bridge())
    if waiting:
        return waiting
    return _("The update to version %s has started.") % json.loads(payload)["version"]


def _playable_answer(_payload):
    """What the page says after `stream_playable`: the answer the command just published.

    The topic it goes out on is not retained, so the page reads it where the
    command left it.
    """
    bridge = _bridge()
    publisher = bridge.publisher("stream") if bridge is not None else None
    answer = getattr(publisher, "last_answer", None) or {}
    if answer.get("playable"):
        return _("The receiver can play that channel now.")
    return _("The receiver cannot play that channel now.")


def _message_payload(values):
    return _json({
        "text": values["text"], "type": values["type"], "timeout": values["timeout"],
        "style": values["style"],
    })


def actions(bouquets=(), node_id="", history=(), versions=()):
    """Every page action, in the order the page shows them.

    Built per request, so every label is in the language of the moment. Every
    command in the dispatcher's table is here except `config`, whose page
    action is the settings form; a test holds the two lists to each other.
    `node_id` is `uninstall`'s payload, which the page fills in: the node id is
    the confirmation a broker client has to type, and on the page the second,
    server-rendered step is that confirmation. `history` is the last published
    `zap_history`, as `(sref, name)`: the page offers what the topic said, by
    reference, as a consumer would. `versions` is `(version, label)` of every
    release the signed index offers that can be installed (`updateview`).
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
            "zap_history", "zap_history", _("Zap to a recently watched channel"),
            (Field("sref", "history", _("Channel"), history),),
            build=lambda values: _json({"sref": values["sref"]}),
        ),
        Action(
            "history_clear", "history_clear", _("Clear the recently watched list"),
            build=lambda _values: "PRESS",
            confirm=lambda _values: _(
                "The receiver switches to channel 1, the first channel of its first "
                "bouquet, and empties its list of recently watched channels."
            ),
        ),
        Action(
            "stream_stop", "stream_stop", _("Stop a stream client"),
            (Field("host", "text", _("Client address")), sref),
            build=lambda values: _json({"host": values["host"], "sref": values["sref"]}),
            confirm=lambda _values: _(
                "The stream to that client stops. A player that reconnects by itself "
                "gets a new one."
            ),
        ),
        Action(
            "stream_playable", "stream_playable", _("Ask whether a channel can be played now"),
            (sref,),
            build=lambda values: values["sref"],
            done=_playable_answer,
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
                # The toast is the second style (ADR-0008), through the same handler:
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
        # Needs no `update_check`: asking from the page is the consent to one request
        # to the release origin. The ten-minute limit applies here as over MQTT.
        Action("update_check", "update_check", _("Check for plugin updates now")),
        Action(
            "update", "update", _("Install a plugin version"),
            (Field("version", "version", _("Version"), versions),),
            build=lambda values: _json({"version": values["version"]}),
            confirm=lambda values: updateview.question(values["version"]),
            consent=_update_consent,
            done=_update_started,
        ),
        Action("discovery", "discovery", _("Publish discovery again")),
        Action("clear_error", "clear_error", _("Clear last error")),
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


def _history(bridge):
    """`(sref, name)` for every entry of the last published `zap_history`, newest first."""
    publisher = bridge.publisher("zap_history") if bridge is not None else None
    payload = getattr(publisher, "published", None)
    found = []
    for entry in (payload or {}).get("entries") or []:
        reference = str(entry.get("sref") or "")
        if reference:
            found.append((reference, str(entry.get("name") or reference)))
    return found


def _node_id(bridge):
    """The node id `uninstall` confirms, as the bridge resolves it, or "" without one."""
    return str(getattr(bridge, "node_id", "") or "") if bridge is not None else ""


def _field_value(request, field):
    """One action input, as the payload wants it; ValueError names the field."""
    if field.kind == "checkbox":
        return _bool_arg(request, field.name) == "true"
    text = _single_arg(request, field.name)
    if field.kind in ("select", "version"):
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
    extra = tuple(name for name in MAIN_SETTING_NAMES if name not in placed)
    groups = []
    for group, names in SETTING_GROUPS:
        names = tuple(name for name in names if name in MAIN_SETTING_NAMES)
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


def _rendered_values(section, names=MAIN_SETTING_NAMES):
    """What a settings form shows, name to value - every setting of `names` but the secrets."""
    return {
        name: settings_module.value(name, section)
        for name in names
        if name not in settings_module.SECRET_NAMES
    }


def _seal(token, values):
    """The rendered values as the form carries them, sealed with the session token.

    A form submits every field, touched or not. Without knowing what it was
    rendered with, a form left open while a setting changed elsewhere - over
    `cmd/config`, or a permission revoked at the television - would write its
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

    Compared after the type coercion alone - the limits are not asked, because a
    field that was not edited is not being written.
    """
    try:
        return settings_module.coerce(name, raw) == rendered
    except ValueError:
        return False


def _changes(request, section, names=MAIN_SETTING_NAMES):
    """The settings the form changes, validated - the form of `names`.

    🔴 A field counts only when its submitted value differs from the value the
    form was rendered with - an untouched field never overwrites a newer value
    set elsewhere since. Secrets are never rendered, so for them empty means
    unchanged and anything else is a change.
    """
    rendered = _unseal(request)
    changes = {}
    for name in names:
        if settings_module.SETTING_KINDS.get(name) == "bool":
            raw = _bool_arg(request, name)
        else:
            raw = _single_arg(request, name)
        if name in settings_module.SECRET_NAMES and raw == "":
            # Write-only: the field is always rendered empty, so empty means
            # „leave it as it is". Clearing a password is the setup screen's job.
            continue
        if name in rendered and _untouched(name, raw, rendered[name]):
            # Not validated either: a value stored before today's rules - a
            # host longer than the page allows, say - must not make every
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


def _apply(request, bridge, changes, answer=None):
    """Save through the path the rest of the plugin uses, chosen by what changed.

    `answer(request, message)` renders the page the form was on: the main page by default.
    """
    answer = answer or _answer
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
        # `cmd/config`'s own validation, of exactly what changed: every key it leaves out keeps
        # the value it has.
        values = settings_module.validate_remote_settings(changes, bridge.settings)
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
    return answer(request, done)


# ----------------------------------------------------------- the POST handlers --


def _save_settings(request):
    bridge = _bridge()
    if bridge is None:
        raise RuntimeError("the bridge is unavailable")
    _exact(request, ("csrf", "form", "rendered") + MAIN_SETTING_NAMES)
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


def _versions(bridge):
    """`(version, label)` of every release the page may offer to install."""
    return updateview.installable(bridge) if bridge is not None else []


def _action(key, bridge=None):
    for action in actions(_bouquets(bridge), _node_id(bridge), _history(bridge),
                          _versions(bridge)):
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
        consents = action.consent(values) if action.consent is not None else None
        return _ask(request, action.key, payload, action.label, sentence, consents=consents)
    return _perform(request, bridge, action, payload)


def _perform(request, bridge, action, payload, consents=None):
    """Run one command through the dispatcher, as the page.

    `consents` come only from a confirmation this session was shown (`_confirmed`).
    """
    consents = dict(consents or {})
    _rotate(request)
    if action.ends_session:
        # Rendered before the command runs: once the main loop quits, nothing
        # rendered afterwards is certain to reach the browser. A refusal still
        # replaces it, because a refused command quits nothing.
        body = _answer(
            request,
            _("Sent: %s. The receiver may stop answering this page now.") % action.label,
        )
        error = bridge.run_command(action.command, payload, PAGE, **consents)
        if error is None:
            return body
        return _answer(request, _("Refused: %s") % error)
    error = bridge.run_command(action.command, payload, PAGE, **consents)
    if error:
        return _answer(request, _("Refused: %s") % error)
    if action.done is not None:
        return _answer(request, action.done(payload))
    return _answer(request, _("Done: %s") % action.label)


def _ask(request, key, payload, label, sentence, detail="", changes=None, consents=None):
    """The first step of a confirmed action: a fresh token bound to exactly this.

    `consents` stay here, on the server, with the pending confirmation: the browser
    sends back the token, the action and the payload, and nothing else it sends can
    add one.
    """
    token = secrets.token_urlsafe(32)
    pending = {"token": token, "action": key, "payload": payload, "asked": time.monotonic()}
    if changes is not None:
        pending["changes"] = dict(changes)
    if consents:
        pending["consents"] = dict(consents)
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
    asked = pending.get("asked")
    if not isinstance(asked, float) or not 0 <= time.monotonic() - asked <= CONFIRM_SECONDS:
        raise _Expired
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
    return _perform(request, bridge, _action(key, bridge), payload, pending.get("consents"))


# ------------------------------------------------------------------ rendering --


def _set_headers(request, content_type="text/html; charset=utf-8", csp=PAGE_CSP):
    request.setHeader("content-type", content_type)
    request.setHeader("cache-control", "no-store")
    request.setHeader("x-content-type-options", "nosniff")
    request.setHeader("x-frame-options", "SAMEORIGIN")
    # 🔴 `same-origin`, never `no-referrer`. By the Fetch standard a browser
    # that POSTs a form from a document whose policy is `no-referrer` sends
    # `Origin: null` - and the same-origin check rightly refuses that, so every
    # save and every command from a real browser was a 403. `same-origin`
    # still sends nothing to another site.
    request.setHeader("referrer-policy", "same-origin")
    request.setHeader("content-security-policy", csp)


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


def _segments(request):
    return [
        part.decode("utf-8", "replace") if isinstance(part, bytes) else str(part)
        for part in (getattr(request, "prepath", None) or [])
        if part
    ]


def _mount_path(request):
    """Where this page is mounted, as the browser reached it.

    The mount point is the first element of the tuple handed to OpenWebif's
    `addExternalChild`, but it is OpenWebif that decides what to do with it, so
    the page asks the request rather than hard-coding its own name.
    """
    joined = "/".join(_segments(request))
    return "/" + joined if joined else ""


def _main_mount(request):
    """The main page's path, from a request that reached the Menu subpage or one of its files.

    Everything before the last `menu` segment: the subpage is that child of the main page.
    """
    segments = _segments(request)
    if MENU_CHILD in segments:
        segments = segments[:len(segments) - 1 - segments[::-1].index(MENU_CHILD)]
    return "/" + "/".join(segments) if segments else ""


def _e(value):
    return html.escape(str(value), quote=True)


def _shown(text):
    """A payload as the page may show it: redacted, and cut to a readable size."""
    text = log_module.redact(str(text))
    if len(text) > MAX_SHOWN_PAYLOAD:
        text = text[:MAX_SHOWN_PAYLOAD] + "\u2026"
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
        elif name == "menu_hotkey":
            options = [(choice, _(MENU_HOTKEY_LABELS.get(choice, choice))) for choice in choices]
        else:
            options = [(choice, choice) for choice in choices]
        return _select(name, options, current)
    if name == "menu_transparency":
        # The television's nine steps, as a list: a number field would offer the values between.
        options = [
            (str(percent), f"{percent} %")
            for percent in settings_module.MENU_TRANSPARENCY_CHOICES
        ]
        return _select(name, options, str(current))
    if kind == "int":
        minimum, maximum = settings_module.INTEGER_LIMITS[name]
        return (
            f"<input type='number' name='{_e(name)}' min='{minimum}' max='{maximum}' "
            f"value='{_e(current)}'>"
        )
    limit = settings_module.TEXT_LIMITS.get(name, settings_module.TEXT_LIMIT)
    if name in settings_module.SECRET_NAMES:
        # 🔴 Write-only. The stored value is never put into the page in any
        # form - not as a value, not as a placeholder, not as a length.
        return (
            f"<input type='password' name='{_e(name)}' value='' maxlength='{limit}' "
            "autocomplete='new-password'>"
        )
    return (
        f"<input type='text' name='{_e(name)}' maxlength='{limit}' value='{_e(current or '')}'>"
    )


def _menu_summary(section):
    """The receiver menu's three settings in one sentence, as the main page shows them."""
    allowed = settings_module.value("menu_allowed", section)
    hotkey = settings_module.value("menu_hotkey", section)
    return _("Home Assistant menu: %s. Key: %s. Background transparency: %s %%.") % (
        _("On") if allowed else _("Off"),
        _(MENU_HOTKEY_LABELS.get(hotkey, str(hotkey))),
        settings_module.value("menu_transparency", section),
    )


def _menu_line(section, mount):
    """The main page's word on the menu: what it is set to, and where it is changed."""
    target = _e(mount + "/" + MENU_CHILD)
    return (
        f"<p class='menu-line'>{_e(_menu_summary(section))} "
        f"<a href='{target}'>{_e(_('Menu settings and the menu creator'))}</a></p>"
    )


def _settings_section(section, token, mount=""):
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
        + _menu_line(section, mount)
        + "<form method='post' class='settings'>"
        + _hidden("form", SETTINGS_FORM)
        + _hidden("csrf", token)
        + _hidden("rendered", _seal(token, _rendered_values(section)))
        + "".join(groups)
        + f"<button type='submit'>{_e(_('Save settings'))}</button></form></section>"
    )


def _action_control(field):
    if field.kind == "checkbox":
        return _checkbox(field.name, False)
    if field.kind in ("select", "version") or (
        field.kind in ("bouquet", "history") and field.options
    ):
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
    # Nothing to choose from - no signed list of versions yet - is nothing to send.
    if any(field.kind == "version" and not field.options for field in action.fields):
        running = False
        controls += "<p>" + _e(_("No list of versions is known yet. Check for updates first.")) \
            + "</p>"
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
        for action in actions(_bouquets(bridge), _node_id(bridge), _history(bridge),
                              _versions(bridge))
    )
    return (
        f"<section><h2>{_e(_('Commands'))}</h2>{note}"
        f"<div class='actions'>{forms}</div></section>"
    )


def _build(bridge):
    """The running build's id: the bridge's, or this process's when there is no bridge."""
    return getattr(bridge, "build", buildid.LOADED) if bridge is not None else buildid.LOADED


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
        (_("Version"), buildid.display_version(__version__, _build(bridge))),
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


def _updates_section(bridge):
    """What the television's "Plugin updates" screen shows, for the page's readers."""
    lines = updateview.header(bridge)
    running = updateview.transaction_line(bridge)
    if running:
        lines.append(running)
    # An install that waits, or how the last wait ended without an update (the relay handshake).
    waited = updateview.relay_line(bridge) or updateview.relay_outcome(bridge)
    if waited:
        lines.append(waited)
    rows = updateview.rows(bridge) if bridge is not None else []
    # The page wraps, so each version that cannot be installed is listed with its reason.
    listing = "".join("<li>" + _e(updateview.row_detail(row)) + "</li>" for row in rows)
    return (
        "<section><h2>" + _e(_("Plugin updates")) + "</h2>"
        + "".join("<p>" + _e(line) + "</p>" for line in lines)
        + ("<ul>" + listing + "</ul>" if listing else "")
        + "</section>"
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


def _document(request, body, head="", mount=None, csp=PAGE_CSP, subtitle=None):
    """A whole page: `mount` is the main page's path, which the icon is under."""
    mount = _mount_path(request) if mount is None else mount
    icon = _e(mount + "/icon")
    subtitle = _("Receiver integration status") if subtitle is None else subtitle
    document = (
        f"<!doctype html><html><head><meta charset='utf-8'>{head}"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>MQTT Bridge</title><style>{_STYLE}</style></head><body><main>"
        f"<header><img src='{icon}' alt=''><div><h1>MQTT Bridge</h1>"
        f"<p>{_e(subtitle)}</p></div></header>"
        f"{body}</main></body></html>"
    )
    _set_headers(request, csp=csp)
    return document.encode("utf-8")


def _updating(bridge):
    """Whether an update has closed the plugin's doors. Anything unreadable is "no"."""
    try:
        return bool(bridge.self_update.closed)
    except Exception:
        return False


def _doors_closed():
    try:
        return _updating(_bridge())
    except Exception:
        LOG.exception("the plugin could not be asked whether an update runs")
        return False


def _repair_posted(request):
    """Whether this POST is the repair closed doors let through (`SelfUpdater.repair`).

    Its action form, or the answer to the confirmation that action asked. Only the repair: every
    other form behind the doors gets the sentence alone (`render_POST`). Anything that cannot be
    read is "no". The token, the confirmation and the command's own guards are all still asked
    after this - it only decides that the doors do not answer first.
    """
    try:
        key = _bridge().self_update.repair()
        if key is None:
            return False
        form = _single_arg(request, "form")
        if form == "action":
            return _single_arg(request, "action") == key
        if form == "confirm":
            pending = _session(request).get(CONFIRM_KEY)
            return isinstance(pending, dict) and pending.get("action") == key
    except Exception:
        return False
    return False


def _repair_form(request, bridge):
    """The form of the command closed doors let through (`SelfUpdater.repair`), or nothing."""
    try:
        key = bridge.self_update.repair()
        if key is None:
            return ""
        return _action_form(_action(key, bridge), _csrf_token(request), bool(bridge.running))
    except Exception:
        LOG.exception("the repair form could not be rendered")
        return ""


def _page(request, message=""):
    bridge = _bridge()
    if bridge is not None and _updating(bridge):
        # The new release is on disk under this process: nothing but the sentence, and no
        # form that could post a change into it (`selfupdate.py`) - except the one command
        # that is the repair the sentence names, when it names one. An answer to a command
        # posted here (the doors' own refusal, or the repair's) is said above it.
        notice = f"<p class='notice'>{_e(message)}</p>" if message else ""
        return _document(request,
                         notice
                         + f"<p class='notice'>{_e(household_doors(bridge.self_update))}</p>"
                         + _repair_form(request, bridge))
    section = _section(bridge)
    token = _csrf_token(request)
    notice = f"<p class='notice'>{_e(message)}</p>" if message else ""
    log_tail = _e(bounded_log_tail())
    body = (
        notice
        + _status_section(bridge, section)
        + _settings_section(section, token, _mount_path(request))
        + _updates_section(bridge)
        + _actions_section(request, bridge, token)
        + _topics_section(bridge)
        + f"<section><h2>{_e(_('Sanitized log tail'))}</h2><pre>{log_tail}</pre></section>"
    )
    head = ""
    if bridge is not None and bridge.running and _capture_in_flight(bridge):
        # „Taking..." without script: the page reloads itself by GET, which never
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


# ------------------------------------------------------------- the Menu subpage --


def _menu_publisher(bridge):
    """The receiver menu's publisher - running only while `menu_allowed` is on - or None."""
    try:
        return bridge.publisher("menu") if bridge is not None and bridge.running else None
    except Exception:
        return None


def _age(seconds):
    """A duration as the status line says it: seconds, minutes or hours."""
    seconds = max(0, int(seconds))
    if seconds < 120:
        return _("%d s") % seconds
    if seconds < 7200:
        return _("%d min") % (seconds // 60)
    return _("%d h") % (seconds // 3600)


def _catalogue_text(publisher, now=None):
    if publisher is None:
        return "-"
    status = publisher.editor.status()
    if status["editing"] is False:
        return _("editing from the receiver is switched off in Home Assistant")
    if status["revision"] is None:
        if status["reason"] == menueditor.NEWER_CATALOGUE:
            return _("from a newer Home Assistant integration than this plugin understands")
        return _("none received")
    now = time.time() if now is None else now
    return _("%s entities, received %s ago") % (status["entities"], _age(now - status["received"]))


def _home_assistant_text(publisher):
    status = getattr(publisher, "ha_status", None) if publisher is not None else None
    if status == "online":
        return _("online")
    if status == "offline":
        return _("offline")
    return _("not heard from since the connection to the broker")


def _menu_status_section(bridge, section, publisher):
    connected = bridge is not None and bool(bridge.connected)
    status = publisher.editor.status() if publisher is not None else {}
    left_out = status.get("left_out")
    rows = (
        (_("Home Assistant menu"),
         _("On") if settings_module.value("menu_allowed", section) else _("Off")),
        (_("MQTT"), _("Connected") if connected else _("Disconnected")),
        (_("Home Assistant"), _home_assistant_text(publisher) if connected else "-"),
        (_("Catalogue"), _catalogue_text(publisher)),
        (_("Entities left out of the catalogue"), "-" if left_out is None else str(left_out)),
    )
    return f"<section><h2>{_e(_('Status'))}</h2><dl>{_rows(rows)}</dl></section>"


def _menu_settings_section(section, token):
    labels = _setting_labels()
    controls = "".join(
        f"<label><span>{_e(labels.get(name, name))}</span>"
        f"{_setting_control(name, settings_module.value(name, section))}</label>"
        for name in MENU_SETTING_NAMES
    )
    hint = _("The background transparency applies the next time the menu is opened.")
    return (
        f"<section><h2>{_e(_('Menu settings'))}</h2><p>{_e(hint)}</p>"
        "<form method='post' class='settings'>"
        + _hidden("form", MENU_SETTINGS_FORM)
        + _hidden("csrf", token)
        + _hidden("rendered", _seal(token, _rendered_values(section, MENU_SETTING_NAMES)))
        + f"<fieldset><legend>{_e(_('Menu'))}</legend>{controls}</fieldset>"
        + f"<button type='submit'>{_e(_('Save settings'))}</button></form></section>"
    )


def creator_texts():
    """Every sentence the menu creator shows, in the household's language, by key."""
    return {
        "loading": _("Loading the catalogue from Home Assistant..."),
        "none": _("Home Assistant has not sent a catalogue for this receiver yet. Is the "
                  "companion integration set up for it, in a version with the menu creator?"),
        "newer": _("The catalogue comes from a newer Home Assistant integration than this "
                   "plugin understands. Update the plugin."),
        "menu_off": _("The menu is switched off on this receiver."),
        "editing_off": _("Editing the menu from the receiver is switched off in Home Assistant: "
                         "the companion integration's options, under the receiver menu, can "
                         "switch it on."),
        "not_running": _("The plugin is not running."),
        "updating": _("The plugin is being updated."),
        "load_failed": _("The catalogue could not be loaded. Reload the page."),
        "left_out": _("Home Assistant left %s entities out of the catalogue: it was too large. "
                      "They cannot be chosen here."),
        "top": _("Top level"),
        "add_group": _("Add a group"),
        "add_row": _("Add a row"),
        "new_group": _("New group"),
        "group": _("Group"),
        "label": _("Label"),
        "icon": _("Icon"),
        "default": _("Default"),
        "entity": _("Entity"),
        "search": _("Search by name, entity id or area"),
        "no_matches": _("No entity matches."),
        "control": _("Control"),
        "place": _("Place"),
        "up": _("Up"),
        "down": _("Down"),
        "remove": _("Remove"),
        "save": _("Save the menu"),
        "reload": _("Reload"),
        "saving": _("Saving..."),
        "unsaved": _("There are changes that are not saved."),
        "empty_menu": _("The menu is empty. Add a group or a row."),
        "empty_group": _("No rows in this group yet."),
        "missing": _("Missing in Home Assistant: %s"),
        "not_listed": _("Not in the catalogue: %s"),
        "hidden": _("hidden"),
        "config": _("configuration"),
        "diagnostic": _("diagnostic"),
        "choose_entity": _("Choose an entity for every row before saving."),
        "name_group": _("Give every group a label before saving."),
        "limit_groups": _("The menu holds at most %s groups."),
        "limit_top": _("The top level holds at most %s entries."),
        "limit_rows": _("A group holds at most %s rows."),
        "control_toggle": _("Toggle"),
        "control_slider": _("Slider"),
        "control_indicator": _("Indicator"),
        "control_button": _("Button"),
        "control_selector": _("Selector"),
        "control_input": _("Input field"),
        "ha_says": _("Home Assistant says:"),
        "where": _("Where:"),
        "confirm_reload": _("Your unsaved changes will be lost."),
        # Who answered is said first: Home Assistant's answers, then the receiver's own.
        "result_saved": _("Saved. Home Assistant stored the menu."),
        "result_waiting": _("Saved. Loading the menu as Home Assistant stored it..."),
        "result_arriving": _("Saved; Home Assistant's copy has not arrived yet."),
        "result_stale": _("Home Assistant refused the save: the menu was changed elsewhere since "
                          "this page loaded it. Reload to see it - your changes here stay on the "
                          "page until you do."),
        "result_invalid": _("Home Assistant refused the menu."),
        "result_busy": _("Home Assistant is busy with another save. Try again in a moment."),
        "result_not_claimed": _("Home Assistant does not see this receiver offering the menu "
                                "creator yet. Try again in a moment."),
        "result_disabled": _("Home Assistant refused the save: editing the menu from the "
                             "receiver is switched off in its integration options."),
        "result_receiver_busy": _("The receiver is already waiting for Home Assistant to answer "
                                  "other saves. Try again in a moment."),
        "result_no_answer": _("The receiver got no answer from Home Assistant within ten "
                              "seconds. The menu may still have been saved: reload to see."),
        "result_not_connected": _("The receiver has no connection to the MQTT broker. Nothing "
                                  "was saved."),
        "result_not_allowed": _("The receiver does not offer the menu creator now."),
        "result_unreadable": _("The receiver could not read what this page sent. Reload the "
                               "page."),
        "result_too_big": _("The receiver refused the menu: it is too large to send."),
        "result_rejected": _("The receiver refused the request. Reload the page."),
        "result_out_of_date": _("The receiver refused the save: this page is out of date - it "
                                "was changed in another tab or window since it was opened. "
                                "Reload the page and try again."),
        "result_failed": _("The receiver could not complete the save; see the plugin log."),
        "result_unknown": _("Home Assistant answered: %s"),
    }


def _creator_section(request, bridge, section, publisher, token):
    if not settings_module.value("menu_allowed", section):
        text = _("The menu is switched off on this receiver: switch it on above to build the "
                 "menu here.")
        return f"<section><h2>{_e(_('Menu creator'))}</h2><p>{_e(text)}</p></section>"
    if publisher is None:
        text = _("The menu creator needs the plugin running with the menu on.")
        return f"<section><h2>{_e(_('Menu creator'))}</h2><p>{_e(text)}</p></section>"
    base = _main_mount(request) + "/" + MENU_CHILD
    from .menu import ICON_NAMES, LABEL_LIMIT, MAX_GROUPS, MAX_ITEMS, MAX_ROWS

    attributes = {
        "data-catalogue": base + "/catalogue.json",
        "data-save": base + "/save",
        "data-iconbase": base + "/icon/",
        "data-csrf": token,
        "data-icons": _json(list(ICON_NAMES)),
        "data-limits": _json({"groups": MAX_GROUPS, "top": MAX_ITEMS, "rows": MAX_ROWS,
                              "text": LABEL_LIMIT}),
        "data-texts": _json(creator_texts()),
    }
    rendered = "".join(f" {name}='{_e(value)}'" for name, value in attributes.items())
    hint = _("Home Assistant checks every save with the rules of its own menu options and "
             "stores the menu; this page edits a copy.")
    noscript = _("The menu creator needs JavaScript. The settings above work without it.")
    return (
        f"<section><h2>{_e(_('Menu creator'))}</h2><p>{_e(hint)}</p>"
        f"<div id='creator'{rendered}></div>"
        f"<noscript><p class='notice'>{_e(noscript)}</p></noscript>"
        f"<script src='{_e(base + '/creator.js')}'></script></section>"
    )


_MENU_STYLE = (
    ".creator .item{border:1px solid #c8ceca;padding:10px;margin:8px 0;display:grid;gap:8px}"
    ".creator .group{background:#eef1ef}.creator .row{background:#fff}"
    ".creator .tools{display:flex;flex-wrap:wrap;gap:6px}"
    ".creator .tools button{padding:4px 8px}"
    ".creator .pick{display:grid;gap:4px;max-height:16rem;overflow:auto}"
    ".creator .pick button{text-align:left;background:#fff;color:#202522}"
    ".creator .pick button.active{outline:2px solid #087f5b}"
    ".creator .meta{color:#59615c;font-size:13px}.creator .bad{color:#a4161a;font-weight:600}"
    ".creator .bar{position:sticky;bottom:0;background:#f4f5f4;padding:8px 0;"
    "display:flex;gap:8px;flex-wrap:wrap;align-items:center}"
    ".creator label{grid-template-columns:140px 1fr}"
    ".creator .icon{display:flex;gap:8px;align-items:center}"
    ".creator .icon img{width:32px;height:32px;background:#3a4a56;border-radius:4px}"
)


def _menu_page(request, message=""):
    bridge = _bridge()
    mount = _main_mount(request)
    if bridge is not None and _updating(bridge):
        notice = f"<p class='notice'>{_e(message)}</p>" if message else ""
        doors = f"<p class='notice'>{_e(household_doors(bridge.self_update))}</p>"
        return _document(request, notice + doors, mount=mount, csp=MENU_CSP)
    section = _section(bridge)
    token = _csrf_token(request)
    publisher = _menu_publisher(bridge)
    notice = f"<p class='notice'>{_e(message)}</p>" if message else ""
    back = _e(mount + "/")
    body = (
        notice
        + f"<p><a href='{back}'>{_e(_('Back to MQTT Bridge'))}</a></p>"
        + _menu_status_section(bridge, section, publisher)
        + _menu_settings_section(section, token)
        + f"<div class='creator'>{_creator_section(request, bridge, section, publisher, token)}"
        + "</div>"
    )
    head = f"<style>{_MENU_STYLE}</style>"
    return _document(request, body, head, mount=mount, csp=MENU_CSP,
                     subtitle=_("The Home Assistant menu"))


def _menu_answer(request, message="", code=None):
    """The Menu subpage, or the failure page when building it raised."""
    if code is not None:
        request.setResponseCode(code)
    try:
        return _menu_page(request, message)
    except Exception:
        LOG.exception("the Menu page could not be rendered")
        return _failed(request)


def _save_menu_settings(request):
    bridge = _bridge()
    if bridge is None:
        raise RuntimeError("the bridge is unavailable")
    _exact(request, ("csrf", "form", "rendered") + MENU_SETTING_NAMES)
    changes = _changes(request, bridge.settings, MENU_SETTING_NAMES)
    if not changes:
        return _menu_answer(request, _("Nothing to save: every setting already has that value."))
    return _apply(request, bridge, changes, _menu_answer)


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("ascii")


def _set_data_headers(request, content_type="application/json"):
    request.setHeader("content-type", content_type)
    request.setHeader("cache-control", "no-store")
    request.setHeader("x-content-type-options", "nosniff")
    request.setHeader("referrer-policy", "same-origin")
    request.setHeader("content-security-policy", DATA_CSP)


def _json_answer(request, payload, code=None):
    if code is not None:
        request.setResponseCode(code)
    _set_data_headers(request)
    return _json_bytes(payload)


def _same_origin_fetch(request):
    """`Sec-Fetch-Site` is `same-origin` where the browser sends it; absent is allowed.

    A browser too old to send it is not refused for that: the `Host` check, the same-origin
    `Origin` of a POST and the token are what keep another site out.
    """
    site = request.getHeader("sec-fetch-site")
    return site is None or site.strip().lower() == "same-origin"


def mounted():
    """OpenWebif took the page (the WebChilds hook): the menu creator can be offered now.

    The hook runs whenever OpenWebif loads it, before or after the bridge has connected, so a
    running bridge is told to say its capabilities again.
    """
    menueditor.mark_mounted()
    try:
        bridge = _bridge()
        if bridge is not None and bridge.running:
            bridge.announce_capabilities()
    except Exception:
        LOG.exception("the bridge could not be told that the OpenWebif page is mounted")


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
    """`<mount>/screen.jpg` - the last picture this process put on `screen`.

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


class CreatorScriptResource(resource.Resource):
    """`<mount>/menu/creator.js` - the menu creator, the one script this plugin serves.

    A file shipped in the package, read as it is: nothing in it is built per request, so no
    request can put anything into it.
    """

    isLeaf = True

    def render_GET(self, request):
        if not _host_allowed(request):
            return _misdirected(request)
        try:
            with open(CREATOR_SCRIPT, "rb") as handle:
                script = handle.read(MAX_SCRIPT_BYTES)
        except OSError:
            request.setResponseCode(404)
            _set_data_headers(request, "text/plain; charset=utf-8")
            return b""
        _set_data_headers(request, "text/javascript; charset=utf-8")
        return script


class CatalogueResource(resource.Resource):
    """`<mount>/menu/catalogue.json` - the last valid catalogue, exactly as Home Assistant sent it.

    Or, when there is none, `{"catalogue": null, "reason": ...}`: `menu_off`, `not_running`,
    `updating`, `none` (nothing received, or Home Assistant took it back) or `newer` (a
    catalogue of a version this plugin does not know).
    """

    isLeaf = True

    def render_GET(self, request):
        if not _host_allowed(request):
            return _misdirected(request)
        if not _same_origin_fetch(request):
            return _json_answer(request, {"catalogue": None, "reason": "rejected"},
                                http.FORBIDDEN)
        try:
            bridge = _bridge()
            if bridge is not None and _updating(bridge):
                return _json_answer(request, {"catalogue": None, "reason": "updating"})
            if bridge is None or not bridge.running:
                return _json_answer(request, {"catalogue": None, "reason": "not_running"})
            publisher = _menu_publisher(bridge)
            if publisher is None:
                return _json_answer(request, {"catalogue": None, "reason": "menu_off"})
            catalogue = publisher.editor.catalogue
            if catalogue is None:
                return _json_answer(request, {"catalogue": None,
                                              "reason": publisher.editor.reason or "none"})
            _set_data_headers(request)
            return catalogue["raw"]
        except Exception:
            LOG.exception("the menu catalogue could not be served")
            return _json_answer(request, {"catalogue": None, "reason": "failed"},
                                http.INTERNAL_SERVER_ERROR)


class MenuIconResource(resource.Resource):
    """`<mount>/menu/icon/<name>.png` - one of the menu's icons, for the creator's icon picker.

    Only a name of the contract's fixed list (`menu.ICON_NAMES`), as the PNG the package ships for
    the television; anything else is a 404. The name is looked up, never used as a path.
    """

    isLeaf = True

    def render_GET(self, request):
        if not _host_allowed(request):
            return _misdirected(request)
        from .menu import ICON_NAMES
        from .styledlist import icon_path

        segments = [
            part.decode("ascii", "replace") if isinstance(part, bytes) else str(part)
            for part in (getattr(request, "postpath", None) or [])
        ]
        name = segments[0][:-len(".png")] if len(segments) == 1 and segments[0].endswith(
            ".png") else None
        image = None
        if name in ICON_NAMES:
            try:
                with open(icon_path(name), "rb") as handle:
                    image = handle.read(256 * 1024)
            except OSError:
                image = None
        if image is None:
            request.setResponseCode(404)
            _set_headers(request, "text/plain; charset=utf-8", csp=DATA_CSP)
            return b""
        _set_headers(request, "image/png", csp="default-src 'none'; frame-ancestors 'self'")
        # A file of the package that changes only with it, asked for on every redraw of the page.
        request.setHeader("cache-control", "private, max-age=86400")
        return image


def _held(request, editor, base, menu):
    """Publish the edit and keep the request open until its answer: `NOT_DONE_YET`, or the answer.

    The answer is written when Home Assistant's comes, or `menueditor.ANSWER_SECONDS` pass, or
    the connection to the broker goes - whichever is first; the editor calls back exactly once.
    A browser that went away in the meantime is not written to.
    """
    state = {"open": True}

    def gone(_failure=None):
        state["open"] = False

    try:
        request.notifyFinish().addErrback(gone)
    except Exception:
        LOG.debug("this request cannot say when its client goes away")

    def answered(answer):
        if not state["open"]:
            LOG.info("the page that saved the menu was gone before the answer: %s",
                     answer.get("result"))
            return
        state["open"] = False
        try:
            request.write(_json_bytes(answer))
            request.finish()
        except Exception:
            LOG.exception("the answer to a menu save could not be written")

    refusal = editor.submit(base, menu, answered)
    if refusal is not None:
        return _json_bytes({"result": refusal})
    return NOT_DONE_YET


class MenuSaveResource(resource.Resource):
    """`<mount>/menu/save` - one save of the menu creator, answered when Home Assistant answers.

    A POST with the page's token, `form=menu_edit`, the catalogue `base` the edit started from
    and the whole `menu` as JSON - exactly these fields. The same `Host`, same-origin and token
    rules as every other form, and `Sec-Fetch-Site` when the browser sends it. The answer is
    JSON: Home Assistant's `result` (`saved`, `stale`, `invalid`, `busy`, `not_claimed`,
    `disabled`),
    `revision`, `path` and `detail`, or the receiver's own `receiver_busy`, `no_answer`,
    `not_connected`, `not_allowed`, `unreadable`, `too_big`, `rejected`, `out_of_date` or
    `failed` - never a word of Home Assistant's. The token is not replaced, so the page saves
    again without a reload.
    """

    isLeaf = True

    def render_GET(self, request):
        if not _host_allowed(request):
            return _misdirected(request)
        return _json_answer(request, {"result": "rejected"}, http.NOT_ALLOWED)

    def render_POST(self, request):
        if not _host_allowed(request):
            return _misdirected(request)
        content_type = (request.getHeader("content-type") or "").split(";", 1)[0].lower()
        if (content_type != "application/x-www-form-urlencoded" or not _same_origin(request)
                or not _same_origin_fetch(request)):
            return _json_answer(request, {"result": "rejected"}, http.FORBIDDEN)
        _set_data_headers(request)
        try:
            bridge = _bridge()
            if bridge is not None and _updating(bridge):
                return _json_answer(request, {"result": menueditor.NOT_ALLOWED}, http.CONFLICT)
            _check_token(request)
            _exact(request, ("csrf", "form", "base", "menu"))
            if _single_arg(request, "form") != MENU_EDIT_FORM:
                raise ValueError("unknown form")
            base = _single_arg(request, "base")
            # Read with the body's own bound, so that a menu over its bound is said to be too
            # big - as an edit too big to publish is - and not unreadable.
            text = _single_arg(request, "menu", MAX_BODY_BYTES)
            if len(text.encode("utf-8", "surrogatepass")) > menueditor.MAX_EDIT_BYTES:
                return _json_answer(request, {"result": menueditor.TOO_BIG}, http.BAD_REQUEST)
            try:
                menu = menueditor.loads_bounded(text)
            except (ValueError, RecursionError, OverflowError):
                raise ValueError("the menu is not JSON") from None
            if not menueditor.is_revision(base) or not isinstance(menu, list):
                raise ValueError("not an edit")
            publisher = _menu_publisher(bridge)
            if publisher is None or _uninstalling(bridge):
                return _json_answer(request, {"result": menueditor.NOT_ALLOWED})
            return _held(request, publisher.editor, base, menu)
        except _OutOfDate:
            return _json_answer(request, {"result": "out_of_date"}, http.FORBIDDEN)
        except PermissionError:
            return _json_answer(request, {"result": "rejected"}, http.FORBIDDEN)
        except (KeyError, TypeError, ValueError):
            return _json_answer(request, {"result": menueditor.UNREADABLE}, http.BAD_REQUEST)
        except Exception:
            LOG.exception("the menu creator's save could not be completed")
            return _json_answer(request, {"result": "failed"}, http.INTERNAL_SERVER_ERROR)


class MenuPageResource(resource.Resource):
    """`<mount>/menu` - the receiver menu's settings and the menu creator (ADR-0022).

    Always the whole page: the panel load is the main page's question, and nothing loads this
    one into OpenWebif's panel. Its settings form takes the main form's rules.
    """

    isLeaf = False

    def __init__(self):
        resource.Resource.__init__(self)
        self.putChild(b"creator.js", CreatorScriptResource())
        self.putChild(b"catalogue.json", CatalogueResource())
        self.putChild(b"save", MenuSaveResource())
        self.putChild(b"icon", MenuIconResource())
        self.putChild(b"", self)

    def render_GET(self, request):
        if not _host_allowed(request):
            return _misdirected(request)
        return _menu_answer(request)

    def render_POST(self, request):
        if not _host_allowed(request):
            return _misdirected(request)
        content_type = (request.getHeader("content-type") or "").split(";", 1)[0].lower()
        if content_type != "application/x-www-form-urlencoded" or not _same_origin(request):
            return _menu_answer(request, _("Request rejected."), http.FORBIDDEN)
        if _doors_closed():
            return _menu_answer(request, code=http.CONFLICT)
        try:
            if _single_arg(request, "form") != MENU_SETTINGS_FORM:
                raise ValueError("unknown form")
            _check_token(request)
            return _save_menu_settings(request)
        except _OutOfDate:
            return _menu_answer(
                request,
                _(
                    "This page is out of date: it was changed in another tab or window "
                    "since it was opened. Reload the page and try again."
                ),
                http.FORBIDDEN,
            )
        except PermissionError:
            return _menu_answer(request, _("Request rejected."), http.FORBIDDEN)
        except (KeyError, TypeError, ValueError) as error:
            return _menu_answer(request, _("Nothing was changed: %s") % error, http.BAD_REQUEST)
        except Exception:
            LOG.exception("the Menu page could not complete a request")
            return _menu_answer(
                request, _("Nothing was changed; see the plugin log."),
                http.INTERNAL_SERVER_ERROR,
            )


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
        self.putChild(MENU_CHILD.encode("ascii"), MenuPageResource())
        # `/mqttbridge/` is the same page as `/mqttbridge`. Twisted resolves the
        # trailing slash to an empty child, and without this the receiver
        # answers 404 to a perfectly ordinary URL - measured on the box.
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
        if _doors_closed() and not _repair_posted(request):
            # Every form - an install, any other action, a settings save, the answer to a
            # confirmation asked before - gets the sentence alone (TRANSACTION.md section
            # 5.3). No question: its answer could only meet the doors, and asking it tells the
            # household something can still be done. Nothing kept: a confirmation asked
            # before the doors closed is dropped, not left to be answered once they reopen.
            # The one exception is the repair the sentence names (after `not_stopped`, the
            # interface restart): its form and its confirmation go on, with every check.
            _session(request).pop(CONFIRM_KEY, None)
            return _answer(request, code=http.CONFLICT)
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
        except _Expired:
            return _answer(
                request,
                _(
                    "This confirmation was asked for more than ten minutes ago and has "
                    "expired. Nothing was changed; start again."
                ),
                http.FORBIDDEN,
            )
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
