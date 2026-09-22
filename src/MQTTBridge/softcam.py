"""Restarting the cam the image chose, and optionally doing it unasked.

The household symptom is a channel that stops decoding. Underneath it, on the
images this was written against, the image's own softcam manager starts the
**bare binary** named in `config.softcammanager.softcams_autostart` and
`/etc/init.d/softcam` is a stub whose whole body is `exit 0`. So „restart the
softcam" cannot mean „run the init script", and it cannot mean „restart the
process", because there may not be one of them.

**Why there may be several.** The manager's liveness check looks the cam up by
process name through enigma2's own `process.ProcessList().named(<basename>)`,
which compares against `/proc/<pid>/stat`'s `comm` field — and the kernel caps
`comm` at 15 characters. A cam binary whose basename is longer than that can
never be equal to its own truncated name, so the lookup always comes back empty
and the manager always takes its „Couldn't find it, start one" branch. That
branch stops nothing. It only adds, once per graphical-interface start. The
defect is therefore **conditional on the filename**: a box whose cam is named 15
characters or fewer does not accumulate copies at all, and `manager_check_on_start`
on the `softcam` topic is how a consumer tells the two kinds of receiver apart
without an SSH session on somebody else's box.

On such an image this command is not mainly „restart a frozen cam". It is
**„collapse the copies the image left behind to exactly one"**, which also fixes
a frozen cam. One sequence serves both readings.

**Counting is the part that is easy to get wrong.** At OSCam's default restart
level the cam forks a supervisor parent that keeps the worker, so a healthy
receiver shows *two* processes for *one* instance — measured: same start time to
the jiffy, the child's parent is the parent, and the pidfile names the child.
A naive process count reports a fault on a working box. `running_instances`
therefore counts matched processes **whose parent is not itself matched**.

Matching is two conditions, and both are needed. `comm` is compared against the
first 15 characters of the basename, because that is all the kernel kept — and
then `/proc/<pid>/exe` must resolve to the binary's exact path, because two
binaries differing only after character 15 truncate to the same `comm`. 🔴 What
is never used is `pgrep -f`: measured, it matched the shell that was running the
search, because that shell's own command line contained the pattern. A matcher
that can match the process doing the matching is a matcher that can kill it.
Signalling resolved PIDs from `/proc` needs no name matching at all and cannot
hit a bystander.

**What this deliberately does not do.** It never cleans the cam's runtime
directory: the manager's own poller does not either, the cam rewrites its pidfile
on start, and the line the image's *manual* start screen uses would delete the
live cam log, which is the only record of what the cam did. And it never writes
the image's „skip this cam" marker file: that marker is shared and unowned, and
on these images the manager's poller is the only thing that starts the cam at
all — a marker left behind by a crash would disable it until the next graphical
restart.

Nothing here blocks the main loop and nothing sleeps. The stop is a signal, the
waiting is an `eTimer`, and the start is an `eConsoleAppContainer`.
"""

import os
import signal
import stat
import time

from . import recording
from .cam import _encrypted
from .enigma2 import Ticker, enigma_attribute, missing
from .log import get_logger
from .power import in_standby
from .service import NavPublisher, event_id

LOG = get_logger("softcam")

SOFTCAM_DIRECTORY = "/usr/softcams"
PROC_DIRECTORY = "/proc"

# Only its modification time is ever read. See `not_decoding_seconds`.
ECM_PATH = "/tmp/ecm.info"

# The kernel's cap on `/proc/<pid>/stat`'s `comm`. Not a formatting choice: it
# is the whole reason the image's own check fails and this module exists.
COMM_LENGTH = 15

# A cam name is put on a shell command line, so it is restricted rather than
# quoted: the safest quoting is a name that needs none. The image would run a
# stranger name unquoted too; that is not a reason for this plugin to.
SAFE_NAME = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._+-"

POLL_MILLISECONDS = 60000
# A poll that reads two small files per process must not show up beside the
# main-loop dispatch-delay diagnostics. Anything above this is worth a line.
SLOW_POLL_SECONDS = 0.05

# The detector's own tick. A healthy encrypted channel rewrites the ECM file
# about every 10 seconds — measured, ten advances with gaps of 9 to 11 — so
# looking more often than that would only resample the same number.
DETECTOR_MILLISECONDS = 10000

TERM_POLL_MILLISECONDS = 500
TERM_DEADLINE_SECONDS = 5.0
# The image's own code sleeps ten seconds after starting a cam, so five seconds
# alone can be early; the second recount is what makes the topic true.
SETTLE_MILLISECONDS = 5000
LATE_SETTLE_MILLISECONDS = 15000

MANUAL_INTERVAL_SECONDS = 60
AUTOHEAL_INTERVAL_SECONDS = 600

# 🔴 The manager's poller arms a one-second timer at every enigma2 start, so its
# check fires about a second after the plugin does, on a worker thread, and on an
# affected image it adds a copy. Restarting inside that window races a copy that
# is already on its way and manufactures the duplicate this feature exists to
# remove. A minute clears it with room to spare.
POST_START_SECONDS = 60

TERMINATE = getattr(signal, "SIGTERM", 15)
KILL = getattr(signal, "SIGKILL", 9)

MANUAL = "manual"
AUTOHEAL = "autoheal"

NO_TIMER = (
    "this image would not give the plugin a timer, so the softcam restart could not be "
    "carried through"
)

# 🔴 These are the *image's* start lines, not the cams' own defaults: `-b` and
# the stack limit come from the image. A restart that omits them starts a
# different process from the one the receiver would have started, which is a
# difference nobody would find until the cam behaved oddly weeks later.
STACK_LIMIT = "ulimit -s 1024;"
BACKGROUND_FAMILIES = ("oscam", "ncam")
GBOX_HELPER = "start-stop-daemon --start --quiet --background --exec /usr/bin/gbox"


# ------------------------------------------------------- the image's settings --


def _image_setting(section, name):
    """One of the *image's* own settings, or None when this image has not got it.

    Read through `Components.config` rather than out of `/etc/enigma2/settings`,
    because a setting left at its declared default is absent from that file and
    present here — which is exactly the case for the two that matter most.
    """
    try:
        from Components.config import config
    except Exception as error:  # pragma: no cover - only on a broken image
        missing("Components.config", error)
        return None
    owner = getattr(config, section, None)
    if owner is None:
        return None
    element = getattr(owner, name, None)
    if element is None:
        return None
    try:
        return element.value
    except Exception:
        LOG.debug("config.%s.%s could not be read", section, name)
        return None


def _without_the_directory(entry, directory):
    """One autostart entry as a bare name.

    🔴 The setting holds **absolute paths** — `/usr/softcams/<name>` — not bare
    names. The image's own manager begins its loop by stripping exactly that
    prefix off, and that line exists only because the prefix is there. A
    normalisation done anywhere later than here is a normalisation the rest of
    this module does not know about: `selected` would publish a path where the
    contract promises a basename, the truncated `comm` would be the first
    fifteen characters of `/usr/softcams/…` and match no process at all, and
    `manager_check_on_start` would read true on every receiver because a path is
    always longer than fifteen characters.

    Only this one prefix is removed, deliberately. An entry pointing somewhere
    else keeps its separators and is refused by `resolve`'s name guard, rather
    than being quietly reinterpreted as a name inside the softcam directory —
    which would run a different program from the one the image was told to.
    """
    prefix = str(directory).rstrip("/") + "/"
    return entry[len(prefix):] if entry.startswith(prefix) else entry


def autostart_entries(directory=SOFTCAM_DIRECTORY):
    """What the image has been told to autostart, as bare names.

    The value is a list of locations on every image measured, but images have
    spelled one entry as a bare string too, so both are accepted.
    """
    raw = _image_setting("softcammanager", "softcams_autostart")
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = str(raw).replace(",", " ").split()
    names = []
    for item in items:
        name = _without_the_directory(str(item).strip(), directory)
        if name:
            names.append(name)
    return names


def uses_the_poller():
    """Whether the image starts the cam through its manager rather than an init script.

    🟡 When `config.misc.softcams` is anything other than `"None"` the image runs
    `/etc/init.d/softcam start` instead, and a process-level restart would fight
    it. No receiver in this project has that shape to measure, so the capability
    is simply not claimed there.

    An image with no such setting at all is read as the poller shape, because
    that is the value the setting is *declared* with and an image that never
    declares it has no init-script path to fight with either.
    """
    value = _image_setting("misc", "softcams")
    return value is None or str(value) == "None"


def manager_timer_minutes():
    """The image's periodic liveness-check interval, or None when it is switched off.

    Worth publishing because of what it would do on an affected box: the default
    interval is six minutes, and on a receiver whose cam name defeats the check
    every pass adds an instance. That is a runaway somebody should be able to see
    on a dashboard before it becomes a symptom.
    """
    if not _image_setting("softcammanager", "softcamtimerenabled"):
        return None
    try:
        return int(_image_setting("softcammanager", "softcamtimer"))
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ resolution --


def resolve(name, directory=SOFTCAM_DIRECTORY):
    """The exact path of one autostart entry, or None when it is not a cam.

    The guard, in order: a name that needs no shell quoting, a path that stays
    **directly under** the softcam directory after every symlink is followed, and
    a regular file the receiver can execute. Anything else is refused, and a
    refusal here is what stops the capability being claimed at all.
    """
    if not name or any(character not in SAFE_NAME for character in name):
        return None
    candidate = os.path.join(directory, name)
    try:
        resolved = os.path.realpath(candidate)
        root = os.path.realpath(directory)
        if os.path.dirname(resolved) != root:
            return None
        status = os.stat(resolved)
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode) or not os.access(resolved, os.X_OK):
        return None
    return resolved


def start_command(name, directory=SOFTCAM_DIRECTORY):
    """The command line the *image* would use for this cam's family, or None.

    The family is the basename's lowercase prefix, and it is the family of the
    **binary**, never the protocol the cam speaks outward: a box can perfectly
    well run an OSCam that talks `cccam` to its servers, and keying on the
    protocol would start the wrong program.

    The table ends in a generic line, so every resolvable name has one today.
    The caller still checks, because a family that needs something else would be
    added here — and a receiver running it must not be offered a restart that
    starts the wrong thing.
    """
    if not name:
        return None
    path = os.path.join(directory, name)
    lowered = name.lower()
    if lowered.startswith(BACKGROUND_FAMILIES):
        return STACK_LIMIT + path + " -b"
    if lowered.startswith("gbox"):
        return STACK_LIMIT + path + ";" + GBOX_HELPER
    return STACK_LIMIT + path


# -------------------------------------------------------------------- counting --


def _process_stat(pid, proc=PROC_DIRECTORY):
    """`(comm, ppid)` for one process, or None when it has already gone.

    The command is read out of `stat` rather than out of `comm` because the
    parent's pid is in the same file, and one read is one read. It is taken
    between the first `(` and the **last** `)`, because a command may contain
    either.
    """
    try:
        with open(os.path.join(proc, pid, "stat"), encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    opened = text.find("(")
    closed = text.rfind(")")
    if opened < 0 or closed < opened:
        return None
    fields = text[closed + 1:].split()
    if len(fields) < 2:
        return None
    try:
        return text[opened + 1:closed], int(fields[1])
    except ValueError:
        return None


# What the kernel appends to `/proc/<pid>/exe` once the file behind a running
# process has been unlinked.
DELETED_SUFFIX = " (deleted)"


def _executable(pid, proc=PROC_DIRECTORY):
    """Where a process was started from, with the kernel's unlinked marker removed.

    🔴 Upgrading the cam replaces the binary, so every copy already running reads
    `…/<name> (deleted)` from that moment on. Comparing the link verbatim would
    drop exactly those processes out of the count — reporting one instance while
    two fight over the card, and leaving the collapse button unable to collapse
    them — at the one moment most likely to precede somebody pressing it.

    A file genuinely named with that suffix would be read as its unlinked twin.
    That is the lesser of the two wrong answers by a wide margin, and it is not a
    name any image ships.
    """
    try:
        link = os.readlink(os.path.join(proc, pid, "exe"))
    except OSError:
        return None
    return link[: -len(DELETED_SUFFIX)] if link.endswith(DELETED_SUFFIX) else link


def scan(command, executable, proc=PROC_DIRECTORY):
    """`{pid: parent pid}` for every process that is this binary, or None.

    None is „the count could not be taken", which the contract publishes as
    `null` — it is not the same answer as „nothing is running", and a caller
    that cannot see the processes must not go on to signal them.

    🔴 Both conditions are load-bearing. `comm` is all the kernel kept of the
    name and is compared truncated; `exe` is exact and is what tells two binaries
    apart when their first fifteen characters agree.
    """
    try:
        entries = os.listdir(proc)
    except OSError:
        LOG.debug("%s could not be listed; the instance count is unknown", proc)
        return None
    found = {}
    for entry in entries:
        if not entry.isdigit():
            continue
        read = _process_stat(entry, proc)
        if read is None:
            # It ended between the listing and the read, which is ordinary.
            continue
        if read[0] != command:
            continue
        if _executable(entry, proc) != executable:
            continue
        found[int(entry)] = read[1]
    return found


def roots(found):
    """The instances in `found`: matched processes whose parent is not matched.

    A supervisor and the worker it forked are one instance presenting as two
    processes, and an orphan whose parent was killed is one instance whose parent
    is now init. Both fall out of the same rule.
    """
    return sorted(pid for pid, parent in found.items() if parent not in found)


def signal_order(found):
    """Roots first, then the processes they supervise.

    🟡 A precaution rather than a measurement. The cam documents a restart level
    where a supervisor exists to relaunch its worker, so killing the worker first
    could be undone by it; whether an unsolicited death also triggers that was
    not tested, because testing it costs the household's decoder. If the
    assumption is wrong this order is merely redundant.
    """
    first = roots(found)
    known = set(first)
    return first + sorted(pid for pid in found if pid not in known)


def _send(pid, number):
    """The one place a signal leaves this plugin."""
    try:
        os.kill(pid, number)
        return True
    except OSError as error:
        LOG.debug("could not signal %d (%s)", pid, error)
        return False


# ------------------------------------------------------------- the decode signal --


def _ecm_modified(path):
    """The modification time of the ECM file, or None when there is not one.

    🔴 **Nothing else about this file is ever read.** It carries a card-sharing
    account identifier, the sharing server's hostname and port, and the live
    control words, none of which may reach the broker, a log line at any level, a
    diagnostic or a test fixture — not even hashed. A modification time answers
    the only question this module asks, so the file is never opened at all.

    `lstat`, and a regular file or nothing: a name that has been replaced by a
    link is not the file whose age was being measured.
    """
    try:
        status = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(status.st_mode):
        return None
    return status.st_mtime


def not_decoding_seconds(path, service_since, since, now):
    """How long the cam has written no ECM, or 0.0 when there is nothing to count from.

    Counted from the most recent of three things: the file's own modification
    time, the start of the service being watched, and `since` — the moment this
    module itself started.

    🔴 Written on the *age* of the file rather than on its existence, because the
    cam **removes** the file when it stops descrambling — measured. Age treats a
    removed file and a merely stale one identically, which is what makes absence
    safe to reason about. It is also why the caller gates on „encrypted" first:
    on a free-to-air channel absence is the normal, healthy state, and a detector
    that read it as „not decoding" would report a stuck cam on every free-to-air
    channel in the house.

    The service start is in here because a channel-hopping household must not
    accumulate „not decoding" time across services: after a zap the file is up to
    a tuning period behind through no fault of the cam.
    """
    latest = max(float(service_since or 0.0), float(since or 0.0))
    modified = _ecm_modified(path)
    if modified is not None:
        latest = max(latest, modified)
    if latest <= 0.0:
        return 0.0
    return max(0.0, now - latest)


# ------------------------------------------------------------------ the publisher --


class SoftcamPublisher(NavPublisher):
    """`softcam` — which cam the image chose, how many are running, and the restarts."""

    name = "softcam"
    # The only event needed: a zap restarts the auto-heal window.
    events = ("evStart",)

    def __init__(self, bridge=None, path=None, directory=None, proc=None):
        NavPublisher.__init__(self, bridge)
        # Resolved here rather than as default arguments, so that a test can put
        # a fake receiver under them and a reader can see there is only one copy.
        self.path = ECM_PATH if path is None else path
        self.directory = SOFTCAM_DIRECTORY if directory is None else directory
        self.proc = PROC_DIRECTORY if proc is None else proc
        self.selected = None
        self._executable = None
        self._command = None
        self._start_line = None
        self._manager_check_on_start = False

        self._started_at = 0.0
        self._since = 0.0
        self._service_since = 0.0

        self._instances = None
        self._last_restart = None
        self._last_reason = None
        self._restarts = 0
        self._restarts_date = None
        self._last_started = None

        self._busy = False
        self._phase = None
        self._deadline = 0.0
        self._reason = None
        self._container = None
        self._finished_container = None

        self._poll = Ticker(self._tick, "softcam instances")
        self._sequence = Ticker(self._advance, "softcam restart")
        self._detector = Ticker(self._look_for_a_stuck_cam, "softcam auto-heal")

    # ---------------------------------------------------------------- lifecycle --

    def start(self):
        if not self._resolve():
            return False
        self._started_at = time.monotonic()
        self._since = time.time()
        if not NavPublisher.start(self):
            # A manual restart does not need a service event; only the auto-heal
            # window reset does, and it refuses to run without one rather than
            # measuring „not decoding" across a zap it never saw.
            LOG.warning(
                "this image gives no service events, so the automatic softcam restart "
                "is unavailable; a restart asked for over the broker is unaffected"
            )
        self._recount()
        self._poll.start(POLL_MILLISECONDS)
        self._detector.start(DETECTOR_MILLISECONDS)
        return True

    def stop(self):
        self._poll.stop()
        self._sequence.stop()
        self._detector.stop()
        # Whatever is still held goes back too, callback and all: a container
        # that keeps a bound method of this publisher keeps the publisher
        # reachable from enigma2's side long after it has let go of its hooks.
        if self._finished_container is None:
            self._finished_container, self._container = self._container, None
        self._release_finished_container()
        self._busy = False
        self._phase = None
        NavPublisher.stop(self)

    def _resolve(self):
        """Work out which cam the image chose, and whether we may restart it at all.

        Every way out of here says, at `info`, what is actually true of this
        receiver. 🔴 The publisher also marks itself switched off on the way out,
        because the bridge's other branch logs „this image does not provide the
        hooks" at warning — and a receiver with no softcam configured has
        perfectly good hooks and nothing to point them at. A false warning on
        every box without a cam is a support thread waiting to happen.
        """
        self.switched_off = True
        if not uses_the_poller():
            LOG.info(
                "this image starts the softcam through its init script, not through its "
                "manager; restarting the process would fight it, so it is not offered"
            )
            return False
        names = autostart_entries(self.directory)
        if not names:
            LOG.info(
                "this receiver has no softcam set to start automatically, so there is "
                "nothing to restart"
            )
            return False
        for name in names:
            resolved = resolve(name, self.directory)
            if resolved is None:
                LOG.info("%s is not an executable under %s; ignoring it", name,
                         self.directory)
                continue
            line = start_command(name, self.directory)
            if not line:
                LOG.info("no start line is known for %s; not offering a restart", name)
                continue
            # 🟡 The first entry that resolves is „the" cam and the rest are
            # ignored. No multi-cam receiver has been measured.
            self.selected = name
            self._executable = resolved
            self._command = name[:COMM_LENGTH]
            self._start_line = line
            self._manager_check_on_start = len(name) > COMM_LENGTH
            if self._manager_check_on_start:
                LOG.info(
                    "%s is longer than %d characters, so the image's own liveness check "
                    "cannot find it and starts another copy at every interface start",
                    name, COMM_LENGTH,
                )
            self.switched_off = False
            return True
        LOG.info(
            "none of the softcams this receiver is set to start (%s) is an executable "
            "this plugin can restart", ", ".join(names)
        )
        return False

    # ------------------------------------------------------------------- state --

    def _restarts_today(self, now=None):
        """The counter, rolled over from a stored local date at read time.

        Deliberately not a midnight timer: the receiver's clock can step shortly
        after boot, and a timer armed before the step fires at the wrong moment
        while a comparison of dates simply comes out right.
        """
        today = time.localtime(time.time() if now is None else now)[:3]
        if today != self._restarts_date:
            self._restarts_date = today
            self._restarts = 0
        return self._restarts

    def _payload(self):
        return {
            "selected": self.selected,
            "running_instances": self._instances,
            "last_restart": self._last_restart,
            "last_restart_reason": self._last_reason,
            "restarts_today": self._restarts_today(),
            "manager_check_on_start": self._manager_check_on_start,
            "manager_timer_minutes": manager_timer_minutes(),
        }

    def snapshot(self):
        return {"softcam": self._payload()}

    def _recount(self):
        found = scan(self._command, self._executable, self.proc)
        self._instances = None if found is None else len(roots(found))
        return found

    def _publish_now(self):
        self._recount()
        self.publish("softcam", self._payload())

    def _tick(self):
        """The poll. `running_instances` changes without us.

        The image adds a copy at every interface start, and on most receivers the
        household can start and stop the cam from the extensions menu at any
        time, so the count is read rather than remembered. The same tick notices
        the local date rolling over, which is what keeps `restarts_today` honest
        on a box that has performed no restart for days.
        """
        started = time.monotonic()
        self._recount()
        elapsed = time.monotonic() - started
        if elapsed >= SLOW_POLL_SECONDS:
            LOG.warning("the softcam instance poll took %d ms", int(elapsed * 1000))
        self.publish("softcam", self._payload())

    def on_service_event(self, event):
        if event == event_id("evStart"):
            self._service_since = time.time()

    # ---------------------------------------------------------------- the guards --

    def _refusal(self, reason):
        """Why this restart may not happen, or None when it may.

        🔴 One guard, used identically by both paths. The asymmetry that suggests
        itself — healing in the ten minutes before a timer, on the argument that a
        recording deserves a working cam — was rejected: a restart landing close
        to a timer risks the opening seconds of the recording, and a scrambled
        recording is recoverable while a truncated one is not. So a dead cam in
        the ten minutes before a recording stays dead until the window passes,
        and that is the accepted cost rather than an oversight.
        """
        if not self.value("softcam_restart_allowed"):
            return "restarting the softcam is switched off in the plugin's settings"
        if self._busy:
            return "a softcam restart is already running"
        now = time.monotonic()
        waited = now - self._started_at
        if waited < POST_START_SECONDS:
            return (
                "the receiver has only just started and the image's own softcam check "
                "runs a moment after that; restarting now would race it. Try again in "
                + str(int(POST_START_SECONDS - waited) + 1) + " s"
            )
        if self._last_started is not None:
            since = now - self._last_started
            if reason == MANUAL and since < MANUAL_INTERVAL_SECONDS:
                return (
                    "the softcam was restarted " + str(int(since))
                    + " s ago; at most one restart a minute"
                )
            if reason != MANUAL and since < AUTOHEAL_INTERVAL_SECONDS:
                return (
                    "the softcam was restarted " + str(int(since))
                    + " s ago; at most one automatic restart every ten minutes"
                )
        return recording.guard(self.session)

    # -------------------------------------------------------------- the sequence --

    def restart(self, reason=MANUAL):
        """Collapse every instance to one. None when it began, a sentence otherwise."""
        refusal = self._refusal(reason)
        if refusal:
            return refusal
        found = self._recount()
        if found is None:
            return "the receiver will not say which processes are running; refusing to guess"
        LOG.info("restarting %s (%s): %d instance(s) running",
                 self.selected, reason, len(roots(found)))
        self._phase = "terminating"
        self._deadline = time.monotonic() + TERM_DEADLINE_SECONDS
        # 🔴 Armed *before* anything is signalled, and refused if it cannot be.
        # An eTimer cannot fire until this returns to the main loop, so there is
        # no race in doing it first — and doing it second is the one path that
        # ends with the cam stopped, nothing started, and `_busy` stuck true so
        # that every later attempt answers „a restart is already running".
        if not self._arm(TERM_POLL_MILLISECONDS):
            return NO_TIMER
        self._busy = True
        self._reason = reason
        self._last_started = time.monotonic()
        for pid in signal_order(found):
            _send(pid, TERMINATE)
        return None

    def _arm(self, milliseconds):
        """Wait for the next turn of the sequence. False when there is no timer."""
        if self._sequence.start(milliseconds, single=True):
            return True
        LOG.error("this image would not give the softcam sequence a timer; abandoning it")
        self._phase = None
        self._busy = False
        return False

    def _survivors(self):
        """What is still running mid-sequence. A list that cannot be read is empty.

        Beginning a restart at all is refused when the process list cannot be
        read, so this is the vanishing case of `/proc` going away underneath a
        sequence already in flight. Treating it as „nothing survived" lets the
        sequence finish and start one, and the settle recount then publishes
        whatever is really true — but it is said out loud, because a stop that
        could not be verified is not a stop that was verified.
        """
        found = scan(self._command, self._executable, self.proc)
        if found is None:
            LOG.warning(
                "the process list could not be read while stopping %s; "
                "carrying on to start one", self.selected
            )
            return {}
        return found

    def _advance(self):
        """One turn of the restart, driven by a timer so that nothing waits."""
        if self._phase == "terminating":
            found = self._survivors()
            if found:
                if time.monotonic() < self._deadline:
                    if not self._arm(TERM_POLL_MILLISECONDS):
                        self.report("softcam_restart", NO_TIMER)
                    return
                LOG.warning("%d %s process(es) did not stop; killing them",
                            len(found), self.selected)
                for pid in signal_order(found):
                    _send(pid, KILL)
                self._phase = "killing"
                if not self._arm(TERM_POLL_MILLISECONDS):
                    self.report("softcam_restart", NO_TIMER)
                return
            self._start_one()
            return
        if self._phase == "killing":
            found = self._survivors()
            if found:
                # Starting anyway is deliberate: one is what the receiver needs,
                # and the recount below publishes the truth rather than a hope.
                LOG.warning("%d %s process(es) survived; starting one beside them",
                            len(found), self.selected)
            self._start_one()
            return
        if self._phase == "settling":
            self._phase = "late"
            self._publish_now()
            # No refusal if this one cannot be armed: the cam is running and the
            # topic has just been republished, so the only loss is the second
            # recount, which the minute poll makes again anyway.
            self._arm(LATE_SETTLE_MILLISECONDS)
            return
        if self._phase == "late":
            self._phase = None
            self._publish_now()

    def _start_one(self):
        """Exactly one, through enigma2, with the image's own line."""
        self._busy = False
        container = self._new_container()
        if container is None:
            self._phase = None
            self.report("softcam_restart", "this image has no eConsoleAppContainer")
            return
        try:
            rejected = container.execute(self._start_line)
        except Exception as error:
            LOG.exception("could not start %s", self.selected)
            self._phase = None
            self.report("softcam_restart", type(error).__name__ + ": " + str(error))
            return
        if rejected:
            # A non-zero answer here is „the program never started", which is a
            # different failure from one that ran and did nothing.
            self._phase = None
            self.report("softcam_restart", self.selected + " could not be started")
            return
        self._last_restart = int(time.time())
        self._last_reason = self._reason
        self._restarts = self._restarts_today() + 1
        LOG.info("started one %s (%s)", self.selected, self._reason)
        self._phase = "settling"
        self._arm(SETTLE_MILLISECONDS)

    def _release_finished_container(self):
        """Take our callback off the container whose program has ended.

        Deliberately not done from inside the callback: that runs while the
        container is walking its own callback list, and a list modified mid-walk
        is a different bug on every image.
        """
        container, self._finished_container = self._finished_container, None
        if container is None:
            return
        hook = getattr(container, "appClosed", None)
        try:
            if hasattr(hook, "remove"):
                hook.remove(self._finished)
            elif hook is not None:
                hook.get().remove(self._finished)
        except Exception:
            LOG.debug("could not detach from a finished console container")

    def _new_container(self):
        self._release_finished_container()
        factory = enigma_attribute("eConsoleAppContainer")
        if factory is None:
            return None
        try:
            container = factory()
        except Exception:
            LOG.exception("could not create a console container")
            return None
        hook = getattr(container, "appClosed", None)
        try:
            if hasattr(hook, "append"):
                hook.append(self._finished)
            elif hook is not None:
                hook.get().append(self._finished)
            else:
                return None
        except Exception:
            LOG.exception("could not attach to the console container")
            return None
        # Held so that the container is not collected while the shell runs.
        self._container = container
        return container

    def _finished(self, retval=0):
        self._finished_container, self._container = self._container, None
        if retval:
            LOG.warning("the %s start line exited with %s", self.selected, retval)

    # -------------------------------------------------------------- the auto-heal --

    def _autoheal_seconds(self):
        try:
            return int(self.value("softcam_autoheal_seconds"))
        except (TypeError, ValueError):
            return None

    def _look_for_a_stuck_cam(self):
        """Opt-in, and quiet about it: a decline is a log line, never `last_error`.

        A refusal belongs on `last_error` when somebody asked for something. This
        is the plugin deciding not to act, and a household that records every
        evening would otherwise find a retained error waiting for them.
        """
        if not self.value("softcam_autoheal") or self._busy:
            return
        if self._nav is None:
            return
        if in_standby() is not False:
            return
        if _encrypted(self.session) is not True:
            # 🔴 Encrypted first, always. On a free-to-air channel the cam removes
            # the ECM file, so „no file" is the healthy state there.
            return
        seconds = self._autoheal_seconds()
        if not seconds:
            return
        stuck = not_decoding_seconds(self.path, self._service_since, self._since, time.time())
        if stuck < seconds:
            return
        refusal = self.restart(AUTOHEAL)
        if refusal:
            LOG.info("the softcam has not decoded for %d s; not restarting it: %s",
                     int(stuck), refusal)
            return
        LOG.warning("the softcam had not decoded for %d s; it is being restarted", int(stuck))
