"""The optional OpenWebif page is authenticated, bounded, and exact-schema."""

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from Components.config import config

from MQTTBridge import webif

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "src" / "WebInterface" / "WebChilds" / "External" / "MQTTBridge.py"


class _Value:
    def __init__(self, value):
        self.value = value


class _Request:
    def __init__(
        self,
        *,
        logged=True,
        secure=False,
        origin="http://receiver.example",
        args=None,
        prepath=(b"mqttbridge",),
    ):
        # What Twisted leaves behind: the path segments already consumed to
        # reach this resource, as bytes.
        self.prepath = list(prepath)
        self._secure = secure
        self._session = SimpleNamespace(sessionNamespaces={"logged": logged})
        self._headers = {
            "host": "receiver.example",
            "origin": origin,
            "content-type": "application/x-www-form-urlencoded",
        }
        self.args = args or {}
        self.response_code = 200
        self.response_headers = {}

    def isSecure(self):
        return self._secure

    def getSession(self):
        return self._session

    def getHeader(self, name):
        return self._headers.get(name)

    def setHeader(self, name, value):
        self.response_headers[name] = value

    def setResponseCode(self, code):
        self.response_code = code


class _Bridge:
    def __init__(self):
        self.connected = True
        # The real bridge's `settings` is the enigma2 config section, and that
        # is where an omitted optional key's current value comes from.
        self.settings = webif.settings_module.settings
        self.values = {
            "publish_keys": True,
            "screenshot": "on_zap",
            "screenshot_interval": 60,
            "screenshot_delay": 3,
            "cam_telemetry": True,
        }
        self.applied = []

    def remote_settings(self):
        return dict(self.values)

    def value(self, name):
        assert name == "log_level"
        return "info"

    def apply_remote_settings(self, values):
        self.applied.append(values)
        self.values = dict(values)
        return None


@pytest.fixture(autouse=True)
def openwebif_config():
    previous = getattr(config, "OpenWebif", None)
    config.OpenWebif = SimpleNamespace(auth=_Value(True), https_auth=_Value(True))
    yield
    if previous is None:
        del config.OpenWebif
    else:
        config.OpenWebif = previous


def _post_args(request, **overrides):
    values = {
        "csrf": webif._csrf_token(request),
        "publish_keys": "false",
        "screenshot": "interval",
        "screenshot_interval": "120",
        "screenshot_delay": "4",
    }
    values.update(overrides)
    encoded = {key.encode(): [value.encode()] for key, value in values.items()}
    encoded[b"publish_keys"] = [b"false"]
    return encoded


def test_page_refuses_anonymous_or_auth_disabled(monkeypatch):
    bridge = _Bridge()
    monkeypatch.setattr(webif, "_bridge", lambda: bridge)
    resource = webif.MQTTBridgeWebResource()

    anonymous = _Request(logged=False)
    assert resource.render_GET(anonymous).startswith(b"OpenWebif authentication")
    assert anonymous.response_code == 403

    config.OpenWebif.auth.value = False
    bypassed_lan = _Request(logged=True)
    resource.render_GET(bypassed_lan)
    assert bypassed_lan.response_code == 403
    assert bridge.applied == []


def test_get_is_a_real_tool_but_never_mutates(monkeypatch, tmp_path):
    bridge = _Bridge()
    monkeypatch.setattr(webif, "_bridge", lambda: bridge)
    log = tmp_path / "bridge.log"
    log.write_text("normal line\n", encoding="utf-8")
    monkeypatch.setattr(webif.log_module, "active_path", lambda: str(log))
    request = _Request()

    body = webif.MQTTBridgeWebResource().render_GET(request)

    assert b"MQTT Bridge" in body
    assert b"plugin.png" not in body
    assert b"/mqttbridge/icon" in body
    assert b"normal line" in body
    assert bridge.applied == []
    assert request.response_headers["cache-control"] == "no-store"


def test_the_icon_url_follows_the_path_the_page_was_reached_by(monkeypatch, tmp_path):
    """OpenWebif owns the mount point; the page must not assume its own name."""
    monkeypatch.setattr(webif, "_bridge", lambda: _Bridge())
    monkeypatch.setattr(webif.log_module, "active_path", lambda: None)
    page = webif.MQTTBridgeWebResource()

    body = page.render_GET(_Request(prepath=[b"somewhere", b"else", b""]))
    assert b"src='/somewhere/else/icon'" in body

    body = page.render_GET(_Request())
    assert b"src='/mqttbridge/icon'" in body


def test_the_page_answers_with_and_without_the_trailing_slash(monkeypatch):
    """🔴 `/mqttbridge/` resolves to an empty child; without one it is a 404."""
    monkeypatch.setattr(webif, "_bridge", lambda: _Bridge())
    monkeypatch.setattr(webif.log_module, "active_path", lambda: None)
    page = webif.MQTTBridgeWebResource()

    assert page.children[b""] is page
    # And the icon URL that page builds still points at the icon, not at itself.
    body = page.render_GET(_Request(prepath=[b"mqttbridge", b""]))
    assert b"src='/mqttbridge/icon'" in body


def test_a_page_that_cannot_be_built_is_the_plugins_own_failure_page(monkeypatch, plugin_log):
    """OpenWebif would otherwise render this plugin's traceback to a browser."""
    monkeypatch.setattr(webif, "_bridge", lambda: _Bridge())
    monkeypatch.setattr(webif, "bounded_log_tail", lambda *_a, **_k: 1 / 0)
    request = _Request()

    body = webif.MQTTBridgeWebResource().render_GET(request)

    assert request.response_code == 500
    assert b"could not be built" in body
    assert b"Traceback" not in body
    assert "the MQTT Bridge status page could not be rendered" in plugin_log()


def test_authenticated_icon_resource_serves_the_bundled_png():
    request = _Request()

    body = webif.PluginIconResource().render_GET(request)

    assert body.startswith(b"\x89PNG\r\n\x1a\n")
    assert request.response_headers["content-type"] == "image/png"
    assert request.response_headers["cache-control"] == "no-store"


def test_post_requires_same_origin_csrf_and_exact_fields(monkeypatch):
    bridge = _Bridge()
    monkeypatch.setattr(webif, "_bridge", lambda: bridge)
    page = webif.MQTTBridgeWebResource()

    wrong_origin = _Request(origin="https://attacker.example")
    wrong_origin.args = _post_args(wrong_origin)
    page.render_POST(wrong_origin)
    assert wrong_origin.response_code == 403

    wrong_csrf = _Request()
    wrong_csrf.args = _post_args(wrong_csrf, csrf="wrong")
    page.render_POST(wrong_csrf)
    assert wrong_csrf.response_code == 403

    extra = _Request()
    extra.args = _post_args(extra)
    extra.args[b"path"] = [b"/etc/enigma2/settings"]
    page.render_POST(extra)
    assert extra.response_code == 400
    assert bridge.applied == []


def test_valid_post_applies_only_normalized_remote_settings(monkeypatch):
    bridge = _Bridge()
    monkeypatch.setattr(webif, "_bridge", lambda: bridge)
    request = _Request()
    old_token = webif._csrf_token(request)
    request.args = _post_args(request)

    body = webif.MQTTBridgeWebResource().render_POST(request)

    assert request.response_code == 200
    assert bridge.applied == [
        {
            "publish_keys": False,
            "screenshot": "interval",
            "screenshot_interval": 120,
            "screenshot_delay": 4,
            "cam_telemetry": True,
            "oscam_telemetry": False,
            # Not on the form, and carried through untouched rather than reset.
            "softcam_autoheal": False,
            "softcam_autoheal_seconds": 90,
        }
    ]
    assert b"Publisher settings saved" in body
    assert webif._csrf_token(request) != old_token


def test_log_tail_is_bounded_redacted_and_html_escaped(monkeypatch, tmp_path):
    secret = "very-secret-value"
    webif.log_module.register_secret(secret)
    log = tmp_path / "bridge.log"
    lines = ["old"] * 250
    lines += [
        "password=plaintext",
        'payload={"password": "quoted secret value", "username": "private user"}',
        "user=private-name",
        "mqtt://user:pass@broker.example/topic",
        secret,
        "<script>alert(1)</script>",
    ]
    log.write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(webif.log_module, "active_path", lambda: str(log))
    monkeypatch.setattr(webif, "_bridge", lambda: _Bridge())

    body = webif.MQTTBridgeWebResource().render_GET(_Request())

    assert b"plaintext" not in body
    assert b"quoted secret value" not in body
    assert b"private user" not in body
    assert b"private-name" not in body
    assert b"user:pass" not in body
    assert secret.encode() not in body
    assert b"<script>" not in body
    assert b"&lt;script&gt;" in body
    assert webif.bounded_log_tail().count("\n") < webif.MAX_LOG_LINES
    webif.log_module.forget_secrets()


def _run_hook(monkeypatch, toplevel=None):
    """Load the OpenWebif hook the way OpenWebif loads it: by path, on its own."""
    if toplevel is not None:
        packages = {
            "Plugins": types.ModuleType("Plugins"),
            "Plugins.Extensions": types.ModuleType("Plugins.Extensions"),
            "Plugins.Extensions.MQTTBridge": types.ModuleType("Plugins.Extensions.MQTTBridge"),
            "Plugins.Extensions.MQTTBridge.webif": webif,
            "Plugins.Extensions.WebInterface": types.ModuleType("Plugins.Extensions.WebInterface"),
            "Plugins.Extensions.WebInterface.WebChilds": types.ModuleType(
                "Plugins.Extensions.WebInterface.WebChilds"
            ),
            "Plugins.Extensions.WebInterface.WebChilds.Toplevel": toplevel,
        }
        for name, module in packages.items():
            monkeypatch.setitem(sys.modules, name, module)
    spec = importlib.util.spec_from_file_location("mqttbridge_openwebif_hook", HOOK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_openwebif_hook_registers_what_upstream_expects(monkeypatch):
    """🔴 Upstream does `six.ensure_binary(plugin[0])` and `"/%s" % plugin[0]`.

    OpenWebif's `addExternalChild` takes one sequence of at least three items
    and mounts it with `root.putChild2(plugin[0], plugin[1])`, so the link is a
    `str` there — while Twisted's own `putChild`, which the resource uses for
    its icon, needs bytes.
    """
    registered = []
    toplevel = types.ModuleType("Plugins.Extensions.WebInterface.WebChilds.Toplevel")
    toplevel.loaded_plugins = []
    toplevel.addExternalChild = registered.append

    _run_hook(monkeypatch, toplevel)

    assert len(registered) == 1
    entry = registered[0]
    assert isinstance(entry, tuple) and len(entry) >= 3
    link, child, name, version, has_gui, target = entry
    assert link == "mqttbridge" and type(link) is str
    assert isinstance(name, str) and isinstance(version, int) and has_gui is True
    assert target == "_self"
    assert hasattr(child, "render_GET") and hasattr(child, "putChild")
    # Twisted 22 on these images refuses a str key here.
    assert child.children and all(type(key) is bytes for key in child.children)


def test_the_openwebif_hook_is_silent_when_openwebif_is_absent(monkeypatch):
    for name in list(sys.modules):
        if name.startswith("Plugins."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    # No exception, and nothing registered: most boxes have no OpenWebif hook.
    assert _run_hook(monkeypatch) is not None


def test_log_tail_refuses_symlinks(tmp_path):
    target = tmp_path / "target"
    target.write_text("should not be read", encoding="utf-8")
    link = tmp_path / "bridge.log"
    link.symlink_to(target)

    assert webif.bounded_log_tail(str(link)) == "Logging is unavailable."


def test_invalid_origin_and_unissued_empty_csrf_fail_closed(monkeypatch):
    bridge = _Bridge()
    monkeypatch.setattr(webif, "_bridge", lambda: bridge)
    page = webif.MQTTBridgeWebResource()
    malformed = _Request(origin="http://[")
    malformed.args = _post_args(malformed)
    page.render_POST(malformed)
    assert malformed.response_code == 403

    empty = _Request()
    empty.args = _post_args(empty, csrf="")
    empty._session.sessionNamespaces.pop(webif.CSRF_KEY, None)
    page.render_POST(empty)
    assert empty.response_code == 403
    assert bridge.applied == []
