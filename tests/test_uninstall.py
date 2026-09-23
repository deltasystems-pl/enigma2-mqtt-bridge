"""`cmd/uninstall`: the refusals, the order on the way out, and the way back when it fails.

The broker here is `FakeMQTTClient`, whose QoS 1 publishes stay unacknowledged
until a test calls `acknowledge()` — which is exactly the question the teardown
asks before it removes anything. Timers are `conftest.MainLoop`'s, so „the next
turn of the main loop" and „100 ms later" are things a test does by hand, and
opkg is `conftest.ConsoleAppContainer`, which runs nothing and remembers the
command line.

The opkg tree the capability reads is built under a temporary root: the host
running the suite is never asked whether it has an opkg.
"""

import builtins
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from Components.config import configfile
from conftest import ConsoleAppContainer, MainLoop, RecordTimerEntry, settle
from Screens.Standby import TryQuitMainloop

from MQTTBridge import uninstall
from MQTTBridge.mqttclient import MAX_QUEUED_MESSAGES
from MQTTBridge.origin import MQTT, PAGE
from MQTTBridge.uninstall import Uninstaller

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
INFO = ROOT + "/info"
LAST_ERROR = ROOT + "/last_error"
AVAILABILITY = ROOT + "/availability"
COMMAND = ROOT + "/cmd/uninstall"
ANNOUNCEMENT = "enigma2mqtt/discovery/" + NODE + "/config"
PACKAGE_DIR = Path(uninstall.__file__).resolve().parent
TESTS = Path(__file__).resolve().parent


# ---------------------------------------------------------------------- helpers --


@pytest.fixture
def opkg_tree(tmp_path, monkeypatch):
    """opkg's files as the receiver has them, under a root of the test's own."""

    def build(opkg=True, control=True, listed=True, info_dir="/var/lib/opkg/info"):
        root = tmp_path / "box"
        (root / "usr" / "bin").mkdir(parents=True, exist_ok=True)
        if opkg:
            binary = root / "usr" / "bin" / "opkg"
            binary.write_text("#!/bin/sh\n", encoding="utf-8")
            binary.chmod(0o755)
        conf = root / "etc" / "opkg"
        conf.mkdir(parents=True, exist_ok=True)
        (conf / "opkg.conf").write_text(
            "option lists_dir   /var/lib/opkg/lists\n"
            "option info_dir    " + info_dir + "\n"
            "option status_file /var/lib/opkg/status\n",
            encoding="utf-8",
        )
        info = root / info_dir.strip("/")
        info.mkdir(parents=True, exist_ok=True)
        if control:
            (info / (uninstall.PACKAGE + ".control")).write_text(
                "Package: " + uninstall.PACKAGE + "\n", encoding="utf-8"
            )
        # opkg 0.6 writes `path<TAB>mode`, measured on the receiver.
        named = str(PACKAGE_DIR / ("plugin.py" if listed else "bridge.py"))
        (info / (uninstall.PACKAGE + ".list")).write_text(
            "/usr/lib/enigma2/python/Plugins/Extensions/WebInterface/WebChilds/External/"
            "MQTTBridge.py\t0100644\n" + named + "\t0100644\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(Uninstaller, "root", str(root))
        return root

    return build


@pytest.fixture
def box(make_bridge, factory, settings, receiver, opkg_tree):
    """A connected bridge on a working receiver, packaged by opkg unless told otherwise."""

    def build(allowed=True, packaged=True):
        if packaged:
            opkg_tree()
        settings.host.value = "10.0.0.5"
        settings.node_id.value = NODE
        settings.friendly_name.value = "Living room receiver"
        settings.uninstall_allowed.value = allowed
        bridge = make_bridge(session=receiver.session)
        bridge.start()
        factory.client.fire_connect()
        return settle(bridge)

    return build


def send(factory, payload=NODE, retain=False):
    data = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
    factory.client.fire_message(COMMAND, data, retain=retain)


def error(client):
    entry = client.last(LAST_ERROR)
    return None if entry is None or entry.text == "" else entry.json()["error"]


def teardown(client, since):
    """What the removal published: everything after index `since`, at QoS 1."""
    return [entry for entry in client.published[since:] if entry.qos == 1]


def run_to_opkg(bridge, factory):
    """Accept, retract, acknowledge: up to the moment opkg has been started."""
    client = factory.client
    send(factory)
    MainLoop.advance(0)
    client.acknowledge()
    MainLoop.advance(uninstall.POLL_MILLISECONDS)
    return client


def opkg():
    containers = [c for c in ConsoleAppContainer.instances
                  if any("opkg" in str(command) for command in c.commands)]
    return containers[-1] if containers else None


# ------------------------------------------------------------------- refusals --


def test_refused_by_default_and_nothing_else_happens(box, factory, receiver):
    """The permission is off on every receiver as shipped; this is what they all do."""
    bridge = box(allowed=False)
    client = factory.client
    before = len(client.published)

    send(factory)
    MainLoop.advance(1000)

    assert error(client) == "uninstall is switched off in the plugin's settings"
    assert [entry.topic for entry in client.published[before:]] == [LAST_ERROR]
    assert opkg() is None
    assert receiver.session.opened == []
    assert bridge.uninstaller.phase is None
    assert not bridge.uninstaller.closed


@pytest.mark.parametrize(
    "payload",
    [b"", b"PRESS", b"vuuno4kse_005302", b"VUUNO4KSE_005301", b"Vuuno4kse_005301",
     b'{"node_id": "vuuno4kse_005301"}'],
    ids=["missing", "press", "another node", "upper case", "capitalised", "json"],
)
def test_refused_unless_the_payload_is_this_node_id(box, factory, receiver, payload):
    box()
    client = factory.client
    before = len(client.published)

    send(factory, payload)
    MainLoop.advance(1000)

    assert error(client) == "the payload must be this receiver's node id"
    assert [entry.topic for entry in client.published[before:]] == [LAST_ERROR]
    assert opkg() is None
    assert receiver.session.opened == []


def test_surrounding_whitespace_is_not_a_different_node(box, factory):
    bridge = box()
    send(factory, "  " + NODE + "\n")
    assert error(factory.client) is None
    assert bridge.uninstaller.phase == "scheduled"


def test_refused_without_the_capability(box, factory, receiver):
    bridge = box(packaged=False)
    assert "uninstall" not in bridge.capabilities()
    client = factory.client
    before = len(client.published)

    send(factory)
    MainLoop.advance(1000)

    assert error(client) == (
        "this plugin was not installed by the package manager, so it cannot remove itself"
    )
    assert [entry.topic for entry in client.published[before:]] == [LAST_ERROR]
    assert opkg() is None
    assert receiver.session.opened == []


@pytest.mark.parametrize("case", ["recording", "due", "unreadable"])
def test_refused_by_the_recording_guard(box, factory, receiver, monkeypatch, case):
    import time

    bridge = box()
    if case == "recording":
        receiver.add_timer(state=RecordTimerEntry.StateRunning)
        expected = "the receiver is recording"
    elif case == "due":
        monkeypatch.setattr(
            receiver.nav.RecordTimer, "getNextRecordingTime", lambda: int(time.time()) + 300
        )
        expected = "a recording starts in "
    else:
        monkeypatch.setattr(receiver.nav.RecordTimer, "isRecording", lambda: 1 / 0)
        expected = "this image will not say whether it is recording"
    client = factory.client
    before = len(client.published)

    send(factory)
    MainLoop.advance(1000)

    assert error(client).startswith(expected)
    assert [entry.topic for entry in client.published[before:]] == [LAST_ERROR]
    assert opkg() is None
    assert receiver.session.opened == []
    assert bridge.uninstaller.phase is None


def test_refused_while_an_uninstall_is_running(box, factory, receiver):
    bridge = box()
    send(factory)
    assert bridge.uninstaller.phase == "scheduled"

    send(factory)

    assert error(factory.client) == "an uninstall is already running"


def test_refused_when_the_image_cannot_restart(box, factory, monkeypatch):
    import Screens.Standby as standby

    box()
    monkeypatch.delattr(standby, "TryQuitMainloop")
    send(factory)
    assert error(factory.client) == "this image has no TryQuitMainloop"
    assert opkg() is None


def test_a_retained_uninstall_is_discarded_and_remembered(box, factory, receiver, plugin_log):
    bridge = box()
    client = factory.client
    before = len(client.published)

    send(factory, retain=True)
    MainLoop.advance(1000)

    assert client.published[before:] == []
    assert bridge.uninstaller.phase is None
    assert COMMAND in bridge.discarded_retained_commands()
    assert "discarding a RETAINED cmd/uninstall" in plugin_log()


# ------------------------------------------------------- the permission itself --


def test_the_permission_is_published_read_only(connected_bridge, factory, settings):
    assert factory.client.last(INFO).json()["settings"]["uninstall_allowed"] is False
    assert "uninstall_allowed" not in connected_bridge.remote_settings()
    factory.client.fire_message(
        ROOT + "/cmd/config",
        b'{"publish_keys":true,"screenshot":"on_zap","screenshot_interval":60,'
        b'"uninstall_allowed":true}',
    )
    assert error(factory.client) == "the config object contains unknown settings"
    assert settings.uninstall_allowed.value is False


def test_the_permission_is_echoed_when_granted(make_bridge, factory, settings):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.uninstall_allowed.value = True
    make_bridge().start()
    factory.client.fire_connect()
    assert factory.client.last(INFO).json()["settings"]["uninstall_allowed"] is True


def test_the_provisioning_file_can_grant_it(tmp_path, settings):
    import json

    from MQTTBridge import config as settings_module

    path = tmp_path / "mqttbridge.json"
    path.write_text(json.dumps({"uninstall_allowed": "yes"}), encoding="utf-8")
    assert settings_module.import_provisioning(str(path)) == ["uninstall_allowed"]
    assert settings.uninstall_allowed.value is True


# ---------------------------------------------------------------- capability --


def test_claimed_where_opkg_installed_this_plugin(box):
    bridge = box()
    assert "uninstall" in bridge.capabilities()


def test_announced_in_info(box, factory):
    box()
    assert "uninstall" in factory.client.last(INFO).json()["capabilities"]


@pytest.mark.parametrize(
    ("tree", "why"),
    [
        ({"opkg": False}, "no executable"),
        ({"control": False}, "does not know"),
        ({"listed": False}, "does not name"),
    ],
    ids=["no opkg", "no control", "list names another file"],
)
def test_not_claimed_otherwise(opkg_tree, tree, why):
    root = opkg_tree(**tree)
    claimed, reason = uninstall.installed_by_package_manager(str(root))
    assert claimed is False
    assert why in reason


def test_the_info_directory_is_the_one_opkg_is_configured_with(opkg_tree):
    root = opkg_tree(info_dir="/usr/lib/opkg/info")
    assert uninstall.info_directory(str(root)) == str(root / "usr" / "lib" / "opkg" / "info")
    assert uninstall.installed_by_package_manager(str(root)) == (True, None)


def test_no_discovery_component_even_when_everything_allows_it(box, factory, settings):
    """A core MQTT button has no confirmation, and this is the one command that needs one."""
    settings.ha_mode.value = "discovery"
    box()
    discovery = [
        entry for entry in factory.client.published if entry.topic.startswith("homeassistant/")
    ]
    assert discovery
    for entry in discovery:
        assert "uninstall" not in entry.text, entry.topic


# --------------------------------------------------------------------- order --


def test_the_order_on_the_way_out(box, factory, receiver, monkeypatch):
    bridge = box()
    client = factory.client
    send(factory, retain=False)
    client.fire_message(ROOT + "/cmd/volume", b"30", retain=True)
    owned = set(bridge.state.retained_topics)
    assert owned and AVAILABILITY in owned
    before = len(client.published)

    seen = []
    original = client.publish

    def watching(topic, payload=None, qos=0, retain=False):
        if qos == 1 and not seen:
            # The first retraction: the doors are already shut.
            seen.append((bridge.uninstaller.closed, list(bridge._publishers)))
        return original(topic, payload, qos=qos, retain=retain)

    monkeypatch.setattr(client, "publish", watching)
    MainLoop.advance(0)

    assert seen == [(True, [])]
    published = teardown(client, before)
    assert all(entry.retain for entry in published)
    retracted = {entry.topic for entry in published if entry.text == ""}
    assert retracted == owned | {ROOT + "/cmd/volume"}
    assert (published[-1].topic, published[-1].text) == (AVAILABILITY, "offline")
    assert client.published[-1] is published[-1]

    # Not a step further until every acknowledgement is in.
    client.acknowledge(len(published) - 1)
    MainLoop.advance(uninstall.POLL_MILLISECONDS * 3)
    assert opkg() is None
    assert not client.client_disconnected()

    client.acknowledge()
    MainLoop.advance(uninstall.POLL_MILLISECONDS)

    assert client.client_disconnected()
    assert bridge.client is None
    assert opkg().commands == [uninstall.COMMAND_LINE]
    with open(bridge.state.path, encoding="utf-8") as handle:
        import json

        saved = json.load(handle)
    assert saved["retained_topics"] == []
    assert saved["discovery_components"] == []
    assert saved["epg_grid_slugs"] == []
    assert receiver.session.opened == []

    opkg().finish(0)
    assert receiver.session.opened == [(TryQuitMainloop, (3,))]
    assert bridge.uninstaller.phase == "done"
    # Nothing at all after the final `offline`.
    assert client.published[-1] is published[-1]


def test_the_command_clears_last_error_before_the_doors_close(box, factory):
    box()
    send(factory, "wrong")
    assert error(factory.client)
    send(factory)
    assert factory.client.last(LAST_ERROR).text == ""


def test_a_publisher_that_fires_after_the_retraction_publishes_nothing(box, factory):
    bridge = box()
    client = factory.client
    power = bridge.publisher("power")
    volume = bridge.publisher("volume")
    send(factory)
    MainLoop.advance(0)
    after = len(client.published)

    # A timer or an event that was already on its way when the hooks came off.
    power._publish_now()
    volume._publish_now()
    bridge.publish_state("volume", {"level": 99, "muted": False})
    bridge.retract(ROOT + "/cmd/anything")

    assert len(client.published) == after


def test_commands_are_not_dispatched_once_the_doors_are_closed(box, factory, receiver):
    bridge = box()
    client = factory.client
    send(factory)
    MainLoop.advance(0)
    after = len(client.published)

    client.fire_message(ROOT + "/cmd/restart_gui", b"PRESS")
    client.fire_message(ROOT + "/cmd/reset", b"PRESS")
    refusal = bridge.run_command("screenshot", "", PAGE)
    settings_refusal = bridge.apply_settings({"log_level": "debug"})

    assert refusal == "an uninstall is already running"
    assert settings_refusal == "an uninstall is already running"
    assert len(client.published) == after
    assert receiver.session.opened == []


def test_more_topics_than_the_queue_holds_go_out_in_batches(box, factory):
    bridge = box()
    client = factory.client
    # Twice paho's bound, not twice the batch: a test sized by the constant it
    # checks could not tell a batch that is too large.
    for number in range(MAX_QUEUED_MESSAGES * 2 + 7):
        bridge.state.remember(ROOT + "/extra/" + str(number))
    total = len(bridge.state.retained_topics) + 1
    before = len(client.published)
    send(factory)
    MainLoop.advance(0)

    rounds = 0
    while opkg() is None:
        outstanding = [info for info in client.infos if not info.acknowledged]
        assert len(outstanding) < MAX_QUEUED_MESSAGES
        client.acknowledge()
        MainLoop.advance(uninstall.POLL_MILLISECONDS)
        rounds += 1
        assert rounds < 10

    published = teardown(client, before)
    assert len(published) == total
    assert (published[-1].topic, published[-1].text) == (AVAILABILITY, "offline")
    assert rounds >= 3


def test_nothing_is_published_at_the_shutdown_afterwards(box, factory):
    bridge = box()
    client = run_to_opkg(bridge, factory)
    opkg().finish(0)
    count = len(client.published)

    bridge.stop()

    assert len(client.published) == count
    assert all(len(made.published) == 0 for made in factory.clients[1:])


def test_the_teardown_never_waits_on_the_main_loop(box, factory):
    bridge = box()
    client = run_to_opkg(bridge, factory)
    opkg().finish(0)
    assert all(info.waited is None for info in client.infos)


def test_the_opkg_command_line_is_exactly_remove_and_nothing_else(box, factory):
    bridge = box()
    run_to_opkg(bridge, factory)
    line = opkg().commands[0]
    assert line == (
        "MQTTBRIDGE_UNINSTALL=1 /usr/bin/opkg remove enigma2-plugin-extensions-mqttbridge"
    )
    assert "--autoremove" not in line and "--force" not in line


def test_nothing_on_the_path_can_block_or_spawn_behind_enigma2s_back():
    source = Path(uninstall.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    for forbidden in ("subprocess", "os.system", "os.popen", "wait_for_publish",
                      "time.sleep", "Popen"):
        assert forbidden not in code, forbidden


def test_the_settings_file_is_not_written_by_the_teardown(box, factory, settings, monkeypatch):
    bridge = box()
    writes = configfile.save_calls
    element_saves = {
        name: getattr(settings, name).save_calls for name in ("uninstall_allowed", "node_id")
    }
    opened = []
    real_open = builtins.open

    def spy(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            opened.append(str(file))
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy)
    run_to_opkg(bridge, factory)
    opkg().finish(0)
    monkeypatch.setattr(builtins, "open", real_open)

    assert configfile.save_calls == writes
    assert {
        name: getattr(settings, name).save_calls for name in element_saves
    } == element_saves
    assert [path for path in opened if "settings" in path] == []


def test_opkg_output_goes_to_the_log(box, factory, plugin_log):
    bridge = box()
    run_to_opkg(bridge, factory)
    for listener in opkg().stdoutAvail:
        listener(b"Removing package enigma2-plugin-extensions-mqttbridge from root...\n")
    opkg().finish(0)
    log = plugin_log()
    assert "opkg: Removing package enigma2-plugin-extensions-mqttbridge" in log
    for step in range(1, 9):
        assert "uninstall step " + str(step) in log, step


# ------------------------------------------------------------------ failures --


def _fresh_session(factory, old):
    """The failure path opened a new session; connect it and return it."""
    assert factory.client is not old
    factory.client.fire_connect()
    return factory.client


def _put_back(client, bridge):
    assert client.last(AVAILABILITY).text == "online"
    assert client.last(INFO) is not None
    assert client.last(ANNOUNCEMENT).text != ""
    assert bridge.state.knows(INFO)
    assert not bridge.uninstaller.closed
    assert bridge.uninstaller.phase is None
    assert bridge.publisher("power") is not None


def test_a_missing_acknowledgement_past_the_bound_removes_nothing(box, factory, receiver):
    bridge = box()
    now = [1000.0]
    bridge.uninstaller._clock = lambda: now[0]
    old = factory.client
    send(factory)
    MainLoop.advance(0)
    old.acknowledge(3)
    now[0] += uninstall.ACK_TIMEOUT_SECONDS - 1
    MainLoop.advance(uninstall.POLL_MILLISECONDS)
    assert factory.client is old

    now[0] += 2
    MainLoop.advance(uninstall.POLL_MILLISECONDS)

    assert opkg() is None
    assert receiver.session.opened == []
    client = _fresh_session(factory, old)
    _put_back(client, bridge)
    assert "did not acknowledge" in error(client)
    assert "the retraction" in error(client)
    assert client.last(LAST_ERROR).json()["cmd"] == "uninstall"


def test_a_dropped_connection_mid_sequence_removes_nothing(box, factory, receiver):
    bridge = box()
    old = factory.client
    send(factory)
    MainLoop.advance(0)
    old.fire_disconnect(7)
    MainLoop.advance(uninstall.POLL_MILLISECONDS)

    assert opkg() is None
    assert receiver.session.opened == []
    client = _fresh_session(factory, old)
    _put_back(client, bridge)
    assert "connection to the broker dropped" in error(client)


def test_a_refused_publish_removes_nothing(box, factory, receiver):
    bridge = box()
    old = factory.client
    old.publish_rc = 4  # paho's MQTT_ERR_NO_CONN
    send(factory)
    MainLoop.advance(0)
    old.publish_rc = 0

    assert opkg() is None
    client = _fresh_session(factory, old)
    _put_back(client, bridge)
    assert "refused a publish" in error(client)


def test_opkg_refusing_the_removal_puts_everything_back(box, factory, receiver):
    bridge = box()
    old = run_to_opkg(bridge, factory)
    for listener in opkg().stdoutAvail:
        listener(b"Collected errors:\n * opkg_conf_load: Could not lock /run/opkg.lock: "
                 b"Resource temporarily unavailable.\n")
    opkg().finish(255)

    assert receiver.session.opened == []
    client = _fresh_session(factory, old)
    _put_back(client, bridge)
    sentence = error(client)
    assert sentence.startswith("the uninstall stopped at the package removal: opkg exited "
                               "with status 255")
    assert "Could not lock /run/opkg.lock" in sentence
    assert sentence.endswith("the plugin is still installed")
    # And it can be asked again.
    send(factory)
    assert bridge.uninstaller.phase == "scheduled"


def test_opkg_that_cannot_start_puts_everything_back(box, factory, monkeypatch):
    bridge = box()
    monkeypatch.setattr(ConsoleAppContainer, "execute", lambda self, command: 1)
    old = factory.client
    send(factory)
    MainLoop.advance(0)
    old.acknowledge()
    MainLoop.advance(uninstall.POLL_MILLISECONDS)

    client = _fresh_session(factory, old)
    _put_back(client, bridge)
    assert "opkg could not be started" in error(client)


def test_a_shutdown_in_the_middle_abandons_the_removal(box, factory, receiver):
    bridge = box()
    send(factory)
    MainLoop.advance(0)
    bridge.stop()
    factory.client.acknowledge()
    MainLoop.advance(uninstall.POLL_MILLISECONDS * 5)

    assert opkg() is None
    assert receiver.session.opened == []
    assert bridge.uninstaller.phase == "abandoned"


# -------------------------------------------------------------- the page seam --


def test_the_page_runs_it_without_the_permission(box, factory):
    bridge = box(allowed=False)
    assert bridge.run_command("uninstall", NODE, PAGE) is None
    assert bridge.uninstaller.phase == "scheduled"


def test_the_page_bypass_is_not_inherited(box, factory):
    bridge = box(allowed=False)
    assert bridge.uninstaller.request(NODE, origin=PAGE) is None
    bridge.uninstaller.phase = None
    assert bridge.uninstaller.request(NODE, origin=MQTT) == uninstall.PERMISSION


def test_the_page_is_still_held_to_the_guards(box, receiver):
    bridge = box(allowed=False)
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    assert bridge.run_command("uninstall", NODE, PAGE) == "the receiver is recording"


# ------------------------------------------------- no first import on the path --


CHILD = textwrap.dedent(
    """
    import importlib, os, shutil, sys
    tests, work = sys.argv[1], sys.argv[2]
    sys.path.insert(0, tests)
    import conftest
    assert "MQTTBridge" not in sys.modules
    sys.path[:] = [p for p in sys.path if os.path.abspath(p) != os.path.abspath(str(conftest.SRC))]
    sys.path.insert(0, os.path.join(work, "pkg"))
    import MQTTBridge.bridge
    from MQTTBridge.discovery import StateStore
    from MQTTBridge import config as settings_module, uninstall
    package = os.path.dirname(MQTTBridge.__file__)
    assert package.startswith(work), package

    root = os.path.join(work, "box")
    info = os.path.join(root, "var", "lib", "opkg", "info")
    os.makedirs(info)
    os.makedirs(os.path.join(root, "usr", "bin"))
    binary = os.path.join(root, "usr", "bin", "opkg")
    open(binary, "w").close()
    os.chmod(binary, 0o755)
    open(os.path.join(info, uninstall.PACKAGE + ".control"), "w").close()
    with open(os.path.join(info, uninstall.PACKAGE + ".list"), "w") as handle:
        handle.write(os.path.join(package, "plugin.py") + "\\t0100644\\n")
    uninstall.Uninstaller.root = root

    settings = settings_module.settings
    settings.host.value = "10.0.0.5"
    settings.node_id.value = "vuuno4kse_005301"
    settings.uninstall_allowed.value = True
    receiver = conftest.Receiver()
    factory = conftest.ClientFactory()
    log = os.path.join(work, "mqttbridge.log")
    bridge = MQTTBridge.bridge.Bridge(
        session=receiver.session, client_factory=factory, dispatcher=conftest.callFromThread,
        state_store=StateStore(path=os.path.join(work, "state.json")),
        provisioning_path=os.path.join(work, "absent.json"), log_path=log, loop_monitor=None,
    )
    bridge.start()
    client = factory.client
    client.fire_connect()
    assert "uninstall" in bridge.capabilities(), bridge.capabilities()

    # What opkg does to the files; the running process keeps what it imported.
    os.rename(package, os.path.join(work, "pkg", "gone"))
    importlib.invalidate_caches()
    try:
        importlib.import_module("MQTTBridge.setup")
    except ImportError:
        pass
    else:
        raise AssertionError("the package directory is still importable")

    client.fire_message("enigma2/vuuno4kse_005301/cmd/uninstall", b"vuuno4kse_005301")
    conftest.MainLoop.advance(0)
    client.acknowledge()
    conftest.MainLoop.advance(uninstall.POLL_MILLISECONDS)
    container = [c for c in conftest.ConsoleAppContainer.instances if c.commands][-1]
    container.finish(0)
    assert receiver.session.opened[-1][1] == (3,), receiver.session.opened
    assert bridge.uninstaller.phase == "done"
    text = open(log, encoding="utf-8").read()
    assert "Traceback" not in text, text
    assert "uninstall step 8" in text
    print("COMPLETE")
    """
)


def test_the_whole_teardown_runs_with_the_package_directory_gone(tmp_path):
    """The proof that nothing on the path is a first import."""
    work = tmp_path / "work"
    shutil_copy = __import__("shutil").copytree
    shutil_copy(
        str(PACKAGE_DIR), str(work / "pkg" / "MQTTBridge"),
        ignore=__import__("shutil").ignore_patterns("__pycache__", "*.pyc"),
    )
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(script), str(TESTS), str(work)],
        capture_output=True, text=True, timeout=120, env=environment, cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "COMPLETE" in result.stdout
