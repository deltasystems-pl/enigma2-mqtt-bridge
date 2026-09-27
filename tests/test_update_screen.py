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
import xml.etree.ElementTree as ElementTree
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_selfupdate_relay
from conftest import MainLoop, MessageBox, RecordTimerEntry
from test_selfupdate import (
    BUILD,
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
from test_webif import (
    _Request,
    action_fields,
    confirmation,
    new_session,
    post,
    settings_fields,
    token,
)
from updatelab import TEST_KEYS, FakeOrigin, release, signed

from MQTTBridge import (
    selfupdate,
    trust,
    trustfile,
    updatecheck,
    updatehelper,
    updatescreen,
    updateview,
    webif,
)
from MQTTBridge import setup as setup_screen
from MQTTBridge.origin import MQTT, PAGE, SCREEN
from MQTTBridge.publisher import Refusal
from MQTTBridge.updatescreen import MQTTBridgeUpdates

# Fixtures of `test_selfupdate.py`, used by name as parameters below.
FIXTURES = (box, mono, tree)

PACKAGE_DIR = Path(updatescreen.__file__).resolve().parent
# The tests' receiver runs a development build of 0.3.0, so its release is not "installed".
RUNNING = "0.3.0 - release; a development build of it runs"
LISTED = ["0.4.0 - newer version", RUNNING, "0.2.5 - older version", "0.2.0 - older version"]


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
    assert "List of versions no. 1 of 2026-09-2" in info and "via Home Assistant" in info
    assert screen.title == "Plugin updates"


def test_a_version_that_cannot_be_installed_says_why_and_is_not_offered(box):
    bridge = box(releases=[release("0.5.0", contract=2), release("0.4.0", depends=("nothere",)),
                           release("0.3.0")])
    # What opkg says is installed; the test's receiver has no opkg database of its own.
    bridge.updates._installed = frozenset({"python3-core"})
    screen = screen_of(bridge)
    # A row is one line on the television, so it says only that; OK says why (review round 2).
    assert labels(screen) == [
        "0.5.0 - cannot be installed",
        "0.4.0 - cannot be installed",
        RUNNING,
    ]
    assert updateview.installable(bridge) == [("0.3.0", RUNNING)]
    screen.keyInstall()
    assert screen.session.questions == []
    assert screen["status"].text == (
        "0.5.0 - cannot be installed: does not work with this plugin or with the Home "
        "Assistant integration")
    choose(screen, "0.4.0")
    screen.keyInstall()
    assert screen["status"].text == (
        "0.4.0 - cannot be installed: needs a package that is not installed on this receiver")
    assert screen.session.questions == []
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
    screen._answered("0.4.0", False, True)


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
    screen._answered("0.2.5", False, True)
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
    "internal_error", "no_relay", "clock_skew",
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
    # Spec ae.10, "bad signature / older index".
    assert entries["The plugin's list of versions has an invalid signature or is older than "
                   "the one already known. Nothing was changed."] == (
        "Lista wersji wtyczki ma nieprawidłowy podpis albo jest starsza od już znanej. Nic nie "
        "zmieniono.")


# ------------------------------------------------------------ review round 1 --


@pytest.mark.parametrize("form", ["install", "downgrade", "restart_gui", "confirm", "identity"])
def test_behind_closed_doors_the_page_answers_only_the_sentence(form, box, page, factory):
    """Spec ae.6 step 3: from `installing` on, every form gets the sentence and nothing is kept.

    Not a question page, not a pending confirmation: an answer to either would meet the doors
    anyway, and a page that asks what it cannot do tells the household the wrong thing.
    """
    bridge = box()
    resource = page(bridge)
    session = new_session()
    token(session)
    fields, csrf = None, True
    if form == "confirm":
        # Asked before the doors closed, answered after.
        _request, body = post(resource, session, install_fields("0.4.0"))
        fields, csrf = confirmation(body), None
    directory = accepted(bridge, factory)
    helper_says(directory, phase="installing")
    tick()
    assert bridge.self_update.closed
    if form == "install":
        fields = install_fields("0.4.0")
    elif form == "downgrade":
        fields = install_fields("0.2.5")
    elif form == "restart_gui":
        fields = action_fields("restart_gui")
    elif form == "identity":
        fields = settings_fields(bridge.settings, node_id="vuuno4kse_005302")
    published = len(factory.client.published)
    request, body = post(resource, session, fields, csrf=csrf)
    text = body.decode("utf-8")
    assert request.response_code == 409
    assert selfupdate.household_doors(bridge.self_update) in text
    assert "<form" not in text
    assert webif.CONFIRM_KEY not in session.sessionNamespaces
    assert factory.client.published[published:] == []
    assert len(directories(bridge.root)) == 1


def test_each_answer_acts_on_its_own_question(box):
    """The answer carries what was asked; nothing another question asked can reach it."""
    bridge = box()
    screen = screen_of(bridge)
    choose(screen, "0.4.0")
    screen.keyInstall()
    choose(screen, "0.2.5")
    screen.keyInstall()
    upgrade, _downgrade = screen.session.questions
    assert upgrade[2][0].startswith("Install version 0.4.0 of the plugin?")
    upgrade[0](True)
    made = directories(bridge.root)
    assert len(made) == 1
    request = request_of(made[0])
    assert (request["target"], request["downgrade"]) == ("0.4.0", False)


def test_the_refresh_stops_however_the_screen_is_closed(box, monkeypatch):
    """Closed by the session - a standby, another plugin - and not by its own keys."""
    screen = screen_of(box())
    timer = screen._ticker.timer
    drawn = []
    monkeypatch.setattr(MQTTBridgeUpdates, "_draw", lambda self: drawn.append(1))
    screen.doClose()
    MainLoop.advance(updatescreen.REFRESH_MILLISECONDS * 5)
    assert not timer.running
    assert drawn == []


CHECK_ERRORS = sorted(set(trust.REASONS) | {
    updatecheck.ERROR_UNREACHABLE, updatecheck.ERROR_REDIRECT, updatecheck.ERROR_HTTP,
    updatecheck.ERROR_INTERNAL, updatecheck.MALFORMED_RELAY, updatecheck.TOO_LARGE,
    trustfile.WRITE_FAILED, trustfile.TRUST_BUSY, trustfile.ERROR_BAD_MEMORY,
    trustfile.ERROR_BAD_KEYS,
})


@pytest.mark.parametrize("code", CHECK_ERRORS)
def test_every_check_error_is_a_household_sentence(code, box):
    """Spec ae.10: the television says what went wrong, never the code (review S4)."""
    bridge = box()
    bridge.updates._check_error = code
    said = updateview.header(bridge)[-1]
    assert code not in said and "(" not in said and said.endswith("."), said


def test_a_check_error_nobody_knows_yet_is_named():
    bridge = SimpleNamespace(build=None, updates=SimpleNamespace(
        payload=lambda: {"check_error": "later_code"}))
    assert updateview.header(bridge)[-1] == "The last check failed (later_code)."


def test_the_selection_follows_its_version_when_the_list_changes(box):
    bridge = box()
    screen = screen_of(bridge)
    choose(screen, "0.2.5")
    hold(bridge, [release("0.5.0"), release("0.4.0"), release("0.3.0"), release("0.2.5"),
                  release("0.2.0")])
    screen.refresh()
    assert labels(screen)[0] == "0.5.0 - newer version"
    assert screen["list"].getCurrent()[1][0] == "0.2.5"
    hold(bridge, [release("0.4.0"), release("0.3.0")])
    screen.refresh()
    # Gone: the cursor stays within the list, on whatever is there.
    assert screen["list"].getCurrent() is not None


def test_a_confirmation_left_for_ten_minutes_has_expired(box, page):
    bridge = box()
    resource = page(bridge)
    session = new_session()
    _request, body = post(resource, session, install_fields("0.4.0"))
    session.sessionNamespaces[webif.CONFIRM_KEY]["asked"] -= webif.CONFIRM_SECONDS + 1
    request, body = post(resource, session, confirmation(body), csrf=None)
    assert request.response_code == 403
    assert b"more than ten minutes ago" in body
    assert webif.CONFIRM_KEY not in session.sessionNamespaces
    assert directories(bridge.root) == []


def test_a_development_build_is_never_shown_as_the_installed_release(box):
    """Spec ae.5 (v5.5): a development build of the same number is never the current release."""
    bridge = box()
    # The tests' receiver runs a development build made from the release's own commit.
    assert "0.3.0 - release; a development build of it runs" in labels(screen_of(bridge))
    for build, installed in (
        (dict(BUILD, flavour="release"), True),
        (dict(BUILD, flavour="release", commit="ef" * 20), False),
        (dict(BUILD, flavour="release", dirty=True), False),
        (dict(BUILD, flavour="acceptance"), False),
        # A copy nobody built compares by its number (every plugin up to 0.3.x).
        (dict(BUILD, commit=None), True),
    ):
        bridge._build = build
        assert ("0.3.0 - installed" in labels(screen_of(bridge))) is installed, build


def test_one_rule_says_what_is_older():
    """The question and the dispatcher judge a downgrade by the same function (review N4)."""
    assert updateview.older is selfupdate.older
    assert selfupdate.older("0.2.5") and not selfupdate.older("0.3.0")
    assert not selfupdate.older("not a version")


def test_the_list_source_has_its_own_words(box):
    """The words for who started an update are not the words for where the list came from."""
    info = screen_of(box())["info"].text
    assert "List of versions no. 1 of 2026-09-2" in info and "via Home Assistant" in info


# The skins' text boxes, judged by a character budget (review S3): a character is taken as 0.55 of
# the font size wide - the stub's `eLabel` model uses 0.5, and the margin is for capitals and
# German compounds - a line as 1.2 of it tall, and text wraps at spaces. A model, not the image's
# font renderer (the spike looks at the real screen), but enough to stop a sentence that cannot
# fit whatever the font: at `Regular;22` a line holds 71 characters and a 200 px key 16.
GLYPH = 0.55
LINE = 1.2
LONG = "0.10.10"
PHASES = ("downloading", "verifying", "snapshot", "installing", "restarting", "proving",
          "rolling_back")
RESULTS = ("installed", "withdrawn_before_restart", "rolled_back", "failed", "interrupted")
STARTERS = ("mqtt", "home_assistant", "screen", "page", "ssh")
REFUSALS = ("no_capability", "busy", "opkg_busy", "standby", "recording", "recording_due",
            "recording_unknown", "epg_import", "cannot_restart", "unknown_version", "withdrawn",
            "below_floor", "incompatible", "depends", "current", "no_space", "rate_limited",
            "internal_error", "no_relay", "clock_skew")
# The longest reason a bridge goes idle with, in English as it is kept.
IDLE = "the bridge failed to start"


def budget(skin):
    """`name -> (characters per line, lines)` for every widget of `skin` that has a font."""
    found = {}
    for widget in ElementTree.fromstring(skin).iter("widget"):
        if "font" not in widget.attrib:
            continue
        width, height = (int(value) for value in widget.get("size").split(","))
        size = int(widget.get("font").split(";")[1])
        found[widget.get("name")] = (int(width // (size * GLYPH)), height // int(size * LINE))
    return found


def wrapped(text, per_line):
    """How many lines `text` takes when it wraps at spaces."""
    total = 0
    for paragraph in text.split("\n"):
        rows, used = 1, None
        for word in paragraph.split():
            if used is None:
                used = len(word)
            elif used + 1 + len(word) <= per_line:
                used += 1 + len(word)
            else:
                rows, used = rows + 1, len(word)
            while used > per_line:
                rows, used = rows + 1, used - per_line
        total += rows
    return total


def translator(language):
    from test_locale import LOCALE, catalogue

    if language == "en":
        return lambda text: text
    entries = catalogue(LOCALE / language / "LC_MESSAGES" / "MQTTBridge.po")
    return lambda text: entries.get(text) or text


def speak(language, monkeypatch):
    translate = translator(language)
    for module in (updateview, updatescreen, selfupdate, setup_screen):
        monkeypatch.setattr(module, "_", translate)
    return translate


def transaction_lines():
    records = [{"target": LONG, "started_by": who, "phase": phase}
               for who in STARTERS for phase in PHASES]
    records += [{"target": LONG, "started_by": who, "phase": "finished", "result": result}
                for who in STARTERS for result in RESULTS]
    return [updateview.transaction_line(SimpleNamespace(
        self_update=SimpleNamespace(transaction_payload=lambda record=record: record)))
        for record in records]


def said_lines(translate):
    said = [updateview.household_refusal(Refusal("contract sentence", reason), LONG)
            for reason in REFUSALS]
    # OK on a version that cannot be installed says the whole reason here (review round 2).
    said += [updateview.row_detail((LONG, updateview.NEWER, reason))
             for reason in ("incompatible", "depends")]
    # The relay handshake's two waits, at their longest count (review round 2).
    said += [relay_line_of({"version": LONG, "seconds_left": seconds, "phase": phase})
             for phase, seconds in (("probe", 60), ("relay", 120))]
    said += [translate("The update to version %s has started.") % LONG,
             translate("Asked for the list of versions; it is shown here as soon as it arrives."),
             translate("Commands need a running bridge: %s") % translate(
                 "the plugin did not start"),
             translate("Commands need a running bridge: %s") % IDLE]
    return said


def header_lines(translate):
    build = dict(BUILD, dirty=True)
    found = []
    for index in (None, {"serial": 1000000, "issued": 1790500000, "source": "relay"},
                  {"serial": 1000000, "issued": 1790500000, "source": "origin"}):
        for code in [None, "later_code"] + CHECK_ERRORS:
            payload = {"index": index, "check_error": code}
            bridge = SimpleNamespace(build=build, updates=SimpleNamespace(
                payload=lambda payload=payload: payload))
            found.append(updateview.header(bridge)
                         + [translate("Commands need a running bridge: %s") % IDLE])
    return found


@pytest.mark.parametrize("language", ["en", "pl", "de"])
def test_the_words_fit_the_screen(language, box, settings, monkeypatch):
    translate = speak(language, monkeypatch)
    room = budget(MQTTBridgeUpdates.skin)
    per_line, status_lines = room["status"]
    # The status box holds what runs or how the last update ended, and what a key just said.
    longest = max(wrapped(line, per_line) for line in transaction_lines())
    said = max(wrapped(line, per_line) for line in said_lines(translate))
    assert longest + said <= status_lines, (language, longest, said, status_lines)
    per_line, info_lines = room["info"]
    for lines in header_lines(translate):
        assert wrapped("\n".join(lines), per_line) <= info_lines, (language, lines)
    # A list row is one line and never wraps (review round 2, S-d2): the list is as wide as the
    # text boxes, and a MenuList row is drawn in the skin's list font, taken at the same 22 px.
    per_row = list_width() // int(22 * GLYPH)
    for row in list_rows():
        label = updateview.row_label(row)
        assert len(label) <= per_row, (language, label, len(label), per_row)
    # Every key label on one line, on this screen and on the setup screen that opens it.
    bridge = box()
    for screen, skin in ((screen_of(bridge), MQTTBridgeUpdates.skin),
                         (setup_screen.MQTTBridgeSetup(FakeSession(), settings=settings,
                                                       bridge=bridge),
                          setup_screen.MQTTBridgeSetup.skin)):
        for name, (per_line, _lines) in budget(skin).items():
            if name.startswith("key_"):
                assert len(screen[name].text) <= per_line, (language, name, screen[name].text)


def relay_line_of(wait):
    """The line the screen and the page show for one `SelfUpdater.relay_wait()` answer."""
    return updateview.relay_line(SimpleNamespace(self_update=SimpleNamespace(
        relay_wait=lambda: wait)))


def list_width():
    widget = ElementTree.fromstring(MQTTBridgeUpdates.skin).find(".//widget[@name='list']")
    return int(widget.get("size").split(",")[0])


def list_rows():
    """Every kind of row the list can show, with a long version number."""
    rows = [(LONG, relation, None) for relation in (
        updateview.INSTALLED, updateview.SAME_NUMBER, updateview.OLDER, updateview.NEWER)]
    rows += [(LONG, updateview.NEWER, reason) for reason in ("incompatible", "depends",
                                                              "a_reason_nobody_knows_yet")]
    return rows


# ------------------------------------------------------------ review round 2 --

PROBE_LINE = "Checking whether the receiver can reach the internet to download version 0.4.0."
NO_RELAY_SAID = ("The receiver has no access to the internet, and Home Assistant did not "
                 "answer. The installation is not possible.")
CLOCK_SKEW_SAID = ("An answer arrived, but its download address had already expired by the "
                   "receiver's clock. If the receiver's clock is wrong, set it and try again.")
STARTED = "The update to version 0.4.0 has started."


def relay_line(seconds):
    return ("The receiver has no access to the internet, so it has asked Home Assistant for "
            f"version 0.4.0. Waiting for the answer: {seconds} s left.")


@pytest.fixture
def no_internet(monkeypatch):
    """The release origin, not answering the probe at all."""
    fake = test_selfupdate_relay.Origin(answers=False)
    monkeypatch.setattr(updatecheck.UpdateChecker, "fetch", staticmethod(fake))
    return fake


def install_at_the_television(bridge, version="0.4.0"):
    screen = screen_of(bridge)
    choose(screen, version)
    screen.keyInstall()
    screen.session.answer(True)
    return screen


def test_the_screen_says_it_looks_at_the_origin_and_then_asks_home_assistant(box, no_internet,
                                                                             monkeypatch):
    bridge = test_selfupdate_relay.unprobed(test_selfupdate_relay.offline(box()))
    jobs = test_selfupdate_relay.held_back(monkeypatch)
    screen = install_at_the_television(bridge)
    assert bridge.self_update.relay_wait()["phase"] == "probe"
    assert screen["status"].text == PROBE_LINE
    jobs.run()
    screen.refresh()
    assert screen["status"].text == relay_line(120)
    assert STARTED not in screen["status"].text


def test_the_screen_counts_down_the_wait_for_home_assistant(box, mono):
    bridge = test_selfupdate_relay.offline(box())
    screen = install_at_the_television(bridge)
    assert screen["status"].text == relay_line(120)
    mono.now += 30
    screen.refresh()
    assert screen["status"].text == relay_line(90)


def test_no_answer_from_home_assistant_is_said_in_the_households_words(box, factory):
    bridge = test_selfupdate_relay.offline(box())
    screen = install_at_the_television(bridge)
    MainLoop.advance(test_selfupdate_relay.WAIT_MS)
    screen.refresh()
    assert screen["status"].text == NO_RELAY_SAID
    assert refusal(factory.client)[0] == "no_relay"
    assert directories(bridge.root) == []


def test_without_an_integration_the_screen_says_it_at_once(box):
    bridge = test_selfupdate_relay.offline(box(), integration=False)
    screen = install_at_the_television(bridge)
    assert screen["status"].text == NO_RELAY_SAID


def test_an_answer_this_clock_calls_expired_is_said_as_such(box, factory, monkeypatch):
    bridge = test_selfupdate_relay.offline(box())
    monkeypatch.setattr(selfupdate.SelfUpdater, "clock", staticmethod(lambda: NOW + 700))
    screen = install_at_the_television(bridge)
    test_selfupdate_relay.reply(factory, test_selfupdate_relay.answer_for(
        factory, expires=NOW + 600))
    MainLoop.advance(test_selfupdate_relay.WAIT_MS)
    screen.refresh()
    assert screen["status"].text == CLOCK_SKEW_SAID
    assert "Home Assistant" not in screen["status"].text


def test_an_answer_taken_starts_the_update_and_the_screen_says_so(box, factory):
    bridge = test_selfupdate_relay.offline(box())
    screen = install_at_the_television(bridge)
    test_selfupdate_relay.reply(factory, test_selfupdate_relay.answer_for(factory))
    screen.refresh()
    status = screen["status"].text
    assert STARTED in status
    assert "Update to 0.4.0, started on the television: downloading" in status
    assert request_of(directories(bridge.root)[0])["relay"]["url"] == test_selfupdate_relay.URL


def test_a_wait_the_page_started_is_shown_but_its_end_is_not_the_screens(box):
    bridge = test_selfupdate_relay.offline(box())
    screen = screen_of(bridge)
    assert bridge.run_command("update", json.dumps({"version": "0.4.0"}), PAGE) is None
    screen.refresh()
    assert screen["status"].text == relay_line(120)
    MainLoop.advance(test_selfupdate_relay.WAIT_MS)
    screen.refresh()
    assert screen["status"].text == ""


def test_the_page_shows_the_relay_handshake(box, page, no_internet, monkeypatch):
    bridge = test_selfupdate_relay.unprobed(test_selfupdate_relay.offline(box()))
    jobs = test_selfupdate_relay.held_back(monkeypatch)
    resource = page(bridge)
    session = new_session()
    _request, body = post(resource, session, install_fields("0.4.0"))
    request, body = post(resource, session, confirmation(body), csrf=None)
    text = html.unescape(body.decode("utf-8"))
    assert request.response_code == 200
    assert PROBE_LINE in text and STARTED not in text
    jobs.run()
    text = html.unescape(get_page(resource)[1].decode("utf-8"))
    assert relay_line(120) in text
    MainLoop.advance(test_selfupdate_relay.WAIT_MS)
    text = html.unescape(get_page(resource)[1].decode("utf-8"))
    assert NO_RELAY_SAID in text and "Waiting for the answer" not in text


def test_the_polish_relay_sentences():
    """Spec ae.6: the television's sentence when neither the internet nor Home Assistant can
    deliver the package, word for word."""
    from test_locale import LOCALE, catalogue

    entries = catalogue(LOCALE / "pl" / "LC_MESSAGES" / "MQTTBridge.po")
    assert entries[NO_RELAY_SAID] == ("Dekoder nie ma dostępu do internetu, a Home Assistant nie "
                                      "odpowiedział. Instalacja nie jest możliwa.")
    # Only what the receiver knows: an answer came, and its clock called it expired.
    assert "Home Assistant" not in entries[CLOCK_SKEW_SAID]


def test_a_cross_site_post_behind_closed_doors_is_refused_and_keeps_the_confirmation(
        box, page, factory):
    """S-d1 (E1): the same-origin check comes first, so another site cannot drop a pending
    confirmation by posting while the doors are closed."""
    bridge = box()
    resource = page(bridge)
    session = new_session()
    _request, body = post(resource, session, install_fields("0.4.0"))
    pending = dict(session.sessionNamespaces[webif.CONFIRM_KEY])
    directory = accepted(bridge, factory)
    helper_says(directory, phase="installing")
    tick()
    assert bridge.self_update.closed
    request, _body = post(resource, session, install_fields("0.4.0"),
                          origin="http://attacker.example")
    assert request.response_code == 403
    assert session.sessionNamespaces[webif.CONFIRM_KEY] == pending


@pytest.mark.parametrize(("age", "expired"), [(599, False), (601, True)])
def test_a_page_confirmation_lasts_ten_minutes(age, expired, box, page):
    """S-d1 (E3): ten minutes, written out rather than read from the code."""
    bridge = box()
    resource = page(bridge)
    session = new_session()
    _request, body = post(resource, session, install_fields("0.4.0"))
    session.sessionNamespaces[webif.CONFIRM_KEY]["asked"] -= age
    request, body = post(resource, session, confirmation(body), csrf=None)
    assert (request.response_code == 403) is expired
    assert (b"more than ten minutes ago" in body) is expired
    assert len(directories(bridge.root)) == (0 if expired else 1)


CHECK_SENTENCES = {
    "The last check could not reach the list of versions on the internet.":
        [updatecheck.ERROR_UNREACHABLE],
    "The last check could not download the list of versions.":
        [updatecheck.ERROR_REDIRECT, updatecheck.ERROR_HTTP],
    "The plugin's list of versions has an invalid signature or is older than the one already "
    "known. Nothing was changed.":
        sorted(set(trust.REASONS) | {updatecheck.MALFORMED_RELAY, updatecheck.TOO_LARGE}),
    "The last check could not save the list of versions; the receiver's memory may be full.":
        [trustfile.WRITE_FAILED],
    "The last check could not save the list of versions. Try again in a moment.":
        [trustfile.TRUST_BUSY],
    "The receiver's record of the list of versions cannot be read, so no list is checked.":
        [trustfile.ERROR_BAD_MEMORY, trustfile.ERROR_BAD_KEYS],
    "The last check failed; the plugin log says why.": [updatecheck.ERROR_INTERNAL],
}


@pytest.mark.parametrize(("sentence", "code"), [
    (sentence, code) for sentence, codes in CHECK_SENTENCES.items() for code in codes])
def test_each_check_error_says_what_its_group_means(sentence, code, box):
    """S-d1 (E7): not only no raw code - the sentence of the code's own group."""
    bridge = box()
    bridge.updates._check_error = code
    assert updateview.header(bridge)[-1] == sentence


def test_every_check_error_code_has_a_group():
    grouped = {code for codes in CHECK_SENTENCES.values() for code in codes}
    assert grouped == set(CHECK_ERRORS)


def test_a_wait_that_ends_in_a_refusal_names_its_version_on_the_page(box, page, no_internet,
                                                                     monkeypatch):
    """The table is asked again when the look at the origin answers; a refusal that names the
    version names the one the wait was for, which the page itself does not know."""
    bridge = test_selfupdate_relay.unprobed(test_selfupdate_relay.offline(box()))
    jobs = test_selfupdate_relay.held_back(monkeypatch)
    resource = page(bridge)
    session = new_session()
    _request, body = post(resource, session, install_fields("0.4.0"))
    post(resource, session, confirmation(body), csrf=None)
    hold(bridge, [release("0.3.0"), release("0.2.5"), release("0.2.0")])
    jobs.run()
    text = html.unescape(get_page(resource)[1].decode("utf-8"))
    assert ("Version 0.4.0 is not on the plugin's signed list of versions. Check for updates "
            "and try again.") in text
