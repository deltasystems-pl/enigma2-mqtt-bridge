"""The OpenWebif page trusts OpenWebif, and guards its own writes.

OpenWebif decides who reaches the page before any plugin code runs, so the page
enforces no login of its own (ADR-0009). What it keeps is what stops another web
page from using it through somebody's browser — the `Host` allowlist, the
same-origin check, the session token and the exact field sets — and
what stops a value from damaging the receiver: control characters refused in
every text setting, secrets write-only.

Five tests here asserted the old rule — that the page answers only a logged
OpenWebif session with authentication switched on — and were rewritten to the
new one rather than deleted: the refusal of an anonymous request is now the
page answering it (`test_the_page_its_icon_and_an_action_answer_without_any_login`);
the icon test lost its login; and the three POST tests keep every refusal they
made, now against an anonymous session and an allowed `Host`, with the settings
form carrying every setting instead of four.
"""

import html
import importlib.util
import io
import re
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote_to_bytes, urlencode

import pytest
from Components.config import config, configfile
from conftest import ConsoleAppContainer, RecordTimerEntry
from Tools.Notifications import Notifications

from MQTTBridge import config as settings_module
from MQTTBridge import discovery, webif
from MQTTBridge.commands import CommandDispatcher
from MQTTBridge.origin import MQTT, PAGE, granted

REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / "src" / "WebInterface" / "WebChilds" / "External" / "MQTTBridge.py"

BOX = "192.0.2.12"
HOSTNAME = "vuuno4kse"
NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
INFO = ROOT + "/info"
LAST_ERROR = ROOT + "/last_error"
PNG = b"\x89PNG\r\n\x1a\n"
FORM = "application/x-www-form-urlencoded"

SETTING_NAMES = settings_module.SETTING_NAMES
TEXT_SETTINGS = [
    name for name in SETTING_NAMES if settings_module.SETTING_KINDS[name] == "text"
]


class _Request:
    def __init__(
        self,
        *,
        session=None,
        host=BOX,
        origin="http://" + BOX,
        content_type=FORM,
        secure=False,
        args=None,
        prepath=(b"mqttbridge",),
        uri=b"/mqttbridge",
        raw_body=None,
        headers=None,
    ):
        # What Twisted leaves behind: the path segments already consumed to
        # reach this resource, as bytes.
        self.prepath = list(prepath)
        self.uri = uri
        self._secure = secure
        self._session = session if session is not None else new_session()
        self._headers = {"host": host, "origin": origin, "content-type": content_type}
        self._headers.update(headers or {})
        # What Twisted does with a POST: the body as a file, and `args` as the
        # query string *and* the body merged, the query parsed its own way.
        body = args or {}
        self.content = io.BytesIO(urlencode(
            [(key, value) for key, values in body.items() for value in values]
        ).encode("ascii") if raw_body is None else raw_body)
        # 🔴 Already read, as on a real server: Twisted has consumed the body to
        # build `args` before the resource runs, so the file is at its end.
        self.content.seek(0, io.SEEK_END)
        query = uri.split(b"?", 1)[1] if b"?" in uri else b""
        self.args = twisted_parse_qs(query)
        for key, values in body.items():
            self.args.setdefault(key, []).extend(values)
        self.response_code = 200
        self.response_headers = {}

    def isSecure(self):
        return self._secure

    def getSession(self):
        return self._session

    def getHost(self):
        return SimpleNamespace(host=BOX)

    def getHeader(self, name):
        return self._headers.get(name)

    def setHeader(self, name, value):
        self.response_headers[name] = value

    def setResponseCode(self, code):
        self.response_code = code


def twisted_parse_qs(query):
    """`twisted.web.http.parse_qs(query, 1)`, as Twisted 24 fills `request.args`.

    🔴 It splits on `;` as well as `&` and unquotes `+` and `%XX` in names, which
    is why the page cannot trust `request.args` to be the body.
    """
    found = {}
    for item in re.split(rb"[&;]", query):
        if not item:
            continue
        pair = item.split(b"=", 1)
        if len(pair) != 2:
            continue
        name = unquote_to_bytes(pair[0].replace(b"+", b" "))
        value = unquote_to_bytes(pair[1].replace(b"+", b" "))
        found.setdefault(name, []).append(value)
    return found


class _UntouchableSession:
    """A session that fails the test the moment anything asks for it."""

    @property
    def sessionNamespaces(self):
        raise AssertionError("the session was read")


def new_session():
    # Anonymous: what OpenWebif opens for every request before it decides
    # anything. There is no `logged` in it and the page must not care.
    return SimpleNamespace(sessionNamespaces={})


def encode(fields):
    encoded = {}
    for key, value in fields.items():
        values = value if isinstance(value, list) else [value]
        encoded[key.encode()] = [item.encode("utf-8") for item in values]
    return encoded


@pytest.fixture(autouse=True)
def hostname(monkeypatch):
    monkeypatch.setattr(webif.socket, "gethostname", lambda: HOSTNAME)


@pytest.fixture(autouse=True)
def no_openwebif_setting():
    """🔴 There is no OpenWebif setting, and reading one fails the test."""

    class Trap:
        def __getattr__(self, name):
            raise AssertionError("the page read config.OpenWebif." + name)

    previous = getattr(config, "OpenWebif", None)
    config.OpenWebif = Trap()
    yield
    if previous is None:
        del config.OpenWebif
    else:
        config.OpenWebif = previous


@pytest.fixture
def page(monkeypatch):
    def build(bridge):
        monkeypatch.setattr(webif, "_bridge", lambda: bridge)
        return webif.MQTTBridgeWebResource()

    return build


def get(resource, session=None, **kwargs):
    request = _Request(session=session, **kwargs)
    return request, resource.render_GET(request)


def token(session):
    return webif._csrf_token(_Request(session=session))


def post(resource, session, fields, csrf=True, **kwargs):
    fields = dict(fields)
    if isinstance(fields.get("rendered"), dict):
        # What the page would have rendered into the form for this session.
        fields["rendered"] = webif._seal(token(session), fields["rendered"])
    if csrf is True:
        fields["csrf"] = token(session)
    elif csrf is not None:
        fields["csrf"] = csrf
    request = _Request(session=session, args=encode(fields), **kwargs)
    return request, resource.render_POST(request)


def settings_fields(section, **overrides):
    """The settings form as a browser submits it, unchanged unless told otherwise.

    Rendered from the section as it stands now; `post` seals the snapshot with
    the session's token, exactly as the page does.
    """
    fields = {"form": "settings", "rendered": webif._rendered_values(section)}
    for name in SETTING_NAMES:
        kind = settings_module.SETTING_KINDS[name]
        current = settings_module.value(name, section)
        if kind == "bool":
            fields[name] = ["false", "true"] if current else ["false"]
        elif name in settings_module.SECRET_NAMES:
            fields[name] = ""
        else:
            fields[name] = str(current)
    for name, value in overrides.items():
        if isinstance(value, bool):
            value = ["false", "true"] if value else ["false"]
        fields[name] = value
    return fields


def action_fields(which, **values):
    fields = {"form": "action", "action": which}
    fields.update(values)
    return fields


def confirmation(body):
    """The hidden fields of a confirmation page, as the browser would send them."""
    text = body.decode("utf-8")
    assert "name='form' value='confirm'" in text, text[:400]
    form = text.split("<form method='post'>", 1)[1].split("</form>", 1)[0]
    return {
        name: html.unescape(value)
        for name, value in re.findall(r"name='([^']+)' value='([^']*)'", form)
    }


def settings_form(body):
    text = body.decode("utf-8")
    return text.split("<form method='post' class='settings'>", 1)[1].split("</form>", 1)[0]


def submitted(body, **overrides):
    """What a browser submits from the settings form in `body`, as it was rendered.

    Parsed from the page itself — hidden fields, inputs, checked boxes, selected
    options — so a test of a stale form submits exactly what a stale tab would.
    """
    fields = {}
    for tag, rest in re.findall(r"<(input|select)([^>]*)>", settings_form(body)):
        attributes = {
            key: html.unescape(value) for key, value in re.findall(r"(\w+)='([^']*)'", rest)
        }
        name = attributes.get("name")
        if tag == "select":
            options = settings_form(body).split("<select name='" + name + "'>", 1)[1]
            options = options.split("</select>", 1)[0]
            fields[name] = html.unescape(re.search(r"value='([^']*)' selected", options).group(1))
        elif attributes.get("type") == "checkbox":
            if " checked" in rest:
                fields[name] = ["false", "true"]
        elif attributes.get("type") == "hidden" and name in fields:
            continue
        else:
            fields[name] = attributes.get("value", "")
    for name, value in overrides.items():
        if isinstance(value, bool):
            value = ["false", "true"] if value else ["false"]
        fields[name] = value
    return fields


def device(factory):
    entry = factory.client.last(discovery.device_topic("homeassistant", NODE))
    return entry.json() if entry is not None and entry.text else {}


def error(factory):
    entry = factory.client.last(LAST_ERROR)
    return None if entry is None or entry.text == "" else entry.json()["error"]


def send(factory, name, payload=b"PRESS"):
    factory.client.fire_message(ROOT + "/cmd/" + name, payload)


# --------------------------------------------------------- OpenWebif's gate only --


def test_the_page_its_icon_and_an_action_answer_without_any_login(connected_bridge, page):
    """Inverts `test_page_refuses_anonymous_or_auth_disabled`.

    That test required OpenWebif authentication on and a logged session, and
    refused everybody else — which on a receiver at OpenWebif's defaults was
    everybody. OpenWebif's own gate runs before this code; the page answers
    whatever it lets through, and there is no setting for it to read.
    """
    resource = page(connected_bridge)
    session = new_session()

    request, body = get(resource, session)
    assert request.response_code == 200
    assert b"<form method='post' class='settings'>" in body
    for action in webif.actions():
        assert ("name='action' value='" + action.key + "'").encode() in body
    assert b"<fieldset disabled>" not in body
    icon = _Request(session=session)
    assert webif.PluginIconResource().render_GET(icon).startswith(PNG)
    assert icon.response_code == 200
    request, body = post(resource, session, action_fields("discovery"))
    assert request.response_code == 200
    assert b"Done: " in body
    assert "logged" not in session.sessionNamespaces


def test_no_code_path_reads_an_openwebif_setting():
    source = Path(webif.__file__).read_text(encoding="utf-8")
    assert "config.OpenWebif" not in source
    assert "_authenticated" not in source


# --------------------------------------------------------------- Host allowlist --


@pytest.mark.parametrize(
    "host",
    [
        BOX,
        BOX + ":80",
        "[2001:db8::12]",
        "[2001:db8::12]:8080",
        "localhost",
        "localhost:80",
        HOSTNAME,
        HOSTNAME + ".local",
        HOSTNAME.upper() + ".LOCAL:80",
    ],
)
def test_the_receivers_own_names_are_accepted(connected_bridge, page, host):
    request, body = get(page(connected_bridge), host=host)
    assert request.response_code == 200
    assert b"MQTT Bridge" in body


@pytest.mark.parametrize(
    "host",
    [
        "attacker.example",
        "attacker.example:80",
        HOSTNAME + ".attacker.example",
        HOSTNAME + ".local.attacker.example",
        "local",
        "2001:db8::12",
        "[" + BOX + "]",
        BOX + ".",
        BOX + ":http",
        BOX + ":123456",
        "user@" + BOX,
        # Matched whole, never by suffix or with the root's trailing dot.
        "not" + HOSTNAME,
        "not" + HOSTNAME + ".local",
        HOSTNAME + ".",
        HOSTNAME + ".local.",
        "localhost.",
        "",
        None,
    ],
)
def test_any_other_host_is_refused_before_the_session_is_read(connected_bridge, page, host):
    """🔴 DNS rebinding: a hostile name is same-origin with itself, so only `Host` can tell."""
    resource = page(connected_bridge)

    request, body = get(resource, _UntouchableSession(), host=host)
    assert request.response_code == 421
    assert BOX.encode() in body
    request = _Request(session=_UntouchableSession(), host=host,
                       origin="http://" + str(host), args=encode({"form": "action"}))
    assert request.response_code == 200
    resource.render_POST(request)
    assert request.response_code == 421
    icon = _Request(session=_UntouchableSession(), host=host)
    assert not webif.PluginIconResource().render_GET(icon).startswith(PNG)
    assert icon.response_code == 421


# ----------------------------------------------------------------- POST checks --


def _valid_change(bridge):
    return settings_fields(bridge.settings, screenshot_delay="9")


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("wrong content type", 403),
        ("missing origin", 403),
        ("cross-scheme origin", 403),
        ("mismatched origin", 403),
        ("missing token", 403),
        ("null origin", 403),
        ("origin with a path", 403),
        ("origin with userinfo", 403),
        ("origin on another port", 403),
        ("short token", 403),
        ("prefix of the real token", 403),
        ("another session's token", 403),
        ("empty unissued token", 403),
        ("rendered snapshot tampered", 403),
        ("rendered snapshot of another session", 403),
        ("extra field", 400),
        ("repeated field", 400),
        ("missing field", 400),
        ("unknown form", 400),
    ],
)
def test_a_refused_post_changes_nothing(connected_bridge, page, factory, settings, case, code):
    resource = page(connected_bridge)
    session = new_session()
    fields = _valid_change(connected_bridge)
    options = {}
    csrf = True
    if case == "wrong content type":
        options["content_type"] = "text/plain"
    elif case == "missing origin":
        options["origin"] = None
    elif case == "cross-scheme origin":
        options["origin"] = "https://" + BOX
    elif case == "mismatched origin":
        options["origin"] = "http://attacker.example"
    elif case == "missing token":
        csrf = None
    elif case == "null origin":
        # What a sandboxed frame or a `data:` page sends.
        options["origin"] = "null"
    elif case == "origin with a path":
        options["origin"] = "http://" + BOX + "/mqttbridge"
    elif case == "origin with userinfo":
        options["origin"] = "http://attacker@" + BOX
    elif case == "origin on another port":
        options["origin"] = "http://" + BOX + ":8080"
    elif case == "short token":
        csrf = token(session)[:31]
    elif case == "prefix of the real token":
        # Long enough to pass a length check, and still not the token.
        csrf = token(session)[:40]
    elif case == "another session's token":
        # The victim has a token of its own; the attacker presents a valid
        # token from a session of their own. Refused for not being *this*
        # session's token — not for the victim having none.
        own = token(session)
        csrf = token(new_session())
        assert len(own) >= 32 and csrf != own
    elif case == "empty unissued token":
        csrf = ""
    elif case == "rendered snapshot tampered":
        fields["rendered"] = webif._seal(token(session), fields["rendered"])
        fields["rendered"] = fields["rendered"].replace('"screenshot_delay":4',
                                                        '"screenshot_delay":5')
    elif case == "rendered snapshot of another session":
        fields["rendered"] = webif._seal(token(new_session()), fields["rendered"])
    elif case == "extra field":
        fields["path"] = "/etc/enigma2/settings"
    elif case == "repeated field":
        fields["screenshot_delay"] = ["9", "10"]
    elif case == "missing field":
        del fields["log_level"]
    elif case == "unknown form":
        fields["form"] = "something"
    before = factory.client.published[:]

    request, body = post(resource, session, fields, csrf=csrf, **options)

    assert request.response_code == code
    assert settings.screenshot_delay.value == 4
    assert configfile.save_calls == 0
    assert factory.client.published == before
    assert b"<script" not in body


@pytest.mark.parametrize(
    "query",
    [
        "csrf={t}",
        ";csrf={t}",
        "x;csrf={t}",
        "form=action;csrf={t}",
        "%63srf={t}",
        "csrf+={t}",
        "a=1&csrf={t}",
        "a=1;%63srf={t}",
    ],
)
def test_a_token_in_the_address_is_not_a_token(connected_bridge, page, monkeypatch, query):
    """🔴 Twisted merges the query into `request.args` and splits it on `;` too.

    The token is only ever read from the POST body, so no spelling of it in the
    URL — which ends up in logs, history and `Referer` — counts.
    """
    sent = []
    monkeypatch.setattr(connected_bridge, "run_command", lambda *args: sent.append(args))
    session = new_session()
    uri = ("/mqttbridge?" + query.format(t=token(session))).encode()

    request, _body = post(page(connected_bridge), session, action_fields("discovery"),
                          csrf=None, uri=uri)

    assert request.response_code == 403
    assert sent == []


def test_only_the_body_is_read_even_for_ordinary_fields(connected_bridge, page, monkeypatch):
    """A field smuggled in the query string is not a field of the form."""
    sent = []
    monkeypatch.setattr(connected_bridge, "run_command",
                        lambda name, text, origin: sent.append((name, text)))
    request, _body = post(page(connected_bridge), new_session(), action_fields("volume"),
                          uri=b"/mqttbridge?level=30")
    assert request.response_code == 400
    assert sent == []


def test_an_already_read_body_is_read_again_from_its_start(connected_bridge, page,
                                                           monkeypatch):
    """🔴 Twisted has read `content` to its end before the page runs."""
    sent = []
    monkeypatch.setattr(connected_bridge, "run_command",
                        lambda name, text, origin: sent.append(name))
    session = new_session()
    request = _Request(session=session, args=encode(dict(
        action_fields("discovery"), csrf=token(session))))
    assert request.content.tell() == len(request.content.getvalue()) > 0

    page(connected_bridge).render_POST(request)

    assert request.response_code == 200
    assert sent == ["discovery"]


def test_a_body_is_unquoted_percent_after_plus(connected_bridge, page, monkeypatch):
    """`%2B` is a literal plus and `+` a space — decoding in the other order swaps them."""
    sent = []
    monkeypatch.setattr(connected_bridge, "run_command",
                        lambda name, text, origin: sent.append(text))
    session = new_session()
    body = ("form=action&action=key&csrf=" + token(session)
            + "&key=a%2Bb+c&long=false").encode()
    request = _Request(session=session, raw_body=body)

    page(connected_bridge).render_POST(request)

    assert request.response_code == 200
    assert sent == ['{"key":"a+b c","long":false}']


def test_a_body_over_the_cap_is_refused(connected_bridge, page, monkeypatch):
    sent = []
    monkeypatch.setattr(connected_bridge, "run_command", lambda *args: sent.append(args))
    session = new_session()
    # Valid in every field; only its size is wrong. Empty `&&…` parts are
    # skipped by the parser, so without the cap this body would be accepted.
    body = ("form=action&action=discovery&csrf=" + token(session)).encode()
    body += b"&" * (webif.MAX_BODY_BYTES + 1 - len(body))
    request = _Request(session=session, raw_body=body)

    page(connected_bridge).render_POST(request)

    assert request.response_code == 400
    assert sent == []


def test_another_sessions_token_gets_the_plain_refusal(connected_bridge, page):
    """„Out of date" is for this session's own replaced token, not a stranger's."""
    resource = page(connected_bridge)
    session = new_session()
    token(session)
    request, body = post(resource, session, action_fields("discovery"),
                         csrf=token(new_session()))
    assert request.response_code == 403
    assert b"Request rejected." in body
    assert b"out of date" not in body


def test_the_body_is_split_on_ampersands_only(connected_bridge, page, monkeypatch):
    """`application/x-www-form-urlencoded` has one separator; `;` stays in its value."""
    sent = []
    monkeypatch.setattr(connected_bridge, "run_command",
                        lambda name, text, origin: sent.append((name, text)))
    session = new_session()
    body = ("form=action&action=key&csrf=" + token(session)
            + "&key=KEY_RED;long=true&long=false").encode()
    request = _Request(session=session, raw_body=body)

    page(connected_bridge).render_POST(request)

    assert request.response_code == 200
    assert sent == [("key", '{"key":"KEY_RED;long=true","long":false}')]


def test_a_page_left_open_after_another_tab_saved_says_it_is_out_of_date(
    connected_bridge, page, settings
):
    resource = page(connected_bridge)
    session = new_session()
    _request, old_tab = get(resource, session)
    _request, new_tab = get(resource, session)

    request, _body = post(resource, session, submitted(new_tab, log_level="debug"), csrf=None)
    assert request.response_code == 200

    request, body = post(resource, session, submitted(old_tab, screenshot_delay="9"), csrf=None)

    assert request.response_code == 403
    assert b"out of date" in body
    assert b"Reload the page" in body
    assert settings.screenshot_delay.value == 4


def test_only_the_last_eight_replaced_tokens_are_out_of_date(connected_bridge, page,
                                                             monkeypatch):
    """The memory of replaced tokens is bounded: the ninth one back is a stranger's.

    Eight is the number, written out rather than read from the module, so that
    changing the bound is a decision this test has to be told about. The session
    lives as long as the browser keeps its cookie; an unbounded list would grow
    by one token for every action anybody ever took on the page.
    """
    monkeypatch.setattr(connected_bridge, "run_command", lambda *args: None)
    resource = page(connected_bridge)
    session = new_session()
    replaced = []
    for _ in range(9):
        replaced.append(token(session))
        request, _body = post(resource, session, action_fields("discovery"))
        assert request.response_code == 200
    assert token(session) not in replaced

    request, body = post(resource, session, action_fields("discovery"), csrf=replaced[0])
    assert request.response_code == 403
    assert b"Request rejected." in body
    assert b"out of date" not in body

    for old in replaced[1:]:
        request, body = post(resource, session, action_fields("discovery"), csrf=old)
        assert request.response_code == 403
        assert b"out of date" in body


def test_an_invalid_value_stored_before_does_not_block_other_saves(connected_bridge, page,
                                                                    settings):
    """A 200-character host from before the page's rules, and a save that leaves it alone."""
    settings.host.value = "h" * 200
    settings.host.save()
    resource = page(connected_bridge)
    session = new_session()
    _request, body = get(resource, session)

    request, reply = post(resource, session, submitted(body, log_level="debug"), csrf=None)

    assert request.response_code == 200, re.findall(r"class='notice'>([^<]*)", reply.decode())
    assert settings.log_level.value == "debug"
    assert settings.host.value == "h" * 200

    # Editing it is validated as always.
    _request, body = get(resource, session)
    request, _reply = post(resource, session, submitted(body, host="i" * 200), csrf=None)
    assert request.response_code == 400
    assert settings.host.value == "h" * 200


def test_the_token_is_replaced_after_a_save(connected_bridge, page, settings):
    resource = page(connected_bridge)
    session = new_session()
    used = token(session)

    request, _body = post(resource, session, _valid_change(connected_bridge), csrf=used)
    assert request.response_code == 200
    assert settings.screenshot_delay.value == 9

    replay = settings_fields(connected_bridge.settings, screenshot_delay="12")
    request, _body = post(resource, session, replay, csrf=used)
    assert request.response_code == 403
    assert settings.screenshot_delay.value == 9


def test_the_token_is_replaced_after_a_page_action(connected_bridge, page):
    resource = page(connected_bridge)
    session = new_session()
    used = token(session)

    request, _body = post(resource, session, action_fields("discovery"), csrf=used)
    assert request.response_code == 200
    assert token(session) != used

    request, _body = post(resource, session, action_fields("discovery"), csrf=used)
    assert request.response_code == 403


def test_a_refused_post_keeps_the_token(connected_bridge, page):
    """Said exactly in SETUP.md: the token changes after a change that took effect only."""
    resource = page(connected_bridge)
    session = new_session()
    used = token(session)
    request, _body = post(resource, session, action_fields("volume", level="loud"), csrf=used)
    assert request.response_code == 400
    assert token(session) == used


# ------------------------------------------------------------- a stale form --


def test_a_stale_form_does_not_revert_a_setting_changed_over_mqtt(
    connected_bridge, page, factory, settings
):
    """🔴 Open the page, change `screenshot_delay` over `cmd/config`, save only `log_level`."""
    resource = page(connected_bridge)
    session = new_session()
    _request, body = get(resource, session)
    assert settings.screenshot_delay.value == 4

    send(factory, "config", b'{"publish_keys": true, "screenshot": "on_zap", '
                            b'"screenshot_interval": 60, "screenshot_delay": 12}')
    assert settings.screenshot_delay.value == 12

    request, _body = post(resource, session, submitted(body, log_level="debug"), csrf=None)

    assert request.response_code == 200
    assert settings.log_level.value == "debug"
    assert settings.screenshot_delay.value == 12
    assert settings.screenshot_delay.saved_value == 12


def test_a_stale_form_does_not_regrant_a_permission_revoked_at_the_television(
    connected_bridge, page, settings
):
    settings.deep_standby_allowed.value = True
    settings.deep_standby_allowed.save()
    resource = page(connected_bridge)
    session = new_session()
    _request, body = get(resource, session)

    # Revoked on the setup screen while the page stays open.
    settings.deep_standby_allowed.value = False
    settings.deep_standby_allowed.save()

    request, _body = post(resource, session, submitted(body, log_level="debug"), csrf=None)

    assert request.response_code == 200
    assert settings.log_level.value == "debug"
    assert settings.deep_standby_allowed.value is False
    assert settings.deep_standby_allowed.saved_value is False


def test_a_field_changed_on_a_stale_form_still_wins(connected_bridge, page, factory, settings):
    """The fix skips untouched fields only: a field the user did edit is applied."""
    resource = page(connected_bridge)
    session = new_session()
    _request, body = get(resource, session)
    send(factory, "config", b'{"publish_keys": true, "screenshot": "on_zap", '
                            b'"screenshot_interval": 60, "screenshot_delay": 12}')

    request, _body = post(resource, session, submitted(body, screenshot_delay="7"), csrf=None)

    assert request.response_code == 200
    assert settings.screenshot_delay.value == 7


# ------------------------------------------------------------ what the page shows --


def test_every_setting_is_on_the_form_and_nothing_else(connected_bridge, page):
    _request, body = get(page(connected_bridge))
    names = re.findall(r"name='([^']+)'", settings_form(body))
    assert set(names) == {"form", "csrf", "rendered"} | set(SETTING_NAMES)
    assert b"oscam_identity_salt" not in body


def test_every_setting_is_placed_in_exactly_one_group():
    placed = [name for _group, names in webif.SETTING_GROUPS for name in names]
    assert sorted(placed) == sorted(SETTING_NAMES)


def test_the_permissions_and_kill_switches_are_one_group():
    groups = dict(webif.SETTING_GROUPS)
    assert set(groups["permissions"]) == {
        "deep_standby_allowed", "wol_arm", "softcam_restart_allowed", "epg_import_allowed",
        "uninstall_allowed", "cec_standby_workaround", "osd_toast",
    }


def test_the_page_says_wake_on_lan_is_not_available_here(connected_bridge, page):
    """The label is the setup screen's, and on a receiver without the image's
    switch it says the setting does nothing, on the page as on the television."""
    _request, body = get(page(connected_bridge))
    assert "Wake-on-LAN is not available on this receiver" in settings_form(body)


def test_wol_arm_saved_on_the_page_restarts_the_bridge_and_is_applied(
    connected_bridge, page, factory, monkeypatch
):
    """Not `cmd/config`'s path: the setup screen's, whose restart is what arms."""
    from MQTTBridge import wol

    armed = []
    monkeypatch.setattr(wol, "arm", lambda section=None: armed.append(section) or False)
    request, _body = post(
        page(connected_bridge), new_session(),
        settings_fields(connected_bridge.settings, wol_arm=True),
    )

    assert request.response_code == 200
    assert connected_bridge.settings.wol_arm.saved_value is True
    assert armed == [connected_bridge.settings]


def test_no_secret_is_ever_rendered(connected_bridge, page, settings, monkeypatch, tmp_path):
    settings.password.value = "broker-pw-marker"
    settings.oscam_password.value = "oscam-pw-marker"
    settings.oscam_identity_salt.value = "salt-marker"
    log = tmp_path / "bridge.log"
    log.write_text(
        "password=broker-pw-marker\n"
        'payload={"password": "oscam-pw-marker"}\n', encoding="utf-8",
    )
    monkeypatch.setattr(webif.log_module, "active_path", lambda: str(log))
    connected_bridge.publish_json(ROOT + "/info", connected_bridge.build_info())
    # A payload is shown as it went out, so a secret that reached one — it
    # must not, but the page is the last line — is redacted on the way in.
    webif.log_module.register_secret("broker-pw-marker")
    connected_bridge.publish_json(ROOT + "/probe", {"echo": "broker-pw-marker"})

    _request, body = get(page(connected_bridge))

    for marker in (b"broker-pw-marker", b"oscam-pw-marker", b"salt-marker"):
        assert marker not in body
    assert b"name='password' value=''" in body
    assert b"name='oscam_password' value=''" in body


def test_a_blank_secret_is_unchanged_and_a_filled_one_is_set(connected_bridge, page, settings):
    resource = page(connected_bridge)
    session = new_session()
    settings.password.value = "old-password"
    settings.password.save()

    request, _body = post(resource, session, settings_fields(connected_bridge.settings))
    assert request.response_code == 200
    assert settings.password.value == "old-password"

    request, _body = post(
        resource, session, settings_fields(connected_bridge.settings, password="new-password")
    )
    assert request.response_code == 200
    assert settings.password.value == "new-password"
    assert settings.password.saved_value == "new-password"


def test_the_status_carries_what_the_bridge_holds(connected_bridge, page, factory):
    connected_bridge.publish_last_error("zap", "no service reference or name given")
    _request, body = get(page(connected_bridge))
    text = body.decode("utf-8")
    assert "10.0.0.5:1883" in text
    assert NODE in text
    assert "no service reference or name given" in text
    assert "dispatch_pending" in text
    assert "deep_standby_allowed" in text
    assert html.escape(INFO) in text


def test_the_page_shows_the_last_payload_as_it_went_out(connected_bridge, page):
    connected_bridge.publish_state("process", {"rss": 1, "ts": 100}, volatile=("ts",))
    connected_bridge.publish_state("process", {"rss": 2, "ts": 300}, volatile=("ts",))
    connected_bridge.publish_raw(ROOT + "/screen", b"\xff\xd8not-json")

    _request, body = get(page(connected_bridge))
    text = html.unescape(body.decode("utf-8"))

    # With the timestamp, which the bridge's change key leaves out.
    assert '{"rss":2,"ts":300}' in text
    assert ROOT + "/screen: published, not shown" in text
    assert "not-json" not in text


def test_the_remembered_payloads_are_bounded(connected_bridge, monkeypatch):
    from MQTTBridge import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "REMEMBERED_TOPICS", 3)
    monkeypatch.setattr(bridge_module, "REMEMBERED_BYTES", 20)
    for number in range(5):
        connected_bridge.publish_json(ROOT + "/t" + str(number), {"n": "x" * 40})
    json_rows = [row for row in connected_bridge.last_payloads() if row[1] is not None]
    assert [topic for topic, _payload in json_rows] == [ROOT + "/t2", ROOT + "/t3", ROOT + "/t4"]
    assert all(len(payload) == 20 and payload.endswith("…") for _topic, payload in json_rows)


def test_a_secret_field_reveals_nothing_not_even_its_length(connected_bridge, page, settings):
    settings.password.value = "twelve-chars"
    settings.oscam_password.value = "x"
    _request, body = get(page(connected_bridge))
    for name in ("password", "oscam_password"):
        tags = re.findall(r"<input type='password' name='" + name + "'[^>]*>", body.decode())
        assert tags == [
            "<input type='password' name='" + name + "' value='' maxlength='128' "
            "autocomplete='new-password'>"
        ]


def test_what_broker_clients_can_influence_is_escaped(connected_bridge, page):
    """`last_error` echoes a command's payload, and payloads carry channel names."""
    connected_bridge.publish_last_error("zap", "no channel called <img src=x onerror=alert(1)>")
    connected_bridge.publish_json(ROOT + "/service", {"name": "</pre><script>alert(2)</script>"})

    _request, body = get(page(connected_bridge))

    assert b"<img src=x" not in body
    assert b"<script>" not in body
    assert b"</pre><script>" not in body
    assert b"&lt;img src=x onerror=alert(1)&gt;" in body
    assert b"&lt;/pre&gt;&lt;script&gt;alert(2)" in body


def test_a_retracted_topic_is_forgotten(connected_bridge):
    connected_bridge.publish_json(ROOT + "/cam", {"system": "x"})
    connected_bridge.retract(ROOT + "/cam")
    assert ROOT + "/cam" not in [topic for topic, _payload in connected_bridge.last_payloads()]


# -------------------------------------------------------- what the page changes --


@pytest.mark.parametrize("name", TEXT_SETTINGS)
@pytest.mark.parametrize("bad", ["\n", "\r", "\x00", "\x1b", "\x7f", "\x85", " "])
def test_a_control_character_is_refused_in_every_text_setting(name, bad):
    """🔴 enigma2's settings file is `key=value` lines with no escaping."""
    with pytest.raises(ValueError):
        settings_module.validate_setting(name, "ab" + bad + "config.OpenWebif.auth=False")


@pytest.mark.parametrize("name", TEXT_SETTINGS)
def test_a_newline_in_any_text_setting_changes_nothing(connected_bridge, page, settings, name):
    before = settings_module.value(name, connected_bridge.settings)
    fields = settings_fields(connected_bridge.settings, **{name: "ab\nconfig.x=1"})

    request, body = post(page(connected_bridge), new_session(), fields)

    assert request.response_code == 400
    assert name.encode() in body
    assert settings_module.value(name, connected_bridge.settings) == before
    assert configfile.save_calls == 0


def test_text_is_length_capped():
    settings_module.validate_setting("host", "h" * 128)
    with pytest.raises(ValueError):
        settings_module.validate_setting("host", "h" * 129)
    settings_module.validate_setting("bouquets_for_select", "b" * 1024)
    with pytest.raises(ValueError):
        settings_module.validate_setting("ca_file", "/" * 1025)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("node_id", "Vuuno4kse"),
        ("node_id", "vu-uno"),
        ("node_id", "vu/uno"),
        ("base_topic", "enigma2/+"),
        ("base_topic", "enigma2/#"),
        ("ha_discovery_prefix", "home#assistant"),
        ("ha_discovery_prefix", "home+assistant"),
    ],
)
def test_topic_settings_refuse_what_mqtt_would_misread(name, value):
    with pytest.raises(ValueError):
        settings_module.validate_setting(name, value)


def test_the_limits_table_is_each_elements_own_limits(settings):
    integers = [
        name for name in SETTING_NAMES if settings_module.SETTING_KINDS[name] == "int"
    ]
    assert sorted(settings_module.INTEGER_LIMITS) == sorted(integers)
    for name in integers:
        assert tuple(getattr(settings, name).limits) == settings_module.INTEGER_LIMITS[name], name
        low, high = settings_module.INTEGER_LIMITS[name]
        assert settings_module.validate_setting(name, str(low)) == low
        with pytest.raises(ValueError):
            settings_module.validate_setting(name, str(high + 1))
        with pytest.raises(ValueError):
            settings_module.validate_setting(name, str(low - 1))


def test_a_remote_subset_change_applies_without_a_reload(
    connected_bridge, page, factory, monkeypatch
):
    reloads = []
    monkeypatch.setattr(connected_bridge, "reload", lambda: reloads.append(1))
    clients = len(factory.clients)

    request, body = post(page(connected_bridge), new_session(), _valid_change(connected_bridge))

    assert request.response_code == 200
    assert b"Settings saved." in body
    assert reloads == []
    assert len(factory.clients) == clients
    assert factory.client.last(INFO).json()["settings"]["screenshot_delay"] == 9
    assert configfile.save_calls == 1


def test_any_other_change_writes_the_file_once_and_reloads_once(
    connected_bridge, page, factory, settings, monkeypatch
):
    reloads = []
    real_reload = connected_bridge.reload

    def counted():
        reloads.append(1)
        return real_reload()

    monkeypatch.setattr(connected_bridge, "reload", counted)
    fields = settings_fields(connected_bridge.settings, log_level="debug", screenshot_delay="9")

    request, body = post(page(connected_bridge), new_session(), fields)

    assert request.response_code == 200
    assert b"reconnecting" in body
    assert reloads == [1]
    assert configfile.save_calls == 1
    assert settings.log_level.saved_value == "debug"
    assert settings.screenshot_delay.saved_value == 9


def test_a_permission_changed_on_the_page_reaches_info_and_discovery(
    connected_bridge, page, factory
):
    """Through the stubbed connect, not by inspecting the settings."""
    resource = page(connected_bridge)
    session = new_session()
    assert "deep_standby" not in device(factory).get("cmps", {})

    post(resource, session,
         settings_fields(connected_bridge.settings, deep_standby_allowed=True))
    factory.client.fire_connect()
    assert factory.client.last(INFO).json()["settings"]["deep_standby_allowed"] is True
    assert device(factory)["cmps"]["deep_standby"]["cmd_t"] == ROOT + "/cmd/deep_standby"

    post(resource, session,
         settings_fields(connected_bridge.settings, deep_standby_allowed=False))
    factory.client.fire_connect()
    assert factory.client.last(INFO).json()["settings"]["deep_standby_allowed"] is False
    assert len(device(factory)["cmps"].get("deep_standby", {})) <= 1


def test_nothing_changed_writes_nothing(connected_bridge, page, factory):
    before = factory.client.published[:]
    request, body = post(
        page(connected_bridge), new_session(), settings_fields(connected_bridge.settings)
    )
    assert request.response_code == 200
    assert b"Nothing to save" in body
    assert configfile.save_calls == 0
    assert factory.client.published == before


def test_an_identity_change_waits_for_its_confirmation(connected_bridge, page, settings):
    resource = page(connected_bridge)
    session = new_session()
    fields = settings_fields(connected_bridge.settings, node_id="vuuno4kse_005302",
                             password="typed-secret")

    request, body = post(resource, session, fields)
    assert request.response_code == 200
    assert b"Home Assistant sees a new device" in body
    assert b"typed-secret" not in body
    assert settings.node_id.value == NODE
    assert configfile.save_calls == 0

    request, body = post(resource, session, confirmation(body), csrf=None)
    assert request.response_code == 200
    assert settings.node_id.value == "vuuno4kse_005302"
    assert settings.password.value == "typed-secret"
    assert configfile.save_calls == 1


@pytest.mark.parametrize("tamper", ["action", "payload", "main token", "twice"])
def test_a_confirmation_is_bound_to_its_action_and_payload(
    live_bridge, page, receiver, tamper
):
    resource = page(live_bridge)
    session = new_session()
    _request, body = post(resource, session, action_fields("restart_gui"))
    fields = confirmation(body)
    if tamper == "action":
        fields["action"] = "reboot"
    elif tamper == "payload":
        fields["payload"] = "x"
    elif tamper == "main token":
        fields["csrf"] = token(session)
    elif tamper == "twice":
        request, _body = post(resource, session, fields, csrf=None)
        assert request.response_code == 200
        receiver.session.opened.clear()

    request, _body = post(resource, session, fields, csrf=None)

    assert request.response_code == 403
    assert receiver.session.opened == []


def test_an_idle_bridge_offers_its_settings_and_disables_its_commands(make_bridge, page,
                                                                      factory, settings):
    bridge = make_bridge()
    bridge.start()
    assert bridge.idle_reason == "no broker address is configured"
    resource = page(bridge)
    session = new_session()

    _request, body = get(resource, session)
    assert body.count(b"<fieldset disabled>") == len(webif.actions())
    assert b"no broker address is configured" in body
    request, _body = post(resource, session, action_fields("discovery"))
    assert request.response_code == 409

    # The recovery path: the broker is set from the page and the bridge starts.
    request, body = post(resource, session,
                         settings_fields(bridge.settings, host="192.0.2.50"))
    assert request.response_code == 200, re.findall(r"class='notice'>([^<]*)", body.decode())
    assert bridge.running, (bridge.idle_reason, bridge.value("host"), bridge.value("enabled"))
    assert factory.client is not None


def test_an_idle_bridge_saves_a_cmd_config_change_by_reloading(make_bridge, page, monkeypatch,
                                                               settings):
    """On an idle bridge the remote path would start publishers nothing asked for."""
    bridge = make_bridge()
    bridge.start()
    assert not bridge.running
    remote, reloads = [], []
    monkeypatch.setattr(bridge, "apply_remote_settings", lambda values: remote.append(values))
    monkeypatch.setattr(bridge, "reload", lambda: reloads.append(1))
    writes = configfile.save_calls

    request, body = post(page(bridge), new_session(),
                         settings_fields(bridge.settings, screenshot_delay="9"))

    assert request.response_code == 200
    assert b"reconnecting" in body
    assert remote == []
    assert reloads == [1]
    assert settings.screenshot_delay.saved_value == 9
    assert configfile.save_calls == writes + 1


# ------------------------------------------------------- what the page can do --


def test_every_command_the_dispatcher_knows_has_a_page_action():
    """A command added to the dispatcher without a page action fails here."""
    handlers = set(CommandDispatcher(SimpleNamespace()).handlers)
    covered = {action.command for action in webif.actions()}
    # `config` is the settings form itself.
    assert handlers == covered | {"config"}


def test_the_origin_answers_the_permission_and_nothing_else():
    assert granted(lambda _name: False, "deep_standby_allowed", PAGE) is True
    assert granted(lambda _name: False, "deep_standby_allowed", MQTT) is False
    assert granted(lambda _name: True, "deep_standby_allowed", MQTT) is True
    # Anything that does not say it is the page is the broker.
    assert granted(lambda _name: False, "deep_standby_allowed", "Page") is False
    assert granted(lambda _name: None, "deep_standby_allowed", None) is False


@pytest.mark.parametrize(("key", "mode"), [("deep_standby", 1), ("reboot", 2)])
def test_a_shutdown_from_the_page_runs_without_the_permission(
    live_bridge, page, receiver, settings, key, mode
):
    from Screens.Standby import TryQuitMainloop

    assert settings.deep_standby_allowed.value is False
    resource = page(live_bridge)
    session = new_session()

    request, body = post(resource, session, action_fields(key))
    assert request.response_code == 200
    assert receiver.session.opened == []
    request, body = post(resource, session, confirmation(body), csrf=None)

    assert request.response_code == 200
    assert b"Sent: " in body
    assert receiver.session.opened[-1] == (TryQuitMainloop, (mode,))


def test_uninstall_from_the_page_states_the_one_way_door_and_sends_the_node_id(
    live_bridge, page, monkeypatch
):
    sent = []
    monkeypatch.setattr(live_bridge, "run_command",
                        lambda name, text, origin: sent.append((name, text, origin)))
    resource = page(live_bridge)
    session = new_session()

    request, body = post(resource, session, action_fields("uninstall"))
    assert request.response_code == 200
    assert sent == []
    text = html.unescape(body.decode("utf-8"))
    assert "There is no way back from here or from Home Assistant" in text
    assert "Its settings stay on the receiver." in text
    fields = confirmation(body)
    assert fields["payload"] == NODE

    request, body = post(resource, session, fields, csrf=None)
    assert request.response_code == 200
    assert sent == [("uninstall", NODE, PAGE)]
    assert b"Sent: " in body


def test_uninstall_from_the_page_runs_without_the_permission_and_mqtt_stays_refused(
    live_bridge, page, factory, settings, monkeypatch
):
    assert settings.uninstall_allowed.value is False
    monkeypatch.setattr(live_bridge.uninstaller, "claimed", True)
    resource = page(live_bridge)
    session = new_session()

    _request, body = post(resource, session, action_fields("uninstall"))
    post(resource, session, confirmation(body), csrf=None)
    assert live_bridge.uninstaller.phase == "scheduled"

    live_bridge.uninstaller.abandon()
    live_bridge.uninstaller.phase = None
    send(factory, "uninstall", NODE.encode())
    assert error(factory) == "uninstall is switched off in the plugin's settings"
    assert live_bridge.uninstaller.phase is None


def test_the_page_bypass_is_not_inherited_by_the_next_mqtt_command(
    live_bridge, page, receiver, factory
):
    resource = page(live_bridge)
    session = new_session()
    _request, body = post(resource, session, action_fields("reboot"))
    post(resource, session, confirmation(body), csrf=None)
    assert len(receiver.session.opened) == 1

    send(factory, "reboot")
    assert "switched off" in error(factory)
    assert len(receiver.session.opened) == 1


@pytest.mark.parametrize("key", ["deep_standby", "reboot", "restart_gui"])
def test_a_recording_refuses_a_shutdown_from_both_origins(
    live_bridge, page, receiver, factory, settings, key
):
    settings.deep_standby_allowed.value = True
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    resource = page(live_bridge)
    session = new_session()

    _request, body = post(resource, session, action_fields(key))
    _request, body = post(resource, session, confirmation(body), csrf=None)
    assert b"Refused: the receiver is recording" in body
    assert receiver.session.opened == []

    send(factory, key)
    assert error(factory) == "the receiver is recording"
    assert receiver.session.opened == []


def test_a_page_action_publishes_and_clears_last_error_as_mqtt_does(
    live_bridge, page, factory
):
    resource = page(live_bridge)
    session = new_session()

    send(factory, "message", b'{"text": ""}')
    over_mqtt = error(factory)
    assert over_mqtt
    factory.client.clear()
    request, body = post(resource, session, action_fields(
        "message", text="", type="info", timeout="10", style="popup"))
    assert request.response_code == 200
    assert error(factory) == over_mqtt
    assert ("Refused: " + over_mqtt).encode() in body

    post(resource, session, action_fields("volume", level="30"))
    assert factory.client.last(LAST_ERROR).text == ""


def test_the_page_sends_a_toast_through_the_message_handler(live_bridge, page, factory, settings):
    resource = page(live_bridge)
    session = new_session()
    request, _body = post(resource, session, action_fields(
        "message", text="Pranie gotowe", type="info", timeout="5", style="toast"))
    assert request.response_code == 200
    assert error(factory) is None
    assert Notifications.popups == []
    dialog = live_bridge.publisher("toast")._dialog
    assert dialog["message"].instance.text == "Pranie gotowe"


def test_an_action_payload_is_exactly_what_a_broker_client_would_send(connected_bridge, page,
                                                                       monkeypatch):
    sent = []
    monkeypatch.setattr(connected_bridge, "run_command",
                        lambda name, text, origin: sent.append((name, text, origin)))
    resource = page(connected_bridge)
    post(resource, new_session(), action_fields("zap", by="name", channel="TVP 1 HD"))
    post(resource, new_session(), action_fields("key", key="KEY_RED", long=["false", "true"]))
    post(resource, new_session(), action_fields("volume", level="35"))
    assert sent == [
        ("zap", '{"name":"TVP 1 HD"}', PAGE),
        ("key", '{"key":"KEY_RED","long":true}', PAGE),
        ("volume", "35", PAGE),
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"level": "101"},
        {"level": "loud"},
        {"level": "30", "extra": "x"},
        {},
    ],
)
def test_an_invalid_action_input_runs_nothing(connected_bridge, page, monkeypatch, fields):
    sent = []
    monkeypatch.setattr(connected_bridge, "run_command", lambda *args: sent.append(args))
    request, _body = post(page(connected_bridge), new_session(),
                          action_fields("volume", **fields))
    assert request.response_code == 400
    assert sent == []


@pytest.mark.parametrize(
    ("which", "fields"),
    [
        ("power", {"state": "deep_standby"}),
        ("ha_mode", {"mode": "everything"}),
        ("message", {"text": "x", "type": "info", "timeout": "5", "style": "fullscreen"}),
    ],
)
def test_a_choice_the_page_did_not_offer_runs_nothing(connected_bridge, page, monkeypatch,
                                                      which, fields):
    sent = []
    monkeypatch.setattr(connected_bridge, "run_command", lambda *args: sent.append(args))
    request, _body = post(page(connected_bridge), new_session(), action_fields(which, **fields))
    assert request.response_code == 400
    assert sent == []


def test_the_dispatcher_run_keeps_the_brokers_size_limit(connected_bridge, factory):
    before = factory.client.published[:]
    refusal = connected_bridge.run_command("message", "x" * 5000, PAGE)
    assert "byte limit" in refusal
    assert factory.client.published == before


# ------------------------------------------------ the page inside OpenWebif (§11 ac) --

XHR = {"x-requested-with": "XMLHttpRequest"}
FETCH = {"sec-fetch-dest": "empty"}


@pytest.mark.parametrize("headers", [XHR, FETCH], ids=["jquery", "fetch"])
def test_a_panel_load_gets_the_fragment_and_nothing_more(connected_bridge, page, monkeypatch,
                                                         tmp_path, headers):
    log = tmp_path / "bridge.log"
    log.write_text("a log line that must not leak\n", encoding="utf-8")
    monkeypatch.setattr(webif.log_module, "active_path", lambda: str(log))
    session = new_session()

    request, body = get(page(connected_bridge), session, headers=headers)
    text = body.decode("utf-8")

    assert request.response_code == 200
    assert text.startswith("<div>") and text.endswith("</div>")
    assert re.findall(r"<iframe src='([^']*)'", text) == ["/mqttbridge/"]
    assert text.count("<iframe") == 1
    assert "href='/mqttbridge/' target='_blank' rel='noopener'>Open in a new tab</a>" in text
    for forbidden in ("<script", "<style", "class=", "csrf", "10.0.0.5", NODE,
                      "a log line that must not leak"):
        assert forbidden not in text, forbidden
    assert webif.CSRF_KEY not in session.sessionNamespaces
    assert request.response_headers["content-type"].startswith("text/html")
    assert request.response_headers["cache-control"] == "no-store"
    assert request.response_headers["x-content-type-options"] == "nosniff"


def test_the_fragment_frames_the_mount_the_request_came_by(connected_bridge, page):
    _request, body = get(page(connected_bridge), headers=XHR,
                         prepath=[b"somewhere", b"else", b""])
    assert b"<iframe src='/somewhere/else/'" in body


@pytest.mark.parametrize(
    "headers",
    [{}, {"sec-fetch-dest": "document"}, {"sec-fetch-dest": "iframe"},
     {"x-requested-with": "somethingelse", "sec-fetch-dest": "iframe"}],
    ids=["plain", "document", "iframe", "other-xrw"],
)
def test_every_navigation_gets_the_full_page(connected_bridge, page, headers):
    request, body = get(page(connected_bridge), headers=headers)
    assert request.response_code == 200
    assert b"<form method='post' class='settings'>" in body
    assert b"<iframe" not in body


@pytest.mark.parametrize("headers", [XHR, FETCH], ids=["jquery", "fetch"])
def test_a_post_is_never_answered_with_a_fragment(connected_bridge, page, headers):
    request, body = post(page(connected_bridge), new_session(), action_fields("discovery"),
                         headers=headers)
    assert request.response_code == 200
    assert b"Done: " in body
    assert b"<iframe" not in body


@pytest.mark.parametrize("host", ["receiver.home.example", "proxy.example:8443"])
def test_a_panel_load_under_a_foreign_host_still_gets_the_fragment(connected_bridge, page, host):
    """🔴 jQuery's `.load()` injects nothing on a 421: the menu entry would do nothing.

    The fragment carries no token and no data, and nothing from `Host`; the
    frame it opens is a navigation, and that one meets the Host check.
    """
    request, body = get(page(connected_bridge), _UntouchableSession(), host=host,
                        headers=XHR, prepath=[b"mqttbridge"])
    assert request.response_code == 200
    assert re.findall(rb"<iframe src='([^']*)'", body) == [b"/mqttbridge/"]
    assert host.split(":")[0].encode() not in body


@pytest.mark.parametrize(
    "headers", [{}, {"sec-fetch-dest": "iframe"}, {"sec-fetch-dest": "document"}],
    ids=["plain", "iframe", "document"],
)
def test_the_framed_page_under_a_foreign_host_is_still_421(connected_bridge, page, headers):
    request, body = get(page(connected_bridge), _UntouchableSession(),
                        host="receiver.home.example", headers=headers,
                        prepath=[b"mqttbridge", b""])
    assert request.response_code == 421
    assert b"<form" not in body


def test_a_post_carrying_the_panel_header_under_a_foreign_host_is_421(connected_bridge, page):
    request = _Request(session=_UntouchableSession(), host="receiver.home.example",
                       origin="http://receiver.home.example", headers=XHR,
                       args=encode({"form": "action"}))
    page(connected_bridge).render_POST(request)
    assert request.response_code == 421


@pytest.mark.parametrize(
    "headers",
    [{"x-requested-with": "xmlhttprequest"}, {"x-requested-with": " XMLHttpRequest "},
     {"sec-fetch-dest": "Empty"}, {"sec-fetch-dest": " empty "}],
    ids=["xrw-lower", "xrw-spaced", "dest-capital", "dest-spaced"],
)
def test_the_panel_signal_ignores_case_and_surrounding_space(connected_bridge, page, headers):
    _request, body = get(page(connected_bridge), headers=headers)
    assert body.startswith(b"<div><iframe")


def test_the_421_varies_on_both_headers_too(connected_bridge, page):
    request, _body = get(page(connected_bridge), _UntouchableSession(), host="attacker.example")
    assert request.response_code == 421
    vary = [part.strip().lower() for part in request.response_headers["vary"].split(",")]
    assert "x-requested-with" in vary and "sec-fetch-dest" in vary


@pytest.mark.parametrize("headers", [XHR, FETCH, {}], ids=["jquery", "fetch", "page"])
def test_every_answer_of_the_pages_get_varies_on_both_headers(connected_bridge, page, headers):
    request, _body = get(page(connected_bridge), headers=headers)
    vary = [part.strip().lower() for part in request.response_headers["vary"].split(",")]
    assert "x-requested-with" in vary and "sec-fetch-dest" in vary
    assert request.response_headers["cache-control"] == "no-store"


# ----------------------------------------------------- the screenshot on the page --

PICTURE = b"\xff\xd8\xff\xe0" + b"picture" * 100
SHOT_TOPIC = ROOT + "/screen"


def take(bridge, tmp_path, data=PICTURE, retval=0):
    """One commanded capture through the stubbed `grab`, finished."""
    found = bridge.publisher("screenshot")
    found.path = str(tmp_path / ("grab-" + str(len(ConsoleAppContainer.instances)) + ".jpg"))
    found._last_capture -= 10  # out of the five-second window
    assert found.capture(commanded=True) is None
    if not retval:
        with open(found.path, "wb") as handle:
            handle.write(data)
    ConsoleAppContainer.instances[-1].finish(retval)
    return found


def shot(bridge, host=BOX, uri=b"/mqttbridge/screen.jpg", args=None):
    request = _Request(host=host, uri=uri, prepath=[b"mqttbridge", b"screen.jpg"],
                       args=args)
    body = webif.ScreenshotResource().render_GET(request)
    return request, body


@pytest.fixture
def screen_page(live_bridge, page, monkeypatch):
    monkeypatch.setattr(webif, "_bridge", lambda: live_bridge)
    return page(live_bridge)


def test_screen_jpg_serves_exactly_what_went_out_on_screen(live_bridge, screen_page, factory,
                                                           tmp_path):
    take(live_bridge, tmp_path)
    request, body = shot(live_bridge)
    assert request.response_code == 200
    assert body == factory.client.last(SHOT_TOPIC).payload == PICTURE
    headers = request.response_headers
    assert headers["content-type"] == "image/jpeg"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "SAMEORIGIN"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'self'"
    assert "content-disposition" not in headers


def test_screen_jpg_ignores_a_query_string(live_bridge, screen_page, tmp_path):
    take(live_bridge, tmp_path)
    request, body = shot(live_bridge, uri=b"/mqttbridge/screen.jpg?v=1&after=9",
                         args={b"v": [b"1"], b"after": [b"9"]})
    assert request.response_code == 200
    assert body == PICTURE


def test_screen_jpg_is_404_before_any_capture(live_bridge, screen_page):
    request, body = shot(live_bridge)
    assert request.response_code == 404
    assert not body.startswith(b"\xff\xd8")
    assert request.response_headers["cache-control"] == "no-store"


def test_screen_jpg_is_404_while_the_bridge_is_idle(make_bridge, page):
    bridge = make_bridge()
    bridge.start()
    page(bridge)
    assert not bridge.running
    request, _body = shot(bridge)
    assert request.response_code == 404


def test_screen_jpg_refuses_a_foreign_host(live_bridge, screen_page, tmp_path):
    take(live_bridge, tmp_path)
    request, body = shot(live_bridge, host="attacker.example")
    assert request.response_code == 421
    assert body != PICTURE


def test_the_page_mounts_screen_jpg_beside_the_icon():
    children = webif.MQTTBridgeWebResource().children
    assert isinstance(children[b"screen.jpg"], webif.ScreenshotResource)


def test_the_picture_survives_every_publisher_replacement(live_bridge, screen_page, factory,
                                                          settings, tmp_path):
    """🔴 Every save replaces the publisher, and the picture lived on the publisher."""
    take(live_bridge, tmp_path)
    assert shot(live_bridge)[1] == PICTURE

    values = live_bridge.remote_settings()
    values["screenshot_delay"] = 9
    assert live_bridge.apply_remote_settings(values) is None
    assert shot(live_bridge)[1] == PICTURE

    live_bridge.reload()
    factory.client.fire_connect()
    assert shot(live_bridge)[1] == PICTURE

    values = live_bridge.remote_settings()
    values["screenshot"] = "off"
    assert live_bridge.apply_remote_settings(values) is None
    assert factory.client.last(SHOT_TOPIC).payload in ("", b"")
    assert shot(live_bridge)[0].response_code == 404


def test_a_reset_clears_the_record_and_its_snapshot_sets_it_again(live_bridge, screen_page,
                                                                  monkeypatch, tmp_path):
    take(live_bridge, tmp_path)
    seen = []
    real = live_bridge.publish_snapshot

    def snapshot(info=None):
        seen.append(live_bridge.last_screenshot())
        return real(info)

    monkeypatch.setattr(live_bridge, "publish_snapshot", snapshot)
    live_bridge.reset_retained()

    assert seen == [None]
    assert live_bridge.last_screenshot()[0] == PICTURE


def test_retracting_screen_clears_the_record(live_bridge, screen_page, tmp_path):
    take(live_bridge, tmp_path)
    live_bridge.retract(SHOT_TOPIC)
    assert live_bridge.last_screenshot() is None


def test_screen_jpg_is_404_with_screenshots_off_even_before_a_retraction(
    live_bridge, screen_page, settings, tmp_path
):
    """The setting is read on every request, not only when the topic is retracted."""
    take(live_bridge, tmp_path)
    settings.screenshot.value = "off"
    assert live_bridge.last_screenshot() is not None
    assert shot(live_bridge)[0].response_code == 404


def test_stop_makes_screen_jpg_404(live_bridge, screen_page, tmp_path):
    take(live_bridge, tmp_path)
    live_bridge.stop()
    assert shot(live_bridge)[0].response_code == 404


def refreshes(body):
    return re.findall(r"<meta http-equiv='refresh' content='(\d+);url=([^']*)'>", body.decode())


def test_a_grab_in_flight_refreshes_the_page_to_its_mount(live_bridge, screen_page, tmp_path):
    found = live_bridge.publisher("screenshot")
    found.path = str(tmp_path / "grab.jpg")
    assert found.capture(commanded=True) is None

    _request, body = get(screen_page)

    assert refreshes(body) == [("2", "/mqttbridge/")]
    assert b"Taking a screenshot; this page will refresh by itself." in body
    assert b"<script" not in body


def test_a_grab_in_flight_for_21_seconds_stops_the_refresh(live_bridge, screen_page, tmp_path):
    found = live_bridge.publisher("screenshot")
    found.path = str(tmp_path / "grab.jpg")
    assert found.capture(commanded=True) is None
    found._last_capture -= 21

    _request, body = get(screen_page)

    assert refreshes(body) == []


def test_the_answer_to_the_screenshot_action_itself_refreshes(live_bridge, screen_page,
                                                              tmp_path):
    live_bridge.publisher("screenshot").path = str(tmp_path / "grab.jpg")
    request, body = post(screen_page, new_session(), action_fields("screenshot"))
    assert request.response_code == 200
    assert refreshes(body) == [("2", "/mqttbridge/")]


def test_a_finished_capture_is_shown_with_its_time(live_bridge, screen_page, tmp_path,
                                                   monkeypatch):
    import time as time_module

    from MQTTBridge import screen as screen_module

    monkeypatch.setattr(screen_module, "time", SimpleNamespace(time=lambda: 1789459200.4))
    found = take(live_bridge, tmp_path)
    assert found.completed_at == 1789459200.4

    _request, body = get(screen_page)
    text = body.decode("utf-8")

    assert refreshes(body) == []
    assert "<img src='/mqttbridge/screen.jpg?v=1789459200'" in text
    expected = time_module.strftime("%H:%M:%S", time_module.localtime(1789459200.4))
    assert "Screenshot from " + expected in text
    assert "href='/mqttbridge/screen.jpg?v=1789459200'>Full size</a>" in text
    assert "<script" not in text


def test_with_screenshots_off_the_figure_says_so(connected_bridge, page, settings):
    settings.screenshot.value = "off"
    _request, body = get(page(connected_bridge))
    assert b"Screenshots are switched off in the settings." in body
    assert b"screen.jpg" not in body


def test_before_any_capture_the_figure_says_there_is_none(live_bridge, screen_page):
    _request, body = get(screen_page)
    assert b"No screenshot to show yet." in body
    assert b"screen.jpg" not in body


def test_screen_jpg_says_why_there_is_nothing(live_bridge, make_bridge, page, screen_page,
                                               settings, tmp_path):
    request, body = shot(live_bridge)
    assert (request.response_code, body) == (404, b"No screenshot to show yet.")

    settings.screenshot.value = "off"
    request, body = shot(live_bridge)
    assert (request.response_code, body) == (404, b"Screenshots are switched off in the settings.")

    settings.screenshot.value = "on_zap"
    live_bridge.stop()
    request, body = shot(live_bridge)
    assert (request.response_code, body) == (404, b"Screenshots are not available right now.")


def test_after_a_save_and_a_reset_the_page_does_not_claim_a_restart(live_bridge, screen_page,
                                                                    tmp_path):
    """The replaced publisher has no picture to republish, so the record stays empty."""
    take(live_bridge, tmp_path)
    values = live_bridge.remote_settings()
    values["screenshot_delay"] = 9
    live_bridge.apply_remote_settings(values)
    live_bridge.reset_retained()
    assert live_bridge.last_screenshot() is None
    _request, body = get(screen_page)
    assert b"No screenshot to show yet." in body
    assert b"since the plugin started" not in body


def test_a_base_topic_rename_drops_the_old_picture(live_bridge, screen_page, factory, settings,
                                                   tmp_path):
    """The old picture is retracted with the rest of the old name, and not shown."""
    take(live_bridge, tmp_path)
    settings.base_topic.value = "renamed"
    assert live_bridge.last_screenshot() is None
    live_bridge.retract_stale()
    assert live_bridge._screenshot is None
    settings.base_topic.value = "enigma2"
    assert live_bridge.last_screenshot() is None


def test_an_empty_screen_payload_is_not_a_picture(live_bridge, screen_page, tmp_path):
    take(live_bridge, tmp_path)
    live_bridge.publish_raw(SHOT_TOPIC, b"")
    assert live_bridge.last_screenshot()[0] == PICTURE
    live_bridge.retract(SHOT_TOPIC)
    live_bridge.publish_raw(SHOT_TOPIC, "")
    assert live_bridge.last_screenshot() is None


def test_a_grab_that_seems_to_start_in_the_future_does_not_refresh(live_bridge, screen_page,
                                                                   tmp_path):
    """A clock stepped back behind a hung grab must not refresh the page for ever."""
    found = live_bridge.publisher("screenshot")
    found.path = str(tmp_path / "grab.jpg")
    assert found.capture(commanded=True) is None
    found._last_capture += 60

    _request, body = get(screen_page)

    assert refreshes(body) == []


def test_the_refresh_stops_when_the_bridge_is_not_running(live_bridge, screen_page, tmp_path):
    found = live_bridge.publisher("screenshot")
    found.path = str(tmp_path / "grab.jpg")
    assert found.capture(commanded=True) is None
    live_bridge.running = False

    _request, body = get(screen_page)

    assert refreshes(body) == []


def test_a_failed_capture_keeps_the_previous_picture_and_reports(live_bridge, screen_page,
                                                                 factory, tmp_path):
    first = take(live_bridge, tmp_path)
    taken = first.completed_at
    take(live_bridge, tmp_path, retval=1)

    assert live_bridge.last_screenshot() == (PICTURE, taken)
    assert shot(live_bridge)[1] == PICTURE
    assert "exited with code 1" in error(factory)
    _request, body = get(screen_page)
    assert ("screen.jpg?v=" + str(int(taken))).encode() in body
    assert refreshes(body) == []


# ------------------------------------------------------------------ the answers --


def test_every_answer_is_uncached_unframeable_and_script_free(live_bridge, page):
    resource = page(live_bridge)
    session = new_session()
    answers = []
    answers.append(get(resource, session))
    answers.append(get(resource, session, host="attacker.example"))
    answers.append(post(resource, session, action_fields("discovery"), csrf="wrong"))
    answers.append(post(resource, session, action_fields("reset")))
    icon = _Request(session=session)
    answers.append((icon, webif.PluginIconResource().render_GET(icon)))
    for request, body in answers:
        assert request.response_headers["cache-control"] == "no-store"
        assert "frame-ancestors 'self'" in request.response_headers["content-security-policy"]
        assert request.response_headers["x-frame-options"] == "SAMEORIGIN"
        # Never `no-referrer`: with it a browser posts the form with `Origin: null`.
        assert request.response_headers["referrer-policy"] == "same-origin"
        assert b"<script" not in body.lower()


def test_get_is_a_real_tool_but_never_mutates(connected_bridge, page, factory, tmp_path,
                                              monkeypatch):
    log = tmp_path / "bridge.log"
    log.write_text("normal line\n", encoding="utf-8")
    monkeypatch.setattr(webif.log_module, "active_path", lambda: str(log))
    before = factory.client.published[:]
    request, body = get(page(connected_bridge))

    assert b"MQTT Bridge" in body
    assert b"plugin.png" not in body
    assert b"/mqttbridge/icon" in body
    assert b"normal line" in body
    assert factory.client.published == before
    assert configfile.save_calls == 0
    assert request.response_headers["cache-control"] == "no-store"


def test_the_icon_url_follows_the_path_the_page_was_reached_by(connected_bridge, page,
                                                                monkeypatch):
    """OpenWebif owns the mount point; the page must not assume its own name."""
    monkeypatch.setattr(webif.log_module, "active_path", lambda: None)
    resource = page(connected_bridge)

    _request, body = get(resource, prepath=[b"somewhere", b"else", b""])
    assert b"src='/somewhere/else/icon'" in body

    _request, body = get(resource)
    assert b"src='/mqttbridge/icon'" in body


def test_the_page_answers_with_and_without_the_trailing_slash(connected_bridge, page,
                                                              monkeypatch):
    """🔴 `/mqttbridge/` resolves to an empty child; without one it is a 404."""
    monkeypatch.setattr(webif.log_module, "active_path", lambda: None)
    resource = page(connected_bridge)

    assert resource.children[b""] is resource
    # And the icon URL that page builds still points at the icon, not at itself.
    _request, body = get(resource, prepath=[b"mqttbridge", b""])
    assert b"src='/mqttbridge/icon'" in body


def test_a_page_that_cannot_be_built_is_the_plugins_own_failure_page(connected_bridge, page,
                                                                      monkeypatch, plugin_log):
    """OpenWebif would otherwise render this plugin's traceback to a browser."""
    monkeypatch.setattr(webif, "bounded_log_tail", lambda *_a, **_k: 1 / 0)

    request, body = get(page(connected_bridge))

    assert request.response_code == 500
    assert b"could not be built" in body
    assert b"Traceback" not in body
    assert "the MQTT Bridge status page could not be rendered" in plugin_log()


def test_the_icon_serves_the_bundled_png_without_a_login():
    """Was `test_authenticated_icon_resource_serves_the_bundled_png`: the login is gone."""
    request = _Request()

    body = webif.PluginIconResource().render_GET(request)

    assert body.startswith(PNG)
    assert request.response_headers["content-type"] == "image/png"
    assert request.response_headers["cache-control"] == "no-store"


def test_log_tail_is_bounded_redacted_and_html_escaped(connected_bridge, page, monkeypatch,
                                                      tmp_path):
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

    _request, body = get(page(connected_bridge))

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


def test_log_tail_refuses_symlinks(tmp_path):
    target = tmp_path / "target"
    target.write_text("should not be read", encoding="utf-8")
    link = tmp_path / "bridge.log"
    link.symlink_to(target)

    assert webif.bounded_log_tail(str(link)) == "Logging is unavailable."


# --------------------------------------------------------------------- the hook --


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
    # 🔴 `"_self"`: OpenWebif loads the page into its own panel, and the page
    # answers that load with a fragment framing itself (ADR-0010). Anything else
    # would be a new-tab link and no panel at all.
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


# ------------------------------------------- a page save while the plugin removes itself --


def _accept_uninstall(bridge, monkeypatch):
    """`cmd/uninstall` accepted from the page: scheduled, the teardown not yet begun."""
    monkeypatch.setattr(bridge.uninstaller, "claimed", True)
    assert bridge.run_command("uninstall", NODE, PAGE) is None
    assert bridge.uninstaller.phase == "scheduled"


@pytest.mark.parametrize(
    "change",
    [{"log_level": "debug"}, {"screenshot_delay": "9"}],
    ids=["reconnecting setting", "cmd-config setting"],
)
def test_a_page_save_after_the_uninstall_was_accepted_does_not_lose_it(
    live_bridge, page, factory, settings, monkeypatch, change
):
    """Between acceptance and the teardown's first turn a reload would leave the
    teardown an unconnected session, and it would fail."""
    from conftest import MainLoop

    _accept_uninstall(live_bridge, monkeypatch)
    clients = len(factory.clients)
    published = len(factory.client.published)

    request, body = post(page(live_bridge), new_session(),
                         settings_fields(live_bridge.settings, **change))

    assert request.response_code == 200
    assert "being removed from this receiver" in html.unescape(body.decode("utf-8"))
    assert len(factory.clients) == clients
    assert len(factory.client.published) == published
    name, value = next(iter(change.items()))
    assert str(getattr(settings, name).saved_value) == value

    MainLoop.advance(0)
    assert live_bridge.uninstaller.phase == "retracting"


def test_a_page_save_after_a_failed_removal_applies_as_usual(live_bridge, page, factory,
                                                             monkeypatch):
    _accept_uninstall(live_bridge, monkeypatch)
    live_bridge.uninstaller._fail("the uninstall stopped: a test")
    factory.client.fire_connect()
    clients = len(factory.clients)

    request, body = post(page(live_bridge), new_session(),
                         settings_fields(live_bridge.settings, log_level="debug"))

    assert request.response_code == 200
    assert b"reconnecting" in body
    assert len(factory.clients) == clients + 1
