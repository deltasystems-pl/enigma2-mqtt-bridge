"""The two places a person installs a release from the receiver itself: the television and the page.

`test_selfupdate.py` proves what `cmd/update` decides; this file proves what the "Plugin updates"
screen and the OpenWebif page offer, ask and send. The consent rule (spec ae.4): at the
television or on a page OpenWebif admitted, neither `update_check` nor `update_allowed` is needed -
to check or to install, a downgrade included - and every household guard still applies. A
downgrade is started only by a "yes" to the question that names what it takes away (spec ae.8),
and on the page that consent never travels through the browser. While an update has closed the
plugin's doors both show only the sentence, and the screen imports nothing new.
"""

import ast
import html
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import MainLoop, MessageBox, RecordTimerEntry
from test_selfupdate import (
    NOW,
    ROOT,
    accepted,
    box,
    directories,
    helper_says,
    hold,
    mono,
    refusal,
    tick,
    tree,
)
from test_setup_screen import FakeSession
from test_webif import _Request, action_fields, confirmation, new_session, post, token
from updatelab import TEST_KEYS, FakeOrigin, release, signed

from MQTTBridge import selfupdate, trust, updatehelper, updatescreen, updateview, webif
from MQTTBridge import setup as setup_screen
from MQTTBridge.origin import MQTT, PAGE, SCREEN
from MQTTBridge.publisher import Refusal
from MQTTBridge.updatescreen import MQTTBridgeUpdates

# Fixtures of `test_selfupdate.py`, used by name as parameters below.
FIXTURES = (box, mono, tree)

PACKAGE_DIR = Path(updatescreen.__file__).resolve().parent
LISTED = ["0.4.0 - newer version", "0.3.0 - installed", "0.2.5 - older version",
          "0.2.0 - older version"]


# ---------------------------------------------------------------------- helpers --


class QuestionSession:
    """A session that records the questions a screen asks, and answers them for the test."""

    def __init__(self):
        self.opened = []
        self.questions = []

    def open(self, screen, *arguments, **kwargs):
        self.opened.append((screen, arguments, kwargs))

    def openWithCallback(self, callback, screen, *arguments, **kwargs):
        self.questions.append((callback, screen, arguments, kwargs))

    def answer(self, value):
        self.questions.pop()[0](value)


def screen_of(bridge):
    return MQTTBridgeUpdates(QuestionSession(), bridge=bridge)


def labels(screen):
    return [entry[0] for entry in screen["list"].list]


def choose(screen, version):
    versions = [entry[1][0] for entry in screen["list"].list]
    screen["list"].moveToIndex(versions.index(version))


def request_of(directory):
    return json.loads((directory / "request.json").read_text())


@pytest.fixture
def page(monkeypatch):
    def build(bridge):
        monkeypatch.setattr(webif, "_bridge", lambda: bridge)
        return webif.MQTTBridgeWebResource()

    return build


def install_fields(version):
    return action_fields("update", version=version)


# ------------------------------------------------------------- the setup screen --


def test_the_blue_key_opens_the_plugin_updates_screen(box, settings):
    bridge = box()
    screen = setup_screen.MQTTBridgeSetup(FakeSession(), settings=settings, bridge=bridge)
    assert screen["key_blue"].text == "Plugin updates"
    assert screen["mqttbridgeActions"].actions["blue"] == screen.keyUpdates
    assert 'name="key_blue"' in setup_screen.MQTTBridgeSetup.skin
    screen.keyUpdates()
    assert screen.session.opened == [MQTTBridgeUpdates]


def test_behind_closed_doors_the_blue_key_says_only_the_sentence(box, factory, settings):
    bridge = box()
    directory = accepted(bridge, factory)
    helper_says(directory, phase="installing")
    tick()
    screen = setup_screen.MQTTBridgeSetup(FakeSession(), settings=settings, bridge=bridge)
    screen.keyUpdates()
    assert screen.session.opened == [MessageBox]


# ---------------------------------------------------------------- what it shows --


def test_the_screen_lists_the_offered_versions_with_the_running_one_marked(box):
    bridge = box()
    screen = screen_of(bridge)
    assert labels(screen) == LISTED
    info = screen["info"].text
    assert "Installed version: 0.3.0+gcdcdcdc" in info
    assert "List of versions no. 1 of 2026-09-2" in info and "from Home Assistant" in info
    assert screen.title == "Plugin updates"


def test_a_version_that_cannot_be_installed_says_why_and_is_not_offered(box):
    bridge = box(releases=[release("0.5.0", contract=2), release("0.4.0", depends=("nothere",)),
                           release("0.3.0")])
    # What opkg says is installed; the test's receiver has no opkg database of its own.
    bridge.updates._installed = frozenset({"python3-core"})
    screen = screen_of(bridge)
    assert labels(screen) == [
        "0.5.0 - cannot be installed: does not work with this plugin or with the Home "
        "Assistant integration",
        "0.4.0 - cannot be installed: needs a package that is not installed on this receiver",
        "0.3.0 - installed",
    ]
    assert updateview.installable(bridge) == [("0.3.0", "0.3.0 - installed")]
    screen.keyInstall()
    assert screen.session.questions == []
    assert "does not work with this plugin" in screen["status"].text
    assert directories(bridge.root) == []


def test_without_a_list_the_screen_says_to_check_first(box):
    bridge = box()
    bridge.updates._held = None
    screen = screen_of(bridge)
    assert labels(screen) == []
    assert "No list of versions is known yet. Check for updates first." in screen["info"].text
    screen.keyInstall()
    assert screen.session.questions == []


def test_the_screen_carries_its_own_skin_and_keys(box):
    screen = screen_of(box())
    for widget in ("info", "list", "status", "key_red", "key_green", "key_yellow"):
        assert 'name="' + widget + '"' in MQTTBridgeUpdates.skin
    keys = screen["mqttbridgeUpdateActions"].actions
    assert keys["ok"] == keys["green"] == screen.keyInstall
    assert keys["yellow"] == screen.keyCheck
    assert keys["cancel"] == keys["red"] == screen.keyClose
    assert (screen["key_red"].text, screen["key_green"].text, screen["key_yellow"].text) == (
        "Close", "Install", "Check now")


def test_the_selection_is_not_moved_by_the_refresh(box, monkeypatch):
    screen = screen_of(box())
    calls = []
    real = screen["list"].setList
    monkeypatch.setattr(screen["list"], "setList", lambda rows: calls.append(rows) or real(rows))
    choose(screen, "0.2.5")
    MainLoop.advance(updatescreen.REFRESH_MILLISECONDS * 3)
    assert calls == []
    assert screen["list"].getCurrent()[1][0] == "0.2.5"


def test_closing_the_screen_stops_its_refresh(box):
    screen = screen_of(box())
    timer = screen._ticker.timer
    assert timer.running
    screen.keyClose()
    for function in screen.onClose:
        function()
    assert not timer.running


def test_an_idle_bridge_is_said_and_nothing_is_asked(make_bridge):
    bridge = make_bridge()
    bridge.start()
    screen = screen_of(bridge)
    assert "Commands need a running bridge: no broker address is configured" in \
        screen["info"].text
    screen.keyCheck()
    assert "Commands need a running bridge" in screen["status"].text


def test_nothing_the_screen_does_raises_into_the_main_loop(box, monkeypatch):
    bridge = box()
    screen = screen_of(bridge)

    def broken(*_arguments, **_kwargs):
        raise RuntimeError("the image moved")

    monkeypatch.setattr(bridge.updates, "payload", broken)
    screen.refresh()
    monkeypatch.setattr(screen.session, "openWithCallback", broken)
    monkeypatch.setattr(bridge, "run_command", broken)
    screen.keyInstall()
    screen.keyCheck()
    screen._asked = ("0.4.0", False)
    screen._answered(True)


# -------------------------------------------------------------------- check now --


def test_check_now_needs_no_update_check_at_the_television(box, factory, settings):
    bridge = box()
    assert settings.update_check.value is False
    origin = FakeOrigin()
    origin.serve(*signed(2))
    bridge.updates.keys = TEST_KEYS
    bridge.updates.acceptance = False
    bridge.updates.fetch = origin
    screen = screen_of(bridge)
    screen.keyCheck()
    assert origin.names() == [trust.SIGNATURE_FILE, trust.INDEX_FILE]
    assert "Asked for the list of versions" in screen["status"].text
    screen.refresh()
    assert "List of versions no. 2" in screen["info"].text
    # The ten-minute limit is the same for every origin: the answer is the stored one.
    screen.keyCheck()
    assert len(origin.calls) == 2
    # Over MQTT the setting still decides.
    factory.client.fire_message(ROOT + "/cmd/update_check", b"PRESS")
    assert refusal(factory.client)[0] == "not_permitted"
    assert len(origin.calls) == 2


# --------------------------------------------------------- install and downgrade --


def test_an_install_is_asked_first_and_needs_no_update_allowed(box):
    bridge = box(allowed=False)
    screen = screen_of(bridge)
    choose(screen, "0.4.0")
    screen.keyInstall()
    callback, question, arguments, kwargs = screen.session.questions[-1]
    assert question is MessageBox
    assert arguments[0].startswith("Install version 0.4.0 of the plugin?")
    assert "user interface restarts" in arguments[0]
    assert arguments[1] == MessageBox.TYPE_YESNO and kwargs == {"default": False}
    assert directories(bridge.root) == []
    screen.session.answer(True)
    made = directories(bridge.root)
    assert len(made) == 1
    request = request_of(made[0])
    assert (request["target"], request["started_by"], request["downgrade"]) == (
        "0.4.0", SCREEN, False)
    assert "The update to version 0.4.0 has started." in screen["status"].text
    assert "Update to 0.4.0, started on the television: downloading" in screen["status"].text


@pytest.mark.parametrize("answer", [False, None])
def test_no_answer_but_yes_starts_nothing(answer, box):
    bridge = box()
    screen = screen_of(bridge)
    choose(screen, "0.2.5")
    screen.keyInstall()
    screen.session.answer(answer)
    assert directories(bridge.root) == []


def test_a_downgrade_is_asked_in_its_own_words_and_its_yes_consents(box):
    bridge = box(allowed=False)
    screen = screen_of(bridge)
    choose(screen, "0.2.5")
    screen.keyInstall()
    text = screen.session.questions[-1][2][0]
    assert text.startswith("Install the older version 0.2.5? The plugin's newer features will "
                           "disappear until it is updated again.")
    assert screen.session.questions[-1][3] == {"default": False}
    screen.session.answer(True)
    request = request_of(directories(bridge.root)[0])
    assert (request["target"], request["started_by"], request["downgrade"]) == (
        "0.2.5", SCREEN, True)


def test_the_yes_to_the_plain_question_is_never_a_downgrade(box, factory):
    """What is asked is what is consented to: the answer does not re-judge the version."""
    bridge = box()
    screen = screen_of(bridge)
    screen._asked = ("0.2.5", False)
    screen._answered(True)
    assert directories(bridge.root) == []
    assert refusal(factory.client)[0] == "downgrade"


@pytest.mark.parametrize("origin", [MQTT, PAGE, SCREEN])
def test_without_the_consent_a_lower_version_is_refused_from_every_origin(origin, box):
    bridge = box()
    error = bridge.run_command("update", json.dumps({"version": "0.2.5"}), origin)
    assert error.reason == "downgrade"
    assert directories(bridge.root) == []


def test_the_consent_is_worth_nothing_over_mqtt(box, factory):
    bridge = box()
    error = bridge.run_command("update", json.dumps({"version": "0.2.5"}), MQTT, downgrade=True)
    assert error.reason == "downgrade"
    # And no payload can carry it: the dispatcher passes the broker none.
    factory.client.fire_message(ROOT + "/cmd/update",
                                json.dumps({"version": "0.2.5", "downgrade": True}).encode())
    assert refusal(factory.client)[0] == "downgrade"
    assert directories(bridge.root) == []


def test_the_households_guards_apply_at_the_television(box, receiver, factory):
    bridge = box()
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    screen = screen_of(bridge)
    choose(screen, "0.4.0")
    screen.keyInstall()
    screen.session.answer(True)
    assert directories(bridge.root) == []
    assert refusal(factory.client)[0] == "recording"
    assert "The receiver is recording or is about to start a recording." in \
        screen["status"].text


def test_a_second_install_is_told_in_the_households_words(box, factory):
    bridge = box()
    accepted(bridge, factory)
    screen = screen_of(bridge)
    choose(screen, "0.4.0")
    screen.keyInstall()
    screen.session.answer(True)
    assert len(directories(bridge.root)) == 1
    assert "An installation or update of the plugin is already running on the receiver." in \
        screen["status"].text


@pytest.mark.parametrize("reason", [
    "no_capability", "busy", "opkg_busy", "standby", "recording", "recording_due",
    "recording_unknown", "epg_import", "cannot_restart", "unknown_version", "withdrawn",
    "below_floor", "incompatible", "depends", "current", "no_space", "rate_limited",
    "internal_error",
])
def test_every_refusal_the_television_can_meet_is_a_household_sentence(reason):
    said = updateview.household_refusal(Refusal("contract sentence", reason), "0.4.0")
    assert said != "contract sentence" and said.endswith(".")
    assert "%" not in said


def test_a_refusal_without_a_household_sentence_is_its_own(box):
    assert updateview.household_refusal(Refusal("something new", "later_code")) == \
        "something new"
    assert updateview.household_refusal("no code at all") == "no code at all"


# ----------------------------------------------------------- progress and doors --


def test_the_progress_says_who_started_it_and_how_far_it_is(box, factory):
    bridge = box()
    directory = accepted(bridge, factory, {"version": "0.4.0", "relay": {
        "url": "http://192.0.2.20/api/enigma2_mqtt/relay/" + "a" * 43, "expires": NOW + 600}})
    screen = screen_of(bridge)
    assert "Update to 0.4.0, started from Home Assistant: downloading" in screen["status"].text
    helper_says(directory, phase="verifying", started_by="home_assistant")
    tick()
    screen.refresh()
    assert "Update to 0.4.0, started from Home Assistant: checking the download" in \
        screen["status"].text


def test_behind_closed_doors_the_screen_shows_only_the_sentence(box, factory):
    bridge = box()
    screen = screen_of(bridge)
    choose(screen, "0.4.0")
    screen.keyInstall()
    screen.session.answer(True)
    directory = directories(bridge.root)[0]
    helper_says(directory, phase="installing", started_by="screen")
    tick()
    MainLoop.advance(updatescreen.REFRESH_MILLISECONDS)
    assert screen["info"].text == selfupdate.household_doors(bridge.self_update)
    assert labels(screen) == [] and screen["status"].text == ""
    assert (screen["key_green"].text, screen["key_yellow"].text) == ("", "")
    asked = len(screen.session.questions)
    screen.keyInstall()
    screen.keyCheck()
    assert len(screen.session.questions) == asked
    assert len(directories(bridge.root)) == 1


def test_a_key_pressed_before_the_screen_redrew_behind_closed_doors_does_nothing(box, factory):
    """The doors can close between two redraws, with the list still on the screen."""
    bridge = box()
    screen = screen_of(bridge)
    directory = accepted(bridge, factory)
    helper_says(directory, phase="installing")
    bridge.self_update._poll()
    assert bridge.self_update.closed and labels(screen) == LISTED
    published = len(factory.client.published)
    choose(screen, "0.2.5")
    screen.keyInstall()
    screen.keyCheck()
    assert screen.session.questions == []
    assert factory.client.published[published:] == []
    assert len(directories(bridge.root)) == 1


def test_a_stuck_update_says_to_install_again(box, factory):
    bridge = box()
    screen = screen_of(bridge)
    directory = accepted(bridge, factory)
    helper_says(directory, phase="installing")
    tick()
    helper_says(directory, phase="finished", result="failed", reason="opkg_failed",
                error="the package manager could not install version 0.4.0: exit 255",
                finished=NOW + 5, record={"restore": "failed: [Errno 5] Input/output error"})
    tick()
    screen.refresh()
    assert bridge.self_update.stuck
    assert screen["info"].text == selfupdate.household_doors(bridge.self_update)
    assert "install the plugin again" in screen["info"].text


def test_a_withdrawn_update_shows_the_list_again_and_how_it_ended(box, factory, receiver):
    bridge = box()
    screen = screen_of(bridge)
    choose(screen, "0.4.0")
    screen.keyInstall()
    screen.session.answer(True)
    directory = directories(bridge.root)[0]
    helper_says(directory, phase="restarting", started_by="screen")
    tick()
    receiver.session.callbacks[-1][0](True)
    helper_says(directory, phase="finished", result="withdrawn_before_restart",
                started_by="screen", reason="question",
                error=updatehelper.SENTENCES["question"], finished=NOW + 70)
    tick()
    factory.client.fire_connect()
    # The reload's update check loads the trust file, empty in the tests; a receiver's holds
    # the index it had.
    hold(bridge)
    screen.refresh()
    assert labels(screen) == LISTED
    assert "Last update to 0.4.0, started on the television: withdrawn before the restart; " \
           "the previous version runs" in screen["status"].text


def test_the_screen_and_its_view_import_nothing_after_their_start():
    """Behind closed doors a first import could read the next release's half-written file."""
    for name in ("updatescreen.py", "updateview.py"):
        module = ast.parse((PACKAGE_DIR / name).read_text(encoding="utf-8"))
        for node in ast.walk(module):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = [child for child in ast.walk(node)
                         if isinstance(child, (ast.Import, ast.ImportFrom))]
                assert inner == [], name + ": " + node.name
    assert updatescreen.MQTTBridgeUpdates is setup_screen.MQTTBridgeUpdates


ISOLATED = "MQTTBRIDGE_TEST_ISOLATED_SCREEN"


def test_the_screen_left_open_behind_closed_doors_imports_nothing_new():
    """The whole transaction with the screen open, alone in a fresh interpreter (review S9)."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider",
         __file__ + "::test_the_screen_left_open_alone"],
        env=dict(os.environ, **{ISOLATED: "1"}), cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "PASSED" in result.stdout and "test_the_screen_left_open_alone" in result.stdout


@pytest.mark.skipif(not os.environ.get(ISOLATED),
                    reason="run alone, in a fresh interpreter, by the test above")
def test_the_screen_left_open_alone(box, factory, receiver, monkeypatch):
    bridge = box()
    screen = screen_of(bridge)
    choose(screen, "0.4.0")
    screen.keyInstall()
    screen.session.answer(True)
    directory = directories(bridge.root)[0]
    attempts = []

    class Refuse:
        def find_spec(self, name, path=None, target=None):
            if name.startswith("MQTTBridge") and name not in sys.modules:
                attempts.append(name)
                raise ImportError("the plugin's files are being replaced: " + name)
            return None

    monkeypatch.setattr(sys, "meta_path", [Refuse()] + sys.meta_path)
    helper_says(directory, phase="installing", started_by="screen")
    tick()
    screen.refresh()
    screen.keyInstall()
    screen.keyCheck()
    assert screen["info"].text == selfupdate.household_doors(bridge.self_update)
    helper_says(directory, phase="restarting", started_by="screen")
    tick()
    receiver.session.callbacks[-1][0](False)
    helper_says(directory, phase="finished", result="withdrawn_before_restart",
                started_by="screen", reason="question",
                error=updatehelper.SENTENCES["question"], finished=NOW)
    tick()
    factory.client.fire_connect()
    hold(bridge)
    screen.refresh()
    assert attempts == []
    assert labels(screen) == LISTED


# --------------------------------------------------------------------- the page --


def test_the_page_offers_what_the_index_offers_and_says_why_not(box, page):
    bridge = box(releases=[release("0.5.0", contract=2), release("0.4.0"), release("0.3.0"),
                           release("0.2.5")])
    request, body = get_page(page(bridge))
    text = html.unescape(body.decode("utf-8"))
    form = text.split("value='update'>", 1)[1].split("</form>", 1)[0]
    assert re.findall(r"<option value='([^']+)'", form) == ["0.4.0", "0.3.0", "0.2.5"]
    assert "<h2>Plugin updates</h2>" in text
    assert "0.5.0 - cannot be installed: does not work with this plugin" in text
    assert "Installed version: 0.3.0+gcdcdcdc" in text


def get_page(resource, session=None):
    request = _Request(session=session or new_session())
    return request, resource.render_GET(request)


def test_without_a_list_the_install_form_is_disabled_and_says_why(box, page):
    bridge = box()
    bridge.updates._held = None
    _request, body = get_page(page(bridge))
    text = body.decode("utf-8")
    disabled = re.findall(r"<fieldset disabled><legend>([^<]*)</legend>", text)
    assert disabled == ["Install a plugin version"]
    assert "No list of versions is known yet. Check for updates first." in text


def test_an_install_from_the_page_is_confirmed_and_needs_no_update_allowed(box, page):
    bridge = box(allowed=False)
    resource = page(bridge)
    session = new_session()
    request, body = post(resource, session, install_fields("0.4.0"))
    assert request.response_code == 200
    text = html.unescape(body.decode("utf-8"))
    assert "Install version 0.4.0 of the plugin?" in text and "user interface restarts" in text
    assert directories(bridge.root) == []
    assert "consents" not in session.sessionNamespaces[webif.CONFIRM_KEY]
    request, body = post(resource, session, confirmation(body), csrf=None)
    assert request.response_code == 200
    assert b"The update to version 0.4.0 has started." in body
    request = request_of(directories(bridge.root)[0])
    assert (request["started_by"], request["downgrade"]) == (PAGE, False)


def test_a_downgrade_from_the_page_names_the_loss_and_its_confirmation_consents(box, page):
    bridge = box(allowed=False)
    resource = page(bridge)
    session = new_session()
    _request, body = post(resource, session, install_fields("0.2.5"))
    text = html.unescape(body.decode("utf-8"))
    assert ("Install the older version 0.2.5? The plugin's newer features will disappear "
            "until it is updated again.") in text
    assert directories(bridge.root) == []
    assert session.sessionNamespaces[webif.CONFIRM_KEY]["consents"] == {"downgrade": True}
    post(resource, session, confirmation(body), csrf=None)
    request = request_of(directories(bridge.root)[0])
    assert (request["target"], request["started_by"], request["downgrade"]) == (
        "0.2.5", PAGE, True)


@pytest.mark.parametrize("attack", [
    "no token", "wrong token", "another site", "no origin", "not a form", "a version not offered",
])
def test_a_forged_install_request_is_refused_and_starts_nothing(attack, box, page):
    bridge = box()
    resource = page(bridge)
    session = new_session()
    token(session)
    fields, options = install_fields("0.2.5"), {}
    csrf = True
    if attack == "no token":
        csrf = None
    elif attack == "wrong token":
        csrf = "A" * 43
    elif attack == "another site":
        options["origin"] = "http://attacker.example"
    elif attack == "no origin":
        options["origin"] = None
    elif attack == "not a form":
        options["content_type"] = "text/plain"
    elif attack == "a version not offered":
        fields = install_fields("0.1.0")
    request, body = post(resource, session, fields, csrf=csrf, **options)
    assert request.response_code in (400, 403)
    assert b"name='form' value='confirm'" not in body
    assert webif.CONFIRM_KEY not in session.sessionNamespaces
    assert directories(bridge.root) == []


def test_a_get_never_installs(box, page):
    bridge = box()
    resource = page(bridge)
    session = new_session()
    query = "form=action&action=update&version=0.2.5&csrf=" + token(session)
    request = _Request(session=session, uri=b"/mqttbridge?" + query.encode())
    resource.render_GET(request)
    assert request.response_code == 200
    assert webif.CONFIRM_KEY not in session.sessionNamespaces
    assert directories(bridge.root) == []


@pytest.mark.parametrize("tamper", [
    "the page's token", "another version", "a consent field", "another site", "twice",
])
def test_the_confirmation_cannot_be_bent(tamper, box, page):
    """The downgrade consent lives with the pending confirmation; the browser cannot add it."""
    bridge = box()
    resource = page(bridge)
    session = new_session()
    wanted = "0.2.5" if tamper == "twice" else "0.4.0"
    _request, body = post(resource, session, install_fields(wanted))
    fields, options = confirmation(body), {}
    if tamper == "the page's token":
        fields["csrf"] = token(session)
    elif tamper == "another version":
        fields["payload"] = json.dumps({"version": "0.2.5"}, separators=(",", ":"))
    elif tamper == "a consent field":
        fields["downgrade"] = "true"
    elif tamper == "another site":
        options["origin"] = "http://attacker.example"
    elif tamper == "twice":
        request, _body = post(resource, session, fields, csrf=None)
        assert request.response_code == 200
        assert len(directories(bridge.root)) == 1
    request, _body = post(resource, session, fields, csrf=None, **options)
    assert request.response_code in (400, 403)
    expected = 1 if tamper == "twice" else 0
    assert len(directories(bridge.root)) == expected
    if expected:
        assert request_of(directories(bridge.root)[0])["downgrade"] is True


def test_behind_closed_doors_a_confirmed_install_is_refused_with_the_sentence(box, page,
                                                                              factory):
    bridge = box()
    resource = page(bridge)
    session = new_session()
    _request, body = post(resource, session, install_fields("0.4.0"))
    directory = accepted(bridge, factory)
    helper_says(directory, phase="installing")
    tick()
    request, body = post(resource, session, confirmation(body), csrf=None)
    text = body.decode("utf-8")
    assert selfupdate.household_doors(bridge.self_update) in text
    assert "<form" not in text
    assert len(directories(bridge.root)) == 1


def test_the_households_guards_apply_on_the_page(box, page, receiver):
    bridge = box()
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    resource = page(bridge)
    session = new_session()
    _request, body = post(resource, session, install_fields("0.4.0"))
    _request, body = post(resource, session, confirmation(body), csrf=None)
    assert b"Refused: the receiver is recording" in body
    assert directories(bridge.root) == []


def test_the_page_shows_the_update_in_progress(box, page, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    helper_says(directory, phase="snapshot")
    tick()
    _request, body = get_page(page(bridge))
    assert b"Update to 0.4.0, started over MQTT: saving the current version" in body


# ------------------------------------------------------------------ catalogues --


PLACEHOLDER = re.compile(r"%\((\w+)\)s|%s")


def test_every_placeholder_survives_translation():
    from test_locale import LANGUAGES, LOCALE, POT, catalogue

    template = catalogue(POT)
    for language in LANGUAGES:
        entries = catalogue(LOCALE / language / "LC_MESSAGES" / "MQTTBridge.po")
        for msgid in template:
            wanted = sorted(PLACEHOLDER.findall(msgid))
            assert sorted(PLACEHOLDER.findall(entries[msgid])) == wanted, (language, msgid)


def test_the_polish_screen_uses_the_specs_words():
    from test_locale import LOCALE, catalogue

    entries = catalogue(LOCALE / "pl" / "LC_MESSAGES" / "MQTTBridge.po")
    assert entries["Plugin updates"] == "Aktualizacje wtyczki"
    assert entries["Check now"] == "Sprawdź teraz"
    assert entries["Install"] == "Zainstaluj"
    assert entries["Install the older version %s? The plugin's newer features will disappear "
                   "until it is updated again."] == (
        "Zainstalować starszą wersję %s? Nowsze funkcje wtyczki znikną do czasu ponownej "
        "aktualizacji.")
