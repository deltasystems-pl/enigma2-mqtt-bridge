"""The relay handshake: an install asked for at the television or on the page, with no internet.

Spec ae.6 "Relay handshake": when the origin is unreachable, the plugin asks Home Assistant on
`relay_request` and takes `cmd/relay` only for the id it is waiting for, within 120 s; anything
else is logged and dropped. Spec ae.4: a request that carries a relay never fetches from the
origin, and nothing is probed to decide - the last check's `update.origin` is the answer.

The receiver is `test_selfupdate.py`'s; its fixtures and helpers are reused as they are.
`test_relay_contract.py` feeds the plugin what the companion integration really sends.
"""

import json
import secrets

import pytest
import test_selfupdate
from conftest import ConsoleAppContainer, MainLoop
from test_selfupdate import BUSY, LAST_ERROR, NODE, NOW, ROOT, directories, refusal, send

from MQTTBridge import selfupdate, updatehelper
from MQTTBridge.origin import MQTT, PAGE, SCREEN
from MQTTBridge.updatecheck import REACHABLE, UNKNOWN, UNREACHABLE, UpdateChecker

# `test_selfupdate.py`'s receiver, by the names its fixtures are asked for.
box = test_selfupdate.box
tree = test_selfupdate.tree
mono = test_selfupdate.mono

RELAY_REQUEST = ROOT + "/relay_request"
RELAY_COMMAND = ROOT + "/cmd/relay"
TOKEN = "A" * 43
URL = "http://192.0.2.5:8123/api/enigma2_mqtt/relay/" + TOKEN
WAIT_MS = selfupdate.RELAY_WAIT_SECONDS * 1000


def offline(bridge, origin=UNREACHABLE):
    """What the last check found when it probed the release origin."""
    bridge.updates._origin = origin
    return bridge


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
    assert bridge.self_update.relay_wait() == {"version": "0.4.0", "seconds_left": 120}
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


@pytest.mark.parametrize("probe", [REACHABLE, UNKNOWN])
def test_an_origin_not_known_to_be_unreachable_is_tried_by_the_helper(probe, box, factory):
    bridge = offline(box(), probe)
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
    bridge.run_command("relay", json.dumps(answer_for(factory)), PAGE)
    assert directories(bridge.root) == []
    assert bridge.self_update.relay_wait() is not None


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
