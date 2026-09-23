"""Collapsing the softcam to one instance, on request and optionally by itself.

The receiver this was written against is modelled here rather than described:
`FakeProc` is a `/proc` tree on disk, with real `exe` symlinks, and a signal sent
to it removes a process the way a signal to a real one would. That is deliberate.
The three things this feature gets wrong if it is written from intuition —
counting a supervisor and its worker as two instances, telling two binaries apart
whose names agree for the first fifteen characters, and reading a missing ECM
file as a fault — are all things a mocked process list would have agreed with.

🔴 Nothing in this module may carry a real value from a receiver's `/tmp/ecm.info`.
The markers below are invented, and the point of the privacy test is that they
never come back out.
"""

import builtins
import json
import os
import shutil
import time

import conftest
import pytest
from Components.config import config
from conftest import ConsoleAppContainer, RecordTimerEntry

from MQTTBridge import softcam
from MQTTBridge.origin import PAGE
from MQTTBridge.softcam import (
    COMM_LENGTH,
    not_decoding_seconds,
    resolve,
    roots,
    scan,
    signal_order,
    start_command,
)

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
SOFTCAM = ROOT + "/softcam"
INFO = ROOT + "/info"
LAST_ERROR = ROOT + "/last_error"

# Sixteen characters: one more than the kernel keeps, which is the whole defect.
CAM = "OSCam_00000-r000"
TWIN = "OSCam_00000-r001"
SHORT_CAM = "oscam"


# ----------------------------------------------------------------- the receiver --


class FakeProc:
    """A `/proc` tree, and processes that die when they are signalled."""

    def __init__(self, path):
        self.path = str(path)
        os.makedirs(self.path, exist_ok=True)
        self.signals = []
        self.stubborn = set()
        self.processes = {}
        # Things under /proc that are not processes, which the scan must skip.
        os.makedirs(os.path.join(self.path, "self"), exist_ok=True)
        with open(os.path.join(self.path, "uptime"), "w", encoding="utf-8") as handle:
            handle.write("1 1\n")

    def set(self, processes):
        """`{pid: (comm, parent pid, executable)}` — the whole table at once."""
        for entry in os.listdir(self.path):
            if entry.isdigit():
                shutil.rmtree(os.path.join(self.path, entry))
        self.processes = dict(processes)
        for pid, (command, parent, executable) in self.processes.items():
            directory = os.path.join(self.path, str(pid))
            os.makedirs(directory)
            with open(os.path.join(directory, "stat"), "w", encoding="utf-8") as handle:
                # The real layout: pid, the command in brackets, the state, and
                # then the parent. Everything after that is padding a real
                # kernel would fill in.
                handle.write(
                    f"{pid} ({command}) S {parent} "
                    "1 1 0 -1 0 0 0 0 0 0 0 20 0 11 0 33161275\n"
                )
            if executable is not None:
                os.symlink(executable, os.path.join(directory, "exe"))
        return self

    def send(self, pid, number):
        self.signals.append((pid, number))
        if number == softcam.KILL or pid not in self.stubborn:
            remaining = dict(self.processes)
            remaining.pop(pid, None)
            self.set(remaining)
        return True

    def pids(self):
        return sorted(self.processes)


def make_binary(directory, name=CAM, mode=0o755):
    os.makedirs(str(directory), exist_ok=True)
    path = os.path.join(str(directory), name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n")
    os.chmod(path, mode)
    return os.path.realpath(path)


def healthy(binary, name=CAM):
    """What a working receiver looks like: a supervisor and the worker it forked."""
    return {
        5112: (name[:COMM_LENGTH], 1, binary),
        5113: (name[:COMM_LENGTH], 5112, binary),
    }


def started_commands():
    return [
        command
        for instance in ConsoleAppContainer.instances
        for command in instance.commands
    ]


def error(factory):
    entry = factory.client.last(LAST_ERROR)
    return None if entry is None or entry.text == "" else entry.json()["error"]


@pytest.fixture
def lab(tmp_path, monkeypatch):
    """A softcam directory and a `/proc`, with the module pointed at both."""

    class Lab:
        def __init__(self):
            self.directory = str(tmp_path / "softcams")
            self.binary = make_binary(self.directory)
            self.proc = FakeProc(tmp_path / "proc").set(healthy(self.binary))
            self.ecm = str(tmp_path / "ecm.info")

    built = Lab()
    monkeypatch.setattr(softcam, "SOFTCAM_DIRECTORY", os.path.realpath(built.directory))
    monkeypatch.setattr(softcam, "PROC_DIRECTORY", built.proc.path)
    monkeypatch.setattr(softcam, "ECM_PATH", built.ecm)
    monkeypatch.setattr(softcam, "_send", built.proc.send)
    return built


@pytest.fixture
def softcam_bridge(lab, make_bridge, factory, settings, receiver, monkeypatch):
    """A started bridge whose softcam publisher is looking at the fake receiver.

    The post-start window is stood down by default: it has its own test, and
    every other test here would otherwise be a test of it.
    """

    def build(name=CAM, post_start=0, absolute=True):
        monkeypatch.setattr(softcam, "POST_START_SECONDS", post_start)
        # 🔴 Absolute by default, because that is what a receiver stores. A run
        # against bare names is the one that agreed with the bug.
        entry = os.path.join(os.path.realpath(lab.directory), name) if absolute else name
        config.softcammanager.softcams_autostart.value = [entry]
        settings.host.value = "10.0.0.5"
        settings.node_id.value = NODE
        settings.friendly_name.value = "Living room receiver"
        bridge = make_bridge(session=receiver.session)
        bridge.start()
        factory.client.fire_connect()
        return bridge

    return build


def send(factory, payload=b"PRESS", retain=False):
    factory.client.fire_message(ROOT + "/cmd/softcam_restart", payload, retain=retain)


def run_the_sequence(publisher, turns=20):
    """Turn the restart's timer until it has settled, as the main loop would."""
    for _ in range(turns):
        if publisher._phase is None:
            break
        publisher._sequence.timer.fire()
    return publisher


# ---------------------------------------------------------------------- counting --


def test_a_supervisor_and_its_worker_are_one_instance(lab):
    found = scan(CAM[:COMM_LENGTH], lab.binary, lab.proc.path)
    assert sorted(found) == [5112, 5113]
    assert roots(found) == [5112]


def test_two_independent_roots_are_two_instances(lab):
    lab.proc.set({
        5112: (CAM[:COMM_LENGTH], 1, lab.binary),
        5113: (CAM[:COMM_LENGTH], 5112, lab.binary),
        7000: (CAM[:COMM_LENGTH], 1, lab.binary),
        7001: (CAM[:COMM_LENGTH], 7000, lab.binary),
    })
    assert roots(scan(CAM[:COMM_LENGTH], lab.binary, lab.proc.path)) == [5112, 7000]


def test_an_orphan_whose_parent_was_killed_is_one_instance(lab):
    """Its parent is init now, which is the same rule and not a special case."""
    lab.proc.set({5113: (CAM[:COMM_LENGTH], 1, lab.binary)})
    assert roots(scan(CAM[:COMM_LENGTH], lab.binary, lab.proc.path)) == [5113]


def test_a_sixteen_character_name_is_matched_through_its_truncated_comm(lab):
    """The name the image looks for can never equal the name the kernel kept."""
    assert len(CAM) > COMM_LENGTH
    assert scan(CAM, lab.binary, lab.proc.path) == {}
    assert scan(CAM[:COMM_LENGTH], lab.binary, lab.proc.path) != {}


def test_two_binaries_that_truncate_alike_are_told_apart_by_exe(lab, tmp_path):
    """🔴 `OSCam_00000-r000` and `OSCam_00000-r001` have the same `comm`.

    Without the `exe` check this counts two instances of a cam of which exactly
    one is running, and a restart would signal somebody else's program.
    """
    twin = make_binary(lab.directory, TWIN)
    assert CAM[:COMM_LENGTH] == TWIN[:COMM_LENGTH]
    lab.proc.set({
        5112: (CAM[:COMM_LENGTH], 1, lab.binary),
        6000: (TWIN[:COMM_LENGTH], 1, twin),
    })
    assert roots(scan(CAM[:COMM_LENGTH], lab.binary, lab.proc.path)) == [5112]
    assert roots(scan(TWIN[:COMM_LENGTH], twin, lab.proc.path)) == [6000]


def test_a_process_list_that_cannot_be_read_is_an_unknown_count(tmp_path):
    """`null`, and not `0`: „I could not look" is not „nothing is running"."""
    assert scan(CAM[:COMM_LENGTH], "/usr/softcams/x", str(tmp_path / "absent")) is None


def test_a_process_that_ends_between_the_listing_and_the_read_is_skipped(lab):
    os.makedirs(os.path.join(lab.proc.path, "9999"))
    assert roots(scan(CAM[:COMM_LENGTH], lab.binary, lab.proc.path)) == [5112]


def test_a_process_with_no_exe_link_is_not_ours(lab):
    """A zombie has no `exe`, and neither has a kernel thread."""
    lab.proc.set({5112: (CAM[:COMM_LENGTH], 1, None)})
    assert scan(CAM[:COMM_LENGTH], lab.binary, lab.proc.path) == {}


def test_a_command_containing_brackets_is_still_read_correctly(lab):
    lab.proc.set({5112: ("we(ird) name", 1, lab.binary)})
    assert scan("we(ird) name", lab.binary, lab.proc.path) == {5112: 1}


def test_the_root_is_signalled_before_the_worker_it_supervises(lab):
    found = scan(CAM[:COMM_LENGTH], lab.binary, lab.proc.path)
    assert signal_order(found) == [5112, 5113]


# -------------------------------------------------------------------- resolution --


def test_the_cam_must_be_an_executable_regular_file_in_the_softcam_directory(lab, tmp_path):
    assert resolve(CAM, lab.directory) == lab.binary
    assert resolve("absent", lab.directory) is None

    make_binary(lab.directory, "notexecutable", mode=0o644)
    assert resolve("notexecutable", lab.directory) is None

    os.makedirs(os.path.join(lab.directory, "adirectory"))
    assert resolve("adirectory", lab.directory) is None


def test_a_name_that_climbs_out_of_the_directory_is_refused(lab, tmp_path):
    outside = make_binary(tmp_path / "elsewhere", "payload")
    assert os.path.exists(outside)
    assert resolve("../elsewhere/payload", lab.directory) is None
    assert resolve("/bin/sh", lab.directory) is None


def test_a_symlink_that_leaves_the_directory_is_refused(lab, tmp_path):
    outside = make_binary(tmp_path / "elsewhere", "payload")
    os.symlink(outside, os.path.join(lab.directory, "innocent"))
    assert resolve("innocent", lab.directory) is None


def test_a_name_that_would_need_shell_quoting_is_refused(lab):
    """The start line is run by a shell, and the safest quoting is none at all."""
    make_binary(lab.directory, "OSCam;reboot")
    assert resolve("OSCam;reboot", lab.directory) is None
    assert resolve("OSCam $(id)", lab.directory) is None


def test_the_absolute_paths_the_image_really_stores_are_what_gets_resolved(
    softcam_bridge, factory, lab
):
    """🔴 `softcams_autostart` holds `/usr/softcams/<name>`, not `<name>`.

    The image's own manager strips that prefix in the first line of its loop,
    and the line exists only because the prefix is there. Nothing downstream may
    see a path: the contract promises a basename, the truncated command is the
    first fifteen characters of one, and „is this name longer than fifteen
    characters" is a question about a name.
    """
    bridge = softcam_bridge(absolute=True)
    stored = config.softcammanager.softcams_autostart.value
    assert stored == [os.path.join(os.path.realpath(lab.directory), CAM)]
    assert stored[0].startswith("/")

    assert bridge.publisher("softcam") is not None
    assert "softcam" in bridge.capabilities()
    payload = factory.client.last(SOFTCAM).json()
    assert payload["selected"] == CAM
    assert payload["running_instances"] == 1
    assert payload["manager_check_on_start"] is True


def test_a_bare_name_is_accepted_too(softcam_bridge, factory):
    """An image that stores it the other way is not punished for it."""
    softcam_bridge(absolute=False)
    assert factory.client.last(SOFTCAM).json()["selected"] == CAM


def test_only_the_softcam_directory_prefix_is_stripped(lab):
    """An entry pointing elsewhere keeps its separators, and the name guard refuses it.

    Taking the basename of anything would rebase `/opt/cams/<name>` onto the
    softcam directory and run a different program from the one the image was
    told to start.
    """
    inside = os.path.join(lab.directory, CAM)
    outside = os.path.join("/opt/cams", CAM)
    config.softcammanager.softcams_autostart.value = [inside, outside, CAM]

    assert softcam.autostart_entries(lab.directory) == [CAM, outside, CAM]
    assert resolve(outside, lab.directory) is None


def test_a_receiver_with_nothing_set_to_autostart_says_so(softcam_bridge, plugin_log):
    """And does not claim the image failed to give the plugin its hooks."""
    config.softcammanager.softcams_autostart.value = []
    bridge = softcam_bridge()
    config.softcammanager.softcams_autostart.value = []
    publisher = softcam.SoftcamPublisher(bridge, directory=softcam.SOFTCAM_DIRECTORY)
    assert publisher.start() is False
    assert publisher.switched_off is True
    text = plugin_log()
    assert "no softcam set to start automatically" in text
    assert "does not provide the softcam hooks" not in text


def test_a_binary_replaced_under_a_running_process_is_still_matched(lab):
    """🔴 Upgrading the cam is exactly when somebody reaches for this button.

    From the moment `opkg` replaces the file, every copy already running reads
    `… (deleted)` from its `exe` link. Comparing verbatim drops them out of the
    count — one instance reported while two fight over the card, and a collapse
    button that cannot collapse them.
    """
    lab.proc.set({
        5112: (CAM[:COMM_LENGTH], 1, lab.binary + " (deleted)"),
        5113: (CAM[:COMM_LENGTH], 5112, lab.binary + " (deleted)"),
        7000: (CAM[:COMM_LENGTH], 1, lab.binary),
    })
    found = scan(CAM[:COMM_LENGTH], lab.binary, lab.proc.path)
    assert sorted(found) == [5112, 5113, 7000]
    assert roots(found) == [5112, 7000]


def test_the_family_of_the_binary_decides_the_start_line():
    """🔴 The family is the binary's, never the protocol it speaks outward."""
    assert start_command(CAM, "/usr/softcams") == "ulimit -s 1024;/usr/softcams/" + CAM + " -b"
    assert start_command("ncam", "/usr/softcams") == "ulimit -s 1024;/usr/softcams/ncam -b"
    assert start_command("CCcam-2.3.2", "/usr/softcams") == (
        "ulimit -s 1024;/usr/softcams/CCcam-2.3.2"
    )
    assert start_command("sbox", "/usr/softcams") == "ulimit -s 1024;/usr/softcams/sbox"
    assert start_command("mgcamd", "/usr/softcams") == "ulimit -s 1024;/usr/softcams/mgcamd"
    assert start_command("gbox", "/usr/softcams").endswith(
        ";start-stop-daemon --start --quiet --background --exec /usr/bin/gbox"
    )
    assert start_command("", "/usr/softcams") is None


# ----------------------------------------------------- the capability and the topic --


def test_there_is_no_softcam_capability_on_a_receiver_without_one(live_bridge, factory):
    assert live_bridge.publisher("softcam") is None
    assert "softcam" not in live_bridge.capabilities()
    assert factory.client.last(SOFTCAM) is None


def test_the_capability_and_the_topic_appear_together(softcam_bridge, factory):
    bridge = softcam_bridge()
    assert "softcam" in bridge.capabilities()
    payload = factory.client.last(SOFTCAM).json()
    assert payload["selected"] == CAM
    assert payload["running_instances"] == 1
    assert payload["last_restart"] is None
    assert payload["last_restart_reason"] is None
    assert payload["restarts_today"] == 0


def test_the_capability_is_not_claimed_when_the_image_uses_its_init_script(softcam_bridge):
    """A process-level restart would fight `/etc/init.d/softcam start`."""
    config.misc.softcams.value = "oscam"
    bridge = softcam_bridge()
    assert bridge.publisher("softcam") is None
    assert "softcam" not in bridge.capabilities()


def test_the_capability_is_not_claimed_for_a_cam_that_does_not_resolve(softcam_bridge):
    bridge = softcam_bridge(name="somethingelse")
    assert bridge.publisher("softcam") is None


def test_the_topic_says_whether_the_image_will_add_a_copy_at_every_start(
    softcam_bridge, factory, lab
):
    """The one fact that separates a receiver needing this from one that does not."""
    bridge = softcam_bridge()
    assert factory.client.last(SOFTCAM).json()["manager_check_on_start"] is True
    assert len(bridge.publisher("softcam").selected) > COMM_LENGTH


def test_a_short_enough_name_is_not_affected_by_the_image_defect(softcam_bridge, factory, lab):
    binary = make_binary(lab.directory, SHORT_CAM)
    lab.proc.set(healthy(binary, SHORT_CAM))
    softcam_bridge(name=SHORT_CAM)
    assert factory.client.last(SOFTCAM).json()["manager_check_on_start"] is False


def test_the_periodic_check_is_reported_only_when_it_is_switched_on(softcam_bridge, factory):
    """Six minutes, on an affected box, is an instance every six minutes for ever."""
    bridge = softcam_bridge()
    assert factory.client.last(SOFTCAM).json()["manager_timer_minutes"] is None
    config.softcammanager.softcamtimerenabled.value = True
    bridge.publisher("softcam")._poll.timer.fire()
    assert factory.client.last(SOFTCAM).json()["manager_timer_minutes"] == 6


def test_the_count_is_polled_because_it_changes_without_us(softcam_bridge, factory, lab):
    """The household can start and stop the cam from the extensions menu."""
    bridge = softcam_bridge()
    assert factory.client.last(SOFTCAM).json()["running_instances"] == 1
    lab.proc.set({
        5112: (CAM[:COMM_LENGTH], 1, lab.binary),
        5113: (CAM[:COMM_LENGTH], 5112, lab.binary),
        7000: (CAM[:COMM_LENGTH], 1, lab.binary),
    })
    bridge.publisher("softcam")._poll.timer.fire()
    assert factory.client.last(SOFTCAM).json()["running_instances"] == 2


def test_a_count_that_could_not_be_taken_is_null(softcam_bridge, factory, lab):
    bridge = softcam_bridge()
    shutil.rmtree(lab.proc.path)
    bridge.publisher("softcam")._poll.timer.fire()
    assert factory.client.last(SOFTCAM).json()["running_instances"] is None


def test_a_slow_poll_says_so(softcam_bridge, monkeypatch, plugin_log):
    bridge = softcam_bridge()
    clock = iter((10.0, 11.0))
    monkeypatch.setattr(softcam.time, "monotonic", lambda: next(clock))
    bridge.publisher("softcam")._tick()
    assert "the softcam instance poll took 1000 ms" in plugin_log()


# ------------------------------------------------------------------- the guards --


def test_the_restart_is_refused_without_the_permission(softcam_bridge, factory, lab):
    softcam_bridge()
    send(factory)
    refusal = error(factory)
    assert refusal and "switched off in the plugin's settings" in refusal
    # Verified by effect: nothing was signalled and nothing was started.
    assert lab.proc.signals == []
    assert lab.proc.pids() == [5112, 5113]


def test_the_restart_is_refused_while_the_receiver_is_recording(
    softcam_bridge, factory, settings, receiver, lab
):
    settings.softcam_restart_allowed.value = True
    softcam_bridge()
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    send(factory)
    assert error(factory) == "the receiver is recording"
    assert lab.proc.signals == []


def test_the_restart_is_refused_with_a_recording_due(
    softcam_bridge, factory, settings, receiver, lab
):
    """🔴 The full guard, not „is it recording". Ten minutes is the window."""
    settings.softcam_restart_allowed.value = True
    softcam_bridge()
    receiver.add_timer(begin=int(time.time()) + 60, end=int(time.time()) + 600)
    send(factory)
    refusal = error(factory)
    assert refusal and "a recording starts in" in refusal
    assert lab.proc.signals == []


# ------------------------------------------------------------ from the page (§11 ab) --


def test_a_restart_from_the_openwebif_page_needs_no_permission(softcam_bridge, factory, lab):
    """The page is as open as the receiver's web interface, which can already grant it."""
    bridge = softcam_bridge()
    assert bridge.run_command("softcam_restart", "", PAGE) is None
    assert lab.proc.signals == [(5112, softcam.TERMINATE), (5113, softcam.TERMINATE)]
    assert error(factory) is None


def test_the_same_restart_over_mqtt_is_still_refused_after_the_page_ran_one(
    softcam_bridge, factory, lab
):
    """The origin is an argument, never a mode: nothing the page did carries over."""
    bridge = softcam_bridge()
    publisher = bridge.publisher("softcam")
    bridge.run_command("softcam_restart", "", PAGE)
    run_the_sequence(publisher)
    lab.proc.signals = []
    lab.proc.set(healthy(lab.binary))
    # Out of the one-a-minute window, so only the permission can refuse it.
    publisher._last_started -= softcam.MANUAL_INTERVAL_SECONDS + 1

    send(factory)
    assert "switched off in the plugin's settings" in error(factory)
    assert lab.proc.signals == []


def test_autoheal_is_still_refused_after_a_page_restart(
    softcam_bridge, settings, receiver, lab, plugin_log
):
    """🔴 The automatic restart is nobody's request, so it stays behind the permission."""
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    publisher = bridge.publisher("softcam")
    bridge.run_command("softcam_restart", "", PAGE)
    run_the_sequence(publisher)
    lab.proc.signals = []
    lab.proc.set(healthy(lab.binary))
    # Out of the ten-minute window, so only the permission can refuse it.
    publisher._last_started -= softcam.AUTOHEAL_INTERVAL_SECONDS + 1
    receiver.info.encrypted = True
    stuck_for(publisher, 600)

    publisher._detector.timer.fire()
    assert lab.proc.signals == []
    assert "restarting the softcam is switched off" in plugin_log()


def test_a_recording_refuses_a_page_restart_as_well(softcam_bridge, factory, receiver, lab):
    bridge = softcam_bridge()
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    assert bridge.run_command("softcam_restart", "", PAGE) == "the receiver is recording"
    assert error(factory) == "the receiver is recording"
    assert lab.proc.signals == []


def test_the_restart_is_refused_when_the_image_will_not_say(
    softcam_bridge, factory, settings, receiver, lab, monkeypatch
):
    settings.softcam_restart_allowed.value = True
    softcam_bridge()
    monkeypatch.setattr(receiver.nav, "RecordTimer", None)
    send(factory)
    assert error(factory) == (
        "this image will not say whether it is recording; refusing to risk it"
    )
    assert lab.proc.signals == []


def test_the_restart_is_refused_for_the_first_minute_after_the_plugin_starts(
    softcam_bridge, factory, settings, lab
):
    """🔴 The image's own check fires about a second after every start.

    Restarting inside that window races a copy already on its way, and
    manufactures exactly the duplicate this command exists to remove.
    """
    settings.softcam_restart_allowed.value = True
    softcam_bridge(post_start=60)
    send(factory)
    refusal = error(factory)
    assert refusal and "only just started" in refusal
    assert "Try again in" in refusal
    assert lab.proc.signals == []


def test_a_second_press_inside_a_minute_is_refused(
    softcam_bridge, factory, settings, lab
):
    settings.softcam_restart_allowed.value = True
    bridge = softcam_bridge()
    send(factory)
    run_the_sequence(bridge.publisher("softcam"))
    before = factory.client.last(SOFTCAM).json()["last_restart"]
    lab.proc.signals = []

    send(factory)
    refusal = error(factory)
    assert refusal and "at most one restart a minute" in refusal
    assert lab.proc.signals == []
    assert factory.client.last(SOFTCAM).json()["last_restart"] == before


def test_a_press_while_a_restart_is_in_flight_is_refused(
    softcam_bridge, factory, settings, lab
):
    settings.softcam_restart_allowed.value = True
    bridge = softcam_bridge()
    lab.proc.stubborn = {5112, 5113}
    send(factory)
    assert bridge.publisher("softcam")._busy is True
    send(factory)
    assert error(factory) == "a softcam restart is already running"


def test_the_restart_is_refused_when_the_process_list_cannot_be_read(
    softcam_bridge, factory, settings, lab
):
    """Signalling what cannot be seen is exactly the mistake to refuse."""
    settings.softcam_restart_allowed.value = True
    softcam_bridge()
    shutil.rmtree(lab.proc.path)
    send(factory)
    assert error(factory) == (
        "the receiver will not say which processes are running; refusing to guess"
    )


def test_there_is_no_command_at_all_without_the_capability(live_bridge, factory):
    factory.client.fire_message(ROOT + "/cmd/softcam_restart", b"PRESS")
    assert error(factory) == "this receiver's image has no softcam this plugin can restart"


# ----------------------------------------------------------------- the sequence --


def test_a_restart_stops_every_instance_and_starts_exactly_one(
    softcam_bridge, factory, settings, lab
):
    settings.softcam_restart_allowed.value = True
    bridge = softcam_bridge()
    send(factory)
    assert lab.proc.signals == [(5112, softcam.TERMINATE), (5113, softcam.TERMINATE)]
    assert lab.proc.pids() == []

    run_the_sequence(bridge.publisher("softcam"))
    assert started_commands() == [
        "ulimit -s 1024;" + os.path.join(os.path.realpath(lab.directory), CAM) + " -b"
    ]


def test_the_payload_cannot_reach_the_command_line(softcam_bridge, factory, settings, lab):
    """🔴 A binary name arriving over the broker is remote code execution."""
    settings.softcam_restart_allowed.value = True
    bridge = softcam_bridge()
    send(factory, b"/bin/sh -c 'rm -rf /'")
    run_the_sequence(bridge.publisher("softcam"))
    assert started_commands() == [
        "ulimit -s 1024;" + os.path.join(os.path.realpath(lab.directory), CAM) + " -b"
    ]


def test_a_process_that_ignores_the_first_signal_is_killed(
    softcam_bridge, factory, settings, lab, monkeypatch
):
    settings.softcam_restart_allowed.value = True
    bridge = softcam_bridge()
    publisher = bridge.publisher("softcam")
    lab.proc.stubborn = {5112, 5113}

    send(factory)
    assert [number for _pid, number in lab.proc.signals] == [softcam.TERMINATE] * 2

    # Five seconds of re-checking, none of which blocks anything.
    for _ in range(10):
        publisher._sequence.timer.fire()
    assert publisher._phase == "terminating"

    monkeypatch.setattr(softcam.time, "monotonic", lambda: publisher._deadline + 1)
    publisher._sequence.timer.fire()
    assert (5112, softcam.KILL) in lab.proc.signals
    assert lab.proc.pids() == []


def test_a_restart_is_counted_and_dated(softcam_bridge, factory, settings, lab):
    settings.softcam_restart_allowed.value = True
    bridge = softcam_bridge()
    send(factory)
    run_the_sequence(bridge.publisher("softcam"))
    payload = factory.client.last(SOFTCAM).json()
    assert payload["last_restart_reason"] == "manual"
    assert payload["restarts_today"] == 1
    assert abs(payload["last_restart"] - int(time.time())) < 5


def test_the_topic_is_republished_once_it_has_settled(
    softcam_bridge, factory, settings, lab
):
    """The image's own code sleeps ten seconds after a start; five can be early."""
    settings.softcam_restart_allowed.value = True
    bridge = softcam_bridge()
    publisher = bridge.publisher("softcam")
    send(factory)
    publisher._sequence.timer.fire()          # the cam has gone; start one
    lab.proc.set(healthy(lab.binary))          # and here it is
    factory.client.clear()

    publisher._sequence.timer.fire()          # five seconds later
    assert factory.client.last(SOFTCAM).json()["running_instances"] == 1
    lab.proc.set({})
    publisher._sequence.timer.fire()          # and twenty
    assert factory.client.last(SOFTCAM).json()["running_instances"] == 0
    assert publisher._phase is None


def test_a_start_line_that_will_not_run_is_reported(
    softcam_bridge, factory, settings, lab, monkeypatch
):
    settings.softcam_restart_allowed.value = True
    bridge = softcam_bridge()
    # A non-zero answer from `execute` is „the program never started at all",
    # which is a different failure from one that ran and produced nothing.
    monkeypatch.setattr(ConsoleAppContainer, "execute", lambda self, command, *rest: 1)
    send(factory)
    run_the_sequence(bridge.publisher("softcam"))
    assert error(factory) == CAM + " could not be started"
    assert factory.client.last(SOFTCAM).json()["last_restart"] is None


def test_a_restart_that_can_get_no_timer_signals_nothing_at_all(
    softcam_bridge, factory, settings, lab, monkeypatch
):
    """🔴 The one path that would otherwise end with a dead cam and no error.

    The sequence is armed before anything is signalled — an `eTimer` cannot fire
    until this returns to the main loop, so there is no race in doing it first —
    which turns „stopped the cam, never started one, and wedged `_busy` so every
    later attempt is refused" into an ordinary refusal that changed nothing.
    """
    settings.softcam_restart_allowed.value = True
    bridge = softcam_bridge()
    publisher = bridge.publisher("softcam")
    monkeypatch.setattr(publisher._sequence, "start", lambda *a, **k: False)

    send(factory)
    refusal = error(factory)
    assert refusal and "would not give the plugin a timer" in refusal
    assert lab.proc.signals == []
    assert lab.proc.pids() == [5112, 5113]
    assert publisher._busy is False
    assert publisher._phase is None


def test_a_sequence_that_loses_its_timer_does_not_wedge(
    softcam_bridge, factory, settings, lab, monkeypatch
):
    """A restart already under way must not leave `_busy` stuck true for ever."""
    settings.softcam_restart_allowed.value = True
    bridge = softcam_bridge()
    publisher = bridge.publisher("softcam")
    lab.proc.stubborn = {5112, 5113}
    send(factory)
    assert publisher._busy is True

    monkeypatch.setattr(publisher._sequence, "start", lambda *a, **k: False)
    publisher._sequence.timer.fire()

    assert publisher._busy is False
    assert publisher._phase is None
    refusal = error(factory)
    assert refusal and "would not give the plugin a timer" in refusal


def test_a_retained_restart_command_is_discarded(softcam_bridge, factory, settings, lab):
    """A retained command re-fires on every reconnect, for ever."""
    settings.softcam_restart_allowed.value = True
    softcam_bridge()
    send(factory, retain=True)
    assert lab.proc.signals == []
    assert error(factory) is None


# ------------------------------------------------------------------ the counter --


def test_the_counter_rolls_over_at_local_midnight_and_survives_a_clock_step(softcam_bridge):
    """🟡 Recomputed from a stored date rather than reset by a timer.

    A midnight timer armed before the receiver's clock steps after boot fires at
    the wrong moment; comparing local dates simply comes out right.
    """
    publisher = softcam_bridge().publisher("softcam")
    noon = time.mktime((2026, 9, 22, 12, 0, 0, 0, 0, -1))
    publisher._restarts_today(now=noon)
    publisher._restarts = 3

    assert publisher._restarts_today(now=noon + 3600) == 3
    # The clock stepping backwards within the same day is not a new day.
    assert publisher._restarts_today(now=noon - 7200) == 3
    assert publisher._restarts_today(now=noon + 86400) == 0


# ------------------------------------------------------------------- auto-heal --


def stuck_for(publisher, seconds, path=None):
    """Make the receiver look like it has not decoded for `seconds`."""
    now = time.time()
    publisher._since = now - seconds - 1
    publisher._service_since = now - seconds - 1
    target = path or publisher.path
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("ecm time: 0.434\n")
    os.utime(target, (now - seconds - 1, now - seconds - 1))


def test_not_decoding_is_measured_from_the_latest_of_three_things(tmp_path):
    path = str(tmp_path / "ecm.info")
    assert not_decoding_seconds(path, 0, 0, 1000.0) == 0.0
    # Absent is infinitely old, so the service start is what it counts from.
    assert not_decoding_seconds(path, 900.0, 0, 1000.0) == 100.0
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("ecm time: 0.434\n")
    os.utime(path, (950.0, 950.0))
    assert not_decoding_seconds(path, 900.0, 0, 1000.0) == 50.0
    # A zap after the last write restarts the window.
    assert not_decoding_seconds(path, 990.0, 0, 1000.0) == 10.0


def test_a_file_replaced_by_a_link_is_not_the_file_whose_age_was_measured(tmp_path):
    target = str(tmp_path / "target")
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("x\n")
    link = str(tmp_path / "ecm.info")
    os.symlink(target, link)
    assert not_decoding_seconds(link, 900.0, 0, 1000.0) == 100.0


def test_autoheal_does_nothing_until_it_is_switched_on(
    softcam_bridge, settings, receiver, lab
):
    settings.softcam_restart_allowed.value = True
    bridge = softcam_bridge()
    receiver.info.encrypted = True
    publisher = bridge.publisher("softcam")
    stuck_for(publisher, 600)
    publisher._detector.timer.fire()
    assert lab.proc.signals == []


def test_autoheal_restarts_a_stuck_encrypted_service(
    softcam_bridge, settings, receiver, lab, factory
):
    settings.softcam_restart_allowed.value = True
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    receiver.info.encrypted = True
    publisher = bridge.publisher("softcam")
    stuck_for(publisher, 600)

    publisher._detector.timer.fire()
    assert lab.proc.signals == [(5112, softcam.TERMINATE), (5113, softcam.TERMINATE)]
    run_the_sequence(publisher)
    assert factory.client.last(SOFTCAM).json()["last_restart_reason"] == "autoheal"
    assert factory.client.last(SOFTCAM).json()["restarts_today"] == 1


def test_autoheal_ignores_a_free_to_air_service_with_no_ecm_file(
    softcam_bridge, settings, receiver, lab
):
    """🔴 Measured: the cam *removes* the file when it stops descrambling.

    So on a free-to-air channel absence is the normal, healthy state, and a
    detector that did not gate on „encrypted" first would report a stuck softcam
    on every free-to-air channel in the house.
    """
    settings.softcam_restart_allowed.value = True
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    receiver.info.encrypted = False
    publisher = bridge.publisher("softcam")
    publisher._since = time.time() - 3600
    publisher._service_since = time.time() - 3600
    assert not os.path.exists(publisher.path)

    publisher._detector.timer.fire()
    assert lab.proc.signals == []


def test_autoheal_honours_the_configured_seconds(
    softcam_bridge, settings, receiver, lab
):
    settings.softcam_restart_allowed.value = True
    settings.softcam_autoheal.value = True
    settings.softcam_autoheal_seconds.value = 300
    bridge = softcam_bridge()
    receiver.info.encrypted = True
    publisher = bridge.publisher("softcam")

    stuck_for(publisher, 200)
    publisher._detector.timer.fire()
    assert lab.proc.signals == []

    stuck_for(publisher, 400)
    publisher._detector.timer.fire()
    assert lab.proc.signals != []


def test_autoheal_restarts_its_window_on_a_service_change(
    softcam_bridge, settings, receiver, lab
):
    """🔴 A channel-hopping household must not accumulate time across services."""
    settings.softcam_restart_allowed.value = True
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    receiver.info.encrypted = True
    publisher = bridge.publisher("softcam")
    stuck_for(publisher, 600)

    receiver.nav.fire(1)          # evStart
    publisher._detector.timer.fire()
    assert lab.proc.signals == []


def test_autoheal_works_with_the_conditional_access_telemetry_switched_off(
    softcam_bridge, settings, receiver, lab
):
    """🔴 A repair must not require a privacy switch to be turned on."""
    settings.softcam_restart_allowed.value = True
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    assert settings.cam_telemetry.value is False
    assert bridge.publisher("cam") is None
    assert "cam" not in bridge.capabilities()

    receiver.info.encrypted = True
    publisher = bridge.publisher("softcam")
    stuck_for(publisher, 600)
    publisher._detector.timer.fire()
    assert lab.proc.signals != []


def test_autoheal_refuses_with_a_recording_due(
    softcam_bridge, settings, receiver, lab, plugin_log
):
    """🔴 The same guard a manual restart uses, whole, by decision.

    A restart landing close to a timer risks the opening seconds of the
    recording, and a scrambled recording is recoverable while a truncated one is
    not. The channel stays dark until the window passes; that is the cost.
    """
    settings.softcam_restart_allowed.value = True
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    receiver.info.encrypted = True
    receiver.add_timer(begin=int(time.time()) + 120, end=int(time.time()) + 600)
    publisher = bridge.publisher("softcam")
    stuck_for(publisher, 600)

    publisher._detector.timer.fire()
    assert lab.proc.signals == []
    assert "a recording starts in" in plugin_log()


def test_autoheal_refuses_when_the_image_will_not_say(
    softcam_bridge, settings, receiver, lab, monkeypatch
):
    settings.softcam_restart_allowed.value = True
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    receiver.info.encrypted = True
    publisher = bridge.publisher("softcam")
    stuck_for(publisher, 600)
    monkeypatch.setattr(receiver.nav, "RecordTimer", None)

    publisher._detector.timer.fire()
    assert lab.proc.signals == []


def test_autoheal_refuses_without_the_permission(
    softcam_bridge, settings, receiver, lab
):
    """Enabling the tuning on a box that never granted the permission does nothing."""
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    receiver.info.encrypted = True
    publisher = bridge.publisher("softcam")
    stuck_for(publisher, 600)
    publisher._detector.timer.fire()
    assert lab.proc.signals == []


def test_autoheal_leaves_the_receiver_alone_in_standby(
    softcam_bridge, settings, receiver, lab
):
    settings.softcam_restart_allowed.value = True
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    receiver.info.encrypted = True
    publisher = bridge.publisher("softcam")
    stuck_for(publisher, 600)
    receiver.enter_standby()

    publisher._detector.timer.fire()
    assert lab.proc.signals == []


def test_autoheal_fires_at_most_once_in_ten_minutes(
    softcam_bridge, settings, receiver, lab
):
    settings.softcam_restart_allowed.value = True
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    receiver.info.encrypted = True
    publisher = bridge.publisher("softcam")
    stuck_for(publisher, 600)

    publisher._detector.timer.fire()
    run_the_sequence(publisher)
    assert lab.proc.signals != []
    lab.proc.signals = []
    lab.proc.set(healthy(lab.binary))

    stuck_for(publisher, 600)
    publisher._detector.timer.fire()
    assert lab.proc.signals == []


def test_a_declined_autoheal_is_a_log_line_and_not_a_retained_error(
    softcam_bridge, factory, settings, receiver
):
    """Nobody asked for it, so nobody should find a red error waiting."""
    settings.softcam_restart_allowed.value = True
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    receiver.info.encrypted = True
    receiver.add_timer(state=RecordTimerEntry.StateRunning)
    publisher = bridge.publisher("softcam")
    stuck_for(publisher, 600)

    publisher._detector.timer.fire()
    assert factory.client.last(LAST_ERROR) is None


# -------------------------------------------------------------------- privacy --

# Invented values, in the shape the real file has. 🔴 No value from a receiver.
MARKERS = {
    "reader": "MARKERREADERaccount",
    "from": "MARKERHOSTname.invalid:12345",
    "caid": "MARKERCAID",
    "prov": "MARKERPROV",
    "chid": "MARKERCHID",
    "pid": "MARKERPID",
    "cw0": "MARKERCWZERO",
    "cw1": "MARKERCWONE",
}
ECM_FIXTURE = "".join(key + ": " + value + "\n" for key, value in MARKERS.items())


def test_nothing_from_the_ecm_file_reaches_a_payload_a_refusal_or_the_log(
    softcam_bridge, factory, settings, receiver, lab, plugin_log, monkeypatch
):
    """🔴 The file carries a card-sharing account, a server and live control words.

    None of it may reach the broker, `last_error`, a log record at any level or a
    diagnostic — not even hashed. The detector asks one question, „when was this
    last written", and a modification time is the whole answer, so the file is
    never opened at all.
    """
    settings.softcam_restart_allowed.value = True
    settings.softcam_autoheal.value = True
    bridge = softcam_bridge()
    receiver.info.encrypted = True
    publisher = bridge.publisher("softcam")

    stuck_for(publisher, 600)
    with open(publisher.path, "w", encoding="utf-8") as handle:
        handle.write(ECM_FIXTURE)
    os.utime(publisher.path, (time.time() - 601, time.time() - 601))

    publisher._detector.timer.fire()
    run_the_sequence(publisher)

    # And again down the exception path, where an echo of the input is the
    # classic way a secret escapes.
    def raises(_path):
        raise OSError("the receiver said no")

    monkeypatch.setattr(softcam, "_ecm_modified", raises)
    publisher._detector.timer.fire()

    published = "".join(entry.text for entry in factory.client.published)
    logged = plugin_log()
    for marker in MARKERS.values():
        assert marker not in published, marker
        assert marker not in logged, marker
    assert factory.client.last(LAST_ERROR) is None or "MARKER" not in (
        factory.client.last(LAST_ERROR).text
    )


def test_the_ecm_file_is_never_opened(tmp_path, monkeypatch):
    """The strongest form of the promise above: the bytes are never in scope."""
    path = str(tmp_path / "ecm.info")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(ECM_FIXTURE)

    opened = []
    real_open = builtins.open

    def watched(name, *args, **kwargs):
        opened.append(str(name))
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", watched)
    not_decoding_seconds(path, 0.0, 900.0, 1000.0)
    monkeypatch.undo()

    assert path not in opened


# ------------------------------------------------- settings, info and discovery --


def test_the_permission_is_published_read_only_and_the_tuning_is_writable(
    connected_bridge, factory, settings
):
    published = factory.client.last(INFO).json()["settings"]
    assert published["softcam_restart_allowed"] is False
    assert published["softcam_autoheal"] is False
    assert published["softcam_autoheal_seconds"] == 90
    assert "softcam_restart_allowed" not in connected_bridge.remote_settings()
    assert "softcam_autoheal" in connected_bridge.remote_settings()


def test_config_can_switch_autoheal_on_but_not_the_permission(
    connected_bridge, factory, settings
):
    """🔴 A setting that *enables* a command is granted at the television."""
    factory.client.fire_message(
        ROOT + "/cmd/config",
        b'{"publish_keys":true,"screenshot":"on_zap","screenshot_interval":60,'
        b'"softcam_autoheal":true,"softcam_autoheal_seconds":120}',
    )
    assert settings.softcam_autoheal.value is True
    assert settings.softcam_autoheal_seconds.value == 120

    factory.client.fire_message(
        ROOT + "/cmd/config",
        b'{"publish_keys":true,"screenshot":"on_zap","screenshot_interval":60,'
        b'"softcam_restart_allowed":true}',
    )
    assert error(factory) == "the config object contains unknown settings"
    assert settings.softcam_restart_allowed.value is False


@pytest.mark.parametrize("seconds", (29, 601))
def test_config_refuses_an_autoheal_delay_outside_its_range(
    connected_bridge, factory, settings, seconds
):
    factory.client.fire_message(
        ROOT + "/cmd/config",
        ('{"publish_keys":true,"screenshot":"on_zap","screenshot_interval":60,'
         '"softcam_autoheal_seconds":' + str(seconds) + "}").encode("utf-8"),
    )
    assert "softcam_autoheal_seconds must be between 30 and 600" in error(factory)
    assert settings.softcam_autoheal_seconds.value == 90


def components(factory):
    return factory.client.last("homeassistant/device/" + NODE + "/config").json()["cmps"]


def test_the_softcam_sensor_follows_the_capability(softcam_bridge, factory, live_bridge):
    assert "softcam" not in components(factory)
    factory.client.clear()
    softcam_bridge()
    sensor = components(factory)["softcam"]
    assert sensor["p"] == "sensor"
    assert sensor["stat_t"] == ROOT + "/softcam"
    assert sensor["ent_cat"] == "diagnostic"


def test_the_softcam_templates_are_rendered_rather_than_matched(softcam_bridge, factory):
    """🔴 A template that reads right can still render something unusable.

    Matching a `val_tpl` as a string is what let „next timer" ship a second
    `+00:00` and read unknown for two releases. These are rendered against the
    payload the box really publishes, and the result is what the entity would
    actually receive.
    """
    softcam_bridge()
    sensor = components(factory)["softcam"]
    payload = factory.client.last(SOFTCAM).json()

    assert conftest.render_value_template(sensor["val_tpl"], payload) == CAM

    attributes = json.loads(
        conftest.render_value_template(sensor["json_attr_tpl"], payload)
    )
    assert attributes == {
        "running_instances": 1,
        "last_restart": None,
        "last_restart_reason": None,
        "restarts_today": 0,
        "manager_check_on_start": True,
        "manager_timer_minutes": None,
    }


def test_a_missing_field_renders_as_unknown_and_not_as_an_empty_message(
    softcam_bridge, factory
):
    """🔴 The case `| default(none)` is actually for: a key that is not there.

    A `null` renders as `None` with or without the filter, so a test written on
    a null value passes either way and proves nothing. An **absent** key renders
    as an empty string, and Home Assistant reads an empty state as „ignore this
    message" — leaving the previous value on screen for ever rather than going
    unknown. That is what an older plugin, or a truncated payload, would send.
    """
    softcam_bridge()
    sensor = components(factory)["softcam"]
    published = factory.client.last(SOFTCAM).json()

    absent = {key: value for key, value in published.items() if key != "selected"}
    assert conftest.render_value_template(sensor["val_tpl"], absent) == "None"

    # And a real null, which is what this box publishes when nothing resolved.
    assert conftest.render_value_template(
        sensor["val_tpl"], dict(published, selected=None)
    ) == "None"
    attributes = json.loads(
        conftest.render_value_template(
            sensor["json_attr_tpl"],
            dict(published, running_instances=None, manager_timer_minutes=None),
        )
    )
    assert attributes["running_instances"] is None
    assert attributes["manager_timer_minutes"] is None


def test_the_restart_button_follows_the_permission(softcam_bridge, factory, settings):
    softcam_bridge()
    assert "softcam_restart" not in components(factory)

    settings.softcam_restart_allowed.value = True
    factory.client.fire_connect()
    button = components(factory)["softcam_restart"]
    assert button["p"] == "button"
    assert button["cmd_t"] == ROOT + "/cmd/softcam_restart"
