"""A picture of what is on the television.

`grab` is a separate program, and running a separate program from the process
that draws the television is exactly the kind of thing that freezes it. So it is
never a `subprocess`: `eConsoleAppContainer` hands the command to enigma2, which
runs it on its own and calls back when it is done. Nothing waits.

Three limits, and each of them is there because of a real failure mode.

**A debounce**, because a zap through ten channels is ten `evStart` events and
one interesting picture. **A rate limit**, because a screenshot is the most
expensive thing this plugin can be asked for and the request can come from
anywhere. **A size cap**, because `screen` is retained: an oversized capture
would sit on the broker being delivered to every new subscriber forever.

It is a picture of somebody's living room television, retained on a broker.
That is a decision for whoever turns it on, which is why `off` is a setting and
why `screenshot` is `on_zap` rather than `interval` by default.
"""

import os
import time

from .enigma2 import Ticker, enigma_attribute
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("screen")

OUTPUT_PATH = "/tmp/mqttbridge.jpg"

# Quality 80 at 720 pixels wide: about 30 KB on this hardware, measured.
#
# 🔴 The output file is a positional argument and **`-o` is not the way to give
# it one** — in this utility `-o` means „the on-screen display only" and `-v`
# means „the video only". `grab -o /tmp/x.jpg` therefore works, writes the file,
# and captures a picture of the menus over a blank screen. Both layers together,
# which is what a person means by a screenshot, is the default with neither.
GRAB_BINARIES = ("/usr/bin/grab", "/usr/sbin/grab", "grab")
GRAB_ARGUMENTS = "-j 80 -r 720 "

DEBOUNCE_MILLISECONDS = 2000
MINIMUM_INTERVAL_SECONDS = 5

# Above this a capture is dropped rather than retained. A 720-pixel JPEG is
# tens of kilobytes; anything near half a megabyte is a grab that went wrong.
MAX_BYTES = 400 * 1024


def grab_binary():
    """The `grab` this image ships, or None."""
    for candidate in GRAB_BINARIES:
        if os.path.sep not in candidate:
            return candidate
        if os.access(candidate, os.X_OK):
            return candidate
    return None


class ScreenPublisher(Publisher):
    """`screen` — a JPEG, retained, and not JSON."""

    name = "screenshot"
    raw = ("screen",)

    def __init__(self, bridge=None, path=OUTPUT_PATH):
        Publisher.__init__(self, bridge)
        self.path = path
        self._container = None
        self._busy = False
        self._commanded = False
        self._last_capture = 0.0
        self._last_image = None
        self._debounce = Ticker(self._debounced, "screenshot debounce")
        self._interval = Ticker(self._on_interval, "screenshot interval")
        self._nav = None
        self._start_event = None
        self._active = False

    # ------------------------------------------------------------------ hooks --

    def start(self):
        if enigma_attribute("eConsoleAppContainer") is None:
            return False
        if grab_binary() is None:
            LOG.warning("this image has no grab utility; screenshots are unavailable")
            return False
        if self.value("screenshot") == "off":
            LOG.info("screenshots are switched off")
            return False
        self._active = True
        self._bind_zap()
        self._arm_interval()
        return True

    def stop(self):
        self._active = False
        self._debounce.stop()
        self._interval.stop()
        self._unbind_zap()

    def _bind_zap(self):
        """`evStart` is the zap; a screenshot of the channel before it is noise."""
        from .service import event_id

        session = self.session
        nav = getattr(session, "nav", None) if session is not None else None
        hook = getattr(nav, "event", None) if nav is not None else None
        if hook is None:
            return
        self._start_event = event_id("evStart")
        if self._start_event is None:
            return
        try:
            hook.append(self._on_service_event)
            self._nav = nav
        except Exception:
            LOG.debug("could not listen for zaps")

    def _unbind_zap(self):
        nav, self._nav = self._nav, None
        if nav is None:
            return
        try:
            hook = getattr(nav, "event", None)
            if hook is not None and self._on_service_event in hook:
                hook.remove(self._on_service_event)
        except Exception:
            LOG.debug("could not stop listening for zaps")

    def _on_service_event(self, event):
        try:
            if event != self._start_event:
                return
            if self.value("screenshot") != "on_zap":
                return
            # One capture for a run through ten channels, taken once the box has
            # settled on the last of them.
            self._debounce.start(DEBOUNCE_MILLISECONDS, True)
        except Exception:
            LOG.exception("the zap listener raised")

    def _arm_interval(self):
        if self.value("screenshot") != "interval":
            self._interval.stop()
            return
        seconds = max(MINIMUM_INTERVAL_SECONDS, int(self.value("screenshot_interval") or 60))
        self._interval.start(seconds * 1000)

    def _debounced(self):
        self.capture()

    def _on_interval(self):
        self.capture()

    # ---------------------------------------------------------------- capturing --

    def _in_standby(self):
        from .power import in_standby

        return bool(in_standby())

    def capture(self, commanded=False):
        """Take a picture. None when one was started, otherwise the refusal.

        „Started", not „taken": the answer comes back on `screen` when `grab`
        finishes. A command that cannot be started is refused now, and one that
        fails later says so on `last_error`.
        """
        if not commanded and self._in_standby():
            LOG.debug("in standby; not taking a screenshot nobody can see")
            return None
        if self._busy:
            return "a screenshot is already being taken"
        since = time.time() - self._last_capture
        if since < MINIMUM_INTERVAL_SECONDS:
            return (
                "a screenshot was taken " + str(int(since)) + " s ago; at most one every "
                + str(MINIMUM_INTERVAL_SECONDS) + " s"
            )
        binary = grab_binary()
        if binary is None:
            return "this image has no grab utility"
        container = self._new_container()
        if container is None:
            return "this image has no eConsoleAppContainer"
        command = binary + " " + GRAB_ARGUMENTS + self.path
        try:
            started = container.execute(command)
        except Exception as error:
            LOG.exception("could not run grab")
            return type(error).__name__ + ": " + str(error)
        if started:
            # eConsoleAppContainer answers non-zero when the program did not
            # start at all, which is a different failure from one that ran and
            # produced nothing.
            return "grab could not be started"
        self._busy = True
        self._commanded = bool(commanded)
        self._last_capture = time.time()
        return None

    def _new_container(self):
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
        # Held so that the container is not collected while grab is running.
        self._container = container
        return container

    def _finished(self, retval=0):
        """`grab` is done — read the file, publish it, and delete it."""
        self._busy = False
        self._container = None
        commanded, self._commanded = self._commanded, False
        try:
            if not self._active:
                self._remove_output()
                return
            if retval:
                LOG.warning("grab exited with %s", retval)
                self._remove_output()
                if commanded:
                    self.report("screenshot", "grab exited with code " + str(retval))
                return
            data = self._read_and_remove()
            if data is None:
                if commanded:
                    self.report("screenshot", "grab produced no image")
                return
            if len(data) > MAX_BYTES:
                message = (
                    "the capture was " + str(len(data)) + " bytes, over the "
                    + str(MAX_BYTES) + " byte limit"
                )
                if commanded:
                    self.report("screenshot", message)
                else:
                    LOG.warning(message)
                return
            self._last_image = data
            # A screenshot is also the acknowledgement of cmd/screenshot. Even
            # two byte-identical captures are two completed commands, so this
            # topic deliberately bypasses the state de-duplication used by the
            # periodic publishers.
            if self.bridge is not None:
                self.bridge.publish_raw(self.bridge.topic("screen"), data)
            LOG.info("published a %d byte screenshot", len(data))
        except Exception:
            LOG.exception("handling a finished screenshot raised")

    def _read_and_remove(self):
        try:
            with open(self.path, "rb") as handle:
                data = handle.read()
        except OSError as error:
            LOG.warning("grab wrote nothing to %s (%s)", self.path, error)
            return None
        try:
            os.remove(self.path)
        except OSError:
            LOG.debug("could not remove %s", self.path)
        if not data:
            LOG.warning("grab wrote an empty file")
            return None
        return data

    def _remove_output(self):
        try:
            os.remove(self.path)
        except OSError:
            pass

    def snapshot(self):
        # The last picture, so a broker that lost its retained store gets it
        # back without waiting for the next zap. Never a fresh capture: a
        # snapshot is published on every reconnect, and a reconnect loop would
        # otherwise run `grab` in a loop.
        if self._last_image is None:
            return {}
        return {"screen": self._last_image}
