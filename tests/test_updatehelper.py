"""The update helper (`updatehelper.py`) against a fake receiver.

A temporary root holds the plugin, opkg's database and enigma2's settings; `Box` stands in for
everything else the helper asks - the clock, `/proc`, `init`, `opkg`, OpenWebif and the origin -
and moves only when the helper sleeps, so every wait is deterministic. Named pause points
(`Receiver.pause`) are where a test changes the world - a new enigma2, a signal, the plugin's
word - at exactly one step of the sequence. The last tests run the helper as the receiver does:
a copy in its own directory, a separate process, a real signal while it waits at a FIFO.
"""

import errno
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import indexlab
import pytest
import released_installer_0_3_1 as released
import updatelab

from MQTTBridge import trust, updatehelper

PLUGIN = updatehelper.PLUGIN_DIR
HOOK = updatehelper.WEBIF_SHIM
TVP1 = "1:0:19:283D:3FB:1:C00000:0:0:0:"
TVN = "1:0:19:283E:3FB:1:C00000:0:0:0:"
ORIGIN = "https://origin.test/feed/"
TID = "0123456789ab"
OLD, NEW = "0.4.0", "0.4.1"


def package(version, *, extra=None, control_version=None, name=None):
    data = {"./" + PLUGIN + "/plugin.py": f"# plugin {version}\n".encode(),
            "./" + PLUGIN + "/__init__.py": b"",
            "./" + HOOK: f"# hook {version}\n".encode()}
    data.update(extra or {})
    control = (f"Package: {name or updatehelper.PACKAGE}\nVersion: {control_version or version}\n"
               "Architecture: all\n").encode()
    members = [("debian-binary", b"2.0\n"),
               ("control.tar.gz", indexlab._tar({"./control": control})),
               ("data.tar.gz", indexlab._tar(data))]
    out = b"!<arch>\n"
    for member, content in members:
        header = f"{member + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}`\n"
        out += header.encode("ascii") + content + (b"\n" if len(content) % 2 else b"")
    return out


def entry_for(version, ipk, **changes):
    entry = updatelab.release(version)
    entry.update(size=len(ipk), sha256=hashlib.sha256(ipk).hexdigest(), commit="e" * 40)
    entry.update(changes)
    return entry


class Box(updatehelper.Receiver):
    """A receiver whose clock, processes, init, opkg, OpenWebif and origin the test owns."""

    def __init__(self, tmp):
        super().__init__(root=str(tmp / "root"), proc=str(tmp / "proc"), init="/stand-in/init",
                         opkg="/stand-in/opkg")
        self.t = 1000.0
        self.events = []
        self.argv = []
        self.pids = {100}
        self.fds = {}
        self.service = TVP1
        self.standby = False
        self.webif_up = True
        self.hook_status = 404
        self.opkg_mode = "install"
        self.opkg_blocker = None
        self.urls = {}
        self.relayed = {}
        self.free = 1 << 40
        self.pauses = {}
        self.zaps = []
        self.power = []
        self.seen = []
        self.init_log = []
        self.lastservice_at_start = None
        self.fetched = []
        self.stops = True
        self.zap_works = True
        self.start_fails = False

    # time
    def clock(self):
        return self.t

    def now(self):
        return 1790410000 + int(self.t)

    def sleep(self, seconds):
        self.t += seconds
        for event in [e for e in self.events if e[0] <= self.t]:
            self.events.remove(event)
            event[1]()

    def at(self, delay, action):
        self.events.append((self.t + delay, action))

    def boot_id(self):
        return "boot-1"

    def uptime(self):
        return self.t

    # processes
    unreadable = 0  # how many of the next looks at /proc fail to list it

    def enigma2_pids(self):
        # As the receiver's own: a /proc that cannot be listed reads as no enigma2 at all.
        if self.unreadable:
            self.unreadable -= 1
            return set()
        return set(self.pids)

    def enigma2_look(self):
        if self.unreadable:
            self.unreadable -= 1
            return None
        return self.enigma2_pids()

    def open_files(self, pid):
        return set(self.fds.get(pid, ()))

    def restart(self, pid=200):
        self.pids = {pid}

    # programs
    def run(self, argv, timeout):
        assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
        self.argv.append(list(argv))
        if argv[0] == self.init:
            self.init_log.append((argv[1], self.read(PLUGIN + "/plugin.py")))
            if argv[1] == "4" and self.stops:
                self.pids = set()
                self.webif_up = False
            elif argv[1] == "3":
                if self.pids and not self.stops:
                    # An `init 4` that took no effect left the runlevel where it was: `init 3`
                    # starts nothing, and the interface that never stopped runs on.
                    return 0
                self.lastservice_at_start = self.setting("config.tv.lastservice")
                if self.start_fails:
                    return 1
                self.pids = {300}
                self.webif_up = True
                self.service = self.lastservice_at_start or TVN
            return 0
        if argv[0] == self.opkg:
            if self.opkg_blocker is not None:
                self.opkg_blocker()
            if self.opkg_mode == "fail":
                self.last_output = "Collected errors:\n * something went wrong"
                return 255
            self.install(argv[-1])
            if self.opkg_mode == "partial":
                with open(self.path(PLUGIN + "/plugin.py"), "w") as handle:
                    handle.write("# garbage\n")
            return 0
        raise AssertionError("unexpected program " + repr(argv))

    def install(self, ipk):
        with open(ipk, "rb") as handle:
            members = dict(updatehelper.ar_members(handle.read()))
        with tarfile.open(fileobj=io.BytesIO(members["data.tar.gz"]), mode="r:gz") as data:
            for member in data.getmembers():
                relative = updatehelper._member_path(member.name)
                if not relative:
                    continue
                target = self.path(relative)
                if member.isdir():
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with open(target, "wb") as handle:
                        handle.write(data.extractfile(member).read())
        with tarfile.open(fileobj=io.BytesIO(members["control.tar.gz"]), mode="r:gz") as control:
            text = control.extractfile("./control").read().decode()
        version = next(line.split(": ")[1] for line in text.splitlines()
                       if line.startswith("Version"))
        write_status(self, version)

    # OpenWebif on the receiver
    def webif(self, path):
        self.seen.append(path)
        if not self.webif_up:
            return None
        if path == "/api/statusinfo":
            return 200, json.dumps({"currservice_serviceref": self.service,
                                    "inStandby": "true" if self.standby else "false"}).encode()
        if path.startswith("/api/zap?sRef="):
            from urllib.parse import unquote

            self.zaps.append(unquote(path.split("=", 1)[1]))
            if self.zap_works:
                self.service = self.zaps[-1]
            return 200, b'{"result": true}'
        if path.startswith("/api/powerstate?newstate="):
            self.power.append(path.rsplit("=", 1)[1])
            self.standby = True
            return 200, b"{}"
        if path == "/mqttbridge":
            return self.hook_status, b""
        return 404, b""

    def fetch_origin(self, url, cap, timeout):
        self.fetched.append(url)
        answer = self.urls.get(url)
        if answer is None:
            raise updatehelper.netfetch.Unreachable("no route to host")
        return answer

    def fetch_relay(self, url, cap, timeout):
        answer = self.relayed.get(url)
        if answer is None:
            raise updatehelper.netfetch.Unreachable("connection refused")
        return answer

    def free_bytes(self, path):
        return self.free

    def pause(self, name):
        action = self.pauses.get(name)
        if action is not None:
            action()

    # reading the root
    def read(self, relative):
        try:
            with open(self.path(relative)) as handle:
                return handle.read()
        except OSError:
            return None

    def setting(self, key):
        for line in (self.read(updatehelper.SETTINGS) or "").splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        return None


def write_status(box, version):
    status, info = updatehelper.opkg_paths(box.root)
    stanzas = [
        "Package: busybox\nVersion: 1.36\nStatus: install ok installed",
        "Package: python3-core\nVersion: 3.12\nStatus: install ok installed",
        f"Package: {updatehelper.PACKAGE}\nVersion: {version}\nStatus: install ok installed",
    ]
    Path(status).write_text("\n\n".join(stanzas) + "\n")
    Path(info).mkdir(parents=True, exist_ok=True)
    Path(info, updatehelper.PACKAGE + ".control").write_text(f"Version: {version}\n")
    Path(info, updatehelper.PACKAGE + ".list").write_text("/" + PLUGIN + "/plugin.py\n")


class Scene:
    """A receiver with OLD installed and running, and an index offering NEW."""

    def __init__(self, tmp, *, target=NEW, started_by="mqtt", downgrade=False, relay=None,
                 entries=None, request=None, acceptance=True, ipk=None, floor="0.2.0",
                 integration=None, integration_mode=False):
        self.tmp = tmp
        self.box = Box(tmp)
        root = Path(self.box.root)
        (root / PLUGIN).mkdir(parents=True)
        (root / PLUGIN / "plugin.py").write_text(f"# plugin {OLD}\n")
        (root / PLUGIN / "__init__.py").write_text("")
        (root / HOOK).parent.mkdir(parents=True)
        (root / HOOK).write_text(f"# hook {OLD}\n")
        (root / "var/lib/opkg").mkdir(parents=True)
        write_status(self.box, OLD)
        (root / "etc/enigma2").mkdir(parents=True)
        (root / updatehelper.SETTINGS).write_text(
            "config.plugins.mqttbridge.enabled=true\nconfig.tv.lastservice=" + TVN
            + "\nconfig.misc.x=1\n")
        (root / "home/root").mkdir(parents=True)
        (root / "tmp").mkdir()
        self.ipk = ipk if ipk is not None else package(target)
        self.entries = entries if entries is not None else [
            entry_for(target, self.ipk), updatelab.release(OLD),
        ]
        index_raw, signature = updatelab.signed(7, self.entries, floor=floor)
        self.box.urls[ORIGIN + trust.SIGNATURE_FILE] = (200, signature)
        self.box.urls[ORIGIN + trust.INDEX_FILE] = (200, index_raw)
        self.box.urls[ORIGIN + self.entries[0]["filename"]] = (200, self.ipk)
        self.directory = root / updatehelper.BACKUPS / ("update-" + TID)
        self.directory.mkdir(parents=True, mode=0o700)
        self.request = {
            "id": TID, "target": target, "sha256": self.entries[0]["sha256"],
            "relay": relay, "started_by": started_by, "downgrade": downgrade,
            "from": {"version": OLD, "commit": "a" * 40}, "enigma2_pid": 100,
            "keys": trust.keys_to_data(updatelab.TEST_KEYS), "acceptance": acceptance,
            "origin": ORIGIN, "contract": 1, "integration": integration,
            "integration_mode": integration_mode,
        }
        self.request.update(request or {})
        updatehelper.write_json(str(self.directory / "request.json"), self.request)
        self.transaction = updatehelper.Transaction(str(self.directory), self.box)
        self.transaction.beat_interval = 3600

    def plugin_word(self, restart=True, started=True, delay=5):
        """The plugin's part at `restarting`: ask, the interface restarts, the new one reports."""
        def act():
            if restart:
                updatehelper.write_json(str(self.directory / "restart.json"), {"pid": 100})
                self.box.at(delay, self.box.restart)
            if started:
                self.box.at(delay + 3, lambda: updatehelper.write_json(
                    str(self.directory / "started.json"),
                    {"version": NEW, "commit": "e" * 40, "pid": 200}))
        self.box.pauses["restarting"] = act

    def run(self):
        return self.transaction.run()

    def status(self):
        path = self.directory / "status.json"
        if path.exists():
            return json.loads(path.read_text())
        return self.transaction.status

    def last(self):
        return json.loads((Path(self.box.root) / updatehelper.LAST).read_text())

    def marker(self):
        path = Path(self.box.root) / updatehelper.MARKER
        return json.loads(path.read_text()) if path.exists() else None

    def plugin_py(self):
        return self.box.read(PLUGIN + "/plugin.py")

    def locked(self):
        return (Path(self.box.root) / updatehelper.BACKUPS / updatehelper.LOCK_NAME).exists()

    def opkg_calls(self):
        return [a for a in self.box.argv if a[0] == self.box.opkg]

    def init_calls(self):
        return [a[1] for a in self.box.argv if a[0] == self.box.init]


# ---------------------------------------------------------------- the whole path --


def test_an_upgrade_proved_by_the_new_plugin_commits(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word()
    assert scene.run() == 0
    last = scene.last()
    assert (last["result"], last["reason"], last["target"], last["from"]) == (
        "installed", None, NEW, OLD)
    assert scene.plugin_py() == f"# plugin {NEW}\n"
    assert not scene.locked()
    # Committed: the transaction directory goes, one snapshot is kept, the marker says so.
    assert not scene.directory.exists()
    assert (Path(scene.box.root) / updatehelper.BACKUPS / ("self-update-" + TID)).is_dir()
    marker = scene.marker()
    assert (marker["phase"], marker["result"], marker["to"]) == (
        "finished", "installed", {"version": NEW, "commit": "e" * 40})
    assert scene.opkg_calls() == [["/stand-in/opkg", "install", "--force-reinstall",
                                   str(scene.directory / entry_for(NEW, scene.ipk)["filename"])]]
    assert scene.init_calls() == []


def test_the_marker_is_written_before_the_restart_is_handed_to_the_plugin(tmp_path):
    scene = Scene(tmp_path)
    seen = {}

    def at_restarting():
        seen["marker"] = scene.marker()
        seen["status"] = json.loads((scene.directory / "status.json").read_text())
        scene.box.pauses.pop("restarting")
    scene.box.pauses["restarting"] = at_restarting
    scene.run()
    assert seen["status"]["phase"] == "restarting"
    marker = seen["marker"]
    assert marker["phase"] == "restarting" and marker["id"] == TID
    assert marker["from"] == {"version": OLD, "commit": "a" * 40}
    assert marker["boot_id"] == "boot-1"
    assert marker["deadline"] > scene.box.uptime()


def test_the_question_answered_no_withdraws_without_a_restart(tmp_path):
    scene = Scene(tmp_path)

    def plugin_says_no():
        updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
        scene.box.at(60, lambda: (scene.directory / "withdraw").write_text(""))
    scene.box.pauses["restarting"] = plugin_says_no
    assert scene.run() == 0
    assert (scene.last()["result"], scene.last()["reason"]) == (
        "withdrawn_before_restart", "question")
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.box.read(HOOK) == f"# hook {OLD}\n"
    status, _info = updatehelper.opkg_paths(scene.box.root)
    assert f"Version: {OLD}" in Path(status).read_text()
    assert scene.init_calls() == [] and not scene.locked()
    # The withdraw never touches the settings block: the running interface owns it.
    assert scene.box.setting("config.tv.lastservice") == TVN


def test_no_word_from_the_plugin_withdraws_after_180_s(tmp_path):
    scene = Scene(tmp_path)
    started = {}

    def asked():
        started["t"] = scene.box.t
        updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
    scene.box.pauses["restarting"] = asked
    assert scene.run() == 0
    assert scene.last()["result"] == "withdrawn_before_restart"
    assert 180 <= scene.box.t - started["t"] <= 182
    assert scene.plugin_py() == f"# plugin {OLD}\n"


def test_a_restart_that_lands_during_the_withdraw_goes_to_r2_not_withdrawn(tmp_path):
    scene = Scene(tmp_path)
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
    scene.box.pauses["withdrawing"] = scene.box.restart
    scene.run()
    assert scene.last()["result"] == "rolled_back"
    assert scene.init_calls() == ["4", "3"]


def test_a_missing_proof_rolls_back_by_r2_and_restores_the_channel(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.service = TVP1  # playing; the saved lastservice is TVN (they differ)
    assert scene.run() == 1
    last = scene.last()
    assert (last["result"], last["reason"]) == ("rolled_back", "not_started")
    assert OLD in last["error"]
    assert scene.init_calls() == ["4", "3"]
    # The files were back before the interface started again, the settings block too, and the
    # recorded channel was written while it was stopped - so it started on it (H2 in the stub).
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")
    assert scene.box.lastservice_at_start == TVP1
    assert scene.box.zaps == []
    record = scene.status()["record"]
    assert (record["restart"], record["channel"], record["restore"]) == ("stopped", "kept",
                                                                         "done")
    assert not scene.locked()
    assert scene.marker()["result"] == "rolled_back"


def test_r3_zaps_back_once_when_the_image_ignored_lastservice(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.service = TVP1
    original = scene.box.run

    def image_ignores_lastservice(argv, timeout):
        code = original(argv, timeout)
        if argv[0] == scene.box.init and argv[1] == "3":
            scene.box.service = TVN
        return code
    scene.box.run = image_ignores_lastservice
    scene.run()
    assert scene.box.zaps == [TVP1]
    assert scene.status()["record"]["channel"] == "restored"


def test_r3_restores_standby_when_the_rollback_began_in_standby(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)

    def in_standby():
        scene.box.standby = True
    scene.box.pauses["proving"] = in_standby
    original = scene.box.run

    def comes_up_awake(argv, timeout):
        code = original(argv, timeout)
        if argv[0] == scene.box.init and argv[1] == "3":
            scene.box.standby = False
        return code
    scene.box.run = comes_up_awake
    scene.run()
    assert scene.box.power == ["5"]
    assert scene.status()["record"]["standby"] == "restored"


def test_r2_with_nothing_recorded_restores_no_channel(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)

    def silent():
        scene.box.webif_up = False
    # OpenWebif answers neither before the restart nor when the rollback begins.
    scene.box.pauses["installing"] = silent
    scene.box.pauses["proving"] = silent
    scene.run()
    assert scene.box.setting("config.tv.lastservice") == TVN
    assert scene.status()["record"]["channel"] == "not recorded"
    assert scene.box.zaps == []


# ------------------------------------------------------------------ the proof --


def test_tier_two_passes_on_an_fd_seen_on_any_poll(tmp_path):
    entries = None
    ipk = package(NEW)
    entries = [entry_for(NEW, ipk, self_update=False)]
    scene = Scene(tmp_path, ipk=ipk, entries=entries)
    scene.plugin_word(started=False)
    log = scene.box.path(updatehelper.LOG_PATHS[0])

    def rotate():
        scene.box.fds[200] = {log}
        scene.box.at(2, lambda: scene.box.fds.pop(200))
    scene.box.pauses["proving"] = lambda: scene.box.at(50, rotate)
    assert scene.run() == 0
    assert scene.status()["record"]["proof"] == "log_fd"


def test_tier_two_is_not_proved_by_a_log_line_alone(tmp_path):
    ipk = package(NEW)
    scene = Scene(tmp_path, ipk=ipk, entries=[entry_for(NEW, ipk, self_update=False)])
    scene.plugin_word(started=False)
    Path(scene.box.path(updatehelper.LOG_PATHS[0])).write_text("starting: plugin=0.4.1\n")
    assert scene.run() == 1
    assert scene.last()["result"] == "rolled_back"


def test_tier_two_uses_the_hook_when_both_log_paths_are_unwritable(tmp_path, monkeypatch):
    ipk = package(NEW)
    scene = Scene(tmp_path, ipk=ipk, entries=[entry_for(NEW, ipk, self_update=False)])
    scene.plugin_word(started=False)
    monkeypatch.setattr(scene.transaction, "any_log_writable", lambda: False)
    scene.box.hook_status = 200
    assert scene.run() == 0
    assert scene.status()["record"]["proof"] == "webif_hook"


def test_a_hook_answering_404_is_no_proof(tmp_path, monkeypatch):
    ipk = package(NEW)
    scene = Scene(tmp_path, ipk=ipk, entries=[entry_for(NEW, ipk, self_update=False)])
    scene.plugin_word(started=False)
    monkeypatch.setattr(scene.transaction, "any_log_writable", lambda: False)
    assert scene.run() == 1
    assert scene.last()["result"] == "rolled_back"
    assert "/mqttbridge" in scene.box.seen


def test_the_hook_is_not_asked_while_a_log_path_is_writable(tmp_path):
    ipk = package(NEW)
    scene = Scene(tmp_path, ipk=ipk, entries=[entry_for(NEW, ipk, self_update=False)])
    scene.plugin_word(started=False)
    scene.box.hook_status = 200
    assert scene.run() == 1
    assert "/mqttbridge" not in scene.box.seen


def test_tier_one_needs_the_signed_commit(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.pauses["proving"] = lambda: updatehelper.write_json(
        str(scene.directory / "started.json"), {"version": NEW, "commit": "f" * 40})
    assert scene.run() == 1
    assert scene.last()["result"] == "rolled_back"


# -------------------------------------------------------------- what is refused --


@pytest.mark.parametrize("change, reason", [
    (dict(target="0.9.9"), "unknown_version"),
    (dict(sha256="0" * 64), "checksum"),
])
def test_a_request_the_index_does_not_back_is_refused(tmp_path, change, reason):
    scene = Scene(tmp_path, request=change)
    assert scene.run() == 1
    assert scene.last()["reason"] == reason
    assert scene.plugin_py() == f"# plugin {OLD}\n" and not scene.locked()
    assert scene.opkg_calls() == []
    assert not list((Path(scene.box.root) / updatehelper.BACKUPS).glob("self-update-*"))


@pytest.mark.parametrize("entry_change, reason", [
    (dict(withdrawn="broken"), "withdrawn"),
    (dict(contract=2), "incompatible"),
    (dict(depends=["python3-missing"]), "depends"),
])
def test_the_index_rules_are_judged_again_by_the_helper(tmp_path, entry_change, reason):
    ipk = package(NEW)
    scene = Scene(tmp_path, ipk=ipk, entries=[entry_for(NEW, ipk, **entry_change)])
    assert scene.run() == 1
    assert scene.last()["reason"] == reason
    assert scene.opkg_calls() == []


def test_below_the_floor_is_refused(tmp_path):
    scene = Scene(tmp_path, floor="0.5.0")
    scene.run()
    assert scene.last()["reason"] == "below_floor"


def test_the_floor_itself_may_be_installed(tmp_path):
    scene = Scene(tmp_path, floor=NEW)
    scene.plugin_word()
    assert scene.run() == 0


def test_the_integrations_word_narrows_the_choice(tmp_path):
    ipk = package(NEW)
    scene = Scene(tmp_path, ipk=ipk, entries=[entry_for(NEW, ipk, min_integration="0.5.0")],
                  integration={"integration": "0.4.0", "contract": 1, "plugin_min": "0.2.0"})
    scene.run()
    assert scene.last()["reason"] == "incompatible"
    assert "Home Assistant integration" in scene.last()["error"]


def test_a_downgrade_over_mqtt_is_refused_even_if_the_request_says_downgrade(tmp_path):
    ipk = package("0.3.9")
    scene = Scene(tmp_path, target="0.3.9", ipk=ipk, downgrade=True, started_by="home_assistant")
    scene.run()
    assert scene.last()["reason"] == "downgrade"
    assert scene.opkg_calls() == []


def test_a_downgrade_from_the_television_forces_it(tmp_path):
    ipk = package("0.3.9")
    scene = Scene(tmp_path, target="0.3.9", ipk=ipk, downgrade=True, started_by="screen")
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
    scene.run()
    assert "--force-downgrade" in scene.opkg_calls()[0]


def test_an_upgrade_never_forces_a_downgrade(tmp_path):
    scene = Scene(tmp_path, started_by="screen")
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
    scene.run()
    assert "--force-downgrade" not in scene.opkg_calls()[0]


@pytest.mark.parametrize("ipk, detail", [
    (package(NEW, control_version="0.4.2"), "version"),
    (package(NEW, name="enigma2-plugin-extensions-other"), "package is not"),
    (package(NEW, extra={"./etc/passwd": b"x"}), "outside the plugin"),
    (package(NEW, extra={"./" + PLUGIN + "/../../x.py": b"x"}), "unsafe"),
    (b"!<arch>\n", "three members"),
    (b"not an archive", "not an ar"),
])
def test_a_package_that_is_not_the_release_is_refused_before_opkg(tmp_path, ipk, detail):
    scene = Scene(tmp_path, ipk=ipk)
    scene.run()
    assert scene.last()["reason"] == "bad_package"
    assert detail in scene.last()["error"]
    assert scene.opkg_calls() == []


def test_a_package_of_another_size_or_checksum_is_refused(tmp_path):
    scene = Scene(tmp_path)
    scene.box.urls[ORIGIN + scene.entries[0]["filename"]] = (200, scene.ipk + b"x")
    scene.run()
    assert scene.last()["reason"] == "bad_package"


def test_not_enough_space_is_refused_before_the_snapshot(tmp_path):
    scene = Scene(tmp_path)
    scene.box.free = 2 * len(scene.ipk)
    scene.run()
    assert scene.last()["reason"] == "no_space"
    assert not list((Path(scene.box.root) / updatehelper.BACKUPS).glob("self-update-*"))


def test_the_origin_is_not_asked_when_home_assistant_relays(tmp_path):
    relay = {"url": "http://192.0.2.10:8123/api/enigma2_mqtt/relay/" + "a" * 43,
             "expires": 1790999999}
    scene = Scene(tmp_path, relay=relay)
    # The index the receiver holds (relayed earlier), and the package from the relay only.
    index_raw, signature = updatelab.signed(7, scene.entries)
    from MQTTBridge import trustfile

    trustfile.keep(scene.box.path(updatehelper.TRUST_FILE), updatelab.TEST_KEYS, True,
                   index_raw, signature, "relay", 1)
    scene.box.urls.clear()
    scene.box.relayed[relay["url"]] = (200, scene.ipk)
    scene.plugin_word()
    assert scene.run() == 0
    assert scene.status()["record"]["digest"] == "not_checked"
    # An install Home Assistant drives needs no internet on the receiver, and asks for none.
    assert scene.box.fetched == []


def test_no_index_at_all_is_refused(tmp_path):
    scene = Scene(tmp_path)
    scene.box.urls.clear()
    scene.run()
    assert scene.last()["reason"] == "unreachable"


def test_an_index_the_helper_fetches_is_kept_by_the_trust_files_rule(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word()
    scene.run()
    from MQTTBridge import trustfile

    state = trustfile.read_state(scene.box.path(updatehelper.TRUST_FILE))
    assert trustfile.held_part(state, "acceptance")["source"] == "origin"
    lock = scene.box.path("etc/enigma2/mqttbridge-index.lock")
    assert os.stat(lock).st_mode & 0o777 == 0o600


def test_the_release_digest_is_cross_checked_when_the_receiver_fetched_it(tmp_path):
    scene = Scene(tmp_path, acceptance=False)
    entry = scene.entries[0]
    api = updatehelper.DIGEST_API + NEW
    scene.box.urls[api] = (200, json.dumps({"assets": [
        {"name": entry["filename"], "digest": "sha256:" + "0" * 64}]}).encode())
    scene.run()
    assert scene.last()["reason"] == "bad_package"
    assert "digest" in scene.last()["error"]


def test_an_unanswered_digest_request_is_noted_not_fatal(tmp_path):
    scene = Scene(tmp_path, acceptance=False)
    scene.plugin_word()
    assert scene.run() == 0
    assert scene.status()["record"]["digest"] == "unavailable"


# ------------------------------------------------------------ opkg and its lock --


def test_opkg_failing_puts_the_old_files_back_without_a_restart(tmp_path):
    scene = Scene(tmp_path)
    scene.box.opkg_mode = "fail"
    scene.run()
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "opkg_failed")
    assert "something went wrong" in scene.last()["error"]
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.init_calls() == [] and not scene.locked()


def test_a_manifest_mismatch_puts_the_old_files_back(tmp_path):
    scene = Scene(tmp_path)
    scene.box.opkg_mode = "partial"
    scene.run()
    assert scene.last()["reason"] == "manifest"
    assert scene.plugin_py() == f"# plugin {OLD}\n"


def test_opkg_busy_is_refused_before_anything_changes(tmp_path):
    scene = Scene(tmp_path)
    lock = scene.box.path("run/opkg.lock")
    os.makedirs(os.path.dirname(lock))
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl, os, sys, time\n"
         "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
         "fcntl.lockf(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)\n", lock],
        stdout=subprocess.PIPE)
    try:
        assert holder.stdout.readline() == b"held\n"
        assert updatehelper.opkg_busy(scene.box)
        scene.run()
    finally:
        holder.kill()
        holder.wait()
    assert scene.last()["reason"] == "opkg_busy"
    assert scene.opkg_calls() == []
    assert not updatehelper.opkg_busy(scene.box)


# ------------------------------------------------------------------- the lock --


def lock_owner(scene):
    return json.loads((Path(scene.box.root) / updatehelper.BACKUPS / updatehelper.LOCK_NAME
                       / "owner.json").read_text())


def test_a_held_lock_is_busy_and_left_alone(tmp_path):
    scene = Scene(tmp_path)
    lock = Path(scene.box.root) / updatehelper.BACKUPS / updatehelper.LOCK_NAME
    lock.mkdir(mode=0o700)
    (lock / "owner.json").write_text(json.dumps(
        {"pid": 1, "started": 5, "boot_id": "boot-1", "uptime": scene.box.t - 60}))
    backups = Path(scene.box.root) / updatehelper.BACKUPS
    for name in ("update-aaaaaaaaaaaa", "update-bbbbbbbbbbbb", "update-cccccccccccc"):
        (backups / name).mkdir()
    assert scene.run() == 1
    assert scene.status()["reason"] == "busy"
    assert json.loads((lock / "owner.json").read_text())["pid"] == 1
    # A helper that never held the lock touches nothing of the transaction that does: not the
    # last-transaction record, not a transaction directory.
    assert not (Path(scene.box.root) / updatehelper.LAST).exists()
    assert all((backups / name).is_dir() for name in (
        "update-aaaaaaaaaaaa", "update-bbbbbbbbbbbb", "update-cccccccccccc"))


def test_a_lock_from_another_boot_is_reclaimed(tmp_path):
    scene = Scene(tmp_path)
    lock = Path(scene.box.root) / updatehelper.BACKUPS / updatehelper.LOCK_NAME
    lock.mkdir(mode=0o700)
    (lock / "owner.json").write_text(json.dumps(
        {"pid": 1, "started": int(time.time()), "boot_id": "boot-0",
         "uptime": scene.box.t - 10}))
    # Fresh by the clock and by uptime: only the boot id says that its holder is gone.
    scene.plugin_word()
    assert scene.run() == 0


def test_the_owner_record_names_the_transaction(tmp_path):
    scene = Scene(tmp_path, started_by="home_assistant")
    seen = {}
    scene.box.pauses["downloading"] = lambda: seen.update(lock_owner(scene))
    scene.run()
    assert {k: seen[k] for k in ("origin", "id", "target", "boot_id")} == {
        "origin": "home_assistant", "id": TID, "target": NEW, "boot_id": "boot-1"}


def point_released_at(monkeypatch, box):
    monkeypatch.setattr(released, "BOOT_ID_PATH", Path(box.proc) / "boot_id")
    monkeypatch.setattr(released, "UPTIME_PATH", Path(box.proc) / "uptime")
    os.makedirs(box.proc, exist_ok=True)
    (Path(box.proc) / "boot_id").write_text("boot-1\n")

    def set_uptime(value):
        box.t = value
        (Path(box.proc) / "uptime").write_text(f"{value:.2f} 0.00\n")
    return set_uptime


def test_the_heartbeat_keeps_the_released_installer_from_reclaiming(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    set_uptime = point_released_at(monkeypatch, scene.box)
    set_uptime(1000.0)
    scene.transaction.load_request()
    scene.transaction.claim()
    lock = Path(scene.box.root) / updatehelper.BACKUPS / updatehelper.LOCK_NAME
    set_uptime(1000.0 + 31 * 60)
    assert released._is_stale(lock) != ""        # no beat: the released rule reclaims it
    assert scene.transaction.beat()
    assert released._is_stale(lock) == ""        # one beat: held, 31 minutes in


def test_the_heartbeat_thread_beats_while_opkg_blocks(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    set_uptime = point_released_at(monkeypatch, scene.box)
    set_uptime(1000.0)
    scene.transaction.beat_interval = 0.01
    lock = Path(scene.box.root) / updatehelper.BACKUPS / updatehelper.LOCK_NAME
    inside = threading.Event()
    go = threading.Event()
    verdicts = []

    def blocking_opkg():
        inside.set()
        assert go.wait(10)
    scene.box.opkg_blocker = blocking_opkg
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
    worker = threading.Thread(target=scene.run)
    worker.start()
    assert inside.wait(10)
    set_uptime(1000.0 + 31 * 60)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and released._is_stale(lock) != "":
        time.sleep(0.01)
    verdicts.append(released._is_stale(lock))
    go.set()
    worker.join(10)
    assert verdicts == [""]


def test_a_lock_taken_from_the_helper_stops_it_touching_the_receiver(tmp_path):
    scene = Scene(tmp_path)

    def taken():
        lock = Path(scene.box.root) / updatehelper.BACKUPS / updatehelper.LOCK_NAME
        (lock / "owner.json").write_text(json.dumps({"id": "ffffffffffff"}))
        scene.transaction.beat()
    scene.box.pauses["snapshot"] = taken
    assert scene.run() == 1
    assert scene.last()["result"] == "interrupted"
    assert scene.opkg_calls() == []
    assert json.loads((Path(scene.box.root) / updatehelper.BACKUPS / updatehelper.LOCK_NAME
                       / "owner.json").read_text())["id"] == "ffffffffffff"


# ---------------------------------------------------------- signals and bounds --


def test_a_signal_before_opkg_ends_it_with_nothing_changed(tmp_path):
    scene = Scene(tmp_path)
    scene.box.pauses["installing"] = lambda: scene.transaction.on_signal(signal.SIGTERM)
    scene.run()
    assert scene.last()["result"] == "interrupted"
    assert scene.opkg_calls() == [] and not scene.locked()
    assert not list((Path(scene.box.root) / updatehelper.BACKUPS).glob("self-update-*"))


def test_a_signal_after_opkg_puts_the_old_files_back(tmp_path):
    scene = Scene(tmp_path)
    scene.box.pauses["opkg_done"] = lambda: scene.transaction.on_signal(signal.SIGHUP)
    scene.run()
    assert scene.last()["result"] == "interrupted"
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.init_calls() == []


def test_a_signal_while_waiting_for_the_restart_withdraws(tmp_path):
    scene = Scene(tmp_path)
    scene.box.pauses["restarting"] = lambda: scene.transaction.on_signal(signal.SIGTERM)
    scene.run()
    assert scene.last()["result"] == "interrupted"
    assert scene.plugin_py() == f"# plugin {OLD}\n"


@pytest.mark.parametrize("point", ["rollback_recorded", "rollback_stopped", "rollback_restored"])
def test_a_signal_inside_r2_never_starts_the_interface_over_an_unrestored_tree(tmp_path, point):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.pauses[point] = lambda: scene.transaction.on_signal(signal.SIGTERM)
    scene.run()
    assert scene.init_calls() == ["4", "3"]
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")
    assert scene.last()["result"] == "rolled_back"


def test_the_forward_path_gives_up_at_its_bound(tmp_path):
    scene = Scene(tmp_path)

    def slow():
        scene.box.t += updatehelper.FORWARD_LIMIT + 1
    scene.box.pauses["verifying"] = slow
    scene.run()
    assert scene.last()["reason"] == "time_limit" and not scene.locked()
    assert scene.opkg_calls() == []


def test_a_failed_start_is_rolled_back_within_its_bound(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    marks = {}
    scene.box.pauses["rollback_recorded"] = lambda: marks.setdefault("start", scene.box.t)
    scene.run()
    assert scene.box.t - marks["start"] < updatehelper.ROLLBACK_LIMIT


# -------------------------------------------------------------------- pruning --


def test_only_self_update_snapshots_are_pruned_and_the_own_one_kept(tmp_path):
    scene = Scene(tmp_path)
    backups = Path(scene.box.root) / updatehelper.BACKUPS
    for index, name in enumerate(("self-update-aaaaaaaaaaaa", "self-update-bbbbbbbbbbbb",
                                  "ha-installer-cccccccccccc", "self-update-extra", "mine")):
        (backups / name).mkdir()
        os.utime(backups / name, (1000 + index, 1000 + index))
    scene.plugin_word()
    scene.run()
    left = sorted(p.name for p in backups.iterdir() if not p.name.startswith("."))
    assert left == ["ha-installer-cccccccccccc", "mine", "self-update-" + TID,
                    "self-update-bbbbbbbbbbbb", "self-update-extra"]


# ------------------------------------------------------------ receiver reading --


def test_enigma2_is_found_by_its_process_name(tmp_path):
    proc = tmp_path / "proc"
    for pid, name in ((1, "init"), (812, "enigma2"), (813, "enigma2.sh"), (900, "python3")):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "comm").write_text(name + "\n")
    (proc / "self").mkdir()
    receiver = updatehelper.Receiver(root=str(tmp_path), proc=str(proc))
    assert receiver.enigma2_pids() == {812}
    (proc / "812" / "fd").mkdir()
    os.symlink("/home/root/mqttbridge.log", proc / "812" / "fd" / "7")
    assert receiver.open_files(812) == {"/home/root/mqttbridge.log"}


def test_programs_run_by_argv_and_never_through_a_shell(tmp_path, monkeypatch):
    calls = []

    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"", b"")
    monkeypatch.setattr(updatehelper.subprocess, "run", fake)
    updatehelper.Receiver(root=str(tmp_path)).run(["/sbin/init", "3"], 5)
    assert calls[0][0] == ["/sbin/init", "3"]
    assert not calls[0][1].get("shell")


def test_the_helpers_modules_import_nothing_of_the_plugins():
    source = Path(updatehelper.__file__).parent
    for name in ("updatehelper.py",) + updatehelper.COPIED_MODULES:
        text = (source / name).read_text()
        for line in text.splitlines():
            if line.startswith(("import ", "from ")) and "from ." in line:
                pytest.fail(name + " imports from the plugin outside a fallback: " + line)


# ------------------------------------------------- the helper as a real process --


INIT_STAND_IN = """#!/bin/sh
# init stand-in: runlevel 4 removes enigma2 from the fake /proc, 3 starts a new one.
echo "$1 $(cat "$ROOT/{plugin}/plugin.py")" >> "$LOG"
if [ "$1" = 4 ]; then rm -rf "$PROC/100" "$PROC/200"; fi
if [ "$1" = 3 ]; then mkdir -p "$PROC/300"; echo enigma2 > "$PROC/300/comm"; fi
"""

OPKG_STAND_IN = """#!/bin/sh
# opkg stand-in: installs the new plugin.py.
echo "# plugin {new}" > "$ROOT/{plugin}/plugin.py"
echo "# hook {new}" > "$ROOT/{hook}"
"""


def shells():
    found = []
    for shell in ("/bin/sh", "/bin/bash", "/bin/dash"):
        if os.path.exists(shell):
            found.append([shell])
    if shutil.which("busybox"):
        found.append([shutil.which("busybox"), "sh"])
    return found


@pytest.mark.parametrize("shell", shells(), ids=lambda s: " ".join(s))
def test_a_real_sigterm_inside_r2_waits_for_the_restore(tmp_path, shell):
    """The receiver's way: a copy in its own directory, its own process, a real signal."""
    ipk = package(NEW)
    token = "/api/enigma2_mqtt/relay/" + "t" * 43
    scene = Scene(tmp_path, ipk=ipk, request={"relay": {"url": "http://127.0.0.1:9" + token,
                                                        "expires": int(time.time()) + 600}})
    root = Path(scene.box.root)
    proc = tmp_path / "proc"
    (proc / "100").mkdir(parents=True)
    (proc / "100" / "comm").write_text("enigma2\n")
    index_raw, signature = updatelab.signed(7, scene.entries)
    from MQTTBridge import trustfile

    trustfile.keep(str(root / updatehelper.TRUST_FILE), updatelab.TEST_KEYS, True, index_raw,
                   signature, "relay", 1)
    import http.server

    class Serve(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(ipk)))
            self.end_headers()
            self.wfile.write(ipk)

        def log_message(self, *args):
            pass
    server = http.server.HTTPServer(("127.0.0.1", 0), Serve)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    request = json.loads((scene.directory / "request.json").read_text())
    request["relay"]["url"] = f"http://127.0.0.1:{server.server_port}{token}"
    updatehelper.write_json(str(scene.directory / "request.json"), request)
    source = Path(updatehelper.__file__).parent
    shutil.copy(source / "updatehelper.py", scene.directory / "helper.py")
    for name in updatehelper.COPIED_MODULES:
        shutil.copy(source / name, scene.directory / name)
    stand = tmp_path / "stand-in"
    stand.mkdir()
    for name, text in (("init", INIT_STAND_IN), ("opkg", OPKG_STAND_IN)):
        script = stand / name
        script.write_text(text.format(plugin=PLUGIN, hook=HOOK, new=NEW))
        script.chmod(0o755)
    wrappers = {}
    for name in ("init", "opkg"):
        wrapper = stand / (name + "-run")
        wrapper.write_text("#!/bin/sh\nexec " + " ".join(shell) + " " + str(stand / name)
                           + ' "$@"\n')
        wrapper.chmod(0o755)
        wrappers[name] = str(wrapper)
    fifos = {}
    for name in ("restarting", "rollback_stopped"):
        fifos[name] = str(tmp_path / (name + ".fifo"))
        os.mkfifo(fifos[name])
    config = {"root": str(root), "proc": str(proc), "init": wrappers["init"],
              "opkg": wrappers["opkg"], "webif": "http://127.0.0.1:9", "pauses": fifos,
              "constants": {"POLL": 0.05, "PROOF_POLL": 0.05, "PROOF_WINDOW": 0.5,
                            "PLUGIN_WAIT": 10, "STOP_WAIT": 5, "START_WAIT": 5,
                            "SERVICE_WAIT": 0.2, "EFFECT_WAIT": 0.2}}
    (tmp_path / "config.json").write_text(json.dumps(config))
    environment = dict(os.environ, ROOT=str(root), PROC=str(proc), LOG=str(tmp_path / "init.log"))
    helper = subprocess.Popen([sys.executable, str(scene.directory / "helper.py"), TID, "--test",
                               str(tmp_path / "config.json")], env=environment,
                              cwd=str(tmp_path))
    try:
        with open(fifos["restarting"], "w") as release:
            # The plugin asked and the interface restarted; the new one never reports.
            updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
            shutil.rmtree(proc / "100")
            (proc / "200").mkdir()
            (proc / "200" / "comm").write_text("enigma2\n")
            release.write("go")
        with open(fifos["rollback_stopped"], "w") as release:
            helper.send_signal(signal.SIGTERM)
            time.sleep(0.2)
            release.write("go")
        assert helper.wait(30) == 1
    finally:
        if helper.poll() is None:
            helper.kill()
        server.shutdown()
    lines = (tmp_path / "init.log").read_text().splitlines()
    assert lines == [f"4 # plugin {NEW}", f"3 # plugin {OLD}"]
    last = json.loads((root / updatehelper.LAST).read_text())
    assert last["result"] == "rolled_back"
    assert not (root / updatehelper.BACKUPS / updatehelper.LOCK_NAME).exists()


# ------------------------------------------------------- the settings block --


def household_changes_a_plugin_setting(scene):
    path = Path(scene.box.path(updatehelper.SETTINGS))
    path.write_text(path.read_text().replace("mqttbridge.enabled=true", "mqttbridge.enabled=false"))


def test_the_withdraw_leaves_the_settings_block_to_the_running_interface(tmp_path):
    scene = Scene(tmp_path)

    def meanwhile():
        household_changes_a_plugin_setting(scene)
        (scene.directory / "withdraw").write_text("")
    scene.box.pauses["restarting"] = meanwhile
    scene.run()
    assert scene.last()["result"] == "withdrawn_before_restart"
    assert scene.box.setting("config.plugins.mqttbridge.enabled") == "false"


def test_r2_puts_the_settings_block_back_once_the_interface_is_stopped(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.pauses["proving"] = lambda: household_changes_a_plugin_setting(scene)
    scene.run()
    assert scene.last()["result"] == "rolled_back"
    assert scene.box.setting("config.plugins.mqttbridge.enabled") == "true"
    assert scene.box.setting("config.misc.x") == "1"


def test_r2_writes_nothing_into_the_settings_of_an_interface_that_did_not_stop(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.stops = False
    scene.box.pauses["proving"] = lambda: household_changes_a_plugin_setting(scene)
    scene.run()
    assert scene.box.setting("config.tv.lastservice") == TVN
    assert scene.box.setting("config.plugins.mqttbridge.enabled") == "false"
    # The files still went back, and the interface was still started.
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.init_calls() == ["4", "3"]


def test_the_integrations_contract_must_match(tmp_path):
    scene = Scene(tmp_path, integration={"integration": "0.4.0", "contract": 2,
                                         "plugin_min": "0.2.0"})
    scene.run()
    assert scene.last()["reason"] == "incompatible"


def test_the_integrations_floor_raises_the_index_floor(tmp_path):
    scene = Scene(tmp_path, integration={"integration": "0.4.0", "contract": 1,
                                         "plugin_min": "0.5.0"})
    scene.run()
    assert scene.last()["reason"] == "below_floor"


def test_without_the_integrations_word_a_needed_integration_is_unmet_in_integration_mode(
        tmp_path):
    ipk = package(NEW)
    scene = Scene(tmp_path, ipk=ipk, entries=[entry_for(NEW, ipk, min_integration="0.4.0")],
                  integration_mode=True)
    scene.run()
    assert scene.last()["reason"] == "incompatible"


def test_without_the_integrations_word_a_needed_integration_is_met_in_discovery_mode(tmp_path):
    ipk = package(NEW)
    scene = Scene(tmp_path, ipk=ipk, entries=[entry_for(NEW, ipk, min_integration="0.4.0")])
    scene.plugin_word()
    assert scene.run() == 0


# ------------------------------------------------ which process is the restart --


def tier_two(tmp_path, **kwargs):
    ipk = package(NEW)
    return Scene(tmp_path, ipk=ipk, entries=[entry_for(NEW, ipk, self_update=False)], **kwargs)


@pytest.mark.parametrize("tier", [1, 2])
def test_an_interface_restart_before_opkg_ends_it_with_nothing_changed(tmp_path, tier):
    scene = tier_two(tmp_path) if tier == 2 else Scene(tmp_path)
    log = scene.box.path(updatehelper.LOG_PATHS[0])

    def household_restarts_the_interface():
        # A crash and respawn, or a restart from the menu, while the package is downloaded:
        # the new process loaded the old code and holds the old plugin's log open.
        scene.box.pids = {150}
        scene.box.fds[150] = {log}
    scene.box.pauses["verifying"] = household_restarts_the_interface
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("interrupted", "interrupted")
    assert "restarted" in scene.last()["error"]
    assert scene.opkg_calls() == [] and scene.init_calls() == []
    assert scene.plugin_py() == f"# plugin {OLD}\n" and not scene.locked()
    assert not list((Path(scene.box.root) / updatehelper.BACKUPS).glob("self-update-*"))


def test_an_interface_restart_during_opkg_is_rolled_back_not_proved(tmp_path):
    scene = tier_two(tmp_path)
    log = scene.box.path(updatehelper.LOG_PATHS[0])
    asked = []

    def restart_while_opkg_unpacks():
        scene.box.pids = {150}
        scene.box.fds[150] = {log}
    scene.box.opkg_blocker = restart_while_opkg_unpacks
    scene.box.pauses["restarting"] = lambda: asked.append(True)
    assert scene.run() == 1
    # The process that started meanwhile may hold either version, or a mix: it is stopped and
    # the old files go back, and the plugin is never told to ask for a restart.
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "interrupted")
    assert scene.init_calls() == ["4", "3"] and asked == []
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")


def test_a_failure_after_an_unasked_restart_stops_the_interface_before_restoring(tmp_path):
    scene = Scene(tmp_path)
    scene.box.opkg_mode = "partial"

    def restart_while_opkg_unpacks():
        scene.box.pids = {150}
    scene.box.opkg_blocker = restart_while_opkg_unpacks
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "manifest")
    # R2 straight away: the files go back only once the running process is stopped.
    assert scene.box.init_log[0] == ("4", "# garbage\n")
    assert scene.plugin_py() == f"# plugin {OLD}\n"


def test_a_short_lived_enigma2_child_is_not_the_restart(tmp_path):
    scene = Scene(tmp_path)

    def plugin_asks_then_the_household_says_no():
        updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
        # enigma2 forks for a console command; the child keeps the name until it execs.
        scene.box.pids = {100, 101}
        scene.box.at(0.5, lambda: setattr(scene.box, "pids", {100}))
        scene.box.at(30, lambda: (scene.directory / "withdraw").write_text(""))
    scene.box.pauses["restarting"] = plugin_asks_then_the_household_says_no
    assert scene.run() == 0
    assert scene.last()["result"] == "withdrawn_before_restart"
    assert scene.init_calls() == []
    assert scene.plugin_py() == f"# plugin {OLD}\n"


def test_the_proof_ignores_a_process_that_ran_before_the_restart(tmp_path):
    scene = tier_two(tmp_path)
    log = scene.box.path(updatehelper.LOG_PATHS[0])

    def a_child_holds_the_old_log():
        # A child enigma2 forked before the restart inherited the old process's log descriptor.
        scene.box.pids = {100, 101}
        scene.box.fds[101] = {log}
        updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
        scene.box.at(5, lambda: setattr(scene.box, "pids", {101, 200}))
    scene.box.pauses["restarting"] = a_child_holds_the_old_log
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    assert scene.status()["record"]["proof"] == "none"


def test_the_new_process_proves_itself_beside_a_child_of_the_old_one(tmp_path):
    scene = tier_two(tmp_path)
    log = scene.box.path(updatehelper.LOG_PATHS[0])

    def restart_with_a_child_left():
        scene.box.pids = {100, 101}
        updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
        scene.box.at(5, lambda: setattr(scene.box, "pids", {101, 200}))
        scene.box.at(9, lambda: scene.box.fds.update({200: {log}}))
    scene.box.pauses["restarting"] = restart_with_a_child_left
    assert scene.run() == 0
    assert scene.status()["record"]["proof"] == "log_fd"


# ------------------------------------------- nothing unproven is left behind --


def fail_marker_at(monkeypatch, phase):
    original = updatehelper.write_json

    def failing(path, value, mode=0o600):
        if path.endswith("mqttbridge-update.json") and value.get("phase") == phase:
            raise OSError(28, "No space left on device")
        return original(path, value, mode)
    monkeypatch.setattr(updatehelper, "write_json", failing)


@pytest.mark.parametrize("phase", ["restarting", "proving", "rolling_back"])
def test_a_marker_that_cannot_be_written_after_opkg_never_leaves_new_code(
        tmp_path, monkeypatch, phase):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    fail_marker_at(monkeypatch, phase)
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.box.pids and not scene.locked()
    assert scene.init_calls() == ["4", "3"]


@pytest.mark.parametrize("error", [RuntimeError("boom"), OSError(5, "Input/output error")])
def test_an_unexpected_error_after_opkg_puts_the_old_files_back(tmp_path, monkeypatch, error):
    scene = Scene(tmp_path)

    def broken(_path):
        raise error
    monkeypatch.setattr(updatehelper, "sha256_file", broken)
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "internal_error")
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.status()["record"]["restore"] == "done"
    assert scene.init_calls() == [] and not scene.locked()


def test_an_unexpected_error_while_waiting_for_the_restart_withdraws(tmp_path):
    scene = Scene(tmp_path)

    def broken():
        raise RuntimeError("boom")
    scene.box.pauses["restarting"] = broken
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "internal_error")
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.init_calls() == [] and not scene.locked()


def test_an_unexpected_error_after_the_restart_rolls_back(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)

    def broken():
        scene.box.pauses.pop("proving")
        raise RuntimeError("boom")
    scene.box.pauses["proving"] = broken
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "internal_error")
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.init_calls() == ["4", "3"] and not scene.locked()
    # Nothing was put back under the new interface before it was stopped.
    assert scene.box.init_log[0] == ("4", f"# plugin {NEW}\n")


def test_an_error_inside_the_restore_of_r2_still_starts_the_interface(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)

    def boom(*_args, **_kwargs):
        raise MemoryError()
    scene.box.pauses["rollback_stopped"] = lambda: monkeypatch.setattr(
        updatehelper, "restore_snapshot", boom)
    assert scene.run() == 1
    assert scene.init_calls() == ["4", "3"] and scene.box.pids
    assert scene.last()["result"] == "failed" and not scene.locked()
    assert scene.status()["record"]["restore"].startswith("failed")


class Escape(BaseException):
    """Something no handler expects - not even `except Exception`."""


def test_whatever_escapes_r2_after_init_4_still_runs_init_3(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)

    def escapes():
        raise Escape()
    scene.box.pauses["rollback_stopped"] = escapes
    with pytest.raises(Escape):
        scene.run()
    assert scene.init_calls() == ["4", "3"] and scene.box.pids
    assert not scene.locked()


# ----------------------------------------------------- the marker before opkg --


def test_the_marker_is_written_before_opkg_runs(tmp_path):
    scene = Scene(tmp_path)
    seen = {}
    scene.box.opkg_blocker = lambda: seen.update(marker=scene.marker())
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
    scene.run()
    assert seen["marker"]["phase"] == "installing" and seen["marker"]["id"] == TID
    assert seen["marker"]["to"] == {"version": NEW, "commit": "e" * 40}


def test_no_marker_before_opkg_means_no_opkg(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    fail_marker_at(monkeypatch, "installing")
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "internal_error")
    assert scene.opkg_calls() == [] and scene.plugin_py() == f"# plugin {OLD}\n"
    assert not list((Path(scene.box.root) / updatehelper.BACKUPS).glob("self-update-*"))


# ------------------------------------------------------ the index in memory --


def hold_index(scene, serial, entries):
    from MQTTBridge import trustfile

    index_raw, signature = updatelab.signed(serial, entries)
    trustfile.keep(scene.box.path(updatehelper.TRUST_FILE), updatelab.TEST_KEYS, True,
                   index_raw, signature, "relay", 1)


@pytest.mark.parametrize("storage", ["write_failed", "trust_busy"])
def test_a_newer_index_that_cannot_be_stored_still_decides(tmp_path, monkeypatch, storage):
    from MQTTBridge import trustfile

    ipk = package(NEW)
    entries = [entry_for(NEW, ipk)]
    scene = Scene(tmp_path, ipk=ipk, entries=entries)
    hold_index(scene, 7, entries)
    index_raw, signature = updatelab.signed(8, [dict(entries[0], withdrawn="breaks the tuner")])
    scene.box.urls[ORIGIN + trust.SIGNATURE_FILE] = (200, signature)
    scene.box.urls[ORIGIN + trust.INDEX_FILE] = (200, index_raw)
    if storage == "write_failed":
        monkeypatch.setattr(trustfile, "write_state", lambda path, state: False)
    else:
        def busy(path, wait):
            raise trustfile.TrustBusy("held")
        monkeypatch.setattr(trustfile, "trust_lock", busy)
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
    assert scene.run() == 1
    assert scene.last()["reason"] == "withdrawn"
    assert scene.opkg_calls() == []
    record = scene.status()["record"]
    assert (record["index_verdict"], record["index_kept"]) == (storage, False)


# ------------------------------------------------ a second helper, same id --


def test_a_second_helper_with_the_same_id_writes_nothing_into_the_first(tmp_path):
    scene = Scene(tmp_path)
    seen = {}

    def second():
        scene.box.pauses.pop("snapshot")
        other = updatehelper.Transaction(str(scene.directory), scene.box)
        seen["rc"] = other.run()
        seen["status"] = json.loads((scene.directory / "status.json").read_text())
    scene.box.pauses["snapshot"] = second
    scene.plugin_word()
    assert scene.run() == 0
    assert seen["rc"] == 1
    assert (seen["status"]["phase"], seen["status"]["result"]) == ("snapshot", None)


def test_a_lock_whose_owner_cannot_be_read_yet_gets_nothing_written(tmp_path):
    scene = Scene(tmp_path)
    # A claimer between its mkdir and its first write - perhaps this transaction's own helper.
    (Path(scene.box.root) / updatehelper.BACKUPS / updatehelper.LOCK_NAME).mkdir(mode=0o700)
    assert scene.run() == 1
    assert not (scene.directory / "status.json").exists()


def test_losing_the_race_for_a_freed_lock_is_busy(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    backups = Path(scene.box.root) / updatehelper.BACKUPS
    lock = backups / updatehelper.LOCK_NAME
    lock.mkdir(mode=0o700)
    (lock / "owner.json").write_text(json.dumps({"boot_id": "boot-0", "id": "ffffffffffff"}))
    calls = []

    def stale(receiver, path):
        calls.append(path)
        if len(calls) == 2:
            # Another claimer takes the name the moment the stale lock was moved aside.
            lock.mkdir(mode=0o700)
            (lock / "owner.json").write_text(json.dumps({"id": "eeeeeeeeeeee"}))
        return "it was claimed before the receiver last rebooted"
    monkeypatch.setattr(updatehelper, "lock_is_stale", stale)
    assert scene.run() == 1
    assert scene.status()["reason"] == "busy"
    assert json.loads((lock / "owner.json").read_text())["id"] == "eeeeeeeeeeee"


def test_a_helper_refused_by_another_transactions_lock_says_busy(tmp_path):
    scene = Scene(tmp_path)
    lock = Path(scene.box.root) / updatehelper.BACKUPS / updatehelper.LOCK_NAME
    lock.mkdir(mode=0o700)
    (lock / "owner.json").write_text(json.dumps(
        {"pid": 1, "started": 5, "boot_id": "boot-1", "uptime": scene.box.t - 60,
         "id": "ffffffffffff"}))
    assert scene.run() == 1
    status = json.loads((scene.directory / "status.json").read_text())
    assert (status["phase"], status["result"], status["reason"]) == ("finished", "failed", "busy")


# -------------------------------------------------- the last transaction's end --


def test_the_last_record_carries_the_boot_and_its_uptime(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word()
    scene.run()
    last = scene.last()
    assert last["boot_id"] == "boot-1"
    assert last["uptime"] == scene.box.uptime()
    assert last["finished"] == scene.box.now()


# ----------------------------------------------------- forcing a downgrade --


@pytest.mark.parametrize("started_by", ["mqtt", "home_assistant", "screen", "page"])
def test_an_upgrade_never_forces_a_downgrade_whatever_the_request_says(tmp_path, started_by):
    scene = Scene(tmp_path, started_by=started_by, downgrade=True)
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
    scene.run()
    assert "--force-downgrade" not in scene.opkg_calls()[0]


# ----------------------------------------------------------- the relay address --


RELAY = "http://192.0.2.10:8123/api/enigma2_mqtt/relay/" + "a" * 43


@pytest.mark.parametrize("relay", [
    {"url": "http://198.51.100.9:22/anything?x=1", "expires": 1790999999},
    {"url": RELAY + "?x=1", "expires": 1790999999},
    {"url": RELAY[:-1], "expires": 1790999999},
    {"url": RELAY + "/", "expires": 1790999999},
    {"url": RELAY.replace("http://", "http://user@"), "expires": 1790999999},
    {"url": RELAY.replace("http://", "ftp://"), "expires": 1790999999},
    {"url": RELAY.replace(":8123", ":0"), "expires": 1790999999},
    {"url": RELAY.replace(":8123", ":99999"), "expires": 1790999999},
    {"url": RELAY.replace("192.0.2.10", "[2001:db8::1]"), "expires": 1790999999},
    {"url": RELAY, "expires": 1790000000},
    {"url": RELAY, "expires": True},
    {"url": RELAY, "expires": "1790999999"},
    {"url": RELAY},
], ids=["another path", "a query", "a short token", "a trailing slash", "user info", "ftp",
        "port 0", "port 99999", "ipv6", "expired", "expires bool", "expires text",
        "no expiry"])
def test_a_relay_address_of_another_shape_or_expired_is_refused(tmp_path, relay):
    scene = Scene(tmp_path, relay=relay)
    hold_index(scene, 7, scene.entries)
    fetched = []
    scene.box.fetch_relay = lambda url, cap, timeout: fetched.append(url) or (404, b"")
    assert scene.run() == 1
    assert scene.last()["reason"] == "relay"
    assert "Home Assistant" in scene.last()["error"]
    assert fetched == [] and scene.opkg_calls() == []


@pytest.mark.parametrize("url", [
    RELAY,
    "https://192.0.2.10/api/enigma2_mqtt/relay/" + "-_" * 21 + "Z",
    "http://homeassistant.local:8123/api/enigma2_mqtt/relay/" + "b" * 43,
])
def test_a_relay_address_of_the_right_shape_is_fetched(tmp_path, url):
    scene = Scene(tmp_path, relay={"url": url, "expires": 1790999999})
    hold_index(scene, 7, scene.entries)
    scene.box.urls.clear()
    scene.box.relayed[url] = (200, scene.ipk)
    scene.plugin_word()
    assert scene.run() == 0


# ------------------------------------------------------------ the package --


def payload_with(member):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, body in (("./" + PLUGIN + "/plugin.py", b"# plugin 0.4.1\n"),
                           ("./" + HOOK, b"# hook\n")):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        tar.addfile(member)
    import gzip

    return gzip.compress(buffer.getvalue(), mtime=0)


def package_with(member):
    control = f"Package: {updatehelper.PACKAGE}\nVersion: {NEW}\n".encode()
    members = [("debian-binary", b"2.0\n"),
               ("control.tar.gz", indexlab._tar({"./control": control})),
               ("data.tar.gz", payload_with(member))]
    out = b"!<arch>\n"
    for name, content in members:
        header = f"{name + '/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(content):<10}`\n"
        out += header.encode("ascii") + content + (b"\n" if len(content) % 2 else b"")
    return out


def tar_member(name, kind, target=""):
    info = tarfile.TarInfo(name)
    info.type = kind
    info.linkname = target
    return info


@pytest.mark.parametrize("member, detail", [
    (tar_member("./" + PLUGIN + "/link.py", tarfile.SYMTYPE, "/etc/passwd"), "other than files"),
    (tar_member("./" + PLUGIN + "/hard.py", tarfile.LNKTYPE, "./" + PLUGIN + "/plugin.py"),
     "other than files"),
    (tar_member("./" + PLUGIN + "/tty", tarfile.CHRTYPE), "other than files"),
    (tar_member("./" + PLUGIN + "/disk", tarfile.BLKTYPE), "other than files"),
    (tar_member("./" + PLUGIN + "/pipe", tarfile.FIFOTYPE), "other than files"),
    (tar_member("/" + PLUGIN + "/abs.py", tarfile.REGTYPE), "unsafe"),
], ids=["symlink", "hardlink", "chardev", "blockdev", "fifo", "absolute"])
def test_a_package_holding_links_devices_or_absolute_paths_is_refused(tmp_path, member, detail):
    ipk = package_with(member)
    scene = Scene(tmp_path, ipk=ipk)
    scene.run()
    assert scene.last()["reason"] == "bad_package"
    assert detail in scene.last()["error"]
    assert scene.opkg_calls() == []


def test_a_request_whose_id_is_not_its_directorys_is_refused(tmp_path):
    scene = Scene(tmp_path, request={"id": "ba9876543210"})
    assert scene.run() == 2
    assert scene.status()["reason"] == "bad_request"
    assert scene.opkg_calls() == []


def test_a_failed_package_write_leaves_no_rollback_point(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    real = updatehelper.atomic_write

    def failing(path, data, mode=0o600):
        if path.endswith(".ipk"):
            raise OSError(28, "No space left on device")
        return real(path, data, mode)
    monkeypatch.setattr(updatehelper, "atomic_write", failing)
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "internal_error")
    assert "could not be written" in scene.last()["error"]
    assert not list((Path(scene.box.root) / updatehelper.BACKUPS).glob("self-update-*"))
    assert scene.opkg_calls() == [] and not scene.locked()


# ------------------------------------------------------- the lock, reclaimed --


def test_a_lock_refreshed_between_the_two_judgements_is_left_to_its_owner(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    lock = Path(scene.box.root) / updatehelper.BACKUPS / updatehelper.LOCK_NAME
    lock.mkdir(mode=0o700)
    owner = {"pid": 1, "started": 5, "boot_id": "boot-0", "uptime": 1.0, "id": "ffffffffffff"}
    (lock / "owner.json").write_text(json.dumps(owner))
    verdicts = iter(["it was claimed before the receiver last rebooted", ""])
    monkeypatch.setattr(updatehelper, "lock_is_stale", lambda receiver, path: next(verdicts))
    assert scene.run() == 1
    assert scene.status()["reason"] == "busy"
    assert json.loads((lock / "owner.json").read_text()) == owner


# ------------------------------------------------------- the withdraw, closely --


def test_the_withdraw_waits_for_opkgs_lock_and_leaves_the_files_to_it(tmp_path):
    scene = Scene(tmp_path)
    lock = scene.box.path("run/opkg.lock")
    holder = {}

    def opkg_runs_again():
        os.makedirs(os.path.dirname(lock), exist_ok=True)
        holder["p"] = subprocess.Popen(
            [sys.executable, "-c", "import fcntl, os, sys, time\n"
             "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT)\n"
             "fcntl.lockf(fd, fcntl.LOCK_EX)\nprint('held', flush=True)\ntime.sleep(60)\n",
             lock], stdout=subprocess.PIPE)
        assert holder["p"].stdout.readline() == b"held\n"
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
    scene.box.pauses["withdrawing"] = opkg_runs_again
    try:
        scene.run()
    finally:
        holder["p"].kill()
        holder["p"].wait()
    assert scene.last()["result"] == "failed"
    assert scene.status()["record"]["restore"].startswith("failed")
    assert scene.plugin_py() == f"# plugin {NEW}\n"


def test_the_plugins_ask_starts_the_180_s_again(tmp_path):
    scene = Scene(tmp_path)

    def the_plugin_asks_late():
        scene.box.at(100, lambda: updatehelper.write_json(
            str(scene.directory / "restart.json"), {"pid": 100}))
        scene.box.at(250, scene.box.restart)
        scene.box.at(253, lambda: updatehelper.write_json(
            str(scene.directory / "started.json"), {"version": NEW, "commit": "e" * 40,
                                                    "pid": 200}))
    scene.box.pauses["restarting"] = the_plugin_asks_late
    assert scene.run() == 0
    assert scene.last()["result"] == "installed"


def test_a_restart_during_a_completed_withdraw_is_rolled_back_even_if_the_new_plugin_speaks(
        tmp_path):
    scene = Scene(tmp_path)
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")

    def restart_and_speak():
        scene.box.restart()
        updatehelper.write_json(str(scene.directory / "started.json"),
                                {"version": NEW, "commit": "e" * 40, "pid": 200})
    scene.box.pauses["withdrawn"] = restart_and_speak
    assert scene.run() == 1
    assert scene.last()["result"] == "rolled_back"
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.init_calls() == ["4", "3"]


def test_the_withdraw_removes_what_only_the_new_version_brought(tmp_path):
    scene = Scene(tmp_path)
    _status, info = updatehelper.opkg_paths(scene.box.root)
    extra_info = Path(info, updatehelper.PACKAGE + ".postinst")
    cache = Path(scene.box.path(HOOK)).parent / "__pycache__"
    bytecode = cache / "MQTTBridge.cpython-312.pyc"
    original = scene.box.install

    def install_with_more(ipk):
        original(ipk)
        extra_info.write_text("#!/bin/sh\n")
        cache.mkdir(exist_ok=True)
        bytecode.write_bytes(b"new bytecode")
    scene.box.install = install_with_more
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
    assert scene.run() == 0
    assert not extra_info.exists() and not bytecode.exists()


# ---------------------------------------------------------------- R3, closely --


def test_a_zap_back_that_does_not_take_is_reported_lost(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.service = TVP1
    scene.box.zap_works = False
    original = scene.box.run

    def image_ignores_lastservice(argv, timeout):
        code = original(argv, timeout)
        if argv[0] == scene.box.init and argv[1] == "3":
            scene.box.service = TVN
        return code
    scene.box.run = image_ignores_lastservice
    scene.run()
    assert scene.box.zaps == [TVP1]
    assert scene.status()["record"]["channel"] == "lost"


def test_an_interface_that_does_not_come_back_is_not_verified(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.start_fails = True
    scene.run()
    record = scene.status()["record"]
    assert record["interface"] == "not started"
    assert record["channel"] == "lost" and "standby" not in record
    assert scene.box.zaps == []


def test_a_rolled_back_transaction_keeps_its_directory(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.run()
    assert scene.last()["result"] == "rolled_back"
    assert json.loads((scene.directory / "status.json").read_text())["result"] == "rolled_back"


# ------------------------------------------------ the acceptance drill's R2 --


def drill_file(scene):
    return Path(scene.box.root) / updatehelper.DRILL_R2


def test_the_drill_file_sends_an_acceptance_build_straight_into_r2(tmp_path):
    scene = Scene(tmp_path, acceptance=True)
    asked = []
    scene.box.pauses["restarting"] = lambda: asked.append(True)
    scene.box.service = TVP1
    drill_file(scene).write_text("")
    assert scene.run() == 1
    last = scene.last()
    assert (last["result"], last["reason"]) == ("rolled_back", "drill")
    # From installed, without a forward restart: never handed to the plugin, one stop and start.
    assert asked == [] and scene.init_calls() == ["4", "3"]
    assert scene.box.init_log[0] == ("4", f"# plugin {NEW}\n")
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")
    assert scene.box.lastservice_at_start == TVP1
    record = scene.status()["record"]
    assert (record["drill"], record["restart"], record["restore"]) == ("r2", "stopped", "done")
    # One transaction's worth: the file is used up.
    assert not drill_file(scene).exists() and not scene.locked()


def test_a_release_or_development_build_ignores_the_drill_file(tmp_path):
    # Release and development builds both write `acceptance: false` (trust.configured).
    scene = Scene(tmp_path, acceptance=False)
    scene.plugin_word()
    drill_file(scene).write_text("")
    assert scene.run() == 0
    assert scene.last()["result"] == "installed"
    assert scene.init_calls() == [] and "drill" not in scene.status()["record"]
    assert drill_file(scene).exists()


def test_a_drill_link_is_not_followed_or_used(tmp_path):
    scene = Scene(tmp_path, acceptance=True)
    scene.plugin_word()
    target = tmp_path / "elsewhere"
    target.write_text("")
    drill_file(scene).symlink_to(target)
    assert scene.run() == 0
    assert scene.init_calls() == [] and target.exists()


@pytest.mark.parametrize("kind", ["content", "fifo"])
def test_a_drill_name_that_is_not_an_empty_file_is_ignored_and_left(tmp_path, kind):
    # The drill's file is made empty; anything else of that name was put there for another
    # reason - and a pipe is never opened.
    scene = Scene(tmp_path, acceptance=True)
    scene.plugin_word()
    if kind == "content":
        drill_file(scene).write_text("r2\n")
    else:
        os.mkfifo(drill_file(scene))
    assert scene.run() == 0
    assert scene.last()["result"] == "installed"
    assert scene.init_calls() == [] and "drill" not in scene.status()["record"]
    assert os.path.lexists(drill_file(scene))


# ----------------------------------------------- R2 before `init 4`, closely --


def test_an_error_recording_the_channel_at_r2s_start_still_puts_the_old_version_back(
        tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    real = updatehelper.Transaction.statusinfo

    def statusinfo(self):
        # Every look before the stop fails the same way, so a second R2 would meet it again.
        if self.status.get("phase") == "rolling_back" and not scene.init_calls():
            raise RecursionError("maximum recursion depth exceeded")
        return real(self)
    monkeypatch.setattr(updatehelper.Transaction, "statusinfo", statusinfo)
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.init_calls() == ["4", "3"] and scene.box.pids and not scene.locked()
    record = scene.status()["record"]
    assert record["internal_error"].startswith("RecursionError")
    # The record taken before the restart stands in: the channel playing then comes back.
    assert scene.box.lastservice_at_start == TVP1


@pytest.mark.parametrize("error", [RuntimeError("boom"), Escape()], ids=["error", "escape"])
def test_whatever_escapes_r2_before_init_4_still_ends_with_the_old_version(tmp_path, error):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)

    def breaks_once():
        scene.box.pauses.pop("rollback_recorded")
        raise error
    scene.box.pauses["rollback_recorded"] = breaks_once
    if isinstance(error, Exception):
        assert scene.run() == 1
    else:
        with pytest.raises(Escape):
            scene.run()
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "internal_error")
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.init_calls() == ["4", "3"] and scene.box.pids and not scene.locked()
    assert scene.box.init_log[0] == ("4", f"# plugin {NEW}\n")


# ------------------------------------------------ a restore that cannot finish --


def end_by(scene, path):
    """The ends that put the files back: the undo and the withdraw (no restart), and R2."""
    if path == "undo":
        scene.box.pauses["opkg_done"] = lambda: scene.transaction.on_signal(signal.SIGHUP)
        return "interrupted"
    if path == "withdraw":
        scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
        return "question"
    scene.plugin_word(started=False)
    return "not_started"


def installed_version(scene):
    status, _info = updatehelper.opkg_paths(scene.box.root)
    text = Path(status).read_text()
    return text.split(updatehelper.PACKAGE + "\nVersion: ")[1].split("\n")[0]


@pytest.mark.parametrize("error", [OSError(28, "No space left on device"),
                                   OSError(5, "Input/output error")], ids=["enospc", "eio"])
@pytest.mark.parametrize("path", ["undo", "withdraw", "r2"])
def test_opkgs_records_that_cannot_be_written_still_let_the_old_code_back(
        tmp_path, monkeypatch, path, error):
    scene = Scene(tmp_path)
    cause = end_by(scene, path)
    status, _info = updatehelper.opkg_paths(scene.box.root)
    real = updatehelper.atomic_write

    def failing(target, data, mode=0o600):
        if target == status:
            raise error
        return real(target, data, mode)
    monkeypatch.setattr(updatehelper, "atomic_write", failing)
    assert scene.run() == 1
    # The code that decides what runs is the old one, hook included ...
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.box.read(HOOK) == f"# hook {OLD}\n"
    # ... and the end says exactly what is missing, and what repairs it.
    last = scene.last()
    assert (last["result"], last["reason"]) == ("failed", "restore_incomplete")
    assert OLD in last["error"] and "Force plugin reinstall" in last["error"]
    assert error.strerror in last["error"]
    record = scene.status()["record"]
    assert record["restore"].startswith("partial: opkg's records")
    assert record["cause"] == cause
    assert installed_version(scene) == NEW and not scene.locked()
    if path == "r2":
        assert scene.init_calls() == ["4", "3"] and scene.box.pids
        assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")
        # The settings block and the channel went back all the same.
        assert scene.box.lastservice_at_start == TVP1
    else:
        assert scene.init_calls() == []


@pytest.mark.parametrize("path", ["undo", "withdraw", "r2"])
def test_a_plugin_tree_that_cannot_go_back_leaves_opkgs_records_agreeing_with_it(
        tmp_path, monkeypatch, path):
    scene = Scene(tmp_path)
    cause = end_by(scene, path)

    def broken(*_args):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(updatehelper, "_replace_tree", broken)
    assert scene.run() == 1
    assert scene.plugin_py() == f"# plugin {NEW}\n"
    # Nothing was put back before the tree, so opkg still names what is on disk.
    assert installed_version(scene) == NEW
    last = scene.last()
    assert (last["result"], last["reason"]) == ("failed", "restore_failed")
    assert "Force plugin reinstall" in last["error"] and "Input/output error" in last["error"]
    record = scene.status()["record"]
    assert record["restore"].startswith("failed") and record["cause"] == cause
    assert not scene.locked()


def test_a_settings_block_that_cannot_be_written_is_named_after_the_code_is_back(
        tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    settings = scene.box.path(updatehelper.SETTINGS)
    real = updatehelper.atomic_write
    failed = []

    def failing(target, data, mode=0o600):
        # Only the restore's write: the channel written after it is R2's own.
        if target == settings and not failed:
            failed.append(target)
            raise OSError(28, "No space left on device")
        return real(target, data, mode)
    monkeypatch.setattr(updatehelper, "atomic_write", failing)
    assert scene.run() == 1
    assert scene.plugin_py() == f"# plugin {OLD}\n" and installed_version(scene) == OLD
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "restore_incomplete")
    record = scene.status()["record"]
    assert record["restore"] == "partial: the settings block: [Errno 28] No space left on device"
    assert scene.init_calls() == ["4", "3"]


def test_a_restart_during_a_withdraw_that_left_opkgs_records_still_goes_to_r2(
        tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
    status, _info = updatehelper.opkg_paths(scene.box.root)
    real = updatehelper.atomic_write

    def failing(target, data, mode=0o600):
        if target == status:
            raise OSError(28, "No space left on device")
        return real(target, data, mode)
    monkeypatch.setattr(updatehelper, "atomic_write", failing)

    def restart_and_speak():
        scene.box.restart()
        updatehelper.write_json(str(scene.directory / "started.json"),
                                {"version": NEW, "commit": "e" * 40, "pid": 200})
    scene.box.pauses["withdrawn"] = restart_and_speak
    assert scene.run() == 1
    # The old code was back when the new process started: it is never proved, but stopped.
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "restore_incomplete")
    assert scene.init_calls() == ["4", "3"]
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")


# ------------------------------------------------------- gaps the mutants found --


@pytest.mark.parametrize("started_by", ["mqtt", "home_assistant"])
def test_a_repair_of_the_installed_version_is_no_downgrade(tmp_path, started_by):
    # `cmd/update` is for upgrades and repairs: the version installed, installed again.
    ipk = package(OLD)
    scene = Scene(tmp_path, target=OLD, ipk=ipk, entries=[entry_for(OLD, ipk)],
                  started_by=started_by)

    def plugin_word():
        updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
        scene.box.at(5, scene.box.restart)
        scene.box.at(8, lambda: updatehelper.write_json(
            str(scene.directory / "started.json"), {"version": OLD, "commit": "e" * 40,
                                                    "pid": 200}))
    scene.box.pauses["restarting"] = plugin_word
    assert scene.run() == 0
    assert (scene.last()["result"], scene.last()["reason"]) == ("installed", None)
    assert "--force-downgrade" not in scene.opkg_calls()[0]


def test_a_restart_that_lands_during_the_undo_goes_to_r2(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    scene.box.opkg_mode = "partial"
    real = updatehelper.restore_snapshot

    def restore_while_the_interface_restarts(*args, **kwargs):
        real(*args, **kwargs)
        if not scene.init_calls():
            scene.box.restart()
    monkeypatch.setattr(updatehelper, "restore_snapshot", restore_while_the_interface_restarts)
    assert scene.run() == 1
    # The new process may have read either version: stopped, restored again, started.
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "manifest")
    assert scene.init_calls() == ["4", "3"]
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")


def test_an_interface_restart_just_before_opkg_runs_changes_nothing(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    real = updatehelper.write_json

    def marker_then_restart(path, value, mode=0o600):
        real(path, value, mode)
        if path.endswith("mqttbridge-update.json") and value.get("phase") == "installing":
            scene.box.pids = {150}
    monkeypatch.setattr(updatehelper, "write_json", marker_then_restart)
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("interrupted", "interrupted")
    assert scene.opkg_calls() == [] and scene.init_calls() == []
    assert scene.plugin_py() == f"# plugin {OLD}\n"


def test_a_child_seen_before_restarting_is_never_the_restart(tmp_path):
    scene = tier_two(tmp_path)
    log = scene.box.path(updatehelper.LOG_PATHS[0])

    def a_child_is_running():
        scene.box.pids = {100, 101}
        scene.box.fds[101] = {log}

    def the_old_interface_quits():
        # The child outlives it, holding the old plugin's log; no new interface comes.
        updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
        scene.box.pids = {101}
    scene.box.pauses["opkg_done"] = a_child_is_running
    scene.box.pauses["restarting"] = the_old_interface_quits
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    assert scene.status()["record"]["proof"] == "none"


def test_a_slow_restart_beside_a_remembered_child_still_gets_its_proof_window(tmp_path):
    scene = Scene(tmp_path)

    def a_child_is_running():
        scene.box.pids = {100, 101}

    def the_old_interface_quits():
        updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
        scene.box.pids = {101}
        # The new interface takes 130 s to come up: inside the 180 s, past a 120 s proof.
        scene.box.at(130, lambda: setattr(scene.box, "pids", {101, 200}))
        scene.box.at(133, lambda: updatehelper.write_json(
            str(scene.directory / "started.json"), {"version": NEW, "commit": "e" * 40,
                                                    "pid": 200}))
    scene.box.pauses["opkg_done"] = a_child_is_running
    scene.box.pauses["restarting"] = the_old_interface_quits
    assert scene.run() == 0
    assert scene.last()["result"] == "installed"


# ------------------------------------------------ R2 cut short, and R3 failing --


@pytest.mark.parametrize("where", ["rollback_stopped", "restore_once", "restore_always"])
def test_whatever_cuts_r2_short_after_init_4_is_finished_before_the_interface_starts(
        tmp_path, monkeypatch, where):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.pauses["proving"] = lambda: household_changes_a_plugin_setting(scene)
    if where == "rollback_stopped":
        def escapes():
            scene.box.pauses.pop("rollback_stopped")
            raise Escape()
        scene.box.pauses["rollback_stopped"] = escapes
    else:
        real = updatehelper.restore_snapshot
        calls = []

        def escapes_from_the_restore(*args, **kwargs):
            calls.append(True)
            if where == "restore_always" or len(calls) == 1:
                raise Escape()
            return real(*args, **kwargs)
        monkeypatch.setattr(updatehelper, "restore_snapshot", escapes_from_the_restore)
    with pytest.raises(Escape):
        scene.run()
    assert scene.init_calls() == ["4", "3"] and scene.box.pids and not scene.locked()
    record = scene.status()["record"]
    # Each remaining step ran on its own: a restore that cannot finish keeps no channel back.
    assert (record["rollback"], record["lastservice"]) == ("cut short", "written")
    assert scene.box.lastservice_at_start == TVP1
    if where == "restore_always":
        assert record["restore"] == "interrupted"
        assert scene.box.init_log[-1] == ("3", f"# plugin {NEW}\n")
    else:
        # The restore that was cut short is made before the interface starts.
        assert record["restore"] == "done"
        assert scene.box.setting("config.plugins.mqttbridge.enabled") == "true"
        assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")


def test_a_channel_write_that_escapes_twice_still_lets_the_interface_start(
        tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)

    def escapes(_root, _reference):
        raise Escape()
    monkeypatch.setattr(updatehelper, "write_lastservice", escapes)
    with pytest.raises(Escape):
        scene.run()
    assert scene.init_calls() == ["4", "3"] and scene.box.pids and not scene.locked()
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")
    record = scene.status()["record"]
    assert (record["rollback"], record["restore"], record["lastservice"]) == (
        "cut short", "done", "failed")


def test_an_error_in_r3_leaves_a_finished_rollback_rolled_back(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)

    def broken(self, recorded):
        raise RuntimeError("the channel check broke")
    monkeypatch.setattr(updatehelper.Transaction, "verify", broken)
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    assert scene.plugin_py() == f"# plugin {OLD}\n" and scene.init_calls() == ["4", "3"]
    record = scene.status()["record"]
    assert (record["channel"], record["standby"]) == ("unconfirmed", "unconfirmed")
    assert record["internal_error"].startswith("RuntimeError")


def test_r2_takes_the_channel_recorded_before_the_restart_when_openwebif_is_silent(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    # OpenWebif answered before the restart; by the time R2 starts, it says nothing.
    scene.box.pauses["proving"] = lambda: setattr(scene.box, "webif_up", False)
    assert scene.run() == 1
    assert scene.last()["result"] == "rolled_back"
    assert scene.box.lastservice_at_start == TVP1
    assert scene.status()["record"]["channel"] == "kept"


# ---------------------------------------- R2's own start, its stop and its verdict --


@pytest.mark.parametrize("error", [Escape, RuntimeError, MemoryError],
                         ids=["escape", "error", "memory"])
def test_an_escape_from_the_init_3_call_itself_still_starts_the_interface(tmp_path, error):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    real = scene.box.run
    refused = []

    def init_3_cannot_be_started_once(argv, timeout):
        if argv[0] == scene.box.init and argv[1] == "3" and not refused:
            refused.append(argv[1])
            raise error("init 3")
        return real(argv, timeout)
    scene.box.run = init_3_cannot_be_started_once
    if issubclass(error, Exception):
        assert scene.run() == 1
    else:
        with pytest.raises(error):
            scene.run()
    # `init 3` is safe to repeat, so the `finally` sends it again: the picture comes back on the
    # old version, with everything R2 did before the call kept.
    assert refused == ["3"] and scene.init_calls() == ["4", "3"] and scene.box.pids
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")
    assert scene.box.lastservice_at_start == TVP1 and not scene.locked()
    record = scene.status()["record"]
    assert (record["rollback"], record["restore"], record["lastservice"]) == (
        "cut short", "done", "written")
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "internal_error")


@pytest.mark.parametrize("error", [RuntimeError("no child"), MemoryError()],
                         ids=["error", "memory"])
def test_a_program_that_cannot_be_started_is_an_exit_status_never_an_escape(
        tmp_path, monkeypatch, error):
    def fake(argv, **kwargs):
        raise error
    monkeypatch.setattr(updatehelper.subprocess, "run", fake)
    receiver = updatehelper.Receiver(root=str(tmp_path))
    assert receiver.run(["/sbin/init", "3"], 5) == 127
    assert receiver.last_output.startswith(type(error).__name__)


def escape_after_init_4(scene):
    """`init 4` takes effect - or not, as the box does - and then something escapes."""
    real = scene.box.run

    def init_4_then_escape(argv, timeout):
        code = real(argv, timeout)
        if argv[0] == scene.box.init and argv[1] == "4":
            raise Escape()
        return code
    scene.box.run = init_4_then_escape


@pytest.mark.parametrize("where", ["init_4", "stop_wait"])
def test_r2_cut_short_before_it_saw_the_stop_still_restores_a_stopped_interfaces_settings(
        tmp_path, where):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.pauses["proving"] = lambda: household_changes_a_plugin_setting(scene)
    if where == "init_4":
        escape_after_init_4(scene)
    else:
        real = scene.box.enigma2_pids
        looks = []

        def the_first_look_after_init_4_escapes():
            if scene.init_calls() and not looks:
                looks.append(True)
                raise Escape()
            return real()
        scene.box.enigma2_pids = the_first_look_after_init_4_escapes
    with pytest.raises(Escape):
        scene.run()
    # enigma2 was down, only not yet seen down: the settings block and the channel go back.
    assert scene.init_calls() == ["4", "3"] and scene.box.pids and not scene.locked()
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")
    assert scene.box.setting("config.plugins.mqttbridge.enabled") == "true"
    assert scene.box.lastservice_at_start == TVP1
    record = scene.status()["record"]
    assert (record["rollback"], record["restore"], record["lastservice"]) == (
        "cut short", "done", "written")


def test_r2_cut_short_in_an_init_4_that_stopped_nothing_writes_no_settings(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.stops = False
    scene.box.pauses["proving"] = lambda: household_changes_a_plugin_setting(scene)
    escape_after_init_4(scene)
    with pytest.raises(Escape):
        scene.run()
    # The running interface writes its own settings over any on its next clean quit.
    assert scene.box.setting("config.plugins.mqttbridge.enabled") == "false"
    assert scene.box.setting("config.tv.lastservice") == TVN
    assert scene.plugin_py() == f"# plugin {OLD}\n" and scene.init_calls() == ["4", "3"]
    assert scene.status()["record"]["restore"] == "done"


def test_an_interface_that_never_stopped_is_not_reported_rolled_back(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.stops = False
    assert scene.run() == 1
    last = scene.last()
    assert (last["result"], last["reason"]) == ("failed", "not_stopped")
    assert OLD in last["error"] and scene.marker()["result"] == "failed"
    record = scene.status()["record"]
    assert (record["restore"], record["stop"], record["interface"], record["cause"]) == (
        "done", "not seen", "not restarted", "not_started")
    assert record["unstopped"] == [200]
    # The old files are on disk, but the process that runs is the one R2 could not stop.
    assert scene.plugin_py() == f"# plugin {OLD}\n" and scene.box.pids == {200}
    assert record["channel"] == "unconfirmed" and scene.box.zaps == []


def test_an_interface_that_did_not_come_back_is_not_reported_rolled_back(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.start_fails = True
    assert scene.run() == 1
    last = scene.last()
    assert (last["result"], last["reason"]) == ("failed", "interface_not_started")
    assert OLD in last["error"]
    record = scene.status()["record"]
    assert (record["restore"], record["stop"], record["interface"], record["cause"]) == (
        "done", "seen", "not started", "not_started")
    assert scene.plugin_py() == f"# plugin {OLD}\n" and not scene.box.pids


def test_an_interface_that_quit_late_and_came_back_is_rolled_back(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.stops = False
    scene.box.pauses["proving"] = lambda: household_changes_a_plugin_setting(scene)
    # Not seen stopped within the wait; gone by the time `init 3` is sent, which then starts.
    scene.box.pauses["rollback_restored"] = lambda: setattr(scene.box, "pids", set())
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    record = scene.status()["record"]
    assert (record["restore"], record["stop"]) == ("done", "not seen")
    assert "interface" not in record and "cause" not in record
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n") and scene.box.pids == {300}
    # Not seen stopped, so nothing was written into its settings.
    assert scene.box.setting("config.plugins.mqttbridge.enabled") == "false"


def test_an_error_in_r3_after_the_channel_was_measured_keeps_the_measurement(
        tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.pauses["proving"] = lambda: setattr(scene.box, "standby", True)
    real = updatehelper.Transaction.statusinfo
    looks = []

    def the_second_look_after_the_start_breaks(self):
        if scene.box.init_log and scene.box.init_log[-1][0] == "3":
            looks.append(True)
            if len(looks) > 1:
                raise RuntimeError("OpenWebif went away")
        return real(self)
    monkeypatch.setattr(updatehelper.Transaction, "statusinfo",
                        the_second_look_after_the_start_breaks)
    assert scene.run() == 1
    assert scene.last()["result"] == "rolled_back"
    record = scene.status()["record"]
    # The channel had been measured before the error: only what was not stays unconfirmed.
    assert (record["channel"], record["standby"]) == ("kept", "unconfirmed")


def test_a_partial_withdraw_then_an_r2_cut_short_still_restores_in_r2(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    scene.box.pauses["restarting"] = lambda: (scene.directory / "withdraw").write_text("")
    real = updatehelper._restore_records
    calls = []

    def records_fail_once(*args, **kwargs):
        calls.append(True)
        if len(calls) == 1:
            raise OSError(5, "Input/output error")
        return real(*args, **kwargs)
    monkeypatch.setattr(updatehelper, "_restore_records", records_fail_once)
    # The withdraw leaves `restore: partial`, a restart lands meanwhile, and R2 is cut short
    # before its own restore: the `finally` still restores, keyed on R2's own step.
    scene.box.pauses["withdrawn"] = scene.box.restart

    def escapes():
        scene.box.pauses.pop("rollback_stopped")
        raise Escape()
    scene.box.pauses["rollback_stopped"] = escapes
    with pytest.raises(Escape):
        scene.run()
    record = scene.status()["record"]
    assert (record["rollback"], record["restore"]) == ("cut short", "done")
    assert len(calls) == 2 and scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")
    status, _info = updatehelper.opkg_paths(scene.box.root)
    assert f"Version: {OLD}\n" in Path(status).read_text()
    assert scene.init_calls() == ["4", "3"] and not scene.locked()



def test_r2_waits_for_a_stop_that_takes_a_few_seconds(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.stops = False
    scene.box.pauses["proving"] = lambda: household_changes_a_plugin_setting(scene)
    real = scene.box.run

    def enigma2_takes_five_seconds_to_quit(argv, timeout):
        if argv[0] == scene.box.init and argv[1] == "4":
            scene.box.at(5, lambda: setattr(scene.box, "pids", set()))
        return real(argv, timeout)
    scene.box.run = enigma2_takes_five_seconds_to_quit
    assert scene.run() == 1
    # Seen running at first, then seen gone: a stop, with everything that follows one.
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    record = scene.status()["record"]
    assert (record["stop"], record["restore"], record["lastservice"]) == (
        "seen", "done", "written")
    assert scene.box.setting("config.plugins.mqttbridge.enabled") == "true"
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n") and scene.box.pids == {300}


# ------------------------------------------- an interruption at every step of R2 --


R2_STEPS = ["init_4", "stop_wait", "rollback_stopped", "opkg_lock", "opkg_records",
            "settings_block", "restore_returned", "rollback_restored", "lastservice", "init_3",
            "r3"]
# Where an `Exception` is the restore's own, handled failure (D2): recorded, not retried.
RESTORE_HANDLES = ("opkg_lock", "opkg_records", "settings_block")


def interrupt_r2_at(scene, monkeypatch, step, error, always):
    """Raise `error` at `step` of R2, once or every time the step comes round."""
    fired = []

    def fire():
        # Only inside R2: the snapshot takes opkg's lock too, long before.
        if "4" not in scene.init_calls() or (fired and not always):
            return False
        fired.append(step)
        return True

    def wrap(owner, name, after, when=lambda *args: True):
        real = getattr(owner, name)

        def wrapped(*args, **kwargs):
            if not after and when(*args) and fire():
                raise error(step)
            out = real(*args, **kwargs)
            if after and when(*args) and fire():
                raise error(step)
            return out
        monkeypatch.setattr(owner, name, wrapped)

    def pause():
        if fire():
            raise error(step)
    if step == "init_4":
        wrap(scene.box, "run", True, lambda argv, _timeout: argv[1:] == ["4"])
    elif step == "init_3":
        wrap(scene.box, "run", False, lambda argv, _timeout: argv[1:] == ["3"])
    elif step == "stop_wait":
        wrap(scene.box, "enigma2_pids", False)
    elif step in ("rollback_stopped", "rollback_restored"):
        scene.box.pauses[step] = pause
    elif step == "opkg_lock":
        wrap(updatehelper.opkg_lock, "__enter__", False)
    elif step == "opkg_records":
        wrap(updatehelper, "_restore_records", False)
    elif step == "settings_block":
        wrap(updatehelper, "_restore_settings", True)
    elif step == "restore_returned":
        wrap(updatehelper.Transaction, "put_back", True)
    elif step == "lastservice":
        wrap(updatehelper, "write_lastservice", True)
    else:
        wrap(updatehelper.Transaction, "statusinfo", False,
             lambda _self: bool(scene.box.init_log) and scene.box.init_log[-1][0] == "3")
    return fired


@pytest.mark.parametrize("always", [False, True], ids=["once", "always"])
@pytest.mark.parametrize("error", [Escape, RuntimeError], ids=["escape", "error"])
@pytest.mark.parametrize("step", R2_STEPS)
def test_an_interruption_at_any_step_of_r2_ends_as_the_unit_would(
        tmp_path, monkeypatch, step, error, always):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.pauses["proving"] = lambda: household_changes_a_plugin_setting(scene)
    fired = interrupt_r2_at(scene, monkeypatch, step, error, always)
    try:
        scene.run()
    except Escape:
        pass
    monkeypatch.undo()
    assert fired
    last, record = scene.last(), scene.status()["record"]
    settings = scene.box.read(updatehelper.SETTINGS)
    # Never a second stop, never the lock kept, never a setting written twice.
    assert scene.init_calls().count("4") == 1 and not scene.locked()
    assert settings.count("config.plugins.mqttbridge.enabled=") == 1
    assert settings.count(updatehelper.LASTSERVICE_KEY + "=") == 1
    if last["result"] == "rolled_back":
        assert record["restore"] == "done" and scene.plugin_py() == f"# plugin {OLD}\n"
    if step == "init_3" and always:
        # A start that fails every time is the one end without a picture.
        assert scene.init_calls() == ["4"] and not scene.box.pids
        return
    assert scene.init_calls()[-1] == "3" and scene.box.pids
    if always or (error is RuntimeError and step in RESTORE_HANDLES):
        return
    # Interrupted once: whatever it cut short is finished before the start - the old version,
    # its records, the household's settings block and the recorded channel.
    assert record["restore"] == "done" and record["stop"] == "seen"
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")
    status, _info = updatehelper.opkg_paths(scene.box.root)
    assert f"Version: {OLD}\n" in Path(status).read_text()
    assert scene.box.setting("config.plugins.mqttbridge.enabled") == "true"
    assert scene.box.lastservice_at_start == TVP1


# ------------------------------------ `init 3` that does not get through (review 5) --


def init_3_fails_once(scene, monkeypatch, failure):
    """The first `init 3` goes through the helper's own `Receiver.run`, whose child fails."""
    real = scene.box.run
    failed = []

    def cannot_start(*_args, **_kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired(["init", "3"], 30)
        raise {"memory": MemoryError(), "fork": OSError(errno.ENOMEM, "Cannot allocate memory"),
               "error": RuntimeError("no child")}[failure]

    def run(argv, timeout):
        if argv[0] == scene.box.init and argv[1] == "3" and not failed:
            failed.append(argv)
            scene.box.argv.append(list(argv))
            with monkeypatch.context() as patch:
                patch.setattr(updatehelper.subprocess, "run", cannot_start)
                return updatehelper.Receiver.run(scene.box, argv, timeout)
        return real(argv, timeout)
    scene.box.run = run
    return failed


@pytest.mark.parametrize("failure", ["memory", "fork", "error", "timeout"])
def test_an_init_3_that_could_not_be_started_is_sent_again(tmp_path, monkeypatch, failure):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    failed = init_3_fails_once(scene, monkeypatch, failure)
    assert scene.run() == 1
    assert failed and scene.init_calls() == ["4", "3", "3"] and scene.box.pids == {300}
    assert scene.box.init_log[-1] == ("3", f"# plugin {OLD}\n")
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    assert scene.status()["record"]["init_3"] == [None if failure == "timeout" else 127, 0]
    assert scene.box.lastservice_at_start == TVP1


def test_an_init_3_that_took_no_effect_after_a_stop_is_sent_again(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    real = scene.box.run

    def the_first_start_is_lost(argv, timeout):
        if argv[0] == scene.box.init and argv[1] == "3" and "3" not in scene.init_calls():
            scene.box.argv.append(list(argv))
            return 0
        return real(argv, timeout)
    scene.box.run = the_first_start_is_lost
    assert scene.run() == 1
    assert scene.init_calls() == ["4", "3", "3"] and scene.box.pids == {300}
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    assert scene.status()["record"]["start"] == "again"


@pytest.mark.parametrize("error", [KeyboardInterrupt, SystemExit])
def test_receiver_run_lets_what_is_not_an_exception_through(tmp_path, monkeypatch, error):
    def interrupted(*_args, **_kwargs):
        raise error()
    monkeypatch.setattr(updatehelper.subprocess, "run", interrupted)
    with pytest.raises(error):
        updatehelper.Receiver(root=str(tmp_path)).run(["/sbin/init", "3"], 5)



# --------------------------------------- a /proc that cannot be read is unknown --


@pytest.mark.parametrize("looks", [1, 10 ** 6], ids=["one_look", "every_look"])
def test_a_proc_that_cannot_be_listed_is_never_a_stop(tmp_path, looks):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.stops = False
    scene.box.pauses["proving"] = lambda: household_changes_a_plugin_setting(scene)
    scene.box.pauses["rollback_recorded"] = lambda: setattr(scene.box, "unreadable", looks)
    assert scene.run() == 1
    # init 4 did nothing: the process R2 could not stop runs on, and no look said otherwise.
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "not_stopped")
    assert scene.status()["record"]["stop"] == "not seen" and scene.box.pids == {200}
    assert scene.box.setting("config.plugins.mqttbridge.enabled") == "false"
    assert scene.box.setting("config.tv.lastservice") == TVN


@pytest.mark.parametrize("how", ["unreadable", "raises"])
def test_a_second_look_that_fails_writes_no_settings(tmp_path, how):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.stops = False
    scene.box.pauses["proving"] = lambda: household_changes_a_plugin_setting(scene)
    real = scene.box.run

    def init_4_then_escape_and_proc_fails(argv, timeout):
        code = real(argv, timeout)
        if argv[0] == scene.box.init and argv[1] == "4":
            if how == "unreadable":
                scene.box.unreadable = 1
            else:
                def broken():
                    raise OSError(errno.EMFILE, "Too many open files")
                scene.box.enigma2_pids = scene.box.enigma2_look = broken
            raise Escape()
        return code
    scene.box.run = init_4_then_escape_and_proc_fails
    with pytest.raises(Escape):
        scene.run()
    assert scene.box.setting("config.plugins.mqttbridge.enabled") == "false"
    assert scene.box.setting("config.tv.lastservice") == TVN
    assert scene.plugin_py() == f"# plugin {OLD}\n"
    assert scene.status()["record"]["stop"] == "not seen"


def test_a_proc_that_cannot_be_listed_is_unknown_to_r2_and_empty_to_the_rest(tmp_path):
    receiver = updatehelper.Receiver(root=str(tmp_path), proc=str(tmp_path / "no-proc"))
    assert receiver.enigma2_look() is None
    assert receiver.enigma2_pids() == set()



# ------------------------------ what R2 could not stop, and what became of it --


@pytest.mark.parametrize("comes_back", [True, False], ids=["restarts", "stays_down"])
def test_an_unstopped_interface_that_quits_after_init_3_gets_init_3_again(tmp_path, comes_back):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.stops = False
    real = scene.box.run

    def quits_ten_seconds_after_init_3(argv, timeout):
        code = real(argv, timeout)
        if argv[0] == scene.box.init and argv[1] == "3" and scene.init_calls() == ["4", "3"]:
            def quits():
                scene.box.pids = set()
                scene.box.start_fails = not comes_back
            scene.box.at(10, quits)
        return code
    scene.box.run = quits_ten_seconds_after_init_3
    assert scene.run() == 1
    record = scene.status()["record"]
    assert (record["stop"], record["unstopped"], record["start"]) == ("not seen", [200], "again")
    if comes_back:
        assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
        assert scene.box.pids == {300} and "interface" not in record
    else:
        # Nothing runs: not the process R2 could not stop, and not the old version either.
        assert (scene.last()["result"], scene.last()["reason"]) == (
            "failed", "interface_not_started")
        assert not scene.box.pids and record["interface"] == "not started"


def test_a_restore_that_failed_keeps_its_reason_when_nothing_starts_either(tmp_path, monkeypatch):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.start_fails = True

    def no_space(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(updatehelper, "restore_snapshot", no_space)
    assert scene.run() == 1
    # The repair the restore's reason names - a forced reinstall - covers the start as well.
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "restore_failed")
    record = scene.status()["record"]
    assert (record["interface"], record["cause"]) == ("not started", "not_started")
    assert record["restore"].startswith("failed")


def test_without_one_answered_look_during_the_stop_nothing_counts_as_a_start(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.stops = False
    # /proc cannot be listed for the whole of the wait for the stop, and answers again after.
    scene.box.pauses["rollback_recorded"] = lambda: setattr(scene.box, "unreadable", 10 ** 6)
    scene.box.pauses["rollback_stopped"] = lambda: setattr(scene.box, "unreadable", 0)
    assert scene.run() == 1
    # The enigma2 seen afterwards may be the very one that never stopped: never a start.
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "not_stopped")
    record = scene.status()["record"]
    assert record["interface"] == "not restarted" and scene.box.pids == {200}
    # Which processes R2 could not stop is unknown, so none are named.
    assert "unstopped" not in record


# ------------------------------ the bounds TRANSACTION.md section 2.4 states --


@pytest.mark.parametrize("tier", [1, 2])
def test_a_restart_that_lands_late_is_given_only_what_is_left_of_the_forward_path(tmp_path, tier):
    scene = tier_two(tmp_path) if tier == 2 else Scene(tmp_path)
    log = scene.box.path(updatehelper.LOG_PATHS[0])
    marks = {}

    def asked_ten_seconds_before_the_deadline():
        scene.box.t = scene.transaction.deadline - 10
        updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
        scene.box.at(1, scene.box.restart)
        # The new plugin would prove itself 30 s after the restart - past the deadline.
        if tier == 1:
            scene.box.at(31, lambda: updatehelper.write_json(
                str(scene.directory / "started.json"),
                {"version": NEW, "commit": "e" * 40, "pid": 200}))
        else:
            scene.box.at(31, lambda: scene.box.fds.update({200: {log}}))
    scene.box.pauses["restarting"] = asked_ten_seconds_before_the_deadline
    scene.box.pauses["rollback_recorded"] = lambda: marks.setdefault("r2", scene.box.t)
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    assert scene.status()["record"]["proof"] == "none"
    # The proof window ended with the forward path, not 120 s after the restart.
    assert marks["r2"] <= scene.transaction.deadline + updatehelper.PROOF_POLL


# What TRANSACTION.md section 2.4 states, in seconds from the helper's start.
FORWARD_BOUND = updatehelper.FORWARD_LIMIT + 45
R2_BOUND = 409
LOCK_BOUND = 1353
WEBIF_TIMEOUT = 5


def every_wait_at_its_bound(scene, monkeypatch, entry, stop, start):
    """R2 entered at the forward deadline, and then every wait as long as it can be.

    Each `init` call runs into its 30 s timeout and answers nothing, each OpenWebif call costs
    its 5 s timeout, each restore waits the whole 40 s for opkg's lock, and R3's zap back and
    standby never take. `entry` is how the forward path reaches R2 at its deadline: a restart
    just before it, a withdraw on it, a withdraw after an opkg that ran up to it, or an undo
    after an opkg that ran past it; the three last see the interface restart while the files go
    back. `stop` is what `init 4` does; `start` is when the new interface appears after the
    first `init 3`, or never.
    """
    box, transaction = scene.box, scene.transaction
    box.standby = True
    box.zap_works = False
    box.stops = stop in ("at_once", "late")
    marks = {"r2": None, "claim": None, "release": None}
    busy = {"until": -1.0}
    real_lockf = updatehelper.fcntl.lockf

    def lockf(descriptor, operation, *rest):
        if box.t < busy["until"]:
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")
        return real_lockf(descriptor, operation, *rest)
    monkeypatch.setattr(updatehelper.fcntl, "lockf", lockf)
    real_restore = updatehelper.restore_snapshot
    restores = []

    def restore(receiver, backup, settings):
        busy["until"] = box.t + updatehelper.OPKG_LOCK_WAIT_RESTORE - updatehelper.OPKG_LOCK_POLL
        real_restore(receiver, backup, settings)
        restores.append(settings)
        if entry != "restart" and len(restores) == 1:
            box.restart()
    monkeypatch.setattr(updatehelper, "restore_snapshot", restore)
    real_webif = box.webif

    def webif(path):
        box.t += WEBIF_TIMEOUT
        if path.startswith("/api/powerstate"):
            return None
        return real_webif(path)
    box.webif = webif
    starts = []

    def run(argv, timeout):
        if argv[0] != box.init:
            return Box.run(box, argv, timeout)
        box.argv.append(list(argv))
        if argv[1] == "4" and stop == "at_once":
            box.at(0, lambda: setattr(box, "pids", set()))
        elif argv[1] == "4" and stop == "late":
            box.at(updatehelper.STOP_WAIT - 1, lambda: setattr(box, "pids", set()))
        elif argv[1] == "3" and not starts:
            starts.append(box.t)
            if stop == "quits":
                box.at(10, lambda: setattr(box, "pids", box.pids - {200}))
            if start is not None:
                box.at(start, lambda: (setattr(box, "pids", {300}),
                                       setattr(box, "webif_up", True),
                                       setattr(box, "service", TVN),
                                       setattr(box, "standby", False)))
        box.t += timeout
        return None
    box.run = run
    if entry == "restart":
        def restart_just_before_the_deadline():
            box.t = transaction.deadline - 1.5
            updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
            box.at(0.5, box.restart)
        box.pauses["restarting"] = restart_just_before_the_deadline
    elif entry == "withdraw":
        def asked_and_nothing_came():
            box.t = transaction.deadline - updatehelper.PLUGIN_WAIT + 1
            updatehelper.write_json(str(scene.directory / "restart.json"), {"pid": 100})
        box.pauses["restarting"] = asked_and_nothing_came
    else:
        late = -0.5 if entry == "opkg_to_the_deadline" else 1
        box.opkg_blocker = lambda: setattr(box, "t", transaction.deadline + late)
    for name in ("claim", "release", "rollback"):
        real = getattr(transaction, name)

        def marked(*args, name=name, real=real):
            if name == "rollback" and marks["r2"] is None:
                marks["r2"] = box.t
            if name == "release" and transaction.holding and marks["release"] is None:
                marks["release"] = box.t
            result = real(*args)
            if name == "claim":
                marks["claim"] = box.t
            return result
        setattr(transaction, name, marked)
    return marks


@pytest.mark.parametrize("start", [None, 0, 90, 185], ids=lambda s: f"start_{s}")
@pytest.mark.parametrize("stop", ["at_once", "late", "quits", "never"])
@pytest.mark.parametrize("entry", ["restart", "withdraw", "opkg_to_the_deadline", "undo"])
def test_the_lock_is_held_no_longer_than_transaction_md_says(
        tmp_path, monkeypatch, entry, stop, start):
    scene = Scene(tmp_path)
    marks = every_wait_at_its_bound(scene, monkeypatch, entry, stop, start)
    begun = scene.box.t
    assert scene.run() == 1
    assert scene.last()["result"] in ("rolled_back", "failed")
    assert marks["r2"] is not None and marks["r2"] - begun <= FORWARD_BOUND
    assert scene.box.t - marks["r2"] <= R2_BOUND
    assert marks["release"] - marks["claim"] <= LOCK_BOUND
    assert scene.box.t - begun <= LOCK_BOUND


# ------------------------------ a second enigma2 beside one R2 could not stop --


@pytest.mark.parametrize("survivor", ["stays", "quits"])
def test_an_enigma2_started_beside_one_r2_could_not_stop_is_no_start(tmp_path, survivor):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.stops = False
    real = scene.box.run

    def a_second_interface_beside_the_first(argv, timeout):
        if argv[0] == scene.box.init and argv[1] == "3":
            scene.box.argv.append(list(argv))
            # The image's start launches a new enigma2 while the one `init 4` did not stop runs.
            scene.box.pids = scene.box.pids | {300}
            if survivor == "quits":
                scene.box.at(40, lambda: setattr(scene.box, "pids", scene.box.pids - {200}))
            return 0
        return real(argv, timeout)
    scene.box.run = a_second_interface_beside_the_first
    assert scene.run() == 1
    record = scene.status()["record"]
    assert (record["stop"], record["unstopped"]) == ("not seen", [200])
    if survivor == "stays":
        # 200 runs on with the code it had: the end must not tell it the old version runs.
        assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "not_stopped")
        assert record["interface"] == "not restarted" and record["cause"] == "not_started"
        assert scene.box.pids == {200, 300}
    else:
        # Once every process R2 could not stop has gone, the new one is the start.
        assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
        assert scene.box.pids == {300} and "interface" not in record


# --------------------------------------------- a look that cannot say, closely --


@pytest.mark.parametrize("error, look", [
    ("ENOENT", {100}), ("ESRCH", {100}),
    ("EMFILE", None), ("ENFILE", None), ("ENOMEM", None), ("EACCES", None),
])
def test_a_process_name_that_cannot_be_read_is_unknown_unless_the_process_exited(
        tmp_path, monkeypatch, error, look):
    error = getattr(errno, error)
    proc = tmp_path / "proc"
    for pid, name in ((100, "enigma2"), (101, "sh"), (102, "enigma2")):
        (proc / str(pid)).mkdir(parents=True)
        (proc / str(pid) / "comm").write_text(name + "\n")
    real_open = open

    def unreadable_102(path, *args, **kwargs):
        if str(path).endswith(os.path.join("102", "comm")):
            raise OSError(error, os.strerror(error))
        return real_open(path, *args, **kwargs)
    monkeypatch.setattr(updatehelper, "open", unreadable_102, raising=False)
    receiver = updatehelper.Receiver(root=str(tmp_path), proc=str(proc))
    # R2 must not read "102 is not enigma2" when it could not tell; a process that exited is
    # simply gone.
    assert receiver.enigma2_look() == look
    # Everything else keeps what it could read.
    assert receiver.enigma2_pids() == {100}


def test_a_second_init_3_that_brings_the_interface_up_late_is_waited_for(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    real = scene.box.run
    # Five seconds inside the 60 s the second chance is given - a number, not the constant,
    # so that a shorter wait fails here.
    late = 55

    def the_first_start_is_lost_the_second_slow(argv, timeout):
        if argv[0] == scene.box.init and argv[1] == "3":
            scene.box.argv.append(list(argv))
            if scene.init_calls().count("3") == 2:
                scene.box.at(late, lambda: real(argv, timeout))
            return 0
        return real(argv, timeout)
    scene.box.run = the_first_start_is_lost_the_second_slow
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    assert scene.status()["record"]["start"] == "again" and scene.box.pids == {300}
    assert scene.box.lastservice_at_start == TVP1


def test_a_restart_in_good_time_gets_the_whole_proof_window_and_no_more(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    marks = {}
    scene.box.pauses["proving"] = lambda: marks.setdefault("proving", scene.box.t)
    scene.box.pauses["rollback_recorded"] = lambda: marks.setdefault("r2", scene.box.t)
    assert scene.run() == 1
    assert (scene.last()["result"], scene.last()["reason"]) == ("rolled_back", "not_started")
    # Far from the deadline, the cap changes nothing: 120 s, then R2.
    waited = marks["r2"] - marks["proving"]
    window, poll = updatehelper.PROOF_WINDOW, updatehelper.PROOF_POLL
    assert window <= waited <= window + poll


def test_a_start_waits_for_every_process_r2_could_not_stop(tmp_path):
    scene = Scene(tmp_path)
    scene.plugin_word(started=False)
    scene.box.stops = False
    scene.box.pauses["rollback_recorded"] = lambda: setattr(scene.box, "pids", {200, 201})
    real = scene.box.run

    def one_of_two_quits_beside_a_new_one(argv, timeout):
        if argv[0] == scene.box.init and argv[1] == "3":
            scene.box.argv.append(list(argv))
            scene.box.pids = scene.box.pids | {300}
            scene.box.at(40, lambda: setattr(scene.box, "pids", scene.box.pids - {201}))
            return 0
        return real(argv, timeout)
    scene.box.run = one_of_two_quits_beside_a_new_one
    assert scene.run() == 1
    # 200 still runs with the code it had: one of the two gone is not enough.
    assert (scene.last()["result"], scene.last()["reason"]) == ("failed", "not_stopped")
    assert scene.status()["record"]["unstopped"] == [200, 201]
    assert scene.box.pids == {200, 300}
