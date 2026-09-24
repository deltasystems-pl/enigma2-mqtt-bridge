"""`cmd/uninstall`: the plugin takes itself off the receiver, and says so on the way out.

A plugin removing its own package from inside the process it runs in sounds
worse than it is. It is what the image itself does: its plugin browser runs
`opkg remove` through `eConsoleAppContainer` from the same GUI process, and does
not even restart the interface afterwards. The running code is in memory; what
goes is the files, and nothing on the way out needs one of them. That last part
is a rule rather than an accident, and it is why **every module this sequence
touches is imported when the plugin starts** — after the package is removed a
first import cannot succeed, and it would fail inside the one sequence that has
nowhere left to report a failure.

What makes a removal *correct* rather than merely possible is the order, and
the first draft of this feature had it wrong in two places. Written out, and
never blocking the main loop:

1. **Close the doors.** Every publisher lets go of its hooks and no command is
   dispatched any more. A publisher that fires after a retraction re-creates a
   retained topic that nothing will ever retract again — which is the exact
   failure this command exists to prevent, and on a receiver that probes its
   disk every minute and rebuilds its EPG grid on a timer it is not a
   theoretical one.
2. **Retract every retained topic this node owns** — the state file's list,
   which is what `cmd/reset` uses — and every command topic on which the
   dispatcher discarded somebody's retained message this session. At **QoS 1**,
   although state is QoS 0 everywhere else: a QoS 0 publish is „done" the moment
   it reaches the socket, and this sequence has to know that the broker has
   them before it removes the only thing that could send them again. A
   subscriber receives at the lower of the two QoS values, so no consumer sees
   the difference.
3. **`availability: offline`**, retained, QoS 1, the last message.
4. **Wait for every acknowledgement**, by polling from a timer, bounded. With
   more topics than paho will queue, they go out in batches as the
   acknowledgements come in.
5. **Forget and save**: the state file left behind says nothing is published.
6. **Disconnect cleanly**, which suppresses the will; `availability` stays at
   the `offline` from step 3, and the shutdown hook later finds no session.
7. **Remove the package** with `opkg remove` — no `--autoremove`, which can take
   dependencies installed as automatic, and no `--force-*`. `prerm`'s sweep is
   what makes it a real removal: the receiver's compiled copies go with the
   sources.
8. **Restart the user interface**, best effort. `TryQuitMainloop` asks a
   question with no timeout when a stream, a background job or timeshift is
   running; the plugin is already disconnected and gone from disk by then, so
   an unanswered question leaves an inert copy in memory, not a running plugin.

**A removal can fail, and a failure ends where a reset ends.** opkg holds a lock
that the image's own update check and plugin browser take too; a broker can
stop acknowledging; a connection can drop. In every case nothing further is
removed, the bridge opens a fresh session — which republishes availability, the
snapshot, the announcement and discovery, as every connect does — and
`last_error` says which step failed. The same holds for anything this code did
not foresee: an exception anywhere after the doors close is a failed step, not
a plugin left closed, silent and deaf.

**opkg's exit status is not proof, and opkg is not atomic.** `eConsoleAppContainer`
reports a child killed by a signal as exit 0, and so it does for an opkg still
running when enigma2 itself goes away. So a zero is checked against the disk:
the package's `.control` and this plugin's `plugin.py` must both be gone before
the interface is restarted. When they are not, the removal may have stopped
half way — opkg deletes files one at a time, so some of the plugin may already
be missing — and `last_error` gives the one command that puts it back whole:
`opkg install --force-reinstall enigma2-plugin-extensions-mqttbridge`.

**Only a plugin the package manager installed can ask it to remove it.** The
capability `uninstall` is claimed when opkg is on the box, knows the package,
and names this very `plugin.py` among its files; a copy unpacked by hand or
carried in a firmware image takes no such command.
"""

import os
import time

from . import epgimport, power, recording
from .enigma2 import Ticker, enigma_attribute
from .log import get_logger, redact
from .mqttclient import MAX_QUEUED_MESSAGES
from .origin import MQTT, granted

LOG = get_logger("uninstall")

PACKAGE = "enigma2-plugin-extensions-mqttbridge"
OPKG = "/usr/bin/opkg"
# `MQTTBRIDGE_UNINSTALL` only silences `prerm`'s closing advice to publish
# `cmd/reset` first, which is the wrong advice for this caller: a reset after an
# uninstall would put everything back. opkg hands its environment to the
# maintainer scripts as it is.
COMMAND_LINE = "MQTTBRIDGE_UNINSTALL=1 " + OPKG + " remove " + PACKAGE

# Where opkg keeps its database is its configuration's business, read the way
# opkg reads it: `opkg.conf` first, then the rest of `/etc/opkg/*.conf` in name
# order, a later `option` overriding an earlier one. Without a word from the
# configuration, the older and the newer default, both halves from one of them.
OPKG_CONF_DIR = "etc/opkg"
OPKG_DATABASES = ("var/lib/opkg", "usr/lib/opkg")

# The retraction and the final `offline`. See the module docstring.
TEARDOWN_QOS = 1
POLL_MILLISECONDS = 100
ACK_TIMEOUT_SECONDS = 15.0
# How many unacknowledged messages may be out at once. Half of paho's queue
# bound, so a batch can never be the publish that paho refuses.
BATCH = max(1, MAX_QUEUED_MESSAGES // 2)
# How much of opkg's last output line goes on `last_error`.
OUTPUT_LINE_LIMIT = 160

COMMAND = "uninstall"
PERMISSION = "uninstall is switched off in the plugin's settings"
WRONG_NODE = "the payload must be this receiver's node id"
NOT_PACKAGED = (
    "this plugin was not installed by the package manager, so it cannot remove itself"
)
RUNNING = "an uninstall is already running"
NO_TIMER = "this image has no eTimer, so the removal cannot be run"

TIMED_OUT = (
    "the uninstall stopped at the retraction: the broker did not acknowledge every "
    "retracted topic within " + str(int(ACK_TIMEOUT_SECONDS)) + " s; nothing was removed"
)
DROPPED = (
    "the uninstall stopped at the retraction: the connection to the broker dropped; "
    "nothing was removed"
)
REFUSED_PUBLISH = (
    "the uninstall stopped at the retraction: the broker session refused a publish; "
    "nothing was removed"
)
NO_CONTAINER = (
    "the uninstall stopped at the package removal: this image has no "
    "eConsoleAppContainer to run opkg in; the plugin is still installed"
)
# The same sentence as `cmd/restart_gui`'s, because the removal ends in the same
# restart, and a restart mid-import loses the run.
EPG_IMPORT_RUNNING = "an EPG import is running"
REINSTALL = "opkg install --force-reinstall " + PACKAGE
NOT_REMOVED = (
    "the uninstall stopped at the package removal: opkg reported success but the "
    "package is still on the receiver, and some of its files may already be gone; "
    "to put it back whole, run: " + REINSTALL
)


def _opkg_config_files(root):
    directory = os.path.join(root, OPKG_CONF_DIR)
    main = os.path.join(directory, "opkg.conf")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        names = []
    ordered = [main] + [
        os.path.join(directory, name)
        for name in names
        if name.endswith(".conf") and name != "opkg.conf"
    ]
    return [path for path in ordered if os.path.isfile(path)]


def _opkg_options(root):
    """`option name value` from the receiver's opkg configuration, the last one winning."""
    options = {}
    for path in _opkg_config_files(root):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.strip().split()
            if len(parts) >= 3 and parts[0] in ("option", "opt"):
                options[parts[1]] = parts[2]
    return options


def _under(root, configured):
    """An absolute path out of the opkg configuration, placed under `root`. None if unusable."""
    parts = [part for part in str(configured).strip().split("/") if part]
    if not parts or ".." in parts:
        return None
    return os.path.join(root, *parts)


def info_directory(root="/"):
    """Where opkg keeps one file per package: `info_dir`, or the default beside the status file."""
    options = _opkg_options(root)
    configured = options.get("info_dir")
    if configured is not None:
        return _under(root, configured)
    for base in OPKG_DATABASES:
        if os.path.isfile(os.path.join(root, base, "status")):
            return os.path.join(root, base, "info")
    return os.path.join(root, OPKG_DATABASES[0], "info")


def _listed(line):
    """The path of one `.list` line: opkg 0.6 writes `path<TAB>mode`, older ones the path alone."""
    return line.split("\t", 1)[0].strip()


def installed_by_package_manager(root="/", plugin_directory=None):
    """Whether opkg can remove *this* copy of the plugin. Why not, when it cannot.

    Four things, all read from the box: an executable opkg, its info directory
    from its own configuration, this package's `.control` there, and this
    package's `.list` naming the `plugin.py` that is running. The last one is
    what tells a packaged plugin from a copy somebody unpacked beside a package
    that happens to be installed.
    """
    opkg = _under(root, OPKG)
    if opkg is None or not os.path.isfile(opkg) or not os.access(opkg, os.X_OK):
        return False, "no executable " + OPKG
    info = info_directory(root)
    if info is None:
        return False, "the opkg configuration names an unusable info_dir"
    if not os.path.isfile(os.path.join(info, PACKAGE + ".control")):
        return False, "opkg does not know " + PACKAGE
    directory = plugin_directory or os.path.dirname(os.path.abspath(__file__))
    running = os.path.join(directory, "plugin.py")
    wanted = {os.path.normpath(running), os.path.realpath(running)}
    try:
        with open(os.path.join(info, PACKAGE + ".list"), encoding="utf-8",
                  errors="replace") as handle:
            for line in handle:
                listed = _listed(line)
                if listed and (
                    os.path.normpath(listed) in wanted or os.path.realpath(listed) in wanted
                ):
                    return True, None
    except OSError:
        return False, "the package's file list cannot be read"
    return False, "the package's file list does not name " + running


def package_gone(root="/", plugin_directory=None):
    """Whether opkg really removed the package: its `.control` and this `plugin.py` are gone.

    Asked after opkg exits 0, because a zero from `eConsoleAppContainer` is also
    what a killed opkg reports. Anything that cannot be answered is „not gone".
    """
    info = info_directory(root)
    if info is None:
        return False
    directory = plugin_directory or os.path.dirname(os.path.abspath(__file__))
    for path in (os.path.join(info, PACKAGE + ".control"), os.path.join(directory, "plugin.py")):
        if os.path.lexists(path):
            return False
    return True


class Uninstaller:
    """The permission, the guards and the ordered teardown of `cmd/uninstall`.

    One per bridge, built with it, so that nothing on the path is imported late.
    `phase` is None when nothing is happening; `closed` is what the bridge asks
    before it publishes anything or dispatches a command.
    """

    # Test seams. On a receiver the root is `/` and the plugin directory is this
    # module's own.
    root = "/"
    plugin_directory = None

    def __init__(self, bridge, clock=None):
        self.bridge = bridge
        self.claimed = False
        self.phase = None
        self.closed = False
        self._clock = clock or time.monotonic
        self._begin_ticker = Ticker(self._begin, "uninstall start")
        self._poll_ticker = Ticker(self._poll, "uninstall acknowledgements")
        self._queue = []
        self._outstanding = []
        self._deadline = None
        self._container = None
        self._finished_container = None
        self._output = ""
        self._last_line = ""

    # -------------------------------------------------------------- capability --

    def probe(self):
        """Claim the capability or not, once per start. Never raises."""
        try:
            self.claimed, why = installed_by_package_manager(
                self.root, self.plugin_directory
            )
        except Exception:
            LOG.exception("could not tell whether opkg installed this plugin")
            self.claimed, why = False, "the check raised"
        if not self.claimed:
            LOG.debug("no uninstall capability: %s", why)
        return self.claimed

    # ----------------------------------------------------------------- command --

    def request(self, text, origin=MQTT):
        """`cmd/uninstall`: the refusal, or None once the teardown is scheduled.

        Every refusal comes before anything changes. The permission is asked
        first, so a receiver at its defaults answers the same sentence whatever
        the payload says.
        """
        bridge = self.bridge
        if not granted(bridge.value, "uninstall_allowed", origin):
            return PERMISSION
        node = bridge.node_id
        if not node or str(text or "").strip() != node:
            return WRONG_NODE
        if not self.claimed:
            return NOT_PACKAGED
        # Exactly `cmd/restart_gui`'s check: the publisher knows when the block
        # has lapsed; without one, only the importer can be asked.
        follower = bridge.publisher("epg_import")
        if follower.blocks_power() if follower is not None else epgimport.running():
            return EPG_IMPORT_RUNNING
        refusal = recording.guard(bridge.session)
        if refusal:
            return refusal
        ready = power.can_quit(bridge.session)
        if ready:
            return ready
        if self.phase is not None:
            return RUNNING
        # The handler returns first, so the dispatcher's own `clear_last_error`
        # has run before the doors close; the rest is the next turn's work.
        if not self._begin_ticker.start(0, single=True):
            return NO_TIMER
        self.phase = "scheduled"
        LOG.warning("uninstall accepted from %s; the teardown starts now", origin)
        return None

    def abandon(self):
        """The interface is going away on its own; stop where we are and do nothing more."""
        if self.phase in (None, "done"):
            return
        LOG.warning("uninstall abandoned at %s: the user interface is shutting down", self.phase)
        self.phase = "abandoned"
        self._begin_ticker.stop()
        self._poll_ticker.stop()

    # ---------------------------------------------------------------- teardown --
    # Every entry point from enigma2 — the two timers and opkg's exit — goes
    # through `_guarded`. The doors are closed by then, so an exception that
    # merely reached the timer's own handler would leave a plugin that neither
    # publishes nor listens until the next restart. It is a failed step instead.

    def _begin(self):
        self._guarded(self._begin_steps)

    def _poll(self):
        self._guarded(self._poll_steps)

    def _removed(self, retval=0):
        self._guarded(self._removed_steps, retval)

    def _guarded(self, steps, *arguments):
        try:
            steps(*arguments)
        except Exception as error:
            LOG.exception("the uninstall raised at %s", self.phase)
            if self.phase in (None, "done", "abandoned"):
                return
            sentence = (
                "the uninstall stopped at " + str(self.phase) + ": an internal error ("
                + type(error).__name__ + ")"
            )
            if self.phase == "removing":
                sentence += "; if the plugin is incomplete, run: " + REINSTALL
            else:
                sentence += "; nothing was removed"
            try:
                self._fail(sentence)
            except Exception:
                LOG.exception("putting the bridge back after a failed uninstall raised")

    def _begin_steps(self):
        if self.phase != "scheduled":
            return
        bridge = self.bridge
        self.phase = "retracting"
        # 1. Close the doors, before a single retraction.
        self.closed = True
        bridge._stop_publishers()
        LOG.info("uninstall step 1: publishers stopped, command intake closed")
        if not bridge.connected:
            self._fail(DROPPED)
            return
        # 2 and 3. Every retained topic this node owns, and `offline` last.
        topics = set(bridge.state.retained_topics)
        topics.update(bridge.discarded_retained_commands())
        self._queue = [(topic, "") for topic in sorted(topics)]
        self._queue.append((bridge.topic("availability"), "offline"))
        self._outstanding = []
        self._deadline = self._clock() + ACK_TIMEOUT_SECONDS
        LOG.info("uninstall step 2: retracting %d retained topic(s) at QoS %d",
                 len(self._queue) - 1, TEARDOWN_QOS)
        if not self._feed():
            return
        # 4. Polled, never waited for on the main loop.
        if not self._poll_ticker.start(POLL_MILLISECONDS):
            self._fail(NO_TIMER)

    def _feed(self):
        """Hand paho the next batch. False when the session refused one."""
        client = self.bridge.client
        while self._queue and len(self._outstanding) < BATCH:
            topic, payload = self._queue.pop(0)
            info = client.publish(topic, payload, qos=TEARDOWN_QOS, retain=True) \
                if client is not None else None
            if info is None or getattr(info, "rc", 0) not in (0, None):
                LOG.warning("the session refused the publish to %s (rc=%s)",
                            topic, getattr(info, "rc", None))
                self._fail(REFUSED_PUBLISH)
                return False
            self._outstanding.append(info)
            if not self._queue:
                LOG.info("uninstall step 3: availability %s published as the last message",
                         payload)
        return True

    def _poll_steps(self):
        if self.phase != "retracting":
            return
        if not self.bridge.connected:
            self._fail(DROPPED)
            return
        waiting = []
        for info in self._outstanding:
            try:
                done = info.is_published()
            except Exception as error:
                LOG.warning("a teardown publish failed: %s", error)
                self._fail(REFUSED_PUBLISH)
                return
            if not done:
                waiting.append(info)
        self._outstanding = waiting
        if not self._feed():
            return
        if not self._queue and not self._outstanding:
            self._poll_ticker.stop()
            self._acknowledged()
            return
        if self._clock() > self._deadline:
            self._fail(TIMED_OUT)

    def _acknowledged(self):
        bridge = self.bridge
        LOG.info("uninstall step 4: the broker acknowledged every retraction and offline")
        # 5. The state file left in /etc/enigma2 says nothing is published.
        bridge.forget_everything_published()
        # A state file that could not be written is left saying what it said.
        # That costs nothing worth stopping for: every topic it names is already
        # empty on the broker, a reinstall republishes them on its first connect,
        # and a retraction of an empty topic is a no-op. Stopping here instead
        # would put back everything the broker has just acknowledged removing.
        if bridge.state.save(force=True):
            LOG.info("uninstall step 5: the state file is empty")
        else:
            LOG.warning("uninstall step 5: the state file could not be emptied; it still "
                        "names topics that are already retracted, which a reinstall "
                        "republishes anyway — carrying on")
        # 6. A clean disconnect suppresses the will.
        bridge.disconnect_for_uninstall()
        LOG.info("uninstall step 6: disconnected from the broker")
        # 7. The package.
        self.phase = "removing"
        container = self._new_container()
        if container is None:
            self._fail(NO_CONTAINER)
            return
        self._output = ""
        self._last_line = ""
        LOG.info("uninstall step 7: %s", COMMAND_LINE)
        try:
            rejected = container.execute(COMMAND_LINE)
        except Exception as error:
            LOG.exception("opkg could not be started")
            self._fail(self._removal_failed("could not be started (" + type(error).__name__ + ")"))
            return
        if rejected:
            self._fail(self._removal_failed("could not be started"))

    def _removal_failed(self, what):
        sentence = "the uninstall stopped at the package removal: opkg " + what
        if self._last_line:
            sentence += " (" + self._last_line + ")"
        return sentence + "; the plugin is still installed"

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
        if not _attach(container, "appClosed", self._removed):
            return None
        # `dataAvail` only. The image sends every chunk on `dataAvail` and again
        # on `stdoutAvail` or `stderrAvail`; listening on both doubles each line
        # and garbles the last one, which is what `last_error` quotes.
        _attach(container, "dataAvail", self._data)
        # Held so that the container is not collected while opkg runs.
        self._container = container
        return container

    def _data(self, data):
        """opkg's output, a line at a time, into the plugin's log."""
        if isinstance(data, (bytes, bytearray)):
            data = bytes(data).decode("utf-8", "replace")
        self._output += str(data or "")
        *lines, self._output = self._output.split("\n")
        for line in lines:
            self._line(line)

    def _line(self, line):
        line = redact(line.strip())
        if not line:
            return
        LOG.info("opkg: %s", line)
        if len(line) > OUTPUT_LINE_LIMIT:
            line = line[: OUTPUT_LINE_LIMIT - 1] + "…"
        self._last_line = line

    def _removed_steps(self, retval=0):
        # Not detached here: the container is walking its own callback list.
        self._finished_container, self._container = self._container, None
        if self._output:
            self._line(self._output)
            self._output = ""
        if self.phase != "removing":
            return
        if retval:
            LOG.error("opkg remove exited with status %s", retval)
            self._fail(self._removal_failed("exited with status " + str(retval)))
            return
        # 🔴 A zero is not proof: a signal-killed opkg — or one still running when
        # enigma2 went away — also reports 0 through the container.
        if not package_gone(self.root, self.plugin_directory):
            LOG.error("opkg remove exited 0 but the package is still installed")
            self._fail(NOT_REMOVED)
            return
        LOG.info("uninstall step 7: the package is removed")
        # 8. Best effort, and said so.
        self.phase = "done"
        error = power.quit_mainloop(self.bridge.session, power.QUIT_RESTART)
        if error:
            LOG.error(
                "uninstall step 8: the user interface could not be restarted (%s); the plugin "
                "is disconnected and removed, and goes at the next restart", error,
            )
        else:
            LOG.info(
                "uninstall step 8: user interface restart requested; if the receiver asks "
                "whether to restart, the plugin is already inert"
            )

    def _fail(self, sentence):
        """Nothing further is removed; a fresh session puts everything back."""
        LOG.error("%s", sentence)
        self._poll_ticker.stop()
        self._begin_ticker.stop()
        self._queue = []
        self._outstanding = []
        self.phase = None
        self.closed = False
        self.bridge.restart_after_failed_uninstall(COMMAND, sentence)


def _attach(container, name, function):
    hook = getattr(container, name, None)
    try:
        if hasattr(hook, "append"):
            hook.append(function)
        elif hook is not None:
            hook.get().append(function)
        else:
            return False
    except Exception:
        LOG.exception("could not attach to the console container's %s", name)
        return False
    return True
