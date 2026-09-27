"""`cmd/update`: the plugin installs one signed release of itself, and the main loop's half of it.

[ADR-0015](../../docs/adr/0015-signed-self-update.md) decides that the plugin updates itself from
its signed release index; [TRANSACTION.md](../../docs/TRANSACTION.md) is the contract with the
update helper (`updatehelper.py`) and with the companion integration's SSH installer. The helper
does everything that has to outlive enigma2 - the lock, the download, the package manager, the
proof and the rollback. This module does what only the running plugin can: it refuses, before
anything changes, every request that must not start; it starts the helper outside enigma2; it
says on the `update` topic how the transaction goes; it closes the plugin's doors while new files
are on disk under the old process; and it asks the image, the way the image itself asks, to
restart its interface.

**Why the guards are all here, and again in the helper.** A refusal here costs nothing and says
so on `last_error` with a reason code a consumer can put into the household's language. The
helper judges the index again anyway (TRANSACTION.md section 7) - it trusts nothing the plugin
or a broker client said about a release - so a guard that is here and not there would be a
guard somebody forgets the day the two disagree. The order is the one TOPICS.md gives, and it
matters: the permission comes first, so a receiver at its defaults answers the same sentence
whatever a payload says; the household comes before the release, so a recording is never
disturbed by the question of which version it would have been.

**Why the helper is a copy, started by `start-stop-daemon`.** The package manager is about to
replace every file of this directory, and the restart that follows ends this process. So the
helper and the four modules it imports are copied into the transaction's own directory and run
from there, detached with `start-stop-daemon -S -b`, which forks, starts a new session and lets
the launching `eConsoleAppContainer` child exit at once. `-p` names a pid file inside the
transaction directory, which nothing has written yet: without it busybox's `start-stop-daemon`
looks for *any* running `/usr/bin/python3`, and finding one it answers "already running" and
starts nothing.

**The doors (M3).** From the helper's phase `restarting` on, the files on disk are the new
release's and the process is still the old one. A module imported for the first time now would
be new code in an old process, so every module this path and the closed state use is imported
when the plugin starts, the publishers are stopped, and every command - over MQTT, from the
page, from the setup screen - answers "an update is being applied on the receiver". For a
downgrade chosen at the television or on the page, every retained topic the node owns except
`availability` is then retracted at QoS 1, because the older release does not know the newer
one's topics and would leave them on the broker for ever (section 11 v of the plan, B1). From
that retraction on nothing at all is published (S-a): refusals are logged, not put on
`last_error`, and the `update` relay stops, so nothing the older release does not know is
re-created behind the retraction.

**The restart (rule R1).** `TryQuitMainloop(session, 3, timeout=60, default_yes=False)` - the
image's own clean quit, which saves the settings and with them the channel being watched. It
quits at once when nothing holds, and asks when a recording, a stream, timeshift or a job does.
**The question's answer is not read from the callback's argument**: the image's
`TryQuitMainloop.close(False)` ends in `MessageBox.close(self, True)`, and a quit never calls
back at all. So a callback, whatever it carries, means the interface is still running: the
helper is told to withdraw. Nothing here reads the dialog stack - a receiver with a
session-start screen (the HbbTV plugin's zero-size `VBMain`) stacks every dialog one deeper.

**The marker.** A plugin that starts reads `/etc/enigma2/mqttbridge-update.json` right after its
logging is configured - before the provisioning file and before it asks whether it is switched
on - so a new release that is switched off still confirms that it started (`started.json`, the
tier-1 proof). A marker from another boot whose lock is still there tells of a power loss, and
is answered by the running version: `installed`, `rolled_back` or `interrupted`.
"""

import json
import os
import re
import secrets
import shutil
import time

from . import buildid, epgimport, power, recording, softcam, trust, updatehelper
from .enigma2 import Ticker, enigma_attribute
from .i18n import _
from .log import get_logger
from .mqttclient import MAX_QUEUED_MESSAGES
from .origin import MQTT, PAGE, SCREEN, granted
from .publisher import Refusal
from .uninstall import _attach, installed_by_package_manager
from .updatecheck import offer
from .version import CONTRACT, __version__

LOG = get_logger("selfupdate")

COMMAND = "update"
CAPABILITY = "self_update"
PERMISSION = "update_allowed"
INTEGRATION_TOPIC = "enigma2mqtt/integration/"

START_STOP_DAEMON = "/sbin/start-stop-daemon"
PYTHON = "/usr/bin/python3"
HELPER_SOURCE = "updatehelper.py"
HELPER_NAME = "helper.py"
REQUEST = "request.json"
STATUS = "status.json"
RESTART = "restart.json"
WITHDRAW = "withdraw"
STARTED = "started.json"
PID_FILE = "helper.pid"

POLL_MILLISECONDS = 1000
# The helper writes its first status within a second or two of being started. With none after
# this long it never ran; its directory is removed, so a helper that starts even later finds no
# request and ends without touching anything.
LAUNCH_WAIT_SECONDS = 60
# The helper writes the end into `status.json`, then the last-transaction record and the marker,
# and lets the lock go last. The end is taken once the lock is gone - or after this long.
RELEASE_WAIT_SECONDS = 5
RATE_LIMIT_SECONDS = 10 * 60
QUESTION_TIMEOUT_SECONDS = 60
QUIT_RESTART = 3

# The downgrade's retraction, exactly as `cmd/uninstall` does it (`uninstall.py`).
RETRACTION_QOS = 1
RETRACTION_POLL_MILLISECONDS = 100
RETRACTION_TIMEOUT_SECONDS = 15.0
BATCH = max(1, MAX_QUEUED_MESSAGES // 2)

# The softcam collapse after a proved install waits for the softcam's own start window.
COLLAPSE_DELAY_MILLISECONDS = (softcam.POST_START_SECONDS + 5) * 1000

PUBLIC_KEYS = ("id", "started_by", "target", "from", "phase", "started", "finished", "result",
               "error")
MAX_TEXT = 512

RELAY_URL = re.compile(
    r"https?://(?:\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9.-]{1,253})(?::[0-9]{1,5})?"
    r"/api/enigma2_mqtt/relay/[A-Za-z0-9_-]{43}",
    re.ASCII,
)

NOT_PERMITTED = "updates over MQTT are switched off in the plugin's settings"
NOT_PACKAGED = "this plugin was not installed by the package manager, so it cannot update itself"
STANDBY = (
    "the receiver is in standby; an update restarts the interface, which wakes the receiver "
    "and may switch the television on"
)
# `cmd/restart_gui`'s own sentence: an update ends in the same restart.
EPG_IMPORT_RUNNING = "an EPG import is running"
CURRENT = "version {version} is already installed and running"
RELAY = "the download address from Home Assistant is not valid"
NO_SPACE = updatehelper.SENTENCES["no_space"]
RATE_LIMITED = "an update ran less than ten minutes ago"
DOORS = "an update is being applied on the receiver"
NOT_STARTED = "the update could not be started: {detail}"


def household_doors():
    """What the setup screen and the page say while the doors are closed."""
    return _("An update of the plugin is being applied on this receiver. Please wait.")


def _refusal(reason, **details):
    """The helper's own sentence for a reason both halves share."""
    return Refusal(str(updatehelper.Fail(reason, **details)), reason)


def _recording_reason(session):
    """Which of the recording guard's three refusals it was, as a reason code."""
    running = recording.is_recording(session)
    if running is None:
        return "recording_unknown"
    return "recording" if running else "recording_due"


def _text(value):
    return value[:MAX_TEXT] if isinstance(value, str) else None


def _whole(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def public(record):
    """`update.transaction` from a status file, a marker or the last-transaction record."""
    if not isinstance(record, dict):
        return None
    source = dict(record)
    if isinstance(source.get("to"), dict) and "target" not in source:
        source["target"] = source["to"].get("version")
    if isinstance(source.get("from"), dict):
        source["from"] = source["from"].get("version")
    out = {key: None for key in PUBLIC_KEYS}
    for key in ("id", "started_by", "target", "from", "error"):
        out[key] = _text(source.get(key))
    for key in ("started", "finished"):
        out[key] = _whole(source.get(key))
    phase = source.get("phase")
    out["phase"] = phase if phase in updatehelper.PHASES else None
    result = source.get("result")
    out["result"] = result if result in updatehelper.RESULTS and out["phase"] == "finished" \
        else None
    if out["id"] is None or not updatehelper.TRANSACTION_ID.fullmatch(out["id"]):
        return None
    return out


def parse_integration(payload):
    """The integration's topic as the rule reads it, or None when it says nothing usable."""
    try:
        data = json.loads(bytes(payload or b"").decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    integration = data.get("integration")
    contract = data.get("contract")
    plugin_min = data.get("plugin_min")
    if not trust.is_version(integration) or _whole(contract) is None:
        return None
    if plugin_min is not None and not trust.is_version(plugin_min):
        plugin_min = None
    return {"integration": integration, "contract": contract, "plugin_min": plugin_min}


class SelfUpdater:
    """The permission, the capability, the guards, the launch, the doors and the restart question.

    One per bridge, built with it and kept across reloads: a transaction outlives every session
    the plugin opens while it runs. `closed` and `silent` are what the bridge and the dispatcher
    ask before they publish or run anything.
    """

    # Test seams. On a receiver the root is `/` and the plugin directory is this module's own.
    root = "/"
    plugin_directory = None
    clock = staticmethod(time.time)
    monotonic = staticmethod(time.monotonic)

    def __init__(self, bridge):
        self.bridge = bridge
        self.claimed = False
        self.closed = False
        self.silent = False
        self.integration = None
        self._current = None
        self._transaction = None
        self._marker_read = False
        self._container = None
        self._finished_container = None
        self._poll_ticker = Ticker(self._poll, "self-update status")
        self._retraction_ticker = Ticker(self._retraction_poll, "self-update retraction")
        self._collapse_ticker = Ticker(self._collapse, "self-update softcam collapse")
        self._queue = []
        self._outstanding = []
        self._retraction_deadline = None

    # ------------------------------------------------------------------ paths --

    def _path(self, relative):
        return os.path.join(self.root, relative)

    @property
    def backups(self):
        return self._path(updatehelper.BACKUPS)

    @property
    def lock_dir(self):
        return os.path.join(self.backups, updatehelper.LOCK_NAME)

    def _receiver(self):
        return updatehelper.Receiver(root=self.root, proc=self._path("proc"))

    def _directory(self):
        return self.plugin_directory or os.path.dirname(os.path.abspath(__file__))

    # ------------------------------------------------------------- capability --

    def probe(self):
        """Claim `self_update` or not, once per start. Never raises."""
        try:
            self.claimed, why = self._can_update()
        except Exception:
            LOG.exception("could not tell whether this plugin can update itself")
            self.claimed, why = False, "the check raised"
        if not self.claimed:
            LOG.debug("no self_update capability: %s", why)
        return self.claimed

    def _can_update(self):
        packaged, why = installed_by_package_manager(self.root, self.plugin_directory)
        if not packaged:
            return False, why
        for program in (START_STOP_DAEMON, PYTHON):
            path = self._path(program.lstrip("/"))
            if not (os.path.isfile(path) and os.access(path, os.X_OK)):
                return False, "no executable " + program
        for name in (HELPER_SOURCE,) + updatehelper.COPIED_MODULES:
            if not os.path.isfile(os.path.join(self._directory(), name)):
                return False, "the helper file " + name + " is missing"
        return True, None

    # ------------------------------------------------------ the integration topic --

    def integration_topic(self):
        node = self.bridge.node_id
        return INTEGRATION_TOPIC + node if node else None

    def on_integration(self, payload):
        """The companion integration's word: its version, contract major and plugin floor."""
        if not payload:
            if self.integration is not None:
                LOG.info("the integration's topic was retracted; the rule uses its own again")
            self.integration = None
        else:
            parsed = parse_integration(payload)
            if parsed is None:
                LOG.warning("ignoring an unreadable message on the integration's topic")
                return
            if parsed == self.integration:
                return
            self.integration = parsed
            LOG.info("the integration says: %s contract %s, plugin at least %s",
                     parsed["integration"], parsed["contract"], parsed["plugin_min"] or "-")
        if not self.closed:
            self.bridge.updates.publish()

    # -------------------------------------------------------------- the topic --

    def transaction_payload(self):
        return dict(self._transaction) if self._transaction is not None else None

    def doors_refusal(self):
        """The sentence every command gets while the doors are closed, or None."""
        return DOORS if self.closed else None

    def _publish(self):
        if not self.silent:
            try:
                self.bridge.updates.publish()
            except Exception:
                LOG.exception("could not publish the update topic")

    # ---------------------------------------------------------------- start --

    def on_start(self):
        """Right after logging is configured, before the provisioning file and `enabled`. Once."""
        if self._marker_read:
            return
        self._marker_read = True
        try:
            last = updatehelper.read_json(self._path(updatehelper.LAST))
            if self._transaction is None:
                self._transaction = public(last)
            self._read_marker()
        except Exception:
            LOG.exception("could not read the update marker")

    def _lock_owner(self):
        return updatehelper.read_json(os.path.join(self.lock_dir, "owner.json"))

    def _running(self):
        # What this process runs - the bridge was built with it ().
        build = self.bridge.build or {}
        return {"version": __version__, "commit": build.get("commit") or ""}

    def _read_marker(self):
        path = self._path(updatehelper.MARKER)
        marker = updatehelper.read_json(path)
        if marker is None:
            return
        ident = marker.get("id")
        if not isinstance(ident, str) or not updatehelper.TRANSACTION_ID.fullmatch(ident):
            LOG.warning("discarding an update marker without a transaction id")
            self._remove_marker(None)
            return
        receiver = self._receiver()
        boot = receiver.boot_id()
        same_boot = bool(boot) and marker.get("boot_id") == boot
        owner = self._lock_owner()
        locked = owner is not None and owner.get("id") == ident
        if not same_boot and not locked:
            LOG.info("discarding the update marker of %s: another boot, and no lock of it", ident)
            self._remove_marker(ident)
            return
        if same_boot and self._past_deadline(marker, receiver):
            LOG.info("discarding the update marker of %s: past its deadline", ident)
            self._remove_marker(ident)
            return
        running = self._running()
        if marker.get("phase") == "finished":
            LOG.info("update %s finished before this start: %s", ident, marker.get("result"))
            self._end(public(marker), marker.get("reason"), followed=True)
            return
        if not same_boot:
            # The receiver lost power (or was reset) with the transaction under way: nothing of
            # it runs any more, and what is running now says how it ended.
            target = marker.get("to") if isinstance(marker.get("to"), dict) else {}
            before = marker.get("from") if isinstance(marker.get("from"), dict) else {}
            if running["version"] == target.get("version") and (
                    running["commit"] == target.get("commit")):
                result, reason = "installed", None
            elif running["version"] == before.get("version"):
                result, reason = "rolled_back", "interrupted"
            else:
                result, reason = "interrupted", "interrupted"
            record = public(marker) or {}
            record.update(phase="finished", result=result, finished=int(self.clock()),
                          error=None if result == "installed" else
                          str(updatehelper.Fail("interrupted", detail="the receiver restarted")))
            LOG.warning("update %s was cut short by a restart of the receiver: %s",
                        ident, result)
            self._end(record, reason, followed=True)
            return
        directory = os.path.join(self.backups, "update-" + ident)
        if marker.get("phase") in ("restarting", "proving"):
            self._confirm_start(directory, ident)
        self._current = {"id": ident, "directory": directory, "ours": False, "downgrade": False,
                         "launched": self.monotonic(), "finished_seen": None,
                         "deadline": marker.get("deadline")}
        self._transaction = public(marker)
        self._poll_ticker.start(POLL_MILLISECONDS)

    def _past_deadline(self, marker, receiver):
        deadline = marker.get("deadline")
        uptime = receiver.uptime()
        return (isinstance(deadline, (int, float)) and not isinstance(deadline, bool)
                and uptime is not None and uptime > deadline)

    def _confirm_start(self, directory, ident):
        """Tier 1: tell the helper which build started - never from the process that asked."""
        request = updatehelper.read_json(os.path.join(directory, REQUEST))
        if request is None or request.get("id") != ident:
            return
        if request.get("enigma2_pid") == os.getpid():
            return
        running = self._running()
        try:
            updatehelper.write_json(os.path.join(directory, STARTED), {
                "version": running["version"], "commit": running["commit"], "pid": os.getpid()})
            LOG.info("update %s: this start confirmed as %s", ident,
                     buildid.display_version(__version__, self.bridge.build))
        except OSError as error:
            LOG.warning("update %s: could not confirm this start (%s)", ident, error)

    def _remove_marker(self, ident):
        path = self._path(updatehelper.MARKER)
        if ident is not None:
            marker = updatehelper.read_json(path)
            if marker is not None and marker.get("id") != ident:
                return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as error:
            LOG.warning("could not remove the update marker: %s", error)

    # ---------------------------------------------------------------- command --

    def request(self, text, origin=MQTT, downgrade=False):
        """`cmd/update`: the refusal, or None once the helper has been started.

        `downgrade` is the television's or the page's confirmed question; over MQTT there is
        none, and a lower version is always refused.
        """
        bridge = self.bridge
        if not granted(bridge.value, PERMISSION, origin):
            return Refusal(NOT_PERMITTED, "not_permitted")
        if not self.claimed:
            return Refusal(NOT_PACKAGED, "no_capability")
        if self._current is not None or self._lock_held():
            return _refusal("busy")
        if updatehelper.opkg_busy(self._receiver()):
            return _refusal("opkg_busy")
        if power.in_standby():
            return Refusal(STANDBY, "standby")
        refusal = recording.guard(bridge.session)
        if refusal:
            return Refusal(refusal, _recording_reason(bridge.session))
        follower = bridge.publisher("epg_import")
        if follower.blocks_power() if follower is not None else epgimport.running():
            return Refusal(EPG_IMPORT_RUNNING, "epg_import")
        ready = power.can_quit(bridge.session)
        if ready:
            return Refusal(ready, "cannot_restart")
        payload = _payload(text)
        if payload is None:
            return _refusal("bad_request")
        entry, refusal = self._entry(payload["version"])
        if refusal:
            return refusal
        version = entry["version"]
        lower = trust.version_key(version) < trust.version_key(__version__)
        allowed_downgrade = lower and downgrade and origin in (SCREEN, PAGE)
        if lower and not allowed_downgrade:
            return _refusal("downgrade")
        if self._current_release(entry):
            return Refusal(CURRENT.format(version=version), "current")
        sha = payload.get("sha256")
        if sha is not None and sha != entry["sha256"]:
            return _refusal("checksum")
        relay = payload.get("relay")
        if relay is not None and not self._relay_valid(relay):
            return Refusal(RELAY, "relay")
        if not self._enough_space(entry):
            return Refusal(NO_SPACE, "no_space")
        if self._rate_limited():
            return Refusal(RATE_LIMITED, "rate_limited")
        if origin in (SCREEN, PAGE):
            started_by = origin
        else:
            started_by = "home_assistant" if relay is not None else "mqtt"
        return self._launch(entry, sha, relay, started_by, allowed_downgrade)

    def _lock_held(self):
        if not os.path.isdir(self.lock_dir):
            return False
        return not updatehelper.lock_is_stale(self._receiver(), self.lock_dir)

    def _index(self):
        held = getattr(self.bridge.updates, "_held", None)
        return held[0] if held else None

    def _installed(self):
        return updatehelper.installed_packages(self.root)

    def _integration_mode(self):
        return self.bridge.value("ha_mode") == "integration"

    def _entry(self, version):
        """The signed entry of `version` after the index's rules, or a refusal."""
        index = self._index()
        if index is not None and version == "latest":
            latest, _available = offer(index, self._installed(), self._integration_mode(),
                                       CONTRACT, self.integration)
            version = latest or version
        entry = None
        if index is not None:
            entry = next((e for e in index["releases"] if e["version"] == version), None)
        if entry is None:
            return None, _refusal("unknown_version", version=version)
        if entry["withdrawn"] is not None:
            return None, _refusal("withdrawn", version=version, detail=entry["withdrawn"])
        floor = updatehelper.floor_of(index, self.integration)
        if trust.version_key(version) < trust.version_key(floor):
            return None, _refusal("below_floor", version=version)
        problem = updatehelper.unmet(entry, CONTRACT, self.integration, self._integration_mode(),
                                     self._installed())
        if problem is not None:
            reason, detail = problem
            return None, _refusal(reason, version=version, detail=detail)
        return entry, None

    def _current_release(self, entry):
        """That release runs, and the build on disk is that release too."""
        if entry["version"] != __version__:
            return False
        running = self._running()["commit"]
        on_disk = buildid.read(self.bridge._build_path)
        return bool(running) and running == entry["commit"] and on_disk is not None and \
            on_disk.get("commit") == entry["commit"]

    def _relay_valid(self, relay):
        if not isinstance(relay, dict):
            return False
        url = relay.get("url")
        expires = _whole(relay.get("expires"))
        if not isinstance(url, str) or not RELAY_URL.fullmatch(url) or expires is None:
            return False
        return expires > int(self.clock())

    def _enough_space(self, entry):
        total = 0
        for base, _dirs, files in os.walk(self._directory()):
            for name in files:
                try:
                    total += os.lstat(os.path.join(base, name)).st_size
                except OSError:
                    pass
        needed = 2 * entry["size"] + total + 256 * 1024 + updatehelper.SPACE_MARGIN
        path = self.backups
        while not os.path.isdir(path) and os.path.dirname(path) != path:
            path = os.path.dirname(path)
        try:
            return self._receiver().free_bytes(path) >= needed
        except OSError:
            # The helper measures again, on the path it writes to.
            return True

    def _rate_limited(self):
        """Ten minutes since the last transaction ended - by uptime within one boot.

        Many receivers boot with the clock in 1970 and jump when NTP answers, so within the
        boot that wrote the record its uptime is the measure; across a boot, or with a record
        that has no uptime, the wall clock, which then cannot go backwards into a refusal.
        """
        last = updatehelper.read_json(self._path(updatehelper.LAST))
        if not last:
            return False
        receiver = self._receiver()
        boot, uptime = receiver.boot_id(), receiver.uptime()
        ended = last.get("uptime")
        if (boot and last.get("boot_id") == boot and uptime is not None
                and isinstance(ended, (int, float)) and not isinstance(ended, bool)):
            return 0 <= uptime - ended < RATE_LIMIT_SECONDS
        finished = _whole(last.get("finished"))
        if finished is None:
            return False
        now = int(self.clock())
        return finished <= now < finished + RATE_LIMIT_SECONDS

    # ----------------------------------------------------------------- launch --

    def _launch(self, entry, sha, relay, started_by, downgrade):
        ident = secrets.token_hex(6)
        directory = os.path.join(self.backups, "update-" + ident)
        updates = self.bridge.updates
        request = {
            "id": ident,
            "target": entry["version"],
            "sha256": sha,
            "relay": None if relay is None else {"url": relay["url"],
                                                 "expires": relay["expires"]},
            "started_by": started_by,
            "downgrade": bool(downgrade),
            "from": self._running(),
            "enigma2_pid": os.getpid(),
            "keys": trust.keys_to_data(updates.keys) if updates.keys else [],
            "acceptance": bool(updates.acceptance),
            "origin": updates.origin,
            "contract": CONTRACT,
            "integration": self.integration,
            "integration_mode": self._integration_mode(),
        }
        try:
            os.makedirs(self.backups, mode=0o700, exist_ok=True)
            os.mkdir(directory, 0o700)
            source = self._directory()
            shutil.copyfile(os.path.join(source, HELPER_SOURCE),
                            os.path.join(directory, HELPER_NAME))
            for name in updatehelper.COPIED_MODULES:
                shutil.copyfile(os.path.join(source, name), os.path.join(directory, name))
            updatehelper.write_json(os.path.join(directory, REQUEST), request)
        except OSError as error:
            LOG.error("could not prepare update %s: %s", ident, error)
            shutil.rmtree(directory, ignore_errors=True)
            return Refusal(NOT_STARTED.format(detail=str(error)), "internal_error")
        command = self.command_line(directory, ident)
        container = self._new_container()
        rejected = True
        if container is not None:
            try:
                rejected = container.execute(command)
            except Exception:
                LOG.exception("start-stop-daemon could not be run")
        if rejected:
            shutil.rmtree(directory, ignore_errors=True)
            self._container = None
            return Refusal(NOT_STARTED.format(detail="the helper could not be launched"),
                           "internal_error")
        now = int(self.clock())
        self._current = {"id": ident, "directory": directory, "ours": True,
                         "downgrade": bool(downgrade), "launched": self.monotonic(),
                         "finished_seen": None, "deadline": None}
        self._transaction = {"id": ident, "started_by": started_by, "target": entry["version"],
                             "from": __version__, "phase": "downloading", "started": now,
                             "finished": None, "result": None, "error": None}
        LOG.warning("update %s accepted from %s: %s -> %s", ident, started_by, __version__,
                    entry["version"])
        self._poll_ticker.start(POLL_MILLISECONDS)
        self._publish()
        return None

    @staticmethod
    def command_line(directory, ident):
        """argv of the launch, as one line for `eConsoleAppContainer`: fixed paths and hex only."""
        return " ".join((
            START_STOP_DAEMON, "-S", "-b", "-m", "-p", os.path.join(directory, PID_FILE),
            "-x", PYTHON, "--", os.path.join(directory, HELPER_NAME), ident,
        ))

    def _new_container(self):
        self._finished_container = None
        factory = enigma_attribute("eConsoleAppContainer")
        if factory is None:
            return None
        try:
            container = factory()
        except Exception:
            LOG.exception("could not create a console container")
            return None
        if not _attach(container, "appClosed", self._launched):
            return None
        self._container = container
        return container

    def _launched(self, retval=0):
        # Not detached here: the container is walking its own callback list.
        self._finished_container, self._container = self._container, None
        current = self._current
        if not retval or current is None or not current["ours"]:
            return
        if os.path.exists(os.path.join(current["directory"], STATUS)):
            return
        self._launch_failed("start-stop-daemon exited with status " + str(retval))

    def _launch_failed(self, detail):
        current, self._current = self._current, None
        self._poll_ticker.stop()
        LOG.error("update %s did not start: %s", current["id"], detail)
        shutil.rmtree(current["directory"], ignore_errors=True)
        error = NOT_STARTED.format(detail=detail)
        record = dict(self._transaction or {}, phase="finished", result="failed",
                      finished=int(self.clock()), error=error)
        self._transaction = public(record)
        self._publish()
        self.bridge.publish_last_error(COMMAND, Refusal(error, "internal_error"))

    # ------------------------------------------------------------ following --

    def _poll(self):
        current = self._current
        if current is None:
            self._poll_ticker.stop()
            return
        directory = current["directory"]
        status = updatehelper.read_json(os.path.join(directory, STATUS))
        if status is not None and status.get("id") != current["id"]:
            status = None
        record = status
        if record is None:
            marker = updatehelper.read_json(self._path(updatehelper.MARKER))
            if marker is not None and marker.get("id") == current["id"]:
                record = marker
        if record is None:
            last = updatehelper.read_json(self._path(updatehelper.LAST))
            if last is not None and last.get("id") == current["id"]:
                record = last
        if record is None:
            if current["ours"] and self._helper_gone(current):
                self._launch_failed("the helper stopped before it wrote anything")
            elif current["ours"] and self.monotonic() - current["launched"] > LAUNCH_WAIT_SECONDS:
                self._launch_failed("the helper wrote nothing within "
                                    + str(LAUNCH_WAIT_SECONDS) + " s")
            elif not current["ours"] and not os.path.isdir(directory):
                LOG.info("update %s left nothing to follow", current["id"])
                self._current = None
                self._poll_ticker.stop()
            return
        payload = public(record)
        if payload is not None and payload != self._transaction:
            self._transaction = payload
            self._publish()
        phase = record.get("phase")
        if phase != "finished" and self._helper_gone(current):
            self._helper_died(current, record)
            return
        if phase == "finished":
            owner = self._lock_owner()
            released = owner is None or owner.get("id") != current["id"]
            if current["finished_seen"] is None:
                current["finished_seen"] = self.monotonic()
            if released or self.monotonic() - current["finished_seen"] > RELEASE_WAIT_SECONDS:
                self._current = None
                self._poll_ticker.stop()
                self._end(payload, record.get("reason"), followed=not current["ours"])
            return
        if current["ours"] and phase == "restarting" and not self.closed:
            self._close_doors()
            return
        if not current["ours"] and self._past_deadline(record, self._receiver()):
            LOG.warning("update %s is past its deadline; no longer following it", current["id"])
            self._current = None
            self._poll_ticker.stop()
            self._remove_marker(current["id"])

    def _helper_gone(self, current):
        """Whether the helper that `start-stop-daemon` recorded has stopped running.

        Its status file says nothing about that: a helper killed outright never writes
        `finished`, and the file's stamp moves only with a phase. The pid file does - written
        by `start-stop-daemon -m` for the process that became the helper - checked against
        `/proc`, and against the command line, so a pid used again by another program is not
        taken for it. No pid file, no verdict.
        """
        try:
            with open(os.path.join(current["directory"], PID_FILE), encoding="ascii") as handle:
                pid = int(handle.read().strip())
        except (OSError, ValueError, UnicodeDecodeError):
            return False
        try:
            with open(self._path(os.path.join("proc", str(pid), "cmdline")), "rb") as handle:
                command = handle.read(4096)
        except OSError:
            return True
        return HELPER_NAME.encode("ascii") not in command

    def _helper_died(self, current, record):
        """The helper is gone mid-way: say so, and never wait for an end it cannot write."""
        self._current = None
        self._poll_ticker.stop()
        detail = "the update helper stopped"
        if record.get("phase") in ("installing", "restarting", "proving", "rolling_back"):
            detail += ("; the plugin's files may not be the running version's - restart the "
                       "receiver's interface, or install the plugin again")
        error = str(updatehelper.Fail("interrupted", detail=detail))
        LOG.error("update %s: %s", current["id"], error)
        payload = public(dict(record, phase="finished", result="interrupted",
                              finished=int(self.clock()), error=error))
        self._end(payload, "interrupted", followed=not current["ours"])

    def _end(self, payload, reason, followed):
        """Say how a transaction ended; reopen the doors with a fresh session when closed."""
        if payload is not None:
            self._transaction = payload
        result = (payload or {}).get("result")
        error = (payload or {}).get("error")
        ident = (payload or {}).get("id")
        LOG.warning("update %s ended: %s%s", ident, result, " (" + error + ")" if error else "")
        self._remove_marker(ident)
        refusal = None
        if result != "installed" and error:
            refusal = Refusal(error, reason or result or "failed")
        if self.closed:
            self.closed = False
            self.silent = False
            self._retraction_ticker.stop()
            self._queue = []
            self._outstanding = []
            if refusal is not None:
                self.bridge._pending_error = (COMMAND, refusal)
            # A fresh session republishes availability, the snapshot - `update` with this
            # transaction's end - the announcement and discovery, and whatever a downgrade's
            # retraction took.
            self.bridge.reload()
            return
        self._publish()
        if refusal is not None:
            if self.bridge.connected:
                self.bridge.publish_last_error(COMMAND, refusal)
            else:
                # Read at start, before any session: said once the first one is up.
                self.bridge._pending_error = (COMMAND, refusal)
        if followed and result == "installed":
            self._schedule_collapse()

    # ------------------------------------------------------------- the doors --

    def _close_doors(self):
        current = self._current
        self.closed = True
        self.bridge._stop_publishers()
        LOG.warning("update %s: the new files are on disk; commands, the page and the setup "
                    "screen are closed until the interface restarts", current["id"])
        if current["downgrade"]:
            self._begin_retraction()
            return
        self._ask_restart()

    def _begin_retraction(self):
        bridge = self.bridge
        # S-a: from here nothing is published until `reload()`.
        self.silent = True
        if not bridge.connected:
            self._withdraw("retraction")
            return
        keep = bridge.topic("availability")
        self._queue = sorted(topic for topic in bridge.state.retained_topics if topic != keep)
        self._outstanding = []
        self._retraction_deadline = self.monotonic() + RETRACTION_TIMEOUT_SECONDS
        LOG.info("update %s: retracting %d retained topic(s) at QoS %d before the downgrade",
                 self._current["id"], len(self._queue), RETRACTION_QOS)
        if not self._feed():
            return
        if not self._retraction_ticker.start(RETRACTION_POLL_MILLISECONDS):
            self._withdraw("retraction")

    def _feed(self):
        client = self.bridge.client
        while self._queue and len(self._outstanding) < BATCH:
            topic = self._queue.pop(0)
            info = client.publish(topic, "", qos=RETRACTION_QOS, retain=True) \
                if client is not None else None
            if info is None or getattr(info, "rc", 0) not in (0, None):
                LOG.warning("the session refused the retraction of %s", topic)
                self._retraction_failed()
                return False
            self._outstanding.append(info)
        return True

    def _retraction_poll(self):
        if self._current is None or not self.silent:
            self._retraction_ticker.stop()
            return
        if not self.bridge.connected:
            self._retraction_failed()
            return
        waiting = []
        for info in self._outstanding:
            try:
                done = info.is_published()
            except Exception as error:
                LOG.warning("a retraction publish failed: %s", error)
                self._retraction_failed()
                return
            if not done:
                waiting.append(info)
        self._outstanding = waiting
        if not self._feed():
            return
        if not self._queue and not self._outstanding:
            self._retraction_ticker.stop()
            bridge = self.bridge
            bridge.forget_everything_published()
            bridge.state.save(force=True)
            LOG.info("update %s: the broker acknowledged every retraction", self._current["id"])
            self._ask_restart()
            return
        if self.monotonic() > self._retraction_deadline:
            self._retraction_failed()

    def _retraction_failed(self):
        self._retraction_ticker.stop()
        self._queue = []
        self._outstanding = []
        LOG.error("update %s: the retraction was not acknowledged; withdrawing the downgrade",
                  self._current["id"] if self._current else "-")
        self._withdraw("retraction")

    def _ask_restart(self):
        """R1: the image's clean quit, with its question bounded at sixty seconds."""
        current = self._current
        try:
            updatehelper.write_json(os.path.join(current["directory"], RESTART),
                                    {"pid": os.getpid()})
        except OSError as error:
            LOG.error("update %s: could not tell the helper about the restart (%s)",
                      current["id"], error)
            self._withdraw("question")
            return
        module = power.standby_module()
        screen = getattr(module, "TryQuitMainloop", None) if module is not None else None
        session = self.bridge.session
        if screen is None or session is None:
            LOG.error("update %s: this image cannot be asked to restart", current["id"])
            self._withdraw("question")
            return
        try:
            session.openWithCallback(self._answered, screen, QUIT_RESTART,
                                     timeout=QUESTION_TIMEOUT_SECONDS, default_yes=False)
        except Exception:
            LOG.exception("update %s: TryQuitMainloop raised", current["id"])
            self._withdraw("question")
            return
        LOG.info("update %s: interface restart requested", current["id"])

    def _answered(self, *_result):
        """The restart screen closed and this process still runs: nobody restarted it."""
        if self._current is None:
            return
        LOG.warning("update %s: the receiver did not restart its interface; withdrawing",
                    self._current["id"])
        self._withdraw("question")

    def _withdraw(self, reason):
        current = self._current
        if current is None:
            return
        try:
            updatehelper.write_json(os.path.join(current["directory"], WITHDRAW),
                                    {"reason": reason})
        except OSError as error:
            # The helper withdraws by itself when no restart comes within its own bound.
            LOG.error("update %s: could not tell the helper to withdraw (%s)", current["id"],
                      error)

    # -------------------------------------------------------------- the end --

    def _schedule_collapse(self):
        """S3: one softcam collapse after a proved install, where the box may and can."""
        publisher = self.bridge.publisher("softcam")
        if publisher is None or not self.bridge.value("softcam_restart_allowed"):
            return
        self._collapse_ticker.start(COLLAPSE_DELAY_MILLISECONDS, single=True)

    def _collapse(self):
        publisher = self.bridge.publisher("softcam")
        if publisher is None or not self.bridge.value("softcam_restart_allowed"):
            return
        refusal = publisher.restart(reason=softcam.AUTOHEAL, origin=MQTT)
        if refusal:
            LOG.info("no softcam collapse after the update: %s", refusal)

    def abandon(self):
        """The interface is going away; nothing more is started from this process."""
        self._retraction_ticker.stop()
        self._collapse_ticker.stop()
        self._poll_ticker.stop()


def _payload(text):
    """`{"version", "sha256", "relay"}` of a `cmd/update`, or None when it is not one."""
    stripped = str(text or "").strip()
    if not stripped.startswith("{"):
        return None
    try:
        payload = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    if version != "latest" and not trust.is_version(version):
        return None
    return payload
