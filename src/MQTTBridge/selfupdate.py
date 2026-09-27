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

**The doors (M3).** From the helper's phase `installing` on, the package manager is rewriting
the files on disk under the running process, and after it they are the new release's while the
process is still the old one. A module imported for the first time now would be half-written or
new code in an old process, so every module this path and the closed state use is imported
when the plugin starts, the publishers are stopped, and every command - over MQTT, from the
page, from the setup screen - answers "an update is being applied on the receiver". When the
package manager fails, the helper puts the old files back and says `finished`, and the doors
open again with a fresh session. When nothing put them back - the restore failed or left opkg's
records naming the new version (`restore_failed`, `restore_incomplete`), or the helper
stopped once the package manager had started, perhaps leaving it running as an orphan - the
doors stay closed for good and say to install the plugin again; that is judged from what the
helper left behind, so a failure that fell between two polls closes them too. After a rollback
that put the old files back under an interface it could not stop (`not_stopped`), a process
whose pid the helper lists as left running (`record.unstopped`) is that interface: its files are
the previous version's now, so its doors stay closed as well - the repair is a restart of the
interface, and `restart_gui` is the one command they let through.

The restart itself waits for the helper's `restarting`: for a downgrade chosen at the television
or on the page, every retained topic the node owns except `availability` is first retracted at
QoS 1, because the older release does not know the newer one's topics and would leave them on
the broker for ever (section 11 v of the plan, B1). From
that retraction on nothing at all is published (S-a): refusals are logged, not put on
`last_error`, and the `update` relay stops, so nothing the older release does not know is
re-created behind the retraction.

**One transaction at a time.** `cmd/uninstall` runs the package manager too, and a removal next
to an update ends in whichever the package manager did last. So an uninstall is refused while an
update runs or the shared lock is held (`busy`), and an update while an uninstall runs.

**The household at the restart.** The request was judged when it came; the restart comes
minutes later. Right before the question the standby, recording and EPG-import guards are asked
again, and a receiver that went into standby meanwhile - or started recording - is not restarted:
the update is withdrawn, with that reason. It is not held back for later, because the helper's
own bound would end it anyway, and a restart hours later is not what anybody asked for.

**The restart (rule R1).** `TryQuitMainloop(session, 3, timeout=60, default_yes=False)` - the
image's own clean quit, which saves the settings and with them the channel being watched. It
quits at once when nothing holds, and asks when a recording, a stream, timeshift or a job does.
**The question's answer is not read from the callback's argument**: the image's
`TryQuitMainloop.close(False)` ends in `MessageBox.close(self, True)`, and a quit never calls
back at all. So a callback, whatever it carries, means the interface is still running: the
helper is told to withdraw. Nothing here reads the dialog stack - a receiver with a
session-start screen (the HbbTV plugin's zero-size `VBMain`) stacks every dialog one deeper.

**A receiver without internet (the relay handshake, OD 2).** An install asked for at the
television or on the page carries no download address: the receiver fetches the release itself,
which the person asking consents to - and whether it can is what decides who fetches. So once
every refusal has been passed, the install looks at the origin's word (`update.origin`). A word
of the last ten minutes is taken as it is. Any other - `unknown` on a receiver that has never
checked, which is the default, or a word read back after a restart or grown old - is asked for
again first: the check's own five-second probe, on the check's worker, while the install waits
(`relay_wait`, phase `probe`, at most `PROBE_WAIT_SECONDS`). That is the one probe the asking
consents to (spec ae.4, "an explicit TV/page action"); nothing else probes, and a command over
MQTT never does. `reachable`: the helper fetches the release itself. `unreachable`: the plugin
asks the companion integration instead. A helper whose own download could not reach the origin
says so in its record, and that becomes the origin's word too. Its failed end starts the
ten-minute limit between updates, so that word lasts the limit and ten minutes more
(`HELPER_WORD_SECONDS`): the next install at the television that the limit lets through asks
Home Assistant without looking again. After that, or after a restart, it is looked at again.

To ask, the plugin publishes `relay_request` (QoS 1, never retained) with a fresh id, the version
and the serial of the index it holds, and waits at most 120 s for `cmd/relay` naming that id -
but only once the integration has said on `enigma2mqtt/integration/<node>` that it is there to
answer: without that word nobody would, and the household is told at once instead of in two
minutes. The answer is transport, not an instruction: it is taken only for the id and the
version this receiver asked for, with an address of Home Assistant's one shape that has not
expired - the rule `updatehelper.relay_ok`, the same the helper asks again before it downloads.
Anything else is logged and dropped, and the wait goes on. **The first answer that passes the
rule is taken**, whoever sent it: nothing on the broker proves that an answer comes from Home
Assistant, and the id is readable by every client that reads `relay_request`. A broker client
that answers first with an address of its own is therefore taken, and Home Assistant's answer
after it is dropped as stale; what that client can do with it is deny and delay, not install -
the helper verifies the bytes it serves against the signed entry (size and sha256), refuses
them with nothing changed, and the failed transaction starts the ten-minute limit between
updates. The spec's threat table accepts exactly that ("can only deny or delay"). Up to two
minutes have passed by the time an answer is taken, so the whole refusal table is asked again
before the helper starts, and the transaction is still the television's or the page's: a
downgrade chosen there stays one. With no answer, or no broker or integration to ask, the install
is refused (`no_relay`); nothing was changed. An answer of Home Assistant's shape that only this
receiver's clock calls expired - a clock ahead of Home Assistant's by more than the address
lives - ends the wait with `clock_skew` instead, because "Home Assistant did not answer" would
send the household looking for the wrong fault. Its sentence says only what the receiver knows:
an answer came whose address its clock calls expired. It does not say that Home Assistant sent
it - any broker client may have (review round 2) - and setting a wrong clock is harmless either
way. A command over MQTT brings its own address or
asks the receiver to fetch (spec ae.4), so it never asks back.

Every wait has a timer, and every wait also ends by its age on the monotonic clock whenever it is
looked at (`busy`, `relay_wait`), so a timer that could not be started cannot leave the receiver
saying `busy` until the plugin restarts.

**The marker.** A plugin that starts reads `/etc/enigma2/mqttbridge-update.json` right after its
logging is configured - before the provisioning file and before it asks whether it is switched
on - so a new release that is switched off still confirms that it started (`started.json`, the
tier-1 proof). A marker from another boot whose lock is still there tells of a power loss, and
is answered by the running version: `installed`, `rolled_back` or `interrupted` - and so is a
marker whose helper is provably gone in this boot, which is how a restart after a helper that
died says how the transaction ended.
"""

import json
import math
import os
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
from .updatecheck import ORIGIN_FRESH_SECONDS, UNREACHABLE, offer
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
# How long an update helper's "the origin gave no answer" counts as the origin's word (see the
# module). The failed transaction that brings it starts the limit above, so the word must outlive
# the limit to reach the next install at all; after that it lasts as long as a probe's.
HELPER_WORD_SECONDS = RATE_LIMIT_SECONDS + ORIGIN_FRESH_SECONDS
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

# The relay handshake (see the module): Home Assistant's answer is `cmd/relay`, the question
# `relay_request`, an event - QoS 1 so the broker takes it, never retained so nobody answers a
# leftover after a reconnect.
RELAY_COMMAND = "relay"
RELAY_REQUEST = "relay_request"
RELAY_QOS = 1
RELAY_WAIT_SECONDS = 120
# How long an install at the television or on the page waits for the origin probe it asked for
# (see the module). The probe itself gives up after five seconds; the rest is room for a check
# already running on the same worker (two rounds of five and ten seconds) to finish first.
PROBE_WAIT_SECONDS = 60
# `relay_wait()`'s phases: looking at the origin, and waiting for Home Assistant's answer.
PROBING = "probe"
ASKING = "relay"

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
# An install at the television or on the page, with the origin unreachable and no answer to the
# relay handshake (or no broker to ask); the screen says it in the household's language.
NO_RELAY = ("the receiver cannot reach the plugin's release origin and Home Assistant did not "
            "answer, so the update cannot be installed")
# An answer of Home Assistant's shape came, with an address this receiver's clock already calls
# expired. Who sent it is not known here, so the sentence does not say.
CLOCK_SKEW = ("an answer arrived whose download address had already expired by the receiver's "
              "clock; if the receiver's clock is wrong, set it and try again")
NO_SPACE = updatehelper.SENTENCES["no_space"]
RATE_LIMITED = "an update ran less than ten minutes ago"
DOORS = "an update is being applied on the receiver"
NOT_STARTED = "the update could not be started: {detail}"
# `busy`, when the lock's owner is a self-update helper that is provably gone: the lock stays
# until the stale rule every implementation shares lets it go (TRANSACTION.md section 2.3).
STOPPED = ("the previous update stopped without finishing; a new one is possible in about "
           "{minutes} minutes, when its lock on the receiver expires")
UNINSTALLING = "the plugin is being removed from the receiver"
# After a `failed` end whose restore did not complete: the files may be the new release's, or a
# mix, under this process, and only a reinstall from outside it can repair them (spec ae.7a).
STUCK = ("an update failed and the plugin's previous files could not be put back; install the "
         "plugin again (from Home Assistant: force plugin reinstall)")
# The same end after a helper that stopped once the package manager had started: nothing put
# the old files back, and an orphaned package manager may still be writing (review round 2).
STUCK_STOPPED = ("an update stopped part-way, so the plugin's files may not be the running "
                 "version's; install the plugin again (from Home Assistant: force plugin "
                 "reinstall)")
# ... and after a restore that put the code back but not all of opkg's records or the settings
# block (`record.restore` `partial: ...`): the package manager's view no longer matches the
# files, which only a reinstall puts right.
STUCK_PARTIAL = ("an update failed; the plugin's previous version is back, but not all of its "
                 "package records; install the plugin again (from Home Assistant: force plugin "
                 "reinstall)")
# The helper's own reasons for a restore that did not complete; their sentence already names
# the reinstall, so `last_error` carries it alone.
RESTORE_REASONS = ("restore_failed", "restore_incomplete")
# ... and after `not_stopped` (TRANSACTION.md section 7, review round 3): R2 put the previous
# version's files back, but saw no stop and no new interface, so the process reading the end is
# the one it could not stop - running the code it had over files that are no longer it. The files
# and opkg's records agree, so the repair is a restart of the interface, not a reinstall.
STUCK_UNSTOPPED = ("an update was rolled back while the receiver's interface kept running, so "
                   "the plugin's files are no longer the ones it runs; restart the receiver's "
                   "interface")
UNSTOPPED = "not_stopped"
# The one command doors closed for good let through, by the sentence they say: the repair
# itself. It keeps its own guards - a recording, an EPG import, a job that holds the quit.
REPAIRS = {STUCK_UNSTOPPED: "restart_gui"}
# The ends whose own sentence names the repair already: `last_error` carries it alone.
SAID_BY_THE_HELPER = RESTORE_REASONS + (UNSTOPPED,)

# The wall clock is believed only from here on: no update helper wrote an end before
# 2026-01-01 (UTC), and a receiver that booted without a clock says a time in 1970.
PLAUSIBLE_SINCE = 1767225600

# From these phases on the package manager has touched the plugin's files (the doors close), and
# a helper that stops leaves them in a state only the next start can judge (the marker stays).
FILES_PHASES = ("installing", "restarting", "proving", "rolling_back")


def older(version):
    """Whether installing `version` is a downgrade of the running plugin (spec ae.8).

    The one rule for the dispatcher, which refuses a downgrade without its consent, and for the
    question the television and the page ask first: if the two ever judged differently, a person
    would be asked the plain question and then refused as a downgrade. Anything that is not a
    version is not older.
    """
    try:
        return trust.version_key(version) < trust.version_key(__version__)
    except (AttributeError, TypeError, ValueError):
        return False


def household_doors(updater=None):
    """What the setup screen and the page say while the doors are closed."""
    if getattr(updater, "stuck", False):
        sentence = getattr(updater, "_stuck_sentence", STUCK)
        if sentence == STUCK_UNSTOPPED:
            # The files are the previous version's and whole; only the process is not.
            return _("The update of the plugin was undone, but the receiver's user interface "
                     "did not restart. Please restart the user interface.")
        if sentence == STUCK:
            return _("The update of the plugin failed and its previous version could not be "
                     "put back. Please install the plugin again, for example from Home "
                     "Assistant.")
        # The code is back but not all of its records, or the helper stopped part-way.
        return _("The update of the plugin did not finish cleanly. Please install the plugin "
                 "again, for example from Home Assistant.")
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
        # The doors stay closed for good, saying `_stuck_sentence`: see `STUCK`.
        self.stuck = False
        self._stuck_sentence = STUCK
        self.integration = None
        self._current = None
        self._transaction = None
        self._marker_read = False
        self._container = None
        self._finished_container = None
        self._poll_ticker = Ticker(self._poll, "self-update status")
        self._retraction_ticker = Ticker(self._retraction_poll, "self-update retraction")
        self._collapse_ticker = Ticker(self._collapse, "self-update softcam collapse")
        # The relay handshake: the install waiting for the origin probe, the request waiting
        # for Home Assistant's answer, and what ended the last wait when it was not a launch
        # (for the screen and the page that asked).
        self._probe_wait = None
        self._probe_ticker = Ticker(self._probe_overdue, "self-update origin probe")
        self._relay_wait = None
        self.relay_refusal = None
        # The version that wait was for, so a sentence that names it can (`updateview.py`), and
        # when it ended on the monotonic clock, so a page read long after does not say it as
        # news (`updateview.RELAY_OUTCOME_SECONDS`).
        self.relay_version = None
        self.relay_ended = None
        self._relay_ticker = Ticker(self._relay_unanswered, "self-update relay wait")
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

    def doors_refusal(self, command=None):
        """The sentence `command` gets while the doors are closed, or None.

        None also for the one command that is the repair the closed doors name (`repair`):
        refusing the restart that ends this process would keep it stuck for no reason.
        """
        if not self.closed:
            return None
        if command is not None and command == self.repair():
            return None
        return self._stuck_sentence if self.stuck else DOORS

    def repair(self):
        """The command let through doors closed for good, or None."""
        return REPAIRS.get(self._stuck_sentence) if self.closed and self.stuck else None

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
        if marker.get("phase") == "finished":
            LOG.info("update %s finished before this start: %s", ident, marker.get("result"))
            self._end(public(marker), marker.get("reason"), followed=True)
            return
        directory = os.path.join(self.backups, "update-" + ident)
        if not same_boot:
            # The receiver lost power (or was reset) with the transaction under way: nothing of
            # it runs any more, and what is running now says how it ended.
            self._verdict(marker, "the receiver restarted")
            return
        if self._helper_gone({"directory": directory}):
            # The helper died with the files perhaps changed, and the interface restarted since
            # (review S4): the same verdict, from the build that started.
            self._verdict(marker, "the update helper stopped")
            return
        if marker.get("phase") in ("restarting", "proving"):
            self._confirm_start(directory, ident)
        self._current = {"id": ident, "directory": directory, "ours": False, "downgrade": False,
                         "launched": self.monotonic(), "finished_seen": None, "asked": True,
                         "deadline": marker.get("deadline")}
        self._transaction = public(marker)
        self._poll_ticker.start(POLL_MILLISECONDS)

    def _verdict(self, marker, cause):
        """How a transaction nothing runs any more ended, said by the build running now.

        By version and build commit: a release built again under the same number (spike S1)
        is another build, so the number alone would call the old one `installed`. `from`
        without a commit - the build before could not read its own - is judged by the number.
        A marker still at `installing` is `interrupted` whatever runs: the package manager may
        have stopped part-way, and a mix of both versions answers to either (TRANSACTION.md 4).
        """
        running = self._running()
        target = marker.get("to") if isinstance(marker.get("to"), dict) else {}
        before = marker.get("from") if isinstance(marker.get("from"), dict) else {}
        if marker.get("phase") == "installing":
            result, reason = "interrupted", "interrupted"
        elif running["version"] == target.get("version") and \
                running["commit"] == target.get("commit"):
            result, reason = "installed", None
        elif running["version"] == before.get("version") and \
                running["commit"] == (before.get("commit") or running["commit"]):
            result, reason = "rolled_back", "interrupted"
        else:
            result, reason = "interrupted", "interrupted"
        record = public(marker) or {}
        record.update(phase="finished", result=result, finished=int(self.clock()),
                      error=None if result == "installed" else
                      str(updatehelper.Fail("interrupted", detail=cause)))
        LOG.warning("update %s was cut short (%s): %s", marker.get("id"), cause, result)
        self._end(record, reason, followed=True)

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
        none, and a lower version is always refused. None also when an install at the
        television or on the page is waiting - for the origin probe it asked for, or, without
        internet, for Home Assistant's answer to its `relay_request` (`relay_wait`); how that
        wait ended, when it was not a launch, is `relay_refusal`.
        """
        self.relay_refusal = None
        self.relay_version = None
        self.relay_ended = None
        return self._request(text, origin, downgrade)

    def _request(self, text, origin, downgrade, probed=False):
        bridge = self.bridge
        if not granted(bridge.value, PERMISSION, origin):
            return Refusal(NOT_PERMITTED, "not_permitted")
        if not self.claimed:
            return Refusal(NOT_PACKAGED, "no_capability")
        refusal = self.busy()
        if refusal:
            return refusal
        uninstaller = getattr(bridge, "uninstaller", None)
        if uninstaller is not None and uninstaller.phase is not None:
            return Refusal(UNINSTALLING, "busy")
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
        lower = older(version)
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
        if relay is None and origin in (SCREEN, PAGE):
            # Who fetches: the helper, or Home Assistant for it. Decided on a fresh word about
            # the origin, looked for once when there is none (see the module).
            if not probed and not self._origin_fresh():
                return self._probe_first(text, origin, downgrade, version)
            if self._origin_unreachable():
                return self._ask_for_relay(entry, sha, origin, allowed_downgrade)
        if origin in (SCREEN, PAGE):
            started_by = origin
        else:
            started_by = "home_assistant" if relay is not None else "mqtt"
        return self._launch(entry, sha, relay, started_by, allowed_downgrade)

    def busy(self):
        """`busy`, or None: a transaction runs, or the shared lock is held and not stale.

        Asked by `cmd/update` and by `cmd/uninstall` (review M1): both end in the package
        manager and a restart, and two of them side by side end in whichever ran last. An
        install waiting for the origin probe or for Home Assistant is one of them - once a wait
        past its bound has been ended, which happens here too, timer or not.
        """
        self._end_overdue_waits()
        if (self._current is not None or self._probe_wait is not None
                or self._relay_wait is not None):
            return _refusal("busy")
        if not os.path.isdir(self.lock_dir):
            return None
        receiver = self._receiver()
        if updatehelper.lock_is_stale(receiver, self.lock_dir):
            return None
        minutes = self._minutes_left_of_a_dead_lock(receiver)
        if minutes is not None:
            return Refusal(STOPPED.format(minutes=minutes), "busy")
        return _refusal("busy")

    def _minutes_left_of_a_dead_lock(self, receiver):
        """Minutes until the lock of a provably dead self-update expires, or None (review S3).

        Proved only for a self-update's record (it has `origin`) of this boot whose pid no longer
        runs that transaction's helper. The SSH installer's record names a process that exits
        between its steps, so its pid proves nothing (TRANSACTION.md 2.2). The lock is not taken
        back here: every implementation - the released installer among them - judges it by the
        one stale rule, and the helper's own claim would refuse it just the same. Only the
        sentence changes, from "running" to what is true.
        """
        owner = self._lock_owner()
        if not isinstance(owner, dict) or owner.get("origin") is None:
            return None
        ident, pid, then = owner.get("id"), _whole(owner.get("pid")), owner.get("uptime")
        boot, now = receiver.boot_id(), receiver.uptime()
        if not boot or owner.get("boot_id") != boot or pid is None or now is None:
            return None
        if not isinstance(ident, str) or not updatehelper.TRANSACTION_ID.fullmatch(ident):
            return None
        if not isinstance(then, (int, float)) or isinstance(then, bool):
            return None
        if self._runs(pid, os.path.join(self.backups, "update-" + ident, HELPER_NAME)):
            return None
        left = updatehelper.STALE_LOCK_SECONDS - (now - then)
        return max(1, int(math.ceil(left / 60.0)))

    def _runs(self, pid, path):
        """Whether process `pid` runs `path`: one whole argument of its command line (review S7).

        Not a substring - the integration's `installer_helper.py` and another transaction's
        `helper.py` both contain the name - and not the program: every helper is python3.
        """
        try:
            with open(self._path(os.path.join("proc", str(pid), "cmdline")), "rb") as handle:
                arguments = handle.read(4096).split(b"\0")
        except OSError:
            return False
        return os.fsencode(path) in arguments

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

    def runs_release(self, version):
        """Whether this process runs the signed release `version` itself: its release build.

        What the television and the page mark as installed (spec ae.5). A release build is clean,
        of the `release` flavour and made from the index entry's commit; a development build of
        the same number is never that release, whichever of the two is newer code (v5.5), so it
        is not shown as installed next to it. A copy nobody built - every plugin up to 0.3.x -
        has no commit and compares by its number only.
        """
        if version != __version__:
            return False
        build = self.bridge.build or {}
        if not build.get("commit"):
            return True
        if build.get("flavour") != buildid.RELEASE or build.get("dirty") is not False:
            return False
        index = self._index()
        entry = next((e for e in index["releases"] if e["version"] == version), None) \
            if index is not None else None
        return entry is not None and entry.get("commit") == build["commit"]

    def _current_release(self, entry):
        """That release runs, and the build on disk is that release too."""
        if entry["version"] != __version__:
            return False
        running = self._running()["commit"]
        on_disk = buildid.read(self.bridge._build_path)
        return bool(running) and running == entry["commit"] and on_disk is not None and \
            on_disk.get("commit") == entry["commit"]

    def _relay_valid(self, relay):
        return updatehelper.relay_ok(relay, self.clock())

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
        """Ten minutes since the last transaction ended - measured on the boot (TRANSACTION.md 1).

        Many receivers boot with the clock in 1970 and jump when NTP answers, so the wall clock
        is never the only measure where the boot is known: within the boot that wrote the
        record the time since the end is its uptime now minus the record's, whatever the clock
        says. A record of another boot ended before this boot, so at least this boot's uptime
        has passed - and when that is under ten minutes, the wall clock may say more, but only
        where it can be believed both at the end and now (review round 2): otherwise every
        update in the first ten minutes after any reboot would be refused as "less than ten
        minutes ago", days after the last one. Only a record, or a receiver, without a boot id
        falls back to the wall clock alone, which then cannot go backwards into a refusal.
        """
        last = updatehelper.read_json(self._path(updatehelper.LAST))
        if not last:
            return False
        receiver = self._receiver()
        boot, uptime = receiver.boot_id(), receiver.uptime()
        ended_boot, ended = last.get("boot_id"), last.get("uptime")
        if boot and uptime is not None and isinstance(ended_boot, str) and ended_boot:
            if ended_boot != boot:
                return uptime < RATE_LIMIT_SECONDS and not self._long_ago(last)
            if isinstance(ended, (int, float)) and not isinstance(ended, bool):
                return 0 <= uptime - ended < RATE_LIMIT_SECONDS
        finished = _whole(last.get("finished"))
        if finished is None:
            return False
        now = int(self.clock())
        return finished <= now < finished + RATE_LIMIT_SECONDS

    def _long_ago(self, last):
        """Whether a believable wall clock puts the record's end ten minutes or more back.

        Believable at the end: not before `PLAUSIBLE_SINCE` - a receiver whose clock had not
        been set wrote 1970, and the distance to a clock set since would be decades. Believable
        now: not before this build was made nor before the release index it holds was issued,
        which no correct clock can be. An end later than now is a clock that moved back, and
        says nothing. Any doubt keeps the refusal: it lasts at most this boot's first ten
        minutes.
        """
        finished = _whole(last.get("finished"))
        if finished is None or finished < PLAUSIBLE_SINCE:
            return False
        now = int(self.clock())
        if now < self._clock_floor():
            return False
        return now - finished >= RATE_LIMIT_SECONDS

    def _clock_floor(self):
        """The earliest time the wall clock can truly say now."""
        floor = PLAUSIBLE_SINCE
        made = (self.bridge.build or {}).get("time")
        if _whole(made) is not None:
            floor = max(floor, made)
        index = self._index()
        issued = index.get("issued") if isinstance(index, dict) else None
        if _whole(issued) is not None:
            floor = max(floor, issued)
        return floor

    # -------------------------------------------------------- the relay handshake --

    def _origin_fresh(self):
        """Whether the origin's word is a probe's of the last ten minutes (`update.origin`)."""
        fresh = getattr(self.bridge.updates, "origin_fresh", None)
        return bool(fresh()) if fresh is not None else False

    def _origin_unreachable(self):
        """What the last look at the release origin found."""
        return getattr(self.bridge.updates, "reachability", None) == UNREACHABLE

    def _probe_first(self, text, origin, downgrade, version):
        """Look at the origin, then decide; None while the probe runs (see the module).

        The request is asked again, whole, once the probe has answered: the household may have
        changed in the meantime. On a worker that runs at once - never on a receiver, where it is
        a thread - the answer is already there when `probe` returns, and so is what came of the
        request, which is then returned as `request` returns it.
        """
        wait = {"version": version, "text": text, "origin": origin, "downgrade": bool(downgrade),
                "asked": self.monotonic(), "inline": True, "done": False, "outcome": None}
        self._probe_wait = wait
        if not self._probe_ticker.start(PROBE_WAIT_SECONDS * 1000, single=True):
            LOG.warning("no timer for the origin probe's wait; its age ends it")
        LOG.info("update to %s from the %s: looking at the release origin first", version, origin)
        try:
            self.bridge.updates.probe(lambda: self._probed(wait))
        except Exception:
            LOG.exception("could not ask for the origin probe")
            self._probed(wait)
        wait["inline"] = False
        return wait["outcome"] if wait["done"] else None

    def _probed(self, wait):
        """The probe answered, or will not: ask the request again, deciding on what is known."""
        if self._probe_wait is not wait:
            # Given up already by its bound, or the plugin stopped since.
            return
        self._probe_wait = None
        self._probe_ticker.stop()
        LOG.info("update to %s: the release origin is %s", wait["version"],
                 getattr(self.bridge.updates, "reachability", None))
        outcome = self._request(wait["text"], wait["origin"], wait["downgrade"], probed=True)
        if wait["inline"]:
            wait["done"], wait["outcome"] = True, outcome
            return
        if outcome:
            self._relay_refused(outcome, wait["version"])
        else:
            self.bridge.clear_last_error()

    def _probe_overdue(self):
        wait = self._probe_wait
        if wait is None:
            return
        LOG.warning("update to %s: the origin probe did not answer within %d s; deciding on what "
                    "is known", wait["version"], PROBE_WAIT_SECONDS)
        self._probed(wait)

    def _end_overdue_waits(self):
        """End a wait past its bound, whether or not its timer ran (review S1)."""
        now = self.monotonic()
        if self._probe_wait is not None and now - self._probe_wait["asked"] >= PROBE_WAIT_SECONDS:
            self._probe_overdue()
        if self._relay_wait is not None and now - self._relay_wait["asked"] >= RELAY_WAIT_SECONDS:
            self._relay_unanswered()

    def _ask_for_relay(self, entry, sha, origin, downgrade):
        """Ask Home Assistant for the package on `relay_request`; None while the answer is due."""
        bridge = self.bridge
        if not bridge.connected:
            LOG.warning("update to %s: the release origin is unreachable and there is no broker "
                        "to ask Home Assistant on", entry["version"])
            return Refusal(NO_RELAY, "no_relay")
        if self.integration is None:
            # Only an integration that relays publishes its word on the retained integration
            # topic: without it, nobody would answer, and two minutes of waiting say nothing.
            LOG.warning("update to %s: the release origin is unreachable and no integration has "
                        "said on %s that it is there to ask", entry["version"],
                        self.integration_topic())
            return Refusal(NO_RELAY, "no_relay")
        ident = secrets.token_hex(6)
        index = self._index()
        question = json.dumps({"id": ident, "version": entry["version"],
                               "serial": index["serial"]}, sort_keys=True)
        try:
            info = bridge.client.publish(bridge.topic(RELAY_REQUEST), question, qos=RELAY_QOS,
                                         retain=False)
        except Exception:
            LOG.exception("could not publish relay_request")
            info = None
        if info is None or getattr(info, "rc", 0) != 0:
            return Refusal(NO_RELAY, "no_relay")
        self._relay_wait = {"id": ident, "version": entry["version"], "sha256": sha,
                            "origin": origin, "downgrade": bool(downgrade),
                            "asked": self.monotonic(), "expired": False}
        if not self._relay_ticker.start(RELAY_WAIT_SECONDS * 1000, single=True):
            LOG.warning("no timer for the relay wait; its age ends it")
        LOG.warning("update to %s from the %s: the release origin is unreachable; asked Home "
                    "Assistant for the package (request %s)", entry["version"], origin, ident)
        return None

    def relay_wait(self):
        """`{"version", "seconds_left", "phase"}` while an install waits, else None.

        `phase` is `probe` while the release origin is looked at, `relay` while Home Assistant's
        answer is awaited. A wait past its bound is ended here, timer or not.
        """
        self._end_overdue_waits()
        if self._probe_wait is not None:
            wait, bound, phase = self._probe_wait, PROBE_WAIT_SECONDS, PROBING
        elif self._relay_wait is not None:
            wait, bound, phase = self._relay_wait, RELAY_WAIT_SECONDS, ASKING
        else:
            return None
        left = bound - (self.monotonic() - wait["asked"])
        return {"version": wait["version"], "seconds_left": max(0, int(math.ceil(left))),
                "phase": phase}

    def on_relay(self, text):
        """`cmd/relay`: Home Assistant's answer to this receiver's `relay_request`. Never raises.

        Only the awaited id, for the awaited version, with an address of the one shape that has
        not expired, and only inside the wait. Anything else is a line in the log and changes
        nothing: it does not end the wait, so a real answer after it is still taken. The first
        answer that passes is taken, whoever sent it (see the module: a broker client that
        answers first can deny and delay, and the helper refuses what it serves). What comes of
        an answer that is taken - the helper started, or a refusal of the table asked again - is
        said the way `cmd/update` says it, on `last_error`.
        """
        try:
            self._on_relay(text)
        except Exception:
            LOG.exception("could not take cmd/relay")

    def _on_relay(self, text):
        try:
            answer = json.loads(str(text or ""))
        except ValueError:
            answer = None
        if not isinstance(answer, dict):
            LOG.info("dropping cmd/relay: not a JSON object")
            return
        wait = self._relay_wait
        if wait is None or answer.get("id") != wait["id"]:
            LOG.info("dropping cmd/relay: this receiver is not waiting for an answer with that id")
            return
        if self.monotonic() - wait["asked"] >= RELAY_WAIT_SECONDS:
            # The wait's own timer has not had its turn yet; the answer is late all the same.
            self._relay_unanswered()
            return
        if answer.get("version") != wait["version"]:
            LOG.warning("dropping cmd/relay for request %s: it names another version", wait["id"])
            return
        relay = {"url": answer.get("url"), "expires": answer.get("expires")}
        now = self.clock()
        if not updatehelper.relay_ok(relay, now):
            if updatehelper.relay_ok(relay, 0):
                # Home Assistant's shape, expired only by this receiver's clock: Home Assistant
                # gives an address ten minutes, so the two clocks differ by more than that. The
                # wait goes on - a good answer may still come - and says so if none does.
                wait["expired"] = True
                LOG.warning("dropping cmd/relay for request %s: its address expired at %s and "
                            "this receiver's clock says %d; the two clocks differ", wait["id"],
                            relay["expires"], int(now))
                return
            LOG.warning("dropping cmd/relay for request %s: not an address Home Assistant hands "
                        "out, or expired", wait["id"])
            return
        self._relay_wait = None
        self._relay_ticker.stop()
        LOG.info("update to %s: request %s was answered", wait["version"], wait["id"])
        refusal = self._request(json.dumps({"version": wait["version"], "sha256": wait["sha256"],
                                            "relay": relay}),
                                wait["origin"], wait["downgrade"])
        if refusal:
            self._relay_refused(refusal, wait["version"])
        else:
            self.bridge.clear_last_error()

    def _relay_unanswered(self):
        wait, self._relay_wait = self._relay_wait, None
        self._relay_ticker.stop()
        if wait is None:
            return
        if wait.get("expired"):
            LOG.warning("update to %s: request %s was answered only with an address this "
                        "receiver's clock calls expired", wait["version"], wait["id"])
            refusal = Refusal(CLOCK_SKEW, "clock_skew")
        else:
            LOG.warning("update to %s: Home Assistant did not answer request %s within %d s",
                        wait["version"], wait["id"], RELAY_WAIT_SECONDS)
            refusal = Refusal(NO_RELAY, "no_relay")
        self._relay_refused(refusal, wait["version"])

    def _relay_refused(self, refusal, version):
        """A wait ended without an update: kept for the screen and the page, and on last_error."""
        self.relay_refusal = refusal
        self.relay_version = version
        self.relay_ended = self.monotonic()
        self.bridge.publish_last_error(COMMAND, refusal)

    def _origin_failed(self, record):
        """The helper could not reach the origin to download: that is the origin's word now."""
        details = record.get("record") if isinstance(record.get("record"), dict) else {}
        if record.get("result") == "installed" or details.get("origin") != UNREACHABLE:
            return
        note = getattr(self.bridge.updates, "note_unreachable", None)
        if note is None:
            return
        LOG.info("update %s could not reach the release origin; the next install at the "
                 "television asks Home Assistant", record.get("id"))
        try:
            note(fresh_for=HELPER_WORD_SECONDS)
        except Exception:
            LOG.exception("could not note the origin as unreachable")

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
                         "finished_seen": None, "asked": False, "deadline": None}
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
        # Through the one end every transaction takes, so doors a vanished transaction had
        # closed open again (review S10).
        self._end(public(record), "internal_error", followed=False)

    # ------------------------------------------------------------ following --

    def _record(self, current):
        """What the helper last wrote about this transaction: its status, the marker, the last."""
        status = updatehelper.read_json(os.path.join(current["directory"], STATUS))
        if status is not None and status.get("id") == current["id"]:
            return status
        for relative in (updatehelper.MARKER, updatehelper.LAST):
            record = updatehelper.read_json(self._path(relative))
            if record is not None and record.get("id") == current["id"]:
                return record
        return None

    def _poll(self):
        current = self._current
        if current is None:
            self._poll_ticker.stop()
            return
        directory = current["directory"]
        record = self._record(current)
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
        if record.get("phase") != "finished" and self._helper_gone(current):
            # Read once more: a helper that wrote its end and exited between the read above and
            # the look into /proc ended the way it wrote (review S6).
            again = self._record(current)
            if again is None or again.get("phase") != "finished":
                self._helper_died(current, again or record)
                return
            record = again
        payload = public(record)
        if payload is not None and payload != self._transaction:
            self._transaction = payload
            self._publish()
        phase = record.get("phase")
        if phase == "finished":
            owner = self._lock_owner()
            released = owner is None or owner.get("id") != current["id"]
            if current["finished_seen"] is None:
                current["finished_seen"] = self.monotonic()
            if released or self.monotonic() - current["finished_seen"] > RELEASE_WAIT_SECONDS:
                self._current = None
                self._poll_ticker.stop()
                details = record.get("record") if isinstance(record.get("record"), dict) else {}
                restore = details.get("restore")
                # Decided by the record alone (review round 2, DS1), not by whether a poll
                # happened to see `installing`: the helper tries to put the old files back only
                # once the package manager has run, so a restore that did not complete means
                # the files under this process changed - even when the whole failure fell
                # between two polls. A process that started on those files is not the one
                # they changed under, so only our own transaction is stuck.
                # Both of the helper's restore reasons count: `restore_failed` (`failed: ...`,
                # the code not back) and `restore_incomplete` (`partial: ...`, the code back
                # but not all of opkg's records or the settings block).
                unrestored = isinstance(restore, str) and restore != "done"
                self._origin_failed(record)
                # Review round 3: after `not_stopped` (`record.interface` `not restarted`) the
                # process R2 could not stop runs on over files that changed under it: to the
                # previous version's, or with a restore that did not complete to a mix. A
                # restore that did complete does not make it safe. Keyed on the reason and the
                # record both, so neither alone reopens the doors - and, since round 4, only for
                # a process that is one of those R2 left running (`_left_running`).
                unstopped = (record.get("reason") == UNSTOPPED
                             or details.get("interface") == "not restarted") \
                    and self._left_running(current, record, details)
                stuck = None
                if unrestored and (current["ours"] or unstopped):
                    stuck = STUCK_PARTIAL if restore.startswith("partial") else STUCK
                elif unstopped:
                    stuck = STUCK_UNSTOPPED
                self._end(payload, record.get("reason"), followed=not current["ours"],
                          stuck=stuck)
            return
        if current["ours"]:
            if phase in FILES_PHASES and not self.closed:
                self._close_doors()
            if phase == "restarting" and not current["asked"]:
                current["asked"] = True
                self._restart()
            return
        # The marker's deadline, read when the following began: `status.json` has none (S5).
        if self._past_deadline(current, self._receiver()):
            LOG.warning("update %s is past its deadline; no longer following it", current["id"])
            self._current = None
            self._poll_ticker.stop()
            self._remove_marker(current["id"])

    def _left_running(self, current, record, details):
        """Whether this process is one R2 could not stop (TRANSACTION.md 7, review round 4).

        The helper lists them as `record.unstopped`, from its last look at /proc that answered:
        this process is held when its own pid is listed, and not otherwise - a process that
        started since (after the repair, or while the helper was still finishing) runs the files
        on disk. With no list - no look answered - the pid proves nothing either way, and the
        version this process runs decides, as for a marker that outlived its helper: running
        the transaction's `from`, it runs the files that were put back.
        """
        listed = details.get("unstopped")
        if isinstance(listed, list):
            held = os.getpid() in [pid for pid in listed if _whole(pid) is not None]
            if not held:
                LOG.info("update %s: R2 could not stop %s; this process (%d) is not one of them",
                         current["id"], listed, os.getpid())
            return held
        return not self._runs_the_previous(current, record)

    def _runs_the_previous(self, current, record):
        """Whether this process runs the transaction's `from` build - the files R2 put back.

        `from` with its commit, from the marker or the transaction's own request; the status
        file has only the number, and a `from` without a commit is judged by the number, as
        `_verdict` does.
        """
        before = None
        for path in (self._path(updatehelper.MARKER),
                     os.path.join(current["directory"], REQUEST)):
            source = updatehelper.read_json(path)
            if isinstance(source, dict) and source.get("id") == current["id"] \
                    and isinstance(source.get("from"), dict):
                before = source["from"]
                break
        if before is None and isinstance(record.get("from"), str):
            before = {"version": record["from"]}
        if before is None:
            return False
        running = self._running()
        return running["version"] == before.get("version") and \
            running["commit"] == (before.get("commit") or running["commit"])

    def _helper_gone(self, current):
        """Whether the helper that `start-stop-daemon` recorded has stopped running.

        Its status file says nothing about that: a helper killed outright never writes
        `finished`, and the file's stamp moves only with a phase. The pid file does - written
        by `start-stop-daemon -m` for the process that became the helper - checked against
        `/proc`, and against the helper's own path in its command line, so a pid used again by
        another program, or by another helper, is not taken for it. No pid file, no verdict.
        """
        try:
            with open(os.path.join(current["directory"], PID_FILE), encoding="ascii") as handle:
                pid = int(handle.read().strip())
        except (OSError, ValueError, UnicodeDecodeError):
            return False
        return not self._runs(pid, os.path.join(current["directory"], HELPER_NAME))

    def _helper_died(self, current, record):
        """The helper is gone mid-way: say so, and never wait for an end it cannot write."""
        self._current = None
        self._poll_ticker.stop()
        detail = "the update helper stopped"
        touched = self._files_may_have_changed(current, record.get("phase"))
        stuck = None
        if touched and current["ours"]:
            # Review round 2 (DS2): the files changed under this process, and a helper killed
            # outright (SIGKILL, the OOM killer) leaves its package manager running as an
            # orphan that may still be writing them. A fresh session would import whatever it
            # wrote, so the doors stay closed, as after a restore that failed.
            detail += " after the package manager had started on the plugin's files"
            stuck = STUCK_STOPPED
        elif touched:
            detail += ("; the plugin's files may not be the running version's - restart the "
                       "receiver's interface, or install the plugin again")
        error = str(updatehelper.Fail("interrupted", detail=detail))
        LOG.error("update %s: %s", current["id"], error)
        payload = public(dict(record, phase="finished", result="interrupted",
                              finished=int(self.clock()), error=error))
        # With the files perhaps changed the marker stays (review S4): the next start - after
        # a reinstall, a restart or a reboot - judges it by the build that starts.
        self._end(payload, "interrupted", followed=not current["ours"], keep_marker=touched,
                  stuck=stuck)

    def _files_may_have_changed(self, current, phase):
        """Whether the package manager may have run, by what the helper left behind.

        From `installing` on, unless the helper's own records prove it never started: it
        writes the marker at `installing` before it runs the package manager, and runs nothing
        without it - so `installing` with no marker of this transaction is a helper that
        stopped before anything of the plugin changed.
        """
        if phase not in FILES_PHASES:
            return False
        if phase != "installing":
            return True
        marker = updatehelper.read_json(self._path(updatehelper.MARKER))
        return isinstance(marker, dict) and marker.get("id") == current["id"]

    def _end(self, payload, reason, followed, keep_marker=False, stuck=None):
        """Say how a transaction ended; reopen the doors with a fresh session when closed.

        Not when the files under this process changed and nothing put them back (`stuck`, the
        sentence that says which: the helper could not restore them, TRANSACTION.md 7, or it
        stopped once the package manager had started): they may then be the new release's, or
        a mix, and the fresh session a reopening starts would import them. The doors close if
        no poll had closed them yet, stay closed and say so, and the repair is a reinstall from
        outside this process - Home Assistant's forced reinstall over SSH (delta review D2).
        Nor after `not_stopped`: the files are the previous version's, whole, under a process
        that holds the other one's code, so the doors stay closed the same way, and the repair
        is the restart that ends this process (`STUCK_UNSTOPPED`, review round 3).
        """
        if payload is not None:
            self._transaction = payload
        result = (payload or {}).get("result")
        error = (payload or {}).get("error")
        ident = (payload or {}).get("id")
        LOG.warning("update %s ended: %s%s", ident, result, " (" + error + ")" if error else "")
        if not keep_marker:
            self._remove_marker(ident)
        refusal = None
        if result != "installed" and error:
            refusal = Refusal(error, reason or result or "failed")
        if stuck is not None and result != "installed":
            if not self.closed:
                # The whole change fell between two polls: the doors close now.
                self.closed = True
                self.bridge._stop_publishers()
            self.stuck = True
            self._stuck_sentence = stuck
            self._retraction_ticker.stop()
            self._queue = []
            self._outstanding = []
            if stuck == STUCK_UNSTOPPED:
                LOG.error("update %s: the previous version's files are back under this process, "
                          "which still runs the other one; the doors stay closed until the "
                          "interface restarts", ident)
            else:
                LOG.error("update %s: the plugin's files changed and were not put back; the "
                          "doors stay closed until the plugin is installed again", ident)
            # Said on `update` and `last_error` - unless a downgrade's retraction made this
            # process silent, which it stays: the page and the setup screen still say it.
            self._publish()
            text = error or result or "failed"
            if reason not in SAID_BY_THE_HELPER:
                text += "; " + stuck
            said = Refusal(text, reason or result or "failed")
            if self.bridge.connected:
                self.bridge.publish_last_error(COMMAND, said)
            else:
                # The session's own connect says it: no reload comes to carry it.
                self.bridge._pending_error = (COMMAND, said)
            return
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
        """From `installing`: the package manager is replacing the files under this process."""
        self.closed = True
        self.bridge._stop_publishers()
        LOG.warning("update %s: the plugin's files are being replaced; commands, the page and "
                    "the setup screen are closed until the update ends", self._current["id"])

    def _restart(self):
        """From `restarting`: the downgrade's retraction first, then the question."""
        if not self._current["downgrade"]:
            self._ask_restart()
            return
        # Asked before the retraction too: a withdrawn downgrade need not retract anything.
        if self._withdrawn_for_the_household():
            return
        self._begin_retraction()

    def _household_changed(self):
        """The request's standby, recording and EPG-import guards, asked again (review S2).

        The reason code the helper puts on `last_error`, or None. A recording due within ten
        minutes, or a receiver that will not say, counts as a recording - as at the request.
        """
        bridge = self.bridge
        if power.in_standby():
            return "standby"
        if recording.guard(bridge.session):
            return "recording"
        follower = bridge.publisher("epg_import")
        if follower.blocks_power() if follower is not None else epgimport.running():
            return "epg_import"
        return None

    def _withdrawn_for_the_household(self):
        reason = self._household_changed()
        if reason is None:
            return False
        LOG.warning("update %s: withdrawn before the restart (%s)", self._current["id"], reason)
        self._withdraw(reason)
        return True

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
        if self._withdrawn_for_the_household():
            return
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
        if self._relay_wait is not None:
            LOG.info("no longer waiting for Home Assistant's answer to request %s",
                     self._relay_wait["id"])
        self._relay_wait = None
        self._relay_ticker.stop()
        self._probe_wait = None
        self._probe_ticker.stop()
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
