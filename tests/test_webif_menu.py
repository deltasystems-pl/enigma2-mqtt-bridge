"""The OpenWebif page's Menu subpage: the menu settings, the creator, its files and its save.

`<mount>/menu` is the one page with a script (ADR-0022): `creator.js`, served from this origin
under a CSP that admits nothing else. The main page keeps its policy byte for byte and stays
script-free. The creator reads `catalogue.json` and posts to `save`, which holds the request
until Home Assistant answers on MQTT, or ten seconds pass.
"""

import html
import json
import re
from pathlib import Path

import pytest
from conftest import MainLoop
from test_menu import World
from test_menueditor import CATALOGUE_TOPIC, EDIT_TOPIC, NEW_REVISION, RESULT_TOPIC, REVISION
from test_menueditor import catalogue as a_catalogue
from test_webif import (
    BOX,
    _Request,
    encode,
    get,
    menu_child,
    menu_get,
    new_session,
    settings_form,
    token,
)

from MQTTBridge import menueditor, webif

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "src" / "MQTTBridge" / "creator.js"
MAIN_CSP = (
    "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; "
    "form-action 'self'; frame-ancestors 'self'"
)
MENU_CSP = (
    "default-src 'none'; script-src 'self'; connect-src 'self'; img-src 'self'; "
    "style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'self'; base-uri 'none'"
)
DATA_CSP = "default-src 'none'; frame-ancestors 'none'"


class _Deferred:
    def __init__(self):
        self.errbacks = []

    def addErrback(self, function):
        self.errbacks.append(function)
        return self


class _HeldRequest(_Request):
    """What Twisted gives a resource that answers later: `write`, `finish`, `notifyFinish`."""

    def __init__(self, **kwargs):
        _Request.__init__(self, **kwargs)
        self.written = []
        self.finished = False
        self.finish_deferred = _Deferred()

    def write(self, data):
        assert not self.finished
        self.written.append(data)

    def finish(self):
        self.finished = True

    def notifyFinish(self):
        return self.finish_deferred

    def lose_connection(self):
        for errback in self.finish_deferred.errbacks:
            errback(None)

    def answer(self):
        assert self.finished, "the request is still held"
        return json.loads(b"".join(self.written))


@pytest.fixture
def world(make_bridge, factory, settings, monkeypatch):
    webif.mounted()
    world = World(make_bridge, factory, settings)
    monkeypatch.setattr(webif, "_bridge", lambda: world.bridge)
    world.resource = webif.MQTTBridgeWebResource()
    return world


def page(world, session=None, **kwargs):
    return menu_get(world.resource, session, **kwargs)


def save_fields(session, menu=None, base=REVISION, **changes):
    fields = {"csrf": token(session), "form": "menu_edit", "base": base,
              "menu": json.dumps([] if menu is None else menu)}
    fields.update(changes)
    return {key: value for key, value in fields.items() if value is not None}


def save(world, session, fields=None, **kwargs):
    fields = save_fields(session) if fields is None else fields
    kwargs.setdefault("prepath", (b"mqttbridge", b"menu", b"save"))
    kwargs.setdefault("uri", b"/mqttbridge/menu/save")
    request = _HeldRequest(session=session, args=encode(fields), **kwargs)
    return request, menu_child(world.resource, "save").render_POST(request)


def answer(world, result="saved", **fields):
    ident = world.client.all_for(EDIT_TOPIC)[-1].json()["id"]
    payload = {"id": ident, "result": result, "revision": NEW_REVISION, "path": None,
               "detail": None}
    payload.update(fields)
    world.client.fire_message(RESULT_TOPIC, json.dumps(payload).encode("utf-8"))
    return ident


def scripts(body):
    return re.findall(r"<script([^>]*)>(.*?)</script>", body.decode("utf-8"), re.S)


def creator_attributes(body):
    mount = re.search(r"<div id='creator'([^>]*)>", body.decode("utf-8")).group(1)
    return {name: html.unescape(value) for name, value in re.findall(r" (data-[a-z]+)='([^']*)'",
                                                                      mount)}


# ----------------------------------------------------------------- the main page --


def test_the_main_page_keeps_its_policy_byte_for_byte_and_no_script(world):
    request, body = get(world.resource)
    assert request.response_headers["content-security-policy"] == MAIN_CSP
    assert webif.PAGE_CSP == MAIN_CSP
    assert b"<script" not in body
    assert b"creator.js" not in body


def test_the_main_page_shows_the_menu_settings_and_links_to_the_subpage(world, settings):
    settings.menu_hotkey.value = "f3"
    settings.menu_transparency.value = 40
    _request, body = get(world.resource)
    text = html.unescape(body.decode("utf-8"))
    assert "Home Assistant menu: On. Key: F3. Background transparency: 40 %." in text
    assert "<a href='/mqttbridge/menu'>Menu settings and the menu creator</a>" in text
    for name in ("menu_allowed", "menu_hotkey", "menu_transparency"):
        assert "name='" + name + "'" not in settings_form(body)


def test_a_menu_setting_posted_on_the_main_form_is_an_extra_field(world, settings):
    from test_webif import post, settings_fields

    session = new_session()
    request, _body = post(world.resource, session,
                          settings_fields(world.bridge.settings, menu_transparency="40"))
    assert request.response_code == 400
    assert settings.menu_transparency.value == 20


# ---------------------------------------------------------------- the subpage --


def test_the_subpage_has_its_own_policy_and_the_usual_headers(world):
    request, body = page(world)
    headers = request.response_headers
    assert request.response_code == 200
    assert headers["content-security-policy"] == MENU_CSP
    assert headers["x-frame-options"] == "SAMEORIGIN"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-type"] == "text/html; charset=utf-8"


def test_the_subpage_runs_one_script_from_a_file_and_none_inline(world):
    _request, body = page(world)
    found = scripts(body)
    assert found == [(" src='/mqttbridge/menu/creator.js'", "")]
    text = body.decode("utf-8")
    assert re.search(r"\son[a-z]+=", text) is None
    assert "javascript:" not in text
    assert "<noscript><p class='notice'>The menu creator needs JavaScript." in text


def test_the_creator_mount_carries_its_addresses_token_icons_bounds_and_texts(world):
    session = new_session()
    _request, body = page(world, session)
    attributes = creator_attributes(body)
    assert attributes["data-catalogue"] == "/mqttbridge/menu/catalogue.json"
    assert attributes["data-save"] == "/mqttbridge/menu/save"
    assert attributes["data-csrf"] == token(session)
    assert "lamp" in json.loads(attributes["data-icons"])
    assert json.loads(attributes["data-limits"]) == {"groups": 8, "top": 24, "rows": 24,
                                                     "text": 60}
    texts = json.loads(attributes["data-texts"])
    assert texts == webif.creator_texts()
    for result in ("saved", "stale", "invalid", "busy", "not_claimed", "no_answer",
                   "not_connected", "not_allowed", "unreadable", "too_big", "rejected",
                   "out_of_date", "unknown"):
        assert texts["result_" + result], result


def test_the_subpage_under_a_foreign_host_is_421(world):
    request, _body = page(world, host="attacker.example")
    assert request.response_code == webif.MISDIRECTED_REQUEST


@pytest.mark.parametrize("headers", [{"x-requested-with": "XMLHttpRequest"},
                                     {"sec-fetch-dest": "empty"}])
def test_the_subpage_is_never_answered_with_the_panel_fragment(world, headers):
    _request, body = page(world, headers=headers)
    assert body.startswith(b"<!doctype html>")
    assert b"<iframe" not in body


def test_the_status_line_names_the_permission_broker_home_assistant_and_catalogue(world):
    world.client.fire_message(CATALOGUE_TOPIC, json.dumps(a_catalogue(left_out=3)).encode(),
                              retain=True)
    world.status("online")
    _request, body = page(world)
    text = html.unescape(body.decode("utf-8"))
    assert "<dt>Home Assistant menu</dt><dd>On</dd>" in text
    assert "<dt>MQTT</dt><dd>Connected</dd>" in text
    assert "<dt>Home Assistant</dt><dd>online</dd>" in text
    assert re.search(r"<dt>Catalogue</dt><dd>2 entities, received \d+ s ago</dd>", text)
    assert "<dt>Entities left out of the catalogue</dt><dd>3</dd>" in text


def test_with_the_menu_off_the_subpage_shows_the_settings_and_says_so(make_bridge, factory,
                                                                     settings, monkeypatch):
    webif.mounted()
    world = World(make_bridge, factory, settings, menu_allowed=False)
    monkeypatch.setattr(webif, "_bridge", lambda: world.bridge)
    world.resource = webif.MQTTBridgeWebResource()
    _request, body = page(world)
    text = html.unescape(body.decode("utf-8"))
    assert "The menu is switched off on this receiver: switch it on above" in text
    assert scripts(body) == []
    assert "name='menu_allowed'" in settings_form(body)


# ----------------------------------------------------- the subpage's settings form --


def menu_settings(section, **overrides):
    from MQTTBridge import config as settings_module

    fields = {"form": "menu_settings",
              "rendered": webif._rendered_values(section, settings_module.MENU_SETTING_NAMES),
              "menu_allowed": ["false", "true"] if section.menu_allowed.value else ["false"],
              "menu_hotkey": section.menu_hotkey.value,
              "menu_transparency": str(settings_module.value("menu_transparency", section))}
    for name, value in overrides.items():
        if isinstance(value, bool):
            value = ["false", "true"] if value else ["false"]
        fields[name] = value
    return fields


def post_menu(world, session, fields, **kwargs):
    fields = dict(fields)
    fields["rendered"] = webif._seal(token(session), fields["rendered"])
    fields.setdefault("csrf", token(session))
    kwargs.setdefault("prepath", (b"mqttbridge", b"menu"))
    request = _Request(session=session, args=encode(fields), **kwargs)
    return request, menu_child(world.resource).render_POST(request)


def test_the_transparency_saved_on_the_subpage_applies_without_a_reconnect(world, factory,
                                                                          settings):
    clients = len(factory.clients)
    request, body = post_menu(world, new_session(),
                              menu_settings(world.bridge.settings, menu_transparency="60"))
    assert request.response_code == 200, body[:300]
    assert b"Settings saved." in body
    assert settings.menu_transparency.saved_value == 60
    assert len(factory.clients) == clients
    assert world.info()["settings"]["menu_transparency"] == 60
    # The answer is the subpage again, under its own policy.
    assert request.response_headers["content-security-policy"] == MENU_CSP


def test_a_subpage_save_keeps_the_other_settings_of_the_bridges_own_section(
    make_bridge, factory, settings, monkeypatch
):
    """The fallback for every key the save leaves out is read from the section it writes."""
    from Components.config import config

    from MQTTBridge import config as settings_module

    shared = config.plugins.mqttbridge
    del config.plugins.mqttbridge
    try:
        own = settings_module._build()
    finally:
        config.plugins.mqttbridge = shared
    for name in settings_module.SETTING_NAMES:
        getattr(own, name).value = getattr(shared, name).value
    own.host.value = "10.0.0.5"
    own.node_id.value = "vuuno4kse_005301"
    own.screenshot_delay.value = 11
    bridge = make_bridge(settings=own)
    bridge.start()
    factory.client.fire_connect()
    monkeypatch.setattr(webif, "_bridge", lambda: bridge)
    world = type("Page", (), {"resource": webif.MQTTBridgeWebResource()})()

    request, _body = post_menu(world, new_session(), menu_settings(own, menu_transparency="30"))

    assert request.response_code == 200
    assert own.menu_transparency.saved_value == 30
    assert own.screenshot_delay.value == 11
    assert settings.menu_transparency.value == 20


def test_switching_the_menu_off_on_the_subpage_reconnects(world, factory, settings):
    clients = len(factory.clients)
    request, _body = post_menu(world, new_session(),
                               menu_settings(world.bridge.settings, menu_allowed=False))
    assert request.response_code == 200
    assert settings.menu_allowed.saved_value is False
    assert len(factory.clients) == clients + 1


def test_the_subpage_form_replaces_the_token_like_the_main_form(world):
    session = new_session()
    before = token(session)
    post_menu(world, session, menu_settings(world.bridge.settings, menu_hotkey="f4"))
    assert token(session) != before


@pytest.mark.parametrize("case, code", [
    ("no origin", 403), ("bad token", 403), ("extra field", 400), ("missing field", 400),
    ("main form", 400), ("transparency between steps", 400), ("foreign host", 421),
])
def test_a_refused_subpage_post_changes_nothing(world, settings, case, code):
    from Components.config import configfile

    session = new_session()
    fields = menu_settings(world.bridge.settings, menu_transparency="60")
    options = {}
    if case == "no origin":
        options["origin"] = None
    elif case == "bad token":
        fields["csrf"] = "x" * 43
    elif case == "extra field":
        fields["host"] = "192.0.2.99"
    elif case == "missing field":
        del fields["menu_hotkey"]
    elif case == "main form":
        fields["form"] = "settings"
    elif case == "transparency between steps":
        fields["menu_transparency"] = "65"
    elif case == "foreign host":
        options["host"] = "attacker.example"
    request, _body = post_menu(world, session, fields, **options)
    assert request.response_code == code
    assert settings.menu_transparency.value == 20
    assert configfile.save_calls == 0


# ------------------------------------------------------------------- creator.js --


def test_the_script_is_the_shipped_file_as_javascript_never_sniffed(world):
    request, body = page(world, name="creator.js")
    headers = request.response_headers
    assert body == SCRIPT.read_bytes()
    assert headers["content-type"] == "text/javascript; charset=utf-8"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"] == DATA_CSP


def test_the_script_under_a_foreign_host_is_421(world):
    request, _body = page(world, name="creator.js", host="attacker.example")
    assert request.response_code == webif.MISDIRECTED_REQUEST


# --------------------------------------------------------------- catalogue.json --


def test_without_a_catalogue_the_file_says_why(world):
    request, body = page(world, name="catalogue.json")
    assert json.loads(body) == {"catalogue": None, "reason": "none"}
    headers = request.response_headers
    assert headers["content-type"] == "application/json"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"] == DATA_CSP


@pytest.mark.parametrize("headers", [{}, {"sec-fetch-site": "same-origin"},
                                     {"sec-fetch-site": "same-origin", "sec-fetch-dest": "empty",
                                      "x-requested-with": "XMLHttpRequest"}])
def test_the_catalogue_is_served_as_the_exact_bytes_received(world, headers):
    raw = json.dumps(a_catalogue(), ensure_ascii=False).encode("utf-8") + b"  "
    world.client.fire_message(CATALOGUE_TOPIC, raw, retain=True)
    request, body = page(world, name="catalogue.json", headers=headers)
    assert request.response_code == 200
    assert body == raw


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none"])
def test_the_catalogue_is_refused_to_another_site(world, site):
    world.client.fire_message(CATALOGUE_TOPIC, json.dumps(a_catalogue()).encode(), retain=True)
    request, body = page(world, name="catalogue.json", headers={"sec-fetch-site": site})
    assert request.response_code == 403
    assert json.loads(body) == {"catalogue": None, "reason": "rejected"}


def test_the_catalogue_under_a_foreign_host_is_421(world):
    request, _body = page(world, name="catalogue.json", host="attacker.example")
    assert request.response_code == webif.MISDIRECTED_REQUEST


def test_with_the_menu_off_the_catalogue_says_so(make_bridge, factory, settings, monkeypatch):
    world = World(make_bridge, factory, settings, menu_allowed=False)
    monkeypatch.setattr(webif, "_bridge", lambda: world.bridge)
    world.resource = webif.MQTTBridgeWebResource()
    _request, body = page(world, name="catalogue.json")
    assert json.loads(body) == {"catalogue": None, "reason": "menu_off"}


def test_a_newer_catalogue_says_so(world):
    world.client.fire_message(CATALOGUE_TOPIC, json.dumps(a_catalogue(v=2)).encode())
    _request, body = page(world, name="catalogue.json")
    assert json.loads(body) == {"catalogue": None, "reason": "newer"}


# ------------------------------------------------------------------------ save --


def test_a_save_is_held_until_home_assistant_answers(world):
    session = new_session()
    menu = [{"type": "group", "id": None, "label": "Garden", "icon": "garden", "rows": []}]
    request, body = save(world, session, save_fields(session, menu))
    assert body == webif.NOT_DONE_YET
    assert not request.finished
    sent = world.client.all_for(EDIT_TOPIC)[-1].json()
    assert sent["base"] == REVISION and sent["menu"] == menu
    assert request.response_headers["content-type"] == "application/json"
    assert request.response_headers["content-security-policy"] == DATA_CSP

    answer(world, "invalid", path="menu[0].label", detail="The label is empty.")

    assert request.answer() == {"result": "invalid", "revision": NEW_REVISION,
                                "path": "menu[0].label", "detail": "The label is empty."}


def test_a_second_save_from_the_same_page_needs_no_reload(world):
    session = new_session()
    before = token(session)
    first, _body = save(world, session)
    answer(world, "saved")
    second, body = save(world, session, save_fields(session, base=NEW_REVISION))
    assert body == webif.NOT_DONE_YET
    answer(world, "stale")
    assert first.answer()["result"] == "saved"
    assert second.answer()["result"] == "stale"
    assert token(session) == before


def test_no_answer_within_ten_seconds_answers_the_page(world):
    request, _body = save(world, new_session())
    MainLoop.advance(menueditor.ANSWER_SECONDS * 1000)
    assert request.answer() == {"result": "no_answer"}


def test_a_page_that_went_away_is_not_written_to(world):
    request, _body = save(world, new_session())
    request.lose_connection()
    answer(world, "saved")
    assert request.written == [] and not request.finished


def test_a_save_without_a_broker_is_answered_at_once(world):
    world.client.fire_disconnect(7)
    request, body = save(world, new_session())
    assert json.loads(body) == {"result": "not_connected"}
    assert request.response_code == 200


def test_a_save_with_the_menu_off_is_not_allowed(make_bridge, factory, settings, monkeypatch):
    webif.mounted()
    world = World(make_bridge, factory, settings, menu_allowed=False)
    monkeypatch.setattr(webif, "_bridge", lambda: world.bridge)
    world.resource = webif.MQTTBridgeWebResource()
    _request, body = save(world, new_session())
    assert json.loads(body) == {"result": "not_allowed"}


def test_a_retired_token_is_out_of_date(world):
    session = new_session()
    old = token(session)
    webif._rotate(_Request(session=session))
    request, body = save(world, session, save_fields(session, csrf=None) | {"csrf": old})
    assert request.response_code == 403
    assert json.loads(body) == {"result": "out_of_date"}


@pytest.mark.parametrize("case, code, result", [
    ("no origin", 403, "rejected"),
    ("another origin", 403, "rejected"),
    ("cross-site fetch", 403, "rejected"),
    ("wrong content type", 403, "rejected"),
    ("another session's token", 403, "rejected"),
    ("no token", 403, "rejected"),
    ("extra field", 400, "unreadable"),
    ("repeated field", 400, "unreadable"),
    ("wrong form", 400, "unreadable"),
    ("menu not JSON", 400, "unreadable"),
    ("menu an object", 400, "unreadable"),
    ("base not a revision", 400, "unreadable"),
    ("menu over 64 KiB", 400, "unreadable"),
])
def test_a_refused_save_publishes_nothing(world, case, code, result):
    session = new_session()
    fields = save_fields(session)
    options = {}
    if case == "no origin":
        options["origin"] = None
    elif case == "another origin":
        options["origin"] = "http://attacker.example"
    elif case == "cross-site fetch":
        options["headers"] = {"sec-fetch-site": "cross-site"}
    elif case == "wrong content type":
        options["content_type"] = "text/plain"
    elif case == "another session's token":
        fields["csrf"] = token(new_session())
    elif case == "no token":
        del fields["csrf"]
    elif case == "extra field":
        fields["rendered"] = "x"
    elif case == "repeated field":
        fields["base"] = [REVISION, REVISION]
    elif case == "wrong form":
        fields["form"] = "menu_settings"
    elif case == "menu not JSON":
        fields["menu"] = "[{"
    elif case == "menu an object":
        fields["menu"] = "{}"
    elif case == "base not a revision":
        fields["base"] = "3F9C2E4A1B7D5C60"
    elif case == "menu over 64 KiB":
        fields["menu"] = json.dumps(["x" * 70000])
    published = len(world.client.published)

    request, body = save(world, session, fields, **options)

    assert request.response_code == code
    assert json.loads(body) == {"result": result}
    assert len(world.client.published) == published


def test_a_save_under_a_foreign_host_is_421_before_the_session_is_read(world):
    from test_webif import _UntouchableSession

    request = _HeldRequest(session=_UntouchableSession(), host="attacker.example",
                           args=encode({"form": "menu_edit"}),
                           prepath=(b"mqttbridge", b"menu", b"save"))
    menu_child(world.resource, "save").render_POST(request)
    assert request.response_code == webif.MISDIRECTED_REQUEST


def test_a_get_of_the_save_is_refused(world):
    request, body = page(world, name="save")
    assert request.response_code == 405
    assert json.loads(body) == {"result": "rejected"}


def test_the_menu_field_alone_may_be_64_kib(world):
    session = new_session()
    menu = [{"type": "row", "id": None, "entity": "light.x", "control": "toggle",
             "label": "x" * 50000, "icon": None}]
    request, body = save(world, session, save_fields(session, menu))
    assert body == webif.NOT_DONE_YET
    assert BOX in request.getHeader("host")


# --------------------------------------------------------------- the hook --


def test_the_openwebif_hook_says_the_page_is_mounted(monkeypatch):
    import types

    from test_webif import _run_hook

    toplevel = types.ModuleType("Plugins.Extensions.WebInterface.WebChilds.Toplevel")
    toplevel.loaded_plugins = []
    toplevel.addExternalChild = toplevel.loaded_plugins.append
    assert not menueditor.mounted()
    _run_hook(monkeypatch, toplevel)
    assert menueditor.mounted()


def test_a_link_openwebif_refused_is_not_mounted(monkeypatch):
    import types

    from test_webif import _run_hook

    toplevel = types.ModuleType("Plugins.Extensions.WebInterface.WebChilds.Toplevel")
    toplevel.addExternalChild = lambda child: False
    _run_hook(monkeypatch, toplevel)
    assert not menueditor.mounted()
