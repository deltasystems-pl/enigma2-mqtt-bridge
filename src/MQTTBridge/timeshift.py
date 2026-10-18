"""Timeshift: whether a buffer runs, whether the picture is paused or behind live.

**The image says nothing when a buffer starts or stops.** enigma2's service starts
its timeshift recording without an event; the info bar's Python does it, from a
key, from its permanent-timeshift timer or after a zap. What the service does
send is `evSeekableStatusChanged` when playback moves into or out of the buffer,
`evStart` and `evEnd` around every service, and on OpenViX and OpenATV
`evUser + 1` when the buffer moves on to its next file; the info bar has its own
`onPlayStateChanged` list for pause and play. So the state is read again on each
of those, and on the info bar's own `activatePermanentTimeshift`,
`startTimeshift` and `stopTimeshift`, which are wrapped on the one info bar there
is. Nothing here is read on a timer: the only timer looks for the info bar, which
some images build after the plugin has started, and stops once it has it.

`since` is the image's own start of the buffer where it keeps one
(`pts_starttime`, OpenViX and OpenATV), and otherwise when this plugin first saw
the buffer running.
"""

import time

from .enigma2 import Ticker, enigma_attribute, missing, navigation
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("timeshift")

TOPIC = "timeshift"

# Looking for the info bar: every two seconds for a minute, then once a minute, as the
# bouquet context does.
BIND_MILLISECONDS = 2000
SLOW_BIND_MILLISECONDS = 60000
BIND_ATTEMPTS = 30

# The info bar's methods that start and stop a buffer without any event of the service's.
WRAPPED = ("activatePermanentTimeshift", "startTimeshift", "stopTimeshift")
MARKER = "_mqttbridge_timeshift"

INACTIVE = {"active": False, "paused": None, "behind": None, "since": None}


def infobar_class_present():
    """Whether this image has timeshift in its info bar at all - the class, wherever it lives."""
    for module, name in (("Components.Timeshift", "InfoBarTimeshift"),
                         ("Screens.InfoBarGenerics", "InfoBarTimeshift")):
        try:
            found = getattr(__import__(module, fromlist=[name]), name, None)
        except Exception:
            found = None
        if found is not None:
            return True
    return False


def _infobar():
    try:
        from Screens.InfoBar import InfoBar
    except Exception:
        return None
    return getattr(InfoBar, "instance", None)


def _events():
    """The `iPlayableService` numbers that can move the state - the ones this image has."""
    owner = enigma_attribute("iPlayableService")
    if owner is None:
        return set()
    wanted = set()
    for name in ("evStart", "evEnd", "evSeekableStatusChanged"):
        value = getattr(owner, name, None)
        if value is not None:
            wanted.add(value)
    user = getattr(owner, "evUser", None)
    if isinstance(user, int):
        wanted.add(user + 1)
    return wanted


def read_state(session, infobar, first_seen=None):
    """The `timeshift` payload as the receiver stands now."""
    nav = navigation(session)
    getter = getattr(nav, "getCurrentService", None) if nav is not None else None
    service = None
    if getter is not None:
        try:
            service = getter()
        except Exception:
            service = None
    shift = getattr(service, "timeshift", None) if service is not None else None
    buffer = None
    if callable(shift):
        try:
            buffer = shift()
        except Exception:
            buffer = None
    if buffer is None:
        return dict(INACTIVE)
    try:
        enabled = bool(buffer.isTimeshiftEnabled())
    except Exception:
        return dict(INACTIVE)
    if not enabled:
        return dict(INACTIVE)
    try:
        behind = bool(buffer.isTimeshiftActive())
    except Exception:
        behind = None
    paused = None
    if infobar is not None:
        state = getattr(infobar, "seekstate", None)
        pause = getattr(infobar, "SEEK_STATE_PAUSE", None)
        if state is not None and pause is not None:
            paused = state == pause
    since = None
    started = getattr(infobar, "pts_starttime", None) if infobar is not None else None
    try:
        if started and float(started) > 0:
            since = int(started)
    except (TypeError, ValueError):
        since = None
    if since is None and first_seen is not None:
        since = int(first_seen)
    return {"active": True, "paused": paused, "behind": behind, "since": since}


class TimeshiftPublisher(Publisher):
    """`timeshift` - claimed once the info bar is found and its hooks are on it."""

    name = TOPIC

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._nav = None
        self._wanted = set()
        self._infobar = None
        self._wrapped = []
        self._attempts = 0
        self._slow = False
        self._bind = Ticker(self._try_bind, "timeshift bind")
        self._first_seen = None
        self._payload = None

    def start(self):
        if not infobar_class_present():
            return False
        nav = navigation(self.session)
        hook = getattr(nav, "event", None) if nav is not None else None
        if hook is None:
            missing("session.nav.event")
            return False
        wanted = _events()
        if not wanted:
            return False
        try:
            hook.append(self._on_event)
        except Exception as error:
            missing("session.nav.event", error)
            return False
        self._nav = nav
        self._wanted = wanted
        if not self._try_bind():
            self._bind.start(BIND_MILLISECONDS)
        return True

    def stop(self):
        self._bind.stop()
        nav, self._nav = self._nav, None
        hook = getattr(nav, "event", None) if nav is not None else None
        try:
            if hook is not None and self._on_event in hook:
                hook.remove(self._on_event)
        except Exception:
            LOG.debug("could not detach from session.nav.event")
        self._unwrap()

    def claimed(self):
        return self._infobar is not None

    # ------------------------------------------------------------- binding --

    def _try_bind(self):
        if self._infobar is not None:
            self._bind.stop()
            return True
        infobar = _infobar()
        if infobar is None or not callable(getattr(infobar, "timeshiftEnabled", None)):
            self._not_yet()
            return False
        self._infobar = infobar
        self._bind.stop()
        self._wrap(infobar)
        listeners = getattr(infobar, "onPlayStateChanged", None)
        if isinstance(listeners, list):
            listeners.append(self._on_play_state)
        LOG.info("the info bar's timeshift is reachable; timeshift is available")
        self.refresh()
        if self.bridge is not None:
            self.bridge.announce_capabilities()
        return True

    def _not_yet(self):
        if self._slow:
            return
        self._attempts += 1
        if self._attempts < BIND_ATTEMPTS:
            return
        self._slow = True
        self._bind.start(SLOW_BIND_MILLISECONDS)
        LOG.warning("no info bar with timeshift after %d seconds; timeshift is not claimed "
                    "and it is now looked for once a minute",
                    BIND_ATTEMPTS * BIND_MILLISECONDS // 1000)

    def _wrap(self, infobar):
        for name in WRAPPED:
            original = getattr(infobar, name, None)
            if not callable(original) or getattr(original, MARKER, False):
                continue
            own = name in getattr(infobar, "__dict__", {})

            def wrapper(*arguments, _original=original, **keywords):
                try:
                    return _original(*arguments, **keywords)
                finally:
                    self._changed()

            setattr(wrapper, MARKER, True)
            try:
                setattr(infobar, name, wrapper)
            except Exception:
                LOG.debug("the info bar's %s could not be wrapped", name)
                continue
            self._wrapped.append((name, original, own, wrapper))

    def _unwrap(self):
        infobar, self._infobar = self._infobar, None
        wrapped, self._wrapped = self._wrapped, []
        if infobar is None:
            return
        for name, original, own, wrapper in wrapped:
            try:
                if getattr(infobar, "__dict__", {}).get(name) is not wrapper:
                    continue
                if own:
                    setattr(infobar, name, original)
                else:
                    delattr(infobar, name)
            except Exception:
                LOG.debug("the info bar's %s could not be put back", name)
        listeners = getattr(infobar, "onPlayStateChanged", None)
        try:
            if isinstance(listeners, list) and self._on_play_state in listeners:
                listeners.remove(self._on_play_state)
        except Exception:
            LOG.debug("could not detach from onPlayStateChanged")

    # ------------------------------------------------------------- events --

    def _on_event(self, event):
        if event in self._wanted:
            self._changed()

    def _on_play_state(self, *_state):
        self._changed()

    def _changed(self):
        try:
            self.refresh()
        except Exception:
            LOG.exception("the timeshift state could not be read")

    def read(self):
        payload = read_state(self.session, self._infobar, self._first_seen)
        if payload["active"]:
            if self._first_seen is None:
                self._first_seen = time.time()
                if payload["since"] is None:
                    payload["since"] = int(self._first_seen)
        else:
            self._first_seen = None
        return payload

    def refresh(self):
        if self._infobar is None:
            return None
        payload = self.read()
        self._payload = payload
        self.publish(TOPIC, payload)
        return payload

    def snapshot(self):
        if self._infobar is None:
            return {}
        self._payload = self.read()
        return {TOPIC: self._payload}
