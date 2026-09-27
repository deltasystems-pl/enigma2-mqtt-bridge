"""The update check: the receiver's reader of the signed release index.

What these pin, in the words of the contract (docs/TOPICS.md, ADR-0015):

- `update_check` is a box-only setting, off by default, echoed read-only in `info.settings` and
  never writable through `cmd/config`; with it off the receiver makes no connection but the
  broker's, and `cmd/update_check` over MQTT is refused;
- a check probes the origin with the signature file (5 s), fetches the index (10 s, 64 KiB), judges
  it by the one rule, keeps it, and says so on the retained `update` topic; manual checks share a
  ten-minute limit, inside which the stored result is the answer;
- an index relayed on `enigma2mqtt/release_index` is judged by the same rule, needs no setting and
  causes no connection;
- nothing is believed before the signature verifies, and nothing is read past its cap.

The worker runs inline (`updatelab.inline`, set for every test by conftest), so a check is one call
and its answer the next line; one test runs it on a real thread.
"""

import json
import os
import ssl
import stat

import pytest
import updatelab
from updatelab import TEST_KEYS, FakeOrigin, relay_payload, release, signed

from MQTTBridge import trust

NODE = "vuuno4kse_005301"
UPDATE_TOPIC = "enigma2/" + NODE + "/update"
LAST_ERROR = "enigma2/" + NODE + "/last_error"
COMMAND = "enigma2/" + NODE + "/cmd/update_check"
RELEASE_INDEX = "enigma2mqtt/release_index"
NOW = 1790600000


class Clock:
    def __init__(self, now=NOW):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def origin():
    return FakeOrigin()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def box(make_bridge, factory, settings, origin, clock):
    """A connected bridge whose update check trusts the vectors' test keys and asks `origin`."""
    settings.host.value = "192.0.2.5"
    settings.node_id.value = NODE
    bridge = make_bridge()
    updates = bridge.updates
    updates.keys = TEST_KEYS
    updates.acceptance = False
    updates.fetch = origin
    updates.clock = clock
    bridge.start()
    factory.client.fire_connect()
    return bridge


def update_state(factory):
    published = factory.client.last(UPDATE_TOPIC)
    assert published is not None, "the update topic was never published"
    return published.json()


def last_error(factory):
    published = factory.client.last(LAST_ERROR)
    return published.json() if published is not None and published.text else None


def check(factory):
    factory.client.fire_message(COMMAND, "PRESS")


def four_releases():
    return [
        release("0.4.0"),
        release("0.3.0", self_update=False),
        release("0.2.0", self_update=False),
        release("0.1.0", contract=0, self_update=False),
    ]


# ----------------------------------------------------------------------- the setting --


def test_the_setting_is_box_only_and_off_by_default(settings):
    from MQTTBridge import config

    assert settings.update_check.value is False
    assert "update_check" in config.READ_ONLY_SETTING_NAMES
    assert "update_check" not in config.REMOTE_SETTING_NAMES
    assert config.SETTING_KINDS["update_check"] == "bool"
    assert "update_check" in config.SETTING_NAMES


def test_cmd_config_cannot_switch_it_on(box, factory, settings):
    factory.client.fire_message(
        "enigma2/" + NODE + "/cmd/config",
        json.dumps({"publish_keys": True, "screenshot": "on_zap", "screenshot_interval": 60,
                    "update_check": True}),
    )
    assert settings.update_check.value is False
    assert "unknown settings" in last_error(factory)["error"]


def test_info_settings_echoes_it_read_only(box, settings):
    assert box.build_info()["settings"]["update_check"] is False
    settings.update_check.value = True
    assert box.build_info()["settings"]["update_check"] is True


def test_the_provisioning_file_sets_it(tmp_path, settings):
    from MQTTBridge import config

    path = tmp_path / "mqttbridge.json"
    path.write_text(json.dumps({"update_check": True}), encoding="utf-8")
    assert config.import_provisioning(str(path), settings) == ["update_check"]
    assert settings.update_check.value is True


def test_the_setup_screen_and_the_page_show_it():
    from MQTTBridge import webif
    from MQTTBridge.setup import setting_labels

    assert "update_check" in dict(setting_labels())
    groups = dict(webif._grouped())
    assert "update_check" in groups["permissions"]


# ------------------------------------------------------------------- who may check --


def test_refused_over_mqtt_while_the_setting_is_off(box, factory, origin):
    check(factory)
    error = last_error(factory)
    assert error["cmd"] == "update_check"
    assert error["reason"] == "not_permitted"
    assert error["error"] == "checking for updates is switched off in the plugin's settings"
    assert origin.calls == []


def test_the_page_may_check_with_the_setting_off(box, origin):
    from MQTTBridge.origin import PAGE

    origin.serve(*signed(1))
    assert box.run_command("update_check", "", PAGE) is None
    assert origin.names() == [trust.SIGNATURE_FILE, trust.INDEX_FILE]


def test_no_connection_at_all_while_the_setting_is_off(box, factory, origin, clock):
    ticker = box.updates._ticker
    for hours in range(1, 50):
        clock.now = NOW + hours * 3600
        ticker.timer.fire()
    factory.client.fire_connect()
    assert origin.calls == []


# ----------------------------------------------------------------------- a check --


def test_a_check_probes_fetches_verifies_and_publishes(box, factory, settings, origin):
    settings.update_check.value = True
    origin.serve(*signed(1, four_releases()))
    check(factory)

    # The probe is the signature file, 5 s and 1 KiB; then the index, 10 s and 64 KiB.
    assert [(name, cap, timeout) for (_url, cap, timeout), name in
            zip(origin.calls, origin.names())] == [
        (trust.SIGNATURE_FILE, trust.MAX_SIGNATURE_BYTES, 5),
        (trust.INDEX_FILE, trust.MAX_INDEX_BYTES, 10),
    ]
    state = update_state(factory)
    assert state["origin"] == "reachable"
    assert state["checked"] == NOW
    assert state["check_error"] is None
    assert state["index"] == {"serial": 1, "issued": 1790500000, "source": "origin"}
    assert [entry["version"] for entry in state["available"]] == ["0.4.0", "0.3.0", "0.2.0"]
    assert state["latest_compatible"] == "0.4.0"
    assert state["transaction"] is None
    assert factory.client.last(UPDATE_TOPIC).retain is True
    assert last_error(factory) is None


def test_the_accepted_index_is_kept_across_a_restart(box, factory, settings, origin,
                                                     make_bridge, clock):
    settings.update_check.value = True
    origin.serve(*signed(3))
    check(factory)
    path = box.updates.path
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    box.stop()

    again = make_bridge()
    again.updates.keys = TEST_KEYS
    again.updates.fetch = FakeOrigin()
    again.updates.clock = clock
    again.start()
    factory.client.fire_connect()
    state = update_state(factory)
    assert state["index"] == {"serial": 3, "issued": 1790500000, "source": "origin"}
    assert state["checked"] == NOW
    assert again.updates.fetch.calls == []


def test_a_second_check_inside_ten_minutes_answers_from_the_stored_result(
        box, factory, settings, origin, clock):
    settings.update_check.value = True
    origin.serve(*signed(1))
    check(factory)
    assert len(origin.calls) == 2

    clock.now = NOW + 599
    check(factory)
    assert len(origin.calls) == 2
    assert last_error(factory) is None
    assert update_state(factory)["index"]["serial"] == 1

    clock.now = NOW + 600
    check(factory)
    assert len(origin.calls) == 4


def test_a_check_that_failed_has_spent_its_turn(box, factory, settings, origin, clock):
    settings.update_check.value = True
    origin.down()
    check(factory)
    state = update_state(factory)
    assert state["origin"] == "unreachable"
    assert state["check_error"] == "unreachable"
    assert len(origin.calls) == 1

    clock.now = NOW + 60
    check(factory)
    assert len(origin.calls) == 1


def test_a_stamp_from_a_clock_that_was_ahead_does_not_block(box, factory, settings, origin,
                                                            clock):
    settings.update_check.value = True
    origin.serve(*signed(1))
    check(factory)
    clock.now = NOW - 3600
    check(factory)
    assert len(origin.calls) == 4


def test_a_redirect_is_not_followed(box, factory, settings, origin):
    settings.update_check.value = True
    origin.answer(trust.SIGNATURE_FILE, 302, b"")
    check(factory)
    state = update_state(factory)
    assert state["origin"] == "reachable"
    assert state["check_error"] == "redirect"
    assert origin.names() == [trust.SIGNATURE_FILE]


def test_an_http_error_is_named(box, factory, settings, origin):
    settings.update_check.value = True
    origin.answer(trust.SIGNATURE_FILE, 404, b"gone")
    check(factory)
    assert update_state(factory)["check_error"] == "http_error"


def test_an_index_over_the_cap_is_refused_before_its_signature_is_looked_at(
        box, factory, settings, origin, monkeypatch):
    from MQTTBridge import ed25519

    settings.update_check.value = True
    index_raw, signature_raw = signed(1)
    origin.serve(index_raw + b" " * (trust.MAX_INDEX_BYTES + 10), signature_raw)

    def never(*_args):
        raise AssertionError("verified an index over the cap")

    monkeypatch.setattr(ed25519, "verify", never)
    check(factory)
    assert update_state(factory)["check_error"] == "too_large"
    assert origin.calls[1][1] == trust.MAX_INDEX_BYTES


def test_a_pair_torn_by_a_publication_is_read_once_more(box, factory, settings, origin):
    settings.update_check.value = True
    old_index, old_signature = signed(1)
    new_index, new_signature = signed(2)
    # The signature was read before the publication landed, the index after it.
    origin.serve(new_index, old_signature)
    origin.then(trust.SIGNATURE_FILE, 200, new_signature)
    check(factory)
    state = update_state(factory)
    assert state["check_error"] is None
    assert state["index"]["serial"] == 2
    assert len(origin.calls) == 4


def test_the_same_index_again_is_not_a_failure(box, factory, settings, origin, clock):
    settings.update_check.value = True
    origin.serve(*signed(1))
    check(factory)
    with open(box.updates.path, "rb") as handle:
        stored = json.loads(handle.read())
    clock.now = NOW + 3600
    check(factory)
    state = update_state(factory)
    assert state["check_error"] is None
    assert state["index"]["serial"] == 1
    with open(box.updates.path, "rb") as handle:
        again = json.loads(handle.read())
    assert again["release"] == stored["release"]


def test_an_older_index_from_the_origin_is_refused(box, factory, settings, origin, clock):
    settings.update_check.value = True
    origin.serve(*signed(5))
    check(factory)
    origin.serve(*signed(4))
    clock.now = NOW + 3600
    check(factory)
    state = update_state(factory)
    assert state["check_error"] == "replay"
    assert state["index"]["serial"] == 5


def test_an_unexpected_failure_is_reported_and_the_next_check_runs(box, factory, settings,
                                                                   clock):
    settings.update_check.value = True

    def broken(_url, _cap, _timeout):
        raise RuntimeError("something nobody expected")

    box.updates.fetch = broken
    check(factory)
    assert update_state(factory)["check_error"] == "internal_error"
    origin = FakeOrigin()
    origin.serve(*signed(1))
    box.updates.fetch = origin
    clock.now = NOW + 600
    check(factory)
    assert update_state(factory)["index"]["serial"] == 1


def test_a_check_runs_on_a_worker_thread_and_answers_on_the_main_loop(
        box, factory, settings, origin, wait_until):
    from MQTTBridge import updatecheck

    box.updates.run_in_background = updatecheck.in_thread
    settings.update_check.value = True
    origin.serve(*signed(1))
    check(factory)
    assert wait_until(lambda: factory.client.last(UPDATE_TOPIC) is not None
                      and update_state(factory)["index"] is not None)


# --------------------------------------------------------------------- the daily check --


def test_the_daily_check_runs_only_with_the_setting_on(box, settings, origin, clock):
    ticker = box.updates._ticker
    origin.serve(*signed(1))
    ticker.timer.fire()
    assert origin.calls == []

    settings.update_check.value = True
    ticker.timer.fire()
    assert len(origin.calls) == 2
    clock.now = NOW + 3600
    ticker.timer.fire()
    assert len(origin.calls) == 2
    clock.now = NOW + 24 * 3600 - 300
    ticker.timer.fire()
    assert len(origin.calls) == 4


def test_the_daily_check_waits_for_the_clock(box, settings, origin, clock):
    from MQTTBridge import updatecheck

    settings.update_check.value = True
    origin.serve(*signed(1))
    clock.now = updatecheck.CLOCK_FLOOR - 1
    box.updates._ticker.timer.fire()
    assert origin.calls == []


# --------------------------------------------------------------------- relayed index --


def relay(factory, payload, retain=True):
    factory.client.fire_message(RELEASE_INDEX, payload, retain=retain)


def test_the_plugin_subscribes_to_the_relayed_index(box, factory):
    assert (RELEASE_INDEX, 1) in factory.client.subscriptions


def test_a_relayed_index_is_accepted_without_the_setting_and_without_a_connection(
        box, factory, origin):
    relay(factory, relay_payload(*signed(2, four_releases())))
    state = update_state(factory)
    assert state["index"] == {"serial": 2, "issued": 1790500000, "source": "relay"}
    assert state["checked"] is None
    assert state["origin"] == "unknown"
    assert state["latest_compatible"] == "0.4.0"
    assert origin.calls == []
    assert factory.client.last(LAST_ERROR) is None
    assert box.updates.last_relay_verdict == "accept"


def test_a_relayed_index_is_kept_across_a_restart(box, factory, make_bridge, clock):
    relay(factory, relay_payload(*signed(2)))
    box.stop()
    again = make_bridge()
    again.updates.keys = TEST_KEYS
    again.updates.clock = clock
    again.start()
    factory.client.fire_connect()
    assert update_state(factory)["index"]["source"] == "relay"


@pytest.mark.parametrize("serial, verdict", [(1, "replay"), (5, "replay"), (1006, "jump"),
                                             (1005, "accept")])
def test_the_relayed_index_follows_the_serial_rule(box, factory, serial, verdict):
    relay(factory, relay_payload(*signed(5)))
    relay(factory, relay_payload(*signed(serial, [release("0.4.1")])))
    assert box.updates.last_relay_verdict == verdict
    expected = serial if verdict == "accept" else 5
    assert update_state(factory)["index"]["serial"] == expected


def test_a_relayed_index_of_a_lower_rank_is_refused_for_good(box, factory):
    relay(factory, relay_payload(*signed(1, key="t2")))
    relay(factory, relay_payload(*signed(2, key="t1")))
    assert box.updates.last_relay_verdict == "rank"
    relay(factory, relay_payload(*signed(3, key="t1")))
    assert box.updates.last_relay_verdict == "rank"
    assert update_state(factory)["index"]["serial"] == 1


def test_a_relayed_payload_over_the_cap_is_dropped_before_it_is_parsed(box, factory,
                                                                       monkeypatch):
    from MQTTBridge import updatecheck

    calls = []
    real = json.loads
    monkeypatch.setattr(updatecheck.json, "loads",
                        lambda *args, **kwargs: calls.append(1) or real(*args, **kwargs))
    relay(factory, b"{" + b" " * updatecheck.MAX_RELAY_PAYLOAD_BYTES + b"}")
    assert calls == []
    assert box.updates.last_relay_verdict == "too_large"


def test_a_relayed_64_kib_index_fits_under_the_payload_cap():
    from MQTTBridge import updatecheck

    index_raw = b"x" * trust.MAX_INDEX_BYTES
    signature_raw = b"y" * trust.MAX_SIGNATURE_BYTES
    assert len(relay_payload(index_raw, signature_raw)) <= updatecheck.MAX_RELAY_PAYLOAD_BYTES


def test_the_signature_is_judged_before_the_index_is_parsed(box, factory):
    _index, signature_raw = signed(1)
    relay(factory, relay_payload(b"this is not an index", signature_raw))
    assert box.updates.last_relay_verdict == "bad_signature"


def test_a_relayed_payload_that_is_not_the_shape_is_dropped(box, factory):
    relay(factory, b'{"index": "not base64!", "sig": ""}')
    assert box.updates.last_relay_verdict == "malformed_relay"
    relay(factory, b"[]")
    assert box.updates.last_relay_verdict == "malformed_relay"
    assert factory.client.last(LAST_ERROR) is None


def test_an_empty_retained_payload_is_ignored(box, factory):
    relay(factory, b"")
    assert box.updates.last_relay_verdict is None
    assert factory.client.last(LAST_ERROR) is None


def test_a_relayed_index_is_not_a_command(box, factory, plugin_log):
    relay(factory, relay_payload(*signed(1)))
    assert "unexpected topic" not in plugin_log()
    assert "RETAINED cmd" not in plugin_log()


def _scenarios():
    return [(scenario["name"], scenario["steps"])
            for scenario in updatelab.VECTORS["scenarios"]]


@pytest.mark.parametrize("name, steps", _scenarios(), ids=[name for name, _ in _scenarios()])
def test_the_relay_path_reaches_the_vectors_verdicts(tmp_path, name, steps):
    """Every scenario of the shared vectors, relayed step by step into one stored state."""
    import base64

    from MQTTBridge import updatecheck

    path = str(tmp_path / "mqttbridge-index.json")
    for number, step in enumerate(steps):
        checker = updatecheck.UpdateChecker(
            None, path=path, keys=updatelab.keyset(step["keyset"]),
            acceptance=step["lineage"] == "acceptance",
        )
        checker.start()
        payload = relay_payload(base64.b64decode(step["index"]), base64.b64decode(step["sig"]))
        checker.on_release_index(payload, True)
        assert checker.last_relay_verdict == step["expect"], (number, step["note"])
        checker.stop()


# --------------------------------------------------------------------- stored state --


def test_a_damaged_memory_judges_nothing_and_is_left_alone(box, factory, settings, origin,
                                                           clock):
    with open(box.updates.path, "w", encoding="utf-8") as handle:
        handle.write('{"schema": 1, "release": {"x": 1}, "acceptance": {}}')
    before = open(box.updates.path, encoding="utf-8").read()
    relay(factory, relay_payload(*signed(1)))
    assert box.updates.last_relay_verdict == "bad_memory"
    settings.update_check.value = True
    origin.serve(*signed(1))
    check(factory)
    assert update_state(factory)["check_error"] == "bad_memory"
    assert update_state(factory)["index"] is None
    assert open(box.updates.path, encoding="utf-8").read() == before


def test_a_held_index_that_no_longer_verifies_is_dropped_and_the_memory_kept(tmp_path):
    from MQTTBridge import updatecheck

    path = str(tmp_path / "mqttbridge-index.json")
    first = updatecheck.UpdateChecker(None, path=path, keys=TEST_KEYS, acceptance=False)
    first.start()
    first.on_release_index(relay_payload(*signed(7)), True)
    assert first.payload()["index"]["serial"] == 7

    other = updatecheck.UpdateChecker(None, path=path, keys=updatelab.keyset("other"),
                                      acceptance=False)
    other.start()
    assert other.payload()["index"] is None
    with open(path, encoding="utf-8") as handle:
        stored = json.load(handle)
    assert any(entry["serials"] for entry in stored["release"].values())

    back = updatecheck.UpdateChecker(None, path=path, keys=TEST_KEYS, acceptance=False)
    back.start()
    assert back.payload()["index"]["serial"] == 7


def test_an_acceptance_build_never_holds_what_a_release_build_accepted(tmp_path):
    from MQTTBridge import updatecheck

    path = str(tmp_path / "mqttbridge-index.json")
    release_build = updatecheck.UpdateChecker(None, path=path, keys=TEST_KEYS, acceptance=False)
    release_build.start()
    release_build.on_release_index(relay_payload(*signed(7)), True)
    acceptance_build = updatecheck.UpdateChecker(None, path=path, keys=TEST_KEYS,
                                                 acceptance=True)
    acceptance_build.start()
    assert acceptance_build.payload()["index"] is None
    acceptance_build.on_release_index(relay_payload(*signed(2)), True)
    assert acceptance_build.last_relay_verdict == "accept"


def _drop_held(path):
    with open(path, encoding="utf-8") as handle:
        stored = json.load(handle)
    del stored["held"]
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(stored, handle)


def test_an_index_lost_from_the_file_is_held_again_at_the_serial_remembered(tmp_path):
    from MQTTBridge import updatecheck

    path = str(tmp_path / "mqttbridge-index.json")
    first = updatecheck.UpdateChecker(None, path=path, keys=TEST_KEYS, acceptance=False)
    first.start()
    index_raw, signature_raw = signed(4)
    first.on_release_index(relay_payload(index_raw, signature_raw), True)
    _drop_held(path)

    again = updatecheck.UpdateChecker(None, path=path, keys=TEST_KEYS, acceptance=False)
    again.start()
    assert again.payload()["index"] is None
    again.on_release_index(relay_payload(*signed(3)), True)
    assert again.last_relay_verdict == "replay"
    assert again.payload()["index"] is None
    again.on_release_index(relay_payload(index_raw, signature_raw), True)
    assert again.last_relay_verdict == "taken_back"
    assert again.payload()["index"]["serial"] == 4

    kept = updatecheck.UpdateChecker(None, path=path, keys=TEST_KEYS, acceptance=False)
    kept.start()
    assert kept.payload()["index"]["serial"] == 4


def test_an_index_that_could_not_be_kept_is_not_shown(tmp_path, monkeypatch):
    from MQTTBridge import updatecheck

    def refuse(*_args):
        raise OSError("read-only file system")

    path = str(tmp_path / "mqttbridge-index.json")
    checker = updatecheck.UpdateChecker(None, path=path, keys=TEST_KEYS, acceptance=False)
    checker.start()
    monkeypatch.setattr(updatecheck.os, "replace", refuse)
    checker.on_release_index(relay_payload(*signed(1)), True)
    assert checker.last_relay_verdict == "write_failed"
    assert checker.payload()["index"] is None


def test_the_stored_state_keeps_what_it_does_not_know(tmp_path):
    from MQTTBridge import updatecheck

    path = str(tmp_path / "mqttbridge-index.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"schema": 3, "release": {}, "acceptance": {}, "later": {"kept": True}}, handle)
    checker = updatecheck.UpdateChecker(None, path=path, keys=TEST_KEYS, acceptance=False)
    checker.start()
    checker.on_release_index(relay_payload(*signed(1)), True)
    with open(path, encoding="utf-8") as handle:
        stored = json.load(handle)
    assert stored["later"] == {"kept": True}
    assert stored["schema"] == 3


# ------------------------------------------------------------------ what is offered --


def offer(releases, **kwargs):
    from MQTTBridge import updatecheck

    index = trust.parse_index(updatelab.index_bytes(1, releases, floor=kwargs.pop("floor",
                                                                                   "0.2.0")))
    return updatecheck.offer(index, **kwargs)


def test_the_twenty_newest_at_or_above_the_floor_that_are_not_withdrawn():
    releases = [release(f"0.{minor}.0") for minor in range(40, 1, -1)]
    releases[0]["withdrawn"] = "broke the channel list"
    latest, available = offer(releases, installed=None, integration_mode=False, contract=1)
    versions = [entry["version"] for entry in available]
    assert len(versions) == 20
    assert versions[0] == "0.39.0"
    assert versions == sorted(versions, key=trust.version_key, reverse=True)
    assert latest == "0.39.0"


def test_below_the_floor_is_never_offered():
    latest, available = offer([release("0.3.0"), release("0.2.0")], floor="0.3.0",
                              installed=None, integration_mode=False, contract=1)
    assert [entry["version"] for entry in available] == ["0.3.0"]


def test_another_contract_major_is_listed_as_incompatible():
    latest, available = offer([release("1.0.0", contract=2), release("0.4.0")],
                              installed=None, integration_mode=False, contract=1)
    assert available[0] == {"version": "1.0.0", "compatible": False, "reason": "incompatible"}
    assert available[1] == {"version": "0.4.0", "compatible": True, "reason": None}
    assert latest == "0.4.0"


def test_a_min_integration_is_unmet_in_integration_mode_and_met_otherwise():
    releases = [release("0.4.0", min_integration="0.4.0")]
    _latest, available = offer(releases, installed=None, integration_mode=True, contract=1)
    assert available[0]["reason"] == "incompatible"
    latest, available = offer(releases, installed=None, integration_mode=False, contract=1)
    assert available[0]["compatible"] is True
    assert latest == "0.4.0"


def test_a_missing_dependency_is_named_and_unknown_packages_are_not_judged():
    releases = [release("0.4.0", depends=("python3-core", "python3-json"))]
    latest, available = offer(releases, installed=frozenset({"python3-core"}),
                              integration_mode=False, contract=1)
    assert available[0] == {"version": "0.4.0", "compatible": False, "reason": "depends"}
    assert latest is None
    _latest, available = offer(releases, installed=None, integration_mode=False, contract=1)
    assert available[0]["compatible"] is True


def test_the_installed_packages_are_read_from_opkgs_status(tmp_path):
    from MQTTBridge import updatecheck

    database = tmp_path / "var" / "lib" / "opkg"
    database.mkdir(parents=True)
    (database / "status").write_text(
        "Package: python3-core\nVersion: 3.12.3\nStatus: install ok installed\n\n"
        "Package: python3-json\nStatus: deinstall ok not-installed\n\n"
        "Package: busybox\nProvides: sh, python3-fake (= 1.0)\nStatus: install user installed\n",
        encoding="utf-8",
    )
    assert updatecheck.installed_packages(str(tmp_path)) == frozenset(
        {"python3-core", "busybox", "sh", "python3-fake"}
    )
    assert updatecheck.installed_packages(str(tmp_path / "nothing")) is None


# --------------------------------------------------------------------- the topic --


def test_the_update_topic_is_published_on_connect_and_after_a_reset(box, factory):
    state = update_state(factory)
    assert state == {"origin": "unknown", "checked": None, "check_error": None, "index": None,
                     "latest_compatible": None, "available": [], "transaction": None}
    factory.client.clear()
    factory.client.fire_message("enigma2/" + NODE + "/cmd/reset", "PRESS")
    assert update_state(factory)["origin"] == "unknown"


# ------------------------------------------------------------------ the fetch itself --


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, amount=None):
        chunk, self._body = self._body[:amount], self._body[amount:]
        return chunk


class FakeConnection:
    made = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.requests = []
        FakeConnection.made.append(self)

    def request(self, method, path, headers=None):
        self.requests.append((method, path))

    def getresponse(self):
        return FakeResponse(*FakeConnection.answer)

    def close(self):
        pass


def test_the_fetch_verifies_even_when_another_plugin_switched_verification_off(monkeypatch):
    from MQTTBridge import updatecheck

    monkeypatch.setattr(ssl, "_create_default_https_context", ssl._create_unverified_context)
    FakeConnection.made = []
    FakeConnection.answer = (200, b"body")
    status, body = updatecheck.https_get(trust.ORIGIN + trust.SIGNATURE_FILE, 1024, 5,
                                         connection_factory=FakeConnection)
    assert (status, body) == (200, b"body")
    made = FakeConnection.made[0]
    assert made.context.verify_mode == ssl.CERT_REQUIRED
    assert made.context.check_hostname is True
    assert (made.host, made.port, made.timeout) == ("deltasystems-pl.github.io", 443, 5)
    assert made.requests == [("GET", "/enigma2-mqtt-bridge/feed/releases.json.sig")]


def test_the_fetch_never_follows_a_redirect_and_reads_at_most_one_byte_past_the_cap():
    from MQTTBridge import updatecheck

    FakeConnection.made = []
    FakeConnection.answer = (301, b"moved")
    assert updatecheck.https_get(trust.ORIGIN + "x", 10, 5,
                                 connection_factory=FakeConnection)[0] == 301
    assert len(FakeConnection.made) == 1
    FakeConnection.answer = (200, b"z" * 100)
    assert updatecheck.https_get(trust.ORIGIN + "x", 10, 5,
                                 connection_factory=FakeConnection)[1] == b"z" * 11


def test_the_fetch_refuses_anything_but_https():
    from MQTTBridge import updatecheck

    with pytest.raises(ValueError):
        updatecheck.https_get("http://example.invalid/releases.json", 10, 5,
                              connection_factory=FakeConnection)


def test_the_fetch_refuses_a_certificate_nobody_vouches_for(tmp_path):
    """Real TLS, on the loopback: a self-signed certificate is `unreachable`, never read."""
    import http.server
    import shutil
    import subprocess
    import threading

    from MQTTBridge import updatecheck

    if shutil.which("openssl") is None:
        pytest.skip("no openssl to make a certificate with")
    key, certificate = tmp_path / "key.pem", tmp_path / "cert.pem"
    made = subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=localhost", "-keyout", str(key), "-out", str(certificate)],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("openssl could not make a certificate")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"a signature")

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    try:
        with pytest.raises(updatecheck.Unreachable):
            updatecheck.https_get(
                f"https://localhost:{server.server_address[1]}/releases.json.sig", 1024, 5
            )
    finally:
        server.server_close()
        thread.join(timeout=5)
