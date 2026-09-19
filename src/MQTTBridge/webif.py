"""Authenticated OpenWebif status, settings, and bounded log viewer."""

import hmac
import html
import os
import re
import secrets
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
        INTERNAL_SERVER_ERROR = 500


from . import config as settings_module
from . import log as log_module
from .i18n import _
from .version import __version__

LOG = log_module.get_logger("webif")

MAX_LOG_BYTES = 65536
MAX_LOG_LINES = 200
CSRF_KEY = "mqttbridge_csrf"
FORM_FIELDS = {
    "publish_keys",
    "screenshot",
    "screenshot_interval",
    "screenshot_delay",
}
FIELD_LABELS = {
    "publish_keys": "Publish remote key presses",
    "screenshot": "Screenshots",
    "screenshot_interval": "Screenshot interval (seconds)",
    "screenshot_delay": "Screenshot delay after a zap (seconds)",
}
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


def _bridge():
    from .plugin import get_bridge

    return get_bridge()


def _authenticated(request):
    """Require OpenWebif auth itself, not its anonymous LAN/VPN bypass."""
    try:
        from Components.config import config

        enabled = (
            config.OpenWebif.https_auth.value if request.isSecure() else config.OpenWebif.auth.value
        )
        session = request.getSession().sessionNamespaces
        return enabled is True and session.get("logged") is True
    except Exception:
        return False


def _session(request):
    return request.getSession().sessionNamespaces


def _csrf_token(request):
    session = _session(request)
    token = session.get(CSRF_KEY)
    if not isinstance(token, str) or len(token) < 32:
        token = secrets.token_urlsafe(32)
        session[CSRF_KEY] = token
    return token


def _single_arg(request, name):
    values = request.args.get(name.encode("ascii"))
    if not isinstance(values, list) or len(values) != 1:
        raise ValueError("missing or repeated form field")
    value = values[0]
    if not isinstance(value, bytes) or len(value) > 128:
        raise ValueError("invalid form field")
    return value.decode("utf-8")


def _bool_arg(request, name):
    values = request.args.get(name.encode("ascii"))
    if values not in ([b"false"], [b"false", b"true"]):
        raise ValueError("invalid checkbox field")
    return values[-1].decode("ascii")


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


def _set_headers(request, content_type="text/html; charset=utf-8"):
    request.setHeader("content-type", content_type)
    request.setHeader("cache-control", "no-store")
    request.setHeader("x-content-type-options", "nosniff")
    request.setHeader("x-frame-options", "SAMEORIGIN")
    request.setHeader("referrer-policy", "no-referrer")
    request.setHeader(
        "content-security-policy",
        "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; form-action 'self'",
    )


def _forbidden(request):
    request.setResponseCode(http.FORBIDDEN)
    _set_headers(request, "text/plain; charset=utf-8")
    return _("OpenWebif authentication is required for MQTT Bridge.").encode("utf-8")


def _failed(request):
    """The last resort, for when building the page is what went wrong.

    OpenWebif would otherwise answer with its own traceback page, which is a
    stack trace from this plugin on an authenticated web page.
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


def _page(request, message=""):
    bridge = _bridge()
    running = bridge is not None
    connected = bool(getattr(bridge, "connected", False)) if running else False
    settings = bridge.remote_settings() if running else {}
    log_level = bridge.value("log_level") if running else "unknown"
    token = _csrf_token(request)
    controls = []
    for name in sorted(settings):
        if name not in FORM_FIELDS:
            continue
        value = settings[name]
        if name == "screenshot":
            options = "".join(
                f"<option value='{html.escape(choice, quote=True)}'"
                f"{' selected' if choice == value else ''}>"
                f"{html.escape(_(SCREENSHOT_LABELS.get(choice, choice)))}</option>"
                for choice in settings_module.SCREENSHOT_MODES
            )
            field = f"<select name='screenshot'>{options}</select>"
        elif isinstance(value, bool):
            field = (
                f"<input type='hidden' name='{name}' value='false'>"
                f"<input type='checkbox' name='{name}' value='true'"
                f"{' checked' if value else ''}>"
            )
        else:
            minimum, maximum = (
                settings_module.SCREENSHOT_INTERVAL_LIMITS
                if name == "screenshot_interval"
                else settings_module.SCREENSHOT_DELAY_LIMITS
            )
            field = (
                f"<input type='number' min='{minimum}' max='{maximum}' "
                f"name='{name}' value='{html.escape(str(value), quote=True)}'>"
            )
        controls.append(f"<label>{html.escape(_(FIELD_LABELS[name]))}{field}</label>")
    status_rows = (
        (_("Version"), __version__),
        (_("Bridge"), _("Running") if running else _("Stopped")),
        (_("MQTT"), _("Connected") if connected else _("Disconnected")),
        (_("Log level"), log_level),
    )
    rows = "".join(
        f"<dt>{html.escape(label)}</dt><dd>{html.escape(str(value))}</dd>"
        for label, value in status_rows
    )
    form = ""
    if running and controls:
        form = (
            "<form method='post'>"
            f"<input type='hidden' name='csrf' value='{html.escape(token, quote=True)}'>"
            f"{''.join(controls)}"
            f"<button type='submit'>{html.escape(_('Save publisher settings'))}</button>"
            "</form>"
        )
    notice = f"<p class='notice'>{html.escape(message)}</p>" if message else ""
    log_tail = html.escape(bounded_log_tail())
    icon = html.escape(_mount_path(request) + "/icon", quote=True)
    document = f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>MQTT Bridge</title><style>
body{{font:15px system-ui,sans-serif;margin:0;background:#f4f5f4;color:#202522}}
body{{letter-spacing:0;overflow-wrap:anywhere}}
main{{max-width:900px;margin:auto;padding:24px}}
header{{display:flex;align-items:center;gap:14px}}header img{{width:52px;height:52px}}
section{{border-top:1px solid #c8ceca;padding:18px 0}}
dl{{display:grid;grid-template-columns:130px 1fr;gap:8px}}dt{{color:#59615c}}dd{{margin:0}}
form{{display:grid;gap:12px;max-width:440px}}
label{{display:grid;grid-template-columns:1fr 180px;align-items:center;gap:12px}}
input,select,button{{font:inherit;padding:8px;background:#fff;color:#202522}}
input,select,button{{border:1px solid #89928c}}
button{{background:#087f5b;color:#fff;cursor:pointer}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:#272b29;color:#f4f5f4;padding:14px}}
pre{{max-height:34rem;overflow:auto}}.notice{{color:#8ce0bd}}
@media(max-width:520px){{main{{padding:16px}}label{{grid-template-columns:1fr}}}}
@media(max-width:520px){{dl{{grid-template-columns:100px 1fr}}}}
</style></head><body><main><header><img src='{icon}' alt=''>
<div><h1>MQTT Bridge</h1><p>{html.escape(_("Receiver integration status"))}</p></div>
</header>{notice}<section><h2>{html.escape(_("Status"))}</h2><dl>{rows}</dl></section>
<section><h2>{html.escape(_("Publisher settings"))}</h2>{form}</section>
<section><h2>{html.escape(_("Sanitized log tail"))}</h2><pre>{log_tail}</pre></section>
</main></body></html>"""
    _set_headers(request)
    return document.encode("utf-8")


class PluginIconResource(resource.Resource):
    isLeaf = True

    def render_GET(self, request):
        if not _authenticated(request):
            return _forbidden(request)
        try:
            with open(os.path.join(os.path.dirname(__file__), "plugin.png"), "rb") as handle:
                image = handle.read(256 * 1024)
        except OSError:
            request.setResponseCode(404)
            return b""
        _set_headers(request, "image/png")
        request.setHeader("content-security-policy", "default-src 'none'")
        return image


class MQTTBridgeWebResource(resource.Resource):
    """Standalone page mounted below OpenWebif's authenticated root."""

    isLeaf = False

    def __init__(self):
        resource.Resource.__init__(self)
        self.putChild(b"icon", PluginIconResource())
        # `/mqttbridge/` is the same page as `/mqttbridge`. Twisted resolves the
        # trailing slash to an empty child, and without this the receiver
        # answers 404 to a perfectly ordinary URL — measured on the box.
        self.putChild(b"", self)

    def render_GET(self, request):
        if not _authenticated(request):
            return _forbidden(request)
        return _answer(request)

    def render_POST(self, request):
        if not _authenticated(request):
            return _forbidden(request)
        content_type = (request.getHeader("content-type") or "").split(";", 1)[0].lower()
        if content_type != "application/x-www-form-urlencoded" or not _same_origin(request):
            return _answer(request, _("Request rejected."), http.FORBIDDEN)
        try:
            supplied = _single_arg(request, "csrf")
            expected = _session(request).get(CSRF_KEY)
            if (
                not isinstance(expected, str)
                or len(expected) < 32
                or len(supplied) < 32
                or not hmac.compare_digest(supplied, expected)
            ):
                raise PermissionError
            bridge = _bridge()
            if bridge is None:
                raise RuntimeError("bridge unavailable")
            current = bridge.remote_settings()
            editable = {name: value for name, value in current.items() if name in FORM_FIELDS}
            if set(editable) != FORM_FIELDS:
                raise ValueError("unsupported settings schema")
            allowed_args = {name.encode("ascii") for name in editable} | {b"csrf"}
            if set(request.args) != allowed_args:
                raise ValueError("unexpected form field")
            raw = dict(current)
            raw.update(
                {
                    name: settings_module.coerce(
                        name,
                        _bool_arg(request, name)
                        if isinstance(value, bool)
                        else _single_arg(request, name),
                    )
                    for name, value in editable.items()
                }
            )
            validated = settings_module.validate_remote_settings(
                raw, getattr(bridge, "settings", None)
            )
            error = bridge.apply_remote_settings(validated)
            if error:
                raise RuntimeError(error)
        except PermissionError:
            return _answer(request, _("Request rejected."), http.FORBIDDEN)
        except (KeyError, TypeError, ValueError):
            return _answer(request, _("Invalid publisher settings."), http.BAD_REQUEST)
        except Exception:
            LOG.exception("the publisher settings could not be saved")
            return _answer(
                request, _("Publisher settings were not changed."), http.INTERNAL_SERVER_ERROR
            )
        _session(request)[CSRF_KEY] = secrets.token_urlsafe(32)
        return _answer(request, _("Publisher settings saved."))


def create_resource():
    return MQTTBridgeWebResource()
