"""The relay handshake: an install asked for at the television or on the page, with no internet.

When the origin is unreachable, the plugin asks Home Assistant on `relay_request` (TOPICS.md)
and takes `cmd/relay` only for the id it is waiting for, within 120 s; anything else is logged
and dropped. The consent rule (ADR-0015 decision 4): a request that carries a relay never fetches
from the origin; an install at the television or on the page decides on a fresh word about the
origin (`update.origin`, ten minutes), and asks for the check's probe first when it has none -
the explicit action is the consent - while a command over MQTT never probes (review round 1,
MF1).

The receiver is `test_selfupdate.py`'s; its fixtures and helpers are reused as they are.
`test_relay_contract.py` feeds the plugin what the companion integration really sends.
"""

import json
import secrets

import pytest
import test_selfupdate
from conftest import ConsoleAppContainer, MainLoop
from test_selfupdate import BUSY, LAST_ERROR, NODE, NOW, ROOT, directories, refusal, send
from updatelab import index_bytes, release

from MQTTBridge import selfupdate, trust, updatecheck, updatehelper
from MQTTBridge.origin import MQTT, PAGE, SCREEN
from MQTTBridge.updatecheck import REACHABLE, UNKNOWN, UNREACHABLE, Unreachable, UpdateChecker

# `test_selfupdate.py`'s receiver, by the names its fixtures are asked for.
box = test_selfupdate.box
tree = test_selfupdate.tree
mono = test_selfupdate.mono

# The scenarios below place releases around a running 0.3.0 (conftest.running_0_3_0).
pytestmark = pytest.mark.usefixtures("running_0_3_0")

RELAY_REQUEST = ROOT + "/relay_request"
RELAY_COMMAND = ROOT + "/cmd/relay"
TOKEN = "A" * 43
URL = "http://192.0.2.5:8123/api/enigma2_mqtt/relay/" + TOKEN
WAIT_MS = selfupdate.RELAY_WAIT_SECONDS * 1000


INTEGRATION_WORD = {"integration": "0.4.0", "contract": 1, "plugin_min": "0.2.0"}


def offline(bridge, origin=UNREACHABLE, integration=True):
    """What a probe of the release origin found a moment ago, and an integration that relays."""
    bridge.updates._origin = origin
    bridge.updates._origin_seen = bridge.updates.monotonic()
    if integration:
        announce(bridge)
    return bridge


def announce(bridge):
    """The integration's retained word on `enigma2mqtt/integration/<node>`, as the bridge
    hands it over (`test_selfupdate.py` sends it through the broker)."""
    bridge.self_update.on_integration(json.dumps(INTEGRATION_WORD).encode())


def unprobed(bridge, origin=UNKNOWN, age=None):
    """A word about the origin with no age (read back from the file), or one `age` s old."""
    bridge.updates._origin = origin
    bridge.updates._origin_seen = (None if age is None
                                   else bridge.updates.monotonic() - age)
    return bridge


class Origin:
    """The release origin as the probe meets it: answering, or not at all. Records each ask."""

    def __init__(self, answers=True, status=200):
        self.answers = answers
        self.status = status
        self.asked = []

    def __call__(self, url, cap, timeout):
        self.asked.append((url, cap, timeout))
        if not self.answers:
            raise Unreachable("no route to host")
        return self.status, b"a signature file"


@pytest.fixture
def still(monkeypatch):
    """The checker's monotonic clock, standing still, so an age is exactly what a test says."""
    monkeypatch.setattr(UpdateChecker, "monotonic", staticmethod(lambda: 10000.0))


@pytest.fixture
def origin(monkeypatch):
    fake = Origin()
    monkeypatch.setattr(UpdateChecker, "fetch", staticmethod(fake))
    return fake


def ask(bridge, version="0.4.0", origin=SCREEN, downgrade=False, **fields):
    """An install asked for at the television (or on the page): `SelfUpdater.request`."""
    return bridge.self_update.request(json.dumps(dict({"version": version}, **fields)),
                                      origin=origin, downgrade=downgrade)


def question(factory):
    entries = factory.client.all_for(RELAY_REQUEST)
    assert len(entries) == 1, entries
    return entries[0]


def answer_for(factory, **changes):
    body = dict({"id": question(factory).json()["id"], "version": "0.4.0", "url": URL,
                 "expires": NOW + 600}, **changes)
    return body


def reply(factory, body, retain=False):
    data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    factory.client.fire_message(RELAY_COMMAND, data, retain=retain)


def request_file(directory):
    return json.loads((directory / "request.json").read_text())


def last_errors(factory, since):
    return [e for e in factory.client.published[since:] if e.topic == LAST_ERROR]


# ------------------------------------------------------------- one address rule --

GOOD = {"url": URL, "expires": NOW + 60}


@pytest.mark.parametrize(("relay", "ok"), [
    (GOOD, True),
    (dict(GOOD, url="https://192.0.2.5/api/enigma2_mqtt/relay/" + "-_" * 21 + "Z"), True),
    (dict(GOOD, url="http://homeassistant.local:8123/api/enigma2_mqtt/relay/" + TOKEN), True),
    (dict(GOOD, url=URL.replace(":8123", ":65535")), True),
    (dict(GOOD, url=URL.replace(":8123", ":1")), True),
    (dict(GOOD, expires=NOW + 1), True),
    # IPv4 only, project-wide: never a bracketed IPv6 literal, mapped or not.
    (dict(GOOD, url=URL.replace("192.0.2.5", "[2001:db8::5]")), False),
    (dict(GOOD, url=URL.replace("192.0.2.5", "[::ffff:192.0.2.5]")), False),
    (dict(GOOD, url=URL.replace("192.0.2.5", "[::1]")), False),
    (dict(GOOD, url=URL.replace("http://", "http://user@")), False),
    (dict(GOOD, url=URL.replace("http://", "http://user:secret@")), False),
    (dict(GOOD, url=URL.replace(":8123", ":0")), False),
    (dict(GOOD, url=URL.replace(":8123", ":65536")), False),
    (dict(GOOD, url=URL.replace(":8123", ":99999")), False),
    (dict(GOOD, url=URL.replace(":8123", ":")), False),
    (dict(GOOD, url=URL + "/"), False),
    (dict(GOOD, url=URL + "?x=1"), False),
    (dict(GOOD, url=URL + "#x"), False),
    (dict(GOOD, url=URL + "\n"), False),
    (dict(GOOD, url=URL[:-1]), False),
    (dict(GOOD, url=URL + "A"), False),
    (dict(GOOD, url=URL[:-1] + "="), False),
    (dict(GOOD, url=URL.replace("http://", "ftp://")), False),
    (dict(GOOD, url=URL.replace("http://", "HTTP://")), False),
    (dict(GOOD, url=URL.replace("/relay/", "/relay/x/")), False),
    (dict(GOOD, url=URL.replace("192.0.2.5", "home_assistant")), False),
    (dict(GOOD, url=URL.replace("192.0.2.5", "\u0661\u0669\u0662.0.2.5")), False),
    (dict(GOOD, expires=NOW), False),
    (dict(GOOD, expires=NOW - 1), False),
    (dict(GOOD, expires=True), False),
    (dict(GOOD, expires=float(NOW + 60)), False),
    (dict(GOOD, expires=str(NOW + 60)), False),
    ({"url": URL}, False),
    ({"expires": NOW + 60}, False),
    (dict(GOOD, url=None), False),
    ("not an object", False),
    (None, False),
])
def test_one_rule_judges_every_relay_address(relay, ok, box, factory):
    assert updatehelper.relay_ok(relay, NOW) is ok
    if relay is None:
        return  # `"relay": null` in `cmd/update` is no relay at all: TOPICS.md, `cmd/update`
    bridge = box()
    before = len(factory.client.published)
    send(factory, {"version": "0.4.0", "relay": relay})
    if ok:
        assert refusal(factory.client) is None
        assert request_file(directories(bridge.root)[0])["relay"] == relay
    else:
        assert refusal(factory.client) == ("relay", selfupdate.RELAY)
        assert directories(bridge.root) == []
        assert last_errors(factory, before)


def test_the_plugin_and_the_helper_ask_the_same_function(box, monkeypatch):
    """One rule, not two copies of it: both sides change when it changes."""
    asked = []
    odd = {"url": "anything at all", "expires": 0}
    monkeypatch.setattr(updatehelper, "relay_ok",
                        lambda relay, now: asked.append(("rule", relay)) or relay == odd)
    bridge = box()
    assert bridge.self_update.request(json.dumps({"version": "0.4.0", "relay": odd})) is None
    assert ("rule", odd) in asked

    class Helper:
        request = {"relay": {"url": URL, "expires": NOW + 60}}

        class receiver:
            @staticmethod
            def now():
                return NOW

    with pytest.raises(updatehelper.Fail) as failed:
        updatehelper.Transaction.check_relay(Helper())
    assert failed.value.reason == "relay"
    assert asked[-1] == ("rule", Helper.request["relay"])
    assert not hasattr(selfupdate, "RELAY_URL")


# ----------------------------------------------------------------- the question --


@pytest.mark.parametrize("origin", [SCREEN, PAGE])
def test_an_install_at_the_receiver_without_internet_asks_home_assistant(origin, box, factory):
    bridge = offline(box())
    if origin == PAGE:
        assert bridge.run_command("update", json.dumps({"version": "0.4.0"}), PAGE) is None
    else:
        assert ask(bridge) is None
    entry = question(factory)
    assert (entry.qos, entry.retain) == (1, False)
    body = entry.json()
    assert set(body) == {"id", "version", "serial"}
    assert body["version"] == "0.4.0" and body["serial"] == 1
    assert updatehelper.TRANSACTION_ID.fullmatch(body["id"])
    # Nothing starts before the answer, and nothing about it goes on `last_error`.
    assert directories(bridge.root) == [] and ConsoleAppContainer.instances == []
    assert refusal(factory.client) is None
    assert bridge.self_update.relay_wait() == {"version": "0.4.0", "seconds_left": 120,
                                               "phase": "relay"}
    # Not remembered as a retained topic of the node: nothing to retract later.
    assert not bridge.state.knows(RELAY_REQUEST)


def test_latest_is_resolved_before_home_assistant_is_asked(box, factory):
    bridge = offline(box())
    assert ask(bridge, "latest") is None
    assert question(factory).json()["version"] == "0.4.0"


def test_every_refusal_comes_before_the_question(box, factory, receiver):
    bridge = offline(box())
    receiver.enter_standby()
    assert ask(bridge).reason == "standby"
    assert factory.client.all_for(RELAY_REQUEST) == []
    assert bridge.self_update.relay_wait() is None


@pytest.mark.parametrize("last", ["no_space", "rate_limited"])
def test_even_the_last_refusals_come_before_the_question(last, box, factory, monkeypatch):
    """Home Assistant downloads a package for a question: only an install that would start asks."""
    bridge = offline(box())
    if last == "no_space":
        monkeypatch.setattr(updatehelper.Receiver, "free_bytes", lambda self, path: 0)
    else:
        updatehelper.write_json(str(bridge.root / updatehelper.LAST),
                                {"id": "0123456789ab", "finished": NOW - 60, "result": "failed"})
    assert ask(bridge).reason == last
    assert factory.client.all_for(RELAY_REQUEST) == []


def test_an_origin_that_answered_is_tried_by_the_helper(box, factory):
    bridge = offline(box(), REACHABLE)
    assert ask(bridge) is None
    assert factory.client.all_for(RELAY_REQUEST) == []
    assert request_file(directories(bridge.root)[0])["relay"] is None


def test_a_command_over_mqtt_never_asks_back(box, factory):
    """Home Assistant sends its address with `cmd/update`; a broker client that sends none
    asked the receiver to fetch."""
    bridge = offline(box())
    send(factory, {"version": "0.4.0"})
    assert refusal(factory.client) is None
    assert factory.client.all_for(RELAY_REQUEST) == []
    assert request_file(directories(bridge.root)[0])["started_by"] == "mqtt"


def test_a_request_that_carries_a_relay_asks_nobody_and_fetches_nothing(box, factory,
                                                                         monkeypatch):
    fetched = []
    monkeypatch.setattr(UpdateChecker, "fetch", staticmethod(
        lambda *args, **kwargs: fetched.append(args)))
    bridge = offline(box())
    relay = {"url": URL, "expires": NOW + 60}
    assert bridge.self_update.request(json.dumps({"version": "0.4.0", "relay": relay}),
                                      origin=SCREEN) is None
    assert factory.client.all_for(RELAY_REQUEST) == []
    request = request_file(directories(bridge.root)[0])
    assert request["relay"] == relay and request["started_by"] == "screen"
    assert fetched == []


def test_without_a_broker_there_is_nobody_to_ask(box, factory):
    bridge = offline(box())
    factory.client.fire_disconnect()
    answer = ask(bridge)
    assert (answer.reason, str(answer)) == ("no_relay", selfupdate.NO_RELAY)
    assert factory.client.all_for(RELAY_REQUEST) == []
    assert bridge.self_update.relay_wait() is None


def test_a_question_the_client_would_not_take_is_no_wait(box, factory):
    bridge = offline(box())
    factory.client.publish_rc = 4
    assert ask(bridge).reason == "no_relay"
    assert bridge.self_update.relay_wait() is None


# ------------------------------------------------------------------- the answer --


@pytest.mark.parametrize("origin", [SCREEN, PAGE])
def test_the_answer_starts_the_helper_with_home_assistants_address(origin, box, factory):
    bridge = offline(box())
    assert ask(bridge, origin=origin, sha256=updatehelper_sha(bridge)) is None
    before = len(factory.client.published)
    body = answer_for(factory)
    reply(factory, body)
    (directory,) = directories(bridge.root)
    request = request_file(directory)
    assert request["relay"] == {"url": URL, "expires": NOW + 600}
    assert request["target"] == "0.4.0" and request["downgrade"] is False
    # The relay is transport, not a trigger: the install is still the one asked for there.
    assert request["started_by"] == origin
    assert request["sha256"] == updatehelper_sha(bridge)
    assert bridge.self_update.relay_wait() is None and bridge.self_update.relay_refusal is None
    assert [e for e in last_errors(factory, before) if e.text] == []


def updatehelper_sha(bridge):
    index = bridge.updates._held[0]
    return next(e["sha256"] for e in index["releases"] if e["version"] == "0.4.0")


def test_a_downgrade_chosen_at_the_television_stays_one(box, factory):
    bridge = offline(box())
    assert bridge.self_update.request(json.dumps({"version": "0.2.5"}), origin=SCREEN,
                                      downgrade=True) is None
    reply(factory, answer_for(factory, version="0.2.5"))
    request = request_file(directories(bridge.root)[0])
    assert (request["target"], request["downgrade"], request["started_by"]) == \
        ("0.2.5", True, "screen")


def bad_answers():
    return [
        ("another id", lambda body: dict(body, id="0123456789ab")),
        ("no id", lambda body: {k: v for k, v in body.items() if k != "id"}),
        ("another version", lambda body: dict(body, version="0.2.5")),
        ("no version", lambda body: {k: v for k, v in body.items() if k != "version"}),
        ("an IPv6 host", lambda body: dict(body, url=URL.replace("192.0.2.5", "[2001:db8::5]"))),
        ("a user part", lambda body: dict(body, url=URL.replace("http://", "http://u:p@"))),
        ("another path", lambda body: dict(body, url=URL.replace("/relay/", "/files/"))),
        ("expired", lambda body: dict(body, expires=NOW)),
        ("expires as text", lambda body: dict(body, expires=str(NOW + 600))),
        ("no expiry", lambda body: {k: v for k, v in body.items() if k != "expires"}),
        ("not JSON", lambda body: b"relay me"),
        ("a list", lambda body: [body]),
        ("empty", lambda body: b""),
    ]


@pytest.mark.parametrize(("case", "spoil"), bad_answers(), ids=[c for c, _ in bad_answers()])
def test_an_answer_that_is_not_the_awaited_one_is_dropped_and_the_wait_goes_on(
        case, spoil, box, factory):
    bridge = offline(box())
    assert ask(bridge) is None
    good = answer_for(factory)
    before = len(factory.client.published)
    reply(factory, spoil(good))
    assert directories(bridge.root) == [], case
    assert last_errors(factory, before) == [], case
    assert bridge.self_update.relay_wait() is not None, case
    # The real answer that follows is still taken.
    reply(factory, good)
    assert len(directories(bridge.root)) == 1, case


def test_a_retained_answer_is_dropped(box, factory):
    bridge = offline(box())
    assert ask(bridge) is None
    reply(factory, answer_for(factory), retain=True)
    assert directories(bridge.root) == []
    assert bridge.self_update.relay_wait() is not None


def test_an_answer_nobody_waits_for_is_dropped_quietly(box, factory, monkeypatch):
    raised = []
    monkeypatch.setattr(selfupdate.LOG, "exception", lambda *args, **kwargs: raised.append(args))
    bridge = box()
    before = len(factory.client.published)
    reply(factory, {"id": "0123456789ab", "version": "0.4.0", "url": URL, "expires": NOW + 600})
    assert directories(bridge.root) == []
    assert factory.client.published[before:] == []
    # Dropped by a decision, not by an exception the guard around it happened to catch.
    assert raised == []


def test_a_bool_is_no_expiry_even_against_a_clock_in_1970():
    """`True > 0` - a receiver whose clock stands at the epoch would take it otherwise."""
    assert updatehelper.relay_ok({"url": URL, "expires": 5}, 0) is True
    assert updatehelper.relay_ok({"url": URL, "expires": True}, 0) is False


def test_a_second_answer_after_the_first_was_taken_is_stale(box, factory):
    bridge = offline(box())
    assert ask(bridge) is None
    body = answer_for(factory)
    reply(factory, body)
    before = len(factory.client.published)
    reply(factory, body)
    assert len(directories(bridge.root)) == 1
    assert last_errors(factory, before) == []


def test_the_page_cannot_hand_in_an_answer(box, factory):
    bridge = offline(box())
    assert ask(bridge) is None
    bridge.publish_last_error("update", "an earlier refusal")
    before = len(factory.client.published)
    assert bridge.run_command("relay", json.dumps(answer_for(factory)), PAGE) is None
    assert directories(bridge.root) == []
    assert bridge.self_update.relay_wait() is not None
    # Nothing at all: not even the clear of `last_error` that a command which ran would send.
    assert last_errors(factory, before) == []
    assert refusal(factory.client) is not None


# ---------------------------------------------------------------- the 120 s bound --


def test_an_answer_just_inside_the_bound_is_taken(box, factory, mono):
    bridge = offline(box())
    assert ask(bridge) is None
    MainLoop.advance(WAIT_MS - 100)
    mono.now += selfupdate.RELAY_WAIT_SECONDS - 0.1
    assert bridge.self_update.relay_wait()["seconds_left"] == 1
    reply(factory, answer_for(factory))
    assert len(directories(bridge.root)) == 1


def test_no_answer_within_120_s_refuses_the_install(box, factory, mono):
    bridge = offline(box())
    assert ask(bridge) is None
    body = answer_for(factory)
    MainLoop.advance(WAIT_MS - 1)
    assert bridge.self_update.relay_wait() is not None
    assert refusal(factory.client) is None
    MainLoop.advance(1)
    assert refusal(factory.client) == ("no_relay", selfupdate.NO_RELAY)
    assert factory.client.last(LAST_ERROR).json()["cmd"] == "update"
    assert bridge.self_update.relay_wait() is None
    assert bridge.self_update.relay_refusal.reason == "no_relay"
    # An answer after that is late, and is dropped.
    mono.now += selfupdate.RELAY_WAIT_SECONDS + 1
    reply(factory, body)
    assert directories(bridge.root) == []


def test_an_answer_read_at_the_bound_is_late_even_before_the_timer_has_run(box, factory, mono):
    """A main loop busy elsewhere does not stretch the bound."""
    bridge = offline(box())
    assert ask(bridge) is None
    mono.now += selfupdate.RELAY_WAIT_SECONDS
    reply(factory, answer_for(factory))
    assert directories(bridge.root) == []
    assert refusal(factory.client) == ("no_relay", selfupdate.NO_RELAY)
    assert bridge.self_update.relay_wait() is None


def test_a_plugin_that_stops_stops_waiting(box, factory):
    bridge = offline(box())
    assert ask(bridge) is None
    body = answer_for(factory)
    bridge.self_update.abandon()
    before = len(factory.client.published)
    MainLoop.advance(WAIT_MS)
    reply(factory, body)
    assert directories(bridge.root) == []
    assert factory.client.published[before:] == []


# --------------------------------------------------- the table, asked once more --


def test_the_table_is_asked_again_when_the_answer_comes(box, factory, receiver):
    bridge = offline(box())
    assert ask(bridge) is None
    receiver.enter_standby()
    reply(factory, answer_for(factory))
    assert directories(bridge.root) == []
    assert refusal(factory.client)[0] == "standby"
    assert factory.client.last(LAST_ERROR).json()["cmd"] == "update"
    assert bridge.self_update.relay_refusal.reason == "standby"


def test_the_next_request_forgets_how_the_last_wait_ended(box, factory, mono):
    bridge = offline(box())
    assert ask(bridge) is None
    MainLoop.advance(WAIT_MS)
    assert bridge.self_update.relay_refusal is not None
    assert ask(bridge) is None
    assert bridge.self_update.relay_refusal is None


@pytest.mark.parametrize("origin", [MQTT, PAGE, SCREEN])
def test_while_waiting_another_update_is_busy(origin, box, factory):
    bridge = offline(box())
    assert ask(bridge) is None
    other = bridge.self_update.request(json.dumps({"version": "0.4.0"}), origin=origin)
    assert (other.reason, str(other)) == ("busy", BUSY)
    assert len(factory.client.all_for(RELAY_REQUEST)) == 1


def test_while_waiting_an_uninstall_is_busy(box, factory):
    bridge = offline(box())
    assert ask(bridge) is None
    assert directories(bridge.root) == [] and bridge.self_update.relay_wait() is not None
    refused = bridge.uninstaller.request(NODE, origin=PAGE)
    assert refused.reason == "busy"


def test_each_question_has_its_own_id(box, factory, mono):
    bridge = offline(box())
    assert ask(bridge) is None
    MainLoop.advance(WAIT_MS)
    assert ask(bridge) is None
    ids = [e.json()["id"] for e in factory.client.all_for(RELAY_REQUEST)]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_the_old_id_is_not_taken_by_the_new_question(box, factory, mono):
    bridge = offline(box())
    assert ask(bridge) is None
    first = question(factory).json()["id"]
    MainLoop.advance(WAIT_MS)
    assert ask(bridge) is None
    reply(factory, {"id": first, "version": "0.4.0", "url": URL, "expires": NOW + 600})
    assert directories(bridge.root) == []
    assert bridge.self_update.relay_wait() is not None


def test_ids_are_not_predictable_from_the_clock(monkeypatch, box, factory):
    drawn = []
    real = secrets.token_hex
    monkeypatch.setattr(selfupdate.secrets, "token_hex",
                        lambda n: drawn.append(n) or real(n))
    bridge = offline(box())
    assert ask(bridge) is None
    assert drawn == [6]


# ------------------------------------------ review round 1: looking at the origin (MF1) --

PROBE_MS = selfupdate.PROBE_WAIT_SECONDS * 1000


def latest_question(factory):
    return factory.client.all_for(RELAY_REQUEST)[-1]


class Jobs(list):
    """The worker's jobs, waiting until a test runs them; `run()` runs them all, in order."""

    def run(self):
        while self:
            self.pop(0)()


def held_back(monkeypatch):
    """A worker that has not had its turn yet: the jobs wait in a list until a test runs them.

    Asked for once the receiver is built, so its own first job - loading the files - has run.
    """
    jobs = Jobs()
    monkeypatch.setattr(UpdateChecker, "run_in_background", staticmethod(jobs.append))
    return jobs


@pytest.mark.parametrize("where", [SCREEN, PAGE])
def test_a_fresh_box_looks_at_the_origin_and_then_asks_home_assistant(where, box, factory,
                                                                      origin):
    """Scenario A of the review: defaults, no internet, an index Home Assistant relayed."""
    origin.answers = False
    bridge = unprobed(offline(box()))
    assert not bridge.value("update_check")
    assert bridge.updates._held[1] == "relay"
    if where == PAGE:
        assert bridge.run_command("update", json.dumps({"version": "0.4.0"}), PAGE) is None
    else:
        assert ask(bridge) is None
    # The check's own probe, once: the signature file, five seconds, 1 KiB.
    assert origin.asked == [(bridge.updates.origin + trust.SIGNATURE_FILE,
                             trust.MAX_SIGNATURE_BYTES, updatecheck.PROBE_TIMEOUT)]
    assert bridge.updates.reachability == UNREACHABLE and bridge.updates.origin_fresh()
    assert question(factory).json()["version"] == "0.4.0"
    assert directories(bridge.root) == []
    assert bridge.self_update.relay_wait()["phase"] == "relay"
    reply(factory, answer_for(factory))
    request = request_file(directories(bridge.root)[0])
    assert request["relay"]["url"] == URL and request["started_by"] == where


@pytest.mark.parametrize("age", [None, updatecheck.ORIGIN_FRESH_SECONDS])
def test_an_old_unreachable_is_looked_at_again_and_the_helper_fetches(age, box, factory, origin,
                                                                       still):
    """Scenario B of the review: the internet came back since the word was written."""
    bridge = unprobed(offline(box()), UNREACHABLE, age)
    assert ask(bridge) is None
    assert len(origin.asked) == 1
    assert bridge.updates.reachability == REACHABLE
    # No 120 s wait for an answer nobody needs: the helper fetches from the origin itself.
    assert factory.client.all_for(RELAY_REQUEST) == []
    assert bridge.self_update.relay_wait() is None
    assert request_file(directories(bridge.root)[0])["relay"] is None


@pytest.mark.parametrize("age", [0, updatecheck.ORIGIN_FRESH_SECONDS - 1])
def test_a_fresh_word_is_not_looked_at_again(age, box, factory, origin, still):
    bridge = unprobed(offline(box()), UNREACHABLE, age)
    assert ask(bridge) is None
    assert origin.asked == []
    assert question(factory).json()["version"] == "0.4.0"


def test_a_check_just_run_is_a_fresh_word(box, factory, origin):
    """The check probes first too: an install right after it does not look again."""
    origin.answers = False
    bridge = unprobed(offline(box()))
    assert bridge.updates.keys
    assert bridge.updates.request_check(PAGE) is None
    assert len(origin.asked) == 1
    assert bridge.updates.reachability == UNREACHABLE and bridge.updates.origin_fresh()
    assert ask(bridge) is None
    assert len(origin.asked) == 1
    assert question(factory).json()["version"] == "0.4.0"


def test_the_probe_goes_to_disk_like_a_checks(box, factory, origin):
    origin.answers = False
    bridge = unprobed(offline(box()))
    assert ask(bridge) is None
    checks = updatecheck.read_checks(bridge.updates.check_path)
    assert checks[bridge.updates.lineage]["origin"] == UNREACHABLE
    # The `update` topic says it too.
    assert factory.client.last(test_selfupdate.UPDATE).json()["origin"] == UNREACHABLE


@pytest.mark.parametrize("where", [SCREEN, PAGE])
def test_a_fresh_box_without_an_integration_is_told_at_once(where, box, factory, origin):
    origin.answers = False
    bridge = unprobed(offline(box(), integration=False))
    if where == PAGE:
        said = bridge.run_command("update", json.dumps({"version": "0.4.0"}), PAGE)
        assert said == selfupdate.NO_RELAY
    else:
        answer = ask(bridge)
        assert (answer.reason, str(answer)) == ("no_relay", selfupdate.NO_RELAY)
    assert len(origin.asked) == 1
    assert factory.client.all_for(RELAY_REQUEST) == []
    assert bridge.self_update.relay_wait() is None


def test_a_command_over_mqtt_never_looks_at_the_origin(box, factory, origin):
    bridge = unprobed(offline(box()))
    send(factory, {"version": "0.4.0"})
    assert refusal(factory.client) is None
    assert origin.asked == []
    assert factory.client.all_for(RELAY_REQUEST) == []
    assert request_file(directories(bridge.root)[0])["started_by"] == "mqtt"


@pytest.mark.parametrize("refused", ["standby", "no_space", "rate_limited"])
def test_a_refused_install_never_looks_at_the_origin(refused, box, factory, origin, receiver,
                                                     monkeypatch):
    """The last refusals of the table too: the probe comes after all of them (review round 2)."""
    bridge = unprobed(offline(box()))
    if refused == "standby":
        receiver.enter_standby()
    elif refused == "no_space":
        monkeypatch.setattr(updatehelper.Receiver, "free_bytes", lambda self, path: 0)
    else:
        ended_as_the_helper_writes_it(bridge, uptime=test_selfupdate.UPTIME - 60)
    assert ask(bridge).reason == refused
    assert origin.asked == []
    assert factory.client.all_for(RELAY_REQUEST) == []


def test_while_the_origin_is_looked_at_the_install_waits_and_is_busy(box, factory, origin,
                                                                     monkeypatch):
    origin.answers = False
    bridge = unprobed(offline(box()))
    jobs = held_back(monkeypatch)
    assert ask(bridge) is None
    assert origin.asked == []
    assert bridge.self_update.relay_wait() == {"version": "0.4.0", "seconds_left": 60,
                                               "phase": "probe"}
    assert ask(bridge).reason == "busy"
    assert bridge.uninstaller.request(NODE, origin=PAGE).reason == "busy"
    assert refusal(factory.client) is None and directories(bridge.root) == []
    jobs.run()
    assert len(origin.asked) == 1
    assert question(factory).json()["version"] == "0.4.0"
    assert bridge.self_update.relay_wait()["phase"] == "relay"


def test_a_probe_that_answers_later_starts_the_helper_and_clears_last_error(box, factory,
                                                                            origin, monkeypatch):
    bridge = unprobed(offline(box()))
    jobs = held_back(monkeypatch)
    bridge.publish_last_error("update", "an earlier refusal")
    assert refusal(factory.client) is not None
    assert ask(bridge) is None
    jobs.run()
    assert request_file(directories(bridge.root)[0])["relay"] is None
    assert factory.client.last(LAST_ERROR).text == ""
    assert bridge.self_update.relay_refusal is None


def test_the_table_is_asked_again_when_the_probe_answers(box, factory, origin, monkeypatch,
                                                         receiver):
    bridge = unprobed(offline(box()))
    jobs = held_back(monkeypatch)
    assert ask(bridge) is None
    receiver.enter_standby()
    jobs.run()
    assert directories(bridge.root) == []
    assert refusal(factory.client)[0] == "standby"
    assert factory.client.last(LAST_ERROR).json()["cmd"] == "update"
    assert bridge.self_update.relay_refusal.reason == "standby"


def test_a_probe_that_does_not_answer_in_time_is_decided_without_it(box, factory, origin,
                                                                   monkeypatch):
    bridge = unprobed(offline(box()))
    jobs = held_back(monkeypatch)
    assert ask(bridge) is None
    MainLoop.advance(PROBE_MS - 1)
    assert directories(bridge.root) == [] and bridge.self_update.relay_wait() is not None
    MainLoop.advance(1)
    # Nothing new is known: the helper fetches itself - the install asked for is the consent.
    assert request_file(directories(bridge.root)[0])["relay"] is None
    assert factory.client.all_for(RELAY_REQUEST) == []
    # The probe that answers after that changes nothing, and says nothing.
    jobs.run()
    assert len(directories(bridge.root)) == 1
    assert factory.client.all_for(RELAY_REQUEST) == []
    assert refusal(factory.client) is None


def test_a_worker_that_cannot_start_does_not_leave_the_install_waiting(box, factory, origin,
                                                                      monkeypatch):
    def refuse(job):
        raise RuntimeError("can't start new thread")

    monkeypatch.setattr(UpdateChecker, "run_in_background", staticmethod(refuse))
    bridge = unprobed(offline(box()), UNREACHABLE)
    assert ask(bridge) is None
    # The word it has is all it has: unreachable, so Home Assistant is asked.
    assert question(factory).json()["version"] == "0.4.0"


def test_a_plugin_that_stops_while_the_origin_is_looked_at_starts_nothing(box, factory, origin,
                                                                         monkeypatch):
    bridge = unprobed(offline(box()))
    jobs = held_back(monkeypatch)
    assert ask(bridge) is None
    bridge.self_update.abandon()
    jobs.run()
    MainLoop.advance(PROBE_MS)
    assert directories(bridge.root) == []
    assert factory.client.all_for(RELAY_REQUEST) == []


@pytest.mark.parametrize(("reason", "record", "noted"), [
    ("download", {"origin": "unreachable"}, True),
    ("unreachable", {"origin": "unreachable"}, True),
    ("download", {"origin": "reachable"}, False),
    ("bad_package", {"origin": "reachable"}, False),
    ("download", {}, False),
])
def test_a_helper_that_could_not_reach_the_origin_makes_the_next_install_ask(
        reason, record, noted, box, factory, origin):
    bridge = offline(box(), REACHABLE)
    assert ask(bridge) is None
    (directory,) = directories(bridge.root)
    test_selfupdate.helper_says(directory, started_by="screen", phase="finished",
                                result="failed", reason=reason, error="it failed",
                                finished=NOW, record=record)
    test_selfupdate.tick()
    assert bridge.self_update.busy() is None
    assert bridge.updates.reachability == (UNREACHABLE if noted else REACHABLE)
    assert bridge.updates.origin_fresh()
    checks = updatecheck.read_checks(bridge.updates.check_path).get(bridge.updates.lineage, {})
    assert (checks.get("origin") == UNREACHABLE) is noted
    assert ask(bridge) is None
    assert origin.asked == []
    if noted:
        assert question(factory).json()["version"] == "0.4.0"
        assert len(directories(bridge.root)) == 1
    else:
        assert factory.client.all_for(RELAY_REQUEST) == []
        assert len(directories(bridge.root)) == 2


# ----------------------------------------------- review round 1: S1, S2, S3, S4 --


@pytest.mark.parametrize("look", ["busy", "relay_wait"])
def test_a_relay_wait_whose_timer_did_not_start_ends_by_its_age(look, box, factory, mono,
                                                                monkeypatch):
    bridge = offline(box())
    monkeypatch.setattr(bridge.self_update._relay_ticker, "start", lambda *a, **k: False)
    assert ask(bridge) is None
    MainLoop.advance(WAIT_MS * 2)
    assert refusal(factory.client) is None
    mono.now += selfupdate.RELAY_WAIT_SECONDS - 1
    assert bridge.self_update.relay_wait() is not None
    mono.now += 1
    if look == "busy":
        assert bridge.self_update.busy() is None
    else:
        assert bridge.self_update.relay_wait() is None
    assert refusal(factory.client) == ("no_relay", selfupdate.NO_RELAY)
    # Not busy for ever: the next install asks again.
    assert ask(bridge) is None
    assert len(factory.client.all_for(RELAY_REQUEST)) == 2


def test_a_probe_wait_whose_timer_did_not_start_ends_by_its_age(box, factory, origin, mono,
                                                                monkeypatch):
    bridge = unprobed(offline(box()))
    held_back(monkeypatch)
    monkeypatch.setattr(bridge.self_update._probe_ticker, "start", lambda *a, **k: False)
    assert ask(bridge) is None
    MainLoop.advance(PROBE_MS * 2)
    assert directories(bridge.root) == []
    mono.now += selfupdate.PROBE_WAIT_SECONDS
    assert bridge.self_update.relay_wait() is None
    assert len(directories(bridge.root)) == 1


def test_without_the_integrations_word_nobody_is_asked_and_nobody_waits(box, factory):
    bridge = offline(box(), integration=False)
    answer = ask(bridge)
    assert (answer.reason, str(answer)) == ("no_relay", selfupdate.NO_RELAY)
    assert factory.client.all_for(RELAY_REQUEST) == []
    assert bridge.self_update.relay_wait() is None and directories(bridge.root) == []
    # Once the integration has said it is there, the same install asks it...
    announce(bridge)
    assert ask(bridge) is None
    assert question(factory).json()["version"] == "0.4.0"
    # ... and once it has taken its word back, nobody is asked again.
    MainLoop.advance(WAIT_MS)
    bridge.self_update.on_integration(b"")
    assert ask(bridge).reason == "no_relay"
    assert len(factory.client.all_for(RELAY_REQUEST)) == 1


def test_an_answer_only_a_clock_ahead_calls_expired_says_so(box, factory, monkeypatch):
    bridge = offline(box())
    monkeypatch.setattr(selfupdate.SelfUpdater, "clock", staticmethod(lambda: NOW + 700))
    assert ask(bridge) is None
    # Home Assistant's address, valid for ten minutes by Home Assistant's clock.
    reply(factory, answer_for(factory, expires=NOW + 600))
    assert directories(bridge.root) == []
    assert bridge.self_update.relay_wait() is not None
    MainLoop.advance(WAIT_MS)
    assert refusal(factory.client) == ("clock_skew", selfupdate.CLOCK_SKEW)
    assert bridge.self_update.relay_refusal.reason == "clock_skew"


def test_a_clock_ahead_does_not_hide_a_good_answer_after_it(box, factory, monkeypatch):
    bridge = offline(box())
    monkeypatch.setattr(selfupdate.SelfUpdater, "clock", staticmethod(lambda: NOW + 700))
    assert ask(bridge) is None
    reply(factory, answer_for(factory, expires=NOW + 600))
    reply(factory, answer_for(factory, expires=NOW + 1300))
    assert len(directories(bridge.root)) == 1


@pytest.mark.parametrize("spoil", [
    lambda body: dict(body, url=URL.replace("http://", "http://u:p@"), expires=NOW - 1),
    lambda body: dict(body, expires=0),
    lambda body: dict(body, expires=True),
    lambda body: dict(body, version="0.2.5", expires=NOW - 1),
    lambda body: dict(body, id="0123456789ab", expires=NOW - 1),
], ids=["a user part", "zero", "a bool", "another version", "another id"])
def test_only_home_assistants_shape_counts_as_a_clock_that_differs(spoil, box, factory):
    bridge = offline(box())
    assert ask(bridge) is None
    reply(factory, spoil(answer_for(factory)))
    MainLoop.advance(WAIT_MS)
    assert refusal(factory.client) == ("no_relay", selfupdate.NO_RELAY)


def test_a_taken_answer_clears_what_the_last_wait_left_on_last_error(box, factory, mono):
    bridge = offline(box())
    assert ask(bridge) is None
    MainLoop.advance(WAIT_MS)
    assert refusal(factory.client)[0] == "no_relay"
    assert ask(bridge) is None
    reply(factory, {"id": latest_question(factory).json()["id"], "version": "0.4.0",
                    "url": URL, "expires": NOW + 600})
    assert len(directories(bridge.root)) == 1
    assert factory.client.last(LAST_ERROR).text == ""


def test_an_oversized_answer_is_discarded_before_it_is_read(box, factory):
    bridge = offline(box())
    assert ask(bridge) is None
    body = dict(answer_for(factory), padding="x" * 4096)
    assert len(json.dumps(body)) > 4096
    reply(factory, body)
    assert directories(bridge.root) == []
    assert bridge.self_update.relay_wait() is not None
    reply(factory, answer_for(factory))
    assert len(directories(bridge.root)) == 1


def test_the_question_names_the_serial_of_the_index_held(box, factory):
    bridge = offline(box())
    raw = index_bytes(7, [release("0.4.0"), release("0.3.0"), release("0.2.0")])
    bridge.updates._held = (trust.parse_index(raw), "relay")
    assert ask(bridge) is None
    assert question(factory).json()["serial"] == 7


@pytest.mark.parametrize("text", [b"[]", b"[1, 2]", b"5", b'"text"', b"null", b"true"])
def test_an_answer_that_is_not_an_object_is_dropped_by_a_decision(text, box, factory,
                                                                  monkeypatch):
    raised = []
    monkeypatch.setattr(selfupdate.LOG, "exception", lambda *args, **kwargs: raised.append(args))
    bridge = offline(box())
    assert ask(bridge) is None
    reply(factory, text)
    assert raised == []
    assert bridge.self_update.relay_wait() is not None


# ------------------------------------------------------------------- review round 2 --


def ended_as_the_helper_writes_it(bridge, uptime, reason="download", ident="0123456789ab"):
    """The last-transaction record in the shape `updatehelper.Helper.finish` writes it.

    The real helper writes it at every end it owns, and it is what starts the ten-minute limit
    between updates - measured on this boot's uptime, which is why `boot_id` and `uptime` are in.
    """
    updatehelper.write_json(str(bridge.root / updatehelper.LAST), {
        "id": ident, "started_by": "screen", "target": "0.4.0", "from": "0.3.0",
        "started": NOW - 30, "finished": NOW, "result": "failed", "reason": reason,
        "error": "it failed", "phase": "finished", "boot_id": test_selfupdate.BOOT,
        "uptime": uptime})


class Clocks:
    """Every clock the receiver has, moved together: the boot's uptime (`/proc/uptime` under
    the test's root), the plugin's monotonic clock and the update check's."""

    def __init__(self, bridge, mono, checker):
        self.bridge, self.mono, self.checker = bridge, mono, checker
        self.uptime = test_selfupdate.UPTIME

    def advance(self, seconds):
        self.uptime += seconds
        (self.bridge.root / "proc" / "uptime").write_text(f"{self.uptime:.2f} 1.00\n")
        self.mono.now += seconds
        self.checker.now += seconds


@pytest.fixture
def checker_clock(monkeypatch):
    clock = test_selfupdate.Clock()
    clock.now = 10000.0
    monkeypatch.setattr(UpdateChecker, "monotonic", staticmethod(clock))
    return clock


def a_download_that_got_no_answer(bridge, factory, reason="download"):
    """An install at the television whose helper could not reach the origin, ended as the real
    helper ends it: the finished status with `record.origin`, then the last-transaction record."""
    assert ask(bridge) is None
    (directory,) = directories(bridge.root)
    test_selfupdate.helper_says(directory, started_by="screen", phase="finished",
                                result="failed", reason=reason, error="it failed",
                                finished=NOW, record={"origin": "unreachable"})
    ended_as_the_helper_writes_it(bridge, uptime=test_selfupdate.UPTIME, reason=reason,
                                  ident=directory.name[len("update-"):])
    test_selfupdate.tick()
    assert bridge.self_update.busy() is None
    return directory


@pytest.mark.parametrize("reason", ["download", "unreachable"])
def test_the_helpers_word_outlives_the_limit_its_own_end_starts(reason, box, factory, origin,
                                                                mono, checker_clock):
    """D-S1: the next install the limit lets through asks Home Assistant without probing."""
    bridge = offline(box(), REACHABLE)
    clocks = Clocks(bridge, mono, checker_clock)
    a_download_that_got_no_answer(bridge, factory, reason)
    assert bridge.updates.reachability == UNREACHABLE
    # At once: the ten-minute limit, and no look at the origin.
    assert ask(bridge).reason == "rate_limited"
    clocks.advance(599)
    assert ask(bridge).reason == "rate_limited"
    assert origin.asked == [] and factory.client.all_for(RELAY_REQUEST) == []
    # The limit lifts; the helper's word still stands, so Home Assistant is asked at once.
    clocks.advance(1)
    assert ask(bridge) is None
    assert origin.asked == []
    assert question(factory).json()["version"] == "0.4.0"
    assert len(directories(bridge.root)) == 1


@pytest.mark.parametrize(("age", "looks"), [(1199, False), (1200, True)])
def test_the_helpers_word_lasts_the_limit_and_ten_minutes_more(age, looks, box, factory, origin,
                                                               mono, checker_clock):
    bridge = offline(box(), REACHABLE)
    clocks = Clocks(bridge, mono, checker_clock)
    a_download_that_got_no_answer(bridge, factory)
    clocks.advance(age)
    assert bridge.updates.origin_fresh() is not looks
    assert ask(bridge) is None
    assert len(origin.asked) == (1 if looks else 0)
    # A probe's own word is the usual ten minutes again: the origin answered this time.
    if looks:
        assert bridge.updates.reachability == REACHABLE
        clocks.advance(599)
        assert bridge.updates.origin_fresh()
        clocks.advance(1)
        assert not bridge.updates.origin_fresh()


@pytest.mark.parametrize(("age", "looks"), [(599, False), (600, True)])
def test_a_probes_word_lasts_ten_minutes(age, looks, box, factory, origin, still):
    """D-S4 (D7): the window is ten minutes, written out here rather than read from the code."""
    bridge = unprobed(offline(box()), UNREACHABLE, age)
    assert ask(bridge) is None
    assert len(origin.asked) == (1 if looks else 0)


@pytest.mark.parametrize("status", [500, 404, 301])
def test_any_answer_at_all_from_the_origin_is_reachable(status, box, factory, origin):
    """D-S4 (D8): only no answer is `unreachable` - an error page is an origin that answers."""
    origin.status = status
    bridge = unprobed(offline(box()))
    assert ask(bridge) is None
    assert len(origin.asked) == 1
    assert bridge.updates.reachability == REACHABLE
    assert factory.client.all_for(RELAY_REQUEST) == []
    assert request_file(directories(bridge.root)[0])["relay"] is None


def test_the_helpers_word_is_on_the_update_topic_at_once(box, factory, origin, monkeypatch):
    """D-S4 (D5): not only once the worker has written it to the check file."""
    bridge = offline(box(), REACHABLE)
    assert factory.client.last(test_selfupdate.UPDATE).json()["origin"] == REACHABLE
    jobs = held_back(monkeypatch)
    a_download_that_got_no_answer(bridge, factory)
    assert factory.client.last(test_selfupdate.UPDATE).json()["origin"] == UNREACHABLE
    assert jobs, "the note waits for the worker; the topic does not"
    jobs.run()
    checks = updatecheck.read_checks(bridge.updates.check_path)
    assert checks[bridge.updates.lineage]["origin"] == UNREACHABLE


def test_the_clock_sentence_says_only_what_the_receiver_knows():
    """D-S3: an answer with an address this clock calls expired may come from any broker client,
    so the sentence does not say that Home Assistant answered."""
    assert selfupdate.CLOCK_SKEW == (
        "an answer arrived whose download address had already expired by the receiver's clock; "
        "if the receiver's clock is wrong, set it and try again")


def test_noting_the_helpers_word_publishes_it(box, factory, monkeypatch):
    """`note_unreachable` says it on `update` itself, whoever calls it and whatever publishes
    next; the check file follows when the worker has its turn."""
    bridge = offline(box(), REACHABLE)
    jobs = held_back(monkeypatch)
    before = len(factory.client.all_for(test_selfupdate.UPDATE))
    bridge.updates.note_unreachable()
    updates = factory.client.all_for(test_selfupdate.UPDATE)
    assert len(updates) == before + 1
    assert updates[-1].json()["origin"] == UNREACHABLE
    assert jobs
