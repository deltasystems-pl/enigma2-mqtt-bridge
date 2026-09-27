"""`cmd/update`: the guards in their order, the launch, the doors, the question and the marker.

The helper itself - the lock, the package, the proof, the rollback - is `test_updatehelper.py`'s.
This is the main loop's half: everything the running plugin decides and says before the helper
takes over, while it runs, and when the plugin that starts afterwards reads what it left.

The receiver's files live under a temporary root: opkg's records, `start-stop-daemon`, the
plugin's own directory with the real helper files in it, `/proc`'s boot id and uptime, and the
backups directory. `eConsoleAppContainer` runs nothing and remembers the command line, so the
helper never starts; a test writes the files the helper would write.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import (
    ConsoleAppContainer,
    MainLoop,
    MessageBox,
    Receiver,
    settle,
)
from Screens import Standby as standby_module
from updatelab import index_bytes, release

from MQTTBridge import epgimport, power, recording, selfupdate, trust, updatehelper, webif
from MQTTBridge.origin import MQTT, PAGE, SCREEN
from MQTTBridge.selfupdate import SelfUpdater
from MQTTBridge.uninstall import Uninstaller
from MQTTBridge.updatecheck import RELEASE_INDEX_TOPIC
from MQTTBridge.version import __version__

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
COMMAND = ROOT + "/cmd/update"
LAST_ERROR = ROOT + "/last_error"
UPDATE = ROOT + "/update"
AVAILABILITY = ROOT + "/availability"
INTEGRATION = "enigma2mqtt/integration/" + NODE
PACKAGE_DIR = Path(selfupdate.__file__).resolve().parent
NOW = 1790410000
COMMIT = "cd" * 20
BUILD = {"commit": COMMIT, "time": 1790400000, "dirty": False, "flavour": "development"}
BOOT = "3f1c2a50-0000-4000-8000-000000000001"
OTHER_BOOT = "3f1c2a50-0000-4000-8000-000000000002"
UPTIME = 1000.0
DOORS = "an update is being applied on the receiver"
BUSY = "an update is already running on the receiver"

assert __version__ == "0.3.0", "the scenarios below place releases around the running 0.3.0"


# ---------------------------------------------------------------------- helpers --


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def mono(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(SelfUpdater, "monotonic", staticmethod(clock))
    monkeypatch.setattr(SelfUpdater, "clock", staticmethod(lambda: NOW))
    return clock


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A receiver's files under a root of the test's own, the way opkg left them."""

    def build(packaged=True, daemon=True, python=True, helper=True, build_commit=COMMIT):
        root = tmp_path / "box"
        (root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
        (root / "sbin").mkdir(parents=True, exist_ok=True)
        opkg = root / "usr" / "bin" / "opkg"
        opkg.write_text("#!/bin/sh\n", encoding="utf-8")
        opkg.chmod(0o755)
        for program, present in ((selfupdate.START_STOP_DAEMON, daemon),
                                 (selfupdate.PYTHON, python)):
            path = root / program.lstrip("/")
            if present:
                path.write_text("#!/bin/sh\n", encoding="utf-8")
                path.chmod(0o755)
        conf = root / "etc" / "opkg"
        conf.mkdir(parents=True, exist_ok=True)
        (conf / "opkg.conf").write_text(
            "option info_dir /var/lib/opkg/info\noption status_file /var/lib/opkg/status\n",
            encoding="utf-8")
        info = root / "var" / "lib" / "opkg" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (root / "var" / "lib" / "opkg" / "status").write_text(
            "Package: python3-core\nVersion: 3.12\nStatus: install ok installed\n\n"
            f"Package: {updatehelper.PACKAGE}\nVersion: 0.3.0\nStatus: install ok installed\n",
            encoding="utf-8")
        (info / (updatehelper.PACKAGE + ".control")).write_text(
            "Package: " + updatehelper.PACKAGE + "\n", encoding="utf-8")
        plugin = root / "plugin" / "MQTTBridge"
        plugin.mkdir(parents=True, exist_ok=True)
        (plugin / "plugin.py").write_text("# the installed plugin\n", encoding="utf-8")
        if helper:
            for name in (selfupdate.HELPER_SOURCE,) + updatehelper.COPIED_MODULES:
                shutil.copyfile(PACKAGE_DIR / name, plugin / name)
        (plugin / "buildinfo.py").write_text(
            f'COMMIT = "{build_commit}"\nCOMMIT_TIME = 1790400000\nDIRTY = False\n'
            'FLAVOUR = "development"\n', encoding="utf-8")
        listed = str(plugin / ("plugin.py" if packaged else "bridge.py"))
        (info / (updatehelper.PACKAGE + ".list")).write_text(listed + "\t0100644\n",
                                                            encoding="utf-8")
        (root / "proc" / "sys" / "kernel" / "random").mkdir(parents=True, exist_ok=True)
        (root / "proc" / "sys" / "kernel" / "random" / "boot_id").write_text(BOOT + "\n")
        (root / "proc" / "uptime").write_text(f"{UPTIME:.2f} 1.00\n")
        (root / "home" / "root").mkdir(parents=True, exist_ok=True)
        (root / "etc" / "enigma2").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(SelfUpdater, "root", str(root))
        monkeypatch.setattr(SelfUpdater, "plugin_directory", str(plugin))
        return root

    return build


def hold(bridge, releases=None, floor="0.2.0"):
    """The signed index the receiver holds, as the update check would have kept it."""
    raw = index_bytes(1, releases if releases is not None else [
        release("0.4.0"), release("0.3.0"), release("0.2.5"), release("0.2.0")], floor=floor)
    bridge.updates._held = (trust.parse_index(raw), "relay")


@pytest.fixture
def box(make_bridge, factory, settings, receiver, tree, mono):
    def build(allowed=True, releases=None, floor="0.2.0", session=None, **tree_options):
        root = tree(**tree_options)
        settings.host.value = "10.0.0.5"
        settings.node_id.value = NODE
        settings.friendly_name.value = "Living room receiver"
        settings.update_allowed.value = allowed
        bridge = make_bridge(session=session or receiver.session, build=BUILD,
                             build_path=str(root / "plugin" / "MQTTBridge" / "buildinfo.py"))
        bridge.start()
        factory.client.fire_connect()
        hold(bridge, releases, floor)
        # The origin answered a probe a moment ago, so an install at the television or on the
        # page does not look again (`test_selfupdate_relay.py` takes this away where it matters).
        bridge.updates._origin = "reachable"
        bridge.updates._origin_seen = bridge.updates.monotonic()
        bridge.root = root
        return settle(bridge)

    return build


def send(factory, payload, retain=False):
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    factory.client.fire_message(COMMAND, data, retain=retain)


def refusal(client):
    entry = client.last(LAST_ERROR)
    if entry is None or entry.text == "":
        return None
    payload = entry.json()
    return payload.get("reason"), payload["error"]


def transaction(client):
    entry = client.last(UPDATE)
    return entry.json()["transaction"] if entry is not None else None


def directories(root):
    backups = root / "home" / "root" / "mqttbridge-backups"
    if not backups.is_dir():
        return []
    return sorted(p for p in backups.iterdir() if p.name.startswith("update-"))


def accepted(bridge, factory, payload=None):
    """Send an accepted `cmd/update`; the transaction directory it made."""
    send(factory, payload or {"version": "0.4.0"})
    assert refusal(factory.client) is None, refusal(factory.client)
    made = directories(bridge.root)
    assert len(made) == 1
    return made[0]


def helper_says(directory, **status):
    record = {"id": directory.name[len("update-"):], "started_by": "mqtt", "target": "0.4.0",
              "from": "0.3.0", "phase": "downloading", "started": NOW, "finished": None,
              "result": None, "reason": None, "error": None}
    record.update(status)
    updatehelper.write_json(str(directory / "status.json"), record)
    return record


def tick(count=1):
    for _ in range(count):
        MainLoop.advance(selfupdate.POLL_MILLISECONDS)


def lock(root, ident, boot=BOOT, uptime=UPTIME - 10, origin="mqtt", pid=4242, alive=True):
    """The shared lock, held by `ident`'s helper - running unless `alive` is False.

    `origin` None is the SSH installer's record, which has none.
    """
    backups = root / "home" / "root" / "mqttbridge-backups"
    directory = backups / updatehelper.LOCK_NAME
    directory.mkdir(parents=True, exist_ok=True)
    record = {"pid": pid, "started": NOW, "boot_id": boot, "uptime": uptime, "origin": origin,
              "id": ident, "target": "0.4.0"}
    if origin is None:
        del record["origin"]
    updatehelper.write_json(str(directory / "owner.json"), record)
    proc = root / "proc" / str(pid)
    if alive:
        proc.mkdir(parents=True, exist_ok=True)
        helper = backups / ("update-" + ident) / "helper.py"
        (proc / "cmdline").write_bytes(b"/usr/bin/python3\0" + str(helper).encode() + b"\0"
                                       + ident.encode() + b"\0")
    else:
        shutil.rmtree(proc, ignore_errors=True)
    return directory


# ------------------------------------------------------------ the permission --


def test_update_allowed_is_off_read_only_and_refused_by_cmd_config(box, factory, settings):
    bridge = box(allowed=False)
    assert settings.update_allowed.default is False
    assert bridge.build_info()["settings"]["update_allowed"] is False
    factory.client.fire_message(ROOT + "/cmd/config", json.dumps({
        "publish_keys": True, "screenshot": "on_zap", "screenshot_interval": 60,
        "update_allowed": True}).encode())
    assert refusal(factory.client)[1] == "the config object contains unknown settings"
    assert settings.update_allowed.value is False


def test_update_allowed_is_on_the_setup_screen_the_page_and_in_provisioning():
    from MQTTBridge import config as settings_module
    from MQTTBridge import setup as setup_screen

    assert "update_allowed" in settings_module.SETTING_NAMES
    assert settings_module.SETTING_KINDS["update_allowed"] == "bool"
    assert "update_allowed" in dict(setup_screen.setting_labels())
    assert "update_allowed" in dict(webif.SETTING_GROUPS)["permissions"]
    assert "update_allowed" not in settings_module.REMOTE_SETTING_NAMES


@pytest.mark.parametrize("origin", [PAGE, SCREEN])
def test_the_page_and_the_screen_need_no_permission(origin, box, factory):
    bridge = box(allowed=False)
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"}), origin=MQTT) \
        .reason == "not_permitted"
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"}), origin=origin) is None
    request = json.loads((directories(bridge.root)[0] / "request.json").read_text())
    assert request["started_by"] == origin


# ------------------------------------------------------------ the capability --


@pytest.mark.parametrize("missing", ["packaged", "daemon", "python", "helper"])
def test_the_capability_needs_every_piece(box, missing):
    bridge = box(**{missing: False})
    assert "self_update" not in bridge.capabilities()
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})).reason == "no_capability"


def test_the_capability_is_claimed_and_announced(box, factory):
    bridge = box()
    assert "self_update" in bridge.capabilities()
    assert "self_update" in factory.client.last(ROOT + "/info").json()["capabilities"]


# -------------------------------------------------------- the refusal table --

ROWS = [
    "not_permitted", "no_capability", "busy", "opkg_busy", "standby", "recording",
    "epg_import", "cannot_restart", "bad_request", "unknown_version", "withdrawn", "below_floor",
    "incompatible", "depends", "downgrade", "current", "checksum", "relay", "no_space",
    "rate_limited",
]
VERSION_ROWS = ("withdrawn", "below_floor", "incompatible", "depends", "downgrade")


def arrange(first, box, receiver, monkeypatch):
    """A receiver on which the guard `first` and every guard after it would refuse."""
    active = set(ROWS[ROWS.index(first):])
    target = "0.2.5" if first in VERSION_ROWS else "0.3.0" if first == "current" else "0.4.0"
    changes = {}
    if "withdrawn" in active:
        changes["withdrawn"] = "a broken build"
    if "incompatible" in active:
        changes["contract"] = 2
    changes["depends"] = ("nope",) if "depends" in active else ("python3-core",)
    releases = [release(target, **changes)] + [release(v) for v in ("0.4.0", "0.3.0", "0.2.0")
                                               if v != target]
    floor = "0.2.9" if "below_floor" in active and target == "0.2.5" else "0.2.0"
    bridge = box(allowed="not_permitted" not in active, releases=releases, floor=floor)
    if "no_capability" in active:
        bridge.self_update.claimed = False
    if "busy" in active:
        lock(bridge.root, "0123456789ab")
    if "opkg_busy" in active:
        monkeypatch.setattr(updatehelper, "opkg_busy", lambda receiver: True)
    if "standby" in active:
        receiver.enter_standby()
    if "recording" in active:
        monkeypatch.setattr(recording, "is_recording", lambda session: True)
    if "epg_import" in active:
        monkeypatch.setattr(epgimport, "running", lambda: True)
    if "cannot_restart" in active:
        monkeypatch.setattr(power, "can_quit", lambda session: "this image has no TryQuitMainloop")
    payload = {"version": "0.9.0" if first == "unknown_version" else target}
    if "checksum" in active:
        payload["sha256"] = "00" * 32
    if "relay" in active:
        payload["relay"] = {"url": "http://192.0.2.5:8123/elsewhere", "expires": NOW + 60}
    if "no_space" in active:
        monkeypatch.setattr(updatehelper.Receiver, "free_bytes", lambda self, path: 0)
    if "rate_limited" in active:
        updatehelper.write_json(str(bridge.root / updatehelper.LAST),
                                {"id": "0123456789ab", "finished": NOW - 60, "result": "failed"})
    if "bad_request" in active:
        payload = "please update"
    return bridge, payload


@pytest.mark.parametrize("first", ROWS)
def test_the_first_guard_that_holds_is_the_refusal(first, box, receiver, factory, monkeypatch):
    bridge, payload = arrange(first, box, receiver, monkeypatch)
    before = len(factory.client.published)
    send(factory, payload if isinstance(payload, dict) else payload.encode())
    assert refusal(factory.client)[0] == first
    # Before anything changes: nothing but `last_error`, and no transaction directory.
    assert {entry.topic for entry in factory.client.published[before:]} == {LAST_ERROR}
    assert directories(bridge.root) == []
    assert ConsoleAppContainer.instances == []


def test_with_no_guard_holding_the_update_is_accepted(box, factory):
    bridge = box()
    accepted(bridge, factory)
    assert transaction(factory.client)["phase"] == "downloading"


@pytest.mark.parametrize("first, sentence", [
    ("not_permitted", "updates over MQTT are switched off in the plugin's settings"),
    ("no_capability",
     "this plugin was not installed by the package manager, so it cannot update itself"),
    ("busy", "an update is already running on the receiver"),
    ("opkg_busy", "the receiver's package manager is busy"),
    ("standby", "the receiver is in standby; an update restarts the interface, which wakes the "
                "receiver and may switch the television on"),
    ("recording", "the receiver is recording"),
    ("epg_import", "an EPG import is running"),
    ("cannot_restart", "this image has no TryQuitMainloop"),
    ("unknown_version", "version 0.9.0 is not in the plugin's signed release index"),
    ("withdrawn", "version 0.2.5 has been withdrawn: a broken build"),
    ("below_floor", "version 0.2.5 is below the lowest version this plugin can install"),
    ("incompatible", "version 0.2.5 is not compatible with this plugin's contract"),
    ("depends", "version 0.2.5 needs nope, which is not installed on this receiver"),
    ("downgrade", "a downgrade can only be started on the receiver or from Home Assistant's "
                  "options"),
    ("current", "version 0.3.0 is already installed and running"),
    ("checksum", "the requested checksum does not match the signed release index"),
    ("relay", "the download address from Home Assistant is not valid"),
    ("no_space", "there is not enough free space on the receiver"),
    ("rate_limited", "an update ran less than ten minutes ago"),
])
def test_each_refusal_says_the_contracts_sentence(first, sentence, box, receiver, factory,
                                                  monkeypatch):
    _bridge, payload = arrange(first, box, receiver, monkeypatch)
    send(factory, payload)
    assert refusal(factory.client) == (first, sentence)


@pytest.mark.parametrize("state, reason", [
    (None, "recording_unknown"), (True, "recording"), (False, "recording_due")])
def test_the_recording_guard_says_which_of_its_refusals(state, reason, box, factory,
                                                        monkeypatch):
    box()
    monkeypatch.setattr(recording, "is_recording", lambda session: state)
    monkeypatch.setattr(recording, "next_recording_time", lambda session: int(
        recording.time.time()) + 300)
    send(factory, {"version": "0.4.0"})
    assert refusal(factory.client)[0] == reason


def test_a_downgrade_over_mqtt_is_refused_even_when_the_call_says_downgrade(box):
    bridge = box()
    text = json.dumps({"version": "0.2.5"})
    assert bridge.self_update.request(text, origin=MQTT, downgrade=True).reason == "downgrade"
    assert bridge.self_update.request(text, origin=SCREEN).reason == "downgrade"
    assert bridge.self_update.request(text, origin=SCREEN, downgrade=True) is None


def test_a_repair_of_the_running_release_is_allowed_when_the_disk_holds_another_build(box):
    bridge = box(build_commit="ef" * 20)
    assert bridge.self_update.request(json.dumps({"version": "0.3.0"})) is None


def test_latest_is_the_newest_compatible_release(box, factory):
    bridge = box(releases=[release("0.5.0", contract=2), release("0.4.0"), release("0.3.0")])
    directory = accepted(bridge, factory, {"version": "latest"})
    assert json.loads((directory / "request.json").read_text())["target"] == "0.4.0"


def test_a_relay_address_must_be_home_assistants_and_unexpired(box):
    bridge = box()
    token = "A" * 43
    good = {"url": "http://192.0.2.5:8123/api/enigma2_mqtt/relay/" + token, "expires": NOW + 60}
    for bad in (dict(good, expires=NOW), dict(good, expires=True),
                dict(good, url=good["url"] + "x"), dict(good, url="ftp://h/api/enigma2_mqtt/"
                                                                   "relay/" + token),
                "not an object"):
        answer = bridge.self_update.request(json.dumps({"version": "0.4.0", "relay": bad}))
        assert answer.reason == "relay", bad
    assert bridge.self_update.request(json.dumps({"version": "0.4.0", "relay": good})) is None


def test_the_rate_limit_ends_ten_minutes_after_the_last_update(box):
    bridge = box()
    updatehelper.write_json(str(bridge.root / updatehelper.LAST),
                            {"id": "0123456789ab", "finished": NOW - 601})
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})) is None


def test_a_stale_lock_is_not_busy(box):
    bridge = box()
    lock(bridge.root, "0123456789ab", boot=OTHER_BOOT)
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})) is None


def test_a_second_update_while_one_runs_is_busy(box, factory):
    bridge = box()
    accepted(bridge, factory)
    send(factory, {"version": "0.4.0"})
    assert refusal(factory.client)[0] == "busy"


def test_a_retained_update_is_discarded(box, factory):
    bridge = box()
    send(factory, {"version": "0.4.0"}, retain=True)
    assert directories(bridge.root) == []


# ------------------------------------------------------------------ the launch --


def test_the_request_the_copies_and_the_command_line(box, factory):
    bridge = box()
    relay = {"url": "http://192.0.2.5:8123/api/enigma2_mqtt/relay/" + "b" * 43,
             "expires": NOW + 60}
    directory = accepted(bridge, factory, {"version": "0.4.0", "sha256": "ab" * 32,
                                           "relay": relay})
    ident = directory.name[len("update-"):]
    assert updatehelper.TRANSACTION_ID.fullmatch(ident)
    assert directory.stat().st_mode & 0o777 == 0o700
    request = directory / "request.json"
    assert request.stat().st_mode & 0o777 == 0o600
    data = json.loads(request.read_text())
    assert data == {
        "id": ident, "target": "0.4.0", "sha256": "ab" * 32, "relay": relay,
        "started_by": "home_assistant", "downgrade": False,
        "from": {"version": "0.3.0", "commit": COMMIT}, "enigma2_pid": os.getpid(),
        "keys": trust.keys_to_data(bridge.updates.keys), "acceptance": bridge.updates.acceptance,
        "origin": bridge.updates.origin, "contract": 1, "integration": None,
        "integration_mode": False,
    }
    plugin = bridge.root / "plugin" / "MQTTBridge"
    assert (directory / "helper.py").read_bytes() == (plugin / "updatehelper.py").read_bytes()
    for name in updatehelper.COPIED_MODULES:
        assert (directory / name).read_bytes() == (plugin / name).read_bytes()
    assert ConsoleAppContainer.instances[-1].commands == [
        f"/sbin/start-stop-daemon -S -b -m -p {directory}/helper.pid -x /usr/bin/python3 -- "
        f"{directory}/helper.py {ident}"]
    # The helper the request is written for reads it.
    transaction = updatehelper.Transaction(str(directory))
    transaction.load_request()
    assert transaction.request["target"] == "0.4.0"


def test_without_a_relay_it_is_an_mqtt_update(box, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    assert json.loads((directory / "request.json").read_text())["started_by"] == "mqtt"


def test_a_launch_that_exits_non_zero_before_the_helper_wrote_anything_fails(box, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    ConsoleAppContainer.instances[-1].finish(1)
    assert not directory.exists()
    assert refusal(factory.client)[0] == "internal_error"
    assert transaction(factory.client)["result"] == "failed"
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})) is None


def test_a_rejected_launch_is_refused_and_leaves_nothing(box, factory, monkeypatch):
    bridge = box()
    monkeypatch.setattr(ConsoleAppContainer, "execute", lambda self, command: 1)
    send(factory, {"version": "0.4.0"})
    assert refusal(factory.client)[0] == "internal_error"
    assert directories(bridge.root) == []


def test_a_helper_that_writes_nothing_within_a_minute_is_given_up(box, factory, mono):
    bridge = box()
    directory = accepted(bridge, factory)
    mono.now = selfupdate.LAUNCH_WAIT_SECONDS - 1
    tick()
    assert directory.exists()
    mono.now = selfupdate.LAUNCH_WAIT_SECONDS + 1
    tick()
    assert not directory.exists()
    assert refusal(factory.client)[0] == "internal_error"


# --------------------------------------------------------- following the helper --


def test_the_helpers_phases_reach_the_update_topic(box, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    for phase in ("verifying", "snapshot", "installing"):
        helper_says(directory, phase=phase)
        tick()
        assert transaction(factory.client)["phase"] == phase
        # Review S1: the doors close when the package manager starts, not after it.
        assert bridge.self_update.closed is (phase == "installing")


def test_a_failure_before_the_restart_is_said_without_a_reload(box, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    clients = len(factory.clients)
    helper_says(directory, phase="finished", result="failed", reason="download",
                error="version 0.4.0 could not be downloaded: timed out", finished=NOW + 5)
    tick()
    assert refusal(factory.client) == ("download",
                                       "version 0.4.0 could not be downloaded: timed out")
    assert transaction(factory.client)["result"] == "failed"
    assert len(factory.clients) == clients
    # The transaction is over: the next one is not refused as busy.
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})) is None


def test_the_end_waits_for_the_lock_to_be_let_go(box, factory, mono):
    bridge = box()
    directory = accepted(bridge, factory)
    ident = directory.name[len("update-"):]
    lock(bridge.root, ident)
    helper_says(directory, phase="finished", result="failed", reason="download", error="x")
    tick()
    assert bridge.self_update._current is not None
    mono.now = selfupdate.RELEASE_WAIT_SECONDS + 1
    tick()
    assert bridge.self_update._current is None


def helper_process(bridge, directory, running=True, pid=4321, command=b"/usr/bin/python3\0"):
    """`start-stop-daemon -m`'s pid file, and the process it names in the receiver's /proc."""
    (directory / "helper.pid").write_text(f"{pid}\n")
    proc = bridge.root / "proc" / str(pid)
    if running:
        proc.mkdir(parents=True, exist_ok=True)
        (proc / "cmdline").write_bytes(command + str(directory / "helper.py").encode() + b"\0")
    else:
        shutil.rmtree(proc, ignore_errors=True)


def test_a_running_helper_is_followed(box, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase="installing")
    tick(3)
    assert bridge.self_update._current is not None
    assert transaction(factory.client)["phase"] == "installing"


def test_a_helper_that_died_before_the_restart_is_reported_interrupted(box, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    helper_says(directory, phase="snapshot")
    helper_process(bridge, directory, running=False)
    tick()
    assert refusal(factory.client)[0] == "interrupted"
    ended = transaction(factory.client)
    assert (ended["phase"], ended["result"]) == ("finished", "interrupted")
    assert "may not be" not in ended["error"]
    assert bridge.self_update._current is None


def test_a_pid_used_again_by_another_program_is_not_the_helper(box, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    helper_says(directory, phase="snapshot")
    (directory / "helper.pid").write_text("4321\n")
    other = bridge.root / "proc" / "4321"
    other.mkdir(parents=True)
    (other / "cmdline").write_bytes(b"/usr/sbin/dropbear\0-R\0")
    tick()
    assert refusal(factory.client)[0] == "interrupted"


def test_with_no_pid_file_there_is_no_verdict(box, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    helper_says(directory, phase="snapshot")
    tick(5)
    assert bridge.self_update._current is not None


def test_a_launch_whose_helper_died_before_writing_fails_at_once(box, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory, running=False)
    tick()
    assert not directory.exists()
    assert refusal(factory.client)[0] == "internal_error"


@pytest.mark.skipif(shutil.which("busybox") is None, reason="busybox is not installed")
def test_busybox_starts_the_helper_while_another_python3_runs(tmp_path):
    """Review H1: `-x /usr/bin/python3` alone matches any running python3 and starts nothing."""
    import subprocess
    import time

    directory = tmp_path / "update-a1b2c3d4e5f6"
    directory.mkdir()
    out = tmp_path / "helper.out"
    (directory / "helper.py").write_text(
        "import os, sys\n"
        f"open({str(out)!r}, 'w').write('%d %d %d' % (os.getpid(), os.getsid(0), os.getpgid(0)))\n")
    other = subprocess.Popen(["/usr/bin/python3", "-c", "import time; time.sleep(20)"])
    try:
        line = SelfUpdater.command_line(str(directory), "a1b2c3d4e5f6").replace(
            selfupdate.START_STOP_DAEMON, "busybox start-stop-daemon", 1)
        # eConsoleAppContainer runs a command line through /bin/sh -c.
        assert subprocess.run(["/bin/sh", "-c", line], timeout=10).returncode == 0
        deadline = time.time() + 10
        while not out.exists() and time.time() < deadline:
            time.sleep(0.05)
        pid, sid, pgid = (int(value) for value in out.read_text().split())
        # A session of its own - busybox forks twice, so the helper is not its leader - and
        # not the launcher's: the interface's restart does not take it along.
        assert sid == pgid != os.getsid(0)
        assert int((directory / "helper.pid").read_text()) == pid
    finally:
        other.kill()
        other.wait()


def test_the_rate_limit_counts_uptime_within_one_boot(box, monkeypatch):
    bridge = box()
    # A receiver that booted in 1970: the wall clock says nothing useful.
    monkeypatch.setattr(SelfUpdater, "clock", staticmethod(lambda: 200))
    last = str(bridge.root / updatehelper.LAST)
    updatehelper.write_json(last, {"id": "0123456789ab", "finished": 100, "boot_id": BOOT,
                                   "uptime": UPTIME - 60})
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})).reason == "rate_limited"
    updatehelper.write_json(last, {"id": "0123456789ab", "finished": 150, "boot_id": BOOT,
                                   "uptime": UPTIME - 601})
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})) is None


def test_across_a_boot_the_wall_clock_is_not_trusted(box):
    bridge = box()
    last = str(bridge.root / updatehelper.LAST)
    updatehelper.write_json(last, {"id": "0123456789ab", "finished": NOW + 3600,
                                   "boot_id": OTHER_BOOT, "uptime": UPTIME - 60})
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})) is None


# ----------------------------------------------------------------- the doors --


def restarting(bridge, factory, payload=None, downgrade=False):
    if downgrade:
        assert bridge.self_update.request(json.dumps(payload), origin=SCREEN,
                                          downgrade=True) is None
        directory = directories(bridge.root)[0]
    else:
        directory = accepted(bridge, factory, payload)
    helper_says(directory, phase="restarting")
    tick()
    return directory


def test_from_restarting_the_doors_close_and_the_image_is_asked_to_restart(box, factory,
                                                                          receiver):
    bridge = box()
    assert bridge.publisher("power") is not None
    directory = restarting(bridge, factory)
    assert bridge.self_update.closed and not bridge.self_update.silent
    assert bridge.publisher("power") is None
    assert json.loads((directory / "restart.json").read_text()) == {"pid": os.getpid()}
    callback, screen, arguments, kwargs = receiver.session.callbacks[-1]
    assert screen is standby_module.TryQuitMainloop
    assert arguments == (3,)
    assert kwargs == {"timeout": 60, "default_yes": False}


def test_behind_closed_doors_every_command_gets_the_sentence(box, factory):
    bridge = box()
    restarting(bridge, factory)
    factory.client.fire_message(ROOT + "/cmd/power", b"standby")
    assert refusal(factory.client) == (None, DOORS)
    assert bridge.run_command("restart_gui", "PRESS", PAGE) == DOORS
    send(factory, {"version": "0.4.0"})
    assert refusal(factory.client)[1] == DOORS
    assert bridge.apply_settings({"log_level": "debug"}) == DOORS


def test_behind_closed_doors_the_page_and_the_setup_screen_say_only_that(box, factory,
                                                                         monkeypatch, settings):
    from test_setup_screen import FakeSession
    from test_webif import _Request

    from MQTTBridge import setup as setup_screen

    bridge = box()
    restarting(bridge, factory)
    monkeypatch.setattr(webif, "_bridge", lambda: bridge)
    body = webif._page(_Request()).decode("utf-8")
    assert selfupdate.household_doors() in body
    assert "<form" not in body
    clients = len(factory.clients)
    screen = setup_screen.MQTTBridgeSetup(FakeSession(), settings=settings, bridge=bridge)
    screen.keySave()
    assert len(factory.clients) == clients
    assert screen.session.opened == [MessageBox]


def test_a_withdrawn_update_reopens_the_doors_with_a_fresh_session(box, factory, receiver):
    bridge = box()
    directory = restarting(bridge, factory)
    callback = receiver.session.callbacks[-1][0]
    # The image's TryQuitMainloop answers True even for "no": any call back is "no restart".
    callback(True)
    assert json.loads((directory / "withdraw").read_text()) == {"reason": "question"}
    clients = len(factory.clients)
    helper_says(directory, phase="finished", result="withdrawn_before_restart",
                reason="question", error=updatehelper.SENTENCES["question"], finished=NOW + 70)
    tick()
    assert not bridge.self_update.closed
    assert len(factory.clients) == clients + 1
    factory.client.fire_connect()
    assert refusal(factory.client)[0] == "question"
    assert transaction(factory.client)["result"] == "withdrawn_before_restart"
    assert bridge.publisher("power") is not None


# ------------------------------------------------- the downgrade and S-a --


def test_a_downgrade_from_the_television_retracts_all_but_availability_first(box, factory,
                                                                             receiver):
    bridge = box()
    retained = set(bridge.state.retained_topics)
    assert UPDATE in retained and AVAILABILITY in retained
    before = len(factory.client.published)
    directory = restarting(bridge, factory, {"version": "0.2.5"}, downgrade=True)
    assert json.loads((directory / "request.json").read_text())["downgrade"] is True
    assert json.loads((directory / "request.json").read_text())["started_by"] == "screen"
    out = factory.client.published[before:]
    retracted = {entry.topic for entry in out if entry.qos == 1 and entry.retain
                 and entry.payload in (b"", "")}
    assert retracted == retained - {AVAILABILITY}
    # Not yet asked: the acknowledgements come first.
    assert not (directory / "restart.json").exists()
    assert receiver.session.callbacks == []
    factory.client.acknowledge()
    MainLoop.advance(selfupdate.RETRACTION_POLL_MILLISECONDS)
    assert (directory / "restart.json").exists()
    assert receiver.session.callbacks[-1][1] is standby_module.TryQuitMainloop


def test_after_the_retraction_nothing_is_published_until_the_reload(box, factory, receiver):
    bridge = box()
    directory = restarting(bridge, factory, {"version": "0.2.5"}, downgrade=True)
    factory.client.acknowledge()
    MainLoop.advance(selfupdate.RETRACTION_POLL_MILLISECONDS)
    assert bridge.self_update.silent
    quiet = len(factory.client.published)
    factory.client.fire_message(ROOT + "/cmd/power", b"standby")
    helper_says(directory, phase="restarting", restart="requested")
    tick(3)
    bridge.check_build_on_disk()
    bridge.updates.publish()
    factory.client.fire_connect()
    assert factory.client.published[quiet:] == []
    helper_says(directory, phase="finished", result="withdrawn_before_restart",
                reason="question", error=updatehelper.SENTENCES["question"], finished=NOW + 70)
    tick()
    factory.client.fire_connect()
    assert factory.client.last(ROOT + "/info") is not None
    assert factory.client.last(UPDATE).json()["transaction"]["result"] == \
        "withdrawn_before_restart"


def test_after_the_retraction_the_shutdown_publishes_no_offline(box, factory):
    bridge = box()
    restarting(bridge, factory, {"version": "0.2.5"}, downgrade=True)
    factory.client.acknowledge()
    MainLoop.advance(selfupdate.RETRACTION_POLL_MILLISECONDS)
    quiet = len(factory.client.published)
    bridge.stop()
    assert factory.client.published[quiet:] == []


def test_an_unacknowledged_retraction_withdraws_the_downgrade(box, factory, mono, receiver):
    bridge = box()
    directory = restarting(bridge, factory, {"version": "0.2.5"}, downgrade=True)
    mono.now = selfupdate.RETRACTION_TIMEOUT_SECONDS + 1
    MainLoop.advance(selfupdate.RETRACTION_POLL_MILLISECONDS)
    assert json.loads((directory / "withdraw").read_text()) == {"reason": "retraction"}
    assert not (directory / "restart.json").exists()
    assert receiver.session.callbacks == []


def test_an_upgrade_retracts_nothing(box, factory):
    bridge = box()
    before = len(factory.client.published)
    restarting(bridge, factory)
    assert [e for e in factory.client.published[before:] if e.qos == 1] == []
    assert not bridge.self_update.silent


# ----------------------------------------------------- the session-start screen --


class ImageQuestion(MessageBox):
    """`Screens.Standby.TryQuitMainloop` when something holds: a question whose "no" closes True.

    As the image has it: `close(False)` ends in `MessageBox.close(self, True)`, and a quit
    never calls back at all.
    """

    def __init__(self, session, retvalue=1, timeout=-1, default_yes=False, check_reset=False):
        MessageBox.__init__(self, session, "Restart?", timeout=timeout, default=default_yes)
        self.retval = retvalue

    def close(self, value):
        MessageBox.close(self, True)


@pytest.mark.parametrize("start_screen", [False, True])
def test_the_question_works_with_and_without_a_session_start_screen(start_screen, box, factory,
                                                                   monkeypatch):
    receiver = Receiver(modal=True, session_start_screen=start_screen)
    receiver.with_channel_list()
    monkeypatch.setattr(standby_module, "TryQuitMainloop", ImageQuestion)
    bridge = box(session=receiver.session)
    directory = restarting(bridge, factory)
    question = receiver.session.current_dialog
    assert isinstance(question, ImageQuestion) and question.timeout == 60
    question.close(False)
    MainLoop.advance(0)
    assert json.loads((directory / "withdraw").read_text()) == {"reason": "question"}


def test_no_code_on_the_update_path_reads_the_dialog_stack(box, factory, monkeypatch):
    receiver = Receiver(modal=True, session_start_screen=True)
    receiver.with_channel_list()
    monkeypatch.setattr(standby_module, "TryQuitMainloop", ImageQuestion)
    session = receiver.session
    reads = []
    watched = ("dialog_stack", "current_dialog")

    class Watched(type(session)):
        def __getattribute__(self, name):
            if name in watched:
                frame = sys._getframe(1)
                if os.path.samefile(frame.f_code.co_filename, selfupdate.__file__):
                    reads.append(name)
            return object.__getattribute__(self, name)

    session.__class__ = Watched
    bridge = box(session=session)
    restarting(bridge, factory)
    session.current_dialog.close(False)
    MainLoop.advance(0)
    assert reads == []


# ------------------------------------------------------------------ the marker --


def marker(root, ident="a1b2c3d4e5f6", phase="proving", boot=BOOT, deadline=UPTIME + 600,
           to=("0.3.0", COMMIT), frm=("0.2.0", "ef" * 20), **extra):
    value = {"id": ident, "from": {"version": frm[0], "commit": frm[1]},
             "to": {"version": to[0], "commit": to[1]}, "boot_id": boot, "deadline": deadline,
             "phase": phase, "started_by": "home_assistant", "started": NOW - 100}
    value.update(extra)
    updatehelper.write_json(str(root / updatehelper.MARKER), value)
    directory = root / "home" / "root" / "mqttbridge-backups" / ("update-" + ident)
    directory.mkdir(parents=True, exist_ok=True)
    updatehelper.write_json(str(directory / "request.json"), {"id": ident, "enigma2_pid": 1})
    return directory


@pytest.fixture
def starting(make_bridge, factory, settings, tree, mono):
    """A plugin that starts on a receiver the test prepared first."""

    def build(enabled=True, prepare=None, session=None):
        root = tree()
        if prepare is not None:
            prepare(root)
        settings.host.value = "10.0.0.5"
        settings.node_id.value = NODE
        settings.enabled.value = enabled
        # With the receiver's session only where a test needs its publishers or its guards.
        options = {} if session is None else {"session": session}
        bridge = make_bridge(build=BUILD, build_path=str(
            root / "plugin" / "MQTTBridge" / "buildinfo.py"), **options)
        bridge.root = root
        bridge.start()
        return bridge

    return build


def test_a_switched_off_new_release_still_confirms_that_it_started(starting):
    made = {}
    bridge = starting(enabled=False, prepare=lambda root: made.update(dir=marker(root)))
    assert bridge.idle_reason
    started = json.loads((made["dir"] / "started.json").read_text())
    assert started == {"version": "0.3.0", "commit": COMMIT, "pid": os.getpid()}


def test_the_marker_is_read_before_the_provisioning_file(starting, monkeypatch):
    from MQTTBridge import config as settings_module

    order = []
    real_import = settings_module.import_provisioning
    real_start = SelfUpdater.on_start
    monkeypatch.setattr(settings_module, "import_provisioning",
                        lambda *a, **k: (order.append("provisioning"), real_import(*a, **k))[1])
    monkeypatch.setattr(SelfUpdater, "on_start",
                        lambda self: (order.append("marker"), real_start(self))[1])
    starting()
    assert order[:2] == ["marker", "provisioning"]


def test_the_process_that_asked_never_confirms_for_the_new_one(starting):
    made = {}

    def prepare(root):
        made["dir"] = marker(root, phase="restarting")
        updatehelper.write_json(str(made["dir"] / "request.json"),
                                {"id": "a1b2c3d4e5f6", "enigma2_pid": os.getpid()})

    starting(prepare=prepare)
    assert not (made["dir"] / "started.json").exists()


@pytest.mark.parametrize("case", ["another_boot_no_lock", "past_deadline", "no_id"])
def test_a_stale_marker_is_discarded(case, starting):
    made = {}

    def prepare(root):
        if case == "another_boot_no_lock":
            made["dir"] = marker(root, boot=OTHER_BOOT)
        elif case == "past_deadline":
            made["dir"] = marker(root, deadline=UPTIME - 1)
        else:
            made["dir"] = marker(root)
            updatehelper.write_json(str(root / updatehelper.MARKER), {"phase": "proving"})

    bridge = starting(prepare=prepare)
    assert not (bridge.root / updatehelper.MARKER).exists()
    assert not (made["dir"] / "started.json").exists()
    assert bridge.self_update.transaction_payload() is None


@pytest.mark.parametrize("to, frm, result", [
    (("0.3.0", COMMIT), ("0.2.0", "ef" * 20), "installed"),
    (("0.4.0", "ab" * 20), ("0.3.0", COMMIT), "rolled_back"),
    (("0.4.0", "ab" * 20), ("0.2.0", "ef" * 20), "interrupted"),
])
def test_after_a_power_loss_the_running_version_says_how_it_ended(to, frm, result, starting,
                                                                  factory):
    def prepare(root):
        marker(root, boot=OTHER_BOOT, to=to, frm=frm)
        lock(root, "a1b2c3d4e5f6", boot=OTHER_BOOT)

    bridge = starting(prepare=prepare)
    assert not (bridge.root / updatehelper.MARKER).exists()
    factory.client.fire_connect()
    ended = transaction(factory.client)
    assert (ended["id"], ended["phase"], ended["result"]) == ("a1b2c3d4e5f6", "finished", result)
    if result != "installed":
        assert refusal(factory.client)[0] == "interrupted"


def test_the_new_plugin_follows_the_proof_to_the_end_and_clears_the_marker(starting, factory):
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(root)))
    factory.client.fire_connect()
    assert transaction(factory.client)["phase"] == "proving"
    # The helper commits: the transaction directory goes, the marker says the end.
    shutil.rmtree(made["dir"])
    marker(bridge.root, phase="finished", result="installed", finished=NOW)
    shutil.rmtree(made["dir"])
    tick()
    assert transaction(factory.client)["result"] == "installed"
    assert refusal(factory.client) is None
    assert not (bridge.root / updatehelper.MARKER).exists()


def test_a_finished_marker_is_reported_once_and_removed(starting, factory):
    bridge = starting(prepare=lambda root: marker(
        root, phase="finished", result="rolled_back", reason="not_started",
        error="the new plugin did not start; the previous version 0.2.0 is back",
        finished=NOW))
    factory.client.fire_connect()
    assert transaction(factory.client)["result"] == "rolled_back"
    assert refusal(factory.client)[0] == "not_started"
    assert not (bridge.root / updatehelper.MARKER).exists()


def test_the_last_update_is_reported_after_a_restart(starting, factory):
    starting(prepare=lambda root: updatehelper.write_json(str(root / updatehelper.LAST), {
        "id": "a1b2c3d4e5f6", "started_by": "mqtt", "target": "0.3.0", "from": "0.2.0",
        "phase": "finished", "started": NOW - 900, "finished": NOW - 600,
        "result": "installed", "reason": None, "error": None}))
    factory.client.fire_connect()
    assert transaction(factory.client)["result"] == "installed"


# ---------------------------------------------------------- the integration --


def test_the_integrations_topic_is_read_and_narrows_the_offer(box, factory):
    bridge = box(releases=[release("0.4.0", min_integration="0.5.0"), release("0.3.1"),
                           release("0.3.0")])
    assert (INTEGRATION, 1) in [(t, q) for t, q in factory.client.subscriptions]
    factory.client.fire_message(INTEGRATION, json.dumps(
        {"integration": "0.4.0", "contract": 1, "plugin_min": "0.3.1"}).encode(), retain=True)
    available = factory.client.last(UPDATE).json()["available"]
    assert [a["version"] for a in available] == ["0.4.0", "0.3.1"]
    assert available[0]["reason"] == "incompatible"
    send(factory, {"version": "0.4.0"})
    assert refusal(factory.client)[0] == "incompatible"
    directory = accepted(bridge, factory, {"version": "0.3.1"})
    assert json.loads((directory / "request.json").read_text())["integration"] == {
        "integration": "0.4.0", "contract": 1, "plugin_min": "0.3.1"}


def test_the_integrations_contract_and_floor_are_judged(box, factory):
    box()
    factory.client.fire_message(INTEGRATION, json.dumps(
        {"integration": "0.4.0", "contract": 2, "plugin_min": None}).encode(), retain=True)
    send(factory, {"version": "0.4.0"})
    assert refusal(factory.client) == (
        "incompatible",
        "version 0.4.0 is not compatible with the Home Assistant integration on this broker")
    factory.client.fire_message(INTEGRATION, json.dumps(
        {"integration": "0.4.0", "contract": 1, "plugin_min": "0.4.1"}).encode(), retain=True)
    send(factory, {"version": "0.4.0"})
    assert refusal(factory.client)[0] == "below_floor"


def test_an_empty_integration_topic_forgets_and_a_bad_one_is_ignored(box, factory):
    bridge = box()
    good = {"integration": "0.4.0", "contract": 1, "plugin_min": "0.2.0"}
    factory.client.fire_message(INTEGRATION, json.dumps(good).encode(), retain=True)
    factory.client.fire_message(INTEGRATION, b"{not json", retain=True)
    assert bridge.self_update.integration == good
    factory.client.fire_message(INTEGRATION, b"", retain=True)
    assert bridge.self_update.integration is None


# ---------------------------------------------------------- the softcam collapse --


def test_a_proved_install_asks_for_one_softcam_collapse_where_it_may(box, factory, settings):
    bridge = box()
    calls = []

    class Softcam:
        name = "softcam"

        def restart(self, reason, origin):
            calls.append((reason, origin))

    bridge._publishers.append(Softcam())
    settings.softcam_restart_allowed.value = True
    bridge.self_update._end({"id": "a1b2c3d4e5f6", "result": "installed", "error": None,
                             "phase": "finished"}, None, followed=True)
    MainLoop.advance(selfupdate.COLLAPSE_DELAY_MILLISECONDS)
    assert calls == [("autoheal", MQTT)]
    settings.softcam_restart_allowed.value = False
    bridge.self_update._end({"id": "a1b2c3d4e5f6", "result": "installed", "error": None,
                             "phase": "finished"}, None, followed=True)
    MainLoop.advance(selfupdate.COLLAPSE_DELAY_MILLISECONDS)
    assert calls == [("autoheal", MQTT)]


# ------------------------------------------------------ no first import --


ISOLATED = "MQTTBRIDGE_TEST_ISOLATED"


def test_the_closed_path_imports_nothing_new():
    """Steps 3-9 with every module the start did not import refused - in a fresh interpreter.

    Review S9: in this process the tests before have imported nearly every module of the plugin,
    and a module already in `sys.modules` is never refused, so a first import on the closed
    path passed here while it failed alone. The scenario therefore runs alone, where
    `sys.modules` holds what the test harness and the bridge's start imported and nothing else.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider",
         __file__ + "::test_the_closed_path_alone"],
        env=dict(os.environ, **{ISOLATED: "1"}), cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    # Ran, not skipped.
    assert "PASSED" in result.stdout and "test_the_closed_path_alone" in result.stdout


class _Session:
    def __init__(self):
        self.opened = []

    def open(self, screen, *args, **kwargs):
        self.opened.append(screen)


@pytest.mark.skipif(not os.environ.get(ISOLATED),
                    reason="run alone, in a fresh interpreter, by the test above")
def test_the_closed_path_alone(box, factory, receiver, monkeypatch):
    from MQTTBridge import plugin as plugin_module

    bridge = box()
    directory = accepted(bridge, factory)
    monkeypatch.setattr(plugin_module, "_bridge", bridge)
    attempts = []

    class Refuse:
        def find_spec(self, name, path=None, target=None):
            if name.startswith("MQTTBridge") and name not in sys.modules:
                attempts.append(name)
                raise ImportError("the plugin's files are being replaced: " + name)
            return None

    monkeypatch.setattr(sys, "meta_path", [Refuse()] + sys.meta_path)
    helper_says(directory, phase="installing")
    tick()
    factory.client.fire_message(ROOT + "/cmd/zap", b"1:0:19:283D:3FB:1:C00000:0:0:0:")
    session = _Session()
    plugin_module.open_setup(session)
    assert session.opened == [MessageBox]
    helper_says(directory, phase="restarting")
    tick()
    receiver.session.callbacks[-1][0](False)
    helper_says(directory, phase="finished", result="withdrawn_before_restart",
                reason="question", error=updatehelper.SENTENCES["question"], finished=NOW)
    tick()
    factory.client.fire_connect()
    assert attempts == []
    assert transaction(factory.client)["result"] == "withdrawn_before_restart"


def test_every_module_the_update_path_uses_is_imported_with_the_bridge():
    import importlib

    importlib.import_module("MQTTBridge.bridge")
    for name in ("selfupdate", "updatehelper", "trust", "trustfile", "netfetch", "power",
                 "recording", "epgimport", "softcam", "updatecheck"):
        assert "MQTTBridge." + name in sys.modules


# --------------------------------------------- the update and the uninstall --


def uninstall_ready(bridge, settings, monkeypatch):
    monkeypatch.setattr(Uninstaller, "root", str(bridge.root))
    monkeypatch.setattr(Uninstaller, "plugin_directory",
                        str(bridge.root / "plugin" / "MQTTBridge"))
    settings.uninstall_allowed.value = True
    assert bridge.uninstaller.probe()


def removals(since):
    return [command for container in ConsoleAppContainer.instances[since:]
            for command in container.commands if "opkg" in command and "remove" in command]


@pytest.mark.parametrize("phase, polled", [
    ("downloading", True), ("snapshot", True),
    # The helper has moved on and the plugin has not polled yet: the doors are still open.
    ("installing", False), ("restarting", False),
])
def test_an_uninstall_is_refused_while_an_update_runs(phase, polled, box, factory, settings,
                                                      monkeypatch):
    """Review M1: two package-manager transactions never run side by side."""
    bridge = box()
    uninstall_ready(bridge, settings, monkeypatch)
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase=phase)
    if polled:
        tick()
    assert not bridge.self_update.closed
    since = len(ConsoleAppContainer.instances)
    factory.client.fire_message(ROOT + "/cmd/uninstall", NODE.encode())
    assert refusal(factory.client) == ("busy", BUSY)
    assert bridge.uninstaller.phase is None
    refused = bridge.uninstaller.request(NODE, origin=PAGE)
    assert (refused.reason, str(refused)) == ("busy", BUSY)
    for _ in range(5):
        MainLoop.advance(100)
        factory.client.acknowledge()
    assert removals(since) == []


def test_an_uninstall_is_refused_while_an_ssh_install_holds_the_lock(box, factory, settings,
                                                                    monkeypatch):
    bridge = box()
    uninstall_ready(bridge, settings, monkeypatch)
    held = lock(bridge.root, "0123456789ab", origin=None, alive=False)
    refused = bridge.uninstaller.request(NODE)
    assert (refused.reason, str(refused)) == ("busy", BUSY)
    # The permission and the node id are still asked first.
    assert bridge.uninstaller.request("another_node") == \
        "the payload must be this receiver's node id"
    # A lock of another boot is stale, and holds nothing.
    shutil.rmtree(held)
    lock(bridge.root, "0123456789ab", boot=OTHER_BOOT)
    assert bridge.uninstaller.request(NODE) is None


@pytest.mark.parametrize("origin", [MQTT, PAGE, SCREEN])
def test_an_update_is_refused_while_an_uninstall_runs(origin, box, factory, settings,
                                                      monkeypatch):
    """Review M1, the other way: the uninstall's closed doors are not the only guard."""
    bridge = box()
    uninstall_ready(bridge, settings, monkeypatch)
    assert bridge.uninstaller.request(NODE) is None
    refused = bridge.self_update.request(json.dumps({"version": "0.4.0"}), origin=origin)
    assert (refused.reason, str(refused)) == ("busy",
                                              "the plugin is being removed from the receiver")
    assert directories(bridge.root) == []


# ------------------------------------------------ the doors from `installing` --


def test_the_doors_close_while_the_package_manager_runs(box, factory, receiver):
    """Review S1: opkg rewrites the files during `installing`; nothing runs from them then."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase="installing")
    tick()
    assert bridge.self_update.closed and not bridge.self_update.silent
    assert bridge.publisher("power") is None
    opened = list(getattr(receiver.session, "opened", []))
    assert bridge.run_command("restart_gui", "PRESS", PAGE) == DOORS
    factory.client.fire_message(ROOT + "/cmd/restart_gui", b"PRESS")
    assert refusal(factory.client) == (None, DOORS)
    assert list(getattr(receiver.session, "opened", [])) == opened
    # Nothing is asked yet: the question waits for the helper's `restarting`.
    assert receiver.session.callbacks == []
    assert not (directory / "restart.json").exists()
    helper_says(directory, phase="restarting")
    tick(3)
    asked = [c for c in receiver.session.callbacks if c[1] is standby_module.TryQuitMainloop]
    assert len(asked) == 1


def test_a_package_manager_failure_behind_closed_doors_reopens_them(box, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase="installing")
    tick()
    assert bridge.self_update.closed
    clients = len(factory.clients)
    error = "the package manager could not install version 0.4.0: exit 255"
    helper_says(directory, phase="finished", result="failed", reason="opkg_failed", error=error,
                finished=NOW + 5)
    tick()
    assert not bridge.self_update.closed
    assert len(factory.clients) == clients + 1
    factory.client.fire_connect()
    assert refusal(factory.client) == ("opkg_failed", error)


# ------------------------------------------- the household at the restart --


@pytest.mark.parametrize("change", ["standby", "recording", "epg_import"])
def test_a_household_change_before_the_restart_withdraws_instead_of_asking(
        change, box, factory, receiver, monkeypatch):
    """Review S2: the guards that refused the request are asked again right before the quit."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase="installing")
    tick()
    if change == "standby":
        receiver.enter_standby()
    elif change == "recording":
        monkeypatch.setattr(recording, "is_recording", lambda session: True)
    else:
        monkeypatch.setattr(epgimport, "running", lambda: True)
    helper_says(directory, phase="restarting")
    tick()
    assert json.loads((directory / "withdraw").read_text()) == {"reason": change}
    assert not (directory / "restart.json").exists()
    assert receiver.session.callbacks == []


def test_a_downgrade_withdrawn_for_standby_retracts_nothing(box, factory, receiver):
    bridge = box()
    assert bridge.self_update.request(json.dumps({"version": "0.2.5"}), origin=SCREEN,
                                      downgrade=True) is None
    directory = directories(bridge.root)[0]
    receiver.enter_standby()
    before = len(factory.client.published)
    helper_says(directory, phase="restarting")
    tick()
    assert json.loads((directory / "withdraw").read_text()) == {"reason": "standby"}
    assert [e for e in factory.client.published[before:] if e.qos == 1] == []
    assert not bridge.self_update.silent


# ------------------------------------------------------- after a dead helper --


def test_after_a_dead_helper_the_next_update_is_told_why_it_must_wait(box, factory):
    """Review S3: not "already running" - the lock of a helper that is gone, and for how long."""
    bridge = box()
    directory = accepted(bridge, factory)
    ident = directory.name[len("update-"):]
    # Its last beat 130 s ago: 1670 s of the thirty minutes are left.
    lock(bridge.root, ident, alive=False, uptime=UPTIME - 130)
    helper_says(directory, phase="snapshot")
    helper_process(bridge, directory, running=False)
    tick()
    assert transaction(factory.client)["result"] == "interrupted"
    refused = bridge.self_update.request(json.dumps({"version": "0.4.0"}))
    assert refused.reason == "busy"
    assert str(refused) == ("the previous update stopped without finishing; a new one is "
                            "possible in about 28 minutes, when its lock on the receiver expires")


@pytest.mark.parametrize("owner", ["running", "ssh"])
def test_a_lock_that_may_be_alive_is_still_already_running(owner, box):
    bridge = box()
    if owner == "running":
        lock(bridge.root, "0123456789ab")
    else:
        # The installer's pid is never a liveness test: its record names a short-lived process.
        lock(bridge.root, "0123456789ab", origin=None, alive=False)
    refused = bridge.self_update.request(json.dumps({"version": "0.4.0"}))
    assert (refused.reason, str(refused)) == ("busy", BUSY)


@pytest.mark.parametrize("to, frm, result", [
    (("0.3.0", COMMIT), ("0.2.0", "ef" * 20), "installed"),
    (("0.4.0", "ab" * 20), ("0.3.0", COMMIT), "rolled_back"),
])
def test_the_start_after_a_dead_helper_says_how_it_ended(to, frm, result, starting, factory):
    """Review S4: the marker outlives the helper, and the running build is the verdict."""
    made = {}

    def prepare(root):
        made["dir"] = marker(root, phase="restarting", to=to, frm=frm)
        (made["dir"] / "helper.pid").write_text("4321\n")

    bridge = starting(prepare=prepare)
    factory.client.fire_connect()
    ended = transaction(factory.client)
    assert (ended["id"], ended["phase"], ended["result"]) == ("a1b2c3d4e5f6", "finished", result)
    assert not (bridge.root / updatehelper.MARKER).exists()
    assert bridge.self_update._current is None


def test_a_followed_transaction_past_its_deadline_is_let_go(starting, factory):
    """Review S5: the marker's deadline bounds the following even while `status.json` exists."""
    made = {}

    def prepare(root):
        made["dir"] = marker(root, phase="proving", deadline=UPTIME + 5)
        helper_says(made["dir"], phase="proving")

    bridge = starting(prepare=prepare)
    assert bridge.self_update._current is not None
    tick(3)
    assert bridge.self_update._current is not None
    (bridge.root / "proc" / "uptime").write_text(f"{UPTIME + 3600:.2f} 1.00\n")
    tick()
    assert bridge.self_update._current is None
    assert not (bridge.root / updatehelper.MARKER).exists()


def test_a_helper_that_finished_and_exited_between_two_reads_is_not_interrupted(
        box, factory, receiver, monkeypatch):
    """Review S6: the status is read again once the helper is seen gone."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase="restarting")
    tick()
    real = SelfUpdater._helper_gone

    def finished_then_gone(self, current):
        helper_says(directory, phase="finished", result="withdrawn_before_restart",
                    reason="question", error=updatehelper.SENTENCES["question"])
        helper_process(bridge, directory, running=False)
        return real(self, current)

    monkeypatch.setattr(SelfUpdater, "_helper_gone", finished_then_gone)
    tick()
    factory.client.fire_connect()
    assert transaction(factory.client)["result"] == "withdrawn_before_restart"
    assert refusal(factory.client)[0] == "question"


@pytest.mark.parametrize("command", [
    b"python3\0/tmp/mqttbridge-install/installer_helper.py\0",
    b"/usr/bin/python3\0/home/root/mqttbridge-backups/update-0123456789ab/helper.py\0",
    b"/usr/bin/python3\0-c\0import helper.py\0",
])
def test_only_this_transactions_helper_is_the_helper(command, box, factory):
    """Review S7: the helper's exact path, as an argument - not a substring of the command."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    assert bridge.self_update._helper_gone(bridge.self_update._current) is False
    (bridge.root / "proc" / "4321" / "cmdline").write_bytes(command)
    assert bridge.self_update._helper_gone(bridge.self_update._current) is True


def test_the_documented_started_by_values_are_the_ones_the_plugin_produces():
    """Review S8: an SSH install is not reported on `update`, so `ssh` is not in the contract."""
    topics = (Path(__file__).resolve().parents[1] / "docs" / "TOPICS.md").read_text(
        encoding="utf-8")
    row = next(line for line in topics.splitlines() if line.startswith("| `started_by` |"))
    values = row.split("|")[3].split(".")[0]
    assert set(re.findall(r"`([a-z_]+)`", values)) == {
        "mqtt", "home_assistant", "screen", "page"}


def test_a_launch_bound_behind_closed_doors_reopens_them(box, factory, mono):
    """Review S10: every record gone after the doors closed - the launch bound reopens them."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_says(directory, phase="restarting")
    tick()
    assert bridge.self_update.closed
    shutil.rmtree(directory)
    clients = len(factory.clients)
    mono.now = selfupdate.LAUNCH_WAIT_SECONDS + 5
    tick()
    assert bridge.self_update._current is None
    assert not bridge.self_update.closed
    assert len(factory.clients) == clients + 1
    factory.client.fire_connect()
    assert refusal(factory.client)[0] == "internal_error"
    assert bridge.run_command("restart_gui", "PRESS", PAGE) != DOORS


# ------------------------------------------------------ review S11: test gaps --


def test_the_normal_end_with_the_pid_file_left_is_not_interrupted(box, factory, receiver):
    """R3: `finished` written and the helper gone - its pid file stays - is the end it wrote."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase="restarting")
    tick()
    receiver.session.callbacks[-1][0](True)
    helper_says(directory, phase="finished", result="withdrawn_before_restart",
                reason="question", error=updatehelper.SENTENCES["question"], finished=NOW + 70)
    helper_process(bridge, directory, running=False)
    assert (directory / "helper.pid").exists()
    tick()
    factory.client.fire_connect()
    assert transaction(factory.client)["result"] == "withdrawn_before_restart"
    assert refusal(factory.client)[0] == "question"


def test_a_new_plugin_confirms_while_the_marker_still_says_restarting(starting):
    """R9: the helper writes `proving` only after it saw the new pid; `restarting` confirms too."""
    made = {}
    starting(prepare=lambda root: made.update(dir=marker(root, phase="restarting")))
    started = json.loads((made["dir"] / "started.json").read_text())
    assert started == {"version": "0.3.0", "commit": COMMIT, "pid": os.getpid()}


@pytest.mark.parametrize("to, frm, result", [
    # Spike S1's shape: the same number, another build.
    (("0.3.0", "ab" * 20), ("0.3.0", COMMIT), "rolled_back"),
    (("0.3.0", COMMIT), ("0.3.0", "ab" * 20), "installed"),
    (("0.3.0", "ab" * 20), ("0.3.0", "ef" * 20), "interrupted"),
])
def test_after_a_power_loss_a_same_number_build_is_judged_by_its_commit(to, frm, result,
                                                                        starting, factory):
    """R16: the power-loss verdict compares the build commit, not only the version."""
    def prepare(root):
        marker(root, boot=OTHER_BOOT, to=to, frm=frm)
        lock(root, "a1b2c3d4e5f6", boot=OTHER_BOOT)

    starting(prepare=prepare)
    factory.client.fire_connect()
    assert transaction(factory.client)["result"] == result


def test_a_retraction_publish_the_session_refuses_withdraws_the_downgrade(box, factory,
                                                                         receiver):
    """R7: a publish refused outright (`rc` not 0) is a failed retraction."""
    bridge = box()
    assert bridge.self_update.request(json.dumps({"version": "0.2.5"}), origin=SCREEN,
                                      downgrade=True) is None
    directory = directories(bridge.root)[0]
    factory.client.publish_rc = 4
    helper_says(directory, phase="restarting")
    tick()
    assert json.loads((directory / "withdraw").read_text()) == {"reason": "retraction"}
    assert not (directory / "restart.json").exists()
    assert receiver.session.callbacks == []


def test_the_plugin_menu_entry_behind_closed_doors_says_only_that(box, factory, monkeypatch):
    """R22: `plugin.open_setup` - the menu entry - with the doors closed opens no setup screen."""
    from MQTTBridge import plugin as plugin_module

    bridge = box()
    restarting(bridge, factory)
    monkeypatch.setattr(plugin_module, "_bridge", bridge)
    session = _Session()
    plugin_module.open_setup(session)
    assert session.opened == [MessageBox]


def test_a_status_of_another_transaction_is_not_followed(box, factory):
    """R10: a status file that names another id says nothing about this transaction."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_says(directory, phase="installing", id="0123456789ab")
    tick()
    assert transaction(factory.client)["phase"] == "downloading"
    assert not bridge.self_update.closed


def test_a_version_that_is_not_a_release_number_is_a_bad_request(box, factory):
    """R20: `0.4` is not `latest` and not a release number: unreadable, not unknown."""
    box()
    send(factory, {"version": "0.4"})
    assert refusal(factory.client)[0] == "bad_request"


def test_a_relayed_index_is_not_judged_behind_closed_doors(box, factory, monkeypatch):
    """R21: nothing is written into the trust file while the doors are closed."""
    bridge = box()
    restarting(bridge, factory)
    calls = []
    monkeypatch.setattr(bridge.updates, "on_release_index",
                        lambda payload, retain: calls.append(payload))
    factory.client.fire_message(RELEASE_INDEX_TOPIC, b'{"index": "", "sig": ""}', retain=True)
    assert calls == []


# ------------------------------------------- the helper's review round (merged) --


@pytest.mark.parametrize("boot", [OTHER_BOOT, BOOT])
def test_a_marker_still_at_installing_is_interrupted_whatever_version_runs(boot, starting,
                                                                          factory):
    """The helper writes the marker before the package manager: the files may be a mix."""
    def prepare(root):
        directory = marker(root, phase="installing", boot=boot, to=("0.3.0", COMMIT))
        lock(root, "a1b2c3d4e5f6", boot=boot, alive=False)
        (directory / "helper.pid").write_text("4321\n")

    bridge = starting(prepare=prepare)
    factory.client.fire_connect()
    ended = transaction(factory.client)
    assert (ended["phase"], ended["result"]) == ("finished", "interrupted")
    assert refusal(factory.client)[0] == "interrupted"
    assert not (bridge.root / updatehelper.MARKER).exists()


def test_after_a_reboot_the_rate_limit_counts_this_boots_uptime(box, monkeypatch):
    """TRANSACTION.md section 1: an end of another boot is at least this boot's uptime ago."""
    bridge = box()
    last = str(bridge.root / updatehelper.LAST)
    # By the wall clock a day ago - a clock that is not to be trusted after a reboot.
    updatehelper.write_json(last, {"id": "0123456789ab", "finished": NOW - 86400,
                                   "boot_id": OTHER_BOOT, "uptime": 5000})
    (bridge.root / "proc" / "uptime").write_text("120.00 1.00\n")
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})).reason == "rate_limited"
    (bridge.root / "proc" / "uptime").write_text("601.00 1.00\n")
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})) is None


def test_after_the_retraction_nothing_stale_is_retracted_either(box, factory):
    """R24: a stale-topic sweep while silent would publish behind the retraction."""
    bridge = box()
    restarting(bridge, factory, {"version": "0.2.5"}, downgrade=True)
    factory.client.acknowledge()
    MainLoop.advance(selfupdate.RETRACTION_POLL_MILLISECONDS)
    assert bridge.self_update.silent
    bridge.state.remember("enigma2/an_older_node/info")
    quiet = len(factory.client.published)
    assert bridge.retract_stale() == 0
    assert factory.client.published[quiet:] == []


def test_a_connection_lost_during_the_retraction_withdraws_at_once(box, factory, receiver):
    """R27: not after the fifteen-second bound - a session that is gone acknowledges nothing."""
    bridge = box()
    directory = restarting(bridge, factory, {"version": "0.2.5"}, downgrade=True)
    factory.client.fire_disconnect(7)
    MainLoop.advance(selfupdate.RETRACTION_POLL_MILLISECONDS)
    assert json.loads((directory / "withdraw").read_text()) == {"reason": "retraction"}
    assert receiver.session.callbacks == []


# ------------------------------------------------ the helper's delta review --


def test_acceptance_keys_and_origin_come_from_the_build_alone(box, factory):
    """Delta review: the request's trust fields are the running build's, whatever anyone sends.

    The same field selects the drill hook, the key set and the index's lineage; nothing a
    broker client, the page or the integration's topic says may reach it.
    """
    bridge = box()
    factory.client.fire_message(INTEGRATION, json.dumps(
        {"integration": "0.4.0", "contract": 1, "plugin_min": None, "acceptance": True,
         "origin": "https://example.invalid/", "keys": []}).encode(), retain=True)
    directory = accepted(bridge, factory, {
        "version": "0.4.0", "acceptance": True, "origin": "https://example.invalid/",
        "keys": [{"id": "x", "key": "00" * 32}]})
    request = json.loads((directory / "request.json").read_text())
    assert request["acceptance"] is False
    assert request["origin"] == bridge.updates.origin
    assert request["keys"] == trust.keys_to_data(bridge.updates.keys)
    assert "acceptance" not in json.dumps(request["integration"])


@pytest.mark.parametrize("origin", [PAGE, SCREEN])
def test_the_page_and_the_screen_cannot_ask_for_acceptance_either(origin, box):
    bridge = box()
    assert bridge.self_update.request(json.dumps({"version": "0.4.0", "acceptance": True}),
                                      origin=origin) is None
    request = json.loads((directories(bridge.root)[0] / "request.json").read_text())
    assert request["acceptance"] is False


@pytest.mark.parametrize("installing_seen", [True, False])
def test_the_drill_from_installing_to_rolling_back_keeps_the_doors_closed(
        installing_seen, box, factory, receiver):
    """The acceptance drill goes from `installing` straight into R2, never through `restarting`.

    With a quick package manager the poll may never see `installing` at all.
    """
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    if installing_seen:
        helper_says(directory, phase="installing")
        tick()
        assert bridge.self_update.closed
    helper_says(directory, phase="rolling_back")
    tick(3)
    assert bridge.self_update.closed
    assert bridge.run_command("restart_gui", "PRESS", PAGE) == DOORS
    assert receiver.session.callbacks == []
    assert not (directory / "restart.json").exists()
    clients = len(factory.clients)
    helper_says(directory, phase="finished", result="rolled_back", reason="drill",
                error=updatehelper.SENTENCES["drill"], finished=NOW + 90,
                record={"restore": "done"})
    tick()
    assert not bridge.self_update.closed
    assert len(factory.clients) == clients + 1


@pytest.mark.parametrize("phase", ["installing", "restarting"])
def test_a_failed_restore_keeps_the_doors_closed_and_says_to_reinstall(phase, box, factory,
                                                                      receiver):
    """Delta review D2: the old files could not be put back - a reload would import new code."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase=phase)
    tick()
    if phase == "restarting":
        receiver.session.callbacks[-1][0](True)
    assert bridge.self_update.closed
    clients = len(factory.clients)
    helper_says(directory, phase="finished", result="failed", reason="question",
                error=updatehelper.SENTENCES["question"], finished=NOW + 90,
                record={"restore": "failed: [Errno 28] No space left on device"})
    tick()
    # No fresh session: the doors stay closed, and say why.
    assert len(factory.clients) == clients
    assert bridge.self_update.closed
    assert bridge.self_update._current is None
    ended = transaction(factory.client)
    assert (ended["phase"], ended["result"]) == ("finished", "failed")
    reason, error = refusal(factory.client)
    assert reason == "question" and "install the plugin again" in error
    stuck = bridge.run_command("restart_gui", "PRESS", PAGE)
    assert stuck != DOORS and "install the plugin again" in stuck
    factory.client.fire_message(ROOT + "/cmd/power", b"standby")
    assert refusal(factory.client) == (None, stuck)
    assert selfupdate.household_doors(bridge.self_update) != selfupdate.household_doors()


def test_the_page_says_when_the_old_files_could_not_be_put_back(box, factory, monkeypatch):
    from test_webif import _Request

    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase="installing")
    tick()
    helper_says(directory, phase="finished", result="failed", reason="opkg_failed",
                error="the package manager could not install version 0.4.0: exit 255",
                finished=NOW + 5, record={"restore": "failed: [Errno 5] Input/output error"})
    tick()
    monkeypatch.setattr(webif, "_bridge", lambda: bridge)
    body = webif._page(_Request()).decode("utf-8")
    assert selfupdate.household_doors(bridge.self_update) in body
    assert "<form" not in body


# ------------------------------------------------------------ review round 2 --


def assert_stuck(bridge, factory, clients, reason):
    """The doors closed for good: no fresh session, every surface says to install again."""
    assert len(factory.clients) == clients
    assert bridge.self_update.closed and bridge.self_update.stuck
    assert bridge.self_update._current is None
    assert bridge._publishers == []
    said, error = refusal(factory.client)
    assert said == reason and "install the plugin again" in error
    stuck = bridge.run_command("restart_gui", "PRESS", PAGE)
    assert stuck != DOORS and "install the plugin again" in stuck
    factory.client.fire_message(ROOT + "/cmd/power", b"standby")
    assert refusal(factory.client) == (None, stuck)
    assert selfupdate.household_doors(bridge.self_update) != selfupdate.household_doors()
    return stuck


def test_a_failed_restore_is_stuck_even_when_no_poll_saw_the_files_change(box, factory):
    """DS1: from `downloading` to a failed restore within one poll - decided by the record."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    tick()
    assert not bridge.self_update.closed
    clients = len(factory.clients)
    helper_says(directory, phase="finished", result="failed", reason="opkg_failed",
                error="the package manager could not install version 0.4.0: exit 255",
                finished=NOW + 5, record={"restore": "failed: [Errno 5] Input/output error"})
    tick()
    assert_stuck(bridge, factory, clients, "opkg_failed")
    assert transaction(factory.client)["result"] == "failed"


def test_a_stuck_end_while_disconnected_is_said_by_the_next_connect(box, factory):
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    tick()
    factory.client.fire_disconnect()
    assert not bridge.connected
    helper_says(directory, phase="finished", result="failed", reason="opkg_failed",
                error="the package manager could not install version 0.4.0: exit 255",
                finished=NOW + 5, record={"restore": "failed: [Errno 5] Input/output error"})
    tick()
    assert bridge.self_update.stuck
    factory.client.fire_connect()
    reason, error = refusal(factory.client)
    assert reason == "opkg_failed" and "install the plugin again" in error


@pytest.mark.parametrize("seen", [True, False])
@pytest.mark.parametrize("reason, restore, name", [
    ("restore_failed", "failed: [Errno 28] No space left on device", "STUCK"),
    ("restore_incomplete", "partial: opkg's records: [Errno 5] Input/output error",
     "STUCK_PARTIAL"),
])
def test_the_helpers_restore_reasons_keep_the_doors_closed(reason, restore, name, seen,
                                                          box, factory):
    """The helper's own ends for a restore that did not complete: no reload after either."""
    sentence = getattr(selfupdate, name, "(no such sentence)")
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    if seen:
        helper_says(directory, phase="installing")
    tick()
    assert bridge.self_update.closed is seen
    clients = len(factory.clients)
    detail = restore.split(": ", 1)[1]
    error = str(updatehelper.Fail(reason, previous="0.3.0", detail=detail))
    helper_says(directory, phase="finished", result="failed", reason=reason, error=error,
                finished=NOW + 5, record={"restore": restore, "cause": "opkg_failed"})
    tick()
    assert len(factory.clients) == clients
    assert bridge.self_update.closed and bridge.self_update.stuck
    assert bridge._publishers == []
    # The helper's sentence names the reinstall already, and is said as it is.
    assert refusal(factory.client) == (reason, error)
    assert "reinstall" in error
    assert bridge.run_command("restart_gui", "PRESS", PAGE) == sentence
    assert "install the plugin again" in sentence
    household = selfupdate.household_doors(bridge.self_update)
    assert household != selfupdate.household_doors()
    assert ("could not be put back" in household) is (reason == "restore_failed")


def test_a_failure_with_nothing_to_restore_is_not_stuck(box, factory):
    """The helper tries a restore only after the package manager ran: none, nothing changed."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase="finished", result="failed", reason="opkg_busy",
                error=updatehelper.SENTENCES["opkg_busy"], finished=NOW + 5, record={})
    tick()
    assert not bridge.self_update.closed and not bridge.self_update.stuck
    assert refusal(factory.client)[0] == "opkg_busy"
    assert bridge.run_command("restart_gui", "PRESS", PAGE) != DOORS


def test_a_followed_transactions_failed_restore_does_not_close_this_process(starting, factory):
    """Only the process the files changed under is stuck; one that started on them is not."""
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(root)))
    factory.client.fire_connect()
    assert bridge.self_update._current is not None
    helper_process(bridge, made["dir"])
    helper_says(made["dir"], phase="finished", result="failed", reason="not_started",
                error="x", finished=NOW + 5, record={"restore": "failed: x"})
    tick()
    assert bridge.self_update._current is None
    assert not bridge.self_update.closed and not bridge.self_update.stuck


@pytest.mark.parametrize("phase", ["installing", "restarting", "rolling_back"])
def test_a_helper_that_died_after_the_package_manager_started_keeps_the_doors_closed(
        phase, box, factory, receiver):
    """DS2: an orphaned opkg may still be writing; a reload would import what it wrote."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase=phase)
    tick()
    assert bridge.self_update.closed
    marker_path = bridge.root / updatehelper.MARKER
    updatehelper.write_json(str(marker_path), {"id": directory.name[len("update-"):],
                                               "phase": phase})
    clients = len(factory.clients)
    helper_process(bridge, directory, running=False)
    tick()
    stuck = assert_stuck(bridge, factory, clients, "interrupted")
    assert "stopped" in stuck
    ended = transaction(factory.client)
    assert (ended["phase"], ended["result"]) == ("finished", "interrupted")
    # The next start says how it ended, by the build that starts.
    assert marker_path.exists()


def test_a_helper_that_died_at_installing_before_its_marker_reopens_the_doors(box, factory):
    """The helper writes the marker before the package manager and runs nothing without it."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase="installing")
    tick()
    assert bridge.self_update.closed
    # A marker left by another transaction says nothing about this one.
    updatehelper.write_json(str(bridge.root / updatehelper.MARKER),
                            {"id": "0123456789ab", "phase": "installing"})
    clients = len(factory.clients)
    helper_process(bridge, directory, running=False)
    tick()
    assert not bridge.self_update.closed and not bridge.self_update.stuck
    assert len(factory.clients) == clients + 1
    factory.client.fire_connect()
    reason, error = refusal(factory.client)
    assert reason == "interrupted" and "install the plugin again" not in error


def test_a_helper_that_died_at_installing_unseen_still_closes_the_doors(box, factory):
    """DS1 and DS2 together: the first look at `installing` finds the helper already gone."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    tick()
    assert not bridge.self_update.closed
    helper_says(directory, phase="installing")
    updatehelper.write_json(str(bridge.root / updatehelper.MARKER),
                            {"id": directory.name[len("update-"):], "phase": "installing"})
    clients = len(factory.clients)
    helper_process(bridge, directory, running=False)
    tick()
    assert_stuck(bridge, factory, clients, "interrupted")


def plausible_index(bridge, issued):
    raw = index_bytes(1, [release("0.4.0"), release("0.3.0")], issued=issued)
    bridge.updates._held = (trust.parse_index(raw), "relay")


def test_after_a_reboot_an_update_days_ago_is_not_rate_limited(box):
    """DS3: with a clock that can be believed, days ago is not less than ten minutes ago."""
    bridge = box()
    plausible_index(bridge, issued=NOW - 86400)
    last = str(bridge.root / updatehelper.LAST)
    updatehelper.write_json(last, {"id": "0123456789ab", "finished": NOW - 3 * 86400,
                                   "boot_id": OTHER_BOOT, "uptime": 5000.0,
                                   "phase": "finished", "result": "installed"})
    (bridge.root / "proc" / "uptime").write_text("120.00 1.00\n")
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})) is None


@pytest.mark.parametrize("case", ["1970_now", "1970_then", "before_the_build",
                                  "before_the_index", "then_after_now", "nine_minutes"])
def test_after_a_reboot_an_implausible_clock_keeps_the_limit(case, box, monkeypatch):
    """DS3: the wall clock lifts the limit only when both ends of it can be believed."""
    bridge = box()
    now, finished, issued = NOW, NOW - 3 * 86400, NOW - 86400
    if case == "1970_now":
        now = 200
    elif case == "1970_then":
        finished = 100
    elif case == "before_the_build":
        now = BUILD["time"] - 1
    elif case == "before_the_index":
        issued = NOW + 60
    elif case == "then_after_now":
        finished = NOW + 3600
    elif case == "nine_minutes":
        finished = NOW - 540
    monkeypatch.setattr(SelfUpdater, "clock", staticmethod(lambda: now))
    plausible_index(bridge, issued=issued)
    last = str(bridge.root / updatehelper.LAST)
    updatehelper.write_json(last, {"id": "0123456789ab", "finished": finished,
                                   "boot_id": OTHER_BOOT, "uptime": 5000.0,
                                   "phase": "finished", "result": "installed"})
    (bridge.root / "proc" / "uptime").write_text("120.00 1.00\n")
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})).reason == "rate_limited"


def test_within_a_boot_the_wall_clock_never_lifts_the_limit(box):
    """The boot-relative rule stands within a boot, whatever the wall clock says."""
    bridge = box()
    plausible_index(bridge, issued=NOW - 86400)
    last = str(bridge.root / updatehelper.LAST)
    updatehelper.write_json(last, {"id": "0123456789ab", "finished": NOW - 3 * 86400,
                                   "boot_id": BOOT, "uptime": UPTIME - 60,
                                   "phase": "finished", "result": "installed"})
    assert bridge.self_update.request(json.dumps({"version": "0.4.0"})).reason == "rate_limited"


# ------------------------------------------------------------ review round 3 --
#
# The helper's R2 now ends `failed` when no interface was seen starting on the old files
# (TRANSACTION.md section 7): `not_stopped` - the process R2 could not stop runs on over the
# previous version's files - and `interface_not_started` - nothing runs when it is written.


def unstopped_sentence():
    return getattr(selfupdate, "STUCK_UNSTOPPED", "(no such sentence)")


def following(starting, factory, receiver):
    """A new plugin that started after the forward restart and follows R2 (`ours` False)."""
    # The marker already says `rolling_back` at this plugin's start, so it records
    # `began: rolling_back` and is, to the plugin, a process R2 itself started - the build rule
    # alone judges it when the helper dies. That is right for the finished ends these tests
    # read; a process of the forward restart that sees R2 begin is `following_the_restart`
    # (marker at `proving`), and tests of a helper that dies under it start from there.
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(root, phase="rolling_back")),
                      session=receiver.session)
    factory.client.fire_connect()
    assert bridge.self_update._current is not None
    assert not bridge.self_update._current["ours"]
    assert not bridge.self_update.closed and bridge._publishers != []
    helper_process(bridge, made["dir"])
    return bridge, made["dir"]


def asking(box, factory, receiver):
    """The process that asked, its doors closed since `installing` (`ours` True)."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase="installing")
    tick()
    assert bridge.self_update.closed
    return bridge, directory


def restarts(receiver):
    return [entry for entry in receiver.session.opened
            if entry == (standby_module.TryQuitMainloop, (3,))]


@pytest.mark.parametrize("record", [
    {"restore": "done", "stop": "not seen", "interface": "not restarted", "cause": "not_started",
     "channel": "unconfirmed"},
    # Keyed on the reason too: a record without `interface` is still the process R2 left.
    {"restore": "done", "cause": "not_started"},
])
@pytest.mark.parametrize("who", ["following", "asking"])
def test_an_interface_r2_could_not_stop_keeps_the_doors_closed_and_says_to_restart(
        who, record, starting, box, factory, receiver):
    """`not_stopped`: this process's files are the previous version's now - never a reload."""
    if who == "following":
        bridge, directory = following(starting, factory, receiver)
        previous = "0.2.0"
    else:
        bridge, directory = asking(box, factory, receiver)
        previous = "0.3.0"
    clients = len(factory.clients)
    error = str(updatehelper.Fail("not_stopped", previous=previous))
    # This process is among those R2 could not stop (review round 4).
    record = dict(record, unstopped=[os.getpid()])
    helper_says(directory, phase="finished", result="failed", reason="not_stopped", error=error,
                finished=NOW + 200, record=record)
    tick()
    updater = bridge.self_update
    # No fresh session: it would import the previous version's modules into this process.
    assert len(factory.clients) == clients
    assert updater.closed and updater.stuck and updater._current is None
    assert bridge._publishers == []
    ended = transaction(factory.client)
    assert (ended["phase"], ended["result"]) == ("finished", "failed")
    # The helper's sentence as it is: it names the repair already.
    assert refusal(factory.client) == ("not_stopped", error)
    assert not (bridge.root / updatehelper.MARKER).exists()
    sentence = unstopped_sentence()
    assert "restart the receiver's interface" in sentence
    assert "install" not in sentence
    # Every other command, and the settings, answer the sentence.
    factory.client.fire_message(ROOT + "/cmd/power", b"standby")
    assert refusal(factory.client) == (None, sentence)
    assert bridge.apply_settings({"log_level": "debug"}) == sentence
    assert bridge.apply_remote_settings({"log_level": "debug"}) == sentence
    send(factory, {"version": "0.4.0"})
    assert refusal(factory.client) == (None, sentence)
    assert restarts(receiver) == []
    # The repair goes through the closed doors - from the page and over MQTT.
    assert bridge.run_command("restart_gui", "PRESS", PAGE) is None
    assert len(restarts(receiver)) == 1
    factory.client.fire_message(ROOT + "/cmd/restart_gui", b"PRESS")
    assert len(restarts(receiver)) == 2
    assert refusal(factory.client) is None
    # Still no session, and the doors still closed: the restart is what ends this process.
    assert len(factory.clients) == clients
    assert updater.closed and updater.doors_refusal() == sentence
    household = selfupdate.household_doors(updater)
    assert household not in (selfupdate.household_doors(), selfupdate.household_doors(
        SimpleNamespace(stuck=True, _stuck_sentence=selfupdate.STUCK)))
    assert "restart the user interface" in household and "install" not in household


def test_the_repair_through_closed_doors_keeps_the_restarts_own_guards(starting, factory,
                                                                        receiver):
    """`restart_gui` is let through, not waved through: a recording still refuses it."""
    from conftest import RecordTimerEntry

    bridge, directory = following(starting, factory, receiver)
    helper_says(directory, phase="finished", result="failed", reason="not_stopped",
                error=str(updatehelper.Fail("not_stopped", previous="0.2.0")),
                finished=NOW + 200, record={"restore": "done", "interface": "not restarted"})
    tick()
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    assert bridge.run_command("restart_gui", "PRESS", PAGE) == "the receiver is recording"
    factory.client.fire_message(ROOT + "/cmd/restart_gui", b"PRESS")
    assert refusal(factory.client) == (None, "the receiver is recording")
    assert restarts(receiver) == []
    # Only `restart_gui`: a reboot is not the repair, and stays behind the doors.
    assert bridge.run_command("reboot", "PRESS", PAGE) == unstopped_sentence()


def test_the_page_offers_only_the_interface_restart_after_not_stopped(starting, factory,
                                                                      receiver, monkeypatch,
                                                                      settings):
    from test_setup_screen import FakeSession
    from test_webif import _Request, action_fields, confirmation, new_session, post

    from MQTTBridge import plugin as plugin_module
    from MQTTBridge import setup as setup_screen

    bridge, directory = following(starting, factory, receiver)
    helper_says(directory, phase="finished", result="failed", reason="not_stopped",
                error=str(updatehelper.Fail("not_stopped", previous="0.2.0")),
                finished=NOW + 200, record={"restore": "done", "interface": "not restarted"})
    tick()
    monkeypatch.setattr(webif, "_bridge", lambda: bridge)
    body = webif._page(_Request()).decode("utf-8")
    assert webif._e(selfupdate.household_doors(bridge.self_update)) in body
    # One form, and it is the restart: nothing else can post into this process.
    assert body.count("<form") == 1
    assert "name='action' value='restart_gui'" in body
    resource = webif.MQTTBridgeWebResource()
    session = new_session()
    _request, body = post(resource, session, action_fields("reboot"))
    _request, body = post(resource, session, confirmation(body), csrf=None)
    # Said on the page, above the sentence: the answer is not lost behind the doors.
    assert webif._e("Refused: " + unstopped_sentence()).encode("utf-8") in body
    assert restarts(receiver) == [] and receiver.session.opened == []
    _request, body = post(resource, session, action_fields("restart_gui"))
    request, body = post(resource, session, confirmation(body), csrf=None)
    assert request.response_code == 200
    assert b"Sent: Restart the user interface" in body
    assert len(restarts(receiver)) == 1
    # The television's plugin entry and setup screen say it, and change nothing.
    tv = _Session()
    monkeypatch.setattr(plugin_module, "_bridge", bridge)
    plugin_module.open_setup(tv)
    assert tv.opened == [MessageBox]
    clients = len(factory.clients)
    screen = setup_screen.MQTTBridgeSetup(FakeSession(), settings=settings, bridge=bridge)
    screen.keySave()
    assert len(factory.clients) == clients
    assert bridge.self_update.closed


def test_a_followed_failed_restore_under_a_process_r2_could_not_stop_is_stuck(starting,
                                                                              factory, receiver):
    """The restore reasons win over `not_stopped`, and this process is the one they changed under.

    Before round 3 only the process that asked was held for a failed restore; a follower that
    R2 could not stop is just as much under the changed files.
    """
    bridge, directory = following(starting, factory, receiver)
    clients = len(factory.clients)
    error = str(updatehelper.Fail("restore_failed", previous="0.2.0",
                                  detail="[Errno 28] No space left on device"))
    helper_says(directory, phase="finished", result="failed", reason="restore_failed",
                error=error, finished=NOW + 200,
                record={"restore": "failed: [Errno 28] No space left on device",
                        "stop": "not seen", "interface": "not restarted", "cause": "not_started"})
    tick()
    assert len(factory.clients) == clients
    assert bridge.self_update.closed and bridge.self_update.stuck
    assert refusal(factory.client) == ("restore_failed", error)
    assert bridge.self_update.doors_refusal() == selfupdate.STUCK
    # Here a restart is not the repair: the reinstall is.
    assert bridge.run_command("restart_gui", "PRESS", PAGE) == selfupdate.STUCK


def test_a_process_that_started_after_r2_gave_up_is_not_stuck(starting, factory, receiver):
    """`interface_not_started` read by an interface that came up late: it runs the old files."""
    bridge, directory = following(starting, factory, receiver)
    helper_says(directory, phase="finished", result="failed", reason="interface_not_started",
                error=str(updatehelper.Fail("interface_not_started", previous="0.2.0")),
                finished=NOW + 200, record={"restore": "done", "stop": "seen",
                                            "interface": "not started", "cause": "not_started"})
    tick()
    assert bridge.self_update._current is None
    assert not bridge.self_update.closed and not bridge.self_update.stuck
    assert refusal(factory.client)[0] == "interface_not_started"


@pytest.mark.parametrize("reason, record", [
    ("interface_not_started",
     {"restore": "done", "stop": "seen", "interface": "not started", "cause": "not_started"}),
    # After `not_stopped` the interface was restarted by hand: this start runs the old files.
    ("not_stopped",
     {"restore": "done", "stop": "not seen", "interface": "not restarted",
      "cause": "not_started"}),
])
def test_the_next_start_reports_an_r2_without_an_interface_and_opens(reason, record, starting,
                                                                     factory, receiver):
    """No plugin ran when the helper wrote it; the one that starts reads it and opens normally."""
    error = str(updatehelper.Fail(reason, previous="0.3.0"))
    bridge = starting(prepare=lambda root: marker(
        root, phase="finished", result="failed", reason=reason, error=error, finished=NOW,
        to=("0.4.0", "ab" * 20), frm=("0.3.0", COMMIT), record=record),
        session=receiver.session)
    updater = bridge.self_update
    assert not updater.closed and not updater.stuck and updater._current is None
    assert updater.doors_refusal() is None
    factory.client.fire_connect()
    ended = transaction(factory.client)
    # Said as the helper wrote it: `failed`, never a rollback that runs.
    assert (ended["id"], ended["phase"], ended["result"]) == ("a1b2c3d4e5f6", "finished",
                                                              "failed")
    assert ended["error"] == error
    assert refusal(factory.client) == (reason, error)
    assert not (bridge.root / updatehelper.MARKER).exists()
    assert bridge._publishers != []
    assert bridge.run_command("restart_gui", "PRESS", PAGE) != unstopped_sentence()


@pytest.mark.parametrize("case", ["past_deadline", "another_boot"])
def test_an_r2_without_an_interface_is_still_on_the_update_topic_after_the_marker_went(
        case, starting, factory):
    """A marker past its deadline or from another boot is discarded; the last record still says."""
    error = str(updatehelper.Fail("interface_not_started", previous="0.3.0"))

    def prepare(root):
        marker(root, phase="finished", result="failed", reason="interface_not_started",
               error=error, finished=NOW, deadline=UPTIME - 1 if case == "past_deadline"
               else UPTIME + 600, boot=OTHER_BOOT if case == "another_boot" else BOOT)
        updatehelper.write_json(str(root / updatehelper.LAST), {
            "id": "a1b2c3d4e5f6", "started_by": "home_assistant", "target": "0.4.0",
            "from": "0.3.0", "phase": "finished", "started": NOW - 100, "finished": NOW,
            "result": "failed", "reason": "interface_not_started", "error": error})

    bridge = starting(prepare=prepare)
    assert not bridge.self_update.closed and not bridge.self_update.stuck
    factory.client.fire_connect()
    ended = transaction(factory.client)
    assert (ended["result"], ended["error"]) == ("failed", error)


def test_the_repair_path_imports_nothing_new():
    """After `not_stopped` the files are the previous version's: a first import would load it.

    The page, the television's entry and `restart_gui` through the doors, in a fresh
    interpreter, with every module the start did not import refused - as for the closed path.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider",
         __file__ + "::test_the_repair_path_alone"],
        env=dict(os.environ, **{ISOLATED: "1"}), cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "PASSED" in result.stdout and "test_the_repair_path_alone" in result.stdout


@pytest.mark.skipif(not os.environ.get(ISOLATED),
                    reason="run alone, in a fresh interpreter, by the test above")
def test_the_repair_path_alone(starting, factory, receiver, monkeypatch):
    from test_webif import _Request, action_fields, confirmation, new_session, post

    from MQTTBridge import plugin as plugin_module

    bridge, directory = following(starting, factory, receiver)
    monkeypatch.setattr(plugin_module, "_bridge", bridge)
    monkeypatch.setattr(webif, "_bridge", lambda: bridge)
    attempts = []

    class Refuse:
        def find_spec(self, name, path=None, target=None):
            if name.startswith("MQTTBridge") and name not in sys.modules:
                attempts.append(name)
                raise ImportError("the previous version's files are on disk: " + name)
            return None

    monkeypatch.setattr(sys, "meta_path", [Refuse()] + sys.meta_path)
    helper_says(directory, phase="finished", result="failed", reason="not_stopped",
                error=str(updatehelper.Fail("not_stopped", previous="0.2.0")),
                finished=NOW + 200, record={"restore": "done", "interface": "not restarted"})
    tick()
    assert bridge.self_update.stuck
    factory.client.fire_message(ROOT + "/cmd/zap", b"1:0:19:283D:3FB:1:C00000:0:0:0:")
    session = _Session()
    plugin_module.open_setup(session)
    assert session.opened == [MessageBox]
    assert "restart_gui" in webif._page(_Request()).decode("utf-8")
    resource = webif.MQTTBridgeWebResource()
    page = new_session()
    _request, body = post(resource, page, action_fields("restart_gui"))
    _request, body = post(resource, page, confirmation(body), csrf=None)
    factory.client.fire_message(ROOT + "/cmd/restart_gui", b"PRESS")
    assert attempts == []
    assert len(restarts(receiver)) == 2


# ------------------------------------------------------------ review round 4 --
#
# `not_stopped` alone cannot tell a reader whether it is the process R2 could not stop: the helper
# now lists those pids as `record.unstopped`, from its last look at /proc that answered, and leaves
# the list out when no look answered (TRANSACTION.md section 7).

OTHER_PID = os.getpid() + 1


def unstopped_record(listed):
    record = {"restore": "done", "stop": "not seen", "interface": "not restarted",
              "cause": "not_started", "channel": "unconfirmed"}
    if listed == "listed":
        record["unstopped"] = [OTHER_PID, os.getpid()]
    elif listed == "not_listed":
        record["unstopped"] = [OTHER_PID]
    return record


def assert_open(bridge, factory, reason, error):
    updater = bridge.self_update
    assert not updater.closed and not updater.stuck and updater._current is None
    assert updater.doors_refusal() is None and updater.repair() is None
    factory.client.fire_connect()
    assert refusal(factory.client) == (reason, error)
    assert transaction(factory.client)["result"] == "failed"
    assert bridge._publishers != []


# Asking: this process runs 0.3.0, the transaction's `from`. Following: it runs 0.3.0, the
# marker's `to`, over the restored 0.2.0 - so with no list, judged by its version, it is held.
@pytest.mark.parametrize("who, listed, held", [
    ("asking", "listed", True),
    ("asking", "not_listed", False),
    ("asking", "no_list", False),
    ("following", "listed", True),
    ("following", "not_listed", False),
    ("following", "no_list", True),
])
def test_not_stopped_holds_only_the_process_r2_left_running(who, listed, held, starting, box,
                                                           factory, receiver):
    if who == "following":
        bridge, directory = following(starting, factory, receiver)
        previous = "0.2.0"
    else:
        bridge, directory = asking(box, factory, receiver)
        previous = "0.3.0"
    clients = len(factory.clients)
    error = str(updatehelper.Fail("not_stopped", previous=previous))
    helper_says(directory, phase="finished", result="failed", reason="not_stopped", error=error,
                finished=NOW + 200, record=unstopped_record(listed))
    tick()
    updater = bridge.self_update
    if held:
        assert len(factory.clients) == clients
        assert updater.closed and updater.stuck and updater.repair() == "restart_gui"
        assert refusal(factory.client) == ("not_stopped", error)
        assert bridge.run_command("reboot", "PRESS", PAGE) == unstopped_sentence()
        return
    # A process R2 did not leave running started on the files on disk, or never saw them change.
    assert len(factory.clients) == clients + (1 if who == "asking" else 0)
    assert_open(bridge, factory, "not_stopped", error)


@pytest.mark.parametrize("listed", ["listed", "not_listed", "no_list"])
def test_a_fresh_start_after_not_stopped_opens_whatever_the_list_says(listed, starting, factory,
                                                                     receiver):
    """A process that starts after the end is never the one R2 left: even a pid used again."""
    error = str(updatehelper.Fail("not_stopped", previous="0.3.0"))
    bridge = starting(prepare=lambda root: marker(
        root, phase="finished", result="failed", reason="not_stopped", error=error, finished=NOW,
        to=("0.4.0", "ab" * 20), frm=("0.3.0", COMMIT), record=unstopped_record(listed)),
        session=receiver.session)
    assert not (bridge.root / updatehelper.MARKER).exists()
    assert_open(bridge, factory, "not_stopped", error)


@pytest.mark.parametrize("listed", ["not_listed", "no_list"])
def test_a_previous_version_started_while_r2_finishes_is_not_held(listed, starting, factory,
                                                                  receiver):
    """The process R2 could not stop died; the previous version started and follows the end.

    It runs the files on disk: not listed, it opens; with no list, its version - the
    transaction's `from` - says the same.
    """
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(
        root, phase="rolling_back", to=("0.4.0", "ab" * 20), frm=("0.3.0", COMMIT))),
        session=receiver.session)
    factory.client.fire_connect()
    assert bridge.self_update._current is not None
    helper_process(bridge, made["dir"])
    error = str(updatehelper.Fail("not_stopped", previous="0.3.0"))
    helper_says(made["dir"], phase="finished", result="failed", reason="not_stopped", error=error,
                target="0.4.0", finished=NOW + 200, record=unstopped_record(listed))
    marker(bridge.root, phase="finished", result="failed", reason="not_stopped", error=error,
           finished=NOW + 200, to=("0.4.0", "ab" * 20), frm=("0.3.0", COMMIT))
    tick()
    assert_open(bridge, factory, "not_stopped", error)


def test_with_no_list_a_follower_is_judged_by_the_request_when_the_marker_is_gone(
        starting, factory, receiver):
    """No marker left to say `from`: the transaction directory's request says it."""
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(
        root, phase="rolling_back", to=("0.4.0", "ab" * 20), frm=("0.3.0", COMMIT))),
        session=receiver.session)
    factory.client.fire_connect()
    updatehelper.write_json(str(made["dir"] / "request.json"), {
        "id": "a1b2c3d4e5f6", "enigma2_pid": 1, "from": {"version": "0.3.0", "commit": COMMIT}})
    os.remove(bridge.root / updatehelper.MARKER)
    helper_process(bridge, made["dir"])
    error = str(updatehelper.Fail("not_stopped", previous="0.3.0"))
    # The status file's `from` is a bare number, and a wrong one here: the request decides.
    helper_says(made["dir"], phase="finished", result="failed", reason="not_stopped", error=error,
                target="0.4.0", finished=NOW + 200, record=unstopped_record("no_list"),
                **{"from": "0.2.0"})
    tick()
    assert_open(bridge, factory, "not_stopped", error)


@pytest.mark.parametrize("bad", ["one", [str(os.getpid())], [float(os.getpid())], None])
def test_an_unreadable_list_holds_no_process_by_its_pid(bad, box, factory, receiver):
    """Only whole numbers are pids: a string or a float that looks like this pid is not it."""
    bridge, directory = asking(box, factory, receiver)
    record = unstopped_record("no_list")
    record["unstopped"] = bad
    helper_says(directory, phase="finished", result="failed", reason="not_stopped",
                error=str(updatehelper.Fail("not_stopped", previous="0.3.0")),
                finished=NOW + 200, record=record)
    tick()
    # Not a list - judged by the version, which is `from` here; a list without this pid - open.
    assert not bridge.self_update.stuck


@pytest.mark.parametrize("result, reason, record", [
    ("rolled_back", "not_started",
     {"restore": "done", "stop": "not seen", "init_3": [127, None], "start": "again"}),
    ("failed", "interface_not_started",
     {"restore": "done", "stop": "not seen", "unstopped": [OTHER_PID], "init_3": [1],
      "start": "again", "interface": "not started", "cause": "not_started"}),
])
def test_the_helpers_new_record_fields_change_nothing_here(result, reason, record, starting,
                                                          factory, receiver):
    """`init_3` and `start: again` are the helper's account; the end is read as before."""
    bridge, directory = following(starting, factory, receiver)
    error = str(updatehelper.Fail(reason, previous="0.2.0"))
    helper_says(directory, phase="finished", result=result, reason=reason, error=error,
                finished=NOW + 200, record=record)
    tick()
    updater = bridge.self_update
    assert not updater.closed and not updater.stuck and updater._current is None
    assert refusal(factory.client) == (reason, error)
    assert transaction(factory.client)["result"] == result


def following_from(starting, factory, receiver, frm):
    """A follower of a transaction whose `from` is `frm`; it runs 0.3.0 at `COMMIT`."""
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(
        root, phase="rolling_back", to=("0.4.0", "ab" * 20), frm=frm)),
        session=receiver.session)
    factory.client.fire_connect()
    helper_process(bridge, made["dir"])
    return bridge, made["dir"]


@pytest.mark.parametrize("frm, held", [
    (("0.3.0", COMMIT), False),
    # A release built again under the same number is another build (spike S1).
    (("0.3.0", "ef" * 20), True),
    (("0.2.0", COMMIT), True),
    # A `from` without a commit is judged by its number, as after a power loss.
    (("0.3.0", None), False),
])
def test_with_no_list_the_version_and_the_commit_decide(frm, held, starting, factory, receiver):
    bridge, directory = following_from(starting, factory, receiver, frm)
    helper_says(directory, phase="finished", result="failed", reason="not_stopped",
                error=str(updatehelper.Fail("not_stopped", previous=frm[0])),
                finished=NOW + 200, record=unstopped_record("no_list"), **{"from": frm[0]})
    tick()
    assert bridge.self_update.stuck is held


@pytest.mark.parametrize("case", ["another_transactions_marker", "nothing_says_from"])
def test_with_no_list_and_no_from_of_this_transaction_the_process_is_held(case, starting,
                                                                          factory, receiver):
    """What cannot be shown to run the files put back is not opened over them."""
    bridge, directory = following_from(starting, factory, receiver, ("0.2.0", "ef" * 20))
    marker_path = bridge.root / updatehelper.MARKER
    if case == "another_transactions_marker":
        # Another transaction's `from` is this very build: it says nothing about this one.
        updatehelper.write_json(str(marker_path), {
            "id": "0123456789ab", "phase": "finished",
            "from": {"version": "0.3.0", "commit": COMMIT}})
        status_from = "0.2.0"
    else:
        os.remove(marker_path)
        status_from = None
    updatehelper.write_json(str(directory / "request.json"),
                            {"id": "a1b2c3d4e5f6", "enigma2_pid": 1})
    helper_says(directory, phase="finished", result="failed", reason="not_stopped",
                error=str(updatehelper.Fail("not_stopped", previous="0.2.0")),
                finished=NOW + 200, record=unstopped_record("no_list"), **{"from": status_from})
    tick()
    assert bridge.self_update.stuck


# ------------------------------------------------------------ review round 5 --
#
# The pid list and the end's reason are the helper's account, and its look at /proc can be wrong.
# Before a process that ran through a transaction opens its doors again - or reloads - it asks
# the files themselves: is the build on disk the build this process loaded at its start? When it
# is not, the process runs other code than the files underneath it and is held, whatever the end
# says (TRANSACTION.md section 7).

BUILDINFO = ("plugin", "MQTTBridge", "buildinfo.py")


def disk_build(bridge, commit="ef" * 20, flavour="release", text=None):
    """The build id on disk, as a rollback left it: another build's, or `text` as it is."""
    path = bridge.root.joinpath(*BUILDINFO)
    if text is None:
        text = (f'COMMIT = "{commit}"\nCOMMIT_TIME = 1790300000\nDIRTY = False\n'
                f'FLAVOUR = "{flavour}"\n')
    path.write_text(text, encoding="utf-8")


def assert_held_for_a_restart(bridge, factory, clients):
    updater = bridge.self_update
    assert updater._current is None
    assert updater.closed and updater.stuck
    assert updater.doors_refusal() == other_build_sentence()
    assert updater.repair() == "restart_gui"
    assert bridge._publishers == []
    # No fresh session: it would import the files on disk into a process running another build.
    assert len(factory.clients) == clients


@pytest.mark.parametrize("where", ["marker", "status_only"])
def test_a_same_number_from_without_a_commit_does_not_open_the_to_process(where, starting,
                                                                         factory, receiver):
    """Review round 5 (PA, PA2): `from` is this very number, built without a commit.

    With no list, the number alone said "this process runs `from`", and it opened - but this
    process is `to`, exactly, and the rollback put `from`'s files back under it.
    """
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(
        root, phase="rolling_back", to=("0.3.0", COMMIT), frm=("0.3.0", ""))),
        session=receiver.session)
    factory.client.fire_connect()
    helper_process(bridge, made["dir"])
    if where == "status_only":
        os.remove(bridge.root / updatehelper.MARKER)
        os.remove(made["dir"] / "request.json")
    disk_build(bridge, commit="", flavour="development")
    clients = len(factory.clients)
    helper_says(made["dir"], phase="finished", result="failed", reason="not_stopped",
                error=str(updatehelper.Fail("not_stopped", previous="0.3.0")), target="0.3.0",
                finished=NOW + 200, record=unstopped_record("no_list"), **{"from": "0.3.0"})
    tick()
    assert_held_for_a_restart(bridge, factory, clients)


@pytest.mark.parametrize("result, reason, record", [
    ("failed", "interface_not_started",
     {"restore": "done", "stop": "not seen", "unstopped": [os.getpid()], "interface": "not started",
      "cause": "not_started"}),
    ("rolled_back", "not_started",
     {"restore": "done", "stop": "not seen", "unstopped": [os.getpid()]}),
    # The look failed from the start: the stop was "seen", and there is no list at all (PRB2).
    ("rolled_back", "not_started", {"restore": "done", "stop": "seen"}),
])
def test_a_follower_under_restored_files_is_held_whatever_the_end_says(result, reason, record,
                                                                      starting, factory,
                                                                      receiver):
    """The helper said nothing runs, or a new interface started - and this process still runs."""
    bridge, directory = following(starting, factory, receiver)
    disk_build(bridge)
    clients = len(factory.clients)
    error = str(updatehelper.Fail(reason, previous="0.2.0"))
    helper_says(directory, phase="finished", result=result, reason=reason, error=error,
                finished=NOW + 200, record=record)
    tick()
    assert_held_for_a_restart(bridge, factory, clients)
    # The helper's end is said as it is, and the restart is the repair it adds.
    assert transaction(factory.client)["result"] == result
    assert refusal(factory.client) == (reason, error + "; " + other_build_sentence())
    assert bridge.run_command("reboot", "PRESS", PAGE) == other_build_sentence()
    assert bridge.run_command("restart_gui", "PRESS", PAGE) is None
    assert len(restarts(receiver)) == 1


def test_the_process_that_asked_is_held_when_the_files_are_another_build(box, factory,
                                                                        receiver):
    """A rollback the helper reports as clean, under files that are not this process's build."""
    bridge, directory = asking(box, factory, receiver)
    disk_build(bridge)
    clients = len(factory.clients)
    helper_says(directory, phase="finished", result="rolled_back", reason="not_started",
                error=str(updatehelper.Fail("not_started", previous="0.3.0")),
                finished=NOW + 200, record={"restore": "done", "stop": "seen"})
    tick()
    assert_held_for_a_restart(bridge, factory, clients)


def test_a_restore_that_did_not_complete_under_another_build_says_to_reinstall(starting, factory,
                                                                              receiver):
    """The build check never softens the reinstall to a restart: a mix is not repaired by one."""
    bridge, directory = following(starting, factory, receiver)
    disk_build(bridge)
    error = str(updatehelper.Fail("restore_incomplete", previous="0.2.0",
                                  detail="the settings block"))
    helper_says(directory, phase="finished", result="failed", reason="restore_incomplete",
                error=error, finished=NOW + 200,
                record={"restore": "partial: the settings block", "stop": "seen",
                        "cause": "not_started"})
    tick()
    updater = bridge.self_update
    assert updater.closed and updater.stuck
    assert updater.doors_refusal() == selfupdate.STUCK_PARTIAL and updater.repair() is None


@pytest.mark.parametrize("result, reason, record", [
    ("rolled_back", "not_started", {"restore": "done", "stop": "seen"}),
    ("failed", "opkg_failed", {"restore": "done", "stop": "seen"}),
    ("failed", "not_stopped", unstopped_record("not_listed")),
])
@pytest.mark.parametrize("flavour", ["development", "release"])
def test_the_process_that_asked_opens_on_its_own_build(flavour, result, reason, record, box,
                                                      factory, receiver):
    """The build on disk is the one this process loaded: the end reopens with a fresh session."""
    bridge, directory = asking(box, factory, receiver)
    loaded = dict(BUILD, flavour=flavour)
    bridge._build = loaded
    disk_build(bridge, text=f'COMMIT = "{COMMIT}"\nCOMMIT_TIME = {BUILD["time"]}\n'
                            f'DIRTY = False\nFLAVOUR = "{flavour}"\n')
    clients = len(factory.clients)
    error = str(updatehelper.Fail(reason, previous="0.3.0"))
    helper_says(directory, phase="finished", result=result, reason=reason, error=error,
                finished=NOW + 200, record=record)
    tick()
    assert len(factory.clients) == clients + 1
    updater = bridge.self_update
    assert not updater.closed and not updater.stuck and updater.repair() is None
    factory.client.fire_connect()
    assert refusal(factory.client) == (reason, error)
    assert transaction(factory.client)["result"] == result
    assert bridge._publishers != []


@pytest.mark.parametrize("on_disk, held", [
    # Nobody built this copy and nothing on disk says a build either: nothing to compare, and
    # the pid list and the version decide - this process runs `from` here.
    (None, False),
    # A copy nobody built, and a built release on disk now: another build.
    ("built", True),
])
def test_a_copy_nobody_built_is_judged_by_what_is_on_disk(on_disk, held, box, factory, receiver):
    bridge, directory = asking(box, factory, receiver)
    bridge._build = None
    path = bridge.root.joinpath(*BUILDINFO)
    if on_disk is None:
        path.unlink()
    else:
        disk_build(bridge)
    clients = len(factory.clients)
    error = str(updatehelper.Fail("not_started", previous="0.3.0"))
    helper_says(directory, phase="finished", result="rolled_back", reason="not_started",
                error=error, finished=NOW + 200, record={"restore": "done", "stop": "seen"})
    tick()
    if held:
        assert_held_for_a_restart(bridge, factory, clients)
    else:
        assert len(factory.clients) == clients + 1
        assert not bridge.self_update.closed and not bridge.self_update.stuck


@pytest.mark.parametrize("damage", ["missing", "not literals", "oversized"])
def test_a_build_id_that_can_no_longer_be_read_holds_the_process(damage, box, factory,
                                                                 receiver):
    """This process loaded a build id at its start; its file now says nothing: the files changed.

    Held for the restart, the conservative side: a fresh start runs whatever is on disk.
    """
    bridge, directory = asking(box, factory, receiver)
    path = bridge.root.joinpath(*BUILDINFO)
    if damage == "missing":
        path.unlink()
    elif damage == "not literals":
        path.write_text('COMMIT = "cd" * 20\n', encoding="utf-8")
    else:
        path.write_text("#" * 5000, encoding="utf-8")
    clients = len(factory.clients)
    helper_says(directory, phase="finished", result="rolled_back", reason="not_started",
                error=str(updatehelper.Fail("not_started", previous="0.3.0")),
                finished=NOW + 200, record={"restore": "done", "stop": "seen"})
    tick()
    assert_held_for_a_restart(bridge, factory, clients)


def test_a_launch_that_failed_under_another_build_is_held(box, factory, mono):
    """Every end the process follows is checked - even one that never reached the package."""
    bridge = box()
    directory = accepted(bridge, factory)
    disk_build(bridge)
    clients = len(factory.clients)
    mono.now += selfupdate.LAUNCH_WAIT_SECONDS + 1
    tick()
    assert not directory.exists()
    assert_held_for_a_restart(bridge, factory, clients)


def test_a_fresh_start_on_another_build_is_not_held(starting, factory, receiver):
    """A process that starts loads what is on disk: its own build id is the one to trust."""
    error = str(updatehelper.Fail("not_stopped", previous="0.3.0"))

    def prepare(root):
        marker(root, phase="finished", result="failed", reason="not_stopped", error=error,
               finished=NOW, to=("0.4.0", "ab" * 20), frm=("0.3.0", COMMIT),
               record=unstopped_record("no_list"))
        root.joinpath(*BUILDINFO).write_text('COMMIT = "' + "ab" * 20 + '"\n', encoding="utf-8")

    bridge = starting(prepare=prepare, session=receiver.session)
    assert_open(bridge, factory, "not_stopped", error)


# The follower during `rolling_back` (review round 5, 3c): R2 is putting the previous version's
# files back under it, for as long as three minutes when the interface does not stop.


def test_a_follower_closes_its_doors_while_r2_puts_the_files_back(starting, factory, receiver):
    bridge, directory = following(starting, factory, receiver)
    helper_says(directory, phase="rolling_back")
    tick()
    updater = bridge.self_update
    assert updater.closed and not updater.stuck
    assert bridge._publishers == []
    factory.client.fire_message(ROOT + "/cmd/power", b"standby")
    assert refusal(factory.client) == (None, DOORS)
    # Not the repair: the restart is R2's own, and the doors say only "wait".
    assert bridge.run_command("restart_gui", "PRESS", PAGE) == DOORS
    assert restarts(receiver) == []


def test_a_follower_on_its_own_build_reopens_with_a_fresh_session_at_the_end(starting, factory,
                                                                            receiver):
    """The previous version started while R2 finished: it runs the files on disk, and opens."""
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(
        root, phase="rolling_back", to=("0.4.0", "ab" * 20), frm=("0.3.0", COMMIT))),
        session=receiver.session)
    factory.client.fire_connect()
    helper_process(bridge, made["dir"])
    helper_says(made["dir"], phase="rolling_back", target="0.4.0")
    tick()
    assert bridge.self_update.closed
    clients = len(factory.clients)
    error = str(updatehelper.Fail("not_stopped", previous="0.3.0"))
    helper_says(made["dir"], phase="finished", result="failed", reason="not_stopped", error=error,
                target="0.4.0", finished=NOW + 200, record=unstopped_record("not_listed"))
    tick()
    assert len(factory.clients) == clients + 1
    assert_open(bridge, factory, "not_stopped", error)


def test_a_follower_whose_helper_died_putting_the_files_back_is_held(starting, factory,
                                                                     receiver):
    """A mix, perhaps with the package manager still writing: the reinstall, as for `ours`."""
    bridge, directory = following_the_restart(starting, factory, receiver)
    helper_says(directory, phase="rolling_back")
    tick()
    clients = len(factory.clients)
    helper_process(bridge, directory, running=False)
    tick()
    updater = bridge.self_update
    assert updater._current is None and updater.closed and updater.stuck
    assert updater.doors_refusal() == selfupdate.STUCK_STOPPED and updater.repair() is None
    assert len(factory.clients) == clients
    assert (bridge.root / updatehelper.MARKER).exists()


def test_a_follower_past_its_deadline_while_r2_puts_the_files_back_is_held(starting, factory,
                                                                           receiver):
    """Let go with the doors closed would leave them saying "wait" for ever."""
    bridge, directory = following_the_restart(starting, factory, receiver)
    helper_says(directory, phase="rolling_back")
    tick()
    assert bridge.self_update.closed
    (bridge.root / "proc" / "uptime").write_text(f"{UPTIME + 601:.2f} 1.00\n")
    tick()
    updater = bridge.self_update
    assert updater._current is None and updater.closed and updater.stuck
    assert updater.doors_refusal() == selfupdate.STUCK_STOPPED
    assert "deadline" in refusal(factory.client)[1]


# Test gaps the review's surviving mutants showed (M5, M16, M9).


def test_with_no_list_the_status_files_bare_from_alone_can_open(starting, factory, receiver):
    """Marker gone, a request without `from`: the status file's number is what is left (M5)."""
    bridge, directory = following_from(starting, factory, receiver, ("0.2.0", "ef" * 20))
    os.remove(bridge.root / updatehelper.MARKER)
    error = str(updatehelper.Fail("not_stopped", previous="0.3.0"))
    helper_says(directory, phase="finished", result="failed", reason="not_stopped", error=error,
                finished=NOW + 200, record=unstopped_record("no_list"), **{"from": "0.3.0"})
    tick()
    assert_open(bridge, factory, "not_stopped", error)


def test_a_request_of_another_transaction_says_nothing_about_from(starting, factory, receiver):
    """Only this transaction's request is read; another's `from` is this very build (M16)."""
    bridge, directory = following_from(starting, factory, receiver, ("0.2.0", "ef" * 20))
    os.remove(bridge.root / updatehelper.MARKER)
    updatehelper.write_json(str(directory / "request.json"), {
        "id": "0123456789ab", "enigma2_pid": 1, "from": {"version": "0.3.0", "commit": COMMIT}})
    helper_says(directory, phase="finished", result="failed", reason="not_stopped",
                error=str(updatehelper.Fail("not_stopped", previous="0.2.0")),
                finished=NOW + 200, record=unstopped_record("no_list"), **{"from": "0.2.0"})
    tick()
    assert bridge.self_update.stuck


def test_the_rendered_repair_form_posts_with_its_own_token(starting, factory, receiver,
                                                           monkeypatch):
    """What the browser sends is what the page rendered: its token must be this session's (M9)."""
    from test_webif import confirmation, get, new_session, post

    bridge, directory = following(starting, factory, receiver)
    helper_says(directory, phase="finished", result="failed", reason="not_stopped",
                error=str(updatehelper.Fail("not_stopped", previous="0.2.0")),
                finished=NOW + 200, record=unstopped_record("listed"))
    tick()
    monkeypatch.setattr(webif, "_bridge", lambda: bridge)
    resource = webif.MQTTBridgeWebResource()
    session = new_session()
    _request, body = get(resource, session)
    form = body.decode("utf-8").split("<form method='post' class='action'>", 1)[1]
    form = form.split("</form>", 1)[0]
    fields = dict(re.findall(r"name='([^']+)' value='([^']*)'", form))
    assert fields["action"] == "restart_gui" and fields["csrf"]
    request, body = post(resource, session, fields, csrf=None)
    assert request.response_code == 200
    request, body = post(resource, session, confirmation(body), csrf=None)
    assert request.response_code == 200
    assert len(restarts(receiver)) == 1


def test_an_installed_end_read_under_another_build_is_held(starting, factory, receiver):
    """`installed` is the helper's word too: only a helper that misjudged writes it to a process
    whose build is not on disk, and the files still decide. No softcam collapse follows."""
    bridge, directory = following(starting, factory, receiver)
    collapses = []
    bridge.self_update._schedule_collapse = lambda: collapses.append(True)
    disk_build(bridge)
    clients = len(factory.clients)
    helper_says(directory, phase="finished", result="installed", finished=NOW + 200,
                record={"proof": "marker"})
    tick()
    assert_held_for_a_restart(bridge, factory, clients)
    assert transaction(factory.client)["result"] == "installed"
    assert collapses == []


def test_a_helper_that_stopped_before_the_package_manager_under_another_build_holds(
        box, factory, receiver):
    """Nothing of this transaction touched the files, yet they are another build: a restart."""
    bridge = box()
    directory = accepted(bridge, factory)
    helper_process(bridge, directory)
    helper_says(directory, phase="downloading")
    tick()
    disk_build(bridge)
    clients = len(factory.clients)
    helper_process(bridge, directory, running=False)
    tick()
    assert_held_for_a_restart(bridge, factory, clients)
    assert refusal(factory.client)[0] == "interrupted"


def test_a_rebuild_of_the_same_commit_from_a_changed_tree_is_another_build(box, factory,
                                                                          receiver):
    """The whole build id is compared, not the commit alone: `dirty` says the trees differ."""
    bridge, directory = asking(box, factory, receiver)
    disk_build(bridge, text=f'COMMIT = "{COMMIT}"\nCOMMIT_TIME = {BUILD["time"]}\n'
                            f'DIRTY = True\nFLAVOUR = "{BUILD["flavour"]}"\n')
    clients = len(factory.clients)
    helper_says(directory, phase="finished", result="rolled_back", reason="not_started",
                error=str(updatehelper.Fail("not_started", previous="0.3.0")),
                finished=NOW + 200, record={"restore": "done", "stop": "seen"})
    tick()
    assert_held_for_a_restart(bridge, factory, clients)


def test_a_followers_helper_dead_after_the_package_manager_under_another_build_says_reinstall(
        starting, factory, receiver):
    """Before `rolling_back` a follower runs the files it started on - unless they are another
    build now: then an orphaned package manager may be writing them, and a restart is no repair."""
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(root, phase="proving")),
                      session=receiver.session)
    factory.client.fire_connect()
    helper_process(bridge, made["dir"])
    helper_says(made["dir"], phase="proving")
    tick()
    assert not bridge.self_update.closed
    disk_build(bridge)
    helper_process(bridge, made["dir"], running=False)
    tick()
    updater = bridge.self_update
    assert updater.closed and updater.stuck
    assert updater.doors_refusal() == selfupdate.STUCK_STOPPED and updater.repair() is None


# ------------------------------------------------------------ review round 6 --
#
# A hold by the build rule alone is not a rollback: after a failed launch, a withdrawn update or an
# `installed` end nothing was undone, so it has its own sentence. A follower that began at
# `rolling_back` - the process R2's own start brought up - runs the files on disk, and a helper
# that stops, or goes missing, meanwhile is judged by the build rule, not held for a reinstall.

OTHER_BUILD_HOUSEHOLD = ("The receiver's user interface is running a different version of the "
                         "plugin than the one now installed. Please restart the user interface.")


def other_build_sentence():
    return getattr(selfupdate, "STUCK_OTHER_BUILD", "(no such sentence)")


def catalogue(language):
    return (PACKAGE_DIR / "locale" / language / "LC_MESSAGES" / "MQTTBridge.po").read_text(
        encoding="utf-8")


def translation(language, msgid):
    text = catalogue(language)
    found = re.search(r'msgid "' + re.escape(msgid) + r'"\nmsgstr "([^"]*)"', text)
    return found.group(1) if found else None


def held_by_a_launch_that_failed(box, starting, factory, receiver, mono):
    bridge = box()
    accepted(bridge, factory)
    disk_build(bridge)
    mono.now += selfupdate.LAUNCH_WAIT_SECONDS + 1
    return bridge


def held_by_a_withdrawn_update(box, starting, factory, receiver, mono):
    bridge, directory = asking(box, factory, receiver)
    disk_build(bridge)
    helper_says(directory, phase="finished", result="withdrawn_before_restart", reason="question",
                error=str(updatehelper.Fail("question")), finished=NOW + 5,
                record={"restore": "done"})
    return bridge


def held_by_an_installed_end(box, starting, factory, receiver, mono):
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(root, phase="proving")),
                      session=receiver.session)
    factory.client.fire_connect()
    helper_process(bridge, made["dir"])
    disk_build(bridge)
    helper_says(made["dir"], phase="finished", result="installed", finished=NOW + 200,
                record={"proof": "marker"})
    return bridge


@pytest.mark.parametrize("arrange", [held_by_a_launch_that_failed, held_by_a_withdrawn_update,
                                     held_by_an_installed_end])
def test_a_hold_by_the_build_alone_does_not_say_an_update_was_rolled_back(arrange, box, starting,
                                                                          factory, receiver,
                                                                          mono):
    bridge = arrange(box, starting, factory, receiver, mono)
    tick()
    updater = bridge.self_update
    assert updater.closed and updater.stuck
    sentence = other_build_sentence()
    assert updater.doors_refusal() == sentence
    assert "restart the receiver's interface" in sentence
    assert "rolled back" not in sentence and "install the plugin" not in sentence
    assert "rolled back" not in refusal(factory.client)[1]
    household = selfupdate.household_doors(updater)
    assert household == OTHER_BUILD_HOUSEHOLD
    # The repair is the same restart, offered on the page and over MQTT.
    assert updater.repair() == "restart_gui"
    assert bridge.run_command("restart_gui", "PRESS", PAGE) is None
    assert len(restarts(receiver)) == 1


def test_the_other_build_sentence_is_said_in_polish_and_german():
    """Natural, in each catalogue's own words for the receiver and its user interface."""
    pl = translation("pl", OTHER_BUILD_HOUSEHOLD)
    de = translation("de", OTHER_BUILD_HOUSEHOLD)
    assert pl and de
    assert "interfejs" in pl and "wycofan" not in pl and "Uruchom ponownie interfejs." in pl
    assert "Benutzeroberfl\u00e4che" in de and "r\u00fcckg\u00e4ngig" not in de
    assert de.endswith("Bitte starten Sie die Benutzeroberfl\u00e4che neu.")


def test_a_rollback_under_an_interface_r2_could_not_stop_still_says_it_was_rolled_back(
        box, factory, receiver):
    """Where something was undone - `not_stopped`, this process listed - the rollback sentence."""
    bridge, directory = asking(box, factory, receiver)
    helper_says(directory, phase="finished", result="failed", reason="not_stopped",
                error=str(updatehelper.Fail("not_stopped", previous="0.3.0")),
                finished=NOW + 200, record=unstopped_record("listed"))
    tick()
    assert bridge.self_update.doors_refusal() == selfupdate.STUCK_UNSTOPPED
    assert "rolled back" in selfupdate.STUCK_UNSTOPPED


@pytest.mark.parametrize("field", ["flavour", "time"])
def test_the_build_ids_flavour_and_time_are_compared_too(field, box, factory, receiver):
    bridge, directory = asking(box, factory, receiver)
    flavour = "release" if field == "flavour" else BUILD["flavour"]
    when = BUILD["time"] + (60 if field == "time" else 0)
    disk_build(bridge, text=f'COMMIT = "{COMMIT}"\nCOMMIT_TIME = {when}\nDIRTY = False\n'
                            f'FLAVOUR = "{flavour}"\n')
    clients = len(factory.clients)
    helper_says(directory, phase="finished", result="rolled_back", reason="not_started",
                error=str(updatehelper.Fail("not_started", previous="0.3.0")),
                finished=NOW + 200, record={"restore": "done", "stop": "seen"})
    tick()
    assert_held_for_a_restart(bridge, factory, clients)


def following_the_restart(starting, factory, receiver):
    """The new build that the forward restart started (`proving`), then R2 putting files back."""
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(root, phase="proving")),
                      session=receiver.session)
    factory.client.fire_connect()
    helper_process(bridge, made["dir"])
    return bridge, made["dir"]


def test_the_deadline_is_asked_after_the_rolling_back_close(starting, factory, receiver):
    """Past the deadline at the very poll that first sees `rolling_back`: closed, then judged."""
    bridge, directory = following_the_restart(starting, factory, receiver)
    helper_says(directory, phase="rolling_back")
    (bridge.root / "proc" / "uptime").write_text(f"{UPTIME + 601:.2f} 1.00\n")
    tick()
    updater = bridge.self_update
    assert updater._current is None and updater.closed and updater.stuck
    assert updater.doors_refusal() == selfupdate.STUCK_STOPPED


def began_at_rolling_back(starting, factory, receiver):
    """The previous version, started by R2's own `init 3`: it runs the files on disk."""
    made = {}
    bridge = starting(prepare=lambda root: made.update(dir=marker(
        root, phase="rolling_back", to=("0.4.0", "ab" * 20), frm=("0.3.0", COMMIT))),
        session=receiver.session)
    factory.client.fire_connect()
    helper_process(bridge, made["dir"])
    helper_says(made["dir"], phase="rolling_back", target="0.4.0")
    tick()
    assert bridge.self_update.closed
    return bridge, made["dir"]


def assert_reopened(bridge, factory, clients):
    updater = bridge.self_update
    assert updater._current is None
    assert not updater.closed and not updater.stuck
    assert len(factory.clients) == clients + 1
    assert bridge._publishers != []


@pytest.mark.parametrize("how", ["killed", "deadline", "records_gone", "records_gone_deadline"])
def test_a_follower_that_began_at_rolling_back_on_its_own_build_opens(how, starting, factory,
                                                                     receiver):
    """R6, R7, R12: the helper stops, hangs past its deadline, or leaves nothing to read."""
    bridge, directory = began_at_rolling_back(starting, factory, receiver)
    clients = len(factory.clients)
    if how == "killed":
        helper_process(bridge, directory, running=False)
    if how in ("deadline", "records_gone_deadline"):
        (bridge.root / "proc" / "uptime").write_text(f"{UPTIME + 601:.2f} 1.00\n")
    if how.startswith("records_gone"):
        os.remove(bridge.root / updatehelper.MARKER)
        if how == "records_gone":
            shutil.rmtree(directory)
        else:
            os.remove(directory / "status.json")
    tick()
    assert_reopened(bridge, factory, clients)
    factory.client.fire_connect()
    reason, error = refusal(factory.client)
    assert reason == "interrupted"
    # It runs the files on disk: nothing asks the household to restart or reinstall.
    assert "install the plugin" not in error and "restart the receiver" not in error


def test_a_follower_that_began_at_rolling_back_under_another_build_says_reinstall(
        starting, factory, receiver):
    bridge, directory = began_at_rolling_back(starting, factory, receiver)
    disk_build(bridge)
    helper_process(bridge, directory, running=False)
    tick()
    updater = bridge.self_update
    assert updater.closed and updater.stuck
    assert updater.doors_refusal() == selfupdate.STUCK_STOPPED


@pytest.mark.parametrize("how", ["records_gone", "records_gone_deadline"])
def test_a_closed_follower_that_loses_every_record_is_not_left_saying_wait(how, starting,
                                                                         factory, receiver):
    """The build that the forward restart started, under R2's restore: a mix, so the reinstall."""
    bridge, directory = following_the_restart(starting, factory, receiver)
    helper_says(directory, phase="rolling_back")
    tick()
    assert bridge.self_update.closed
    os.remove(bridge.root / updatehelper.MARKER)
    if how == "records_gone":
        shutil.rmtree(directory)
    else:
        os.remove(directory / "status.json")
        (bridge.root / "proc" / "uptime").write_text(f"{UPTIME + 601:.2f} 1.00\n")
    tick()
    updater = bridge.self_update
    assert updater._current is None and updater.closed and updater.stuck
    assert updater.doors_refusal() == selfupdate.STUCK_STOPPED
    assert updater.doors_refusal() != DOORS
